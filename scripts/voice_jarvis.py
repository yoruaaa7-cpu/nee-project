"""Voice mode for OpenJarvis — talk to Jarvis, and Jarvis talks back.

Fully local pipeline:
  mic -> openwakeword ("Hey Jarvis") -> faster-whisper (speech-to-text)
      -> OpenJarvis SDK -> Kokoro (text-to-speech) -> speakers

Two modes:
  Wake-word (default if openwakeword is installed):
      Say "Hey Jarvis", wait for the ding, speak your request.
  Push-to-talk (fallback, or with --push-to-talk):
      Press Enter to talk, Enter again to send.

Usage (from the OpenJarvis source directory):
    uv run python voice_jarvis.py
    uv run python voice_jarvis.py --push-to-talk
    uv run python voice_jarvis.py --voice af_heart
    uv run python voice_jarvis.py --stt-model small

Requires (inside the OpenJarvis venv):
    uv pip install kokoro soundfile sounddevice openwakeword

First run downloads models automatically (whisper ~150 MB, Kokoro ~330 MB,
wake-word models ~15 MB).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import warnings
import webbrowser

warnings.filterwarnings("ignore")

# Bump this whenever the script changes so you can confirm your copy is current.
VERSION = "2.1"

# Answer --version without loading the heavy audio/ML deps.
if __name__ == "__main__" and "--version" in sys.argv:
    print(f"voice_jarvis {VERSION}")
    raise SystemExit(0)

MISSING_DEP_HINT = (
    "\n[voice] Missing dependency: {name}\n"
    "Install everything voice mode needs with:\n\n"
    "    uv pip install kokoro soundfile sounddevice openwakeword\n"
)


def _import_or_exit(module_name: str, pip_name: str | None = None):
    try:
        return __import__(module_name)
    except ImportError:
        print(MISSING_DEP_HINT.format(name=pip_name or module_name))
        sys.exit(1)


np = _import_or_exit("numpy")
sd = _import_or_exit("sounddevice")
sf = _import_or_exit("soundfile")

SAMPLE_RATE = 16_000          # what faster-whisper and openwakeword expect
FRAME_SAMPLES = 1280          # 80 ms frames for openwakeword
WAKE_THRESHOLD = 0.5
COMMAND_MAX_SECONDS = 12.0    # hard cap per utterance
SILENCE_STOP_SECONDS = 1.2    # stop after this much trailing silence
SPEECH_WAIT_SECONDS = 6.0     # give up if nothing said after the ding

EXIT_PHRASES = {
    "goodbye", "good bye", "quit", "exit",
    "power down", "power off", "terminate", "kill switch", "shut yourself down",
}


def start_stop_hotkey(combo_label: str = "Ctrl+Alt+J") -> None:
    """Register a global hotkey (Ctrl+Alt+J) that stops Jarvis instantly.

    Works even though the process runs hidden with no window. Windows only;
    no extra packages needed (uses the Win32 API via ctypes).
    """
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    MOD_ALT, MOD_CONTROL, VK_J, WM_HOTKEY = 0x0001, 0x0002, 0x4A, 0x0312

    def loop() -> None:
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, MOD_ALT | MOD_CONTROL, VK_J):
            return  # another instance already owns it
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            if msg.message == WM_HOTKEY:
                print(f"\n[voice] {combo_label} pressed - shutting down.")
                log_event("system", f"Stopped via hotkey ({combo_label})")
                set_status("off")
                os._exit(0)

    threading.Thread(target=loop, daemon=True).start()
    print(f"[voice] Stop hotkey armed: {combo_label}")

_SLEEP_EXACT = {
    "shut down", "shutdown", "go to sleep", "sleep", "stand down",
    "stop listening", "dismissed", "that's all",
}
_SLEEP_HINTS = ("shut down", "shutdown", "go to sleep", "stand down", "sleep")
_WAKE_HINTS = ("wake up", "listen up", "wakey", "i need you", "you there", "hello")


def _norm_phrase(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def is_sleep_command(text: str) -> bool:
    norm = _norm_phrase(text)
    if norm in _SLEEP_EXACT:
        return True
    # "jarvis shut down", "shut down jarvis", etc.
    return "jarvis" in norm and any(h in norm for h in _SLEEP_HINTS)


def is_wake_command(text: str) -> bool:
    norm = _norm_phrase(text)
    return any(h in norm for h in _WAKE_HINTS)


# ---------------------------------------------------------------------------
# Live state + dashboard server (http://localhost:8765)
# ---------------------------------------------------------------------------

STATE: dict = {
    "status": "starting",   # starting|standby|listening|thinking|speaking|asleep|off
    "log": [],
    "last_command": "",
    "last_reply": "",
    "active_project": "",
    "model": "",
    "voice": "",
    "started_at": time.time(),
}


def set_status(status: str) -> None:
    STATE["status"] = status


def log_event(kind: str, text: str) -> None:
    STATE["log"].append(
        {"t": time.strftime("%H:%M:%S"), "kind": kind, "text": str(text)[:200]}
    )
    del STATE["log"][:-40]


def start_dashboard(port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    page_path = pathlib.Path(__file__).with_name("jarvis_dashboard.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence request spam
            pass

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/state"):
                try:
                    import psutil

                    STATE["cpu"] = psutil.cpu_percent(interval=None)
                    vm = psutil.virtual_memory()
                    STATE["mem_used_gb"] = round(vm.used / 2**30, 1)
                    STATE["mem_total_gb"] = round(vm.total / 2**30, 1)
                    STATE["mem_pct"] = vm.percent
                    root = "C:\\" if sys.platform == "win32" else "/"
                    STATE["disk_pct"] = psutil.disk_usage(root).percent
                except Exception:
                    pass
                STATE["active_project"] = get_active_project()
                STATE["uptime_s"] = int(time.time() - STATE["started_at"])
                self._send(200, "application/json", json.dumps(STATE).encode())
            elif page_path.exists():
                self._send(200, "text/html; charset=utf-8", page_path.read_bytes())
            else:
                self._send(
                    404,
                    "text/plain",
                    b"jarvis_dashboard.html not found next to voice_jarvis.py",
                )

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"[voice] Dashboard live at http://localhost:{port}")
    except OSError as exc:
        print(f"[voice] Dashboard disabled ({exc})")


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def _rms(frame_int16: "np.ndarray") -> float:
    return float(np.sqrt(np.mean(frame_int16.astype(np.float64) ** 2)))


def _ding() -> None:
    """Short rising two-tone chime so you know Jarvis is listening."""
    t1 = np.linspace(0, 0.09, int(0.09 * 44100), endpoint=False)
    t2 = np.linspace(0, 0.12, int(0.12 * 44100), endpoint=False)
    tone = np.concatenate(
        [0.25 * np.sin(2 * np.pi * 880 * t1), 0.25 * np.sin(2 * np.pi * 1320 * t2)]
    ).astype("float32")
    sd.play(tone, 44100)
    sd.wait()


def _int16_to_whisper(frames: list["np.ndarray"]) -> "np.ndarray":
    if not frames:
        return np.zeros(0, dtype="float32")
    joined = np.concatenate(frames).astype("float32") / 32768.0
    return joined


# ---------------------------------------------------------------------------
# Push-to-talk mode
# ---------------------------------------------------------------------------

def record_push_to_talk() -> "np.ndarray":
    """Record from the default microphone until the user presses Enter."""
    frames: list[np.ndarray] = []

    def _callback(indata, _frames, _time, status):
        if status:
            print(f"[voice] mic status: {status}", file=sys.stderr)
        frames.append(indata[:, 0].copy())

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        callback=_callback,
    )
    with stream:
        print("[voice] Recording... press Enter when you're done speaking.")
        input()

    if not frames:
        return np.zeros(0, dtype="float32")
    return np.concatenate(frames)


# ---------------------------------------------------------------------------
# Wake-word mode
# ---------------------------------------------------------------------------

def load_wakeword_model():
    """Load openwakeword's pretrained 'Hey Jarvis' model (downloads once)."""
    import openwakeword
    from openwakeword.model import Model

    try:
        openwakeword.utils.download_models()
    except Exception:
        pass  # already downloaded, or offline — Model() will complain if truly missing

    for framework in ("onnx", "tflite"):
        try:
            model = Model(
                wakeword_models=["hey_jarvis_v0.1"],
                inference_framework=framework,
            )
            return model
        except Exception:
            continue
    raise RuntimeError("could not load the 'hey jarvis' wake-word model")


def _wake_score(prediction: dict) -> float:
    for key, score in prediction.items():
        if "jarvis" in key.lower():
            return float(score)
    return max((float(s) for s in prediction.values()), default=0.0)


def calibrate_ambient(stream) -> float:
    """Sample ~0.8s of room noise and derive a speech threshold."""
    levels = []
    for _ in range(10):
        frame, _ = stream.read(FRAME_SAMPLES)
        levels.append(_rms(frame[:, 0]))
    ambient = float(np.median(levels))
    return max(ambient * 3.5, 250.0)


def wait_for_wake(stream, oww) -> None:
    oww.reset()
    while True:
        frame, _ = stream.read(FRAME_SAMPLES)
        prediction = oww.predict(frame[:, 0])
        if _wake_score(prediction) >= WAKE_THRESHOLD:
            return


def record_command(stream, speech_threshold: float) -> "np.ndarray":
    """After the wake word: record until the speaker goes quiet."""
    frames: list[np.ndarray] = []
    started = False
    silent_for = 0.0
    waited = 0.0
    frame_seconds = FRAME_SAMPLES / SAMPLE_RATE
    total = 0.0

    while total < COMMAND_MAX_SECONDS:
        frame, _ = stream.read(FRAME_SAMPLES)
        mono = frame[:, 0]
        level = _rms(mono)
        total += frame_seconds

        if not started:
            waited += frame_seconds
            if level >= speech_threshold:
                started = True
                frames.append(mono.copy())
            elif waited >= SPEECH_WAIT_SECONDS:
                return np.zeros(0, dtype="float32")
            continue

        frames.append(mono.copy())
        if level < speech_threshold:
            silent_for += frame_seconds
            if silent_for >= SILENCE_STOP_SECONDS:
                break
        else:
            silent_for = 0.0

    return _int16_to_whisper(frames)


# ---------------------------------------------------------------------------
# Speech-to-text / text-to-speech
# ---------------------------------------------------------------------------

def transcribe(model, audio: "np.ndarray") -> str:
    if audio.size < SAMPLE_RATE // 4:  # under ~0.25s: nothing usable
        return ""
    segments, _info = model.transcribe(audio, beam_size=1, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002300-\U000023FF\U00002B00-\U00002BFF]+"
)


def clean_for_speech(text: str) -> str:
    """Strip markdown/symbols/emojis/URLs so only real words get spoken."""
    t = text
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)   # code blocks
    t = t.replace("`", "")
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)        # [text](url) -> text
    t = re.sub(r"https?://\S+", "a link", t)              # bare URLs
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)  # headers
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.MULTILINE)       # bullets
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"(?<!\w)[*_](?=\w)|(?<=\w)[*_](?!\w)", "", t)    # stray * _
    t = t.replace("*", "").replace("#", "").replace(">", " ")
    t = t.replace("&", " and ")
    t = _EMOJI_RE.sub("", t)
    t = t.replace("“", "").replace("”", "").replace("’", "'")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", ". ", t).replace("\n", " ")
    return t.strip()


def _speech_chunks(text: str, target: int = 240) -> list[str]:
    """Split into sentence-ish chunks so audio can start before the full
    reply is synthesized (feels much faster)."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, buf = [], ""
    for s in sentences:
        if len(buf) + len(s) + 1 <= target:
            buf = f"{buf} {s}".strip()
        else:
            if buf:
                chunks.append(buf)
            buf = s
    if buf:
        chunks.append(buf)
    return chunks or [text]


def speak(tts_backend, text: str, voice: str) -> None:
    """Synthesize with Kokoro and play through the default output device."""
    text = clean_for_speech(text)
    if not text:
        return
    for chunk in _speech_chunks(text):
        result = tts_backend.synthesize(chunk, voice_id=voice, output_format="wav")
        if not result.audio:
            continue
        data, sample_rate = sf.read(io.BytesIO(result.audio), dtype="float32")
        sd.play(data, sample_rate)
        sd.wait()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Fast paths — instant, no LLM involved
# ---------------------------------------------------------------------------

FAST_APPS = {
    "chrome": "chrome", "google chrome": "chrome", "browser": "chrome",
    "edge": "msedge", "notepad": "notepad", "calculator": "calc",
    "explorer": "explorer", "file explorer": "explorer", "files": "explorer",
    "task manager": "taskmgr", "terminal": "wt", "powershell": "powershell",
    "settings": "ms-settings:", "spotify": "spotify", "word": "winword",
    "excel": "excel", "vs code": "code", "vscode": "code",
}


def _win_key(vk: int, times: int = 1) -> None:
    """Tap a Windows virtual key (media/volume) via the OS, no extra deps."""
    import ctypes

    KEYEVENTF_KEYUP = 0x0002
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def try_pc_control(cleaned: str) -> str | None:
    """Instant OS controls: volume, media, lock. Windows only."""
    if sys.platform != "win32":
        return None

    VK = {
        "vol_up": 0xAF, "vol_down": 0xAE, "mute": 0xAD,
        "play": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2,
    }

    if re.search(r"\b(volume up|louder|turn it up|increase volume)\b", cleaned):
        _win_key(VK["vol_up"], 5); return "Volume up."
    if re.search(r"\b(volume down|quieter|turn it down|lower the volume|decrease volume)\b", cleaned):
        _win_key(VK["vol_down"], 5); return "Volume down."
    if re.search(r"\b(mute|unmute|silence)\b", cleaned):
        _win_key(VK["mute"]); return "Toggled mute."
    if re.search(r"\b(pause|resume|play|play music|pause music)\b", cleaned):
        _win_key(VK["play"]); return "Done."
    if re.search(r"\b(next track|next song|skip)\b", cleaned):
        _win_key(VK["next"]); return "Next track."
    if re.search(r"\b(previous track|previous song|last song|go back a song)\b", cleaned):
        _win_key(VK["prev"]); return "Previous track."
    if re.search(r"\b(lock (the )?(computer|screen|pc)|lock it)\b", cleaned):
        import ctypes
        ctypes.windll.user32.LockWorkStation(); return "Locking the computer."
    if re.search(r"\b(close|quit|kill) (.+)$", cleaned):
        m = re.search(r"\b(?:close|quit|kill) (.+)$", cleaned)
        app = m.group(1).strip().strip(".")
        exe = FAST_APPS.get(app, app.split()[0])
        if not exe.lower().endswith(".exe"):
            exe += ".exe"
        subprocess.Popen(f'taskkill /IM "{exe}" /F', shell=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Closing {app}."
    return None


def try_fast_path(text: str) -> str | None:
    """Handle common commands instantly, skipping the LLM entirely."""
    cleaned = text.strip().strip(".!?,").lower()

    pc = try_pc_control(cleaned)
    if pc is not None:
        return pc

    m = re.match(r"^(?:please\s+)?(?:open|launch|start)\s+(.+)$", cleaned)
    if m:
        target = m.group(1).strip()
        exe = FAST_APPS.get(target)
        # Only fast-path things we recognize or single-word app names;
        # anything fuzzier goes to the agent.
        if exe or len(target.split()) == 1:
            exe = exe or target
            if sys.platform == "win32":
                subprocess.Popen(f'start "" "{exe}"', shell=True)
            else:
                subprocess.Popen([exe])
            return f"Opening {target}."

    m = re.match(r"^(?:search(?:\s+the\s+web)?(?:\s+for)?|google)\s+(.+)$", cleaned)
    if m:
        q = m.group(1).strip()
        webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote(q))
        return f"Searching the web for {q}."

    return None


# ---------------------------------------------------------------------------
# Active project — lets you say "run the tests" about the folder you work in
# ---------------------------------------------------------------------------

ACTIVE_PROJECT_FILE = (
    pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home())))
    / "OpenJarvis"
    / "active_project.txt"
)


def get_active_project() -> str:
    try:
        p = ACTIVE_PROJECT_FILE.read_text(encoding="utf-8").strip()
        if p and pathlib.Path(p).is_dir():
            return p
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# Browser control — persistent, logged-in profile so Jarvis can drive any site
# ---------------------------------------------------------------------------

BROWSER_ENABLED = False
BROWSER_TOOLS = [
    "browser_navigate", "browser_click", "browser_type",
    "browser_extract", "browser_axtree", "browser_screenshot",
]


def _browser_profile_dir() -> str:
    d = (
        pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home())))
        / "OpenJarvis"
        / "browser_profile"
    )
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def enable_persistent_browser(headful: bool = True) -> None:
    """Make OpenJarvis's browser tools use a persistent, logged-in Chrome
    profile (so cookies/sessions survive) instead of a throwaway headless one."""
    import openjarvis.tools.browser as bmod

    profile = _browser_profile_dir()

    def _ensure(self):
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        kwargs = dict(
            headless=not headful,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            # Prefer the user's real Chrome — best for logins & bot-detection.
            ctx = self._playwright.chromium.launch_persistent_context(
                profile, channel="chrome", **kwargs
            )
        except Exception:
            ctx = self._playwright.chromium.launch_persistent_context(
                profile, **kwargs
            )
        self._browser = ctx  # BrowserContext also has .close()
        self._page = ctx.pages[0] if ctx.pages else ctx.new_page()

    bmod._BrowserSession._ensure_browser = _ensure


def browser_login() -> None:
    """Open the Jarvis browser profile visibly so you can log into your sites
    once. Sessions are saved and reused by all future automation."""
    enable_persistent_browser(headful=True)
    from openjarvis.tools.browser import _session

    page = _session.page
    try:
        page.goto("https://www.google.com")
    except Exception:
        pass
    print("\n" + "=" * 58)
    print("  A browser window is open using Jarvis's own profile.")
    print("  Log into any sites you want Jarvis to use (Google, etc.).")
    print("  When you're done, come back here and press Enter to save.")
    print("=" * 58)
    try:
        input()
    except EOFError:
        pass
    _session.close()
    print("[voice] Logins saved to the Jarvis browser profile.")


# ---------------------------------------------------------------------------
# The acting agent (built once, reused every turn)
# ---------------------------------------------------------------------------

_AGENT_CACHE: dict = {}


def get_agent(jarvis):
    if "agent" in _AGENT_CACHE:
        return _AGENT_CACHE["agent"]

    # config.tools.enabled may be a comma-separated string or a list
    raw = getattr(jarvis.config.tools, "enabled", "") or ""
    if isinstance(raw, str):
        tool_names = [t.strip() for t in raw.split(",") if t.strip()]
    else:
        tool_names = [str(t).strip() for t in raw if str(t).strip()]
    if not tool_names:
        tool_names = ["code_interpreter", "web_search", "file_read", "shell_exec"]

    if BROWSER_ENABLED:
        for bt in BROWSER_TOOLS:
            if bt not in tool_names:
                tool_names.append(bt)

    import openjarvis.tools  # noqa: F401
    from openjarvis.core.registry import ToolRegistry

    available = [n for n in tool_names if ToolRegistry.contains(n)]
    print(f"[voice] Tools ready: {', '.join(available) or 'none'}")

    jarvis._ensure_engine()
    from openjarvis.cli.ask import _build_tools

    model = getattr(jarvis.config.intelligence, "default_model", "") or None
    if not model:
        models = jarvis._engine.list_models()
        model = models[0] if models else "default"

    tool_objects = _build_tools(available, jarvis.config, jarvis._engine, model)

    def _approve(prompt: str) -> bool:
        print(f"  [action] {prompt}")
        log_event("action", prompt)
        return True  # auto-approve so voice commands actually execute

    # Orchestrator uses Claude's NATIVE function-calling (real tool_calls),
    # so tools actually execute. native_react's text protocol makes Claude
    # print tool-call XML as prose instead of running anything.
    from openjarvis.agents.orchestrator import OrchestratorAgent

    agent = OrchestratorAgent(
        jarvis._engine,
        model,
        tools=tool_objects,
        bus=jarvis._bus,
        max_turns=8,
        max_tokens=1024,
        interactive=True,
        confirm_callback=_approve,
    )
    _AGENT_CACHE["agent"] = agent
    return agent


# Words that mean "do something on my machine / the web" -> needs the tool
# agent. Everything else is a plain question and gets the fast chat lane.
_ACTION_VERBS = (
    "open", "close", "launch", "start", "stop", "run", "execute", "play",
    "pause", "create", "make", "write", "delete", "remove", "move", "rename",
    "copy", "install", "uninstall", "update", "download", "build", "test",
    "compile", "commit", "push", "pull", "deploy", "send", "email", "message",
    "text", "schedule", "remind", "set ", "click", "type", "navigate",
    "screenshot", "lock", "mute", "search", "google", "find", "look up",
    "browse", "go to", "check my", "list ", "show me my", "organize", "clean up",
)
# Real-time / system-specific references a chat model can't answer alone.
_TOOL_REFS = (
    "my ", "this computer", "this pc", "disk", "storage", "cpu", "memory",
    "downloads", "desktop", "folder", "directory", " file", "files",
    "installed", "running", "what time", "current time", "today's date",
    "weather", "news", "latest", "current price", "stock", "flight", "flights",
    "on my screen", "screenshot", "the web", "online",
    "website", "web site", ".com", ".org", "log in", "sign in", "checkout",
    "add to cart", "calendar", "book ", "order ", "fill out", "on the site",
)


def needs_tools(text: str) -> bool:
    t = " " + text.lower().strip() + " "
    if any(t.lstrip().startswith(v) or f" {v}" in t for v in _ACTION_VERBS):
        return True
    return any(ref in t for ref in _TOOL_REFS)


def ask_jarvis(jarvis, history: list, text: str, use_tools: bool) -> str:
    if use_tools:
        fast = try_fast_path(text)
        if fast is not None:
            return fast

    hist_block = ""
    if history:
        hist_block = (
            "\n".join(f"User: {u}\nJarvis: {a}" for u, a in history[-4:]) + "\n"
        )

    # Fast lane: plain questions/chat -> straight to Claude, no agent, no tools.
    if not (use_tools and needs_tools(text)):
        print("[voice] Quick answer")
        chat_query = (
            "You are Jarvis, a concise spoken assistant. Answer in 1-3 short "
            "sentences, natural to say out loud. Give a direct opinion when "
            "asked; don't hedge. Plain text only — no markdown, symbols, "
            "bullet points, or emojis.\n\n"
            f"{hist_block}User: {text}\nJarvis:"
        )
        return jarvis.ask(chat_query)

    # Tool lane: real actions on the machine / web.
    spoken = (
        "(Reply will be spoken aloud: when done, summarize the result in 1-3 "
        "short, natural sentences. No markdown, symbols, bullet points, or "
        "URLs. Read out only what matters, not raw page text or headers.)\n"
    )
    query = f"{spoken}{hist_block}User: {text}" if hist_block else spoken + text
    project = get_active_project()
    if project:
        query = (
            f'(Active project folder: {project} — run shell commands inside '
            f'it, e.g. cd /d "{project}" && <command>)\n' + query
        )
    try:
        agent = get_agent(jarvis)
        result = agent.run(query)
        for tr in getattr(result, "tool_results", []) or []:
            out = (getattr(tr, "content", "") or "").strip().replace("\n", " ")
            print(f"  [{getattr(tr, 'tool_name', 'tool')}] {out[:160]}")
        content = (getattr(result, "content", "") or "").strip()
        if content:
            return content
        print("[voice] Agent returned nothing; retrying as plain chat...")
    except Exception as exc:
        print(f"[voice] Tool agent failed ({exc}); answering without tools.")

    return jarvis.ask(text)


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenJarvis voice mode")
    parser.add_argument(
        "--voice",
        default="am_michael",
        help="Kokoro voice: af_heart, af_bella, am_adam, am_michael (default: am_michael)",
    )
    parser.add_argument(
        "--stt-model",
        default="base",
        help="faster-whisper model size: tiny, base, small, medium (default: base)",
    )
    parser.add_argument(
        "--push-to-talk",
        action="store_true",
        help="Use Enter-to-record instead of the 'Hey Jarvis' wake word",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8765,
        help="Port for the local dashboard (0 disables it)",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Enable browser control (persistent, logged-in Chrome profile)",
    )
    parser.add_argument(
        "--browser-login",
        action="store_true",
        help="Open the Jarvis browser to log into your sites once, then exit",
    )
    parser.add_argument(
        "--browser-headless",
        action="store_true",
        help="Run browser automation invisibly (default is a visible window)",
    )
    parser.add_argument(
        "--project",
        default="",
        help="Set the active project folder Jarvis runs commands in",
    )
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help="Chat only - don't let Jarvis run tools (shell, files, web)",
    )
    parser.add_argument(
        "--no-speak",
        action="store_true",
        help="Skip text-to-speech (voice input only)",
    )
    args = parser.parse_args()

    # One-time login flow: open the profile browser, let the user sign in, exit.
    if args.browser_login:
        browser_login()
        return

    global BROWSER_ENABLED
    if args.browser:
        BROWSER_ENABLED = True
        try:
            enable_persistent_browser(headful=not args.browser_headless)
            print("[voice] Browser control ON (persistent profile).")
        except Exception as exc:
            print(f"[voice] Browser control unavailable: {exc}")
            BROWSER_ENABLED = False

    if args.project:
        ACTIVE_PROJECT_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_PROJECT_FILE.write_text(
            str(pathlib.Path(args.project).resolve()), encoding="utf-8"
        )
    active = get_active_project()
    if active:
        print(f"[voice] Active project: {active}")

    if args.dashboard_port:
        start_dashboard(args.dashboard_port)
    start_stop_hotkey()
    STATE["voice"] = args.voice
    log_event("system", "Boot sequence started")

    print(f"[voice] Loading speech-to-text (faster-whisper '{args.stt_model}')...")
    from faster_whisper import WhisperModel

    stt = WhisperModel(args.stt_model, device="cpu", compute_type="int8")

    tts = None
    if not args.no_speak:
        print("[voice] Loading text-to-speech (Kokoro)...")
        try:
            from openjarvis.speech.kokoro_tts import KokoroTTSBackend

            tts = KokoroTTSBackend()
            if not tts.health():
                raise RuntimeError("kokoro package not installed")
        except Exception as exc:
            print(f"[voice] Kokoro unavailable ({exc}); continuing without speech output.")
            tts = None

    oww = None
    if not args.push_to_talk:
        print("[voice] Loading wake-word model ('Hey Jarvis')...")
        try:
            oww = load_wakeword_model()
        except Exception as exc:
            print(f"[voice] Wake word unavailable ({exc}).")
            print("[voice] Falling back to push-to-talk. To fix:")
            print("        uv pip install openwakeword")

    print("[voice] Connecting to Jarvis...")
    from openjarvis.sdk import Jarvis

    jarvis = Jarvis()
    STATE["model"] = getattr(jarvis.config.intelligence, "default_model", "") or "auto"

    # Resolve the brain up front and announce it loudly, so it's never a
    # mystery which model is actually answering.
    try:
        jarvis._ensure_engine()
        brain = jarvis._resolved_engine_key or "?"
    except Exception as exc:
        brain = f"unavailable ({exc})"
    STATE["engine"] = brain
    STATE["version"] = VERSION
    banner = f"v{VERSION}  BRAIN: {STATE['model']} via {brain}"
    print("=" * 56)
    print(f"  {banner}")
    if brain == "cloud":
        print("  (cloud model — fast, high quality)")
    elif brain == "ollama":
        print("  !! Using the LOCAL model, not the cloud. Check config +")
        print("     ANTHROPIC_API_KEY + that 'anthropic' is installed.")
    print("=" * 56)
    log_event("system", banner)

    history: list[tuple[str, str]] = []
    set_status("standby")
    log_event("system", "All systems online — awaiting command")

    print()
    print("=" * 56)
    if oww is not None:
        print("  Jarvis is listening.")
        print("  Say 'HEY JARVIS', wait for the ding, then speak.")
        print("  Say 'goodbye' to quit, or press Ctrl+C.")
    else:
        print("  Jarvis voice mode ready (push-to-talk).")
        print("  Press Enter to talk, Enter again to send.")
        print("  Say 'goodbye' or press Ctrl+C to quit.")
    print("=" * 56)

    try:
        if oww is not None:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
            )
            stream.start()
            speech_threshold = calibrate_ambient(stream)

            asleep = False
            while True:
                wait_for_wake(stream, oww)

                if asleep:
                    # No ding while dormant - just quietly check for a wake-up.
                    audio = record_command(stream, speech_threshold)
                    text = transcribe(stt, audio)
                    if text and is_wake_command(text):
                        asleep = False
                        set_status("standby")
                        log_event("system", "Resumed from sleep")
                        print("\n[voice] Awake again.")
                        stream.stop()
                        if tts:
                            speak(tts, "At your service.", args.voice)
                        stream.start()
                        print("[voice] Listening for 'Hey Jarvis'...")
                    else:
                        print(f"[voice] (asleep) ignored: {text or '...'}")
                    continue

                set_status("listening")
                _ding()
                print("\n[voice] Yes? (listening...)")
                audio = record_command(stream, speech_threshold)

                text = transcribe(stt, audio)
                if not text:
                    print("[voice] Didn't catch anything - say 'Hey Jarvis' and try again.")
                    set_status("standby")
                    continue
                print(f"You said: {text}")
                STATE["last_command"] = text
                log_event("user", text)

                if text.lower().strip(" .!,") in EXIT_PHRASES:
                    set_status("off")
                    log_event("system", "Shutdown by voice command")
                    if tts:
                        speak(tts, "Goodbye.", args.voice)
                    break

                if is_sleep_command(text):
                    asleep = True
                    set_status("asleep")
                    log_event("system", "Entering sleep mode")
                    print("[voice] Going to sleep. Say 'Hey Jarvis, wake up' to resume.")
                    stream.stop()
                    if tts:
                        speak(
                            tts,
                            "Going to sleep. Say, Hey Jarvis, wake up, when you need me.",
                            args.voice,
                        )
                    stream.start()
                    continue

                print("[voice] Thinking...")
                set_status("thinking")
                started_at = time.time()
                reply = ask_jarvis(jarvis, history, text, use_tools=not args.no_tools)
                print(f"Jarvis ({time.time() - started_at:.1f}s): {reply}\n")
                history.append((text, reply))
                STATE["last_reply"] = reply
                log_event("jarvis", reply)

                # Pause the mic while Jarvis speaks so it doesn't hear itself.
                stream.stop()
                set_status("speaking")
                if tts:
                    speak(tts, reply, args.voice)
                stream.start()
                set_status("standby")
                print("[voice] Listening for 'Hey Jarvis'...")
        else:
            while True:
                input("\n[voice] Press Enter to talk...")
                audio = record_push_to_talk()
                print("[voice] Transcribing...")
                text = transcribe(stt, audio)
                if not text:
                    print("[voice] Didn't catch anything - try again, a bit louder.")
                    continue
                print(f"\nYou said: {text}")

                if text.lower().strip(" .!,") in EXIT_PHRASES:
                    if tts:
                        speak(tts, "Goodbye.", args.voice)
                    break

                print("[voice] Thinking...")
                started_at = time.time()
                reply = ask_jarvis(jarvis, history, text, use_tools=not args.no_tools)
                print(f"\nJarvis ({time.time() - started_at:.1f}s): {reply}\n")
                history.append((text, reply))
                if tts:
                    speak(tts, reply, args.voice)
    except (KeyboardInterrupt, EOFError):
        print("\n[voice] Bye.")


if __name__ == "__main__":
    main()

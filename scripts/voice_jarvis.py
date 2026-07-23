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
import sys
import time
import warnings

warnings.filterwarnings("ignore")

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

EXIT_PHRASES = {"goodbye", "good bye", "quit", "exit", "stop listening"}


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


def speak(tts_backend, text: str, voice: str) -> None:
    """Synthesize with Kokoro and play through the default output device."""
    chunks = [c.strip() for c in text.split("\n\n") if c.strip()] or [text]
    for chunk in chunks:
        result = tts_backend.synthesize(chunk, voice_id=voice, output_format="wav")
        if not result.audio:
            continue
        data, sample_rate = sf.read(io.BytesIO(result.audio), dtype="float32")
        sd.play(data, sample_rate)
        sd.wait()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def ask_jarvis(jarvis, history: list, text: str) -> str:
    query = text
    if history:
        context_block = "\n".join(f"User: {u}\nJarvis: {a}" for u, a in history[-4:])
        query = (
            "Continue this spoken conversation. Keep the reply short and "
            "natural to say out loud.\n\n"
            f"{context_block}\nUser: {text}"
        )
    return jarvis.ask(query)


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
        "--no-speak",
        action="store_true",
        help="Skip text-to-speech (voice input only)",
    )
    args = parser.parse_args()

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
    history: list[tuple[str, str]] = []

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

            while True:
                wait_for_wake(stream, oww)
                _ding()
                print("\n[voice] Yes? (listening...)")
                audio = record_command(stream, speech_threshold)

                text = transcribe(stt, audio)
                if not text:
                    print("[voice] Didn't catch anything - say 'Hey Jarvis' and try again.")
                    continue
                print(f"You said: {text}")

                if text.lower().strip(" .!,") in EXIT_PHRASES:
                    if tts:
                        speak(tts, "Goodbye.", args.voice)
                    break

                print("[voice] Thinking...")
                started_at = time.time()
                reply = ask_jarvis(jarvis, history, text)
                print(f"Jarvis ({time.time() - started_at:.1f}s): {reply}\n")
                history.append((text, reply))

                # Pause the mic while Jarvis speaks so it doesn't hear itself.
                stream.stop()
                if tts:
                    speak(tts, reply, args.voice)
                stream.start()
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
                reply = ask_jarvis(jarvis, history, text)
                print(f"\nJarvis ({time.time() - started_at:.1f}s): {reply}\n")
                history.append((text, reply))
                if tts:
                    speak(tts, reply, args.voice)
    except (KeyboardInterrupt, EOFError):
        print("\n[voice] Bye.")


if __name__ == "__main__":
    main()

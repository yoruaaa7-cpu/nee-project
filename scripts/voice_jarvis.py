"""Voice mode for OpenJarvis — talk to Jarvis, and Jarvis talks back.

Fully local pipeline:
  mic -> faster-whisper (speech-to-text) -> OpenJarvis SDK -> Kokoro (text-to-speech) -> speakers

Usage (from the OpenJarvis source directory):
    uv run python voice_jarvis.py            # push-to-talk loop
    uv run python voice_jarvis.py --voice af_heart
    uv run python voice_jarvis.py --stt-model small

Controls:
    Enter  -> start recording
    Enter  -> stop recording and send
    Ctrl+C -> quit (or say "goodbye")

Requires (inside the OpenJarvis venv):
    uv pip install kokoro soundfile sounddevice

First run downloads two models automatically:
    faster-whisper "base" (~150 MB) and the Kokoro voice model (~330 MB).
"""

from __future__ import annotations

import argparse
import io
import sys
import time

MISSING_DEP_HINT = (
    "\n[voice] Missing dependency: {name}\n"
    "Install everything voice mode needs with:\n\n"
    "    uv pip install kokoro soundfile sounddevice\n"
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

SAMPLE_RATE = 16_000  # what faster-whisper expects

EXIT_PHRASES = {"goodbye", "good bye", "quit", "exit", "stop listening"}


def record_push_to_talk() -> "np.ndarray":
    """Record from the default microphone until the user presses Enter."""
    frames: list[np.ndarray] = []

    def _callback(indata, _frames, _time, status):
        if status:
            print(f"[voice] mic status: {status}", file=sys.stderr)
        frames.append(indata.copy())

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
    return np.concatenate(frames)[:, 0]


def transcribe(model, audio: "np.ndarray") -> str:
    if audio.size < SAMPLE_RATE // 4:  # under ~0.25s: nothing usable
        return ""
    segments, _info = model.transcribe(audio, beam_size=1, vad_filter=True)
    return " ".join(seg.text.strip() for seg in segments).strip()


def speak(tts_backend, text: str, voice: str) -> None:
    """Synthesize with Kokoro and play through the default output device."""
    # Kokoro handles long text fine, but keep replies snappy to synthesize
    # by splitting on paragraph breaks and playing as they're ready.
    chunks = [c.strip() for c in text.split("\n\n") if c.strip()] or [text]
    for chunk in chunks:
        result = tts_backend.synthesize(chunk, voice_id=voice, output_format="wav")
        if not result.audio:
            continue
        data, sample_rate = sf.read(io.BytesIO(result.audio), dtype="float32")
        sd.play(data, sample_rate)
        sd.wait()


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
        "--no-speak",
        action="store_true",
        help="Skip text-to-speech (voice input only)",
    )
    args = parser.parse_args()

    print("[voice] Loading speech-to-text (faster-whisper '%s')..." % args.stt_model)
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
            print(MISSING_DEP_HINT.format(name="kokoro"))
            tts = None

    print("[voice] Connecting to Jarvis...")
    from openjarvis.sdk import Jarvis

    jarvis = Jarvis()

    # Short rolling transcript so follow-up questions make sense.
    history: list[tuple[str, str]] = []

    print()
    print("=" * 56)
    print("  Jarvis voice mode ready.")
    print("  Press Enter to talk, Enter again to send.")
    print("  Say 'goodbye' or press Ctrl+C to quit.")
    print("=" * 56)

    try:
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
                farewell = "Goodbye."
                print(f"Jarvis: {farewell}")
                if tts:
                    speak(tts, farewell, args.voice)
                break

            query = text
            if history:
                context_block = "\n".join(
                    f"User: {u}\nJarvis: {a}" for u, a in history[-4:]
                )
                query = (
                    "Continue this spoken conversation. Keep the reply short and "
                    "natural to say out loud.\n\n"
                    f"{context_block}\nUser: {text}"
                )

            print("[voice] Thinking...")
            started = time.time()
            reply = jarvis.ask(query)
            print(f"\nJarvis ({time.time() - started:.1f}s): {reply}\n")

            history.append((text, reply))

            if tts:
                speak(tts, reply, args.voice)
    except (KeyboardInterrupt, EOFError):
        print("\n[voice] Bye.")


if __name__ == "__main__":
    main()

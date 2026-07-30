"""One-shot fixer: point OpenJarvis at the Claude cloud brain and prove it works.

Run from the OpenJarvis source dir:
    uv run python fix_brain.py                     # -> claude-haiku-4-5
    uv run python fix_brain.py --model claude-sonnet-4-6
    uv run python fix_brain.py --local             # revert to qwen3.5:2b (offline)

It edits ~/.openjarvis/config.toml in place (keeping a .bak), checks the API
key, and runs a timed test call so you know for certain which brain answered.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import time


def config_path() -> pathlib.Path:
    return pathlib.Path.home() / ".openjarvis" / "config.toml"


def set_key_in_section(text: str, section: str, key: str, value: str) -> str:
    """Set `key = "value"` inside [section], adding the section/key if absent."""
    lines = text.splitlines()
    out: list[str] = []
    in_section = False
    key_written = False
    section_seen = False
    header_re = re.compile(r"^\s*\[([^\]]+)\]\s*$")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")

    for line in lines:
        m = header_re.match(line)
        if m:
            # leaving the target section without having written the key -> add it
            if in_section and not key_written:
                out.append(f'{key} = "{value}"')
                key_written = True
            in_section = m.group(1) == section
            if in_section:
                section_seen = True
            out.append(line)
            continue
        if in_section and key_re.match(line):
            out.append(f'{key} = "{value}"')
            key_written = True
            continue
        out.append(line)

    if in_section and not key_written:
        out.append(f'{key} = "{value}"')
        key_written = True

    if not section_seen:
        out.append("")
        out.append(f"[{section}]")
        out.append(f'{key} = "{value}"')

    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--local", action="store_true")
    args = ap.parse_args()

    cfg = config_path()
    if not cfg.exists():
        print(f"[fail] Config not found at {cfg}. Run 'jarvis' once first.")
        sys.exit(1)

    text = cfg.read_text(encoding="utf-8")
    cfg.with_suffix(".toml.bak").write_text(text, encoding="utf-8")

    if args.local:
        engine, model = "ollama", "qwen3.5:2b"
    else:
        engine, model = "cloud", args.model

    text = set_key_in_section(text, "engine", "default", engine)
    text = set_key_in_section(text, "intelligence", "default_model", model)
    cfg.write_text(text, encoding="utf-8")
    print(f"[ok]   config set: engine={engine}, model={model}")
    print(f"       (backup at {cfg.with_suffix('.toml.bak')})")

    if not args.local:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            print("[fail] ANTHROPIC_API_KEY is NOT set for this process.")
            print("       Run:  setx ANTHROPIC_API_KEY \"sk-ant-...\"")
            print("       then open a NEW PowerShell and run this again.")
            sys.exit(1)
        print(f"[ok]   API key present (sk-ant-...{key[-4:]})")

    print("[..]   Testing the brain with a live call...")
    from openjarvis.sdk import Jarvis

    j = Jarvis()
    started = time.time()
    try:
        reply = j.ask("Reply with exactly: JARVIS ONLINE")
    except Exception as exc:
        print(f"[fail] Test call errored: {exc}")
        print("       Common causes: bad/again-not-loaded key, no billing credit,")
        print("       or model name typo. Fix and re-run.")
        sys.exit(1)
    elapsed = time.time() - started

    resolved = getattr(j, "_resolved_engine_key", "?")
    print(f"[ok]   Answered in {elapsed:.1f}s via engine '{resolved}'")
    print(f"       Reply: {reply.strip()[:120]}")
    if resolved == "cloud" and elapsed < 15:
        print("\n[done] Cloud brain is live. Restart Jarvis voice to use it:")
        print("       voice_jarvis_service.ps1 stop; voice_jarvis_service.ps1 start")
    elif args.local:
        print("\n[done] Reverted to the local model.")
    else:
        print("\n[warn] Did not resolve to the cloud engine - check the messages above.")


if __name__ == "__main__":
    main()

"""One-shot fixer: point OpenJarvis at the Claude cloud brain and prove it works.

Run from the OpenJarvis source dir:
    uv run python fix_brain.py                     # -> claude-haiku-4-5
    uv run python fix_brain.py --model claude-sonnet-4-6
    uv run python fix_brain.py --local             # revert to qwen3.5:2b (offline)

It rewrites ~/.openjarvis/config.toml cleanly (keeping a .bak): duplicate
sections are merged, duplicate keys de-duplicated (last wins), and the engine
+ model set correctly. Then it checks the API key and runs a timed test call
so you know for certain which brain answered.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import time
from collections import OrderedDict

HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
KEYLINE_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*=(.*)$")


def config_path() -> pathlib.Path:
    return pathlib.Path.home() / ".openjarvis" / "config.toml"


def normalize_and_set(text: str, updates: dict) -> str:
    """Parse a (possibly broken) TOML into sections, dedupe, apply updates.

    - Merges repeated top-level sections into one.
    - Within a section, a repeated key keeps the last value.
    - Drops in-section comments (keeps top-of-file preamble comments).
    Produces valid TOML that tomllib will accept.
    """
    preamble: list[str] = []
    sections: "OrderedDict[str, OrderedDict[str, str]]" = OrderedDict()
    cur: str | None = None

    for line in text.splitlines():
        m = HEADER_RE.match(line)
        if m:
            cur = m.group(1)
            sections.setdefault(cur, OrderedDict())
            continue
        km = KEYLINE_RE.match(line)
        if km and cur is not None:
            sections[cur][km.group(1)] = line.rstrip()
        elif cur is None and line.strip():
            preamble.append(line.rstrip())

    for (sec, key), val in updates.items():
        sections.setdefault(sec, OrderedDict())
        sections[sec][key] = f'{key} = "{val}"'

    out: list[str] = list(preamble)
    for name, keys in sections.items():
        if out and out[-1] != "":
            out.append("")
        out.append(f"[{name}]")
        out.extend(keys.values())
    return "\n".join(out).strip() + "\n"


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

    new_text = normalize_and_set(
        text,
        {
            ("engine", "default"): engine,
            ("intelligence", "default_model"): model,
        },
    )
    cfg.write_text(new_text, encoding="utf-8")
    print(f"[ok]   config rewritten cleanly: engine={engine}, model={model}")
    print(f"       (backup at {cfg.with_suffix('.toml.bak')})")

    # Confirm it now parses.
    try:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore
        with cfg.open("rb") as fh:
            tomllib.load(fh)
        print("[ok]   config.toml parses cleanly (no duplicate sections)")
    except Exception as exc:
        print(f"[fail] config still invalid: {exc}")
        sys.exit(1)

    if not args.local:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            print("[fail] ANTHROPIC_API_KEY is NOT set for this process.")
            print('       Run:  setx ANTHROPIC_API_KEY "sk-ant-..."')
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
        print("       Common causes: no billing credit on the account,")
        print("       a bad key, or a model-name typo. Fix and re-run.")
        sys.exit(1)
    elapsed = time.time() - started

    resolved = getattr(j, "_resolved_engine_key", "?")
    print(f"[ok]   Answered in {elapsed:.1f}s via engine '{resolved}'")
    print(f"       Reply: {reply.strip()[:120]}")
    if resolved == "cloud" and elapsed < 20:
        print("\n[done] Cloud brain is live. Restart Jarvis voice to use it:")
        print("       .\\voice_jarvis_service.ps1 stop")
        print("       .\\voice_jarvis_service.ps1 start")
    elif args.local:
        print("\n[done] Reverted to the local model.")
    else:
        print("\n[warn] Did not resolve to the cloud engine - check messages above.")


if __name__ == "__main__":
    main()

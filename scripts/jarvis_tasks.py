"""Jarvis task & lifestyle manager — voice-driven to-dos, reminders, routines.

Runs inside voice_jarvis.py. State persists at
%LOCALAPPDATA%/OpenJarvis/jarvis_tasks.json. Natural-language commands are
parsed by Claude into a small structured action, then executed locally.

Voice examples:
  "Jarvis, add finish the report to my list"
  "Jarvis, remind me to call mum at 6pm"
  "Jarvis, what's on my plate today?"
  "Jarvis, I finished the report"       (marks it done)
"""

from __future__ import annotations

import difflib
import json
import os
import pathlib
import time
from datetime import datetime
from typing import Callable, List, Optional


def _path() -> pathlib.Path:
    return (
        pathlib.Path(os.environ.get("LOCALAPPDATA", str(pathlib.Path.home())))
        / "OpenJarvis"
        / "jarvis_tasks.json"
    )


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {"tasks": [], "next_id": 1}


def _save(d: dict) -> None:
    try:
        _path().parent.mkdir(parents=True, exist_ok=True)
        _path().write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass


_HINTS = (
    "task", "to-do", "to do", "todo", "remind", "reminder", "my list", "my plate",
    "routine", "agenda", "errand", "chore", "my day", "on today", "add ", "mark ",
    "what do i need to do", "what's on my", "i finished", "i completed",
    "done with", "cross off", "shopping list", "schedule",
)


def looks_like_task(text: str) -> bool:
    """Light gate — decide whether it's worth asking the model to parse."""
    t = (text or "").lower()
    return any(h in t for h in _HINTS)


def _parse_when(when) -> Optional[float]:
    if not when:
        return None
    try:
        return datetime.fromisoformat(str(when)).timestamp()
    except Exception:
        return None


def _match(tasks: List[dict], text: str) -> Optional[dict]:
    open_t = [t for t in tasks if not t.get("done")]
    best, score = None, 0.0
    for t in open_t:
        r = difflib.SequenceMatcher(None, t["text"].lower(), (text or "").lower()).ratio()
        if r > score:
            score, best = r, t
    return best if score >= 0.4 else None


def _summary(d: dict) -> str:
    open_t = [t for t in d["tasks"] if not t.get("done") and t.get("kind") != "reminder"]
    rem = [
        t for t in d["tasks"]
        if not t.get("done") and t.get("kind") == "reminder" and not t.get("reminded")
    ]
    if not open_t and not rem:
        return "Your list is clear, sir. Nothing pending."
    parts = []
    if open_t:
        s = "s" if len(open_t) != 1 else ""
        parts.append(
            f"You have {len(open_t)} task{s}: " + "; ".join(t["text"] for t in open_t[:6])
        )
    if rem:
        rp = []
        for t in rem[:4]:
            ts = (
                datetime.fromtimestamp(t["remind_at"]).strftime("%I:%M %p")
                if t.get("remind_at") else "later"
            )
            rp.append(f"{t['text']} at {ts}")
        parts.append("Reminders: " + "; ".join(rp))
    return ". ".join(parts) + "."


def handle(utterance: str, ask: Callable[[str], str]) -> Optional[str]:
    """Parse a spoken task command via Claude and execute it. Returns a spoken
    reply, or None if the utterance isn't actually a task/reminder command."""
    now = datetime.now()
    prompt = (
        "You convert a spoken personal-assistant command into JSON. The current "
        f"date and time is {now.isoformat(timespec='minutes')}. "
        "Return ONLY JSON: {\"action\": one of add|list|complete|remind|none, "
        "\"text\": the task or reminder wording (or null), \"when\": an ISO-8601 "
        "datetime if the user gave a time, else null}. Rules: use 'remind' when a "
        "time is given, 'add' for a to-do with no time, 'list' to hear tasks or "
        "the day's plan, 'complete' to mark something finished, and 'none' if the "
        "message is NOT about personal tasks, reminders, to-dos, or the day's "
        "plan.\n\nMessage: " + utterance
    )
    try:
        raw = ask(prompt) or ""
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception:
        return None

    action = (obj.get("action") or "none").lower()
    text = (obj.get("text") or "").strip()
    when = obj.get("when")
    if action == "none":
        return None

    d = _load()
    if action == "add" and text:
        d["tasks"].append({
            "id": d["next_id"], "text": text, "done": False,
            "kind": "task", "created": now.isoformat(),
        })
        d["next_id"] += 1
        _save(d)
        return f"Added to your list, sir: {text}."

    if action == "remind" and text:
        ep = _parse_when(when)
        d["tasks"].append({
            "id": d["next_id"], "text": text, "done": False, "kind": "reminder",
            "remind_at": ep, "reminded": False, "created": now.isoformat(),
        })
        d["next_id"] += 1
        _save(d)
        whenstr = (
            datetime.fromtimestamp(ep).strftime("%I:%M %p") if ep else "later"
        )
        return f"I'll remind you to {text} at {whenstr}, sir."

    if action == "complete":
        m = _match(d["tasks"], text)
        if m:
            m["done"] = True
            _save(d)
            return f"Marked done, sir: {m['text']}."
        return "I couldn't find that one on your list, sir."

    if action == "list":
        return _summary(d)

    return None


def pop_due_reminders() -> List[str]:
    """Return reminder texts that have come due (and mark them fired). Called
    by the voice loop between listen cycles so Jarvis announces them aloud."""
    d = _load()
    now = time.time()
    due = []
    changed = False
    for t in d["tasks"]:
        if (
            t.get("kind") == "reminder" and not t.get("reminded")
            and t.get("remind_at") and t["remind_at"] <= now
        ):
            t["reminded"] = True
            due.append(t["text"])
            changed = True
    if changed:
        _save(d)
    return due


def open_count() -> int:
    d = _load()
    return sum(1 for t in d["tasks"] if not t.get("done"))

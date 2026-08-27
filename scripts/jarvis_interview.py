"""Jarvis interview mode — spoken SOC / Security-Analyst mock interviews.

Reworked from the standalone 'jarvis-interviewer' project to run *inside*
voice_jarvis.py, reusing its voice pipeline. voice_jarvis calls run_interview()
with injected speak/listen/ask callables so there's no separate process, venv,
or console-script trampoline to break.

Design (unchanged from the original):
  - phases: warm-up -> technical -> scenario -> behavioural -> your questions
  - probes weak/vague answers with one follow-up, like a real interviewer
  - straight face during; a full scored debrief only at the end
  - answers scored 1-5 (technical/structure/specificity/communication)
  - weak categories are tracked across sessions and biased into later ones
  - a markdown debrief is saved per session
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional


# ---------------------------------------------------------------------------
# Question bank (phase, category, question)
# ---------------------------------------------------------------------------

QUESTIONS = [
    ("warm-up", "motivation",
     "To start, tell me a bit about yourself and what draws you to a security "
     "analyst role."),

    ("technical", "networking",
     "Walk me through the TCP three-way handshake, and tell me how a SYN flood "
     "abuses it."),
    ("technical", "networking",
     "What's the difference between TCP and UDP, and when might an attacker "
     "prefer one over the other?"),
    ("technical", "cryptography",
     "Explain the difference between symmetric and asymmetric encryption, and "
     "where each is used in practice."),
    ("technical", "identity",
     "At a high level, how does Kerberos authentication work, and what is a "
     "golden ticket attack?"),
    ("technical", "linux",
     "On a Linux server you suspect an SSH brute-force attempt. Which logs do "
     "you check, and what specifically are you looking for?"),
    ("technical", "windows",
     "What do Windows event IDs 4624 and 4625 represent, and how would you use "
     "them during an investigation?"),
    ("technical", "concepts",
     "Explain the difference between a vulnerability, a threat, and a risk, "
     "with an example of each."),
    ("technical", "concepts",
     "What is the principle of least privilege, and how would you enforce it in "
     "an organisation?"),
    ("technical", "frameworks",
     "What is the MITRE ATT&CK framework, and how would you actually use it day "
     "to day in a SOC?"),
    ("technical", "detection",
     "How do you tell a true positive from a false positive on an IDS alert, "
     "and how do you reduce alert fatigue?"),

    ("scenario", "siem",
     "A SIEM alert fires: several failed logins for one account, then a "
     "success, from an unfamiliar country. Walk me through your triage."),
    ("scenario", "malware",
     "A user says their laptop is slow and showing pop-ups. Walk me through how "
     "you'd investigate it for malware."),
    ("scenario", "network",
     "You notice a host beaconing to a known-bad IP on port 443 every sixty "
     "seconds. What's your hypothesis, and what do you do next?"),
    ("scenario", "incident-response",
     "Walk me through the phases of the incident response lifecycle, and what "
     "happens in each."),

    ("behavioural", "learning",
     "Tell me about a time you had to learn a technical topic quickly. How did "
     "you go about it?"),
    ("behavioural", "ownership",
     "Describe a mistake you made in a technical context, and what you took "
     "away from it."),
    ("behavioural", "prioritisation",
     "When several alerts fire at once and you can't work them all, how do you "
     "decide what to handle first?"),

    ("your-questions", "engagement",
     "That's the end of my questions. What would you like to ask me about the "
     "role or the team?"),
]

PHASE_ORDER = ["warm-up", "technical", "scenario", "behavioural", "your-questions"]

STOP_PHRASES = (
    "stop the interview", "end the interview", "end interview", "stop interview",
    "that's enough", "i'm done", "quit interview", "cancel interview",
)

NEUTRAL_ACKS = ["Noted.", "Thank you.", "Alright.", "Okay.", "Understood.", "Right."]


@dataclass
class InterviewCtx:
    speak: Callable[[str], None]
    listen: Callable[..., str]
    ask: Callable[[str], str]
    log: Callable[[str, str], None] = lambda k, t: None
    set_status: Callable[[str], None] = lambda s: None
    voice: str = ""
    short: bool = False
    data_dir: pathlib.Path = field(
        default_factory=lambda: pathlib.Path(
            os.environ.get("LOCALAPPDATA", str(pathlib.Path.home()))
        )
        / "OpenJarvis"
    )


# ---------------------------------------------------------------------------
# Weakness memory across sessions
# ---------------------------------------------------------------------------

def _weakness_path(ctx: InterviewCtx) -> pathlib.Path:
    return ctx.data_dir / "interview_weaknesses.json"


def load_weaknesses(ctx: InterviewCtx) -> dict:
    try:
        return json.loads(_weakness_path(ctx).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_weaknesses(ctx: InterviewCtx, weak: dict) -> None:
    try:
        ctx.data_dir.mkdir(parents=True, exist_ok=True)
        _weakness_path(ctx).write_text(json.dumps(weak, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Question selection (biased toward known weaknesses)
# ---------------------------------------------------------------------------

def select_questions(ctx: InterviewCtx) -> List[tuple]:
    warmup = [q for q in QUESTIONS if q[0] == "warm-up"][:1]
    closing = [q for q in QUESTIONS if q[0] == "your-questions"][:1]
    middle = [q for q in QUESTIONS if q[0] in ("technical", "scenario", "behavioural")]

    weak = load_weaknesses(ctx)
    # sort so weak categories come first, otherwise keep a natural spread
    middle.sort(key=lambda q: -weak.get(q[1], 0))

    if ctx.short:
        # a compact but representative spread
        picks, seen_phases = [], set()
        for q in middle:
            if q[0] not in seen_phases or len(picks) < 4:
                picks.append(q)
                seen_phases.add(q[0])
            if len(picks) >= 5:
                break
        middle = picks
    else:
        # full: shuffle within the weak-first ordering a little for variety
        top = middle[:4]
        rest = middle[4:]
        random.shuffle(rest)
        middle = top + rest

    return warmup + middle + closing


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def maybe_probe(ctx: InterviewCtx, question: str, answer: str) -> Optional[str]:
    """Ask the model for ONE follow-up if the answer is weak; else None."""
    if not answer or len(answer.split()) < 4:
        return "Could you expand on that a little?"
    prompt = (
        "You are a security interviewer. Given the question and the candidate's "
        "answer, decide if the answer is vague, shallow, or missing something "
        "important. If so, reply with ONE short probing follow-up question and "
        "nothing else. If the answer is solid, reply with exactly: SKIP.\n\n"
        f"Question: {question}\nAnswer: {answer}\n"
    )
    try:
        out = (ctx.ask(prompt) or "").strip()
    except Exception:
        return None
    if not out or out.upper().startswith("SKIP") or len(out) > 240:
        return None
    return out.strip().strip('"')


# ---------------------------------------------------------------------------
# Scoring + debrief
# ---------------------------------------------------------------------------

def score_all(ctx: InterviewCtx, transcript: List[dict]) -> List[dict]:
    items = []
    for i, t in enumerate(transcript, 1):
        items.append(
            f"{i}. [{t['category']}] Q: {t['question']}\n   A: {t['answer'] or '(no answer)'}"
        )
    prompt = (
        "You are a senior security hiring manager scoring a candidate's mock "
        "SOC analyst interview. For EACH item, score the ANSWER from 1 to 5 on: "
        "technical (correctness and depth), structure, specificity, and "
        "communication. Give an overall 1-5. If overall is 2 or below, set "
        "\"weakness\" to the item's bracketed category tag; otherwise null. Add "
        "a one-sentence note. Return ONLY a JSON array, one object per item, in "
        "order, each like: {\"technical\":n,\"structure\":n,\"specificity\":n,"
        "\"communication\":n,\"overall\":n,\"weakness\":\"tag\"|null,\"note\":\"...\"}\n\n"
        "ITEMS:\n" + "\n".join(items)
    )
    try:
        raw = ctx.ask(prompt) or ""
        start, end = raw.find("["), raw.rfind("]")
        scores = json.loads(raw[start:end + 1])
    except Exception:
        scores = []
    # pad/truncate to match
    while len(scores) < len(transcript):
        scores.append({"overall": 3, "weakness": None, "note": ""})
    return scores[: len(transcript)]


def write_debrief(ctx: InterviewCtx, transcript: List[dict], scores: List[dict]) -> pathlib.Path:
    sessions = ctx.data_dir / "interview_sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = sessions / f"session_{stamp}.md"

    overalls = [s.get("overall", 3) for s in scores]
    avg = round(sum(overalls) / max(1, len(overalls)), 1)

    lines = [
        f"# SOC Interview Debrief — {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"**Overall: {avg} / 5**  ·  {len(transcript)} questions",
        "",
    ]
    for i, (t, s) in enumerate(zip(transcript, scores), 1):
        lines += [
            f"## {i}. [{t['category']}] {t['question']}",
            f"> {t['answer'] or '(no answer)'}",
            "",
            f"- technical {s.get('technical','-')} · structure {s.get('structure','-')} "
            f"· specificity {s.get('specificity','-')} · communication "
            f"{s.get('communication','-')} · **overall {s.get('overall','-')}/5**",
        ]
        if s.get("weakness"):
            lines.append(f"- ⚠ weakness: **{s['weakness']}**")
        if s.get("note"):
            lines.append(f"- {s['note']}")
        lines.append("")

    weak_tags = [s["weakness"] for s in scores if s.get("weakness")]
    if weak_tags:
        lines += ["## Focus next", ""]
        for tag in sorted(set(weak_tags)):
            lines.append(f"- {tag}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_interview(ctx: InterviewCtx) -> None:
    questions = select_questions(ctx)
    n = len(questions)

    ctx.set_status("speaking")
    intro = (
        f"Right, let's begin, sir. I'll ask {n} questions across a few areas. "
        "Answer as you would in a real interview — I won't react as we go, and "
        "you'll get full feedback at the end. Say 'stop the interview' any time "
        "to end early. First question."
    )
    ctx.speak(intro)
    ctx.log("system", f"Interview started ({n} questions)")

    transcript: List[dict] = []
    aborted = False

    for idx, (phase, category, question) in enumerate(questions, 1):
        ctx.set_status("speaking")
        ctx.speak(question)
        ctx.log("jarvis", f"Q{idx}: {question}")

        ctx.set_status("listening")
        answer = (ctx.listen(max_seconds=75, silence=2.4) or "").strip()
        low = answer.lower()
        if any(p in low for p in STOP_PHRASES):
            aborted = True
            break
        ctx.log("user", f"A{idx}: {answer or '(no answer)'}")

        # one probing follow-up on weak answers (skip on the closing question)
        if phase != "your-questions":
            ctx.set_status("thinking")
            follow = maybe_probe(ctx, question, answer)
            if follow:
                ctx.set_status("speaking")
                ctx.speak(follow)
                ctx.log("jarvis", f"Probe: {follow}")
                ctx.set_status("listening")
                extra = (ctx.listen(max_seconds=60, silence=2.4) or "").strip()
                if any(s in extra.lower() for s in STOP_PHRASES):
                    aborted = True
                    transcript.append(
                        {"phase": phase, "category": category,
                         "question": question, "answer": answer}
                    )
                    break
                if extra:
                    answer = f"{answer} | (follow-up) {extra}"

        transcript.append(
            {"phase": phase, "category": category,
             "question": question, "answer": answer}
        )

        # neutral, straight-faced transition (no evaluation)
        if idx < n and phase != "your-questions":
            ctx.speak(random.choice(NEUTRAL_ACKS))

    if not transcript:
        ctx.set_status("speaking")
        ctx.speak("Interview cancelled, sir. No answers to review.")
        return

    # Scoring happens after — quality over speed.
    ctx.set_status("thinking")
    ctx.speak("That's the end. Give me a moment to review your answers.")
    ctx.log("system", "Scoring interview…")
    scores = score_all(ctx, transcript)

    # persist weaknesses
    weak = load_weaknesses(ctx)
    for s in scores:
        tag = s.get("weakness")
        if tag:
            weak[tag] = weak.get(tag, 0) + 1
    save_weaknesses(ctx, weak)

    path = write_debrief(ctx, transcript, scores)

    overalls = [s.get("overall", 3) for s in scores]
    avg = round(sum(overalls) / max(1, len(overalls)), 1)
    weak_tags = sorted({s["weakness"] for s in scores if s.get("weakness")})

    summary = f"Done, sir. Overall, I'd put you at {avg} out of 5. "
    if weak_tags:
        top = ", ".join(weak_tags[:3])
        summary += f"The areas to work on are: {top}. "
    else:
        summary += "No glaring weak spots this round. "
    if aborted:
        summary = "We stopped early. " + summary
    summary += "I've saved a full written debrief for you to read."

    ctx.set_status("speaking")
    ctx.speak(summary)
    ctx.log("system", f"Debrief saved: {path}")
    ctx.log("jarvis", summary)

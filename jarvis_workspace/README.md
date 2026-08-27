# Jarvis ⇄ Claude workspace

This folder is the **handoff board** between your local Jarvis and Claude
(the coding/reasoning agent in this repo). They can't talk over a live socket —
Jarvis runs on your PC, Claude runs in an ephemeral cloud container — so they
cooperate **asynchronously through this shared folder in Git**.

## How the loop works

1. **You ask Jarvis** for something that needs heavy lifting — write code, do
   research, draft a document, analyse a file.
2. **Jarvis queues it** into `for_claude.md` (a request with context) and, if
   git is wired up, pushes it.
3. **Claude picks it up** — in a Claude session you open, or on a schedule
   (a Claude "routine") — reads `for_claude.md`, does the work in the repo,
   and writes a summary + links into `from_claude.md`.
4. **Jarvis reads the result** back to you from `from_claude.md`.

## The three roles Jarvis plays

- **Interviewer** — `jarvis_interview.py` ("Jarvis, interview me")
- **Task & lifestyle manager** — `jarvis_tasks.py` (to-dos, reminders, your day)
- **Coworker with Claude** — this folder

## Files

- `for_claude.md` — requests from Jarvis/you *to* Claude (the inbox)
- `from_claude.md` — results from Claude *back* to Jarvis/you (the outbox)

Keep entries short and dated. Newest at the top.

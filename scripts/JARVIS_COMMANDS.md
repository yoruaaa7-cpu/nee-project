# JARVIS — Command Reference

Everything runs in **PowerShell**. Most commands assume you're in the Jarvis
folder first, so run this once per window:

```powershell
cd $env:LOCALAPPDATA\OpenJarvis\src
```

The `jarvis` CLI (chat, ask, doctor) works from anywhere.

---

## ▶️ Start / Stop Jarvis (voice + dashboard)

| Action | Command |
|---|---|
| **Start** (background, hidden) | `powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 start` |
| **Stop** (until next login) | `powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 stop` |
| **Status** (running? last logs) | `powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 status` |
| **Install auto-start** (runs at every login) | `powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 install` |
| **Remove auto-start** (stop it coming back) | `powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 uninstall` |

**Instant stop:** press **Ctrl + Alt + J** anywhere (works even though it's hidden).

**Restart** = `stop` then `start`.

---

## 🗣️ Voice controls (say these out loud)

| Say | Effect |
|---|---|
| "Hey Jarvis" | Wake it — wait for the ding, then speak |
| "Hey Jarvis, **go to sleep**" | Dormant (ignores everything) |
| "Hey Jarvis, **wake up**" | Resume from sleep |
| "Hey Jarvis, **power down**" (or "goodbye") | Fully stop the process |

Examples once awake: *"open Chrome"*, *"volume up"*, *"mute"*, *"next song"*,
*"lock the computer"*, *"what time is it"*, *"who's a better coder"*, etc.

---

## 🖥️ Dashboard

- Open: **http://localhost:8765** (only works while Jarvis is running)
- **F** = focus mode (just the core). **Esc** = bring panels back.
- **⛶** button or **F11** = fullscreen.
- Top bar shows **VER** (version), **MODEL**, **ENGINE**, status, uptime.

---

## ⬆️ Update to the latest version

```powershell
cd $env:LOCALAPPDATA\OpenJarvis\src
powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 stop
irm https://raw.githubusercontent.com/yoruaaa7-cpu/nee-project/claude/openjarvis-repo-setup-1mhtsd/scripts/voice_jarvis.py -OutFile voice_jarvis.py
irm https://raw.githubusercontent.com/yoruaaa7-cpu/nee-project/claude/openjarvis-repo-setup-1mhtsd/scripts/jarvis_dashboard.html -OutFile jarvis_dashboard.html
powershell -ExecutionPolicy Bypass -File .\voice_jarvis_service.ps1 start
```

**Check version:** `uv run python voice_jarvis.py --version`
(or look at **VER** on the dashboard).

---

## 🧠 Brain (which AI model answers)

| Action | Command |
|---|---|
| Test which brain is active | `uv run python -c "from openjarvis.sdk import Jarvis; j=Jarvis(); print(j.ask('hi'), j._resolved_engine_key)"` |
| Switch to cloud (Claude) | `uv run python fix_brain.py` |
| Switch to a smarter cloud model | `uv run python fix_brain.py --model claude-sonnet-4-6` |
| Go back to local/offline | `uv run python fix_brain.py --local` |

Config file: `notepad $env:USERPROFILE\.openjarvis\config.toml`
Your API key lives in the `ANTHROPIC_API_KEY` environment variable
(set with `setx ANTHROPIC_API_KEY "sk-ant-..."`, then open a new window).

---

## 🌐 Browser control

| Action | Command |
|---|---|
| Install browser engine (one time) | `uv pip install playwright` |
| **Log into your sites once** | `uv run python voice_jarvis.py --browser-login` |
| (Browser is on automatically in the background service) | — |

Then just ask, e.g. *"Hey Jarvis, go to Wikipedia and tell me the population of Dubai."*

---

## 📁 Project mode (run commands in a specific folder)

In any project folder, open PowerShell there and run:
```powershell
jarvis-here
```
Then *"Hey Jarvis, run the tests"* acts inside that folder.

---

## 🩺 Troubleshooting

| Problem | Fix |
|---|---|
| Dashboard "can't be reached" | Jarvis isn't running → `...voice_jarvis_service.ps1 start` |
| See what it's doing / errors | `Get-Content $env:LOCALAPPDATA\OpenJarvis\voice_jarvis.log -Wait -Tail 30` |
| It's slow / wrong brain | `uv run python fix_brain.py` then restart |
| General health check | `jarvis doctor` |
| Multiple copies running | `Get-CimInstance Win32_Process -Filter "Name='python.exe'" \| Where-Object { $_.CommandLine -like '*voice_jarvis.py*' } \| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }` then `start` |

---

## 📌 Key locations

- Jarvis code: `%LOCALAPPDATA%\OpenJarvis\src`
- Settings: `%USERPROFILE%\.openjarvis\config.toml`
- Log file: `%LOCALAPPDATA%\OpenJarvis\voice_jarvis.log`
- Browser profile (your logins): `%LOCALAPPDATA%\OpenJarvis\browser_profile`

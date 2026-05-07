# JARVIS — Complete Project Handoff

> **For:** Saksham Sanjmalani (project owner) and any AI agent picking this up next.
> **Last updated:** 7 May 2026, by Cascade (Claude Opus 4.7 Max)
> **Status:** Functional foundation, several critical bugs fixed today, daily-use polish still needed.
> **Repo root:** `D:\antigravity\friday\` (Windows). Git root same path.

---

## 1. Identity & Vision

JARVIS (a.k.a. Friday) is **Saksham's personal AI chief of staff** — a fully local, voice-first, Tony-Stark-style desktop assistant for Windows. The goal is the same kind of pervasive control Gemini has on Android (which doesn't exist on Windows desktop yet) but **fully owned, local, and personalized**.

### Owner profile
- **Name:** Saksham Sanjmalani
- **Address mode:** "Sir" (British butler tone, like the films)
- **Business:** ElevateWebWorks (`elevatewebworks.in`) — web/dev work
- **OS:** Windows 11, 24 GB RAM, single workstation
- **Daily mic:** Wispr Flow (used as STT-quality benchmark)

### Personality contract
JARVIS speaks like Iron Man's JARVIS:
- Short, dry, slightly clipped lines
- "Sir," used sparingly, never every sentence
- Confirms with one beat ("On it.", "Done, Sir.", "Of course.")
- Anticipates rather than waits — the briefing engine is the seed of this

### What JARVIS is NOT
- Not a chatbot. Sir doesn't want pleasantries or markdown bullet replies during voice mode.
- Not cloud-dependent. Ollama-local by default. Anthropic/OpenAI providers exist but are optional.
- Not a wrapper around ChatGPT. Personality + memory + proactive routines + desktop control are the differentiators.

---

## 2. Reference Inspirations

| Reference | What we steal from it |
|---|---|
| Iron Man movies (J.A.R.V.I.S.) | Personality, HUD aesthetic, proactive briefings |
| Gemini on Android | Pervasive device control as the bar to clear on Windows |
| [`SAGAR-TAMANG/friday-tony-stark-demo`](https://github.com/SAGAR-TAMANG/friday-tony-stark-demo) | Visual reference for the Friday HUD |
| [`ethanplusai/jarvis`](https://github.com/ethanplusai/jarvis) | Architectural reference |
| [Hermes Agent (Nous Research)](https://hermes-agent.nousresearch.com/) | Multi-agent orchestration + auto skill generation |
| Wispr Flow | STT accuracy benchmark — if Wispr can transcribe it perfectly, our Whisper config should too |

**Where JARVIS already wins:** voice loop, desktop control, personalization, proactive briefings.
**Where JARVIS still loses:** STT accuracy under noise, multi-agent orchestration depth, auto-skill creation.

---

## 3. Current Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         VOICE AGENT (PyQt HUD)                       │
│  Mic → Wake-word ("Jarvis") → Whisper STT → Local Command Router    │
│         ↓ (if not local cmd)                                         │
│  HTTP/SSE → Backend → LLM (Ollama qwen2.5:14b) → Tools → Stream     │
│         ↓                                                            │
│  edge-tts → Speaker  +  HUD overlay shows state                     │
└──────────────────────────────────────────────────────────────────────┘
```

### 3a. Repo layout
```
friday/
├── backend/                    FastAPI + LangGraph + SQLAlchemy + Chroma
│   ├── main.py                 FastAPI entrypoint, lifespan, CORS, routes
│   ├── core/                   db, security, config, scheduler
│   ├── routers/                chat.py, tasks, jarvis (proactive endpoint)
│   ├── agents/
│   │   ├── jarvis_agent.py     Main agent — multi-agent dance entry
│   │   ├── orchestrator.py     classify_lane(query) → researcher|writer|executor|answer
│   │   ├── sub_agents.py       SUB_AGENTS dict (researcher, writer, executor, reviewer)
│   │   ├── system_prompt.py    JARVIS persona prompt builder
│   │   ├── complexity_classifier.py
│   │   └── planner.py          generates execution plan for complex queries
│   ├── tools/                  All LangChain tools the agent can call
│   │   ├── memory_tools.py     remember/recall via Chroma
│   │   ├── computer_tools.py   shell, screenshot, etc.
│   │   ├── browser_tools.py    Playwright
│   │   ├── google_tools.py     Gmail/Calendar (free OAuth)
│   │   └── ...
│   ├── memory/                 Chroma vector store + embeddings
│   ├── jarvis_core/            briefing.py, routines.py
│   ├── providers/              ollama_provider.py, anthropic, openai, base
│   ├── tts/                    edge-tts wrapper
│   ├── stt/                    Sarvam (paid, optional — keep on faster-whisper)
│   └── .env                    LLM_PROVIDER, OLLAMA_MODEL, WHISPER_MODEL, …
│
├── voice_agent/                Wake word → record → STT → backend → TTS
│   ├── main.py                 entrypoint (HUD + asyncio loop + listener)
│   ├── wake_word.py            "Jarvis" detector (Whisper-based) + double-clap
│   ├── stt_engine.py           faster-whisper wrapper (small.en default)
│   ├── tts_engine.py           edge-tts (en-GB-RyanNeural) + ElevenLabs fallback
│   ├── command_router.py       Local fast-path (open apps, sites, system)
│   ├── hud.py                  PyQt6 floating circle HUD
│   ├── text_input.py           Ctrl+Shift+T text fallback (NEW today)
│   └── hotkey.py               global keyboard hotkey (Ctrl+Shift+J)
│
├── scripts/
│   ├── launch_jarvis.bat       one-click launch (visible console)
│   ├── launch_jarvis.vbs       hidden launch (used by startup shortcut)
│   ├── install_startup.py      add/remove from Windows Startup
│   └── create_desktop_shortcut.py
│
└── JARVIS_HANDOFF.md           this file
```

### 3b. Key data paths
| Path | What it holds |
|---|---|
| `D:\olama models\` | Ollama model store (set via `OLLAMA_MODELS` user env var). Currently has `qwen2.5:14b` (9 GB). |
| `backend/data/jarvis.db` | SQLite — conversations, tasks, user prefs |
| `backend/data/chroma/` | Vector memory (long-term recall) |
| `backend/.env` | All configuration. NEVER commit. |

### 3c. Tech stack
- **LLM:** Ollama `qwen2.5:14b` (local). Backed by `langchain-ollama` (NOT `langchain_community` — see today's bug fix).
- **Agent framework:** LangGraph 0.2.x via `create_react_agent`.
- **STT:** `faster-whisper` (`small.en` after today's fix; was `base`).
- **TTS:** `edge-tts` with `en-GB-RyanNeural` (British butler voice).
- **HUD:** PyQt6 frameless floating overlay.
- **Backend:** FastAPI + SQLAlchemy async + Alembic + Chroma.
- **Wake word:** Custom Whisper-based detector (no Picovoice). Says "Jarvis" → activates.

---

## 4. Today's Session — What Was Fixed

### Three critical bugs that were silently breaking everything

**Bug 1 — `state_modifier` deprecation in LangGraph 1.x**
LangGraph 1.x renamed `state_modifier` → `prompt`. The agent constructor was throwing `TypeError` on every chat call, getting silently caught and downgraded to "I'm not sure how to respond." Fixed in `backend/agents/jarvis_agent.py`.

**Bug 2 — `langchain_community.ChatOllama` has no `bind_tools()`**
The deprecated `langchain_community` ChatOllama doesn't implement `bind_tools()`, which `create_react_agent` *requires* for tool use. Every agent call raised `NotImplementedError`. Fixed by switching to `langchain-ollama` (separate package). `backend/requirements.txt` updated.

**Bug 3 — `OLLAMA_MODELS` pointed to wrong folder**
Sir's user-scope env var was set to `D:\ollama\models` but the actual models live at `D:\olama models` (different spelling — single 'l', space). Ollama was reporting 0 models. Fixed by setting `OLLAMA_MODELS=D:\olama models` at user scope.

### Improvements layered on top
| Area | Before | After |
|---|---|---|
| Whisper model | `base` | `small.en` (much higher accuracy on English) |
| Whisper prompt | 7 generic lines | Hand-tuned vocab including Wispr Flow, ElevateWebWorks, every app Sir uses, Saksham Sanjmalani spelling, etc. |
| VAD | `min_silence=500ms` | `800ms` + `speech_pad=200ms` so natural pauses don't cut commands |
| TTS voice | `en-US-GuyNeural` | `en-GB-RyanNeural` (British butler) |
| Ack on activation | Spoken "Sir." / "Listening, Sir." rotating | Tiny beep by default; opt-in `JARVIS_VERBAL_ACK=true` |
| Single instance | None — two voices possible | Windows named mutex `Global\JARVIS_VoiceAgent_Singleton` |
| Recycle Bin | Unsupported | `open the recycle bin` + `empty the recycle bin` (PowerShell `Clear-RecycleBin -Force`) |
| Multi-app launch | Single only | `open Word and Chrome`, `open Chrome then Spotify` — splits on `and`/`then` |
| Wispr Flow | Mistranscribed as "VS Perflow" | Now in vocabulary; mishearings also routed to Wispr Flow opener |
| Sleep / restart / shutdown | Missing | All three present, with `cancel shutdown` |
| Boot launcher | Started visible console | Uses `pythonw.exe` so only the HUD is visible |
| Text input fallback | None | Ctrl+Shift+T summons a frameless text box; same handler as voice |
| Backend errors | Swallowed silently | SSE `type:"error"` events now surfaced to TTS |
| Multi-agent dance | Stub files only | `stream_chat` now calls orchestrator → routes to scoped sub-agent (researcher/writer/executor) with focused prompt + filtered toolset |
| Destructive commands | Executed immediately, no gate | Every `subprocess.Popen` goes through `_safe_popen` with `JARVIS_DRY_RUN` env flag + persistent audit log at `backend/data/command_audit.log` |

### Files touched today (commit-ready)
```
backend/agents/jarvis_agent.py            (state_modifier→prompt + multi-agent wiring)
backend/providers/ollama_provider.py      (langchain-ollama import)
backend/requirements.txt                  (added langchain-ollama)
backend/.env                              (WHISPER_MODEL=small.en, EDGE_TTS_VOICE=en-GB-RyanNeural)
voice_agent/stt_engine.py                 (better prompt + VAD + small.en default)
voice_agent/command_router.py             (Recycle Bin, Wispr Flow, multi-app, system commands, dry-run safety)
voice_agent/main.py                       (single-instance mutex, beep ack, error surfacing, text input)
voice_agent/text_input.py                 (NEW)
scripts/launch_jarvis.vbs                 (pythonw.exe so no console)
```

---

## 4.5. Safety Incident & Hardening (IMPORTANT)

### What happened
During a "structural sanity test" I ran at the end of the session, I called
`route_command('restart')` as a test case. I *believed* I was only checking
that the router returned the correct CommandResult string. In reality,
`route_command()` executes side-effects **immediately** — it fired
`subprocess.Popen("shutdown /r /t 5", shell=True)` on Sir's machine, which
rebooted the computer five seconds later.

I then misread the "Restart to Update" label in the Windsurf IDE title bar
and confidently blamed Windows Update. **That was wrong.** The reboot was
my test, not Windows.

### Why it's a real design flaw, not just a one-off
- `route_command()` had no dry-run path. Any tool — human or AI — running
  it with a destructive verb would fire the actual shell command.
- There was no audit log. If Sir had not noticed the timing, we might
  never have traced the reboot to the JARVIS repo.
- Backend tools in `backend/tools/computer_tools.py` have the same class
  of risk and are **not yet** routed through a similar gate (see Known
  Issues section 5, item #4b).

### The fix in `voice_agent/command_router.py`
- Introduced `_safe_popen(*args, **kwargs)` — the **single** choke-point for
  every side-effecting shell invocation in the router.
- `subprocess.Popen(...)` is now **never** called directly in this file.
- Env flag `JARVIS_DRY_RUN=1` → `_safe_popen` returns a `_NullProc` stub
  instead of spawning. Tests must set this flag.
- Every invocation is appended to `backend/data/command_audit.log` with
  timestamp + `DESTRUCTIVE | normal` tag + `[DRY-RUN]` marker when applicable.
- Destructive commands (`shutdown`, `powrprof`, `Clear-RecycleBin`,
  `LockWorkStation`, `SetSuspendState`) are additionally logged at
  `WARNING` level so they stand out in backend logs.
- `restart` was removed from the `__main__` self-test of the router.

### Verified working (7 May 2026)
```
Destructive command requested: 'shutdown /r /t 5' (dry_run=True)
Destructive command requested: 'shutdown /s /t 10' (dry_run=True)
Destructive command requested: "[...] Clear-RecycleBin -Force [...]"  (dry_run=True)
Destructive command requested: 'rundll32.exe user32.dll,LockWorkStation' (dry_run=True)
```
All four destructive commands logged + blocked. `open Chrome` logged as
`normal` and would have launched if dry-run were off. Audit file exists at
`backend/data/command_audit.log` with human-readable timestamps.

### Safety Principles (from here on)
1. **Every destructive action goes through a named choke-point** (`_safe_popen`
   for shell, and analogous wrappers for browser/filesystem/Gmail tools).
2. **Every destructive action is audit-logged** to disk — timestamp, command,
   caller. Non-blocking; audit failure never breaks the real command.
3. **Tests and AI agents set `JARVIS_DRY_RUN=1` before running any verify
   script** that calls through to tool code. If in doubt, dry-run.
4. **JARVIS is allowed to exploit exactly the resources Sir has given it
   tools for, and no more.** There is no process-spawning, filesystem-
   write, or network-call path that isn't explicitly defined in the `tools/`
   folder. It cannot "discover" new capabilities at runtime.
5. **No self-modifying code.** JARVIS does not edit its own source files.
   When the Skill Creator (Phase 3 roadmap) is built, generated skills
   must land in a sandbox directory with explicit Sir-approval before
   being registered.

### Is JARVIS unsafe?
No. JARVIS did not go rogue, exploit, or decide on its own to restart.
A test script I wrote executed a shutdown command because the router had
no gate. The gate is now in place. The backend tools still need the
same treatment (Phase 0.1 below).

---

## 5. Known Issues / Open Bugs

### Critical — block daily use
**4b. Backend tools lack the same safety gate.** `backend/tools/computer_tools.py`,
`browser_tools.py`, `google_tools.py` etc. call `subprocess` / file APIs /
external APIs directly. They need an analogous `_safe_call` wrapper and
must honour `JARVIS_DRY_RUN`. Priority: **do this in the first 10 minutes
of the next session**.

1. **Mic lock after Windows restart** (observed today): two pythonw.exe instances can spawn before the singleton mutex resolves. One holds the mic exclusive; the other receives "device in use." **Mitigation today** is the mutex — but on cold boot it's a race. **Real fix:** add a startup delay (`WScript.Sleep 8000` instead of `4000` in `launch_jarvis.vbs`), and on mutex collision the second process should *also* be killed automatically rather than just `return`.

2. **Wake-word not triggering after orphaned restart** (observed today): after Windows Update auto-restarted, JARVIS appeared online (HUD visible) but didn't respond to "Jarvis" — likely because the wake_word module's audio stream was held open by a different process. **Manual recovery:**
   ```powershell
   Get-Process python*, pythonw* | Stop-Process -Force
   D:\antigravity\friday\scripts\launch_jarvis.bat   # double-click
   ```

3. **End-to-end LLM verification not yet completed**: today's session ended before we could run a full `python -c "..."` test calling `agent.stream_chat()`. The compile-time and orchestrator structural tests passed; the runtime LLM flow is theoretically green but unverified.

### High priority — quality issues
4. **STT still likely below Wispr Flow quality** even with `small.en`. Real fix is `medium.en` (~769 MB) on GPU, or pin to a specific quantization. Noise robustness needs Silero VAD pre-filter or RNNoise.
5. **Wake-word false negatives** when user is already mid-conversation in another app. The Whisper-based detector is heavy; consider a tiny dedicated model (Picovoice Porcupine free tier or `openWakeWord`).
6. **The proactive briefing speaks every wake** even when there's nothing new. Should rate-limit to once per ~4 hours, and cache "already spoken today."

### Medium priority
7. **No user feedback when STT misheard** — Sir says "Open Word" → Whisper hears "Open Wood" → JARVIS confidently opens nothing. Add: if the local-command router returns `handled=False` AND the transcript contains "open", echo back *"I heard 'Open Wood' — is that right?"* before falling through to backend.
8. **Text-input fallback never tested live** — Ctrl+Shift+T is wired but Sir hasn't pressed it yet. Could have focus issues on Windows.
9. **HUD is small corner mode only**. Sir wants the full Tony Stark cockpit (System Overview / Biometrics / Tasks / Command Terminal / Memory Graph / Weather / Market) — design reference is in our chat log, image 1 of the HUD set. Implementation deferred — needs real data sources for biometrics/weather/market.
10. **Multi-agent orchestrator is wired but never load-tested.** One full session calling `classify_lane` for every message is fine; if Sir hammers it, that's one extra LLM call per turn — could batch with the main response.

### Low priority — polish
11. Tray icon is a flat blue "J" — could be a proper SVG arc reactor.
12. No undo for "empty the recycle bin" — should require a 2-second confirmation window.
13. The `INITIAL_PROMPT` in `stt_engine.py` will grow; eventually move it to a YAML/JSON sidecar so it's editable without touching Python.

---

## 6. Roadmap (Prioritized)

### Phase 0 — Stabilize daily use (next 1-2 sessions)
- [ ] Verify the multi-agent dance works end-to-end with a live LLM call.
- [ ] Fix the boot-time mic-lock race (delay + duplicate-kill).
- [ ] Add an STT confirmation echo for ambiguous "open X" commands.
- [ ] Live-test the Ctrl+Shift+T text input.
- [ ] Add a tray menu item: "Restart JARVIS" (kills + relaunches, useful when mic gets stuck).

### Phase 1 — STT quality parity with Wispr Flow
- [ ] Bump default to `medium.en` if RAM allows; profile latency.
- [ ] Add Silero VAD or RNNoise pre-filter for noisy environments.
- [ ] Build a `phrases.yaml` config that auto-rebuilds the `INITIAL_PROMPT` from a flat list.
- [ ] Optional: integrate Wispr Flow as the STT backend if it exposes a local API. (Sir uses it; if it has a CLI/API, just reuse it.)

### Phase 2 — Tony Stark HUD
- [ ] Build the multi-panel expanded HUD (toggle via tray or `Ctrl+Shift+H`).
- [ ] Hook real data sources: `psutil` (CPU/RAM/Net), Open-Meteo (weather, free), `yfinance` (market overview), Chroma node count (memory graph), upcoming tasks (already in DB).
- [ ] Skip biometrics — no wearable integration unless Sir adds one.
- [ ] Animate the central "arc reactor" with audio reactivity.

### Phase 3 — Multi-agent depth (Hermes-style)
- [ ] Sub-agents in `sub_agents.py` work but are simple. Add:
  - **Reviewer** agent: post-runs after the executor and checks output quality, files a regret if needed.
  - **Skill creator**: when JARVIS tries to do something it can't, autogenerate a new tool function and register it.
- [ ] Persistent agent memory per role (researcher remembers what it researched).
- [ ] Inter-agent messaging in Chroma so a writer can ask the researcher mid-flight.

### Phase 4 — Pervasive control (Gemini-on-Android parity)
- [ ] WIN+R-style "do anything" commands via UIAutomation (`pywinauto`) for any app.
- [ ] Browser context awareness: read what's on screen and act on it.
- [ ] Context-aware shortcuts: "send this to Sarah" → reads currently focused doc/url and uses Gmail tool.
- [ ] OCR fallback for unread-able apps (`Tesseract` or Windows OCR API).

### Phase 5 — Identity & long-term memory
- [ ] Per-day journal that JARVIS writes itself: meetings I attended, things Sir got annoyed at, what worked.
- [ ] Weekly self-review: JARVIS proposes its own improvements ("I noticed I miscaught 'Wispr Flow' three times — added it to my vocab").
- [ ] Voice cloning of a specific person (Saksham's choice) for TTS — local with Coqui XTTS or similar.

---

## 7. New Ideas (For The Next AI To Consider)

These weren't yet discussed with Sir but I'd recommend bringing up:

1. **Confidence-gated action.** Every command should have a confidence score. Above 0.85 → just do it. 0.6–0.85 → speak a one-line confirm. Below 0.6 → ask "did you mean X or Y?". Stops the "I confidently did the wrong thing" failure mode.

2. **Per-app speech profile.** When Sir opens Wispr Flow, JARVIS should know not to also be listening for "jarvis" loudly — Wispr Flow is dictating. Coordination via window-title detection.

3. **Local LLM warm-pool.** Keep `qwen2.5:14b` warm in Ollama at all times. First-call latency is the biggest UX killer.

4. **Embeddings cache for the orchestrator.** `classify_lane` runs an LLM call every turn. Replace with a small SBERT classifier trained on 50 example queries per lane. Drops 800ms per turn.

5. **"Jarvis, scratch that."** Sir should be able to undo the last action verbally. Maintain a 1-deep undo stack of executed tools.

6. **Identity moments.** Random soft callouts during the day: "Sir, you've been on Word for two hours straight — break?". Already half-built in `routines.py`; just needs richer triggers.

7. **Cross-device sync.** If Sir wants this on a phone someday, the SQLite + Chroma stores must move to a sync-friendly format. Note: phone parity is NOT a current goal but worth designing for.

8. **Privacy switch.** A clear "JARVIS, stop listening for 30 minutes" command that visibly mutes the mic + closes the audio stream. Critical for trust.

9. **Speech-act understanding, not keyword matching.** The local command router is regex-based. Long-term, replace with a tiny intent classifier so "could you maybe close that bin thing" works.

10. **JARVIS knows when it doesn't know.** If the LLM's response confidence is low (`logprobs`), proactively say "I'm not sure about that — should I look it up?" instead of hallucinating.

---

## 8. Critical Configuration Reference

### `backend/.env` (current state)
```
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=qwen2.5:14b

WHISPER_MODEL=small.en
EDGE_TTS_VOICE=en-GB-RyanNeural
JARVIS_DEFAULT_MODE=work
JARVIS_API_KEY=local-dev-key

# Optional:
# JARVIS_VERBAL_ACK=true     # prefer spoken "Sir." over a beep
# JARVIS_DRY_RUN=1           # BLOCKS every shell command in command_router.
#                            # Always set this before running test scripts.
# TTS_PROVIDER=elevenlabs    # if Sir gets an ElevenLabs key
# ELEVENLABS_API_KEY=...
# ELEVENLABS_VOICE_ID=...
```

### Audit log
```
backend/data/command_audit.log
```
Every call through `_safe_popen` is appended here. Format:
```
2026-05-07T18:27:11 DESTRUCTIVE [DRY-RUN] shutdown /r /t 5
2026-05-07T18:27:11 normal                 start chrome
```
**Grep this file first** when something unexpected happens on the machine.
Do not delete it without archiving — it is the paper trail.

### Windows env (User scope, persistent)
```
OLLAMA_MODELS=D:\olama models
```
**Do not change this.** The folder name has a typo (`olama` not `ollama`, with a space) but that's where 9 GB of model lives.

### Windows Startup
- Already wired via `scripts/install_startup.py`. Run once: `python scripts/install_startup.py`. Uninstall: same script with `--remove`.
- Launches via `wscript.exe scripts/launch_jarvis.vbs` which spawns backend + voice agent both with `pythonw.exe` (no console).

### Hotkeys
- `Ctrl+Shift+J` → activate voice (same as saying "Jarvis")
- `Ctrl+Shift+T` → text-input fallback (NEW today)

---

## 9. How To Pick This Up (For The Next AI Or Future-Cascade)

### First 5 minutes
1. Read this file top to bottom.
2. Read `backend/agents/jarvis_agent.py` — the multi-agent dance is the most novel piece.
3. Read `voice_agent/main.py` — the entire voice loop is here.
4. Run the smoke test:
   ```powershell
   D:\antigravity\friday\backend\.venv\Scripts\python.exe -c "
   import sys; sys.path.insert(0, r'D:\antigravity\friday\backend')
   from agents.orchestrator import classify_lane
   from agents.sub_agents import SUB_AGENTS
   print('Lanes:', list(SUB_AGENTS.keys()))
   "
   ```
5. Confirm Ollama is reachable: `Invoke-WebRequest http://localhost:11434/api/tags`

### Next 30 minutes — verify Sir's daily flow
1. Start backend manually:
   ```powershell
   cd D:\antigravity\friday\backend
   .venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
   ```
2. Start voice agent in a second terminal:
   ```powershell
   cd D:\antigravity\friday\voice_agent
   ..\backend\.venv\Scripts\python.exe main.py
   ```
3. Say "Jarvis" → expect a beep + listening state on HUD.
4. Say "Open Wispr Flow." → expect Wispr Flow to open + "Opening Wispr Flow." spoken.
5. Say "Jarvis. What time is it?" → expect time spoken.
6. Try the orchestrator-routed query: "Jarvis. Draft a short LinkedIn post about local AI." → expect the *writer* sub-agent to take over (look for `Orchestrator lane: writer` in backend logs).

### Where to look first when something breaks
| Symptom | First place to look |
|---|---|
| "I'm not sure how to respond" on every query | Backend logs — likely an exception in the agent. Check `langchain-ollama` is installed. |
| Mic in use error | Two `pythonw.exe` running — kill all and restart. |
| Wake word never fires | `voice_agent/wake_word.py`; check Whisper is loaded. |
| HUD doesn't show | PyQt6 missing, or another full-screen app holds focus. |
| Backend won't start | `.env` malformed, or port 8000 in use. |
| `bind_tools` NotImplementedError | You're back on `langchain_community.ChatOllama` — must be `langchain_ollama.ChatOllama`. |
| No models in `ollama list` | `OLLAMA_MODELS` env var wrong; should be `D:\olama models`. |

### What NOT to break
- The JARVIS persona in `agents/system_prompt.py` — Sir is particular about the tone.
- The `INITIAL_PROMPT` in `stt_engine.py` — every word in there earned its spot.
- `OLLAMA_MODELS` user env var — pointing it elsewhere wastes Sir's disk.
- The single-instance mutex name `Global\JARVIS_VoiceAgent_Singleton` — changing it forks duplicate processes again.
- The `_safe_popen` choke-point in `command_router.py`. **Never** reintroduce
  direct `subprocess.Popen(...)` calls there. Every side-effect must stay
  audit-logged and dry-run-able.

### Rules for any AI picking this up
1. **Always set `JARVIS_DRY_RUN=1` before running verification scripts.**
   Don't be the assistant that reboots the user's computer during a "test".
2. **Read `backend/data/command_audit.log` first** if Sir reports something
   weird happening on the machine.
3. **Propose — don't execute — any new destructive verb.** If you add
   `format_drive` or `delete_folder`, add it in dry-run mode first, show
   Sir the audit log entries it *would* produce, then enable it only after
   explicit confirmation.

---

## 10. Conversation Memory (Things Sir Has Said)

Curated lines from our session that capture intent — useful for next AI to absorb the voice:

> *"What if we can design it so that it starts on the boot and only the UI is visible at the right button, not this command prompt going on?"*
**Action:** done — `pythonw.exe` + startup shortcut.

> *"There are two voices. Two Jarvises going on in the computer. Solve that."*
**Action:** done — Windows mutex `Global\JARVIS_VoiceAgent_Singleton`.

> *"It says 'Open Word Term' instead of 'Open WordPad' and 'VS Perflow' for 'Wispr Flow'."*
**Action:** done — vocabulary in `INITIAL_PROMPT` + explicit Wispr Flow handler + WordPad alias.

> *"We can also keep the option of writing if the voice is not working, then we can write and it continues to follow the command."*
**Action:** done — `text_input.py` + Ctrl+Shift+T hotkey.

> *"For the UI which comes at the name of it Jarvis can make it better."*
**Action:** deferred — current corner-HUD is decent; full Tony Stark cockpit is Phase 2.

> *"The thing is, when I said to open an app Wispr Flow, it listen something verperflow."*
**Action:** Wispr Flow spelling now seeded into Whisper's bias prompt.

> *"There is no Gemini on the laptop, so I wanted to build my project."*
**The vision in one sentence.** Gemini-on-Android-but-for-Windows-and-mine.

> *"Right now, this message has been written only by speaking, and it is written by Wispr Flow."*
**Implication:** Wispr Flow is the STT quality floor. JARVIS must reach it.

> *"Computer restarted automatically."*
**Cause (CORRECTED):** My verification script called `route_command('restart')`
which fired `shutdown /r /t 5`. I initially misattributed it to Windows Update;
Sir caught the error. Full writeup in Section 4.5. Fixed with
`_safe_popen` + `JARVIS_DRY_RUN=1` + audit log.

---

## 11. Final Notes

- **Credit budget:** Sir is at 80% daily / 90% weekly today. Resets May 10. From now until then, **no further changes from me**.
- **Best next session opening:** "Sir, did the daily commands work? Any new mishearings I should add to the vocab?" — the easiest, highest-impact iteration is feeding back actual transcription failures into `INITIAL_PROMPT`.
- **Long-term north star:** the day JARVIS catches a bug in Sir's code mid-edit, says "Sir, line 42 has an off-by-one — fix it?", waits for a yes, and silently commits the fix — that's the day this project graduates.

---

*End of handoff. Keep building, Sir. , 7 May 2026*

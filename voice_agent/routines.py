"""
Routines — chainable task sequences triggered by a single voice command.

Sir says "perform my daily tasks" or "run my morning routine" and Friday
executes a stored sequence of steps. Each step is one of:

    open_app(name)        — open an installed app (resolved via app_index)
    open_website(url)     — open a URL in the default browser
    speak(text)           — speak a line through the TTS engine
    ask_claude(prompt)    — open Claude Desktop with a prompt pre-filled
    wait(seconds)         — pause between steps
    fetch_briefing()      — pull the morning brief from the backend

Routines live in ``~/.friday/routines.json``. On first run we seed two
starters so Sir can test immediately:

    "daily tasks"     — LinkedIn posts + cold emails, delegated to Claude
    "morning routine" — open the usual apps, fetch the briefing

This is the **Phase 1 scaffold** for the autopilot Sir asked for. Phase 2
(watch-and-learn from screen observation) is documented in JARVIS_HANDOFF.md
and builds on the same `routines.json` schema, so anything Sir records today
will keep working once the observer ships.
"""

import difflib
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_ROUTINES_FILE = Path(os.path.expanduser("~")) / ".friday" / "routines.json"


# ─── Default callbacks (registered by main.py at startup) ────────────────────
# A routine running on a background thread can't easily reach into the asyncio
# event loop where the TTS engine lives. main.py registers a sync wrapper here
# so `_run_step` can speak by simply calling _DEFAULT_SPEAK(text), and the
# wrapper schedules the actual coroutine via run_coroutine_threadsafe.

_DEFAULT_SPEAK: Optional[Callable[[str], None]] = None
_DEFAULT_STATUS: Optional[Callable[[str], None]] = None


def set_speak_handler(fn: Optional[Callable[[str], None]]) -> None:
    """Register the default speak callback. Pass None to clear."""
    global _DEFAULT_SPEAK
    _DEFAULT_SPEAK = fn


def set_status_handler(fn: Optional[Callable[[str], None]]) -> None:
    """Register the default HUD-status callback. Pass None to clear."""
    global _DEFAULT_STATUS
    _DEFAULT_STATUS = fn


# ─── Starter routines (seeded on first run) ──────────────────────────────────

_STARTER_ROUTINES: dict[str, dict] = {
    "daily tasks": {
        "name": "Daily Tasks",
        "description": (
            "Sir's daily LinkedIn + cold-email pipeline, routed into the "
            "existing Claude Desktop Cowork tab so context isn't lost. "
            "Friday pastes each prompt and waits for Sir to press Enter."
        ),
        "steps": [
            {"type": "speak",
             "text": "Starting your daily tasks, Sir. Routing the LinkedIn brief into Cowork now."},
            {"type": "ask_claude",
             "tab": "cowork",
             "prompt": (
                 "Please open LinkedIn in a new browser tab and publish the "
                 "three draft posts I have queued in my notes for today. Use "
                 "computer-use to navigate, paste each post, and click publish. "
                 "Confirm in the chat once each one is live."
             )},
            {"type": "wait", "seconds": 8},
            {"type": "speak",
             "text": "Pausing so you can press Enter on the LinkedIn brief. I'll queue the cold-email brief next."},
            {"type": "wait", "seconds": 12},
            {"type": "ask_claude",
             "tab": "cowork",
             "prompt": (
                 "Send the cold emails from today's prospect list. Use the "
                 "template in my drafts folder and personalise the first "
                 "paragraph for each prospect. Pause for my approval before "
                 "sending each email."
             )},
        ],
    },
    "morning routine": {
        "name": "Morning Routine",
        "description": "Open Sir's daily apps and pull the briefing.",
        "steps": [
            {"type": "open_app",   "name": "Google Chrome"},
            {"type": "open_app",   "name": "Spotify"},
            {"type": "open_app",   "name": "WhatsApp"},
            {"type": "speak",
             "text": "Good morning, Sir. Let me pull your briefing."},
            {"type": "fetch_briefing"},
        ],
    },
}


# ─── Storage ─────────────────────────────────────────────────────────────────

# Bump when the starter-routine schema changes so existing installs get
# migrated (without wiping any routines Sir added on top). May 18 2026:
# v2 switched ask_claude steps to route into the existing Cowork tab
# instead of opening a fresh chat each time.
_STARTERS_VERSION = 2


def _load() -> dict[str, dict]:
    """Read routines.json, seeding/migrating starters as needed.

    Migration rule: any starter routine whose KEY matches a routine
    already in the file gets REPLACED with the latest starter version,
    but routines Sir added on top (keys not in ``_STARTER_ROUTINES``)
    are preserved verbatim. A version sentinel ``_version`` in the file
    is bumped to ``_STARTERS_VERSION`` after a successful migration.
    """
    try:
        _ROUTINES_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not _ROUTINES_FILE.exists():
            payload = {"_version": _STARTERS_VERSION, **_STARTER_ROUTINES}
            _ROUTINES_FILE.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
            logger.info("Seeded routines file with %d starter routines",
                        len(_STARTER_ROUTINES))
            return dict(_STARTER_ROUTINES)

        data = json.loads(_ROUTINES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("routines.json is not a JSON object")

        # Always strip the version sentinel before returning so the
        # rest of routines.py never has to think about it.
        file_version = int(data.pop("_version", 1))
        if file_version < _STARTERS_VERSION:
            for k, v in _STARTER_ROUTINES.items():
                data[k] = v
            payload = {"_version": _STARTERS_VERSION, **data}
            _ROUTINES_FILE.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
            logger.info(
                "Migrated routines.json from v%d → v%d "
                "(refreshed %d starter routines, preserved %d custom)",
                file_version, _STARTERS_VERSION,
                len(_STARTER_ROUTINES),
                max(0, len(data) - len(_STARTER_ROUTINES)),
            )
        return data
    except Exception as exc:
        logger.warning("Failed to load routines (%s) — using starter set", exc)
        return dict(_STARTER_ROUTINES)


def _save(routines: dict[str, dict]) -> None:
    try:
        _ROUTINES_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {"_version": _STARTERS_VERSION, **routines}
        _ROUTINES_FILE.write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("Failed to save routines: %s", exc)


def list_routines() -> list[str]:
    """All known routine keys (used by the HUD's routine picker, later)."""
    return list(_load().keys())


def find_routine(query: str) -> Optional[tuple[str, dict]]:
    """Fuzzy-match a routine name. Returns (key, routine_dict) or None."""
    routines = _load()
    key = query.lower().strip().rstrip(".")
    if key in routines:
        return key, routines[key]
    matches = difflib.get_close_matches(key, routines.keys(), n=1, cutoff=0.6)
    if matches:
        m = matches[0]
        return m, routines[m]
    return None


# ─── Execution ───────────────────────────────────────────────────────────────

def run_routine(
    name: str,
    *,
    on_speak: Optional[Callable[[str], None]] = None,
    on_status: Optional[Callable[[str], None]] = None,
) -> bool:
    """Execute a routine by (fuzzy) name. Blocking — call from a thread.

    Args:
        name:      routine key, fuzzy-matched against ``list_routines()``.
        on_speak:  callback for spoken lines (typically the TTS engine).
        on_status: callback for HUD status text (e.g. "Step 2 of 5: ...").

    Returns True if a routine was found and started; False if no match.
    """
    found = find_routine(name)
    if not found:
        logger.info("No routine matched %r", name)
        return False
    key, routine = found
    pretty = routine.get("name", key)
    steps = routine.get("steps", []) or []
    logger.info("Running routine %r (%d steps)", pretty, len(steps))

    # Fall back to the module-level handlers registered by main.py so
    # routines triggered straight from the voice command still speak,
    # without the dispatcher in command_router needing to know about
    # the asyncio loop or the TTS engine.
    speak_fn = on_speak or _DEFAULT_SPEAK
    status_fn = on_status or _DEFAULT_STATUS

    for i, step in enumerate(steps, start=1):
        if status_fn:
            try:
                status_fn(f"{pretty} — step {i}/{len(steps)}")
            except Exception:
                pass
        try:
            _run_step(step, on_speak=speak_fn, on_status=status_fn)
        except Exception as exc:
            logger.error("Routine step %d failed (%s): %s", i, step, exc)
            if speak_fn:
                try:
                    speak_fn("One of the steps had an issue, Sir. Continuing.")
                except Exception:
                    pass

    if speak_fn:
        try:
            speak_fn(f"Done with {pretty}, Sir.")
        except Exception:
            pass
    return True


def run_routine_async(name: str, **kwargs) -> threading.Thread:
    """Fire-and-forget runner used by the voice-command path so the asyncio
    loop isn't blocked by an 8-second `wait` step.
    """
    t = threading.Thread(
        target=run_routine, args=(name,), kwargs=kwargs,
        name=f"FridayRoutine[{name}]", daemon=True,
    )
    t.start()
    return t


def _run_step(step: dict, on_speak=None, on_status=None) -> None:
    """Dispatch a single step. Unknown step types are logged and skipped."""
    t = (step.get("type") or "").lower()

    if t == "speak":
        text = step.get("text", "")
        if on_speak and text:
            on_speak(text)
        return

    if t == "wait":
        time.sleep(float(step.get("seconds", 1.0)))
        return

    if t == "open_app":
        try:
            from app_index import find_app, launch
        except ImportError:
            logger.warning("app_index not importable — skipping open_app step")
            return
        app = find_app(step.get("name", ""))
        if app is not None:
            launch(app)
        else:
            logger.info("open_app step: no installed match for %r",
                        step.get("name"))
        return

    if t == "open_website":
        url = step.get("url", "")
        if url and sys.platform == "win32":
            try:
                os.startfile(url)
            except OSError as exc:
                logger.warning("open_website failed for %s: %s", url, exc)
        return

    if t in ("ask_claude", "ask_claude_new_chat"):
        prompt = step.get("prompt", "")
        if not prompt:
            return
        # The step optionally pins a Claude Desktop tab ("chat" / "cowork"
        # / "code"). If absent, the router infers from message content.
        tab_hint = step.get("tab") or step.get("hint")

        # New behaviour (May 18 2026): route into the EXISTING Claude
        # Desktop window so conversation context is preserved. The old
        # implementation called `claude://claude.ai/new?q=...` which
        # spawned a fresh chat every time, throwing away memory of the
        # previous step. Each call here:
        #   1. Finds Claude Desktop (any tab),
        #   2. Clicks the requested tab via UI Automation,
        #   3. Pastes the prompt,
        #   4. Leaves Sir to press Enter.
        # Only when no Claude window exists at all do we open one.
        try:
            from claude_router import send_to_existing_claude
        except Exception as exc:
            logger.debug("claude_router import failed: %s — URI fallback", exc)
            send_to_existing_claude = None  # type: ignore[assignment]

        routed = False
        if send_to_existing_claude is not None:
            try:
                result = send_to_existing_claude(prompt, hint=tab_hint)
                routed = bool(result.success)
                if routed:
                    logger.info(
                        "ask_claude routed into existing window (%s)",
                        result.note,
                    )
            except Exception as exc:
                logger.warning("claude_router raised: %s", exc)

        if not routed:
            # Fallback: open a new chat via URI, but speak a warning so
            # Sir knows this step lost prior-chat context.
            encoded = urllib.parse.quote(prompt, safe="")
            uri = f"claude://claude.ai/new?q={encoded}"
            try:
                if sys.platform == "win32":
                    os.startfile(uri)
                if _DEFAULT_SPEAK is not None:
                    _DEFAULT_SPEAK(
                        "No Claude window was open, Sir, so I opened a new "
                        "chat for this step."
                    )
            except OSError as exc:
                logger.warning(
                    "Claude URI handler missing (%s) — open Claude manually "
                    "and paste this prompt: %s", exc, prompt[:120],
                )
        return

    if t == "fetch_briefing":
        try:
            import httpx
            url = "http://127.0.0.1:8000/jarvis/pending?force_brief=true"
            r = httpx.get(url, timeout=15)
            data = r.json() if r.status_code == 200 else {}
            briefing = (data or {}).get("briefing")
            if briefing and on_speak:
                on_speak(briefing)
        except Exception as exc:
            logger.debug("Briefing fetch failed: %s", exc)
        return

    logger.warning("Unknown routine step type: %r — skipping", t)


# ─── Recording (manual; observer comes in Phase 2) ───────────────────────────

def append_step(routine_key: str, step: dict) -> None:
    """Append one step to an existing (or new) routine and persist.

    Wired up to "remember step: <description>" voice commands in a later
    session. Today it's used by tests + any AI agent picking this up.
    """
    routines = _load()
    if routine_key not in routines:
        routines[routine_key] = {
            "name": routine_key.title(),
            "description": "",
            "steps": [],
        }
    routines[routine_key].setdefault("steps", []).append(step)
    _save(routines)
    logger.info("Appended step to routine %r: %s", routine_key, step)


# ─── Standalone smoke test ───────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("Known routines:", list_routines())
    for q in ["daily tasks", "daily", "morning", "morning routine", "xyz"]:
        match = find_routine(q)
        print(f"  find_routine({q!r}) → {match[0] if match else None}")

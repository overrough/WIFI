"""
Local command router — intercepts common voice commands for instant execution.

When you say "Jarvis, open Chrome" or "Jarvis, open YouTube", this router
handles the command locally (no backend API call needed), giving you
sub-second response times for everyday actions.

Commands that don't match any local pattern fall through to the Jarvis
backend for full AI-powered processing.

Supported command categories:
  1. APP LAUNCH    — "open Chrome", "open VS Code", "open Notepad"
  2. WEBSITE       — "open YouTube", "go to Google", "open GitHub"
  3. CLAUDE        — "open Claude", "send this to Claude", "Claude cowork"
  4. SYSTEM        — "lock screen", "shutdown", "volume up/down"
  5. QUICK ACTIONS — "what time is it", "take a screenshot"
"""

import difflib
import logging
import os
import platform
import re
import subprocess
import threading
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Shutdown handler injection slot ──────────────────────────────────────
# main.py registers a callable here that performs the actual shutdown
# (kill backend subprocess, stop listener, quit Qt, os._exit). We don't
# import main.py from this module — too much coupling — so the
# handler is plugged in at startup. ``None`` means the command will
# refuse to fire.
_SHUTDOWN_HANDLER: Optional[Callable[[], None]] = None


def set_shutdown_handler(fn: Optional[Callable[[], None]]) -> None:
    """Register the function that actually executes a confirmed shutdown."""
    global _SHUTDOWN_HANDLER
    _SHUTDOWN_HANDLER = fn

# ── Lazy-imported helpers ────────────────────────────────────────────────────
# `app_index` and `routines` are deliberately imported INSIDE the functions
# that need them so that:
#   - import order of voice_agent/* stays simple,
#   - a broken/missing dependency in either module never breaks the bulk of
#     the router (it just falls through to the legacy APPS dict + browser).
# The top-level `import app_index` + `import routines` would also work, but
# pulling them in lazily keeps the router resilient when run from tests.

# ── Safety: dry-run wrapper ──────────────────────────────────────────────────
#
# Set env var JARVIS_DRY_RUN=1 to block *all* shell/app executions in this
# module — route_command() will still return the correct CommandResult but
# no actual subprocess will spawn. Used for:
#   • Automated tests / verification scripts (so e.g. "restart" does NOT
#     actually reboot the machine, which happened once — a real incident
#     that proved why we need this gate).
#   • "Preview what JARVIS would do" mode in future UI.
#
# Separately, every destructive command is AUDIT-LOGGED to
#   backend/data/command_audit.log
# with a timestamp so there's always a paper trail of what ran and when.

_DRY_RUN = os.getenv("JARVIS_DRY_RUN", "").lower() in ("1", "true", "yes")

# Commands we consider "destructive" — logged with a louder WARNING level.
_DESTRUCTIVE_PATTERNS = (
    "shutdown", "powrprof", "Clear-RecycleBin",
    "LockWorkStation", "SetSuspendState",
)


class _NullProc:
    """Stand-in for a subprocess.Popen handle when dry-running."""
    returncode = 0
    def wait(self, *a, **kw): return 0
    def poll(self): return 0
    def terminate(self): pass
    def kill(self): pass
    def communicate(self, *a, **kw): return (b"", b"")


def _audit_log(cmd_repr: str, destructive: bool) -> None:
    """Append every spawned command to an audit log on disk."""
    try:
        from datetime import datetime
        from pathlib import Path
        audit_dir = Path(__file__).resolve().parent.parent / "backend" / "data"
        audit_dir.mkdir(parents=True, exist_ok=True)
        tag = "DESTRUCTIVE" if destructive else "normal"
        dry = " [DRY-RUN]" if _DRY_RUN else ""
        line = f"{datetime.now().isoformat(timespec='seconds')} {tag}{dry} {cmd_repr}\n"
        with open(audit_dir / "command_audit.log", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # audit failures must never break the actual command


def _safe_popen(*args, **kwargs):
    """subprocess.Popen wrapper that respects JARVIS_DRY_RUN and audit-logs.

    Use this instead of subprocess.Popen throughout command_router. It's
    the single choke-point where destructive OS commands become observable
    and, when necessary, blockable.
    """
    cmd_repr = args[0] if args else kwargs.get("args", "")
    cmd_str = str(cmd_repr)
    destructive = any(pat.lower() in cmd_str.lower() for pat in _DESTRUCTIVE_PATTERNS)

    if destructive:
        logger.warning("Destructive command requested: %r (dry_run=%s)", cmd_str, _DRY_RUN)
    _audit_log(cmd_str, destructive)

    if _DRY_RUN:
        logger.info("[DRY-RUN] skipped: %r", cmd_str)
        return _NullProc()

    # NOTE: must call subprocess.Popen here, NOT _safe_popen — calling
    # ourselves would be infinite recursion (RecursionError seen in the
    # wild on every "open <app>" command, May 10 2026). Single line, but
    # this is the actual subprocess spawn.
    return subprocess.Popen(*args, **kwargs)


@dataclass
class CommandResult:
    """Result of a local command execution."""
    handled: bool           # True = command was handled locally
    response: str           # speech response for TTS
    success: bool = True    # whether the command succeeded


# ── Smart app finder ─────────────────────────────────────────────────────────
#
# Before falling back to a browser URL, check if the desktop app is installed.
# Priority: URI protocol handler → known EXE path → start command → browser.

# Apps that register Windows URI protocol handlers when installed
_URI_PROTOCOLS = {
    "whatsapp":   "whatsapp://",
    "spotify":    "spotify://",
    "discord":    "discord://",
    "zoom":       "zoommtg://",
    "slack":      "slack://",
    "telegram":   "tg://",
    "signal":     "sgnl://",
    "skype":      "skype://",
    "notion":     "notion://",
    "figma":      "figma://",
    "claude":     "claude://claude.ai/new",
    "obsidian":   "obsidian://",
    "linear":     "linear://",
}

# Common desktop app EXE paths (for apps that don't use URI schemes)
_APP_PATHS = {
    "whatsapp": [
        r"%LOCALAPPDATA%\WhatsApp\WhatsApp.exe",
        r"%APPDATA%\WhatsApp\WhatsApp.exe",
        r"%PROGRAMFILES%\WhatsApp\WhatsApp.exe",
    ],
    "spotify": [
        r"%APPDATA%\Spotify\Spotify.exe",
        r"%LOCALAPPDATA%\Microsoft\WindowsApps\Spotify.exe",
    ],
    "discord": [
        r"%LOCALAPPDATA%\Discord\Update.exe",
        r"%LOCALAPPDATA%\Discord\app-*\Discord.exe",
    ],
    "telegram": [
        r"%APPDATA%\Telegram Desktop\Telegram.exe",
        r"%LOCALAPPDATA%\Telegram Desktop\Telegram.exe",
    ],
    "notion":   [r"%LOCALAPPDATA%\Programs\Notion\Notion.exe"],
    "figma":    [r"%LOCALAPPDATA%\Figma\Figma.exe"],
    "obsidian": [r"%LOCALAPPDATA%\Obsidian\Obsidian.exe"],
    "signal":   [r"%LOCALAPPDATA%\Programs\signal-desktop\Signal.exe"],
    "zoom":     [r"%APPDATA%\Zoom\bin\Zoom.exe"],
    "slack":    [r"%LOCALAPPDATA%\slack\slack.exe"],
}


def _try_uri(app: str) -> bool:
    """Try to launch via Windows URI protocol handler. Returns True if attempted."""
    uri = _URI_PROTOCOLS.get(app.lower())
    if not uri:
        return False
    try:
        os.startfile(uri)
        return True
    except OSError:
        return False


def _try_exe_paths(app: str) -> bool:
    """Try known EXE paths. Returns True if found and launched."""
    import glob
    paths = _APP_PATHS.get(app.lower(), [])
    for raw in paths:
        expanded = os.path.expandvars(raw)
        # Support glob patterns (e.g. app-*)
        matches = glob.glob(expanded) if "*" in expanded else ([expanded] if os.path.exists(expanded) else [])
        for path in matches:
            if os.path.exists(path):
                _safe_popen([path], shell=False)
                return True
    return False


def _smart_open(app_name: str, fallback_url: str | None = None) -> CommandResult:
    """
    Intelligent app launcher:
    1. Try URI protocol (whatsapp://, spotify://, etc.)
    2. Try known EXE paths
    3. Try Windows 'start <name>' command
    4. Fall back to browser URL if provided
    """
    key = app_name.lower().strip()

    if _try_uri(key):
        return CommandResult(handled=True, response=f"Opening {app_name.title()}.")

    if _try_exe_paths(key):
        return CommandResult(handled=True, response=f"Opening {app_name.title()}.")

    # Try Windows start command (works for many installed apps)
    try:
        _safe_popen(f"start {key}", shell=True)
        return CommandResult(handled=True, response=f"Opening {app_name.title()}.")
    except Exception:
        pass

    # Last resort: open in browser
    if fallback_url:
        try:
            os.startfile(fallback_url)
            logger.info("Desktop app not found for %r — opened browser fallback: %s", app_name, fallback_url)
            return CommandResult(handled=True, response=f"Desktop app not found. Opening {app_name.title()} in your browser.")
        except Exception:
            pass

    return CommandResult(handled=False, response="")


# ── App registry ──────────────────────────────────────────────────────────────
# Maps app names (lowercase) to their launch commands on Windows.
# Add your own apps here!

APPS = {
    # Browsers
    "chrome": "start chrome",
    "google chrome": "start chrome",
    "firefox": "start firefox",
    "edge": "start msedge",
    "brave": "start brave",

    # Dev tools
    "vs code": "code",
    "vscode": "code",
    "visual studio code": "code",
    "terminal": "start wt",
    "windows terminal": "start wt",
    "powershell": "start powershell",
    "cmd": "start cmd",
    "command prompt": "start cmd",
    "git bash": "start git-bash",

    # Productivity
    "notepad": "start notepad",
    "word": "start winword",
    "excel": "start excel",
    "powerpoint": "start powerpnt",
    "outlook": "start outlook",
    "teams": "start msteams",
    "slack": "start slack",
    "discord": "start discord",
    "zoom": "start zoom",

    # System
    "file explorer": "start explorer",
    "explorer": "start explorer",
    "settings": "start ms-settings:",
    "task manager": "start taskmgr",
    "calculator": "start calc",
    "paint": "start mspaint",
    "snipping tool": "start snippingtool",
    "camera": "start microsoft.windows.camera:",
    "webcam": "start microsoft.windows.camera:",
    "photos": "start ms-photos:",
    "store": "start ms-windows-store:",
    "maps": "start bingmaps:",
    "clock": "start ms-clock:",
    "alarms": "start ms-clock:",
    "weather": "start bingweather:",
    "mail": "start outlookmail:",

    # Media
    "spotify": "start spotify",
    "vlc": "start vlc",

    # AI tools
    "claude": "__CLAUDE__",     # special handler
    "claude desktop": "__CLAUDE__",
    "cursor": "start cursor",
    "chatgpt": "start https://chatgpt.com",
    "chat gpt": "start https://chatgpt.com",

    # Voice / dictation
    "wispr flow": "__WISPR_FLOW__",
    "whisper flow": "__WISPR_FLOW__",
    "vs perflow": "__WISPR_FLOW__",   # common Whisper mishearing

    # Adobe & creative
    "photoshop": "start photoshop",
    "premiere pro": "start premiere",
    "after effects": "start afterfx",
    "figma": "start figma",
    "canva": "start https://canva.com",
    "notion": "start notion",
    "obs": "start obs64",

    # Special: Recycle Bin (no path → use shell namespace)
    "recycle bin": "__RECYCLE_BIN__",
    "recyclebin": "__RECYCLE_BIN__",
    "trash": "__RECYCLE_BIN__",
    "bin": "__RECYCLE_BIN__",

    # Office variants
    "wordpad": "start write",
    "word pad": "start write",
    "microsoft word": "start winword",
    "microsoft excel": "start excel",
    "microsoft powerpoint": "start powerpnt",
}


# ── Website registry ─────────────────────────────────────────────────────────
# Maps website names to URLs. "Open YouTube" → start https://youtube.com

WEBSITES = {
    "youtube": "https://youtube.com",
    "google": "https://google.com",
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "reddit": "https://reddit.com",
    "linkedin": "https://linkedin.com",
    "stack overflow": "https://stackoverflow.com",
    "stackoverflow": "https://stackoverflow.com",
    "chatgpt": "https://chatgpt.com",
    "claude ai": "https://claude.ai",
    "whatsapp": "https://web.whatsapp.com",
    "instagram": "https://instagram.com",
    "facebook": "https://facebook.com",
    "netflix": "https://netflix.com",
    "amazon": "https://amazon.com",
    "notion": "https://notion.so",
    "figma": "https://figma.com",
    "canva": "https://canva.com",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
    "docs": "https://docs.google.com",
    "google docs": "https://docs.google.com",
    "sheets": "https://sheets.google.com",
    "google sheets": "https://sheets.google.com",
    "calendar": "https://calendar.google.com",
    "google calendar": "https://calendar.google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
}


# ── Command patterns ─────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Normalize transcribed text for command matching."""
    text = text.lower().strip()
    # Remove filler words that Whisper sometimes adds
    text = re.sub(r"^(hey |okay |ok |please |can you |could you |would you )", "", text)
    # Remove trailing punctuation
    text = re.sub(r"[.!?,]+$", "", text)
    return text.strip()


def _fuzzy_match(name: str, registry: dict, threshold: float = 0.75) -> str | None:
    """Find the closest key in a registry using fuzzy matching.

    Threshold raised from 0.7 → 0.75 on May 15 after the "microsoft edge"
    → "microsoft excel" collision. Difflib's SequenceMatcher is purely
    character-level; with 0.7 it would accept any two app names that
    share ~70% of characters even if the unique tokens disagree. The
    real fix is the new ``app_index`` token matcher — this fuzzy match
    only runs as a last-resort fallback now, but the higher threshold
    further reduces collateral damage.
    """
    if name in registry:
        return name
    matches = difflib.get_close_matches(name, registry.keys(), n=1, cutoff=threshold)
    if matches:
        logger.info("Fuzzy matched %r → %r", name, matches[0])
        return matches[0]
    return None


def _open_app(app_name: str) -> CommandResult:
    """Launch an application by spoken name.

    Resolution order (changed May 15: was fuzzy-match against the legacy
    ``APPS`` dict, which made "open Microsoft Edge" launch Excel because
    difflib scored "microsoft excel" higher than the unrelated key
    "edge"). The new order is:

      1. **Special scripted handlers** — ``__CLAUDE__``, ``__WISPR_FLOW__``,
         ``__RECYCLE_BIN__``. These need custom behaviour (prompt support,
         file-system fallback, shell namespace) that a generic launcher
         can't provide.
      2. **Installed-app index** (``app_index.find_app``) — enumerates
         Get-StartApps and matches with token containment + Jaccard. This
         is the primary path; covers WhatsApp, YouTube, Edge, Excel,
         Chrome, etc., and launches via ``shell:AppsFolder`` so we never
         accidentally open the browser version of an installed app.
      3. **Legacy ``APPS`` dict** — final fallback for things app_index
         can't find (e.g. when Sir's Start Menu hasn't refreshed yet, or
         for protocol-like aliases such as ``start ms-settings:``).
    """
    key = app_name.lower().strip().rstrip(".")
    if not key:
        return CommandResult(handled=False, response="")

    # ── 1. Special scripted handlers ────────────────────────────────────
    special_apps = {k: v for k, v in APPS.items()
                    if isinstance(v, str) and v.startswith("__")}
    matched_special = _fuzzy_match(key, special_apps, threshold=0.75)
    if matched_special:
        cmd = special_apps[matched_special]
        if cmd == "__CLAUDE__":
            return _open_claude_desktop()
        if cmd == "__WISPR_FLOW__":
            return _open_wispr_flow()
        if cmd == "__RECYCLE_BIN__":
            _safe_popen('explorer.exe shell:RecycleBinFolder', shell=True)
            return CommandResult(handled=True, response="Opening the Recycle Bin.")

    # ── 2. Installed-app index (Get-StartApps) ───────────────────────────
    try:
        from app_index import find_app, launch as launch_app
        app = find_app(key)
        if app is not None:
            if launch_app(app):
                return CommandResult(handled=True,
                                     response=f"Opening {app.name}.")
            return CommandResult(
                handled=True, success=False,
                response=f"I found {app.name} but couldn't launch it, Sir.",
            )
    except Exception as exc:
        logger.debug("app_index lookup failed for %r: %s", key, exc)

    # ── 3. Legacy APPS dict fallback ─────────────────────────────────────
    matched = _fuzzy_match(key, APPS, threshold=0.75)
    cmd = APPS.get(matched) if matched else None
    if cmd and not cmd.startswith("__"):
        try:
            _safe_popen(cmd, shell=True)
            pretty = app_name.title()
            logger.info("Launched via legacy APPS dict: %s (cmd=%s)", pretty, cmd)
            return CommandResult(handled=True, response=f"Opening {pretty}.")
        except Exception as exc:
            logger.error("Failed to open %s: %s", app_name, exc)
            return CommandResult(
                handled=True, success=False,
                response=f"Sorry, I couldn't open {app_name}.",
            )

    return CommandResult(handled=False, response="")


def _open_wispr_flow() -> CommandResult:
    """Launch Wispr Flow. Tries Start Menu first, then known EXE paths."""
    # Try the installed-app index first — it'll find 'Wispr Flow' for us
    try:
        from app_index import find_app, launch as launch_app
        app = find_app("wispr flow")
        if app is not None and launch_app(app):
            return CommandResult(handled=True, response="Opening Wispr Flow.")
    except Exception:
        pass
    # File-system fallback for the rare case Wispr isn't in Start Menu
    for path in [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\WisprFlow\Wispr Flow.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\WisprFlow\Wispr Flow.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\WisprFlow\Wispr Flow.exe"),
    ]:
        if os.path.exists(path):
            _safe_popen([path], shell=False)
            return CommandResult(handled=True, response="Opening Wispr Flow.")
    return CommandResult(
        handled=True, success=False,
        response="I can't find Wispr Flow on this system, Sir.",
    )


def _open_website(site_name: str) -> CommandResult:
    """Open a website in the default browser."""
    matched = _fuzzy_match(site_name, WEBSITES)
    url = WEBSITES.get(matched) if matched else None
    if not url:
        # Check if it looks like a URL
        if "." in site_name and " " not in site_name:
            url = f"https://{site_name}" if not site_name.startswith("http") else site_name
        else:
            return CommandResult(handled=False, response="")

    try:
        if platform.system() == "Windows":
            os.startfile(url)
        elif platform.system() == "Darwin":
            subprocess.run(["open", url])
        else:
            subprocess.run(["xdg-open", url])

        pretty = site_name.title()
        logger.info("Opened website: %s (%s)", pretty, url)
        return CommandResult(handled=True, response=f"Opening {pretty}.")
    except Exception as exc:
        logger.error("Failed to open %s: %s", site_name, exc)
        return CommandResult(
            handled=True,
            response=f"Couldn't open {site_name}.",
            success=False,
        )


def _open_claude_desktop(prompt: str = "") -> CommandResult:
    """Launch Claude Desktop app. Opens a NEW chat — use sparingly.

    For sending messages to Claude, prefer ``_route_to_claude`` which
    finds an existing window and pastes there, preserving conversation
    memory. This function is the fallback when no Claude is running yet.
    """
    try:
        if prompt:
            encoded = urllib.parse.quote(prompt)
            url = f"claude://claude.ai/new?q={encoded}"
            os.startfile(url)
            return CommandResult(
                handled=True,
                response="Opening Claude with your message in a new chat.",
            )
        try:
            os.startfile("claude://claude.ai/new")
            return CommandResult(handled=True, response="Opening Claude.")
        except OSError:
            for path in [
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\claude\Claude.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Claude\Claude.exe"),
                os.path.expandvars(r"%PROGRAMFILES%\Claude\Claude.exe"),
            ]:
                if os.path.exists(path):
                    _safe_popen([path], shell=False)
                    return CommandResult(handled=True, response="Opening Claude Desktop.")
            return CommandResult(
                handled=True,
                response="Claude Desktop is not installed. Download it from claude.ai.",
                success=False,
            )
    except Exception as exc:
        logger.error("Failed to open Claude: %s", exc)
        return CommandResult(
            handled=True,
            response="Sorry, I couldn't open Claude.",
            success=False,
        )


def _route_to_claude(
    task: str,
    *,
    hint: Optional[str] = None,
) -> CommandResult:
    """Send ``task`` to whichever Claude window is most recently active.

    The key behaviour change vs. the old `_open_claude_desktop(prompt=...)`
    path: this **does not** spawn a new chat by default. It finds an
    existing Claude Desktop / Computer Use / Claude Code window, focuses
    it, and pastes the message into the input box — Sir's conversation
    context is preserved. Sir presses Enter when ready.

    If no Claude window is open, we fall back to opening Claude Desktop
    with the prompt pre-filled and explicitly warn Sir that this is a
    new chat (so the response is unambiguous).

    ``hint`` is "code" / "computer use" / "chat" / None.
    """
    if _DRY_RUN:
        logger.info("[DRY-RUN] _route_to_claude would route: %s", task[:80])
        return CommandResult(handled=True, response="[dry-run] would route to Claude.")

    try:
        from claude_router import send_to_existing_claude
    except Exception as exc:
        logger.debug("claude_router unavailable: %s — falling back to URI", exc)
        return _open_claude_desktop(prompt=task)

    result = send_to_existing_claude(task, hint=hint)
    try:
        from safety import audit
        audit(
            "claude_focus_existing" if result.success else "claude_open_new_chat",
            result="ok" if result.success else "fallback_new_chat",
            hint=hint or "auto",
            note=result.note,
        )
    except Exception:
        pass

    if result.success and result.window is not None:
        variant = result.window.variant.value.replace("_", " ")
        return CommandResult(
            handled=True,
            response=(
                f"Pasted into your existing Claude {variant} window, Sir. "
                f"Press Enter to send."
            ),
        )

    # No Claude window found — explicit new-chat fallback. Sir wanted
    # to never lose context silently, so the response makes the
    # transition explicit.
    logger.info("No existing Claude window (note=%s) — opening fresh chat",
                result.note)
    fresh = _open_claude_desktop(prompt=task)
    if fresh.handled and fresh.success:
        fresh.response = (
            "No Claude window was open, Sir, so I'm starting a new chat "
            "with your message ready. Open the variant you want first if "
            "you'd rather continue an existing conversation."
        )
    return fresh


def _send_to_claude(task: str, folder: str = "") -> CommandResult:
    """Send a task to Claude Code CLI."""
    try:
        cwd = folder if folder else os.path.expanduser("~")
        cmd = ["claude", "-p", task]
        logger.info("Sending to Claude Code: %s", task[:80])
        proc = _safe_popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
        )
        return CommandResult(
            handled=True,
            response=f"I've sent your task to Claude Code. It's working on it now.",
        )
    except FileNotFoundError:
        return CommandResult(
            handled=True,
            response="Claude Code is not installed. You need to run npm install -g @anthropic-ai/claude-code.",
            success=False,
        )
    except Exception as exc:
        return CommandResult(
            handled=True,
            response=f"Failed to send task to Claude: {exc}",
            success=False,
        )


def _handle_system_command(cmd_text: str) -> CommandResult:
    """Handle system commands like lock screen, volume, etc."""
    # ── Recycle Bin specific actions ─────────────────────────────────────
    if ("empty" in cmd_text or "clear" in cmd_text) and (
        "recycle" in cmd_text or "recycling" in cmd_text or "trash" in cmd_text or "bin" in cmd_text
    ):
        try:
            # PowerShell Clear-RecycleBin -Force is the cleanest way
            _safe_popen(
                ['powershell', '-NoProfile', '-Command', 'Clear-RecycleBin -Force -ErrorAction SilentlyContinue'],
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
            )
            return CommandResult(handled=True, response="Recycle Bin emptied, Sir.")
        except Exception as exc:
            logger.error("Clear-RecycleBin failed: %s", exc)
            return CommandResult(handled=True, success=False,
                                 response="I couldn't empty the Recycle Bin, Sir.")

    if "lock" in cmd_text and ("screen" in cmd_text or "computer" in cmd_text):
        _safe_popen("rundll32.exe user32.dll,LockWorkStation", shell=True)
        return CommandResult(handled=True, response="Locking your screen.")

    if ("sleep" in cmd_text and ("computer" in cmd_text or "laptop" in cmd_text or "machine" in cmd_text or "pc" in cmd_text)) \
            or cmd_text in ("sleep", "go to sleep", "put to sleep"):
        # Note: this works only if hibernation is OFF.
        _safe_popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
        return CommandResult(handled=True, response="Sleeping, Sir.")

    if cmd_text in ("restart", "reboot", "restart the computer", "reboot the computer"):
        _safe_popen("shutdown /r /t 5", shell=True)
        return CommandResult(handled=True, response="Restarting in five seconds, Sir.")

    if cmd_text in ("shut down", "shutdown", "shut down the computer", "power off"):
        _safe_popen("shutdown /s /t 10", shell=True)
        return CommandResult(handled=True, response="Shutting down in ten seconds. Say cancel to abort.")

    if cmd_text in ("cancel shutdown", "abort shutdown", "cancel"):
        _safe_popen("shutdown /a", shell=True)
        return CommandResult(handled=True, response="Shutdown cancelled, Sir.")

    if "screenshot" in cmd_text or "screen shot" in cmd_text:
        _safe_popen("snippingtool", shell=True)
        return CommandResult(handled=True, response="Opening the snipping tool.")

    if "volume up" in cmd_text or "turn up" in cmd_text:
        # Use PowerShell to increase volume
        _safe_popen(
            'powershell -Command "(New-Object -ComObject WScript.Shell).SendKeys([char]175)"',
            shell=True,
        )
        return CommandResult(handled=True, response="Volume up.")

    if "volume down" in cmd_text or "turn down" in cmd_text:
        _safe_popen(
            'powershell -Command "(New-Object -ComObject WScript.Shell).SendKeys([char]174)"',
            shell=True,
        )
        return CommandResult(handled=True, response="Volume down.")

    if "mute" in cmd_text:
        _safe_popen(
            'powershell -Command "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"',
            shell=True,
        )
        return CommandResult(handled=True, response="Toggled mute.")

    return CommandResult(handled=False, response="")


# ── Main router ──────────────────────────────────────────────────────────────

def route_command(raw_text: str) -> CommandResult:
    """
    Try to handle a voice command locally for instant response.

    Returns:
      CommandResult with handled=True if the command was processed locally.
      CommandResult with handled=False if it should go to the Jarvis backend.

    Usage:
        result = route_command("open chrome")
        if result.handled:
            speak(result.response)
        else:
            # send to Jarvis backend API
            ...
    """
    text = _normalize(raw_text)

    if not text:
        return CommandResult(handled=False, response="")

    # ── Pattern: routines ("perform my daily tasks", "morning routine", ...) ─
    # Sir's autopilot trigger. The actual work runs on a background thread
    # so the asyncio loop and the HUD stay responsive; the response below
    # is just the kick-off acknowledgement that goes to TTS.
    result = _maybe_run_routine(text)
    if result.handled:
        return result

    # ── Pattern: "refresh my apps" — rebuild the installed-app index ─────
    if text in ("refresh my apps", "refresh apps", "rescan apps",
                "rebuild app index", "update app list"):
        try:
            from app_index import refresh
            n = refresh()
            return CommandResult(
                handled=True,
                response=f"Refreshed. I now know about {n} apps, Sir.",
            )
        except Exception as exc:
            logger.error("App index refresh failed: %s", exc)
            return CommandResult(
                handled=True, success=False,
                response="I couldn't refresh the app list, Sir.",
            )

    # ── Pattern: "open X and Y" / "open X then Y" — multi-app launch ─────
    multi_match = re.match(r"^open\s+(.+)$", text)
    if multi_match and re.search(r"\s+(?:and|then|and then)\s+", multi_match.group(1)):
        targets = re.split(r"\s+(?:and|then|and then)\s+", multi_match.group(1))
        opened = []
        for t in targets:
            t = t.strip().rstrip(".")
            if not t:
                continue
            r = _open_app(t)
            if not r.handled:
                r = _open_website(t)
            if r.handled and r.success:
                opened.append(t.title())
        if opened:
            joined = ", ".join(opened[:-1]) + (f", and {opened[-1]}" if len(opened) > 1 else opened[0])
            return CommandResult(handled=True, response=f"Opening {joined}.")

    # ── Pattern: "open <app/website>" ─────────────────────────────────────
    open_match = re.match(r"^open\s+(?:the\s+)?(.+)$", text)
    if open_match:
        target = open_match.group(1).strip().rstrip(".")

        # _open_app now consults app_index FIRST (token-aware match against
        # every Start-Menu-registered app on the machine), so installed
        # apps always beat the website fallback. The legacy `_smart_open`
        # path with its tiny _URI_PROTOCOLS / _APP_PATHS tables is now
        # redundant for the common cases (WhatsApp, YouTube, Edge, ...)
        # — it stays below as a safety net for apps that aren't in the
        # Start Menu but DO ship a known EXE or URI handler.
        result = _open_app(target)
        if result.handled:
            return result

        # Safety-net: URI / EXE-path table for non-Start-Menu installs
        key = target.lower()
        if key in _URI_PROTOCOLS or key in _APP_PATHS:
            result = _smart_open(key, fallback_url=WEBSITES.get(key))
            if result.handled:
                return result

        # Final fallback: open the website in a browser
        result = _open_website(target)
        if result.handled:
            return result

    # ── Pattern: "go to <website>" ────────────────────────────────────────
    goto_match = re.match(r"^(?:go to|navigate to|visit)\s+(.+)$", text)
    if goto_match:
        target = goto_match.group(1).strip()
        result = _open_website(target)
        if result.handled:
            return result

    # ── Pattern: "search for <query>" on the web ──────────────────────────
    search_match = re.match(r"^(?:search|search for|google|look up)\s+(.+)$", text)
    if search_match:
        query = search_match.group(1).strip()
        encoded = urllib.parse.quote(query)
        url = f"https://www.google.com/search?q={encoded}"
        try:
            os.startfile(url)
            return CommandResult(handled=True, response=f"Searching for {query}.")
        except Exception:
            pass

    # ── Pattern: "ask claude code to <X>" — explicit CLI delegation ──────
    # Must come BEFORE the generic "ask claude to" because "claude code"
    # is a more specific match.
    claude_code_match = re.match(
        r"^(?:ask claude code to|tell claude code to|have claude code|"
        r"send (?:this )?to claude code|claude code)\s+(.+)$",
        text,
    )
    if claude_code_match:
        task = claude_code_match.group(1).strip().rstrip(".")
        # If Claude Code is already running in a terminal, route there so
        # the existing session's context is preserved.
        try:
            from claude_router import find_best_window
            existing = find_best_window(hint="code")
            if existing is not None:
                return _route_to_claude(task, hint="code")
        except Exception:
            pass
        # Otherwise spawn a fresh CLI invocation.
        return _send_to_claude(task)

    # ── Pattern: "ask claude computer use to <X>" / "ask co-work claude" ──
    claude_cu_match = re.match(
        r"^(?:ask claude(?: computer use| computer\-use| co\-work| cowork)? to|"
        r"tell claude(?: computer use| computer\-use| co\-work| cowork)? to|"
        r"claude(?: computer use| computer\-use| co\-work| cowork) please)\s+(.+)$",
        text,
    )
    if claude_cu_match:
        # Pull which variant was named ("computer use" / "co-work" / none).
        # If "computer use" or "co-work" was uttered, force that hint.
        hint = None
        m_low = text.lower()
        if "computer use" in m_low or "computer-use" in m_low:
            hint = "computer use"
        elif "co-work" in m_low or "cowork" in m_low:
            hint = "computer use"  # Sir's "co-work" = Computer Use (confirmed)
        task = claude_cu_match.group(1).strip().rstrip(".")
        return _route_to_claude(task, hint=hint)

    # ── Pattern: "ask claude to <X>" — generic. Route to most-recent window.
    # If no Claude is open, fall back to a new Desktop chat with warning.
    claude_desktop_match = re.match(
        r"^(?:ask claude to|tell claude to|have claude|let claude|"
        r"claude please|claude can you|claude could you)\s+(.+)$",
        text,
    )
    if claude_desktop_match:
        task = claude_desktop_match.group(1).strip().rstrip(".")
        return _route_to_claude(task, hint=None)

    # ── Pattern: "send to claude <X>" / "give task to claude <X>" ────────
    # Old generic delegation. Routes to existing window first.
    claude_send_match = re.match(
        r"^(?:send (?:this )?to claude|give (?:this )?(?:task )?to claude|"
        r"claude (?:do|handle|work on))\s+(.+)$",
        text,
    )
    if claude_send_match:
        task = claude_send_match.group(1).strip().rstrip(".")
        return _route_to_claude(task, hint=None)

    # ── Pattern: "open claude" (standalone) ───────────────────────
    # If Claude Desktop is already running, focus the existing window
    # instead of opening a new chat. Preserves the sidebar of past
    # conversations exactly where Sir left them.
    if text in ("open claude", "launch claude", "start claude", "claude"):
        try:
            from claude_router import find_best_window, focus_window
            existing = find_best_window(hint="chat")
            if existing is not None and focus_window(existing.hwnd):
                return CommandResult(
                    handled=True,
                    response="Bringing your Claude window forward, Sir.",
                )
        except Exception as exc:
            logger.debug("Could not focus existing Claude: %s", exc)
        return _open_claude_desktop()

    # ── Pattern: shutdown / sleep commands ──────────────────────────
    if _is_shutdown_phrase(text):
        return _trigger_shutdown()

    # ── Pattern: skill library introspection ─────────────────────────
    if text in (
        "what skills do you have", "what skills do you know",
        "list your skills", "list my skills", "show me your skills",
        "what can you do", "list skills",
    ):
        try:
            from agent_brain import list_skills
            skills = list_skills()
            if not skills:
                return CommandResult(handled=True,
                                     response="No skills registered yet, Sir.")
            names = ", ".join(s.name.replace("_", " ") for s in skills[:10])
            extra = f", and {len(skills) - 10} more" if len(skills) > 10 else ""
            return CommandResult(
                handled=True,
                response=f"I have {len(skills)} skills, Sir: {names}{extra}.",
            )
        except Exception as exc:
            logger.debug("list_skills voice command failed: %s", exc)

    # ── Pattern: corrections ─────────────────────────────────────────
    # "JARVIS, remember a correction: don't open the chat tab, use cowork."
    # "JARVIS, don't do X. Do Y instead."
    # "Remember this: <anything>."
    # We capture whatever comes after the trigger and store it verbatim
    # — the planner LLM is responsible for interpreting natural-language
    # corrections in context. We don't try to be clever here.
    correction_match = re.match(
        r"^(?:remember (?:a |this |the )?correction[:\s]+|"
        r"remember this[:\s]+|"
        r"(?:jarvis|friday)?,?\s*don'?t\s+|"
        r"note to self[:\s]+|"
        r"correction[:\s]+|"
        r"from now on[,\s]+)(.+)$",
        text,
    )
    if correction_match:
        body = correction_match.group(1).strip().rstrip(".")
        if body:
            try:
                from agent_brain import add_correction
                if add_correction(body):
                    return CommandResult(
                        handled=True,
                        response="Noted, Sir. I'll act on that from now on.",
                    )
            except Exception as exc:
                logger.debug("add_correction failed: %s", exc)
            return CommandResult(
                handled=True, success=False,
                response="I heard the correction but couldn't save it, Sir.",
            )

    # ── Pattern: system commands ──────────────────────────────────────────
    result = _handle_system_command(text)
    if result.handled:
        return result

    # ── Pattern: "what time is it" ────────────────────────────────────────
    if "what time" in text or "what's the time" in text:
        from datetime import datetime
        now = datetime.now().strftime("%I:%M %p")
        return CommandResult(handled=True, response=f"It's {now}.")

    if "what day" in text or "what's the date" in text or "today's date" in text:
        from datetime import datetime
        now = datetime.now().strftime("%A, %B %d, %Y")
        return CommandResult(handled=True, response=f"Today is {now}.")

    # ── Nothing matched — let the backend handle it ──────────────────────
    return CommandResult(handled=False, response="")


# ── Shutdown phrase detection + dispatcher ────────────────────────────

_SHUTDOWN_PHRASES = (
    "shut down friday", "shut down jarvis", "shutdown friday",
    "shutdown jarvis", "go to sleep friday", "sleep friday",
    "power down friday", "stop friday", "turn off friday",
    "shut yourself down", "shut down",  # "shut down" alone is risky;
                                          # safety confirmation gates it
    "that will be all friday", "that'll be all friday",
    "you can shut down now", "power off friday",
)


def _is_shutdown_phrase(text: str) -> bool:
    t = (text or "").lower().strip().rstrip(".")
    return t in _SHUTDOWN_PHRASES or any(
        t == p.replace("friday", "jarvis") for p in _SHUTDOWN_PHRASES
    )


def _trigger_shutdown() -> CommandResult:
    """Run the confirmation → audit → execute sequence on a worker thread.

    The asyncio loop in main.py is busy running the TTS for the
    confirmation prompt, so we can't block here. We spawn a thread that
    drives the safety primitives, and we return an empty handled
    response — the TTS narration comes from inside the safety layer's
    confirm-handler (which main.py provides).
    """
    if _SHUTDOWN_HANDLER is None:
        logger.warning("Shutdown requested but no handler registered.")
        return CommandResult(
            handled=True, success=False,
            response="I can't shut down right now, Sir — my shutdown wiring isn't ready.",
        )
    if _DRY_RUN:
        logger.info("[DRY-RUN] shutdown_friday would execute")
        return CommandResult(handled=True, response="[dry-run] shutdown skipped.")

    def _worker() -> None:
        try:
            from safety import audit, request_confirmation
        except Exception as exc:
            logger.error("safety module unavailable: %s", exc)
            return
        audit("shutdown_friday", result="requested")
        confirmed = request_confirmation(
            "Are you sure you want to shut me down, Sir? Say yes to confirm.",
            timeout_s=8.0,
            action="shutdown_friday",
        )
        if confirmed:
            audit("shutdown_friday", result="executing")
            try:
                _SHUTDOWN_HANDLER()
            except Exception as exc:
                logger.error("Shutdown handler crashed: %s", exc)
                audit("shutdown_friday", result="error", note=str(exc))

    threading.Thread(
        target=_worker, name="FridayShutdownConfirm", daemon=True,
    ).start()
    # Empty response — the confirmation prompt itself is the spoken output.
    return CommandResult(handled=True, response="")


# ── Routine dispatch helper ─────────────────────────────────────────────────

_ROUTINE_TRIGGERS = (
    "perform my daily tasks", "run my daily tasks", "do my daily tasks",
    "start my daily tasks", "execute my daily tasks", "daily tasks",
    "perform daily tasks", "run daily tasks", "do daily tasks",
    "morning routine", "run my morning routine", "do my morning routine",
    "perform my morning routine", "start my morning routine",
)

_ROUTINE_NAME_RE = re.compile(
    r"^(?:perform|run|do|execute|start)\s+(?:my\s+)?(.+?)(?:\s+routine)?\.?$"
)


def _maybe_run_routine(text: str) -> CommandResult:
    """If `text` looks like a routine trigger, kick it off and return
    the acknowledgement CommandResult. Otherwise returns handled=False.
    """
    candidate: Optional[str] = None
    if text in _ROUTINE_TRIGGERS:
        candidate = "morning routine" if "morning" in text else "daily tasks"
    else:
        m = _ROUTINE_NAME_RE.match(text)
        if m:
            inner = m.group(1).strip()
            # Only accept short, plausible routine names — don't hijack
            # things like "run the test" or "start the timer".
            if 0 < len(inner.split()) <= 4:
                candidate = inner

    if not candidate:
        return CommandResult(handled=False, response="")

    try:
        from routines import find_routine, list_routines, run_routine_async
    except Exception as exc:
        logger.error("routines module unavailable: %s", exc)
        return CommandResult(handled=False, response="")

    found = find_routine(candidate)
    if not found:
        # Sir said something that looked like a routine trigger but the
        # name doesn't match anything. Tell him — don't silently forward
        # to the backend, that would be more confusing.
        try:
            known = ", ".join(f'"{k}"' for k in list_routines())
        except Exception:
            known = ""
        msg = f"I don't have a routine called {candidate!r}, Sir."
        if known:
            msg += f" Try one of: {known}."
        return CommandResult(handled=True, success=False, response=msg)

    key, routine = found
    run_routine_async(key)
    pretty = routine.get("name", key)
    return CommandResult(handled=True, response=f"Starting {pretty}, Sir.")


# ── Standalone test ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    test_commands = [
        "Open Chrome",
        "open YouTube",
        "Go to GitHub",
        "Open Claude",
        "Send to Claude fix the bug in main.py",
        "What time is it",
        "Lock screen",
        "open VS Code",
        "search for Python voice assistant tutorial",
        "What's the weather like?",  # should fall through
    ]

    for cmd in test_commands:
        result = route_command(cmd)
        status = "✅ LOCAL" if result.handled else "↗️  BACKEND"
        print(f"  {status}  |  \"{cmd}\"  →  {result.response or '(forwarded to Jarvis)'}")

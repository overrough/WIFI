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

import logging
import os
import platform
import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Result of a local command execution."""
    handled: bool           # True = command was handled locally
    response: str           # speech response for TTS
    success: bool = True    # whether the command succeeded


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

    # Media
    "spotify": "start spotify",
    "vlc": "start vlc",

    # AI tools
    "claude": "__CLAUDE__",     # special handler
    "claude desktop": "__CLAUDE__",
    "cursor": "start cursor",
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


def _open_app(app_name: str) -> CommandResult:
    """Launch an application."""
    cmd = APPS.get(app_name)
    if not cmd:
        return CommandResult(handled=False, response="")

    if cmd == "__CLAUDE__":
        return _open_claude_desktop()

    try:
        subprocess.Popen(cmd, shell=True)
        pretty = app_name.title()
        logger.info("Launched app: %s (cmd=%s)", pretty, cmd)
        return CommandResult(handled=True, response=f"Opening {pretty}.")
    except Exception as exc:
        logger.error("Failed to open %s: %s", app_name, exc)
        return CommandResult(
            handled=True,
            response=f"Sorry, I couldn't open {app_name}.",
            success=False,
        )


def _open_website(site_name: str) -> CommandResult:
    """Open a website in the default browser."""
    url = WEBSITES.get(site_name)
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
    """Launch Claude Desktop app."""
    try:
        if prompt:
            encoded = urllib.parse.quote(prompt)
            url = f"claude://claude.ai/new?q={encoded}"
            os.startfile(url)
            return CommandResult(
                handled=True,
                response=f"Opening Claude with your message.",
            )
        else:
            try:
                os.startfile("claude://claude.ai/new")
            except OSError:
                # Fallback: try direct exe path
                for path in [
                    os.path.expandvars(r"%LOCALAPPDATA%\Programs\claude\Claude.exe"),
                    os.path.expandvars(r"%LOCALAPPDATA%\Claude\Claude.exe"),
                    os.path.expandvars(r"%PROGRAMFILES%\Claude\Claude.exe"),
                ]:
                    if os.path.exists(path):
                        subprocess.Popen([path], shell=False)
                        return CommandResult(handled=True, response="Opening Claude Desktop.")
                return CommandResult(
                    handled=True,
                    response="Claude Desktop is not installed. Download it from claude.ai.",
                    success=False,
                )
            return CommandResult(handled=True, response="Opening Claude.")
    except Exception as exc:
        logger.error("Failed to open Claude: %s", exc)
        return CommandResult(
            handled=True,
            response="Sorry, I couldn't open Claude.",
            success=False,
        )


def _send_to_claude(task: str, folder: str = "") -> CommandResult:
    """Send a task to Claude Code CLI."""
    try:
        cwd = folder if folder else os.path.expanduser("~")
        cmd = ["claude", "-p", task]
        logger.info("Sending to Claude Code: %s", task[:80])
        proc = subprocess.Popen(
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
    if "lock" in cmd_text and ("screen" in cmd_text or "computer" in cmd_text):
        subprocess.Popen("rundll32.exe user32.dll,LockWorkStation", shell=True)
        return CommandResult(handled=True, response="Locking your screen.")

    if "screenshot" in cmd_text or "screen shot" in cmd_text:
        subprocess.Popen("snippingtool", shell=True)
        return CommandResult(handled=True, response="Opening the snipping tool.")

    if "volume up" in cmd_text or "turn up" in cmd_text:
        # Use PowerShell to increase volume
        subprocess.Popen(
            'powershell -Command "(New-Object -ComObject WScript.Shell).SendKeys([char]175)"',
            shell=True,
        )
        return CommandResult(handled=True, response="Volume up.")

    if "volume down" in cmd_text or "turn down" in cmd_text:
        subprocess.Popen(
            'powershell -Command "(New-Object -ComObject WScript.Shell).SendKeys([char]174)"',
            shell=True,
        )
        return CommandResult(handled=True, response="Volume down.")

    if "mute" in cmd_text:
        subprocess.Popen(
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

    # ── Pattern: "open <app/website>" ─────────────────────────────────────
    open_match = re.match(r"^open\s+(.+)$", text)
    if open_match:
        target = open_match.group(1).strip()

        # Check apps first
        result = _open_app(target)
        if result.handled:
            return result

        # Then check websites
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

    # ── Pattern: "send to claude <task>" or "give this to claude <task>" ──
    claude_send_match = re.match(
        r"^(?:send (?:this )?to claude|give (?:this )?(?:task )?to claude|"
        r"ask claude to|tell claude to|claude (?:do|handle|work on))\s+(.+)$",
        text,
    )
    if claude_send_match:
        task = claude_send_match.group(1).strip()
        return _send_to_claude(task)

    # ── Pattern: "claude cowork <task>" ───────────────────────────────────
    cowork_match = re.match(
        r"^(?:claude cowork|cowork with claude|let claude work on|claude work on)\s+(.+)$",
        text,
    )
    if cowork_match:
        task = cowork_match.group(1).strip()
        return _send_to_claude(task)

    # ── Pattern: "open claude" (standalone) ───────────────────────────────
    if text in ("open claude", "launch claude", "start claude", "claude"):
        return _open_claude_desktop()

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

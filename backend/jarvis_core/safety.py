"""
Safety confirmation layer — JARVIS never does irreversible things blindly.

Pattern:
  Every destructive tool takes a `confirm: bool = False` parameter.
  - confirm=False (default): tool returns a JARVIS-voice description of
    what WOULD happen, asks Sir to confirm, and does nothing.
  - confirm=True: tool actually executes.

The agent's system prompt mandates that confirm=True is only passed after
Sir has explicitly approved in conversation. The behaviour rule:

    "CONFIRM BEFORE IRREVERSIBLE ACTIONS. Sending external emails,
    deleting files, posting publicly, executing destructive shell commands,
    spending money. Describe the exact action. Wait for explicit yes."

Helpers in this module make it cheap to apply this pattern to a tool.
"""

import re
from typing import Optional

# Shell patterns that always require confirmation, even if not in _BLOCKED_SHELL.
# These are operations that destroy data or change system state irreversibly.
DESTRUCTIVE_PATTERNS = [
    r"\brm\s+-rf?\b",
    r"\brmdir\b",
    r"\bdel\s+/[fsq]",
    r"\bdel\s+\*",
    r"\berase\b",
    r"\bformat\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bdrop\s+(table|database|schema)\b",
    r"\btruncate\s+table\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bgit\s+push\s+.*--force\b",
    r"\bgit\s+reset\s+--hard\b",
]

_DESTRUCTIVE_RE = re.compile("|".join(DESTRUCTIVE_PATTERNS), re.IGNORECASE)


def is_destructive_shell(command: str) -> bool:
    """True if the shell command matches any destructive pattern."""
    return bool(_DESTRUCTIVE_RE.search(command or ""))


def confirmation_prompt(
    action: str,
    target: str,
    details: Optional[str] = None,
) -> str:
    """
    Build a JARVIS-voice confirmation message for the user.

    Example:
        confirmation_prompt(
            "send email", "alice@client.com",
            details="Subject: Project update | Body: Hi Alice..."
        )

        → "Sir, this will send email to alice@client.com.
           Subject: Project update | Body: Hi Alice...
           Reply 'yes' to proceed, or anything else to abort."
    """
    lines = [f"Sir, this will {action}: {target}."]
    if details:
        lines.append(details)
    lines.append("Reply 'yes' to proceed, or anything else to abort.")
    return "\n".join(lines)

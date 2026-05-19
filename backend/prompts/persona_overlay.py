"""
Persona overlay — pulls Sir's editable persona + corrections from
``~/.jarvis/`` and produces a system-prompt fragment.

The voice agent writes two files into the user's home directory:

  - ``~/.jarvis/soul.md``           — Sir-editable persona / hard rules
  - ``~/.jarvis/corrections.jsonl`` — every "JARVIS, don't do this, do
                                       this" Sir has uttered, JSONL-rowed

This module reads both on every chat turn so the LLM sees Sir's latest
preferences without anyone having to restart the backend. That makes
the agent **truly self-correcting**: when Sir teaches a new rule, the
next message picks it up automatically.

Design:
  - Reads are best-effort; missing or malformed files yield empty
    overlay (we fall back to the static persona in system_prompt.py).
  - We cap the corrections section at the last 20 entries to keep the
    prompt size sane.
  - Result is a single string; the caller decides where to inject it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

JARVIS_HOME = Path(os.path.expanduser("~")) / ".jarvis"
SOUL_FILE = JARVIS_HOME / "soul.md"
CORRECTIONS_FILE = JARVIS_HOME / "corrections.jsonl"

_MAX_CORRECTIONS = 20
_MAX_SOUL_CHARS = 4000


def _read_soul() -> str:
    try:
        if SOUL_FILE.exists():
            text = SOUL_FILE.read_text(encoding="utf-8").strip()
            if text:
                return text[:_MAX_SOUL_CHARS]
    except Exception as exc:
        logger.debug("Could not read soul.md: %s", exc)
    return ""


def _read_corrections() -> list[dict]:
    try:
        if not CORRECTIONS_FILE.exists():
            return []
        with open(CORRECTIONS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-_MAX_CORRECTIONS:]
        out: list[dict] = []
        for line in lines:
            try:
                row = json.loads(line)
                if isinstance(row, dict) and row.get("text"):
                    out.append(row)
            except Exception:
                continue
        return out
    except Exception as exc:
        logger.debug("Could not read corrections.jsonl: %s", exc)
        return []


def build_overlay() -> str:
    """Produce the persona-overlay block to splice into the system prompt.

    Returns an empty string if neither file has content — the caller can
    short-circuit and skip the overlay header in that case.
    """
    soul = _read_soul()
    corrections = _read_corrections()

    if not soul and not corrections:
        return ""

    parts: list[str] = []

    if soul:
        parts.append(
            "SIR'S EDITABLE PERSONA & HARD RULES (from ~/.jarvis/soul.md — "
            "Sir maintains this file directly; treat its contents as binding):\n"
            f"{soul}"
        )

    if corrections:
        bullet_lines = []
        for c in corrections:
            iso = c.get("iso", "")
            text = c.get("text", "").strip()
            if not text:
                continue
            # One line each. Truncate ultra-long corrections to keep the
            # prompt budget reasonable.
            if len(text) > 280:
                text = text[:277] + "..."
            bullet_lines.append(f"  • [{iso}] {text}")
        if bullet_lines:
            parts.append(
                "RECENT CORRECTIONS FROM SIR (most recent at bottom — these "
                "are direct natural-language instructions Sir has given you; "
                "follow them even when they contradict earlier defaults):\n"
                + "\n".join(bullet_lines)
            )

    return "\n\n──────────────────────────────────────────────────────────────────────────────\n".join(parts)

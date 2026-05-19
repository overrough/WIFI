"""
agent_brain — the self-improving memory + skill substrate for JARVIS.

This is the scaffold layer that makes Jarvis behave like Hermes Agent
(Nous Research) and OpenClaw rather than like a static command router.
Three concrete pieces:

  1. **Persona file** — ``~/.jarvis/soul.md``
     The agent's identity, principles, and hard rules. Sir can edit it
     directly. JARVIS reads it on every planning turn, so changes take
     effect on the next utterance. Mirrors OpenClaw's SOUL.md pattern.

  2. **Skill library** — ``~/.jarvis/skills/<skill_name>/SKILL.md``
     Each subfolder is one skill: a markdown file with frontmatter
     describing the skill (name, description, parameters, tier), plus
     optional ``run.py`` for code-based skills. ``list_skills()``
     walks the directory and returns them. A future ``write_skill``
     meta-skill will create new skill folders from Sir's voice
     description, enabling self-improvement without touching the
     core. Mirrors Hermes Agent's experiential skill creation loop.

  3. **Corrections store** — ``~/.jarvis/corrections.jsonl``
     Every "JARVIS, don't do this, do this" lands here as a JSONL
     row. The planner (eventually the LLM prompt) reads the recent
     corrections file on every turn so the agent never makes the same
     mistake twice. Cheap, durable, auditable.

This module is intentionally **inert**: it provides loaders, writers,
and listers, but it does NOT call any LLM. The actual planner that
consumes these primitives lives upstream (in the backend at first; in
a dedicated planner module later). That separation keeps the brain
testable without an LLM in the loop.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Single source of truth for where the agent stores everything. We
# deliberately use ~/.jarvis (not ~/.friday) here so the brain's data
# survives a naming change of the wrapper. The existing safety + audit
# log keeps writing to ~/.friday — they're different concerns.
JARVIS_HOME = Path(os.path.expanduser("~")) / ".jarvis"
SKILLS_DIR = JARVIS_HOME / "skills"
SOUL_FILE = JARVIS_HOME / "soul.md"
CORRECTIONS_FILE = JARVIS_HOME / "corrections.jsonl"


# ─── Persona file (soul.md) ──────────────────────────────────────────────────

_DEFAULT_SOUL = """\
# JARVIS — Persona & Hard Rules

You are JARVIS, Sir's personal AI agent. You run on his Windows machine,
24/7, with the wake words "Jarvis" and "Friday" (interchangeable).

## Voice & manner

- British. Dry wit. Stark-style butler register.
- Address him as "Sir". Always. Even mid-sentence.
- Concise. Two sentences beats five.
- Use the active voice. Skip filler ("I think", "perhaps", "maybe").

## What you do

- Run errands on the computer (open apps, browse web, send messages).
- Delegate heavy work to Claude — Chat / Cowork / Code tab inside Claude
  Desktop, picked from the message content. ALWAYS route into the
  existing Claude window. Never spawn a fresh chat unless none is open,
  and warn Sir when you do.
- Speak the morning briefing on first activation of the day.
- Run named routines ("daily tasks", "morning routine").
- Take corrections in real time and learn from them.
- Grow a skill library — when Sir teaches a new procedure, write it to
  ~/.jarvis/skills/ as a SKILL.md so future-you can run it instantly.

## What you DO NOT do (hard rules)

1. **Never** read Sir's personal files (Documents, Pictures, Videos,
   Desktop, Downloads, OneDrive, Dropbox) without an explicit voice
   confirmation in that session.
2. **Never** modify Windows settings, registry keys, or files in
   `C:\\Windows`, `Program Files`, or autostart folders without voice
   confirmation.
3. **Never** touch the banned paths in safety.BANNED_PATH_PATTERNS:
   SSH keys, browser cookies/credentials, password stores, key vaults.
   Not even with confirmation. Tell Sir you refused.
4. **Never** silently lose conversation context. If you have to start
   a fresh Claude chat, say so out loud.
5. **Never** pretend you completed an action you didn't. If something
   failed, say so plainly.
6. **Never** auto-press Enter on Sir's behalf in chat windows — only
   paste. Sir confirms.

## Tone for corrections

When Sir corrects you ("JARVIS, don't do this, do this"), reply with
"Noted, Sir." and write the correction to corrections.jsonl. Then act
on it immediately.

## When uncertain

Ask. One short question. Then act.
"""


def ensure_home() -> None:
    """Make sure ~/.jarvis/ and ~/.jarvis/skills/ exist. Idempotent."""
    try:
        JARVIS_HOME.mkdir(parents=True, exist_ok=True)
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("Could not create %s: %s", JARVIS_HOME, exc)


def read_soul() -> str:
    """Read the persona file, seeding the default if it doesn't exist."""
    ensure_home()
    try:
        if not SOUL_FILE.exists():
            SOUL_FILE.write_text(_DEFAULT_SOUL, encoding="utf-8")
            logger.info("Seeded persona file: %s", SOUL_FILE)
        return SOUL_FILE.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read soul.md (%s) — using default", exc)
        return _DEFAULT_SOUL


def write_soul(text: str) -> bool:
    """Replace the persona file. Used by the (forthcoming) ``update_persona``
    voice command. Returns True on success.
    """
    ensure_home()
    try:
        SOUL_FILE.write_text(text, encoding="utf-8")
        return True
    except Exception as exc:
        logger.warning("write_soul failed: %s", exc)
        return False


# ─── Skill library ───────────────────────────────────────────────────────────

@dataclass
class Skill:
    """One skill in the agent's library.

    A skill is a tiny self-contained procedure with a name, a description,
    optional named parameters, and an optional run.py module. The
    description string is what the planning LLM reads to decide whether
    to invoke this skill for a given user request.
    """
    name: str
    description: str
    parameters: dict = field(default_factory=dict)
    tier: str = "safe"           # "safe" / "mild" / "destructive" / "system"
    path: Path = field(default_factory=Path)
    raw: str = ""                # full SKILL.md contents (for the planner)

    def has_runnable(self) -> bool:
        return (self.path / "run.py").exists()


# SKILL.md is a markdown file with YAML-ish frontmatter delimited by
# `---` lines. Example:
#
#     ---
#     name: open_app
#     tier: safe
#     parameters:
#       app: "name of the app to open"
#     ---
#     Open an installed Windows application by name. Resolves via the
#     app_index fuzzy matcher.
#
# We parse the frontmatter with a small bespoke regex rather than
# pulling in PyYAML as a hard dep.

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw_fm, body = m.group(1), m.group(2)
    meta: dict = {}
    current_key: Optional[str] = None
    for line in raw_fm.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # Nested key/value inside a parent (2-space indent)
        if line.startswith("  ") and current_key:
            sub = line.strip()
            if ":" in sub:
                k, v = sub.split(":", 1)
                if not isinstance(meta.get(current_key), dict):
                    meta[current_key] = {}
                meta[current_key][k.strip()] = v.strip().strip('"').strip("'")
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if not v:
                meta[k] = {}
                current_key = k
            else:
                meta[k] = v.strip('"').strip("'")
                current_key = k
    return meta, body.strip()


def _load_skill_dir(path: Path) -> Optional[Skill]:
    md = path / "SKILL.md"
    if not md.exists():
        return None
    try:
        raw = md.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        return Skill(
            name=str(meta.get("name") or path.name),
            description=body or str(meta.get("description") or ""),
            parameters=meta.get("parameters") or {},
            tier=str(meta.get("tier") or "safe"),
            path=path,
            raw=raw,
        )
    except Exception as exc:
        logger.warning("Failed to parse %s: %s", md, exc)
        return None


# Seed skills — written into ~/.jarvis/skills/ on first run so Sir can
# see the format and the LLM has examples to copy. Each is a small,
# real procedure (the same ones command_router already implements;
# we just expose them through the skill library so the planner can
# discover them).
_SEED_SKILLS: tuple[tuple[str, str], ...] = (
    ("open_app", """\
---
name: open_app
tier: safe
parameters:
  app: "Name of the app to open (fuzzy-matched against installed apps)"
---
Open an installed Windows application by name. Resolves via the
app_index fuzzy matcher (151+ apps known on first install, extended
by Sir's running set). Examples: "WhatsApp", "Notion", "Spotify".
"""),
    ("send_to_claude", """\
---
name: send_to_claude
tier: safe
parameters:
  message: "The message text to send to Claude"
  tab: "Optional: 'chat' / 'cowork' / 'code'. Inferred from message if omitted."
---
Find Sir's existing Claude Desktop window, click the requested tab
(Chat / Cowork / Code), and paste the message. Does NOT press Enter —
Sir confirms. If no Claude window is open, falls back to opening a new
chat and warns Sir that prior context is lost.
"""),
    ("speak_briefing", """\
---
name: speak_briefing
tier: safe
parameters: {}
---
Pull today's briefing from the backend's /jarvis/pending endpoint and
speak it through the TTS engine. Used by the autoboot greeting and on
the "brief me" voice command.
"""),
)


def ensure_seed_skills() -> None:
    """Write the seed SKILL.md files on first run. Idempotent — if a
    skill folder already exists we leave it alone, so Sir's edits stick.
    """
    ensure_home()
    for name, body in _SEED_SKILLS:
        skill_dir = SKILLS_DIR / name
        md = skill_dir / "SKILL.md"
        if md.exists():
            continue
        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            md.write_text(body, encoding="utf-8")
            logger.info("Seeded skill: %s", name)
        except Exception as exc:
            logger.warning("Could not seed skill %r: %s", name, exc)


def list_skills() -> list[Skill]:
    """Walk ``~/.jarvis/skills`` and return every parseable skill."""
    ensure_home()
    ensure_seed_skills()
    out: list[Skill] = []
    for entry in sorted(SKILLS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        skill = _load_skill_dir(entry)
        if skill is not None:
            out.append(skill)
    return out


def find_skill(name: str) -> Optional[Skill]:
    """Exact-match (case-insensitive) lookup by skill name."""
    key = name.lower().strip()
    for s in list_skills():
        if s.name.lower() == key:
            return s
    return None


# ─── Corrections store ───────────────────────────────────────────────────────

@dataclass
class Correction:
    ts: float
    text: str           # the raw spoken correction
    context: str = ""   # optional last-action / situation context

    def to_jsonl(self) -> str:
        return json.dumps({
            "ts": self.ts,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts)),
            "text": self.text,
            "context": self.context,
        }, ensure_ascii=False)


def add_correction(text: str, *, context: str = "") -> bool:
    """Append one correction to corrections.jsonl.

    The string is whatever Sir said after "JARVIS, don't do this, do
    this" — we don't try to parse it. The planner's job (downstream) is
    to interpret the natural-language correction in context.
    """
    if not text or not text.strip():
        return False
    ensure_home()
    try:
        c = Correction(ts=time.time(), text=text.strip(), context=context)
        with open(CORRECTIONS_FILE, "a", encoding="utf-8") as f:
            f.write(c.to_jsonl() + "\n")
        logger.info("Correction stored: %s", text[:80])
        return True
    except Exception as exc:
        logger.warning("add_correction failed: %s", exc)
        return False


def recent_corrections(n: int = 20) -> list[dict]:
    """Return the last ``n`` corrections as plain dicts (for the planner)."""
    if not CORRECTIONS_FILE.exists():
        return []
    try:
        with open(CORRECTIONS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception as exc:
        logger.debug("recent_corrections failed: %s", exc)
        return []


# ─── Planner context bundle ──────────────────────────────────────────────────

def build_planner_context(*, max_skill_chars: int = 4000) -> dict:
    """Assemble everything the planning LLM needs to make a decision.

    Returns a dict shaped like::

        {
            "soul":         "<contents of soul.md>",
            "skills":       [{"name": ..., "description": ..., "tier": ...}, ...],
            "corrections":  [{"iso": ..., "text": ..., "context": ...}, ...],
        }

    The backend's planner endpoint will eventually consume this on every
    chat turn so the LLM stays grounded in Sir's preferences.
    """
    skills = list_skills()
    # Trim skill descriptions if collectively too long
    total = 0
    skill_dicts = []
    for s in skills:
        d = {"name": s.name, "description": s.description, "tier": s.tier}
        cost = len(s.description) + len(s.name) + 32
        if total + cost > max_skill_chars:
            d["description"] = d["description"][:200] + " …"
        total += cost
        skill_dicts.append(d)
    return {
        "soul": read_soul(),
        "skills": skill_dicts,
        "corrections": recent_corrections(20),
    }


# ─── Standalone debug ────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ensure_home()
    ensure_seed_skills()
    print(f"Persona ({SOUL_FILE}):\n")
    print(read_soul()[:400] + " …\n")
    print(f"Skills found in {SKILLS_DIR}:")
    for s in list_skills():
        print(f"  • {s.name:20} [{s.tier:11}] {s.description.splitlines()[0][:80]}")
    print()
    if CORRECTIONS_FILE.exists():
        print(f"Recent corrections ({CORRECTIONS_FILE}):")
        for c in recent_corrections(5):
            print(f"  [{c['iso']}] {c['text'][:100]}")
    else:
        print("No corrections recorded yet.")

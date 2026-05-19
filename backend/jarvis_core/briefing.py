"""
Briefing engine — the moment that makes JARVIS feel alive.

When Sir activates JARVIS for the first time on a given day (or after a
long silence), instead of asking "what?" JARVIS opens with a brief:

  "Good morning, Sir. Three items today: a 2pm call with the Singh client,
   two overdue tasks from yesterday, and an unanswered email from Priya."

The brief is built locally from:
  - Time of day (morning / afternoon / evening / night)
  - Open tasks in the database
  - Pending notifications (calendar reminders, etc.)
  - Recent memory (e.g. last conversation's summary)

Briefings are NOT time-scheduled — they trigger on activation, per Sir's spec.
We track last_briefing_at in the user_profile so we only brief once per day
unless the user explicitly says "brief me".
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
from sqlalchemy import and_, select

from core.config import get_settings
from core.database import db_context
from jarvis_core import notifications as notif
from models.task import Task
from models.user import UserProfile

logger = logging.getLogger(__name__)
settings = get_settings()

# Don't brief twice in this many hours
BRIEFING_COOLDOWN_HOURS = 8

LAST_BRIEFING_KEY = "last_briefing_at"


def _greeting_for_hour(hour: int) -> str:
    if 5 <= hour < 12:
        return "Good morning, Sir."
    if 12 <= hour < 17:
        return "Good afternoon, Sir."
    if 17 <= hour < 22:
        return "Good evening, Sir."
    return "Burning the midnight oil, Sir?"


async def _get_last_briefing(user_id: str) -> Optional[datetime]:
    async with db_context() as db:
        result = await db.execute(
            select(UserProfile).where(
                and_(
                    UserProfile.user_id == user_id,
                    UserProfile.key == LAST_BRIEFING_KEY,
                )
            )
        )
        row = result.scalar_one_or_none()
        if not row:
            return None
        try:
            return datetime.fromisoformat(row.value)
        except ValueError:
            return None


async def _set_last_briefing(user_id: str, when: datetime) -> None:
    async with db_context() as db:
        result = await db.execute(
            select(UserProfile).where(
                and_(
                    UserProfile.user_id == user_id,
                    UserProfile.key == LAST_BRIEFING_KEY,
                )
            )
        )
        row = result.scalar_one_or_none()
        iso = when.isoformat()
        if row:
            row.value = iso
        else:
            db.add(UserProfile(
                user_id=user_id,
                key=LAST_BRIEFING_KEY,
                value=iso,
                source="system",
            ))


async def _open_tasks(user_id: str, max_items: int = 5) -> list[Task]:
    async with db_context() as db:
        result = await db.execute(
            select(Task).where(
                and_(
                    Task.user_id == user_id,
                    Task.status.in_(["pending", "in_progress"]),
                )
            )
        )
        rows = list(result.scalars().all())
    # Priority sort: high > medium > low; then oldest first
    rank = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda t: (rank.get(t.priority, 99), t.created_at))
    return rows[:max_items]


async def should_brief(user_id: str, force: bool = False) -> bool:
    """
    Returns True if it's time for a briefing.

    Conditions:
      - force=True (user said "brief me")
      - No briefing has ever happened
      - Last briefing was more than BRIEFING_COOLDOWN_HOURS ago
      - Or the calendar day has rolled over since the last briefing
    """
    if force:
        return True

    last = await _get_last_briefing(user_id)
    if last is None:
        return True

    tz = pytz.timezone(settings.jarvis_timezone)
    now = datetime.now(tz)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    last_local = last.astimezone(tz)

    # Cooldown
    if now - last_local >= timedelta(hours=BRIEFING_COOLDOWN_HOURS):
        return True

    # Calendar day rollover
    if now.date() > last_local.date():
        return True

    return False


async def build_briefing(user_id: str) -> str:
    """
    Build the briefing text JARVIS will speak. Always one cohesive paragraph
    in JARVIS voice, never a bulleted list (this gets spoken aloud).

    If there is genuinely nothing to brief, returns a short greeting only.
    """
    tz = pytz.timezone(settings.jarvis_timezone)
    now = datetime.now(tz)

    greeting = _greeting_for_hour(now.hour)
    parts: list[str] = []

    # 1) Pending notifications (these were queued by routines)
    pending = await notif.drain_pending(user_id, max_items=3)
    for n in pending:
        parts.append(n["content"])

    # 2) Open tasks
    tasks = await _open_tasks(user_id)
    if tasks:
        if len(tasks) == 1:
            parts.append(
                f"One open task: {tasks[0].title}."
            )
        else:
            top = tasks[0].title
            parts.append(
                f"{len(tasks)} open tasks. Top of the list: {top}."
            )

    if not parts:
        # Nothing to report — keep it short and authentic.
        return f"{greeting} The deck is clear."

    body = " ".join(parts)
    return f"{greeting} {body}"


async def maybe_brief(user_id: str, force: bool = False) -> Optional[str]:
    """
    Convenience: returns a briefing string if one is due, else None.
    Records the briefing time on success.
    """
    if not await should_brief(user_id, force=force):
        return None

    text = await build_briefing(user_id)

    tz = pytz.timezone(settings.jarvis_timezone)
    await _set_last_briefing(user_id, datetime.now(tz))
    logger.info("Briefing delivered to %s: %s", user_id, text[:120])
    return text

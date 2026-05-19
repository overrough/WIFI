"""
Background routines — APScheduler workers that make JARVIS proactive.

Each routine runs on a schedule, scans the world for things Sir should
know about, and writes them to the notification queue. The voice agent
surfaces them on the next activation.

Currently registered:
  - task_nudger: every 30 min, flag overdue tasks
  - calendar_watcher: every 5 min, alert 15 min before meetings
                      (no-op if Google Calendar not connected)

Add new routines by writing an async function and registering it with
@scheduler.scheduled_job(...).

The scheduler is started by main.py during FastAPI lifespan; it shuts
down cleanly on app exit.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import and_, select

from core.database import db_context
from jarvis_core import notifications as notif
from models.task import Task
from models.user import User

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None


# ── Individual routines ──────────────────────────────────────────────────────


async def task_nudger() -> None:
    """
    Every run, find tasks whose due_date has passed and that are still
    pending or in_progress. Queue a low-priority nudge for each.
    Dedup by task ID so we don't nag every cycle.
    """
    now = datetime.now(timezone.utc)
    try:
        async with db_context() as db:
            result = await db.execute(
                select(Task).where(
                    and_(
                        Task.status.in_(["pending", "in_progress"]),
                        Task.due_date.is_not(None),
                        Task.due_date < now,
                    )
                )
            )
            rows = list(result.scalars().all())

        for task in rows:
            content = (
                f"Sir, the task '{task.title}' has slipped past its deadline."
            )
            await notif.enqueue(
                user_id=task.user_id,
                content=content,
                source="task",
                priority="medium",
                payload={"task_id": task.id, "title": task.title},
                dedup_key=f"task_overdue_{task.id}",
                expires_at=now + timedelta(days=2),
            )
        if rows:
            logger.info("task_nudger: queued %d overdue task notifications", len(rows))
    except Exception as exc:
        logger.warning("task_nudger failed: %s", exc)


async def calendar_watcher() -> None:
    """
    Look ahead ~20 minutes in Sir's Google Calendar and queue a
    "meeting in N minutes" notification once per event.

    Silent no-op if Google Calendar isn't connected yet. We don't crash
    the scheduler just because OAuth hasn't been set up.
    """
    import os

    # Cheap pre-check — if no token file, don't even try
    token_file = os.getenv("GOOGLE_TOKEN_FILE", "./google_token.json")
    if not os.path.exists(token_file):
        return

    try:
        from tools.calendar_tools import _get_calendar_service, _parse_event
    except Exception:
        return

    try:
        # Build the service ONCE — there's only one Google account in this
        # local instance, so we apply the same calendar to every user row.
        try:
            service = _get_calendar_service()
        except Exception as exc:
            logger.debug("calendar service unavailable: %s", exc)
            return

        now = datetime.now(timezone.utc)
        window_end = now + timedelta(minutes=20)

        try:
            result = service.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=window_end.isoformat(),
                maxResults=10,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
        except Exception as exc:
            logger.debug("calendar fetch failed: %s", exc)
            return

        items = result.get("items", [])
        if not items:
            return

        # Notify all known users (single-user instance in practice)
        async with db_context() as db:
            user_result = await db.execute(select(User))
            users = list(user_result.scalars().all())

        for ev in items:
            parsed = _parse_event(ev)
            start_str = parsed.get("start", "")
            try:
                start_dt = datetime.fromisoformat(start_str)
            except ValueError:
                continue
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)

            minutes_until = int((start_dt - now).total_seconds() / 60)
            if minutes_until < 0 or minutes_until > 20:
                continue

            title = parsed.get("title", "an event")
            content = f"Sir, '{title}' begins in {minutes_until} minutes."
            for user in users:
                await notif.enqueue(
                    user_id=user.id,
                    content=content,
                    source="calendar",
                    priority="high",
                    payload={"event_id": parsed.get("id"), "title": title},
                    dedup_key=f"calendar_pre_{parsed.get('id')}",
                    expires_at=start_dt + timedelta(minutes=5),
                )
    except Exception as exc:
        logger.warning("calendar_watcher failed: %s", exc)


# ── Scheduler lifecycle ──────────────────────────────────────────────────────


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def start_scheduler() -> None:
    """Wire up routines and start the scheduler."""
    sched = get_scheduler()
    if sched.running:
        return

    sched.add_job(
        task_nudger,
        trigger=IntervalTrigger(minutes=30),
        id="task_nudger",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    sched.add_job(
        calendar_watcher,
        trigger=IntervalTrigger(minutes=5),
        id="calendar_watcher",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    sched.start()
    logger.info("JARVIS routines scheduler started.")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("JARVIS routines scheduler stopped.")
    _scheduler = None

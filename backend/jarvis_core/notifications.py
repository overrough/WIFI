"""
Notification queue — JARVIS's outbox for proactive messages.

Background routines call enqueue() when they spot something Sir should know.
The voice agent calls drain_pending() at the start of every activation
to surface what's been queued since the last time he was awake.

Priority order: urgent > high > medium > low.
Within the same priority, oldest first.

Dedup keys prevent the same alert from being queued repeatedly. If a routine
runs every 15 minutes and notices the same overdue task, it should pass a
stable dedup_key (e.g. "task_overdue_<task_id>") so we don't pile up duplicates.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import and_, or_, select, update

from core.database import db_context
from models.notification import Notification

logger = logging.getLogger(__name__)

PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}


async def enqueue(
    user_id: str,
    content: str,
    source: str,
    priority: str = "medium",
    payload: Optional[dict] = None,
    dedup_key: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> Optional[str]:
    """
    Add a notification to the queue.

    If dedup_key is given and a pending notification with the same key already
    exists, this is a no-op and we return None. Otherwise, the new notification
    ID is returned.
    """
    async with db_context() as db:
        if dedup_key:
            existing = await db.execute(
                select(Notification).where(
                    and_(
                        Notification.user_id == user_id,
                        Notification.dedup_key == dedup_key,
                        Notification.status == "pending",
                    )
                )
            )
            if existing.scalar_one_or_none():
                return None

        n = Notification(
            user_id=user_id,
            content=content,
            source=source,
            priority=priority,
            payload_json=json.dumps(payload) if payload else None,
            dedup_key=dedup_key,
            expires_at=expires_at,
        )
        db.add(n)
        await db.flush()
        logger.info("Queued [%s/%s] %s: %s", source, priority, n.id, content[:80])
        return n.id


async def drain_pending(
    user_id: str,
    max_items: int = 5,
) -> list[dict]:
    """
    Fetch pending notifications, ordered by priority then age, and mark them
    surfaced. Returns a list of dicts ready for the voice agent to speak.

    Expired pending items are auto-dismissed on the way through.
    """
    now = datetime.now(timezone.utc)

    async with db_context() as db:
        # Auto-expire stale items
        await db.execute(
            update(Notification)
            .where(
                and_(
                    Notification.user_id == user_id,
                    Notification.status == "pending",
                    Notification.expires_at.is_not(None),
                    Notification.expires_at < now,
                )
            )
            .values(status="dismissed")
        )

        result = await db.execute(
            select(Notification).where(
                and_(
                    Notification.user_id == user_id,
                    Notification.status == "pending",
                )
            )
        )
        rows = list(result.scalars().all())

    rows.sort(key=lambda n: (PRIORITY_RANK.get(n.priority, 99), n.created_at))
    rows = rows[:max_items]

    out = []
    async with db_context() as db:
        for n in rows:
            out.append({
                "id": n.id,
                "source": n.source,
                "priority": n.priority,
                "content": n.content,
                "payload": json.loads(n.payload_json) if n.payload_json else None,
            })
            await db.execute(
                update(Notification)
                .where(Notification.id == n.id)
                .values(status="surfaced", surfaced_at=now)
            )

    return out


async def count_pending(user_id: str) -> int:
    async with db_context() as db:
        result = await db.execute(
            select(Notification).where(
                and_(
                    Notification.user_id == user_id,
                    Notification.status == "pending",
                )
            )
        )
        return len(list(result.scalars().all()))


async def dismiss(notification_id: str) -> None:
    async with db_context() as db:
        await db.execute(
            update(Notification)
            .where(Notification.id == notification_id)
            .values(status="dismissed")
        )

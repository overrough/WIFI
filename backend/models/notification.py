"""
Notification model — JARVIS's outbox for proactive messages.

Background routines (calendar watcher, task nudger, etc.) write to this
table when they spot something Sir should know about. The voice agent
drains the queue at the start of every activation and speaks the
highest-priority items before listening for the next command.

This is the mechanism that turns JARVIS from reactive to proactive.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # Where did this come from?
    # 'calendar' | 'task' | 'email' | 'system' | 'agent' | 'manual'
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    # 'low' | 'medium' | 'high' | 'urgent'
    priority: Mapped[str] = mapped_column(String(16), default="medium")

    # The actual line JARVIS will speak / display, in JARVIS voice.
    # e.g. "Sir, the Singh client meeting begins in ten minutes."
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Optional structured payload for the agent to act on if surfaced.
    # JSON-serializable.
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # State machine: pending → surfaced → dismissed (or expired)
    status: Mapped[str] = mapped_column(String(16), default="pending")

    # When was this surfaced to the user?
    surfaced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Optional expiry — drop pending items after this time
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Optional dedup key — prevents the same alert from being queued twice
    # (e.g. "calendar_event_<event_id>", "task_overdue_<task_id>")
    dedup_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

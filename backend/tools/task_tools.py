"""
Task tools — Jarvis's internal to-do system.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from langchain_core.tools import tool
from sqlalchemy import select

from core.database import db_context
from models.task import Task

logger = logging.getLogger(__name__)


def make_task_tools(user_id: str):
    """Return task tools bound to a specific user."""

    @tool
    async def create_task(
        title: str,
        description: Optional[str] = None,
        priority: str = "medium",
        due_date: Optional[str] = None,
    ) -> str:
        """
        Create a new task or reminder in the internal task system.
        priority: 'low' | 'medium' | 'high'
        due_date: ISO format string, e.g. '2026-04-15T18:00:00' or '2026-04-15'
        """
        parsed_due: Optional[datetime] = None
        if due_date:
            try:
                parsed_due = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
                if parsed_due.tzinfo is None:
                    parsed_due = parsed_due.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        async with db_context() as db:
            task = Task(
                user_id=uuid.UUID(user_id),
                title=title,
                description=description,
                priority=priority if priority in {"low", "medium", "high"} else "medium",
                due_date=parsed_due,
                source="agent_created",
            )
            db.add(task)

        due_str = f" (due: {parsed_due.strftime('%a %d %b')})" if parsed_due else ""
        return f"Task created: '{title}'{due_str}"

    @tool
    async def list_tasks(status: str = "pending") -> str:
        """
        List tasks filtered by status.
        status: 'pending' | 'in_progress' | 'done' | 'cancelled' | 'all'
        """
        async with db_context() as db:
            query = select(Task).where(Task.user_id == uuid.UUID(user_id))
            if status != "all":
                query = query.where(Task.status == status)
            query = query.order_by(Task.created_at.desc()).limit(20)
            result = await db.execute(query)
            tasks = result.scalars().all()

        if not tasks:
            return f"No {status} tasks."

        lines = []
        for t in tasks:
            due = f" — due {t.due_date.strftime('%a %d %b')}" if t.due_date else ""
            lines.append(f"[{t.priority.upper()}] {t.title}{due} ({t.status})")

        return f"{len(tasks)} task(s):\n" + "\n".join(lines)

    @tool
    async def update_task(task_title: str, new_status: str) -> str:
        """
        Mark a task as done, in_progress, or cancelled by its title.
        new_status: 'pending' | 'in_progress' | 'done' | 'cancelled'
        """
        valid = {"pending", "in_progress", "done", "cancelled"}
        if new_status not in valid:
            return f"Invalid status '{new_status}'. Use: {', '.join(valid)}"

        async with db_context() as db:
            result = await db.execute(
                select(Task).where(
                    Task.user_id == uuid.UUID(user_id),
                    Task.title.ilike(f"%{task_title}%"),
                )
            )
            task = result.scalars().first()
            if not task:
                return f"No task matching '{task_title}' found."
            task.status = new_status
            task.updated_at = datetime.now(timezone.utc)

        return f"Task '{task.title}' marked as {new_status}."

    return [create_task, list_tasks, update_task]

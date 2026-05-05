"""
Jarvis MCP Server — FastMCP tool server.

Exposes all Jarvis tools as MCP tools so the voice agent (LiveKit)
and any other client can call them via the standard MCP protocol.

The voice agent and the API backend share the same tool definitions;
they just call them through different transports.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import pytz
from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://jarvis:localdev@localhost:5432/jarvis",
)
USER_ID = os.getenv("JARVIS_DEFAULT_USER_ID", "")  # set after first boot

mcp = FastMCP("Jarvis Tools", port=8001)


# ── Helpers ───────────────────────────────────────────────────────────────

async def _get_db_session():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.orm import DeclarativeBase

    engine = create_async_engine(DATABASE_URL, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    return SessionLocal()


async def _get_default_user_id() -> str:
    """Resolve the default user (single-user personal instance)."""
    async with await _get_db_session() as db:
        from sqlalchemy import text
        result = await db.execute(
            text("SELECT id FROM users WHERE email = 'user@jarvis.local' LIMIT 1")
        )
        row = result.fetchone()
        return str(row[0]) if row else ""


# ── Date / Time ───────────────────────────────────────────────────────────

@mcp.tool()
def get_datetime(timezone_name: str = "Asia/Kolkata") -> str:
    """Get the current date and time in the specified timezone."""
    try:
        tz = pytz.timezone(timezone_name)
    except Exception:
        tz = pytz.UTC
    now = datetime.now(tz)
    return now.strftime("Date: %A, %d %B %Y\nTime: %H:%M %Z\nISO: %Y-%m-%dT%H:%M:%S%z")


# ── Tasks ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def create_task(
    title: str,
    description: str = "",
    priority: str = "medium",
    due_date: str = "",
) -> str:
    """Create a new task. priority: low | medium | high. due_date: ISO string."""
    user_id = await _get_default_user_id()
    if not user_id:
        return "Error: no default user found"

    parsed_due = None
    if due_date:
        try:
            parsed_due = datetime.fromisoformat(due_date.replace("Z", "+00:00"))
            if parsed_due.tzinfo is None:
                parsed_due = parsed_due.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    async with await _get_db_session() as db:
        from sqlalchemy import text
        result = await db.execute(
            text("""
                INSERT INTO tasks (user_id, title, description, priority, due_date, source)
                VALUES (:user_id, :title, :description, :priority, :due_date, 'agent_created')
                RETURNING id
            """),
            {
                "user_id": user_id,
                "title": title,
                "description": description or None,
                "priority": priority,
                "due_date": parsed_due,
            },
        )
        await db.commit()
        task_id = result.fetchone()[0]

    due_str = f" (due: {parsed_due.strftime('%a %d %b')})" if parsed_due else ""
    return f"Task created: '{title}'{due_str} [id: {str(task_id)[:8]}]"


@mcp.tool()
async def list_tasks(status: str = "pending") -> str:
    """List tasks. status: pending | in_progress | done | all"""
    user_id = await _get_default_user_id()
    if not user_id:
        return "Error: no default user"

    async with await _get_db_session() as db:
        from sqlalchemy import text
        if status == "all":
            result = await db.execute(
                text("SELECT title, status, priority, due_date FROM tasks WHERE user_id = :uid ORDER BY created_at DESC LIMIT 20"),
                {"uid": user_id},
            )
        else:
            result = await db.execute(
                text("SELECT title, status, priority, due_date FROM tasks WHERE user_id = :uid AND status = :status ORDER BY created_at DESC LIMIT 20"),
                {"uid": user_id, "status": status},
            )
        rows = result.fetchall()

    if not rows:
        return f"No {status} tasks."

    lines = [f"[{r.priority.upper()}] {r.title} ({r.status})" for r in rows]
    return f"{len(lines)} task(s):\n" + "\n".join(lines)


# ── Memory ────────────────────────────────────────────────────────────────

@mcp.tool()
async def save_memory(content: str, memory_type: str = "semantic") -> str:
    """
    Save a fact, event, or pattern to long-term memory.
    memory_type: semantic | episodic | procedural
    """
    user_id = await _get_default_user_id()
    if not user_id:
        return "Error: no default user"

    mem_id = str(uuid.uuid4())
    async with await _get_db_session() as db:
        from sqlalchemy import text
        await db.execute(
            text("""
                INSERT INTO memories (id, user_id, type, content, chroma_id, importance_score)
                VALUES (:id, :user_id, :type, :content, :chroma_id, 0.7)
            """),
            {
                "id": str(uuid.UUID(mem_id)),
                "user_id": user_id,
                "type": memory_type,
                "content": content,
                "chroma_id": mem_id,
            },
        )
        await db.commit()

    return f"Remembered ({memory_type}): '{content[:80]}...'" if len(content) > 80 else f"Remembered: '{content}'"


@mcp.tool()
async def search_memory(query: str) -> str:
    """Search long-term memory for relevant context."""
    user_id = await _get_default_user_id()
    if not user_id:
        return "Error: no default user"

    # Call the backend API for proper vector search
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                "http://backend:8000/memory/search",
                json={"query": query, "top_k": 5},
                headers={"X-API-Key": os.getenv("JARVIS_API_KEY", "local-dev-key")},
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                if not results:
                    return "No relevant memories found."
                return "\n".join(f"- {r['content']}" for r in results)
    except Exception as exc:
        logger.warning("Memory search via API failed: %s", exc)

    # Fallback: simple text search in DB
    async with await _get_db_session() as db:
        from sqlalchemy import text
        result = await db.execute(
            text("""
                SELECT content, type FROM memories
                WHERE user_id = :uid AND content ILIKE :q
                ORDER BY importance_score DESC LIMIT 5
            """),
            {"uid": user_id, "q": f"%{query}%"},
        )
        rows = result.fetchall()

    if not rows:
        return "No relevant memories found."
    return "\n".join(f"[{r.type}] {r.content}" for r in rows)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8001)

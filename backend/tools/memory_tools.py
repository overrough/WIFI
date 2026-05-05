"""
Memory tools — the agent's interface to the memory system.
These are exposed as LangChain tools for use in the agent loop.
"""

import logging
from typing import Optional

from langchain_core.tools import tool

from memory.manager import get_memory_manager

logger = logging.getLogger(__name__)

# ── LangChain tool definitions (used by the agent) ────────────────────────────


def make_memory_tools(user_id: str):
    """Return memory tools bound to a specific user."""

    @tool
    async def save_memory(content: str, memory_type: str = "semantic") -> str:
        """
        Save an important fact, event, or behavioural pattern to long-term memory.
        memory_type: 'semantic' (facts about user), 'episodic' (events),
                     or 'procedural' (behaviour patterns).
        Use this when the user shares important information you should remember later.
        """
        valid_types = {"semantic", "episodic", "procedural"}
        if memory_type not in valid_types:
            memory_type = "semantic"

        manager = get_memory_manager()
        mem_id = await manager.store_memory(
            user_id=user_id,
            content=content,
            memory_type=memory_type,
        )
        return f"Remembered: '{content}' (id: {mem_id[:8]})"

    @tool
    async def search_memory(query: str) -> str:
        """
        Search long-term memory for information relevant to the query.
        Use this when you need to recall something the user told you previously.
        """
        manager = get_memory_manager()
        results = manager.retriever.search(user_id=user_id, query=query, top_k=5)

        if not results:
            return "No relevant memories found."

        lines = []
        for r in results:
            score = r.get("score", 0)
            mem_type = r.get("metadata", {}).get("type", "?")
            lines.append(f"[{mem_type}, relevance={score:.2f}] {r['content']}")

        return "\n".join(lines)

    @tool
    async def update_profile(key: str, value: str) -> str:
        """
        Update a profile fact about the user (name, timezone, preferences, working hours, etc.)
        Examples: key='timezone', value='Europe/London'
                  key='name', value='Rahul'
                  key='working_hours', value='9am-7pm'
        """
        manager = get_memory_manager()
        await manager.upsert_profile(user_id=user_id, key=key, value=value)
        return f"Profile updated: {key} = {value}"

    return [save_memory, search_memory, update_profile]

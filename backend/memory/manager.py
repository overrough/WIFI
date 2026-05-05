"""
MemoryManager — the single interface the agent uses for all memory operations.

Four memory types (per spec):
  A. Working memory  — in-context (handled by the agent itself)
  B. Episodic memory — past events, decisions, tasks completed
  C. Semantic memory — facts about the user (profile, preferences)
  D. Procedural memory — learned behaviour patterns

Retrieval is called BEFORE every LLM turn.
Extraction is called AFTER every conversation ends.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import db_context
from memory.extractor import extract_memories
from memory.retriever import MemoryRetriever, get_retriever
from models.memory import Memory
from models.user import User, UserProfile
from providers.base import ModelProvider

logger = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self, retriever: Optional[MemoryRetriever] = None) -> None:
        self.retriever = retriever or get_retriever()

    # ── Retrieval ─────────────────────────────────────────────────────────────

    async def retrieve_for_context(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
    ) -> str:
        """
        Called before every LLM turn.
        Returns a formatted string block injected into the system prompt.
        """
        async with db_context() as db:
            profile_text = await self._get_profile_text(db, user_id)
            episodic = self.retriever.search(user_id, query, top_k=top_k, memory_type="episodic")
            semantic = self.retriever.search(user_id, query, top_k=3, memory_type="semantic")
            procedural = self.retriever.search(user_id, query, top_k=2, memory_type="procedural")

            # Update access counts
            all_ids = (
                [h["metadata"].get("db_id") for h in episodic + semantic + procedural]
            )
            await self._bump_access_counts(db, [i for i in all_ids if i])

        sections = []
        if profile_text:
            sections.append(f"USER PROFILE:\n{profile_text}")

        if semantic:
            facts = "\n".join(f"- {h['content']}" for h in semantic)
            sections.append(f"KNOWN FACTS:\n{facts}")

        if episodic:
            events = "\n".join(f"- {h['content']}" for h in episodic)
            sections.append(f"RELEVANT PAST EVENTS:\n{events}")

        if procedural:
            patterns = "\n".join(f"- {h['content']}" for h in procedural)
            sections.append(f"BEHAVIOURAL PATTERNS:\n{patterns}")

        return "\n\n".join(sections) if sections else "(No prior context)"

    async def _get_profile_text(self, db: AsyncSession, user_id: str) -> str:
        result = await db.execute(
            select(UserProfile).where(UserProfile.user_id == uuid.UUID(user_id))
        )
        rows = result.scalars().all()
        if not rows:
            return ""
        return "\n".join(f"{r.key}: {r.value}" for r in rows)

    async def _bump_access_counts(self, db: AsyncSession, db_ids: list[str]) -> None:
        if not db_ids:
            return
        for db_id in db_ids:
            try:
                await db.execute(
                    update(Memory)
                    .where(Memory.id == uuid.UUID(db_id))
                    .values(
                        access_count=Memory.access_count + 1,
                        last_accessed_at=datetime.now(timezone.utc),
                    )
                )
            except Exception:
                pass   # Non-critical

    # ── Storage ───────────────────────────────────────────────────────────────

    async def store_memory(
        self,
        user_id: str,
        content: str,
        memory_type: str,
        importance: float = 0.5,
        tags: Optional[list[str]] = None,
    ) -> str:
        """Store a single memory. Returns the memory ID."""
        tags = tags or []
        memory_id = str(uuid.uuid4())

        async with db_context() as db:
            mem = Memory(
                id=uuid.UUID(memory_id),
                user_id=uuid.UUID(user_id),
                type=memory_type,
                content=content,
                chroma_id=memory_id,
                metadata_={"tags": tags},
                importance_score=importance,
            )
            db.add(mem)

        self.retriever.upsert(
            user_id=user_id,
            memory_id=memory_id,
            content=content,
            metadata={
                "type": memory_type,
                "db_id": memory_id,
                "tags": ",".join(tags),
            },
        )

        logger.debug("Stored %s memory for user %s: %.60s…", memory_type, user_id, content)
        return memory_id

    async def extract_and_store(
        self,
        conversation: list[dict],
        user_id: str,
        provider: ModelProvider,
    ) -> int:
        """
        Called after a conversation ends.
        Extracts memorable facts and stores them.
        Returns the number of memories stored.
        """
        memories = await extract_memories(conversation, provider)
        count = 0
        for mem in memories:
            await self.store_memory(
                user_id=user_id,
                content=mem["content"],
                memory_type=mem["type"],
                importance=mem["importance"],
                tags=mem.get("tags", []),
            )
            count += 1

        if count:
            logger.info("Stored %d memories for user %s", count, user_id)
        return count

    # ── Profile Management ────────────────────────────────────────────────────

    async def upsert_profile(
        self,
        user_id: str,
        key: str,
        value: str,
        source: str = "explicit",
    ) -> None:
        """Set or update a profile fact (name, timezone, preferences, etc.)."""
        async with db_context() as db:
            result = await db.execute(
                select(UserProfile).where(
                    UserProfile.user_id == uuid.UUID(user_id),
                    UserProfile.key == key,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.value = value
                existing.source = source
            else:
                db.add(UserProfile(
                    user_id=uuid.UUID(user_id),
                    key=key,
                    value=value,
                    source=source,
                ))

    async def get_user_id_by_api_key(self, api_key: str) -> Optional[str]:
        """Resolve API key → user_id for auth."""
        async with db_context() as db:
            result = await db.execute(
                select(User).where(User.hashed_api_key == api_key)
            )
            user = result.scalar_one_or_none()
            return str(user.id) if user else None

    async def get_user(self, user_id: str) -> Optional[User]:
        async with db_context() as db:
            result = await db.execute(
                select(User).where(User.id == uuid.UUID(user_id))
            )
            return result.scalar_one_or_none()


# Module-level singleton
_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    global _manager
    if _manager is None:
        _manager = MemoryManager()
    return _manager

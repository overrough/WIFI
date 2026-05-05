"""
Memory router — /memory/*
Exposes memory management to the frontend dashboard.
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from memory.manager import get_memory_manager
from models.memory import Memory
from models.user import User, UserProfile
from routers.auth import get_current_user

router = APIRouter(prefix="/memory", tags=["memory"])


class SearchRequest(BaseModel):
    query: str
    top_k: int = 5
    memory_type: Optional[str] = None


class StoreRequest(BaseModel):
    content: str
    memory_type: str = "semantic"
    importance: float = 0.5
    tags: list[str] = []


class ProfileUpdateRequest(BaseModel):
    key: str
    value: str


@router.post("/search")
async def search_memory(
    body: SearchRequest,
    user: User = Depends(get_current_user),
):
    manager = get_memory_manager()
    results = manager.retriever.search(
        user_id=str(user.id),
        query=body.query,
        top_k=body.top_k,
        memory_type=body.memory_type,
    )
    return {"results": results}


@router.post("/store")
async def store_memory(
    body: StoreRequest,
    user: User = Depends(get_current_user),
):
    manager = get_memory_manager()
    mem_id = await manager.store_memory(
        user_id=str(user.id),
        content=body.content,
        memory_type=body.memory_type,
        importance=body.importance,
        tags=body.tags,
    )
    return {"id": mem_id, "status": "stored"}


@router.get("/list")
async def list_memories(
    memory_type: Optional[str] = None,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(Memory).where(Memory.user_id == user.id)
    if memory_type:
        query = query.where(Memory.type == memory_type)
    query = query.order_by(Memory.created_at.desc()).limit(limit)
    result = await db.execute(query)
    memories = result.scalars().all()
    return [
        {
            "id": str(m.id),
            "type": m.type,
            "content": m.content,
            "importance": m.importance_score,
            "access_count": m.access_count,
            "created_at": m.created_at.isoformat(),
        }
        for m in memories
    ]


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Memory).where(
            Memory.id == uuid.UUID(memory_id),
            Memory.user_id == user.id,
        )
    )
    mem = result.scalar_one_or_none()
    if not mem:
        raise HTTPException(status_code=404, detail="Memory not found")

    manager = get_memory_manager()
    manager.retriever.delete(str(user.id), memory_id)
    await db.delete(mem)
    return {"status": "deleted"}


@router.get("/profile")
async def get_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserProfile).where(UserProfile.user_id == user.id)
    )
    rows = result.scalars().all()
    return {r.key: {"value": r.value, "source": r.source} for r in rows}


@router.put("/profile")
async def update_profile(
    body: ProfileUpdateRequest,
    user: User = Depends(get_current_user),
):
    manager = get_memory_manager()
    await manager.upsert_profile(
        user_id=str(user.id),
        key=body.key,
        value=body.value,
    )
    return {"status": "updated", "key": body.key, "value": body.value}

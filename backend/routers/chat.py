"""
Chat router — /chat (non-streaming) and /chat/stream (SSE streaming).

Conversation lifecycle:
  POST /chat/start          → create conversation, get id
  POST /chat/{id}/message   → send message, get streaming response
  POST /chat/{id}/end       → close conversation, trigger memory extraction
  GET  /chat/history        → list past conversations
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.jarvis_agent import get_agent
from core.database import get_db
from models.conversation import Conversation, Message
from models.user import User
from routers.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


# ── Request / Response schemas ─────────────────────────────────────────────


class StartConversationRequest(BaseModel):
    mode: Optional[str] = None   # defaults to user's active_mode


class StartConversationResponse(BaseModel):
    conversation_id: str
    mode: str


class SendMessageRequest(BaseModel):
    content: str


class ConversationSummary(BaseModel):
    id: str
    mode: str
    started_at: datetime
    summary: Optional[str]
    message_count: int


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/start", response_model=StartConversationResponse)
async def start_conversation(
    body: StartConversationRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    mode = body.mode or user.active_mode or "work"
    conv = Conversation(
        user_id=user.id,
        mode=mode,
        channel="chat",
    )
    db.add(conv)
    await db.flush()
    return StartConversationResponse(
        conversation_id=str(conv.id),
        mode=mode,
    )


@router.post("/{conversation_id}/message")
async def send_message(
    conversation_id: str,
    body: SendMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Send a message and receive a streaming SSE response.
    Each event is a JSON object: {"type": "token", "content": "..."}
    A final event {"type": "done"} signals completion.
    """
    # Verify conversation belongs to this user
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == uuid.UUID(conversation_id),
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Load history (last 20 messages)
    history_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc())
        .limit(20)
    )
    history_rows = list(reversed(history_result.scalars().all()))
    history = [{"role": m.role, "content": m.content} for m in history_rows]

    # Save the user message
    user_msg = Message(
        conversation_id=conv.id,
        role="user",
        content=body.content,
    )
    db.add(user_msg)
    await db.commit()

    agent = get_agent()

    async def event_stream():
        full_response = []
        try:
            async for chunk in agent.stream_chat(
                query=body.content,
                user_id=str(user.id),
                user_name=user.profile_dict().get("name", "Boss"),
                mode=conv.mode,
                history=history,
            ):
                full_response.append(chunk)
                yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

            # Persist the assistant response
            response_text = "".join(full_response)
            async with db.begin():  # new transaction
                db.add(Message(
                    conversation_id=conv.id,
                    role="assistant",
                    content=response_text,
                ))

            yield f"data: {json.dumps({'type': 'done', 'content': response_text})}\n\n"

        except Exception as exc:
            logger.error("Stream error: %s", exc, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{conversation_id}/end")
async def end_conversation(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Close a conversation and trigger async memory extraction.
    """
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == uuid.UUID(conversation_id),
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Load full conversation for extraction
    msgs_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at)
    )
    messages = [{"role": m.role, "content": m.content} for m in msgs_result.scalars().all()]

    conv.ended_at = datetime.now(timezone.utc)
    await db.commit()

    # Extract memories (fire and forget — don't block the response)
    agent = get_agent()
    try:
        count = await agent.close_conversation(messages, str(user.id))
    except Exception as exc:
        logger.warning("Memory extraction failed: %s", exc)
        count = 0

    return {"status": "closed", "memories_stored": count}


@router.get("/history")
async def list_conversations(
    limit: int = 20,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.started_at.desc())
        .limit(limit)
    )
    conversations = result.scalars().all()
    return [
        {
            "id": str(c.id),
            "mode": c.mode,
            "started_at": c.started_at.isoformat(),
            "summary": c.summary,
        }
        for c in conversations
    ]


@router.get("/{conversation_id}/messages")
async def get_messages(
    conversation_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == uuid.UUID(conversation_id),
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at)
    )
    return [
        {
            "id": str(m.id),
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in msgs_result.scalars().all()
    ]

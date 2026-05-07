"""
Notification router — JARVIS's outbox endpoints.

The voice agent calls these at activation time:
  GET  /jarvis/pending            → briefing or queue, ready to speak
  POST /jarvis/notifications/{id}/dismiss  → mark a notification handled
  GET  /jarvis/notifications/count  → for HUD ambient glow (unread badge)

This is also where the "brief me" flow lives — a manual trigger for the
activation briefing, in case Sir asks for it explicitly.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from jarvis_core import briefing, notifications as notif
from models.user import User
from routers.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/jarvis", tags=["jarvis"])


class PendingResponse(BaseModel):
    briefing: Optional[str] = None
    notifications: list[dict] = []
    pending_count: int = 0


@router.get("/pending", response_model=PendingResponse)
async def pending(
    force_brief: bool = False,
    user: User = Depends(get_current_user),
):
    """
    Called by the voice agent at the start of every activation.

    Returns either:
      - A briefing (if it's been a while since the last one), OR
      - The next queued notifications to surface.

    The voice agent speaks whatever comes back, then proceeds to its
    normal "listen for command" flow.
    """
    brief_text = await briefing.maybe_brief(user.id, force=force_brief)
    if brief_text:
        # Briefing already drained the top notifications inside build_briefing().
        return PendingResponse(briefing=brief_text)

    queued = await notif.drain_pending(user.id, max_items=3)
    count = await notif.count_pending(user.id)
    return PendingResponse(
        briefing=None,
        notifications=queued,
        pending_count=count,
    )


@router.get("/notifications/count")
async def count(user: User = Depends(get_current_user)):
    """For HUD ambient glow — how many pending items are waiting?"""
    return {"count": await notif.count_pending(user.id)}


@router.post("/notifications/{notification_id}/dismiss")
async def dismiss(
    notification_id: str,
    user: User = Depends(get_current_user),
):
    await notif.dismiss(notification_id)
    return {"status": "dismissed"}


class EnqueueRequest(BaseModel):
    content: str
    source: str = "manual"
    priority: str = "medium"
    dedup_key: Optional[str] = None


@router.post("/notifications")
async def enqueue_notification(
    body: EnqueueRequest,
    user: User = Depends(get_current_user),
):
    """Manual / agent-driven notification enqueue."""
    nid = await notif.enqueue(
        user_id=user.id,
        content=body.content,
        source=body.source,
        priority=body.priority,
        dedup_key=body.dedup_key,
    )
    return {"id": nid, "status": "queued" if nid else "deduplicated"}

"""
Auth dependency — simple API-key auth for personal instance.
Pass X-API-Key header or ?api_key= query param.
"""

import uuid
from typing import Optional

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.database import get_db
from models.user import User

settings = get_settings()


async def get_current_user(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    api_key: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    key = x_api_key or api_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Pass X-API-Key header.",
        )

    result = await db.execute(select(User).where(User.hashed_api_key == key))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )
    return user

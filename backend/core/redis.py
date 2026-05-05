"""
Redis client — optional. If Redis is unavailable, cache calls are no-ops.
Jarvis works fine without Redis; it's only used for optional response caching.
"""

import json
import logging
from typing import Any, Optional

from core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_redis = None
_redis_available = None   # None = untested, True/False = tested


async def get_redis():
    global _redis, _redis_available
    if _redis_available is False:
        return None
    if _redis is None:
        try:
            import redis.asyncio as aioredis
            _redis = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=1,
            )
            await _redis.ping()
            _redis_available = True
            logger.info("Redis connected at %s", settings.redis_url)
        except Exception as exc:
            _redis_available = False
            _redis = None
            logger.debug("Redis unavailable (%s) — caching disabled (not required).", exc)
    return _redis


async def cache_set(key: str, value: Any, ttl: int = 3600) -> None:
    r = await get_redis()
    if r is None:
        return
    try:
        await r.setex(key, ttl, json.dumps(value))
    except Exception:
        pass


async def cache_get(key: str) -> Optional[Any]:
    r = await get_redis()
    if r is None:
        return None
    try:
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def cache_delete(key: str) -> None:
    r = await get_redis()
    if r is None:
        return
    try:
        await r.delete(key)
    except Exception:
        pass

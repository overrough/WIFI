"""
Jarvis — FastAPI application entrypoint.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from core.config import get_settings
from routers import chat, memory, tasks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Jarvis starting up — provider: %s", settings.effective_provider)

    # Auto-create all tables (SQLite creates the file; Postgres needs schema already applied)
    try:
        from core.database import Base, engine
        # Import all models so SQLAlchemy knows about them before create_all
        import models.user          # noqa: F401
        import models.conversation  # noqa: F401
        import models.memory        # noqa: F401
        import models.task          # noqa: F401
        import models.tool_log      # noqa: F401
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables ready.")
    except Exception as exc:
        logger.warning("Could not create tables: %s", exc)

    # Seed a default user so the API key works immediately
    try:
        from core.database import AsyncSessionLocal
        from models.user import User
        from sqlalchemy import select
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(User).where(User.hashed_api_key == settings.jarvis_api_key)
            )
            if not result.scalar_one_or_none():
                import uuid
                user = User(
                    id=str(uuid.uuid4()),
                    email="jarvis@local",
                    hashed_api_key=settings.jarvis_api_key,
                )
                session.add(user)
                await session.commit()
                logger.info("Default user seeded (api_key=%s).", settings.jarvis_api_key)
    except Exception as exc:
        logger.warning("Could not seed user: %s", exc)

    # Warm up the sentence-transformer embedder on startup
    try:
        from memory.retriever import get_retriever
        retriever = get_retriever()
        retriever._get_embedder()
        logger.info("Embedding model loaded.")
    except Exception as exc:
        logger.warning("Could not pre-load embedder: %s", exc)

    yield

    logger.info("Jarvis shutting down.")


app = FastAPI(
    title="Jarvis API",
    description="Your digital chief of staff. Remembers, plans, executes.",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS (allow the Next.js frontend) ─────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request timing middleware ──────────────────────────────────────────────
@app.middleware("http")
async def add_timing(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration = int((time.monotonic() - start) * 1000)
    response.headers["X-Response-Time"] = f"{duration}ms"
    return response


# ── Global exception handler ───────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred. Check server logs."},
    )


# ── Routes ─────────────────────────────────────────────────────────────────
app.include_router(chat.router)
app.include_router(memory.router)
app.include_router(tasks.router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "provider": settings.effective_provider,
        "mode": settings.jarvis_default_mode,
    }


@app.get("/")
async def root():
    return {
        "name": "Jarvis",
        "version": "1.0.0",
        "docs": "/docs",
    }

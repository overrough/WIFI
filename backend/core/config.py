"""
Central configuration — loaded once from environment / .env file.
Everything that might change (models, URLs, keys) lives here.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ────────────────────────────────────────────────
    app_name: str = "Jarvis"
    secret_key: str = "change-me-in-production"
    jarvis_api_key: str = "local-dev-key"
    jarvis_default_mode: Literal["work", "personal", "strategic"] = "work"
    jarvis_user_name: str = "Boss"
    jarvis_timezone: str = "Asia/Kolkata"

    # ── LLM ────────────────────────────────────────────────
    # Free-first: defaults to Ollama (local). Set LLM_PROVIDER=anthropic + key for cloud.
    llm_provider: Literal["anthropic", "openai", "ollama"] = "ollama"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"   # cheapest Anthropic model
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"                    # cheapest OpenAI model
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:14b"                    # best 14B for agent tasks; alt: gemma3:12b

    # ── Database ───────────────────────────────────────────
    # SQLite default — zero install, works everywhere.
    # To use Postgres: postgresql+asyncpg://user:pass@localhost:5432/jarvis
    database_url: str = "sqlite+aiosqlite:///./jarvis.db"
    redis_url: str = "redis://localhost:6379"  # optional, not required

    # ── Vector / Memory ────────────────────────────────────
    chroma_persist_dir: str = "./chroma_data"

    # ── Search ─────────────────────────────────────────────
    # DuckDuckGo is always free (no key). Tavily/Serper give richer results.
    tavily_api_key: str = ""
    serper_api_key: str = ""

    # ── Voice ──────────────────────────────────────────────
    # STT: faster-whisper (local/free). Model: tiny | base | small | medium | large-v3
    whisper_model: str = "base"
    # TTS: edge-tts is free (MS neural). elevenlabs needs a key for premium voices.
    tts_provider: Literal["edge-tts", "elevenlabs"] = "edge-tts"
    # JARVIS = British male butler. Default to a confident neural British voice.
    # Alternatives: en-GB-ThomasNeural (warmer), en-GB-SoniaNeural (female),
    # en-AU-WilliamNeural (Australian male). Override in .env if desired.
    edge_tts_voice: str = "en-GB-RyanNeural"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    # Wake word sensitivity 0.0–1.0 (lower = more sensitive, more false positives)
    wake_word_sensitivity: float = 0.5
    # LiveKit (optional cloud room-based voice, not needed for local pipeline)
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    sarvam_api_key: str = ""

    # ── Google ─────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    @property
    def has_search(self) -> bool:
        return True  # DuckDuckGo always available as free fallback

    @property
    def has_premium_search(self) -> bool:
        return bool(self.tavily_api_key or self.serper_api_key)

    @property
    def effective_provider(self) -> str:
        """Auto-detect provider if not explicitly set."""
        if self.llm_provider == "anthropic" and self.anthropic_api_key:
            return "anthropic"
        if self.openai_api_key:
            return "openai"
        return "ollama"


@lru_cache
def get_settings() -> Settings:
    return Settings()

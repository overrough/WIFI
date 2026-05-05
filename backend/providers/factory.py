"""
Provider factory — returns the configured LLM provider as a singleton.
Call get_provider() everywhere; never instantiate providers directly.
"""

from functools import lru_cache

from core.config import get_settings
from providers.base import ModelProvider


@lru_cache
def get_provider() -> ModelProvider:
    settings = get_settings()
    provider = settings.effective_provider

    if provider == "anthropic":
        from providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
        )
    elif provider == "openai":
        from providers.openai_provider import OpenAIProvider
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
        )
    else:
        from providers.ollama_provider import OllamaProvider
        return OllamaProvider(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
        )

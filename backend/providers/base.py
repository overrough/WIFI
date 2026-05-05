"""
ModelProvider abstraction — never call a vendor SDK directly from business logic.
Swap the LLM without touching the agent code.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional


@dataclass
class Message:
    role: str   # "system" | "user" | "assistant" | "tool"
    content: str
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None   # for tool result messages


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict   # JSON Schema


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = "stop"

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class ModelProvider(ABC):
    """Abstract base — every LLM backend implements this interface."""

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: Optional[list[Tool]] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> ModelResponse:
        """Single-turn completion. Returns a full response."""

    @abstractmethod
    async def stream_chat(
        self,
        messages: list[Message],
        tools: Optional[list[Tool]] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncGenerator[str, None]:
        """Token-streaming completion. Yields text chunks."""

    def to_langchain_model(self) -> Any:
        """Return an equivalent LangChain chat model for agent use."""
        raise NotImplementedError

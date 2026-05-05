"""
Anthropic (Claude) provider — recommended for complex reasoning and writing.
Used as the default when ANTHROPIC_API_KEY is set.
"""

import json
import logging
from typing import Any, AsyncGenerator, Optional

import anthropic
from langchain_anthropic import ChatAnthropic

from providers.base import Message, ModelProvider, ModelResponse, Tool, ToolCall

logger = logging.getLogger(__name__)


class AnthropicProvider(ModelProvider):
    def __init__(self, api_key: str, model: str = "claude-opus-4-6"):
        self.model = model
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self._api_key = api_key
        self._lc_model: Optional[Any] = None

    def _build_messages(
        self, messages: list[Message]
    ) -> tuple[str | None, list[dict]]:
        """Split system prompt out; Anthropic takes it separately."""
        system = None
        converted = []
        for m in messages:
            if m.role == "system":
                system = m.content
                continue
            if m.role == "tool":
                converted.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id,
                            "content": m.content,
                        }
                    ],
                })
            elif m.tool_calls:
                tool_use_blocks = [
                    {
                        "type": "tool_use",
                        "id": tc.get("id"),
                        "name": tc.get("name"),
                        "input": tc.get("arguments", {}),
                    }
                    for tc in m.tool_calls
                ]
                converted.append({"role": "assistant", "content": tool_use_blocks})
            else:
                converted.append({"role": m.role, "content": m.content})
        return system, converted

    def _build_tools(self, tools: list[Tool]) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in tools
        ]

    async def chat(
        self,
        messages: list[Message],
        tools: Optional[list[Tool]] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> ModelResponse:
        system, converted = self._build_messages(messages)
        kwargs: dict = {"model": self.model, "max_tokens": max_tokens,
                        "temperature": temperature, "messages": converted}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._build_tools(tools)

        resp = await self.client.messages.create(**kwargs)

        text_parts = []
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input or {},
                ))

        return ModelResponse(
            content=" ".join(text_parts),
            tool_calls=tool_calls,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            finish_reason=resp.stop_reason or "stop",
        )

    async def stream_chat(
        self,
        messages: list[Message],
        tools: Optional[list[Tool]] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncGenerator[str, None]:
        system, converted = self._build_messages(messages)
        kwargs: dict = {"model": self.model, "max_tokens": max_tokens,
                        "temperature": temperature, "messages": converted}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._build_tools(tools)

        async with self.client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text

    def to_langchain_model(self) -> ChatAnthropic:
        if self._lc_model is None:
            self._lc_model = ChatAnthropic(
                model=self.model,
                anthropic_api_key=self._api_key,
                temperature=0.7,
                streaming=True,
            )
        return self._lc_model

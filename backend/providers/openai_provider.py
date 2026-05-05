"""
OpenAI (GPT-4o) provider — default for 90% of requests per the spec.
"""

import json
import logging
from typing import Any, AsyncGenerator, Optional

from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI

from providers.base import Message, ModelProvider, ModelResponse, Tool, ToolCall

logger = logging.getLogger(__name__)


class OpenAIProvider(ModelProvider):
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key)
        self._api_key = api_key
        self._lc_model: Optional[Any] = None

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        result = []
        for m in messages:
            if m.role == "tool":
                result.append({
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": m.content,
                })
            elif m.tool_calls:
                result.append({
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": tc.get("id"),
                            "type": "function",
                            "function": {
                                "name": tc.get("name"),
                                "arguments": json.dumps(tc.get("arguments", {})),
                            },
                        }
                        for tc in m.tool_calls
                    ],
                })
            else:
                result.append({"role": m.role, "content": m.content})
        return result

    def _convert_tools(self, tools: list[Tool]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
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
        kwargs: dict = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = "auto"

        resp = await self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        return ModelResponse(
            content=msg.content or "",
            tool_calls=tool_calls,
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            finish_reason=choice.finish_reason or "stop",
        )

    async def stream_chat(
        self,
        messages: list[Message],
        tools: Optional[list[Tool]] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncGenerator[str, None]:
        kwargs: dict = {
            "model": self.model,
            "messages": self._convert_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = self._convert_tools(tools)
            kwargs["tool_choice"] = "auto"

        async for chunk in await self.client.chat.completions.create(**kwargs):
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    def to_langchain_model(self) -> ChatOpenAI:
        if self._lc_model is None:
            self._lc_model = ChatOpenAI(
                model=self.model,
                openai_api_key=self._api_key,
                temperature=0.7,
                streaming=True,
            )
        return self._lc_model

"""
Ollama provider — local models (LLaMA-3, Mistral, Qwen) for offline/private use.
Requires Ollama running at OLLAMA_BASE_URL.
"""

import json
import logging
from typing import Any, AsyncGenerator, Optional

import httpx
from langchain_community.chat_models import ChatOllama

from providers.base import Message, ModelProvider, ModelResponse, Tool, ToolCall

logger = logging.getLogger(__name__)


class OllamaProvider(ModelProvider):
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3"):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._lc_model: Optional[Any] = None

    def _build_payload(
        self,
        messages: list[Message],
        tools: Optional[list[Tool]],
        stream: bool,
        temperature: float,
    ) -> dict:
        converted = [
            {"role": m.role if m.role != "tool" else "user", "content": m.content}
            for m in messages
            if m.role != "tool" or True  # include tool results as user messages
        ]
        payload: dict = {
            "model": self.model,
            "messages": converted,
            "stream": stream,
            "options": {"temperature": temperature},
        }
        if tools:
            payload["tools"] = [
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
        return payload

    async def chat(
        self,
        messages: list[Message],
        tools: Optional[list[Tool]] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> ModelResponse:
        payload = self._build_payload(messages, tools, stream=False, temperature=temperature)
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

        msg = data.get("message", {})
        content = msg.get("content", "")
        tool_calls = []
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append(ToolCall(
                id=f"ollama-{fn.get('name', 'tool')}",
                name=fn.get("name", ""),
                arguments=args,
            ))

        return ModelResponse(content=content, tool_calls=tool_calls)

    async def stream_chat(
        self,
        messages: list[Message],
        tools: Optional[list[Tool]] = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncGenerator[str, None]:
        payload = self._build_payload(messages, tools, stream=True, temperature=temperature)
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        chunk = data.get("message", {}).get("content", "")
                        if chunk:
                            yield chunk
                    except json.JSONDecodeError:
                        continue

    def to_langchain_model(self) -> ChatOllama:
        if self._lc_model is None:
            self._lc_model = ChatOllama(
                base_url=self.base_url,
                model=self.model,
                temperature=0.7,
            )
        return self._lc_model

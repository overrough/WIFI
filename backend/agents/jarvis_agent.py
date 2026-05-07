"""
Jarvis Core Agent — planning + execution loop.

Architecture upgrade from Phase 1:
  1. Retrieve relevant memories (ChromaDB semantic search)
  2. CLASSIFY: is this a simple 1-step task or a complex multi-step goal?
  3. PLAN (complex tasks only): decompose into ordered steps before acting
  4. EXECUTE: LangGraph ReAct agent with the full tool registry
  5. Stream response tokens to the caller
  6. After conversation: extract + store new memories

The planning step is what separates a reactive assistant from one that
can handle "analyze my project files and find performance bottlenecks"
or "automate my morning briefing" without needing hand-holding.
"""

import logging
import uuid
from typing import Any, AsyncGenerator, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from agents.planner import classify_complexity, generate_plan
from core.config import get_settings
from memory.manager import MemoryManager, get_memory_manager
from prompts.system_prompt import build_system_prompt
from providers.factory import get_provider
from tools.browser_tools import make_browser_tools
from tools.calendar_tools import make_calendar_tools
from tools.claude_tools import make_claude_tools
from tools.computer_tools import make_computer_tools
from tools.datetime_tool import make_datetime_tools
from tools.gmail_tools import make_gmail_tools
from tools.memory_tools import make_memory_tools
from tools.search_tools import make_search_tools
from tools.task_tools import make_task_tools

logger = logging.getLogger(__name__)
settings = get_settings()

ALL_TOOL_NAMES = [
    # Memory
    "save_memory", "search_memory", "update_profile",
    # Tasks
    "create_task", "list_tasks", "update_task",
    # Search
    "search_web",
    # Date/time
    "get_datetime",
    # Computer automation
    "list_files", "read_file", "write_file", "search_files",
    "execute_command", "run_python_code",
    "take_screenshot", "analyze_screen", "get_system_info",
    # Browser
    "extract_webpage", "run_browser_task",
    # Google Calendar
    "list_calendar_events", "add_calendar_event", "find_free_slots",
    # Gmail
    "draft_email", "send_email", "list_emails", "reply_email",
    # Claude integration
    "open_claude", "send_to_claude", "claude_cowork",
]


class JarvisAgent:
    """
    Stateless per-request agent.
    Conversation history is passed in; agent state lives in the database.
    """

    def __init__(self, memory_manager: Optional[MemoryManager] = None) -> None:
        self.memory_manager = memory_manager or get_memory_manager()
        self.provider = get_provider()

    def _build_tools(self, user_id: str) -> list:
        return (
            make_memory_tools(user_id)
            + make_task_tools(user_id)
            + make_search_tools()
            + make_datetime_tools()
            + make_computer_tools()
            + make_browser_tools()
            + make_calendar_tools()
            + make_gmail_tools()
            + make_claude_tools()
        )

    def _build_agent(self, user_id: str, system_prompt: str, tools: Optional[list] = None):
        """Build a LangGraph ReAct agent with the given system prompt.

        `tools` may be passed explicitly to scope the agent to a sub-agent's
        toolset. If None, the full registry is used.
        """
        if tools is None:
            tools = self._build_tools(user_id)
        llm = self.provider.to_langchain_model()
        # NOTE: LangGraph 0.2+ renamed `state_modifier` → `prompt`.
        # Passing the wrong name raises TypeError and the entire stream
        # silently returns empty, which surfaces as "I'm not sure how to
        # respond" in the voice agent. This was the bug behind every
        # failed command.
        return create_react_agent(
            model=llm,
            tools=tools,
            prompt=system_prompt,
        )

    async def _prepare_context(
        self,
        query: str,
        user_id: str,
        user_name: str,
        mode: str,
        plan: Optional[str] = None,
    ) -> str:
        """Build system prompt: memory context + optional execution plan."""
        memory_context = await self.memory_manager.retrieve_for_context(query, user_id)
        system_prompt = build_system_prompt(
            user_name=user_name,
            mode=mode,
            memory_context=memory_context,
            available_tool_names=ALL_TOOL_NAMES,
        )
        if plan:
            system_prompt += f"\n\n─────────────────────────────────\nEXECUTION PLAN (follow this):\n{plan}\n─────────────────────────────────"
        return system_prompt

    # ── Public interface ──────────────────────────────────────────────────────

    async def chat(
        self,
        query: str,
        user_id: str,
        user_name: str,
        mode: str,
        history: Optional[list[dict]] = None,
    ) -> str:
        """Single-turn chat. Returns the full response string."""
        plan = await self._maybe_plan(query, user_id, user_name, mode)
        system_prompt = await self._prepare_context(query, user_id, user_name, mode, plan)
        agent = self._build_agent(user_id, system_prompt)
        messages = self._build_messages(history or [], query)
        result = await agent.ainvoke({"messages": messages})
        last = result["messages"][-1]
        return last.content if hasattr(last, "content") else str(last)

    async def stream_chat(
        self,
        query: str,
        user_id: str,
        user_name: str,
        mode: str,
        history: Optional[list[dict]] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Streaming chat — yields text chunks as they arrive.
        Tool calls are executed silently; only the final text stream is yielded.

        Multi-agent dance:
          1. Orchestrator: classify the request into a lane
             (research / write / execute / answer).
          2. If a sub-agent owns the lane, build a SCOPED agent with
             that sub-agent's role prompt + restricted toolset. This keeps
             the LLM focused and stops the model from grabbing irrelevant
             tools when the task is, say, just "draft a LinkedIn post".
          3. If lane == "answer", fall through to a planning + ReAct loop
             with the full toolset (existing behaviour).
        """
        # ── Step 1: Orchestrator ────────────────────────────────────────────
        from agents.orchestrator import classify_lane, get_sub_agent_role
        lane = await classify_lane(query, self.provider)
        logger.info("Orchestrator lane: %s for query=%r", lane, query[:80])

        sub_role = get_sub_agent_role(lane) if lane in ("researcher", "writer", "executor") else None

        # ── Step 2: Sub-agent path (focused role + scoped tools) ────────────
        if sub_role is not None:
            # Build memory context for the role
            memory_context = await self.memory_manager.retrieve_for_context(query, user_id)
            # Use the SAME system prompt structure as the main agent so the
            # JARVIS personality is preserved, but layer the role's specific
            # operating instructions on top.
            base_prompt = build_system_prompt(
                user_name=user_name,
                mode=mode,
                memory_context=memory_context,
                available_tool_names=sub_role.tool_names,
            )
            system_prompt = (
                base_prompt
                + "\n\n─────────────────────────────────\n"
                + f"ROLE FOR THIS TURN — {sub_role.name.upper()}:\n"
                + sub_role.system_prompt
                + "\n─────────────────────────────────"
            )
            # Filter the toolset down to just the role's allowed tools
            all_tools = self._build_tools(user_id)
            scoped_tools = [t for t in all_tools if t.name in sub_role.tool_names]
            agent = self._build_agent(user_id, system_prompt, tools=scoped_tools)
            messages = self._build_messages(history or [], query)

            try:
                async for event in agent.astream_events({"messages": messages}, version="v2"):
                    kind = event.get("event")
                    if kind == "on_chat_model_stream":
                        chunk = event.get("data", {}).get("chunk")
                        if chunk and hasattr(chunk, "content"):
                            content = chunk.content
                            if isinstance(content, str) and content:
                                yield content
                            elif isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        text = block.get("text", "")
                                        if text:
                                            yield text
                    elif kind == "on_tool_start":
                        logger.info(
                            "[%s] Tool: %s(%s)",
                            sub_role.name,
                            event.get("name", ""),
                            str(event.get("data", {}).get("input", {}))[:120],
                        )
            except Exception as exc:
                logger.error("Sub-agent %s stream error: %s", sub_role.name, exc, exc_info=True)
                yield f"\n\n[{sub_role.name} error: {exc}]"
            return

        # ── Step 3: Default path (full agent + optional plan) ───────────────
        is_complex = await classify_complexity(query, self.provider)

        # Signal thinking start to UI
        if is_complex:
            yield "\u200b"   # zero-width space — tells UI we're thinking

        plan = None
        if is_complex:
            memory_context = await self.memory_manager.retrieve_for_context(query, user_id)
            plan = await generate_plan(
                query=query,
                context=memory_context,
                available_tools=ALL_TOOL_NAMES,
                provider=self.provider,
            )

        system_prompt = await self._prepare_context(query, user_id, user_name, mode, plan)
        agent = self._build_agent(user_id, system_prompt)
        messages = self._build_messages(history or [], query)

        try:
            async for event in agent.astream_events(
                {"messages": messages},
                version="v2",
            ):
                kind = event.get("event")

                # Stream text tokens from the LLM
                if kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content"):
                        content = chunk.content
                        if isinstance(content, str) and content:
                            yield content
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text:
                                        yield text

                # Log tool calls (visible in server logs, not streamed to user)
                elif kind == "on_tool_start":
                    tool_name = event.get("name", "")
                    tool_input = event.get("data", {}).get("input", {})
                    logger.info("Tool call: %s(%s)", tool_name, str(tool_input)[:120])

        except Exception as exc:
            logger.error("Agent stream error: %s", exc, exc_info=True)
            yield f"\n\n[Error during execution: {exc}]"

    async def _maybe_plan(
        self,
        query: str,
        user_id: str,
        user_name: str,
        mode: str,
    ) -> Optional[str]:
        """Plan if complex; skip if simple."""
        is_complex = await classify_complexity(query, self.provider)
        if not is_complex:
            return None
        memory_context = await self.memory_manager.retrieve_for_context(query, user_id)
        return await generate_plan(
            query=query,
            context=memory_context,
            available_tools=ALL_TOOL_NAMES,
            provider=self.provider,
        )

    def _build_messages(self, history: list[dict], query: str) -> list:
        """Convert stored history dicts to LangChain message objects. Cap at 20 turns."""
        msgs = []
        for msg in history[-20:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if not content:
                continue
            if role == "user":
                msgs.append(HumanMessage(content=content))
            elif role == "assistant":
                msgs.append(AIMessage(content=content))
        msgs.append(HumanMessage(content=query))
        return msgs

    async def close_conversation(
        self,
        conversation: list[dict],
        user_id: str,
    ) -> int:
        """Extract and store memories from the finished conversation."""
        return await self.memory_manager.extract_and_store(
            conversation=conversation,
            user_id=user_id,
            provider=self.provider,
        )


# Module-level singleton
_agent: Optional[JarvisAgent] = None


def get_agent() -> JarvisAgent:
    global _agent
    if _agent is None:
        _agent = JarvisAgent()
    return _agent

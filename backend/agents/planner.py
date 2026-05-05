"""
Task planner — decomposes complex goals into an ordered execution plan.

The distinction between a reactive chatbot and a true assistant:
  - Chatbot: hears "automate my morning briefing" → asks clarifying questions forever
  - Planner: hears "automate my morning briefing" → breaks it into:
      1. Check calendar for today's events
      2. Search for news in user's interest areas
      3. List open high-priority tasks
      4. Compose a structured briefing
      5. Optionally draft an email summary

The plan is injected into the system prompt so the ReAct loop has direction.
Simple requests (one clear step) skip planning entirely to avoid latency.
"""

import logging
import re
from typing import Optional

from providers.base import Message, ModelProvider

logger = logging.getLogger(__name__)


COMPLEXITY_PROMPT = """\
Classify this user request as SIMPLE or COMPLEX.

SIMPLE: single clear action, one tool call, direct question/answer.
  Examples: "What time is it?", "Create a task: call dentist", "List my tasks"

COMPLEX: requires multiple steps, planning, reasoning over results, or
  coordinates across several tools or data sources.
  Examples: "Plan my week", "Analyze the files in my projects folder",
            "Research X and write a report", "Automate my morning routine"

Request: {query}

Answer with one word: SIMPLE or COMPLEX"""


PLANNING_PROMPT = """\
You are planning the execution of a user request for an AI assistant named Jarvis.

USER REQUEST: {query}

USER CONTEXT:
{context}

AVAILABLE TOOLS:
{tools}

Create a concise execution plan. Number each step. Be specific about which tool to use.
Focus on the minimum steps needed — avoid redundancy.
Maximum 7 steps. If fewer steps suffice, use fewer.

FORMAT:
1. [tool_name] What to do
2. [tool_name] What to do
...
EXPECTED OUTCOME: one sentence describing what success looks like

Plan:"""


async def classify_complexity(query: str, provider: ModelProvider) -> bool:
    """Returns True if the request is complex and warrants planning."""
    # Fast heuristic first — avoid an extra API call for obvious cases
    simple_patterns = [
        r"^(what|who|when|where) (time|day|date)",
        r"^(list|show|get) (my )?(tasks?|memories?)",
        r"^(create|add|make) (a )?(task|reminder)",
        r"^(switch to|enable) \w+ mode",
        r"^(remember|note|save)",
        r"^hello|^hi |^hey ",
    ]
    lower = query.lower().strip()
    for pattern in simple_patterns:
        if re.match(pattern, lower):
            return False  # simple

    # For ambiguous cases, ask the LLM
    try:
        response = await provider.chat(
            [Message(role="user", content=COMPLEXITY_PROMPT.format(query=query))],
            temperature=0.0,
            max_tokens=10,
        )
        return "COMPLEX" in response.content.upper()
    except Exception:
        return False   # fail open — skip planning on error


async def generate_plan(
    query: str,
    context: str,
    available_tools: list[str],
    provider: ModelProvider,
) -> Optional[str]:
    """
    Generate a step-by-step execution plan for a complex request.
    Returns the plan as a formatted string to inject into the system prompt.
    Returns None if planning fails (agent continues without plan).
    """
    tools_summary = ", ".join(available_tools)
    prompt = PLANNING_PROMPT.format(
        query=query,
        context=context[:1500],
        tools=tools_summary,
    )

    try:
        response = await provider.chat(
            [Message(role="user", content=prompt)],
            temperature=0.2,
            max_tokens=512,
        )
        plan = response.content.strip()
        if not plan or len(plan) < 20:
            return None
        logger.debug("Generated plan for '%s':\n%s", query[:60], plan)
        return plan
    except Exception as exc:
        logger.warning("Planning failed (proceeding without plan): %s", exc)
        return None

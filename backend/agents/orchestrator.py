"""
Master Orchestrator — routes a request to the right sub-agent(s).

Spec §6: "All coordinated by a master Orchestrator agent."

This is the brain layer above the existing JarvisAgent. For now, it uses
a fast LLM call to classify the request into one of four lanes:

    research / write / execute / answer

If the request needs research → invoke Researcher.
If it needs drafted content → invoke Writer.
If it needs computer-side action → invoke Executor (this is what JarvisAgent
already does, so we delegate).
If it's a simple Q&A or chat → answer directly with the main JarvisAgent.

For complex multi-step jobs, the Orchestrator can chain (Researcher →
Writer → Executor) — but we keep that path simple right now and let the
existing planner do most of the heavy lifting.

This file is the foundation. The full multi-agent dance — Reviewer
gating every output, parallel research threads, etc. — is a future
expansion. For now, we get the routing skeleton in place so the rest
of the system can evolve without major surgery.
"""

import logging
import re
from typing import Optional

from agents.sub_agents import SUB_AGENTS, SubAgentRole
from providers.base import Message, ModelProvider

logger = logging.getLogger(__name__)


CLASSIFY_PROMPT = """\
Classify the user's request into ONE of these lanes:

RESEARCH — needs gathering information from the web, documents, or
           Sir's memory. Examples: "what are competitors doing?",
           "find the latest on X", "summarise this article".

WRITE    — needs producing text in Sir's voice (emails, LinkedIn,
           Instagram, proposals, replies). Examples: "draft a follow-up
           to the Singh client", "write today's LinkedIn post".

EXECUTE  — needs doing something on the computer or sending something
           externally. Examples: "open Chrome", "send the email",
           "run the deploy script", "list my tasks", "what's on my
           calendar?".

ANSWER   — direct conversational reply (greetings, opinions, short
           factual answers Jarvis already knows). Examples: "hi",
           "what time is it?", "what's your favourite framework?".

Respond with ONE WORD: RESEARCH, WRITE, EXECUTE, or ANSWER.

Request: {query}"""


_LANE_TO_AGENT = {
    "RESEARCH": "researcher",
    "WRITE":    "writer",
    "EXECUTE":  "executor",
    "ANSWER":   None,   # default: handle in main JarvisAgent
}


async def classify_lane(query: str, provider: ModelProvider) -> str:
    """
    Decide which sub-agent (if any) should handle this request.
    Returns the name of the sub-agent role, or 'answer' for default.

    Uses a fast heuristic first; falls back to the LLM for ambiguous cases.
    """
    lower = query.lower().strip()

    # Quick obvious patterns — avoid an LLM call for these
    if re.match(r"^(hi|hello|hey|thanks|thank you|good (morning|afternoon|evening|night))\b", lower):
        return "answer"
    if re.match(r"^(what time|what day|what date|what's the time)\b", lower):
        return "answer"
    if re.match(r"^(open|launch|run|start) ", lower):
        return "executor"
    if re.match(r"^(draft|write|compose|reply to) ", lower):
        return "writer"
    if re.match(r"^(research|find out|look up|search for|what's the latest) ", lower):
        return "researcher"

    # Ambiguous — ask the LLM
    try:
        response = await provider.chat(
            [Message(role="user", content=CLASSIFY_PROMPT.format(query=query))],
            temperature=0.0,
            max_tokens=8,
        )
        word = response.content.strip().upper().split()[0] if response.content else ""
        lane = _LANE_TO_AGENT.get(word, None)
        return lane or "answer"
    except Exception as exc:
        logger.debug("Lane classification failed (%s) — defaulting to answer", exc)
        return "answer"


def get_sub_agent_role(name: str) -> Optional[SubAgentRole]:
    """Return the SubAgentRole config for a given agent name, or None."""
    return SUB_AGENTS.get(name)

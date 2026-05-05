"""
Memory extractor — uses the LLM to decide what's worth remembering
after a conversation ends. Conservative by design: it's better to
store nothing than to store a wrong "fact" about the user.
"""

import json
import logging
from typing import Optional

from providers.base import Message, ModelProvider

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """You are extracting memories from a conversation to build a long-term profile of the user.

Be CONSERVATIVE. Only extract facts that are:
1. Explicitly stated (not inferred)
2. Likely to remain true for more than a week
3. Useful context for future conversations

Return a JSON array (possibly empty). Each item:
{{
  "content": "the memory as a clear, standalone fact",
  "type": "episodic" | "semantic" | "procedural",
  "importance": 0.1–1.0,
  "tags": ["tag1", "tag2"]
}}

Memory types:
- episodic: Something that happened ("User finished the Acme proposal on Apr 10")
- semantic: A fact about the user ("User's timezone is Asia/Kolkata", "User hates early meetings")
- procedural: A behavioural pattern ("User always wants emails shortened to < 5 sentences")

Conversation to analyse:
{conversation}

Return ONLY valid JSON. No explanation, no markdown fences."""


async def extract_memories(
    conversation: list[dict],
    provider: ModelProvider,
) -> list[dict]:
    """
    Given a list of {role, content} dicts, return extractable memories.
    Returns [] on any failure — never crash from a bad extraction.
    """
    if not conversation:
        return []

    # Build a readable transcript
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in conversation
        if m.get("content")
    )

    messages = [
        Message(
            role="user",
            content=EXTRACTION_PROMPT.format(conversation=transcript),
        )
    ]

    try:
        response = await provider.chat(messages, temperature=0.1, max_tokens=1024)
        raw = response.content.strip()

        # Strip markdown fences if the model added them
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        memories = json.loads(raw)
        if not isinstance(memories, list):
            return []

        validated = []
        for m in memories:
            if isinstance(m, dict) and "content" in m and "type" in m:
                validated.append({
                    "content": str(m["content"])[:2000],
                    "type": m.get("type", "semantic"),
                    "importance": float(m.get("importance", 0.5)),
                    "tags": m.get("tags", []),
                })
        return validated

    except Exception as exc:
        logger.warning("Memory extraction failed (safe to ignore): %s", exc)
        return []

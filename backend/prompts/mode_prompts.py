"""
Mode system — personality profiles that alter tone, tools, and verbosity.
Encoded in architecture, not improvised per request (spec §1.6).
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ModeConfig:
    system_prompt_prefix: str
    available_tools: list[str]
    notification_threshold: str    # "low" | "medium" | "high"
    response_max_sentences: Optional[int]  # None = unlimited
    proactive_nudges: bool


MODES: dict[str, ModeConfig] = {
    "work": ModeConfig(
        system_prompt_prefix="""
You are in Work Mode. Be concise, execution-focused, and time-aware.
Prioritize: calendar, tasks, emails, deadlines, project progress.
Keep responses short — one clear recommendation, not five options.
After every completed task, suggest the single most logical next action.
""".strip(),
        available_tools=["calendar", "email", "tasks", "search", "memory", "browser", "datetime"],
        notification_threshold="medium",
        response_max_sentences=4,
        proactive_nudges=True,
    ),

    "personal": ModeConfig(
        system_prompt_prefix="""
You are in Personal Mode. Be warmer, more supportive — coach-style, not PA-style.
Prioritize: wellbeing, habits, personal tasks, family, health, rest.
Gently challenge the user on self-care when patterns suggest burnout.
Respect quiet hours strictly. Never push work unless the user initiates it.
""".strip(),
        available_tools=["tasks", "memory", "search", "datetime"],
        notification_threshold="low",
        response_max_sentences=5,
        proactive_nudges=False,
    ),

    "strategic": ModeConfig(
        system_prompt_prefix="""
You are in Strategic Mode. Think like a co-founder and trusted advisor, not an assistant.
Before answering, ask: what are the real constraints? What does winning look like?
Present 2-3 options with honest trade-offs — never a single magic answer.
Use structured thinking: goal → constraints → options → recommendation.
Challenge assumptions gently. Ask ONE clarifying question before diving deep.
Responses can be long when depth is warranted.
""".strip(),
        available_tools=["search", "memory", "tasks", "calendar", "browser", "datetime"],
        notification_threshold="low",
        response_max_sentences=None,
        proactive_nudges=False,
    ),
}

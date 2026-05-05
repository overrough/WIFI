"""
Tool contract — every Jarvis tool inherits from JarvisTool.
The safe_execute wrapper handles risk-level confirmation and logging.
"""

import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from core.database import db_context
from models.tool_log import ToolExecution

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    success: bool
    data: Any
    error_message: Optional[str] = None
    requires_confirmation: bool = False
    confirmation_prompt: Optional[str] = None

    def to_str(self) -> str:
        if self.success:
            if isinstance(self.data, (dict, list)):
                import json
                return json.dumps(self.data, default=str, indent=2)
            return str(self.data)
        return f"Error: {self.error_message}"


class JarvisTool(ABC):
    name: str
    description: str
    parameters_schema: dict
    risk_level: str = "low"   # "low" | "medium" | "high"

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        pass

    async def safe_execute(
        self,
        conversation_id: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        """
        Wraps execute() with:
        - Risk-level confirmation gating
        - Tool execution logging (mandatory per spec)
        - Timing
        """
        if self.risk_level == "high":
            # Return a confirmation request without executing
            param_summary = ", ".join(f"{k}={v}" for k, v in kwargs.items())
            return ToolResult(
                success=False,
                data=None,
                requires_confirmation=True,
                confirmation_prompt=(
                    f"I'm about to run `{self.name}` with: {param_summary}. "
                    "Confirm to proceed."
                ),
            )

        start = time.monotonic()
        result = ToolResult(success=False, data=None)
        try:
            result = await self.execute(**kwargs)
        except Exception as exc:
            logger.error("Tool %s failed: %s", self.name, exc, exc_info=True)
            result = ToolResult(success=False, data=None, error_message=str(exc))
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            await self._log(
                conversation_id=conversation_id,
                params=kwargs,
                result=result,
                duration_ms=duration_ms,
            )

        return result

    async def _log(
        self,
        conversation_id: Optional[str],
        params: dict,
        result: ToolResult,
        duration_ms: int,
    ) -> None:
        try:
            async with db_context() as db:
                db.add(ToolExecution(
                    conversation_id=uuid.UUID(conversation_id) if conversation_id else None,
                    tool_name=self.name,
                    parameters=params,
                    result={"data": str(result.data)[:2000]} if result.data else None,
                    success=result.success,
                    error_message=result.error_message,
                    duration_ms=duration_ms,
                ))
        except Exception as exc:
            logger.warning("Failed to log tool execution: %s", exc)

"""Base interfaces for tool execution policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from reuleauxcoder.domain.llm.models import ToolCall


@dataclass(frozen=True, slots=True)
class ToolPolicyDecision:
    """Decision emitted by hard tool policies before gateway mapping."""

    allowed: bool = True
    reason: str | None = None
    warning: str | None = None
    requires_approval: bool = False

    @classmethod
    def allow(cls) -> "ToolPolicyDecision":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str | None = None) -> "ToolPolicyDecision":
        return cls(allowed=False, reason=reason)

    @classmethod
    def warn(cls, warning: str | None = None) -> "ToolPolicyDecision":
        return cls(allowed=True, warning=warning)

    @classmethod
    def require_approval(cls, reason: str | None = None) -> "ToolPolicyDecision":
        return cls(allowed=True, reason=reason, requires_approval=True)


class ToolPolicy(Protocol):
    """Policy interface for validating tool calls before execution."""

    def evaluate(self, tool_call: ToolCall) -> ToolPolicyDecision | None:
        """Return a decision when the policy applies, else None."""

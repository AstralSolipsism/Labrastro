"""Stable deferred capability execution gateway."""

from __future__ import annotations

from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.extensions.tools.spec import ToolRisk


@register_tool
class CapabilityExecuteTool(Tool):
    """Execute a registered deferred capability by tool_id."""

    name = "capability_execute"
    description = (
        "Execute a deferred capability tool returned by tool_search. The "
        "target is selected only by tool_id and executed through the shared "
        "ToolExecutor permission, approval, preview, and audit path."
    )
    parameters = {
        "type": "object",
        "properties": {
            "tool_id": {
                "type": "string",
                "description": "Deferred capability tool id returned by tool_search.",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments for the selected deferred tool.",
                "additionalProperties": True,
            },
        },
        "required": ["tool_id", "arguments"],
        "additionalProperties": False,
    }
    risk = ToolRisk.CAPABILITY
    permission_policy = "capability"
    search_keywords = ("capability", "execute", "deferred")

    def execute(self, **kwargs) -> str:  # noqa: ARG002
        return "Error: capability_execute must be executed through ToolExecutor"

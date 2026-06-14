"""Stable deferred capability search gateway."""

from __future__ import annotations

from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.extensions.tools.spec import (
    ToolOutputStrategy,
    ToolRisk,
)


@register_tool
class ToolSearchTool(Tool):
    """Search deferred capability tools without changing the top-level tool list."""

    name = "tool_search"
    description = (
        "Search deferred capability tools by query. Results are returned as "
        "tool_id entries for append-only follow-up with capability_execute."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search terms describing the needed capability tool.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of matching tools to return.",
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    output_strategy = ToolOutputStrategy.JSON
    risk = ToolRisk.READ_ONLY
    permission_policy = "read_only"
    search_keywords = ("capability", "search", "deferred")

    def execute(self, **kwargs) -> str:  # noqa: ARG002
        return "Error: tool_search must be executed through ToolExecutor"

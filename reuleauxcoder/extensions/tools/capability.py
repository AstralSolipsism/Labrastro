"""Capability-backed deferred tool references."""

from __future__ import annotations

from typing import Any

from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.spec import ToolSpec


class CapabilityToolReference(Tool):
    """Deferred capability tool registered in the unified tool catalog."""

    tool_source = "capability"

    def __init__(self, spec_data: dict[str, Any]) -> None:
        spec_data = dict(spec_data)
        metadata = dict(spec_data.get("metadata") or {})
        if spec_data.get("tool_id"):
            metadata.setdefault("tool_id", str(spec_data.get("tool_id")))
        if spec_data.get("source_type"):
            metadata.setdefault("source_type", str(spec_data.get("source_type")))
        if spec_data.get("target_tool_ref"):
            metadata.setdefault("target_tool_ref", str(spec_data.get("target_tool_ref")))
        spec_data["metadata"] = metadata
        self._spec = ToolSpec.from_dict(spec_data)
        self.name = self._spec.name
        self.description = self._spec.description
        self.parameters = self._spec.input_schema
        self.tool_id = metadata.get("tool_id", f"{self._spec.namespace}:{self._spec.name}")
        super().__init__(backend=None)

    def tool_spec(self) -> ToolSpec:
        return self._spec

    def execute(self, **kwargs) -> str:  # noqa: ARG002
        return (
            f"Error: capability tool '{self.tool_id}' must be invoked through "
            "capability_execute"
        )

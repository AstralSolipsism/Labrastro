"""Tool wrappers for MCP servers running on remote peers."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from labrastro_server.adapters.reuleauxcoder.remote_backend import RemoteRelayToolBackend
from labrastro_server.interfaces.http.remote.protocol import RemoteMCPToolInfo
from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.spec import ToolExposure, ToolRisk, ToolSpec


class RemotePeerMCPTool(Tool):
    """Expose a peer-hosted MCP tool through the remote relay."""

    namespace = "mcp"
    risk = ToolRisk.CAPABILITY
    exposure = ToolExposure.DEFERRED
    permission_policy = "capability"
    tool_source = "mcp"

    def __init__(self, backend: RemoteRelayToolBackend, tool_info: RemoteMCPToolInfo):
        super().__init__(backend)
        self._tool_info = tool_info
        self.name = tool_info.name
        self.description = tool_info.description
        self.parameters = tool_info.input_schema or {"type": "object", "properties": {}}
        self.server_name = tool_info.server_name

    def execute(self, **kwargs: Any) -> str:
        if not isinstance(self.backend, RemoteRelayToolBackend):
            return "Error: peer MCP tool requires a remote relay backend"
        return self.backend.exec_tool(
            "mcp",
            {
                "server_name": self.server_name,
                "tool_name": self._tool_info.name,
                "arguments": kwargs,
            },
        )

    def tool_spec(self) -> ToolSpec:
        spec = super().tool_spec()
        server_name = str(self.server_name or "default").strip() or "default"
        tool_name = str(self._tool_info.name or self.name).strip()
        tool_id = f"mcp:{server_name}:{tool_name}"
        metadata = {
            **dict(spec.metadata),
            "tool_id": tool_id,
            "server_name": server_name,
            "source_type": "remote_mcp",
        }
        return replace(
            spec,
            search_keywords=tuple(
                item
                for item in ("mcp", "remote", server_name, tool_name)
                if item
            ),
            metadata=metadata,
        )

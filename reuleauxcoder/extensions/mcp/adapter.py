"""MCP adapter - wraps MCP tools as internal tools."""

import asyncio
import concurrent.futures
import contextvars
from dataclasses import replace

from reuleauxcoder.extensions.mcp.client import MCPClient
from reuleauxcoder.extensions.mcp.models import MCPToolInfo
from reuleauxcoder.extensions.mcp.timeouts import MCP_TOOL_CALL_TIMEOUT_SEC
from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.spec import ToolExposure, ToolRisk, ToolSpec


class MCPTool(Tool):
    """Wraps an MCP tool as an internal Tool instance."""

    namespace = "mcp"
    risk = ToolRisk.CAPABILITY
    exposure = ToolExposure.DEFERRED
    permission_policy = "capability"
    tool_source = "mcp"

    def __init__(
        self, client: MCPClient, tool_info: MCPToolInfo, loop: asyncio.AbstractEventLoop
    ):
        self._client = client
        self._tool_info = tool_info
        self._loop = loop
        self.name = tool_info.name
        self.description = tool_info.description
        self.parameters = tool_info.input_schema
        self.server_name = tool_info.server_name
        self._lifecycle_context: contextvars.ContextVar[dict | None] = (
            contextvars.ContextVar(
                f"mcp_tool_{tool_info.server_name}_{tool_info.name}_lifecycle_context",
                default=None,
            )
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
            "source_type": "local_mcp",
        }
        return replace(
            spec,
            search_keywords=tuple(
                item
                for item in ("mcp", "local", server_name, tool_name)
                if item
            ),
            metadata=metadata,
        )

    def bind_lifecycle_context(self, context: dict):
        token = self._lifecycle_context.set(dict(context))

        def _restore() -> None:
            self._lifecycle_context.reset(token)

        return _restore

    def execute(self, **kwargs) -> str:
        if self._loop is None or not self._loop.is_running():
            return "Error: MCP event loop not running"

        future = asyncio.run_coroutine_threadsafe(
            self._client.call_tool(
                self._tool_info.name,
                kwargs,
                lifecycle_context=dict(self._lifecycle_context.get() or {}),
            ),
            self._loop,
        )
        try:
            return future.result(timeout=MCP_TOOL_CALL_TIMEOUT_SEC)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return "Error: MCP tool call timed out"
        except Exception as e:
            return f"Error: {e}"

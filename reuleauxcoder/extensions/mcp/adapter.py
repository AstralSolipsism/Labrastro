"""MCP adapter - wraps MCP tools as internal tools."""

import asyncio
import concurrent.futures
import contextvars

from reuleauxcoder.extensions.mcp.client import MCPClient
from reuleauxcoder.extensions.mcp.models import MCPToolInfo
from reuleauxcoder.extensions.mcp.timeouts import MCP_TOOL_CALL_TIMEOUT_SEC
from reuleauxcoder.extensions.tools.base import Tool


class MCPTool(Tool):
    """Wraps an MCP tool as an internal Tool instance."""

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

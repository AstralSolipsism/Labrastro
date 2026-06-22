"""MCP client - connects to MCP servers and calls their tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reuleauxcoder.interfaces.events import UIEventBus

from reuleauxcoder import __version__
from reuleauxcoder.extensions.mcp.models import MCPToolInfo
from reuleauxcoder.extensions.mcp.timeouts import (
    MCP_REQUEST_TIMEOUT_SEC,
    MCP_TOOL_CALL_REQUEST_TIMEOUT_SEC,
)
from reuleauxcoder.infrastructure.platform import get_platform_info

MCPElicitationHandler = Callable[
    [dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]
]

MCP_PROTOCOL_VERSION = "2025-11-25"
# Send the latest version we implement, while accepting older negotiated
# stdio tool protocol revisions that this client can still operate against.
MCP_SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {
        "2025-11-25",
        "2025-06-18",
        "2025-03-26",
        "2024-11-05",
        "2024-10-07",
    }
)
_MCP_FORM_ELICITATION_CAPABILITY = {"form": {}}
_MCP_UNSUPPORTED_ELICITATION_MODE_REASON = "mcp_elicitation_unsupported_mode"


@dataclass(frozen=True)
class _ActiveMCPToolCall:
    name: str
    arguments: dict[str, Any]
    lifecycle_context: dict[str, Any]


class MCPClient:
    """Async client for communicating with an MCP server via stdio."""

    def __init__(
        self,
        config,
        ui_bus: "UIEventBus | None" = None,
        elicitation_handler: MCPElicitationHandler | None = None,
    ):
        self.config = config
        self._ui_bus = ui_bus
        self._elicitation_handler = elicitation_handler
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._tools: list[MCPToolInfo] = []
        self._initialized = False
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._receive_task: asyncio.Task | None = None
        self._negotiated_protocol_version = ""
        self._active_tool_call: _ActiveMCPToolCall | None = None
        self._tool_call_lock: asyncio.Lock | None = None
        self._tool_call_lock_loop: asyncio.AbstractEventLoop | None = None
        self._active_tool_name = ""
        self._active_tool_arguments: dict[str, Any] = {}
        self._active_lifecycle_context: dict[str, Any] = {}

    @property
    def tools(self) -> list[MCPToolInfo]:
        return self._tools

    def _client_capabilities(self) -> dict[str, Any]:
        capabilities: dict[str, Any] = {}
        if self._elicitation_handler is not None:
            capabilities["elicitation"] = {
                key: dict(value)
                for key, value in _MCP_FORM_ELICITATION_CAPABILITY.items()
            }
        return capabilities

    def _emit(self, level: str, message: str) -> None:
        """Emit a UI event if bus is available."""
        if not self._ui_bus:
            return
        from reuleauxcoder.interfaces.events import UIEventKind

        method = getattr(self._ui_bus, level, None)
        if method:
            method(f"[MCP] {message}", kind=UIEventKind.MCP)

    async def connect(self) -> bool:
        cmd = shutil.which(self.config.command)
        if not cmd:
            for prefix in get_platform_info().get_bin_paths():
                candidate = os.path.join(prefix, self.config.command)
                if os.path.exists(candidate):
                    cmd = candidate
                    break

        if not cmd:
            self._emit("error", f"Cannot find command: {self.config.command}")
            return False

        env = os.environ.copy()
        env.update(self.config.env)

        try:
            self._process = await asyncio.create_subprocess_exec(
                cmd,
                *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.config.cwd,
            )
            self._reader = self._process.stdout
            self._writer = self._process.stdin
        except Exception as e:
            self._emit("error", f"Failed to start server '{self.config.name}': {e}")
            return False

        self._receive_task = asyncio.create_task(self._receive_loop())

        try:
            result = await self._request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": self._client_capabilities(),
                    "clientInfo": {"name": "reuleauxcoder", "version": __version__},
                },
            )

            if not result:
                self._emit("error", f"Failed to initialize server '{self.config.name}'")
                return False

            protocol_version = str(result.get("protocolVersion") or "").strip()
            if protocol_version not in MCP_SUPPORTED_PROTOCOL_VERSIONS:
                negotiated = protocol_version or "<missing>"
                self._emit(
                    "error",
                    (
                        f"Server '{self.config.name}' negotiated unsupported MCP "
                        f"protocol version: {negotiated}"
                    ),
                )
                return False
            self._negotiated_protocol_version = protocol_version

            await self._notify("notifications/initialized", {})

            server_capabilities = result.get("capabilities")
            if not isinstance(server_capabilities, dict):
                server_capabilities = {}
            tools_result = None
            if isinstance(server_capabilities.get("tools"), dict):
                tools_result = await self._request("tools/list", {})
            if tools_result and "tools" in tools_result:
                for t in tools_result["tools"]:
                    self._tools.append(
                        MCPToolInfo(
                            name=t["name"],
                            description=t.get("description", ""),
                            input_schema=t.get(
                                "inputSchema", {"type": "object", "properties": {}}
                            ),
                            server_name=self.config.name,
                        )
                    )

            self._initialized = True
            self._emit(
                "success",
                f"Connected to '{self.config.name}' with {len(self._tools)} tools",
            )
            return True
        except Exception as e:
            self._emit("error", f"Initialization error: {e}")
            return False

    def is_connected(self) -> bool:
        """Check if the MCP server is still connected."""
        if not self._initialized:
            return False
        if not self._process or self._process.returncode is not None:
            return False
        if not self._writer or not self._reader:
            return False
        return True

    async def reconnect(self) -> bool:
        """Disconnect and reconnect to the MCP server."""
        self._emit("warning", f"Attempting to reconnect to '{self.config.name}'...")
        await self.disconnect()
        # Clear tools since they'll be re-fetched on connect
        self._tools = []
        success = await self.connect()
        if success:
            self._emit("success", f"Reconnected to '{self.config.name}'")
        else:
            self._emit("error", f"Failed to reconnect to '{self.config.name}'")
        return success

    async def disconnect(self):
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            finally:
                self._receive_task = None

        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()

        writer = self._writer
        process = self._process

        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            wait_closed = getattr(writer, "wait_closed", None)
            if callable(wait_closed):
                try:
                    await asyncio.wait_for(wait_closed(), timeout=1.0)
                except Exception:
                    pass

        if process:
            try:
                if process.returncode is None:
                    process.terminate()
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except Exception:
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except Exception:
                    pass

        self._process = None
        self._reader = None
        self._writer = None
        self._initialized = False
        self._negotiated_protocol_version = ""

    def _get_tool_call_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._tool_call_lock is None or self._tool_call_lock_loop is not loop:
            self._tool_call_lock = asyncio.Lock()
            self._tool_call_lock_loop = loop
        return self._tool_call_lock

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        _retry: bool = False,
        *,
        lifecycle_context: dict[str, Any] | None = None,
    ) -> str:
        async with self._get_tool_call_lock():
            return await self._call_tool_locked(
                name,
                arguments,
                _retry=_retry,
                lifecycle_context=lifecycle_context,
            )

    async def _call_tool_locked(
        self,
        name: str,
        arguments: dict,
        _retry: bool = False,
        *,
        lifecycle_context: dict[str, Any] | None = None,
    ) -> str:
        if not self._initialized:
            # Try reconnect once if not initialized
            if not _retry:
                if await self.reconnect():
                    return await self._call_tool_locked(
                        name,
                        arguments,
                        _retry=True,
                        lifecycle_context=lifecycle_context,
                    )
            return "Error: MCP client not connected"

        # Check if process is still alive
        if not self.is_connected():
            if not _retry:
                if await self.reconnect():
                    return await self._call_tool_locked(
                        name,
                        arguments,
                        _retry=True,
                        lifecycle_context=lifecycle_context,
                    )
            return "Error: MCP server connection lost"

        try:
            active_tool_call = _ActiveMCPToolCall(
                name=name,
                arguments=dict(arguments or {}),
                lifecycle_context=dict(lifecycle_context or {}),
            )
            previous_tool_call = self._active_tool_call
            previous_tool_name = self._active_tool_name
            previous_tool_arguments = self._active_tool_arguments
            previous_lifecycle_context = self._active_lifecycle_context
            self._active_tool_call = active_tool_call
            self._active_tool_name = active_tool_call.name
            self._active_tool_arguments = active_tool_call.arguments
            self._active_lifecycle_context = active_tool_call.lifecycle_context
            try:
                result = await self._request(
                    "tools/call",
                    {
                        "name": active_tool_call.name,
                        "arguments": active_tool_call.arguments,
                    },
                )
            finally:
                self._active_tool_call = previous_tool_call
                self._active_tool_name = previous_tool_name
                self._active_tool_arguments = previous_tool_arguments
                self._active_lifecycle_context = previous_lifecycle_context

            if not result:
                # No response - might be connection issue, try reconnect
                if not _retry:
                    if await self.reconnect():
                        return await self._call_tool_locked(
                            name,
                            arguments,
                            _retry=True,
                            lifecycle_context=lifecycle_context,
                        )
                return "Error: No response from MCP server"

            content = result.get("content", [])
            if not content:
                return "(no output)"

            text_parts = []
            for item in content:
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "resource":
                    resource = item.get("resource", {})
                    text_parts.append(f"[Resource: {resource.get('uri', 'unknown')}]")
                elif item.get("type") == "image":
                    mime_type = item.get("mimeType", "unknown")
                    data = item.get("data", "")
                    text_parts.append(f"[Image: {mime_type}, {len(data)} chars base64]")
                elif item.get("type") == "audio":
                    mime_type = item.get("mimeType", "unknown")
                    data = item.get("data", "")
                    text_parts.append(f"[Audio: {mime_type}, {len(data)} chars base64]")

            result_text = "\n".join(text_parts)
            if result.get("isError"):
                return f"Error: {result_text}"
            return result_text or "(no output)"
        except Exception as e:
            # Exception during call - try reconnect once
            if not _retry:
                if await self.reconnect():
                    return await self._call_tool_locked(
                        name,
                        arguments,
                        _retry=True,
                        lifecycle_context=lifecycle_context,
                    )
            return f"Error calling MCP tool: {e}"

    async def _request(self, method: str, params: dict) -> dict | None:
        if not self._writer or not self._reader:
            return None

        self._request_id += 1
        request_id = self._request_id
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending_requests[request_id] = future

        try:
            line = json.dumps(message) + "\n"
            self._writer.write(line.encode())
            await self._writer.drain()
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            self._emit("error", f"Send error: {e}")
            return None

        try:
            timeout = (
                MCP_TOOL_CALL_REQUEST_TIMEOUT_SEC
                if method == "tools/call"
                else MCP_REQUEST_TIMEOUT_SEC
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            self._emit("warning", f"Request timeout: {method}")
            return None

    async def _notify(self, method: str, params: dict):
        if not self._writer:
            return

        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        try:
            line = json.dumps(message) + "\n"
            self._writer.write(line.encode())
            await self._writer.drain()
        except Exception as e:
            self._emit("error", f"Notify error: {e}")

    async def _receive_loop(self):
        if not self._reader:
            return

        buffer = b""
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    break
                buffer += chunk

                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue

                    try:
                        message = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue

                    if "id" in message and message["id"] in self._pending_requests:
                        future = self._pending_requests.pop(message["id"])
                        if not future.done():
                            if "error" in message:
                                future.set_result(None)
                            else:
                                future.set_result(message.get("result"))
                        continue

                    if await self._handle_server_request(message):
                        continue

                    if message.get("method") == "notifications/message":
                        params = message.get("params", {})
                        level = params.get("level", "info")
                        data = params.get("data", "")
                        if level in ("error", "warning"):
                            self._emit(level, f"[{self.config.name}] {data}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._emit("error", f"Receive error: {e}")

    async def _handle_server_request(self, message: dict[str, Any]) -> bool:
        if "id" not in message or "method" not in message:
            return False
        request_id = message.get("id")
        method = str(message.get("method") or "")
        if method == "elicitation/create":
            result = await self._handle_elicitation_create(message)
            await self._send_jsonrpc_response(request_id, result=result)
            return True
        await self._send_jsonrpc_response(
            request_id,
            error={"code": -32601, "message": f"Unsupported MCP client request: {method}"},
        )
        return True

    async def _handle_elicitation_create(self, message: dict[str, Any]) -> dict[str, Any]:
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        request_payload = self._elicitation_request_payload(message, params)
        diagnostics: list[dict[str, Any]] = []
        self._emit_elicitation_lifecycle(
            "Elicitation",
            request_payload,
            message=str(request_payload.get("message") or "MCP elicitation requested."),
        )
        mode = str(request_payload.get("mode") or "form").strip().lower() or "form"
        if mode != "form":
            diagnostics.append(
                {
                    "code": _MCP_UNSUPPORTED_ELICITATION_MODE_REASON,
                    "message": f"Unsupported MCP elicitation mode: {mode}",
                    "severity": "warning",
                }
            )
            result = self._normalize_elicitation_result(
                {
                    "action": "decline",
                    "content": {},
                    "reason": _MCP_UNSUPPORTED_ELICITATION_MODE_REASON,
                },
                diagnostics,
            )
            self._emit_elicitation_lifecycle(
                "ElicitationResult",
                {
                    **request_payload,
                    "result_action": result["action"],
                    "result_content": result["content"],
                    "reason": result["reason"],
                },
                diagnostics=diagnostics,
                message=f"MCP elicitation {result['action']}.",
            )
            return result
        handler_result: dict[str, Any]
        if self._elicitation_handler is None:
            diagnostics.append(
                {
                    "code": "mcp_elicitation_unhandled",
                    "message": "MCP elicitation has no unified input handler.",
                    "severity": "warning",
                }
            )
            handler_result = {
                "action": "decline",
                "content": {},
                "reason": "mcp_elicitation_unhandled",
            }
        else:
            try:
                raw_result = self._elicitation_handler(dict(request_payload))
                if inspect.isawaitable(raw_result):
                    raw_result = await raw_result
                handler_result = (
                    dict(raw_result) if isinstance(raw_result, dict) else {}
                )
            except Exception as exc:
                diagnostics.append(
                    {
                        "code": "mcp_elicitation_handler_error",
                        "message": str(exc),
                        "severity": "error",
                    }
                )
                handler_result = {
                    "action": "decline",
                    "content": {},
                    "reason": "mcp_elicitation_handler_error",
                }
        result = self._normalize_elicitation_result(handler_result, diagnostics)
        self._emit_elicitation_lifecycle(
            "ElicitationResult",
            {
                **request_payload,
                "result_action": result["action"],
                "result_content": result["content"],
                **({"reason": result["reason"]} if result.get("reason") else {}),
            },
            diagnostics=diagnostics,
            message=f"MCP elicitation {result['action']}.",
        )
        return result

    def _elicitation_request_payload(
        self,
        message: dict[str, Any],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        input_schema = params.get("requestedSchema")
        if not isinstance(input_schema, dict):
            input_schema = params.get("inputSchema")
        if not isinstance(input_schema, dict):
            input_schema = {}
        mode = str(params.get("mode") or "form").strip().lower() or "form"
        active_tool_call = self._active_tool_call
        context = dict(
            active_tool_call.lifecycle_context
            if active_tool_call is not None
            else self._active_lifecycle_context or {}
        )
        tool_arguments = dict(
            active_tool_call.arguments
            if active_tool_call is not None
            else self._active_tool_arguments or {}
        )
        active_tool_name = (
            active_tool_call.name
            if active_tool_call is not None
            else self._active_tool_name
        )
        tool_name = (
            str(context.get("tool_name") or "")
            or str(params.get("toolName") or "")
            or active_tool_name
        )
        return {
            "mcp_server": str(getattr(self.config, "name", "") or ""),
            "tool_name": tool_name,
            "tool_arguments": tool_arguments,
            "request_id": str(message.get("id") or ""),
            "mode": mode,
            "message": str(params.get("message") or ""),
            "input_schema": dict(input_schema),
            "session_run_id": str(context.get("session_run_id") or ""),
            "agent_run_id": str(context.get("agent_run_id") or ""),
            "branch_binding_id": str(context.get("branch_binding_id") or ""),
            "turn_id": str(context.get("turn_id") or ""),
            "tool_call_id": str(context.get("tool_call_id") or ""),
        }

    @staticmethod
    def _normalize_elicitation_result(
        result: dict[str, Any],
        diagnostics: list[dict[str, Any]],
    ) -> dict[str, Any]:
        action = str(result.get("action") or "decline").lower()
        if action not in {"accept", "decline", "cancel"}:
            diagnostics.append(
                {
                    "code": "mcp_elicitation_invalid_result_action",
                    "message": f"Unsupported MCP elicitation action: {action}",
                    "severity": "error",
                }
            )
            action = "decline"
        content = result.get("content")
        normalized = {
            "action": action,
            "content": dict(content) if isinstance(content, dict) else {},
        }
        reason = str(result.get("reason") or "").strip()
        if reason:
            normalized["reason"] = reason
        return normalized

    def _emit_elicitation_lifecycle(
        self,
        event_name: str,
        payload: dict[str, Any],
        *,
        diagnostics: list[dict[str, Any]] | None = None,
        message: str,
    ) -> None:
        level_value = "warning" if diagnostics else "info"
        data = {
            "event_type": "lifecycle_hook",
            "phase": "request" if event_name == "Elicitation" else "result",
            "event_name": event_name,
            "placement": "server",
            "session_run_id": str(payload.get("session_run_id") or ""),
            "agent_run_id": str(payload.get("agent_run_id") or ""),
            "branch_binding_id": str(payload.get("branch_binding_id") or ""),
            "turn_id": str(payload.get("turn_id") or ""),
            "tool_call_id": str(payload.get("tool_call_id") or ""),
            "tool_name": str(payload.get("tool_name") or ""),
            "mcp_server": str(payload.get("mcp_server") or ""),
            "trigger_source": "mcp",
            "source": "mcp_server",
            "decision": "none",
            "continue_flow": True,
            "diagnostics": list(diagnostics or []),
            "level": level_value,
            "title": event_name,
            "message": message,
            "payload": dict(payload),
        }
        active_tool_call = self._active_tool_call
        lifecycle_context = (
            active_tool_call.lifecycle_context
            if active_tool_call is not None
            else self._active_lifecycle_context
        )
        emitter = lifecycle_context.get("_agent_lifecycle_event_emitter")
        if callable(emitter):
            try:
                emitter(dict(data))
            except Exception:
                pass
        if not self._ui_bus:
            return
        try:
            from reuleauxcoder.interfaces.events import (
                UIEvent,
                UIEventKind,
                UIEventLevel,
            )

            level = UIEventLevel.WARNING if diagnostics else UIEventLevel.INFO
            data["level"] = level.value
            self._ui_bus.emit(
                UIEvent(
                    message=message,
                    level=level,
                    kind=UIEventKind.AGENT,
                    data=data,
                )
            )
        except Exception:
            return

    async def _send_jsonrpc_response(
        self,
        request_id: Any,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if not self._writer:
            return
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result or {}
        try:
            line = json.dumps(message) + "\n"
            self._writer.write(line.encode())
            await self._writer.drain()
        except Exception as e:
            self._emit("error", f"Response error: {e}")

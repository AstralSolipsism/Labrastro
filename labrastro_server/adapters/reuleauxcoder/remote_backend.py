"""Remote relay tool backend implementation."""

from __future__ import annotations

import json
import uuid
from typing import Any

from labrastro_server.relay.errors import (
    PeerNotFoundError,
    RemoteExecError,
)
from labrastro_server.interfaces.http.remote.protocol import (
    ExecToolRequest,
    ToolMutationPreviewState,
    ToolPreviewRequest,
    ToolPreviewResult,
    ToolStreamChunk,
)
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.domain.files import FileChange, FileMutationResult
from reuleauxcoder.extensions.tools.backend import ExecutionContext, ToolBackend
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


class RemoteRelayToolBackend(ToolBackend):
    """Backend that forwards tool execution to a remote peer via the relay server."""

    backend_id = "remote_relay"

    def __init__(
        self,
        relay_server: RelayServer,
        context: ExecutionContext | None = None,
        ui_bus: UIEventBus | None = None,
    ):
        super().__init__(context or ExecutionContext(execution_target="remote_peer"))
        self.relay_server = relay_server
        self.ui_bus = ui_bus
        self._pending_preview_states: dict[str, ToolMutationPreviewState] = {}
        self._approved_preview_states: dict[str, ToolMutationPreviewState] = {}
        self.execution_target = "remote_peer"
        self.path_space = "remote_peer_workspace"
        self.workspace_id = str(getattr(self.context, "workspace_root", "") or "")

    def exec_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """Execute a tool on the remote peer and return the text result.

        If no peer is explicitly selected, picks the default online peer.
        """
        peer_id = self.context.peer_id
        peer = None
        if peer_id is None:
            peer = self.relay_server.registry.pick_default_peer()
            if peer is None:
                return "Error: no remote peer is currently connected"
            peer_id = peer.peer_id
        elif tool_name == "lsp":
            peer = self.relay_server.registry.get(peer_id)

        if tool_name == "lsp":
            if peer is None:
                peer = self.relay_server.registry.get(peer_id)
            if peer is None:
                return f"Error: peer '{peer_id}' is not online"
            if "lsp" not in peer.features:
                return f"Error: peer '{peer_id}' does not advertise LSP support"

        timeout = None
        if tool_name == "shell":
            timeout = args.get("timeout", 120)
        elif tool_name == "draft_document_commit":
            timeout = 120
        else:
            timeout = 30

        tool_call_id = self._resolve_tool_call_id(tool_name)
        request = ExecToolRequest(
            tool_name=tool_name,
            args=args,
            cwd=self.context.cwd,
            timeout_sec=timeout,
            tool_call_id=tool_call_id,
            permission_context=dict(self.context.permission_context or {}),
            expected_state=self._approved_preview_states.pop(
                self._request_key(tool_name, args), None
            ),
        )

        stream_handler = self._build_stream_handler(tool_name, tool_call_id)

        try:
            result = self.relay_server.send_exec_request(
                peer_id=peer_id,
                request=request,
                timeout_sec=timeout,
                stream_handler=stream_handler,
            )
        except PeerNotFoundError:
            return f"Error: peer '{peer_id}' is not online"
        except RemoteExecError as e:
            return f"Error [{e.code}]: {e.message}"
        except Exception as e:
            return f"Error executing {tool_name} remotely: {e}"

        if result.ok:
            return result.result
        error_msg = result.error_message or "unknown remote error"
        return f"Error [{result.error_code or 'REMOTE_TOOL_ERROR'}]: {error_msg}"

    def preview_tool(self, tool_name: str, args: dict[str, Any]) -> ToolPreviewResult:
        """Request a non-mutating tool preview from the selected peer."""
        peer_id = self.context.peer_id
        if peer_id is None:
            peer = self.relay_server.registry.pick_default_peer()
            if peer is None:
                return ToolPreviewResult(
                    ok=False,
                    error_code="PEER_NOT_FOUND",
                    error_message="No online peer available for tool preview",
                )
            peer_id = peer.peer_id

        request = ToolPreviewRequest(
            tool_name=tool_name,
            args=args,
            cwd=self.context.cwd,
            timeout_sec=30,
        )
        try:
            return self.relay_server.send_preview_request(
                peer_id=peer_id,
                request=request,
                timeout_sec=30,
            )
        except Exception as exc:
            return ToolPreviewResult(
                ok=False,
                error_code="PREVIEW_FAILED",
                error_message=str(exc),
            )

    def remember_approved_preview(
        self,
        tool_name: str,
        args: dict[str, Any],
        state: ToolMutationPreviewState | None,
    ) -> None:
        if state is not None and not state.is_empty():
            self._approved_preview_states[self._request_key(tool_name, args)] = state

    def preview_text_patch(self, patch: str) -> FileMutationResult:
        return self._preview_mutation_tool("apply_patch", {"patch": patch})

    def apply_text_patch(self, patch: str) -> FileMutationResult:
        return self._exec_mutation_tool("apply_patch", {"patch": patch})

    def preview_document_commit(
        self,
        target_path: str,
        content: str,
    ) -> FileMutationResult:
        args = {"target_path": target_path, "content": content}
        key = self._request_key("draft_document_commit", args)
        self._pending_preview_states.pop(key, None)
        result, state = self._preview_mutation_tool_with_state(
            "draft_document_commit",
            args,
        )
        if state is not None and not state.is_empty():
            self._pending_preview_states[key] = state
        return result

    def commit_document(
        self,
        target_path: str,
        content: str,
    ) -> FileMutationResult:
        args = {"target_path": target_path, "content": content}
        key = self._request_key("draft_document_commit", args)
        state = self._pending_preview_states.pop(key, None)
        if state is not None and not state.is_empty():
            self._approved_preview_states[key] = state
        return self._exec_mutation_tool("draft_document_commit", args)

    def _preview_mutation_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> FileMutationResult:
        result, _state = self._preview_mutation_tool_with_state(tool_name, args)
        return result

    def _preview_mutation_tool_with_state(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> tuple[FileMutationResult, ToolMutationPreviewState | None]:
        preview = self.preview_tool(tool_name, args)
        if not preview.ok:
            message = preview.error_message or preview.error_code or "remote preview failed"
            return (
                FileMutationResult(
                    status="failed",
                    message=f"Error: {message}",
                    error=message,
                ),
                None,
            )
        return (
            FileMutationResult(
                status="in_progress",
                changes=_changes_from_preview(preview),
                diff=preview.diff,
                message=f"Preview {tool_name}",
                plan_id=str(preview.meta.get("plan_id") or "") or None,
                plan_hash=str(preview.meta.get("plan_hash") or "") or None,
            ),
            ToolMutationPreviewState.from_preview(preview),
        )

    def _exec_mutation_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> FileMutationResult:
        result = self.exec_tool(tool_name, args)
        if result.startswith("Error"):
            return FileMutationResult(
                status="failed",
                message=result,
                error=result,
            )
        return FileMutationResult(status="completed", message=result)

    def _build_stream_handler(self, tool_name: str, tool_call_id: str | None = None):
        remote_stream_handler = getattr(self.context, "remote_stream_handler", None)
        if tool_name != "shell" and remote_stream_handler is None:
            return None
        if (
            tool_name == "shell"
            and self.ui_bus is None
            and not callable(remote_stream_handler)
        ):
            return None

        def _handle(chunk: ToolStreamChunk) -> None:
            if not chunk.data:
                return
            if callable(remote_stream_handler):
                try:
                    remote_stream_handler(
                        tool_name,
                        chunk,
                        chunk.tool_call_id or tool_call_id,
                    )
                except Exception:
                    pass
            if tool_name == "shell" and self.ui_bus is not None:
                self.ui_bus.info(
                    "",
                    kind=UIEventKind.REMOTE,
                    remote_stream=True,
                    tool_name=tool_name,
                    stream=chunk.chunk_type,
                    chunk=chunk.data,
                )

        return _handle

    def _resolve_tool_call_id(self, tool_name: str) -> str:
        current = self.context.current_tool_call_id
        if isinstance(current, str) and current.strip():
            return current.strip()
        return f"manual-{tool_name}-{uuid.uuid4().hex}"

    def _request_key(self, tool_name: str, args: dict[str, Any]) -> str:
        return json.dumps(
            {
                "tool_name": tool_name,
                "args": args,
                "cwd": self.context.cwd,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )


def _changes_from_preview(preview: ToolPreviewResult) -> tuple[FileChange, ...]:
    changes: list[FileChange] = []
    for index, section in enumerate(preview.sections):
        if not isinstance(section, dict):
            continue
        path = str(section.get("path") or preview.resolved_path or f"change-{index + 1}")
        kind = str(section.get("change_kind") or "update")
        if kind not in {"add", "update", "delete", "move"}:
            kind = "update"
        changes.append(
            FileChange(
                path=path,
                kind=kind,  # type: ignore[arg-type]
                diff=str(section.get("content") or ""),
                move_path=(
                    str(section["move_path"])
                    if section.get("move_path") is not None
                    else None
                ),
            )
        )
    if changes:
        return tuple(changes)
    if preview.diff:
        return (
            FileChange(
                path=str(preview.resolved_path or "workspace"),
                kind="update",
                diff=preview.diff,
            ),
        )
    return ()

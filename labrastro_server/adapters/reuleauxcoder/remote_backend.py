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
    ToolPreviewRequest,
    ToolPreviewResult,
    ToolStreamChunk,
)
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.domain.files import FileChange, FileMutationResult
from reuleauxcoder.extensions.tools.backend import ExecutionContext, ToolBackend
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


_MUTATION_TOOLS_REQUIRING_SAVE_CANDIDATE = {
    "apply_patch",
    "draft_document_commit",
}


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
        self._pending_save_candidates: dict[str, dict[str, Any]] = {}
        self._approved_save_candidates: dict[str, dict[str, Any]] = {}
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
        request_key = self._request_key(tool_name, args)
        approved_save_candidate = self._approved_save_candidates.pop(request_key, None)
        missing_candidate_fields = _missing_required_save_candidate_fields(
            tool_name,
            approved_save_candidate,
        )
        if missing_candidate_fields:
            return (
                "Error [APPROVED_SAVE_CANDIDATE_REQUIRED]: "
                "remote mutation execute missing required approved_save_candidate fields: "
                + ", ".join(missing_candidate_fields)
            )
        preview_identity = _preview_identity_from_candidate(approved_save_candidate)
        request = ExecToolRequest(
            tool_name=tool_name,
            args=args,
            cwd=self.context.cwd,
            timeout_sec=timeout,
            tool_call_id=tool_call_id,
            permission_context=dict(self.context.permission_context or {}),
            preview_identity=preview_identity,
            approved_save_candidate=approved_save_candidate or {},
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

    def remember_approved_candidate(
        self,
        tool_name: str,
        args: dict[str, Any],
        candidate: dict[str, Any] | None,
    ) -> None:
        self.bind_save_candidate(tool_name, args, candidate)

    def bind_save_candidate(
        self,
        tool_name: str,
        args: dict[str, Any],
        candidate: dict[str, Any] | None,
    ) -> None:
        if isinstance(candidate, dict) and candidate:
            self._approved_save_candidates[self._request_key(tool_name, args)] = dict(candidate)

    def preview_text_patch(self, patch: str) -> FileMutationResult:
        args = {"patch": patch}
        key = self._request_key("apply_patch", args)
        self._pending_save_candidates.pop(key, None)
        result, candidate = self._preview_mutation_tool_with_candidate("apply_patch", args)
        if candidate:
            self._pending_save_candidates[key] = candidate
        return result

    def apply_text_patch(self, patch: str) -> FileMutationResult:
        args = {"patch": patch}
        key = self._request_key("apply_patch", args)
        candidate = self._pending_save_candidates.pop(key, None) or self._approved_save_candidates.pop(key, None)
        if candidate is None:
            preview, candidate = self._preview_mutation_tool_with_candidate(
                "apply_patch",
                args,
            )
            if preview.status == "failed" or preview.error:
                return preview
        return self.save_candidate(candidate or {})

    def preview_document_commit(
        self,
        target_path: str,
        content: str,
    ) -> FileMutationResult:
        args = {"target_path": target_path, "content": content}
        key = self._request_key("draft_document_commit", args)
        self._pending_save_candidates.pop(key, None)
        result, candidate = self._preview_mutation_tool_with_candidate(
            "draft_document_commit",
            args,
        )
        if candidate:
            self._pending_save_candidates[key] = candidate
        return result

    def commit_document(
        self,
        target_path: str,
        content: str,
    ) -> FileMutationResult:
        args = {"target_path": target_path, "content": content}
        key = self._request_key("draft_document_commit", args)
        candidate = self._pending_save_candidates.pop(key, None) or self._approved_save_candidates.pop(key, None)
        if candidate is None:
            preview, candidate = self._preview_mutation_tool_with_candidate(
                "draft_document_commit",
                args,
            )
            if preview.status == "failed" or preview.error:
                return preview
        return self.save_candidate(candidate or {})

    def _preview_mutation_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> FileMutationResult:
        result, _candidate = self._preview_mutation_tool_with_candidate(tool_name, args)
        return result

    def _preview_mutation_tool_with_candidate(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> tuple[FileMutationResult, dict[str, Any] | None]:
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
        candidate = _approved_save_candidate_from_preview(preview)
        preview_identity = _preview_identity_from_candidate(candidate)
        missing_candidate_fields = _missing_required_save_candidate_fields(tool_name, candidate)
        if missing_candidate_fields:
            message = (
                "remote mutation preview missing required approved_save_candidate fields: "
                + ", ".join(missing_candidate_fields)
            )
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
                candidate_hash=str(preview_identity.get("candidate_hash") or "") or None,
                preview_identity=preview_identity,
                approved_save_candidate=candidate or {},
            ),
            candidate,
        )

    def _exec_mutation_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> FileMutationResult:
        key = self._request_key(tool_name, args)
        candidate = self._approved_save_candidates.get(key)
        missing_candidate_fields = _missing_required_save_candidate_fields(tool_name, candidate)
        if missing_candidate_fields:
            message = (
                "remote mutation execute missing required approved_save_candidate fields: "
                + ", ".join(missing_candidate_fields)
            )
            return FileMutationResult(
                status="failed",
                message=f"Error: {message}",
                error=message,
            )
        result = self.exec_tool(tool_name, args)
        if result.startswith("Error"):
            return FileMutationResult(
                status="failed",
                message=result,
                error=result,
            )
        return FileMutationResult(status="completed", message=result)

    def save_candidate(self, candidate: dict[str, Any]) -> FileMutationResult:
        tool_name = str((candidate or {}).get("tool_name") or "")
        if tool_name not in _MUTATION_TOOLS_REQUIRING_SAVE_CANDIDATE:
            message = "approved_save_candidate tool_name is required"
            return FileMutationResult(
                status="failed",
                message=f"Error: {message}",
                error=message,
            )
        missing_fields = _missing_required_save_candidate_fields(
            tool_name,
            candidate,
        )
        if missing_fields:
            message = (
                "remote mutation execute missing required approved_save_candidate fields: "
                + ", ".join(missing_fields)
            )
            return FileMutationResult(
                status="failed",
                message=f"Error: {message}",
                error=message,
            )
        timeout = 120 if tool_name == "draft_document_commit" else 30
        request = ExecToolRequest(
            tool_name=tool_name,
            args={},
            cwd=self.context.cwd,
            timeout_sec=timeout,
            tool_call_id=self._resolve_tool_call_id(tool_name),
            permission_context=dict(self.context.permission_context or {}),
            preview_identity=_preview_identity_from_candidate(candidate),
            approved_save_candidate=dict(candidate),
        )
        peer_id = self.context.peer_id
        if peer_id is None:
            peer = self.relay_server.registry.pick_default_peer()
            if peer is None:
                return FileMutationResult(
                    status="failed",
                    message="Error: no remote peer is currently connected",
                    error="no remote peer is currently connected",
                )
            peer_id = peer.peer_id
        try:
            result = self.relay_server.send_exec_request(
                peer_id=peer_id,
                request=request,
                timeout_sec=timeout,
                stream_handler=self._build_stream_handler(tool_name, request.tool_call_id),
            )
        except PeerNotFoundError:
            message = f"peer '{peer_id}' is not online"
            return FileMutationResult(status="failed", message=f"Error: {message}", error=message)
        except RemoteExecError as exc:
            message = f"[{exc.code}] {exc.message}"
            return FileMutationResult(status="failed", message=f"Error {message}", error=message)
        except Exception as exc:
            message = f"executing {tool_name} remotely: {exc}"
            return FileMutationResult(status="failed", message=f"Error {message}", error=message)
        if not result.ok:
            message = result.error_message or "unknown remote error"
            return FileMutationResult(
                status="failed",
                message=f"Error [{result.error_code or 'REMOTE_TOOL_ERROR'}]: {message}",
                error=message,
            )
        return FileMutationResult(
            status="completed",
            message=result.result,
            preview_identity=_preview_identity_from_candidate(candidate),
            approved_save_candidate=dict(candidate),
        )

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


def _approved_save_candidate_from_preview(
    preview: ToolPreviewResult,
) -> dict[str, Any] | None:
    raw = preview.meta.get("approved_save_candidate")
    if not isinstance(raw, dict):
        raw = preview.meta.get("save_candidate")
    return dict(raw) if isinstance(raw, dict) and raw else None


def _preview_identity_from_candidate(
    candidate: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        return {}
    identity = candidate.get("preview_identity")
    return dict(identity) if isinstance(identity, dict) and identity else {}


def _missing_required_save_candidate_fields(
    tool_name: str,
    candidate: dict[str, Any] | None,
) -> list[str]:
    if tool_name not in _MUTATION_TOOLS_REQUIRING_SAVE_CANDIDATE:
        return []
    if not isinstance(candidate, dict) or not candidate:
        return ["approved_save_candidate", "preview_identity", "operations"]
    missing: list[str] = []
    preview_identity = candidate.get("preview_identity")
    if not isinstance(preview_identity, dict) or not preview_identity:
        missing.append("preview_identity")
    else:
        for key in (
            "plan_id",
            "candidate_hash",
            "tool_name",
            "workspace_id",
            "execution_target",
            "path_space",
            "args_hash",
        ):
            if not str(preview_identity.get(key) or "").strip():
                missing.append(f"preview_identity.{key}")
    operations = candidate.get("operations")
    if not isinstance(operations, list) or not operations:
        missing.append("operations")
    if str(candidate.get("tool_name") or "").strip() != tool_name:
        missing.append("tool_name")
    return missing

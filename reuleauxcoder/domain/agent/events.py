"""Agent events - event types for telemetry and hooks."""

import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional, Any

from reuleauxcoder.domain.agent.document_draft_text import draft_text_units
from enum import Enum

from reuleauxcoder.domain.agent.tool_diagnostics import (
    ToolDiagnosticKind,
    ToolDiagnosticStage,
    tool_diagnostic_from_failure,
)


class AgentEventType(Enum):
    """Types of agent events."""

    SESSION_RUN_START = "session_run_start"
    SESSION_RUN_END = "session_run_end"
    SESSION_RUN_EVENT = "session_run_event"
    STREAM_TOKEN = "stream_token"
    REASONING_TOKEN = "reasoning_token"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_ARGUMENTS_COMPLETE = "tool_arguments_complete"
    TOOL_ARGUMENTS_VALID = "tool_arguments_valid"
    TOOL_ARGUMENTS_INVALID = "tool_arguments_invalid"
    MUTATION_PREVIEWING = "mutation_previewing"
    MUTATION_PREVIEW_READY = "mutation_preview_ready"
    MUTATION_PREVIEW_FAILED = "mutation_preview_failed"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TOOL_CALL_PROTOCOL_ERROR = "tool_call_protocol_error"
    FILE_CHANGE_STARTED = "file_change_started"
    FILE_CHANGE_PATCH_UPDATED = "file_change_patch_updated"
    FILE_CHANGE_APPROVAL_REQUESTED = "file_change_approval_requested"
    FILE_CHANGE_APPROVAL_RESOLVED = "file_change_approval_resolved"
    FILE_CHANGE_COMPLETED = "file_change_completed"
    TURN_DIFF_UPDATED = "turn_diff_updated"
    DOCUMENT_DRAFT_STARTED = "document_draft_started"
    DOCUMENT_DRAFT_PREVIEW_CHUNK = "document_draft_preview_chunk"
    DOCUMENT_DRAFT_PROGRESS = "document_draft_progress"
    DOCUMENT_DRAFT_SNAPSHOT = "document_draft_snapshot"
    DOCUMENT_DRAFT_COMMIT_REQUESTED = "document_draft_commit_requested"
    DOCUMENT_DRAFT_COMMITTED = "document_draft_committed"
    DOCUMENT_DRAFT_FAILED = "document_draft_failed"
    DOCUMENT_DRAFT_CANCELLED = "document_draft_cancelled"
    DRAFT_BODY_STALLED = "draft_body_stalled"
    DRAFT_INTERRUPTED_RECOVERABLE = "draft_interrupted_recoverable"
    LIFECYCLE_HOOK = "lifecycle_hook"
    PROVIDER_STREAM_INTERRUPTED = "provider_stream_interrupted"
    PROVIDER_STREAM_RECOVERING = "provider_stream_recovering"
    PROVIDER_STREAM_RECOVERED = "provider_stream_recovered"
    SESSION_RUN_INTERRUPTED = "session_run_interrupted"
    AGENT_RELATION_COMPLETED = "agent_relation_completed"
    # User-visible context compression lifecycle events are currently emitted via
    # UIEventKind.CONTEXT so CLI, remote relay, and webview can share one UI path.
    COMPRESSION_START = "compression_start"
    COMPRESSION_END = "compression_end"
    USAGE_UPDATE = "usage_update"
    RUNTIME_STATUS = "runtime_status"
    ERROR = "error"


class ToolFailureKind(str, Enum):
    TOOL_RESULT_ERROR = "tool_result_error"
    APPROVAL_DENIED = "approval_denied"
    TOOL_PROTOCOL_ERROR = "tool_protocol_error"
    CHAT_TERMINAL_ERROR = "chat_terminal_error"


@dataclass
class AgentEvent:
    """An event emitted by the agent during execution."""

    event_type: AgentEventType
    timestamp: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)

    # Tool call specific fields
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_args: Optional[dict] = None
    tool_result: Optional[str] = None

    # Error specific fields
    error_message: Optional[str] = None
    runtime_artifacts: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def session_run_start(cls, user_input: str) -> "AgentEvent":
        """Create a session run start event."""
        return cls(
            event_type=AgentEventType.SESSION_RUN_START,
            data={"user_input": user_input},
        )

    @classmethod
    def session_run_end(
        cls,
        response: str,
        *,
        render_response: bool = True,
        status: str | None = None,
        error: str | None = None,
        session_state: str | None = None,
    ) -> "AgentEvent":
        """Create a session run end event."""
        data = {
            "response": response,
            "render_response": render_response,
        }
        if status:
            data["status"] = status
        if error:
            data["error"] = error
        if session_state:
            data["session_state"] = session_state
        return cls(
            event_type=AgentEventType.SESSION_RUN_END,
            data=data,
        )

    @classmethod
    def session_run_event(
        cls,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> "AgentEvent":
        """Carry a whitelisted user-visible SessionRun event through AgentRun."""

        return cls(
            event_type=AgentEventType.SESSION_RUN_EVENT,
            data={
                "event_type": str(event_type or ""),
                "payload": dict(payload or {}),
            },
        )

    @classmethod
    def tool_call_start(
        cls,
        tool_name: str,
        tool_args: dict,
        *,
        tool_call_id: str | None = None,
        tool_source: str | None = None,
        index: int | None = None,
        tool_metadata: dict[str, Any] | None = None,
    ) -> "AgentEvent":
        """Create a tool call start event."""
        data = {
            **({"tool_source": tool_source} if tool_source else {}),
            **({"index": index} if index is not None else {}),
            **(dict(tool_metadata) if tool_metadata else {}),
        }
        return cls(
            event_type=AgentEventType.TOOL_CALL_START,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_args=tool_args,
            data=data,
        )

    @classmethod
    def tool_call_delta(
        cls,
        *,
        index: int,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        arguments_delta: str = "",
        arguments_preview: str = "",
        tool_source: str | None = None,
    ) -> "AgentEvent":
        """Create a UI-only event for a streamed tool-call draft."""
        name = tool_name or None
        payload = {
            "index": index,
            "tool_call_id": tool_call_id or "",
            "tool_name": tool_name or "",
            "arguments_delta": arguments_delta,
            "arguments_preview": arguments_preview,
            "status": "preparing",
            **({"tool_source": tool_source} if tool_source else {}),
        }
        return cls(
            event_type=AgentEventType.TOOL_CALL_DELTA,
            tool_name=name,
            tool_call_id=tool_call_id or None,
            data=payload,
        )

    @classmethod
    def tool_arguments_complete(
        cls,
        tool_name: str,
        *,
        tool_call_id: str | None = None,
        index: int | None = None,
        tool_source: str | None = None,
    ) -> "AgentEvent":
        payload = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id or "",
            **({"index": index} if index is not None else {}),
            "status": "complete",
            **({"tool_source": tool_source} if tool_source else {}),
        }
        return cls(
            event_type=AgentEventType.TOOL_ARGUMENTS_COMPLETE,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            data=payload,
        )

    @classmethod
    def tool_arguments_valid(
        cls,
        tool_name: str,
        *,
        tool_call_id: str | None = None,
        index: int | None = None,
        tool_source: str | None = None,
    ) -> "AgentEvent":
        payload = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id or "",
            **({"index": index} if index is not None else {}),
            "status": "valid",
            **({"tool_source": tool_source} if tool_source else {}),
        }
        return cls(
            event_type=AgentEventType.TOOL_ARGUMENTS_VALID,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            data=payload,
        )

    @classmethod
    def tool_arguments_invalid(
        cls,
        tool_name: str,
        *,
        tool_call_id: str | None = None,
        index: int | None = None,
        message: str,
        code: str | None = None,
        retry_hint: str | None = None,
        tool_source: str | None = None,
    ) -> "AgentEvent":
        payload = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id or "",
            **({"index": index} if index is not None else {}),
            "status": "invalid",
            "message": message,
            **({"code": code} if code else {}),
            **({"retry_hint": retry_hint} if retry_hint else {}),
            **({"tool_source": tool_source} if tool_source else {}),
        }
        return cls(
            event_type=AgentEventType.TOOL_ARGUMENTS_INVALID,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            data=payload,
            error_message=message,
        )

    @classmethod
    def mutation_previewing(
        cls,
        tool_name: str,
        *,
        item_id: str,
        tool_call_id: str | None = None,
        index: int | None = None,
        tool_metadata: dict[str, Any] | None = None,
    ) -> "AgentEvent":
        payload = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id or "",
            "item_id": item_id,
            **({"index": index} if index is not None else {}),
            "status": "previewing",
            **(dict(tool_metadata) if tool_metadata else {}),
        }
        return cls(
            event_type=AgentEventType.MUTATION_PREVIEWING,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            data=payload,
        )

    @classmethod
    def mutation_preview_ready(
        cls,
        tool_name: str,
        *,
        item_id: str,
        tool_call_id: str | None = None,
        changes: list[dict[str, Any]] | None = None,
        index: int | None = None,
        tool_metadata: dict[str, Any] | None = None,
    ) -> "AgentEvent":
        payload = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id or "",
            "item_id": item_id,
            "changes": list(changes or []),
            **({"index": index} if index is not None else {}),
            "status": "ready",
            **(dict(tool_metadata) if tool_metadata else {}),
        }
        return cls(
            event_type=AgentEventType.MUTATION_PREVIEW_READY,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            data=payload,
        )

    @classmethod
    def mutation_preview_failed(
        cls,
        tool_name: str,
        *,
        item_id: str,
        tool_call_id: str | None = None,
        error: str,
        failure_code: str | None = None,
        retry_hint: str | None = None,
        index: int | None = None,
        tool_metadata: dict[str, Any] | None = None,
    ) -> "AgentEvent":
        payload = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id or "",
            "item_id": item_id,
            **({"index": index} if index is not None else {}),
            "status": "failed",
            "error": error,
            **({"failure_code": failure_code} if failure_code else {}),
            **({"retry_hint": retry_hint} if retry_hint else {}),
            **(dict(tool_metadata) if tool_metadata else {}),
        }
        return cls(
            event_type=AgentEventType.MUTATION_PREVIEW_FAILED,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            data=payload,
            error_message=error,
        )

    @classmethod
    def tool_call_end(
        cls,
        tool_name: str,
        result: str,
        *,
        tool_call_id: str | None = None,
        tool_source: str | None = None,
        index: int | None = None,
        meta: dict[str, Any] | None = None,
        tool_metadata: dict[str, Any] | None = None,
    ) -> "AgentEvent":
        """Create a tool call end event."""
        return cls(
            event_type=AgentEventType.TOOL_CALL_END,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_result=result,
            data={
                **({"tool_source": tool_source} if tool_source else {}),
                **({"index": index} if index is not None else {}),
                **(dict(tool_metadata) if tool_metadata else {}),
                **(
                    {"tool_result_preview": result[:500]}
                    if len(result) > 500
                    else {}
                ),
                **({"meta": meta} if meta else {}),
            },
        )

    @classmethod
    def tool_call_protocol_error(
        cls,
        tool_name: str,
        *,
        tool_call_id: str | None = None,
        code: str,
        message: str,
    ) -> "AgentEvent":
        """Create a protocol error event for a tool call that did not return."""
        diagnostic = tool_diagnostic_from_failure(
            stage=ToolDiagnosticStage.PROTOCOL,
            kind=ToolDiagnosticKind.TOOL_PROTOCOL_ERROR,
            code=code,
            message=message,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        payload = {
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "code": code,
            "message": message,
            "failure_kind": ToolFailureKind.TOOL_PROTOCOL_ERROR.value,
            "tool_diagnostics": [diagnostic.to_dict()],
        }
        return cls(
            event_type=AgentEventType.TOOL_CALL_PROTOCOL_ERROR,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            data=payload,
            error_message=message,
        )

    @classmethod
    def file_change_started(
        cls,
        *,
        item_id: str,
        tool_call_id: str | None = None,
        changes: list[dict[str, Any]] | None = None,
        status: str = "in_progress",
        tool_metadata: dict[str, Any] | None = None,
    ) -> "AgentEvent":
        payload = {
            "item_id": item_id,
            "tool_call_id": tool_call_id or "",
            "changes": list(changes or []),
            "status": status,
            **(dict(tool_metadata) if tool_metadata else {}),
        }
        return cls(
            event_type=AgentEventType.FILE_CHANGE_STARTED,
            tool_call_id=tool_call_id or None,
            data=payload,
        )

    @classmethod
    def file_change_patch_updated(
        cls,
        *,
        item_id: str,
        tool_call_id: str | None = None,
        changes: list[dict[str, Any]] | None = None,
        patch_delta: str = "",
        patch_preview: str = "",
    ) -> "AgentEvent":
        payload = {
            "item_id": item_id,
            "tool_call_id": tool_call_id or "",
            "changes": list(changes or []),
            "patch_delta": patch_delta,
            "patch_preview": patch_preview,
            "status": "in_progress",
        }
        return cls(
            event_type=AgentEventType.FILE_CHANGE_PATCH_UPDATED,
            tool_call_id=tool_call_id or None,
            data=payload,
        )

    @classmethod
    def file_change_approval_requested(
        cls,
        *,
        item_id: str,
        approval_id: str,
        tool_call_id: str | None = None,
        reason: str = "",
        tool_metadata: dict[str, Any] | None = None,
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.FILE_CHANGE_APPROVAL_REQUESTED,
            tool_call_id=tool_call_id or None,
            data={
                "item_id": item_id,
                "approval_id": approval_id,
                "tool_call_id": tool_call_id or "",
                "reason": reason,
                "status": "awaiting_approval",
                **(dict(tool_metadata) if tool_metadata else {}),
            },
        )

    @classmethod
    def file_change_approval_resolved(
        cls,
        *,
        item_id: str,
        approval_id: str,
        decision: str,
        tool_call_id: str | None = None,
        reason: str = "",
        tool_metadata: dict[str, Any] | None = None,
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.FILE_CHANGE_APPROVAL_RESOLVED,
            tool_call_id=tool_call_id or None,
            data={
                "item_id": item_id,
                "approval_id": approval_id,
                "tool_call_id": tool_call_id or "",
                "decision": decision,
                "reason": reason,
                "status": "approved" if decision == "allow_once" else "declined",
                **(dict(tool_metadata) if tool_metadata else {}),
            },
        )

    @classmethod
    def file_change_completed(
        cls,
        *,
        item_id: str,
        tool_call_id: str | None = None,
        changes: list[dict[str, Any]] | None = None,
        status: str,
        error: str | None = None,
        duration_ms: int | None = None,
        tool_metadata: dict[str, Any] | None = None,
    ) -> "AgentEvent":
        payload: dict[str, Any] = {
            "item_id": item_id,
            "tool_call_id": tool_call_id or "",
            "changes": list(changes or []),
            "status": status,
            **(dict(tool_metadata) if tool_metadata else {}),
        }
        if error:
            payload["error"] = error
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        return cls(
            event_type=AgentEventType.FILE_CHANGE_COMPLETED,
            tool_call_id=tool_call_id or None,
            data=payload,
        )

    @classmethod
    def document_draft_started(
        cls,
        *,
        draft_id: str,
        target_path: str,
        title: str,
        format: str = "markdown",
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.DOCUMENT_DRAFT_STARTED,
            data={
                "draft_id": draft_id,
                "target_path": target_path,
                "title": title,
                "format": format,
                "status": "streaming",
            },
        )

    @classmethod
    def document_draft_preview_chunk(
        cls,
        *,
        draft_id: str,
        target_path: str,
        chunk_seq: int,
        start_offset: int,
        content: str,
        flush_latency_ms: int | None = None,
    ) -> "AgentEvent":
        end_offset = int(start_offset) + draft_text_units(content)
        payload: dict[str, Any] = {
            "draft_id": draft_id,
            "target_path": target_path,
            "chunk_seq": int(chunk_seq),
            "start_offset": int(start_offset),
            "end_offset": end_offset,
            "content": content,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "status": "streaming",
        }
        if flush_latency_ms is not None:
            payload["flush_latency_ms"] = max(0, int(flush_latency_ms))
        return cls(
            event_type=AgentEventType.DOCUMENT_DRAFT_PREVIEW_CHUNK,
            data=payload,
        )

    @classmethod
    def document_draft_progress(
        cls,
        *,
        draft_id: str,
        target_path: str,
        content_length: int,
        content_sha256: str,
        last_chunk_seq: int,
        status: str = "streaming",
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.DOCUMENT_DRAFT_PROGRESS,
            data={
                "draft_id": draft_id,
                "target_path": target_path,
                "content_length": int(content_length),
                "content_sha256": content_sha256,
                "last_chunk_seq": int(last_chunk_seq),
                "status": status,
            },
        )

    @classmethod
    def document_draft_snapshot(
        cls,
        *,
        draft_id: str,
        target_path: str,
        content: str,
        snapshot_kind: str,
        final: bool,
        last_chunk_seq: int,
        status: str = "streaming",
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.DOCUMENT_DRAFT_SNAPSHOT,
            data={
                "draft_id": draft_id,
                "target_path": target_path,
                "content": content,
                "content_length": draft_text_units(content),
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "snapshot_kind": snapshot_kind,
                "final": bool(final),
                "last_chunk_seq": int(last_chunk_seq),
                "status": status,
            },
        )

    @classmethod
    def document_draft_commit_requested(
        cls,
        *,
        draft_id: str,
        target_path: str,
        item_id: str,
        approval_id: str,
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.DOCUMENT_DRAFT_COMMIT_REQUESTED,
            data={
                "draft_id": draft_id,
                "target_path": target_path,
                "item_id": item_id,
                "approval_id": approval_id,
                "status": "committing",
            },
        )

    @classmethod
    def document_draft_committed(
        cls,
        *,
        draft_id: str,
        target_path: str,
        item_id: str,
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.DOCUMENT_DRAFT_COMMITTED,
            data={
                "draft_id": draft_id,
                "target_path": target_path,
                "item_id": item_id,
                "status": "committed",
            },
        )

    @classmethod
    def document_draft_failed(
        cls,
        *,
        draft_id: str,
        target_path: str,
        error: str,
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.DOCUMENT_DRAFT_FAILED,
            data={
                "draft_id": draft_id,
                "target_path": target_path,
                "status": "failed",
                "error": error,
            },
            error_message=error,
        )

    @classmethod
    def document_draft_cancelled(
        cls,
        *,
        draft_id: str,
        target_path: str,
        reason: str,
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.DOCUMENT_DRAFT_CANCELLED,
            data={
                "draft_id": draft_id,
                "target_path": target_path,
                "status": "cancelled",
                "reason": reason,
            },
        )

    @classmethod
    def draft_body_stalled(
        cls,
        *,
        draft_id: str,
        target_path: str,
        content_length: int,
        content_sha256: str,
        last_chunk_seq: int | None = None,
        reason: str = "",
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.DRAFT_BODY_STALLED,
            data={
                "draft_id": draft_id,
                "target_path": target_path,
                "status": "stalled",
                "content_length": int(content_length),
                "content_sha256": content_sha256,
                **(
                    {"last_chunk_seq": int(last_chunk_seq)}
                    if last_chunk_seq is not None
                    else {}
                ),
                **({"reason": reason} if reason else {}),
            },
        )

    @classmethod
    def draft_interrupted_recoverable(
        cls,
        *,
        draft_id: str,
        target_path: str,
        content_length: int,
        content_sha256: str,
        last_chunk_seq: int | None = None,
        reason: str = "",
    ) -> "AgentEvent":
        return cls(
            event_type=AgentEventType.DRAFT_INTERRUPTED_RECOVERABLE,
            data={
                "draft_id": draft_id,
                "target_path": target_path,
                "status": "recoverable",
                "content_length": int(content_length),
                "content_sha256": content_sha256,
                **(
                    {"last_chunk_seq": int(last_chunk_seq)}
                    if last_chunk_seq is not None
                    else {}
                ),
                **({"reason": reason} if reason else {}),
                "recovery_action": "continue",
            },
        )

    @classmethod
    def lifecycle_hook(
        cls,
        payload: dict[str, Any],
        *,
        runtime_artifacts: list[dict[str, Any]] | None = None,
    ) -> "AgentEvent":
        """Create a canonical lifecycle hook observation event."""
        return cls(
            event_type=AgentEventType.LIFECYCLE_HOOK,
            data=dict(payload),
            runtime_artifacts=[
                dict(artifact)
                for artifact in runtime_artifacts or []
                if isinstance(artifact, dict)
            ],
        )

    @classmethod
    def usage_update(
        cls,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        context_tokens: int | None = None,
        context_window: int | None = None,
        max_output_tokens: int | None = None,
        model: str | None = None,
        mode: str | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        cost_usd: float | None = None,
        usage_extra: dict[str, Any] | None = None,
        run_status: str | None = None,
    ) -> "AgentEvent":
        """Create a token/context usage update event."""
        return cls(
            event_type=AgentEventType.USAGE_UPDATE,
            data={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "context_tokens": context_tokens,
                "context_window": context_window,
                "max_output_tokens": max_output_tokens,
                "model": model,
                "mode": mode,
                "cache_reads": cache_read_tokens,
                "cache_writes": cache_write_tokens,
                "cost_usd": cost_usd,
                "cost_status": "available" if cost_usd is not None else "unavailable",
                "usage_extra": usage_extra or {},
                "run_status": run_status,
            },
        )

    @classmethod
    def agent_relation_completed(
        cls,
        *,
        run_id: str,
        agent_id: str,
        task: str,
        status: str,
        result: str | None = None,
        error: str | None = None,
    ) -> "AgentEvent":
        """Create an AgentRun relation completion event."""
        return cls(
            event_type=AgentEventType.AGENT_RELATION_COMPLETED,
            data={
                "run_id": run_id,
                "agent_id": agent_id,
                "task": task,
                "status": status,
                "result": result,
                "error": error,
            },
        )

    @classmethod
    def stream_token(cls, token: str) -> "AgentEvent":
        """Create a stream token event."""
        return cls(
            event_type=AgentEventType.STREAM_TOKEN,
            data={"token": token},
        )

    @classmethod
    def reasoning_token(cls, token: str) -> "AgentEvent":
        """Create a streamed reasoning token event."""
        return cls(
            event_type=AgentEventType.REASONING_TOKEN,
            data={"token": token},
        )

    @classmethod
    def error(cls, message: str) -> "AgentEvent":
        """Create an error event."""
        return cls(
            event_type=AgentEventType.ERROR,
            error_message=message,
        )

    @classmethod
    def provider_stream_interrupted(cls, payload: dict[str, Any]) -> "AgentEvent":
        """Create an event for a recoverable provider stream interruption."""
        return cls(
            event_type=AgentEventType.PROVIDER_STREAM_INTERRUPTED,
            data=payload,
            error_message=str(payload.get("message") or ""),
        )

    @classmethod
    def provider_stream_recovering(cls, payload: dict[str, Any]) -> "AgentEvent":
        """Create an event for an active provider stream recovery attempt."""
        return cls(event_type=AgentEventType.PROVIDER_STREAM_RECOVERING, data=payload)

    @classmethod
    def provider_stream_recovered(cls, payload: dict[str, Any]) -> "AgentEvent":
        """Create an event for a completed provider stream recovery."""
        return cls(event_type=AgentEventType.PROVIDER_STREAM_RECOVERED, data=payload)

    @classmethod
    def session_run_interrupted(cls, response: str, payload: dict[str, Any]) -> "AgentEvent":
        """Create a terminal recoverable session run interruption event."""
        return cls(
            event_type=AgentEventType.SESSION_RUN_INTERRUPTED,
            data={**payload, "response": response},
            error_message=str(payload.get("message") or ""),
        )

    @classmethod
    def runtime_status(cls, payload: dict[str, Any]) -> "AgentEvent":
        """Create a runtime limiter status event."""
        return cls(
            event_type=AgentEventType.RUNTIME_STATUS,
            data=payload,
        )

"""Agent events - event types for telemetry and hooks."""

import time
from dataclasses import dataclass, field
from typing import Optional, Any
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
    STREAM_TOKEN = "stream_token"
    REASONING_TOKEN = "reasoning_token"
    TOOL_CALL_DELTA = "tool_call_delta"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TOOL_CALL_PROTOCOL_ERROR = "tool_call_protocol_error"
    LIFECYCLE_HOOK = "lifecycle_hook"
    PROVIDER_STREAM_INTERRUPTED = "provider_stream_interrupted"
    PROVIDER_STREAM_RECOVERING = "provider_stream_recovering"
    PROVIDER_STREAM_RECOVERED = "provider_stream_recovered"
    SESSION_RUN_INTERRUPTED = "session_run_interrupted"
    DELEGATED_RUN_COMPLETED = "delegated_run_completed"
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
    def tool_call_start(
        cls,
        tool_name: str,
        tool_args: dict,
        *,
        tool_call_id: str | None = None,
        tool_source: str | None = None,
        index: int | None = None,
    ) -> "AgentEvent":
        """Create a tool call start event."""
        data = {
            **({"tool_source": tool_source} if tool_source else {}),
            **({"index": index} if index is not None else {}),
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
    def tool_call_end(
        cls,
        tool_name: str,
        result: str,
        *,
        tool_call_id: str | None = None,
        tool_source: str | None = None,
        index: int | None = None,
        meta: dict[str, Any] | None = None,
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
    def delegated_run_completed(
        cls,
        *,
        run_id: str,
        agent_id: str,
        task: str,
        status: str,
        result: str | None = None,
        error: str | None = None,
    ) -> "AgentEvent":
        """Create a delegated AgentRun completion event."""
        return cls(
            event_type=AgentEventType.DELEGATED_RUN_COMPLETED,
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

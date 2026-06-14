"""Project AgentRun execution facts into canonical SessionRun events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


TerminalMessageFn = Callable[[dict[str, Any], list[dict[str, Any]], str], str]


TOOL_RESULT_MAIN_TIMELINE_MAX_CHARS = 4000
RESULT_CONTEXT_MAIN_TIMELINE_MAX_CHARS = 4000
TEXT_MAIN_TIMELINE_MAX_CHARS = 8000
PUBLIC_PAYLOAD_STRING_MAX_CHARS = 4000
OUTPUT_SUMMARY_HEAD_CHARS = 1200
OUTPUT_SUMMARY_TAIL_CHARS = 1200
DEFAULT_OUTPUT_TRUNCATION_MARKER = "\n... output omitted from the main timeline; open raw events for the complete content ...\n"


@dataclass(frozen=True)
class AgentRunSessionProjectionLabels:
    agent_id: str = "agent"
    workflow: str = "agent_run"
    queued_title: str = "AgentRun queued"
    claimed_title: str = "AgentRun claimed by worker"
    session_ready_title: str = "AgentRun execution environment ready"
    session_ready_with_workdir_title: str = "AgentRun execution environment ready: {workdir}"
    log_fallback_title: str = "AgentRun log"
    error_fallback_message: str = "AgentRun error"
    output_truncation_marker: str = DEFAULT_OUTPUT_TRUNCATION_MARKER
    terminal_titles: dict[str, str] = field(
        default_factory=lambda: {
            "completed": "AgentRun completed",
            "failed": "AgentRun failed",
            "cancelled": "AgentRun cancelled",
            "blocked": "AgentRun blocked",
        }
    )


DEFAULT_AGENT_RUN_SESSION_PROJECTION_LABELS = AgentRunSessionProjectionLabels()


def agent_run_event_to_session_events(
    event: dict[str, Any],
    *,
    labels: AgentRunSessionProjectionLabels = DEFAULT_AGENT_RUN_SESSION_PROJECTION_LABELS,
    terminal_message: TerminalMessageFn | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    event_type = str(event.get("type") or "")
    payload = event.get("payload")
    data = payload if isinstance(payload, dict) else {}
    agent_run_id = str(event.get("agent_run_id") or "")
    seq = event.get("seq") or 0
    base = {
        "agent_run_id": agent_run_id,
        "agent_id": labels.agent_id,
        "workflow": labels.workflow,
        "raw_event_refs": [_raw_event_ref(event)],
    }
    if event_type == "queued":
        task = data.get("agent_run") if isinstance(data.get("agent_run"), dict) else {}
        return [
            (
                "context_event",
                _context_event(
                    labels,
                    labels.queued_title,
                    "agent_run_queued",
                    {**base, "agent_run_status": str(task.get("status") or "queued")},
                ),
            )
        ]
    if event_type == "claimed":
        return [
            (
                "context_event",
                _context_event(
                    labels,
                    labels.claimed_title,
                    "agent_run_claimed",
                    {
                        **base,
                        "agent_run_status": "claimed",
                        "worker_id": str(data.get("worker_id") or ""),
                        "peer_id": str(data.get("peer_id") or ""),
                        "worker_kind": str(data.get("worker_kind") or ""),
                        "request_id": str(data.get("request_id") or ""),
                    },
                ),
            )
        ]
    if event_type in {"session_metadata", "session_pinned"}:
        workdir = str(data.get("workdir") or "")
        title = (
            labels.session_ready_with_workdir_title.format(workdir=workdir)
            if workdir
            else labels.session_ready_title
        )
        return [
            (
                "context_event",
                _context_event(
                    labels,
                    title,
                    "agent_run_session_ready",
                    {**base, "agent_run_status": "session_ready", **data},
                ),
            )
        ]
    if event_type == "session_run_start":
        start_data = _event_data(data)
        if not start_data:
            start_data = {
                str(key): value
                for key, value in data.items()
                if key not in {"type", "text"}
            }
        prompt = str(
            start_data.get("prompt")
            or start_data.get("user_input")
            or data.get("prompt")
            or ""
        )
        return [
            (
                "session_run_start",
                {**base, **start_data, "prompt": prompt},
            )
        ]
    if event_type == "session_run_end":
        end_data = _event_data(data)
        if not end_data:
            end_data = {
                str(key): value
                for key, value in data.items()
                if key not in {"type", "text"}
            }
        response = str(
            end_data.get("response")
            or end_data.get("result")
            or data.get("text")
            or ""
        )
        if "response_rendered" in end_data:
            rendered = bool(end_data.get("response_rendered"))
        elif "render_response" in end_data:
            rendered = not bool(end_data.get("render_response"))
        else:
            rendered = True
        public_end = {
            str(key): value
            for key, value in end_data.items()
            if key != "render_response"
        }
        public_end["response"] = response
        public_end["response_rendered"] = rendered
        return [("session_run_end", {**base, **public_end})]
    if event_type == "status":
        status_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        status = str(status_data.get("status") or "").strip()
        if not status:
            return []
        status_events = [
            (
                "context_event",
                _context_event(
                    labels,
                    f"{labels.agent_id} {status}",
                    f"agent_run_{status}",
                    {**base, "agent_run_status": status, **status_data},
                ),
            )
        ]
        if status == "waiting_approval":
            status_events.append(
                (
                    "approval_request",
                    _waiting_approval_payload(
                        status_data,
                        base=base,
                        marker=labels.output_truncation_marker,
                    ),
                )
            )
        return status_events
    if event_type == "text":
        text = str(data.get("text") or "")
        return [("assistant_delta", {**base, "content": text})] if text else []
    if event_type == "thinking":
        text = str(data.get("text") or "")
        return [("reasoning_delta", {**base, "content": text})] if text else []
    if event_type == "log":
        text = str(data.get("text") or data.get("message") or "")
        level_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        level = str(level_data.get("level") or "info")
        text, text_meta = _project_large_output(
            text,
            max_chars=RESULT_CONTEXT_MAIN_TIMELINE_MAX_CHARS,
            marker=labels.output_truncation_marker,
        )
        public_meta = _public_payload(level_data, marker=labels.output_truncation_marker)
        public_meta.update(_prefixed_projection_meta("log", text_meta))
        return [
            (
                "context_event",
                _context_event(
                    labels,
                    text or labels.log_fallback_title,
                    "agent_run_log",
                    {**base, "level": level, "log": text, "meta": public_meta},
                ),
            )
        ]
    if event_type == "error":
        message = str(data.get("text") or data.get("message") or labels.error_fallback_message)
        return [("error", {**base, "message": message, "code": "agent_run_error"})]
    if event_type == "result":
        result_data = _event_data(data)
        raw_output = str(
            data.get("text")
            or result_data.get("output")
            or result_data.get("result")
            or ""
        )
        output, output_meta = _project_large_output(
            raw_output,
            max_chars=RESULT_CONTEXT_MAIN_TIMELINE_MAX_CHARS,
            marker=labels.output_truncation_marker,
        )
        public_result = _public_payload(
            _without_large_output_fields(result_data),
            marker=labels.output_truncation_marker,
        )
        public_result.update(output_meta)
        status = str(result_data.get("status") or "completed")
        return [
            (
                "context_event",
                _context_event(
                    labels,
                    labels.terminal_titles.get(status, f"{labels.agent_id} result"),
                    "agent_run_result",
                    {**base, "agent_run_status": status, "output": output, "result": public_result},
                ),
            )
        ]
    if event_type == "usage":
        usage_data = _event_data(data)
        return [
            (
                "context_event",
                _context_event(
                    labels,
                    f"{labels.agent_id} usage updated",
                    "agent_run_usage",
                    {**base, "usage": usage_data},
                ),
            )
        ]
    if event_type == "lifecycle_hook":
        hook_payload = _event_data(data)
        if not hook_payload:
            hook_payload = {
                str(key): value
                for key, value in data.items()
                if key not in {"type", "text", "data"}
            }
        public_payload = _public_lifecycle_hook_payload(
            hook_payload,
            base=base,
            marker=labels.output_truncation_marker,
        )
        public_payload["raw_event_refs"] = base["raw_event_refs"]
        return [("lifecycle_hook", public_payload)]
    if event_type == "permission.blocked_review":
        permission_payload = _event_data(data)
        if not permission_payload:
            permission_payload = {
                str(key): value
                for key, value in data.items()
                if key not in {"type", "text", "data"}
            }
        return [
            (
                "workflow_decision",
                _permission_blocked_review_payload(
                    permission_payload,
                    base=base,
                    marker=labels.output_truncation_marker,
                ),
            )
        ]
    if event_type == "delegated_run_completed":
        child_run_id = str(data.get("agent_run_id") or data.get("run_id") or "")
        child_agent_id = str(data.get("agent_id") or "")
        status = str(data.get("status") or "completed")
        result, result_meta = _project_large_output(
            str(data.get("result") or ""),
            max_chars=RESULT_CONTEXT_MAIN_TIMELINE_MAX_CHARS,
            marker=labels.output_truncation_marker,
        )
        error = str(data.get("error") or "")
        return [
            (
                "context_event",
                _context_event(
                    labels,
                    f"{child_agent_id or 'delegated agent'} {status}",
                    "delegated_run_completed",
                    {
                        **base,
                        "child_agent_run_id": child_run_id,
                        "child_agent_id": child_agent_id,
                        "agent_run_status": status,
                        "result": result,
                        "error": error,
                        "meta": {
                            **_public_payload(
                                _without_large_output_fields(data),
                                marker=labels.output_truncation_marker,
                            ),
                            **_prefixed_projection_meta("result", result_meta),
                        },
                    },
                ),
            )
        ]
    if event_type == "tool_use":
        tool_data = _event_data(data)
        tool_name = _tool_name(tool_data)
        tool_call_id = _tool_call_id(tool_data, agent_run_id, seq)
        raw_tool_args = (
            tool_data.get("input")
            if isinstance(tool_data.get("input"), dict)
            else tool_data
        )
        tool_args = _public_payload(
            raw_tool_args if isinstance(raw_tool_args, dict) else {"value": raw_tool_args},
            marker=labels.output_truncation_marker,
        )
        return [
            (
                "tool_call_start",
                {
                    **base,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "tool_args": tool_args,
                    **_tool_identity_payload(tool_data),
                },
            )
        ]
    if event_type == "tool_result":
        tool_data = _event_data(data)
        tool_name = _tool_name(tool_data)
        tool_call_id = _tool_call_id(tool_data, agent_run_id, seq)
        output = tool_data.get("output")
        if not isinstance(output, str):
            output = str(data.get("text") or "")
        projected_output, output_meta = _project_large_output(
            output,
            max_chars=TOOL_RESULT_MAIN_TIMELINE_MAX_CHARS,
            marker=labels.output_truncation_marker,
        )
        public_tool_data = _public_payload(
            _without_large_output_fields(tool_data),
            marker=labels.output_truncation_marker,
        )
        nested_meta = tool_data.get("meta")
        if isinstance(nested_meta, dict):
            public_tool_data.pop("meta", None)
            public_tool_data.update(
                _public_payload(
                    nested_meta,
                    marker=labels.output_truncation_marker,
                )
            )
        public_tool_data.update(output_meta)
        permission = _project_permission_audit(
            tool_data,
            marker=labels.output_truncation_marker,
        )
        if permission:
            public_tool_data["permission"] = permission
        return [
            (
                "tool_call_end",
                {
                    **base,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "tool_result": projected_output,
                    **_tool_identity_payload(tool_data),
                    "meta": public_tool_data,
                },
            )
        ]
    if event_type in {"completed", "failed", "cancelled", "blocked"}:
        task = data.get("agent_run") if isinstance(data.get("agent_run"), dict) else {}
        message = (
            terminal_message(task, [event], event_type)
            if terminal_message is not None
            else _terminal_message(task, data, event_type)
        )
        output, output_meta = _project_large_output(
            str(task.get("output") or ""),
            max_chars=RESULT_CONTEXT_MAIN_TIMELINE_MAX_CHARS,
            marker=labels.output_truncation_marker,
        )
        message, message_meta = _project_large_output(
            message,
            max_chars=RESULT_CONTEXT_MAIN_TIMELINE_MAX_CHARS,
            marker=labels.output_truncation_marker,
        )
        return [
            (
                "context_event",
                _context_event(
                    labels,
                    labels.terminal_titles.get(event_type, event_type),
                    f"agent_run_{event_type}",
                    {
                        **base,
                        "agent_run_status": event_type,
                        "output": output,
                        "message": message,
                        "terminal": {
                            **_prefixed_projection_meta("output", output_meta),
                            **_prefixed_projection_meta("message", message_meta),
                        },
                    },
                ),
            )
        ]
    return []


def agent_run_events_to_session_events(
    events: list[dict[str, Any]],
    *,
    labels: AgentRunSessionProjectionLabels = DEFAULT_AGENT_RUN_SESSION_PROJECTION_LABELS,
    terminal_message: TerminalMessageFn | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Project raw AgentRun facts into coarser user-visible SessionRun facts."""

    projected: list[tuple[str, dict[str, Any]]] = []
    buffered_type = ""
    buffered_content: list[str] = []
    buffered_payload: dict[str, Any] = {}
    buffered_refs: list[dict[str, Any]] = []

    def flush_buffer() -> None:
        nonlocal buffered_type, buffered_content, buffered_payload, buffered_refs
        if not buffered_type or not buffered_content:
            buffered_type = ""
            buffered_content = []
            buffered_payload = {}
            buffered_refs = []
            return
        payload = dict(buffered_payload)
        content, content_meta = _project_large_output(
            "".join(buffered_content),
            max_chars=TEXT_MAIN_TIMELINE_MAX_CHARS,
            marker=labels.output_truncation_marker,
        )
        payload["content"] = content
        if content_meta.get("output_truncated"):
            payload["content_projection"] = _prefixed_projection_meta("content", content_meta)
        payload["raw_event_refs"] = list(buffered_refs)
        projected.append((buffered_type, payload))
        buffered_type = ""
        buffered_content = []
        buffered_payload = {}
        buffered_refs = []

    for event in events:
        for event_type, payload in agent_run_event_to_session_events(
            event,
            labels=labels,
            terminal_message=terminal_message,
        ):
            content = str(payload.get("content") or "")
            if event_type in {"assistant_delta", "reasoning_delta"} and content:
                if buffered_type == event_type:
                    buffered_content.append(content)
                    buffered_refs.extend(_raw_event_refs(payload))
                    continue
                flush_buffer()
                buffered_type = event_type
                buffered_content = [content]
                buffered_payload = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"content", "raw_event_refs"}
                }
                buffered_refs = _raw_event_refs(payload)
                continue
            flush_buffer()
            projected.append((event_type, payload))
    flush_buffer()
    return projected


def _context_event(
    labels: AgentRunSessionProjectionLabels,
    title: str,
    phase: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "message": title,
        "phase": phase,
        "workflow": labels.workflow,
        **(extra or {}),
    }


def _event_data(data: dict[str, Any]) -> dict[str, Any]:
    nested = data.get("data")
    return dict(nested) if isinstance(nested, dict) else {}


def _project_large_output(
    output: str,
    *,
    max_chars: int,
    marker: str = DEFAULT_OUTPUT_TRUNCATION_MARKER,
) -> tuple[str, dict[str, Any]]:
    if len(output) <= max_chars:
        return output, {
            "output_chars": len(output),
            "output_truncated": False,
        }
    head = output[:OUTPUT_SUMMARY_HEAD_CHARS]
    tail = output[-OUTPUT_SUMMARY_TAIL_CHARS:] if OUTPUT_SUMMARY_TAIL_CHARS > 0 else ""
    return f"{head}{marker}{tail}", {
        "output_chars": len(output),
        "output_truncated": True,
        "output_summary_chars": OUTPUT_SUMMARY_HEAD_CHARS + OUTPUT_SUMMARY_TAIL_CHARS,
        "output_source": "raw_event",
    }


def _without_large_output_fields(data: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in data.items()
        if key not in {"output", "content", "text", "result"}
    }


def _tool_identity_payload(data: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "tool_id": data.get("tool_id", data.get("toolId")),
        "risk": data.get("risk"),
        "exposure": data.get("exposure"),
        "capability_name": data.get("capability_name", data.get("capabilityName")),
    }
    return {
        key: str(value).strip()
        for key, value in fields.items()
        if value is not None and str(value).strip()
    }


def _public_payload(data: dict[str, Any], *, marker: str) -> dict[str, Any]:
    public: dict[str, Any] = {}
    truncated_fields: list[str] = []
    for key, value in data.items():
        public_value, value_truncated = _public_value(value, marker=marker, path=str(key))
        public[key] = public_value
        truncated_fields.extend(value_truncated)
    if truncated_fields:
        public["truncated_fields"] = truncated_fields
        public["full_payload_source"] = "raw_event"
    return public


_LIFECYCLE_PUBLIC_FIELDS: tuple[str, ...] = (
    "phase",
    "event_name",
    "placement",
    "session_run_id",
    "agent_run_id",
    "turn_id",
    "tool_call_id",
    "tool_name",
    "mcp_server",
    "child_agent_run_id",
    "child_agent_id",
    "parent_session_id",
    "parent_turn_id",
    "lifecycle_hook_id",
    "lifecycle_hook_source",
    "status",
    "trigger_source",
    "hook_id",
    "display_name",
    "source",
    "handler_type",
    "decision",
    "continue_flow",
    "level",
    "title",
    "message",
    "user_message",
    "result_action",
    "reason",
)

_LIFECYCLE_PUBLIC_CONTEXT_FIELDS: tuple[str, ...] = (
    "tool_call_id",
    "tool_name",
    "mcp_server",
    "child_agent_run_id",
    "child_agent_id",
    "parent_session_id",
    "parent_turn_id",
    "lifecycle_hook_id",
    "lifecycle_hook_source",
    "status",
    "tool_names",
    "tool_call_ids",
    "tool_sources",
    "mcp_servers",
    "result_action",
    "reason",
)

_LIFECYCLE_PUBLIC_DIAGNOSTIC_FIELDS: tuple[str, ...] = (
    "code",
    "message",
    "level",
    "event_name",
    "handler_type",
    "hook_id",
    "failure_policy",
)

_LIFECYCLE_PUBLIC_ARTIFACT_FIELDS: tuple[str, ...] = (
    "kind",
    "id",
    "artifact_id",
    "field",
    "original_chars",
    "original_bytes",
)


def _public_lifecycle_hook_payload(
    hook_payload: dict[str, Any],
    *,
    base: dict[str, Any],
    marker: str,
) -> dict[str, Any]:
    public: dict[str, Any] = dict(base)
    for key in _LIFECYCLE_PUBLIC_FIELDS:
        if key in hook_payload:
            public_value, _truncated = _public_value(
                hook_payload[key],
                marker=marker,
                path=key,
            )
            public[key] = public_value
    context_payload = hook_payload.get("payload")
    if isinstance(context_payload, dict):
        for key in _LIFECYCLE_PUBLIC_CONTEXT_FIELDS:
            if key in context_payload:
                public_value, _truncated = _public_value(
                    context_payload[key],
                    marker=marker,
                    path=f"payload.{key}",
                )
                public[key] = public_value
    diagnostics = _public_lifecycle_hook_diagnostics(
        hook_payload.get("diagnostics"),
        marker=marker,
    )
    if diagnostics:
        public["diagnostics"] = diagnostics
    artifacts = _public_lifecycle_hook_artifacts(
        hook_payload.get("artifacts"),
        marker=marker,
    )
    output = hook_payload.get("output")
    if isinstance(output, dict):
        artifacts.extend(
            _public_lifecycle_hook_output_artifacts(
                output.get("artifacts"),
                marker=marker,
            )
        )
    if artifacts:
        public["artifacts"] = _dedupe_lifecycle_artifacts(artifacts)
    return public


def _public_lifecycle_hook_diagnostics(
    value: Any,
    *,
    marker: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    diagnostics: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        public_item: dict[str, Any] = {}
        for key in _LIFECYCLE_PUBLIC_DIAGNOSTIC_FIELDS:
            if key not in item:
                continue
            public_value, _truncated = _public_value(
                item[key],
                marker=marker,
                path=f"diagnostics.{key}",
            )
            public_item[key] = public_value
        if public_item:
            diagnostics.append(public_item)
    return diagnostics


def _public_lifecycle_hook_artifacts(
    value: Any,
    *,
    marker: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    artifacts: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        public_item: dict[str, Any] = {}
        for key in _LIFECYCLE_PUBLIC_ARTIFACT_FIELDS:
            if key not in item:
                continue
            public_value, _truncated = _public_value(
                item[key],
                marker=marker,
                path=f"artifacts.{key}",
            )
            public_item[key] = public_value
        if public_item:
            artifacts.append(public_item)
    return artifacts


def _public_lifecycle_hook_output_artifacts(
    value: Any,
    *,
    marker: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    artifacts: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("kind") != "lifecycle_output_overflow":
            continue
        public_item: dict[str, Any] = {}
        for key in _LIFECYCLE_PUBLIC_ARTIFACT_FIELDS:
            if key not in item:
                continue
            public_value, _truncated = _public_value(
                item[key],
                marker=marker,
                path=f"output.artifacts.{key}",
            )
            public_item[key] = public_value
        if public_item:
            artifacts.append(public_item)
    return artifacts


def _dedupe_lifecycle_artifacts(
    artifacts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in artifacts:
        key = (
            str(item.get("kind") or ""),
            str(item.get("id") or item.get("artifact_id") or ""),
            str(item.get("field") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


_PERMISSION_PUBLIC_FIELDS: tuple[str, ...] = (
    "action",
    "authorized",
    "policy_matched",
    "capability_matched",
    "reason",
    "approval_id",
)

_PERMISSION_AUDIT_PUBLIC_FIELDS: tuple[str, ...] = (
    "lifecycle_event",
)

_PERMISSION_HOOK_PUBLIC_FIELDS: tuple[str, ...] = (
    "hook_id",
    "display_name",
    "source",
    "handler_type",
    "decision",
    "reason",
)


def _project_permission_audit(
    tool_data: dict[str, Any],
    *,
    marker: str,
) -> dict[str, Any]:
    for permission in _permission_candidates(tool_data):
        public = _public_permission(permission, marker=marker)
        if public:
            return public
    return {}


def _permission_blocked_review_payload(
    data: dict[str, Any],
    *,
    base: dict[str, Any],
    marker: str,
) -> dict[str, Any]:
    tool_name = str(data.get("tool_name") or data.get("tool") or "").strip()
    reason = str(
        data.get("reason")
        or data.get("message")
        or "background execution cannot wait for interactive approval"
    )
    permission = (
        _public_permission(data["permission"], marker=marker)
        if isinstance(data.get("permission"), dict)
        else {}
    )
    review: dict[str, Any] = {
        "tool_name": tool_name,
        "reason": reason,
    }
    if permission:
        review["permission"] = permission
    return {
        **base,
        "workflow": "agent_run_permission",
        "decision_type": "permission_review",
        "status": "pending",
        "title": "Permission review required",
        "summary": reason,
        "approval_id": str(data.get("approval_id") or ""),
        "tool_name": tool_name,
        "review": review,
    }


def _waiting_approval_payload(
    data: dict[str, Any],
    *,
    base: dict[str, Any],
    marker: str,
) -> dict[str, Any]:
    reason = str(data.get("reason") or data.get("message") or "approval required")
    tool_args = data.get("tool_args")
    if not isinstance(tool_args, dict):
        tool_args = data.get("args") if isinstance(data.get("args"), dict) else {}
    permission = (
        _public_permission(data["permission"], marker=marker)
        if isinstance(data.get("permission"), dict)
        else {}
    )
    public = {
        **base,
        "approval_id": str(data.get("approval_id") or ""),
        "tool_name": str(data.get("tool_name") or data.get("tool") or ""),
        "tool_call_id": str(data.get("tool_call_id") or ""),
        "reason": reason,
        "intent": str(data.get("intent") or reason),
        "tool_args": _public_payload(tool_args, marker=marker),
    }
    if permission:
        public["permission"] = permission
    return public


def _permission_candidates(tool_data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    direct = tool_data.get("permission")
    if isinstance(direct, dict):
        candidates.append(direct)
    meta = tool_data.get("meta")
    if isinstance(meta, dict):
        meta_permission = meta.get("permission")
        if isinstance(meta_permission, dict):
            candidates.append(meta_permission)
        candidates.extend(_permission_candidates_from_diagnostics(meta))
    candidates.extend(_permission_candidates_from_diagnostics(tool_data))
    return candidates


def _permission_candidates_from_diagnostics(
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    diagnostics = data.get("tool_diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = data.get("diagnostics")
    if not isinstance(diagnostics, list):
        return candidates
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        permission = diagnostic.get("permission")
        if isinstance(permission, dict):
            candidates.append(permission)
        metadata = diagnostic.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("permission"), dict):
            candidates.append(metadata["permission"])
    return candidates


def _public_permission(
    permission: dict[str, Any],
    *,
    marker: str,
) -> dict[str, Any]:
    public: dict[str, Any] = {}
    for key in _PERMISSION_PUBLIC_FIELDS:
        if key not in permission:
            continue
        value, _truncated = _public_value(permission[key], marker=marker, path=key)
        public[key] = value
    audit = permission.get("audit")
    if isinstance(audit, dict):
        for key in _PERMISSION_AUDIT_PUBLIC_FIELDS:
            if key not in audit:
                continue
            value, _truncated = _public_value(
                audit[key],
                marker=marker,
                path=f"audit.{key}",
            )
            public[key] = value
        hooks = _public_permission_lifecycle_hooks(
            audit.get("lifecycle_hooks"),
            marker=marker,
        )
        if hooks:
            public["lifecycle_hooks"] = hooks
    return public if _has_permission_identity(public) else {}


def _public_permission_lifecycle_hooks(
    value: Any,
    *,
    marker: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    hooks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        public_item: dict[str, Any] = {}
        for key in _PERMISSION_HOOK_PUBLIC_FIELDS:
            if key not in item:
                continue
            public_value, _truncated = _public_value(
                item[key],
                marker=marker,
                path=f"lifecycle_hooks.{key}",
            )
            public_item[key] = public_value
        if public_item:
            hooks.append(public_item)
    return hooks


def _has_permission_identity(permission: dict[str, Any]) -> bool:
    for key in ("action", "authorized", "policy_matched", "reason"):
        if key in permission:
            return True
    return False


def _public_value(value: Any, *, marker: str, path: str) -> tuple[Any, list[str]]:
    if isinstance(value, str):
        projected, meta = _project_large_output(
            value,
            max_chars=PUBLIC_PAYLOAD_STRING_MAX_CHARS,
            marker=marker,
        )
        return projected, [path] if meta.get("output_truncated") else []
    if isinstance(value, dict):
        public: dict[str, Any] = {}
        truncated: list[str] = []
        for key, item in value.items():
            public_item, item_truncated = _public_value(
                item,
                marker=marker,
                path=f"{path}.{key}",
            )
            public[str(key)] = public_item
            truncated.extend(item_truncated)
        return public, truncated
    if isinstance(value, list):
        public_items: list[Any] = []
        truncated: list[str] = []
        for index, item in enumerate(value):
            public_item, item_truncated = _public_value(
                item,
                marker=marker,
                path=f"{path}[{index}]",
            )
            public_items.append(public_item)
            truncated.extend(item_truncated)
        return public_items, truncated
    return value, []


def _prefixed_projection_meta(prefix: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        f"{prefix}_chars": meta.get("output_chars", 0),
        f"{prefix}_truncated": bool(meta.get("output_truncated")),
        **(
            {
                f"{prefix}_summary_chars": meta.get("output_summary_chars", 0),
                f"{prefix}_source": meta.get("output_source", "raw_event"),
            }
            if meta.get("output_truncated")
            else {}
        ),
    }


def _tool_name(data: dict[str, Any]) -> str:
    return str(data.get("tool_name") or data.get("name") or data.get("tool") or "tool")


def _tool_call_id(data: dict[str, Any], agent_run_id: str, seq: Any) -> str:
    return str(data.get("tool_call_id") or data.get("id") or f"{agent_run_id}:tool:{seq}")


def _terminal_message(task: dict[str, Any], data: dict[str, Any], status: str) -> str:
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    for value in (
        task.get("failure_reason"),
        task.get("cancel_reason"),
        result.get("error"),
        data.get("error"),
        data.get("message"),
        result.get("output"),
        task.get("output"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return status


def _raw_event_ref(event: dict[str, Any]) -> dict[str, Any]:
    try:
        seq = int(event.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return {
        "agent_run_id": str(event.get("agent_run_id") or ""),
        "seq": seq,
        "type": str(event.get("type") or ""),
    }


def _raw_event_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    refs = payload.get("raw_event_refs")
    if not isinstance(refs, list):
        return []
    return [dict(item) for item in refs if isinstance(item, dict)]


__all__ = [
    "AgentRunSessionProjectionLabels",
    "DEFAULT_AGENT_RUN_SESSION_PROJECTION_LABELS",
    "agent_run_events_to_session_events",
    "agent_run_event_to_session_events",
]

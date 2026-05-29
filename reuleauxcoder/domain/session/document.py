"""Session document reducer used as the UI history authority."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

_LIVE_ONLY_SESSION_EVENTS = frozenset(
    {"assistant_delta", "reasoning_delta", "tool_call_delta", "tool_call_stream"}
)
_DEFAULT_SESSION_TITLES = frozenset({"", "新会话"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def empty_session_document(
    session_id: str,
    *,
    metadata: dict[str, Any] | None = None,
    runtime_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(metadata or {})
    now = str(meta.get("saved_at") or meta.get("updatedAt") or utc_now())
    model = str(meta.get("model") or _string(runtime_state, "model") or "")
    title = str(meta.get("preview") or meta.get("title") or "新会话")
    stats = _empty_stats()
    stats["model"] = model
    active_mode = _string(runtime_state, "active_mode")
    if active_mode:
        stats["mode"] = active_mode
    return {
        "version": 1,
        "metadata": {
            "id": session_id,
            "model": model,
            "saved_at": now,
            "preview": str(meta.get("preview") or ""),
            "fingerprint": str(meta.get("fingerprint") or ""),
        },
        "session": {
            "id": session_id,
            "title": title,
            "updatedAt": now,
            "kind": str(meta.get("kind") or "main"),
            "state": str(meta.get("state") or "active"),
            "summary": str(meta.get("summary") or meta.get("preview") or ""),
        },
        "stats": stats,
        "turns": [],
        "parts": [],
        "trace": {
            "nodes": [],
            "edges": [],
            "ui": _empty_trace_ui(),
        },
        "traceNodes": [],
        "traceEdges": [],
        "traceUI": _empty_trace_ui(),
        "revision": 0,
        "last_event_seq": 0,
        "run_state": {
            "status": "idle",
            "session_run_id": None,
            "error": None,
        },
    }


def apply_session_event(
    document: dict[str, Any] | None,
    *,
    session_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    session_event_seq: int,
    session_run_id: str | None = None,
    session_run_seq: int | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    doc = deepcopy(document) if isinstance(document, dict) and document else empty_session_document(session_id)
    _ensure_document_shape(doc, session_id)
    payload = dict(payload or {})
    event_type = str(event_type or "")
    if event_type in _LIVE_ONLY_SESSION_EVENTS:
        return doc
    event_seq = max(0, int(session_event_seq or 0))
    timestamp = created_at or utc_now()
    meta = {
        "eventKey": f"session:{session_id}:{event_seq}",
        "sessionEventSeq": event_seq,
    }

    doc["last_event_seq"] = max(int(doc.get("last_event_seq") or 0), event_seq)
    doc["revision"] = int(doc.get("revision") or 0) + 1
    session = _dict(doc, "session")
    session["updatedAt"] = timestamp
    metadata = _dict(doc, "metadata")
    metadata["saved_at"] = timestamp

    if session_run_id:
        run_state = _dict(doc, "run_state")
        run_state["session_run_id"] = session_run_id
    if session_run_seq is not None:
        run_state = _dict(doc, "run_state")
        run_state["last_session_run_seq"] = int(session_run_seq)

    if event_type == "session_run_start":
        _apply_session_run_start(doc, payload, timestamp, meta)
    elif event_type == "remote_peer_ready":
        _apply_remote_peer_ready(doc, payload)
        _patch_stats(doc, {
            "model": _string(payload, "model") or None,
            "mode": _string(payload, "mode") or None,
        })
    elif event_type in {"usage_update", "run_stats"}:
        _apply_usage(doc, payload)
    elif event_type == "reasoning_message":
        _upsert_reasoning_part(
            doc,
            str(payload.get("content") or ""),
            "plain" if str(payload.get("format") or "") == "plain" else "markdown",
            meta,
        )
    elif event_type == "assistant_message":
        _append_text_part(
            doc,
            str(payload.get("content") or ""),
            "assistant-message",
            "markdown",
            meta,
        )
    elif event_type == "session_run_end":
        _apply_session_run_end(doc, payload, meta)
    elif event_type in {"output", "tool_call_start", "tool_call_end",
                        "tool_call_protocol_error", "approval_request", "approval_resolved"}:
        _apply_tool_or_output_event(doc, event_type, payload, meta)
    elif event_type == "runtime_status":
        _apply_runtime_status(doc, payload, meta)
    elif event_type == "memory_context" or (
        event_type == "context_event" and _is_memory_payload(payload)
    ):
        _append_part(doc, _part("memory", "memory_context", meta, {
            "memoryTitle": _string(payload, "title") or "注入记忆",
            "memoryPayload": payload,
        }))
    elif event_type == "context_event":
        _append_part(doc, _part("context", "context_event", meta, {
            "contextTitle": _string(payload, "message") or _string(payload, "phase") or "上下文事件",
            "contextPayload": payload,
        }))
    elif event_type == "view":
        _append_view_part(doc, payload, meta)
    elif event_type in {
        "remote_event",
        "mcp_event",
        "model_event",
        "session_event",
        "command_event",
        "approval_event",
        "system_event",
        "agent_event",
        "ui_event",
        "delegated_run_completed",
        "taskflow_started",
    }:
        _append_part(doc, _part(event_type, "ui_event", meta, {
            "uiEventKind": _string(payload, "kind") or event_type.replace("_event", ""),
            "uiEventLevel": _string(payload, "level") or "info",
            "uiEventTitle": _string(payload, "title") or _string(payload, "message") or "运行事件",
            "uiEventPayload": payload,
        }))
    elif event_type in {"error", "session_run_failed"}:
        _settle_stale_pending_approvals(
            doc,
            status="denied",
            reason=str(payload.get("message") or "unknown error"),
            meta=meta,
        )
        _patch_run_state(doc, status="error", error=str(payload.get("message") or "unknown error"))
        if event_type == "session_run_failed":
            _append_text_part(
                doc,
                f"错误：{payload.get('message') or 'unknown error'}",
                "error",
                "plain",
                meta,
            )
    elif event_type in {"session_run_cancel_requested", "session_run_cancelled"}:
        _patch_run_state(doc, status="cancelled", error=str(payload.get("reason") or "session_run_cancelled"))
        if event_type == "session_run_cancelled":
            _settle_stale_pending_approvals(
                doc,
                status="cancelled",
                reason=str(payload.get("reason") or "session_run_cancelled"),
                meta=meta,
            )
            _append_text_part(doc, "已取消当前请求。", "cancelled", "plain", meta)

    _sync_compatible_trace_fields(doc)
    return doc


def settle_orphaned_running_session_run(
    document: dict[str, Any] | None,
    *,
    session_id: str,
    reason: str,
) -> dict[str, Any]:
    doc = deepcopy(document) if isinstance(document, dict) and document else empty_session_document(session_id)
    _ensure_document_shape(doc, session_id)
    run_state = _dict(doc, "run_state")
    stats = _dict(doc, "stats")
    if str(run_state.get("status") or "") != "running" and str(stats.get("runStatus") or "") != "running":
        return doc

    event_seq = int(doc.get("last_event_seq") or 0)
    meta = {
        "eventKey": f"session:{session_id}:orphaned-session-run",
        "sessionEventSeq": event_seq,
    }
    _settle_stale_pending_approvals(
        doc,
        status="denied",
        reason=reason,
        meta=meta,
    )
    _patch_run_state(doc, status="error", error=reason)
    _dict(doc, "session")["state"] = "error"
    doc["revision"] = int(doc.get("revision") or 0) + 1
    _sync_compatible_trace_fields(doc)
    return doc


def update_session_document_metadata(
    document: dict[str, Any] | None,
    *,
    session_id: str,
    model: str,
    saved_at: str,
    preview: str,
    fingerprint: str,
    runtime_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    doc = deepcopy(document) if isinstance(document, dict) and document else empty_session_document(
        session_id,
        metadata={
            "model": model,
            "saved_at": saved_at,
            "preview": preview,
            "fingerprint": fingerprint,
        },
        runtime_state=runtime_state,
    )
    _ensure_document_shape(doc, session_id)
    doc["metadata"] = {
        "id": session_id,
        "model": model,
        "saved_at": saved_at,
        "preview": preview,
        "fingerprint": fingerprint,
    }
    session = _dict(doc, "session")
    session["id"] = session_id
    session["title"] = session.get("title") or preview or "新会话"
    session["updatedAt"] = saved_at
    session["summary"] = session.get("summary") or preview
    stats = _dict(doc, "stats")
    stats["model"] = model
    active_mode = _string(runtime_state, "active_mode")
    if active_mode:
        stats["mode"] = active_mode
    doc["revision"] = int(doc.get("revision") or 0) + 1
    _sync_compatible_trace_fields(doc)
    return doc


def session_metadata_from_document(document: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    session = document.get("session") if isinstance(document.get("session"), dict) else {}
    stats = document.get("stats") if isinstance(document.get("stats"), dict) else {}
    session_id = str(metadata.get("id") or session.get("id") or fallback.get("id") or "")
    preview = str(
        session.get("summary")
        or metadata.get("preview")
        or stats.get("taskText")
        or fallback.get("preview")
        or session.get("title")
        or ""
    )
    return {
        "id": session_id,
        "model": str(stats.get("model") or metadata.get("model") or fallback.get("model") or ""),
        "saved_at": str(session.get("updatedAt") or metadata.get("saved_at") or fallback.get("saved_at") or ""),
        "preview": preview,
        "fingerprint": str(metadata.get("fingerprint") or fallback.get("fingerprint") or ""),
        "kind": str(session.get("kind") or "main"),
        "summary": preview,
        "run_state": (document.get("run_state") or {}).get("status")
        if isinstance(document.get("run_state"), dict)
        else None,
    }


def _empty_stats() -> dict[str, Any]:
    return {
        "taskText": "",
        "tokensIn": 0,
        "tokensOut": 0,
        "cacheReads": None,
        "cacheWrites": None,
        "totalCost": None,
        "costStatus": "unavailable",
        "contextTokens": 0,
        "contextWindow": 0,
        "maxOutputTokens": 0,
        "runStatus": "idle",
    }


def _empty_trace_ui() -> dict[str, Any]:
    return {
        "activeNodeId": None,
        "selectedNodeId": None,
        "focusedBranchId": "main",
        "showInspector": False,
        "showMiniMap": False,
        "viewMode": "compact",
    }


def _ensure_document_shape(doc: dict[str, Any], session_id: str) -> None:
    doc.setdefault("version", 1)
    doc.setdefault("metadata", {"id": session_id})
    doc.setdefault("session", {"id": session_id, "title": "新会话", "updatedAt": utc_now()})
    doc.setdefault("stats", _empty_stats())
    doc.setdefault("turns", [])
    doc.setdefault("parts", [])
    doc.setdefault("trace", {"nodes": [], "edges": [], "ui": _empty_trace_ui()})
    doc.setdefault("revision", 0)
    doc.setdefault("last_event_seq", 0)
    doc.setdefault("run_state", {"status": "idle", "session_run_id": None, "error": None})
    _sync_compatible_trace_fields(doc)


def _sync_compatible_trace_fields(doc: dict[str, Any]) -> None:
    trace = _dict(doc, "trace")
    trace.setdefault("nodes", doc.get("traceNodes") if isinstance(doc.get("traceNodes"), list) else [])
    trace.setdefault("edges", doc.get("traceEdges") if isinstance(doc.get("traceEdges"), list) else [])
    trace.setdefault("ui", doc.get("traceUI") if isinstance(doc.get("traceUI"), dict) else _empty_trace_ui())
    doc["traceNodes"] = trace.get("nodes") if isinstance(trace.get("nodes"), list) else []
    doc["traceEdges"] = trace.get("edges") if isinstance(trace.get("edges"), list) else []
    doc["traceUI"] = trace.get("ui") if isinstance(trace.get("ui"), dict) else _empty_trace_ui()


def _apply_session_run_start(doc: dict[str, Any], payload: dict[str, Any], timestamp: str, meta: dict[str, Any]) -> None:
    prompt = str(payload.get("prompt") or "")
    user_id = f"user-{meta['sessionEventSeq']}"
    turn = {
        "userMessage": {
            "id": user_id,
            "role": "user",
            "text": prompt,
            "parts": [],
            "timestamp": _timestamp_ms(timestamp),
            **meta,
        },
        "assistantMessages": [],
    }
    turns = _list(doc, "turns")
    should_initialize_title = _should_initialize_session_title(doc, len(turns))
    turns.append(turn)
    session = _dict(doc, "session")
    if prompt and should_initialize_title:
        session["title"] = prompt[:80]
        session["summary"] = prompt
        _dict(doc, "metadata")["preview"] = prompt
    _patch_stats(doc, {
        "taskText": prompt,
        "model": _string(payload, "model_id") or None,
        "mode": _string(payload, "mode") or None,
        "runStatus": "running",
    })
    _patch_run_state(doc, status="running", error=None)


def _should_initialize_session_title(doc: dict[str, Any], turns_before: int) -> bool:
    if turns_before <= 0:
        return True
    title = str(_dict(doc, "session").get("title") or "").strip()
    return title in _DEFAULT_SESSION_TITLES


def _apply_session_run_end(doc: dict[str, Any], payload: dict[str, Any], meta: dict[str, Any]) -> None:
    response = str(payload.get("response") or "")
    rendered = bool(payload.get("response_rendered"))
    if response and not rendered:
        _append_text_part(doc, response, "assistant-final", "markdown", meta)
    _settle_stale_pending_approvals(doc, status="denied", reason="session_run_closed", meta=meta)
    _patch_stats(doc, {"runStatus": "done"})
    _patch_run_state(doc, status="done", error=None)
    _dict(doc, "session")["state"] = "success"


def _apply_remote_peer_ready(doc: dict[str, Any], payload: dict[str, Any]) -> None:
    run_state = _dict(doc, "run_state")
    run_state["remote_peer"] = {
        "peer_id": _string(payload, "peer_id"),
        "session_id": _string(payload, "session_id"),
        "fingerprint": _string(payload, "fingerprint"),
        "mode": _string(payload, "mode"),
        "model": _string(payload, "model"),
        "workspace_root": _string(payload, "workspace_root"),
    }


def _apply_runtime_status(doc: dict[str, Any], payload: dict[str, Any], meta: dict[str, Any]) -> None:
    _dict(doc, "run_state")["runtime_status"] = dict(payload)
    phase = str(payload.get("phase") or "")
    if phase == "shell_queue":
        _apply_shell_queue_runtime_status(doc, payload, meta)


def _apply_shell_queue_runtime_status(doc: dict[str, Any], payload: dict[str, Any], meta: dict[str, Any]) -> None:
    tool_call_id = _tool_call_id(payload)
    if not tool_call_id:
        return
    status = str(payload.get("status") or "")
    if status == "queued":
        next_status = "pending"
    elif status == "running":
        next_status = "running"
    else:
        return
    _patch_existing_tool_part(doc, tool_call_id, {
        "status": next_status,
    }, meta)


def _apply_usage(doc: dict[str, Any], payload: dict[str, Any]) -> None:
    patch: dict[str, Any] = {}
    mapping = {
        "prompt_tokens": "tokensIn",
        "completion_tokens": "tokensOut",
        "context_tokens": "contextTokens",
        "context_window": "contextWindow",
        "max_context_tokens": "contextWindow",
        "max_output_tokens": "maxOutputTokens",
        "cache_reads": "cacheReads",
        "cache_read_tokens": "cacheReads",
        "cache_writes": "cacheWrites",
        "cache_write_tokens": "cacheWrites",
        "cost_usd": "totalCost",
        "cost_status": "costStatus",
        "model": "model",
        "mode": "mode",
        "run_status": "runStatus",
    }
    for source, target in mapping.items():
        if source in payload:
            value = payload.get(source)
            if value is not None:
                patch[target] = value
            elif target in {"cacheReads", "cacheWrites", "totalCost"}:
                patch[target] = None
    _patch_stats(doc, patch)
    if "runStatus" in patch:
        _patch_run_state(doc, status=str(patch["runStatus"]))


def _apply_tool_or_output_event(
    doc: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    if event_type == "output":
        content = str(payload.get("content") or "")
        if str(payload.get("format") or "") == "terminal":
            _append_part(doc, _part("terminal", "terminal", meta, {
                "terminalTitle": "终端输出",
                "terminalContent": content,
            }))
        else:
            _append_text_part(
                doc,
                content,
                "output",
                "markdown" if str(payload.get("format") or "") == "markdown" else "plain",
                meta,
            )
        return

    if event_type == "tool_call_start":
        _upsert_tool_part(doc, str(payload.get("tool_name") or "tool"), {
            "status": "running",
            "toolCallId": _tool_call_id(payload),
            "toolSource": _string(payload, "tool_source"),
            "toolStartedAt": payload.get("started_at"),
            "toolInput": payload.get("tool_args") if isinstance(payload.get("tool_args"), dict) else {},
        }, meta)
    elif event_type == "tool_call_protocol_error":
        code = _string(payload, "code")
        message = str(payload.get("message") or code or "Remote tool protocol error")
        _upsert_tool_part(doc, str(payload.get("tool_name") or "tool"), {
            "status": "protocol_error",
            "toolCallId": _tool_call_id(payload),
            "toolOutput": f"[{code}] {message}" if code else message,
            "toolOutputFormat": "plain",
            "toolResultMeta": {
                "code": code,
                "message": message,
                "failure_kind": _string(payload, "failure_kind"),
                "tool_diagnostics": payload.get("tool_diagnostics")
                if isinstance(payload.get("tool_diagnostics"), list)
                else [],
            },
        }, meta)
    elif event_type == "tool_call_end":
        _upsert_tool_part(doc, str(payload.get("tool_name") or "tool"), {
            "status": "returned",
            "toolCallId": _tool_call_id(payload),
            "toolSource": _string(payload, "tool_source"),
            "toolEndedAt": payload.get("ended_at"),
            "toolOutput": str(payload.get("tool_result") or ""),
            "toolOutputFormat": "plain",
            "toolResultMeta": payload.get("meta") if isinstance(payload.get("meta"), dict) else {},
        }, meta)
    elif event_type == "approval_request":
        _upsert_tool_part(doc, str(payload.get("tool_name") or "tool"), {
            "status": "awaiting_approval",
            "approvalId": _string(payload, "approval_id"),
            "approvalReason": _string(payload, "reason"),
            "approvalIntent": _string(payload, "intent"),
            "approvalSections": payload.get("sections") if isinstance(payload.get("sections"), list) else [],
            "approvalContent": _string(payload, "content"),
            "toolCallId": _tool_call_id(payload),
            "toolSource": _string(payload, "tool_source"),
            "toolInput": payload.get("tool_args") if isinstance(payload.get("tool_args"), dict) else {},
        }, meta)
    elif event_type == "approval_resolved":
        _patch_tool_part(doc, _tool_call_id(payload), _string(payload, "approval_id"), {
            "approvalDecision": _string(payload, "decision"),
            "approvalResultReason": _string(payload, "reason"),
            "status": "approved" if _string(payload, "decision") == "allow_once" else "denied",
        }, meta)


def _append_text_part(
    doc: dict[str, Any],
    content: str,
    stream_key: str,
    fmt: str,
    meta: dict[str, Any],
) -> None:
    if not content:
        return
    assistant = _ensure_assistant_message(doc)
    parts = _list_value(assistant.get("parts"))
    for index, current in enumerate(parts):
        if (
            isinstance(current, dict)
            and current.get("type") == "text"
            and current.get("textStreamKey") == stream_key
        ):
            next_part = dict(current)
            next_part.update(meta)
            next_part["text"] = f"{current.get('text') or ''}{content}"
            next_part["textFormat"] = str(current.get("textFormat") or fmt)
            parts[index] = next_part
            assistant["parts"] = parts
            assistant["text"] = f"{assistant.get('text') or ''}{content}"
            return
    part = _part(stream_key, "text", meta, {
        "text": content,
        "textFormat": fmt,
        "textStreamKey": stream_key,
    })
    assistant["parts"] = [*parts, part]
    assistant["text"] = f"{assistant.get('text') or ''}{content}"


def _upsert_reasoning_part(doc: dict[str, Any], content: str, fmt: str, meta: dict[str, Any]) -> None:
    if not content:
        return
    assistant = _ensure_assistant_message(doc)
    parts = _list_value(assistant.get("parts"))
    for index, part in enumerate(parts):
        if isinstance(part, dict) and part.get("type") == "reasoning":
            current = str(part.get("reasoningText") or "")
            part = dict(part)
            part.update(meta)
            part["reasoningText"] = f"{current}{content}"
            part["reasoningFormat"] = str(part.get("reasoningFormat") or fmt)
            part["reasoningStreamKey"] = str(part.get("reasoningStreamKey") or "reasoning-stream")
            parts[index] = part
            assistant["parts"] = parts
            return
    parts.insert(0, _part("reasoning", "reasoning", meta, {
        "reasoningText": content,
        "reasoningFormat": fmt,
        "reasoningStreamKey": "reasoning-stream",
    }))
    assistant["parts"] = parts


def _append_view_part(
    doc: dict[str, Any],
    payload: dict[str, Any],
    meta: dict[str, Any],
    *,
    title: str | None = None,
    kind: str | None = None,
) -> None:
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    _append_part(doc, _part("view", "view", meta, {
        "viewTitle": title or _string(payload, "title") or _string(payload, "message") or "结构化视图",
        "viewType": _string(payload, "view_type") or kind or _string(payload, "kind") or "view",
        "viewLevel": _string(payload, "level") or "info",
        "viewPayload": nested,
    }))


def _append_part(doc: dict[str, Any], part: dict[str, Any]) -> None:
    assistant = _ensure_assistant_message(doc)
    assistant["parts"] = [*_list_value(assistant.get("parts")), part]
    _list(doc, "parts").append(part)


def _upsert_tool_part(
    doc: dict[str, Any],
    tool_name: str,
    patch: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    tool_call_id = str(patch.get("toolCallId") or "")
    if not tool_call_id:
        return
    assistant = _ensure_assistant_message(doc)
    parts = _list_value(assistant.get("parts"))
    index = next((
        idx for idx, part in enumerate(parts)
        if isinstance(part, dict) and part.get("type") == "tool" and part.get("toolCallId") == tool_call_id
    ), -1)
    current = parts[index] if index >= 0 and isinstance(parts[index], dict) else {
        "id": f"tool-{tool_call_id}",
        "type": "tool",
        "tool": tool_name,
        "toolCallId": tool_call_id,
        "status": "running",
        "toolOutput": "",
    }
    next_part = {**current, **_defined(patch), **meta, "type": "tool", "tool": tool_name, "toolCallId": tool_call_id}
    if index >= 0:
        parts[index] = next_part
    else:
        parts.append(next_part)
    assistant["parts"] = parts


def _patch_tool_part(
    doc: dict[str, Any],
    tool_call_id: str,
    approval_id: str,
    patch: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    assistant = _ensure_assistant_message(doc)
    parts = []
    for part in _list_value(assistant.get("parts")):
        if not isinstance(part, dict) or part.get("type") != "tool":
            parts.append(part)
            continue
        if tool_call_id and part.get("toolCallId") != tool_call_id:
            parts.append(part)
            continue
        if not tool_call_id and approval_id and part.get("approvalId") != approval_id:
            parts.append(part)
            continue
        parts.append({**part, **_defined(patch), **meta})
    assistant["parts"] = parts


def _patch_existing_tool_part(
    doc: dict[str, Any],
    tool_call_id: str,
    patch: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    if not tool_call_id:
        return
    for turn in _list(doc, "turns"):
        if not isinstance(turn, dict):
            continue
        for message in _list_value(turn.get("assistantMessages")):
            if not isinstance(message, dict):
                continue
            parts = []
            changed = False
            for part in _list_value(message.get("parts")):
                if (
                    isinstance(part, dict)
                    and part.get("type") == "tool"
                    and part.get("toolCallId") == tool_call_id
                ):
                    parts.append({**part, **_defined(patch), **meta})
                    changed = True
                    continue
                parts.append(part)
            if changed:
                message["parts"] = parts
                return


def _settle_stale_pending_approvals(
    doc: dict[str, Any],
    *,
    status: str,
    reason: str,
    meta: dict[str, Any],
) -> None:
    for turn in _list(doc, "turns"):
        if not isinstance(turn, dict):
            continue
        for message in _list_value(turn.get("assistantMessages")):
            if not isinstance(message, dict):
                continue
            parts = []
            for part in _list_value(message.get("parts")):
                if (
                    isinstance(part, dict)
                    and part.get("type") == "tool"
                    and part.get("status") == "awaiting_approval"
                    and part.get("approvalId")
                    and not part.get("approvalDecision")
                ):
                    parts.append({
                        **part,
                        **meta,
                        "status": status,
                        "approvalDecision": "deny_once",
                        "approvalResultReason": reason,
                    })
                    continue
                parts.append(part)
            message["parts"] = parts


def _find_tool_part(doc: dict[str, Any], tool_call_id: str) -> dict[str, Any] | None:
    if not tool_call_id:
        return None
    for turn in _list(doc, "turns"):
        if not isinstance(turn, dict):
            continue
        for message in _list_value(turn.get("assistantMessages")):
            if not isinstance(message, dict):
                continue
            for part in _list_value(message.get("parts")):
                if isinstance(part, dict) and part.get("type") == "tool" and part.get("toolCallId") == tool_call_id:
                    return part
    return None


def _ensure_assistant_message(doc: dict[str, Any]) -> dict[str, Any]:
    turns = _list(doc, "turns")
    if not turns:
        turns.append({
            "userMessage": {
                "id": "user-0",
                "role": "user",
                "text": "",
                "parts": [],
                "timestamp": _timestamp_ms(utc_now()),
            },
            "assistantMessages": [],
        })
    turn = turns[-1]
    if not isinstance(turn, dict):
        turn = {"userMessage": {"id": "user-0", "role": "user", "text": "", "parts": []}, "assistantMessages": []}
        turns[-1] = turn
    messages = turn.setdefault("assistantMessages", [])
    if not isinstance(messages, list):
        messages = []
        turn["assistantMessages"] = messages
    if not messages:
        messages.append({
            "id": f"assistant-{len(turns) - 1}",
            "role": "assistant",
            "text": "",
            "parts": [],
            "timestamp": _timestamp_ms(utc_now()),
            "traceNodeKind": "assistant_message",
            "traceNodeStatus": "active",
        })
    message = messages[-1]
    if not isinstance(message, dict):
        message = {"id": f"assistant-{len(turns) - 1}", "role": "assistant", "text": "", "parts": []}
        messages[-1] = message
    message.setdefault("parts", [])
    return message


def _patch_stats(doc: dict[str, Any], patch: dict[str, Any]) -> None:
    stats = _dict(doc, "stats")
    for key, value in patch.items():
        if value is not None:
            stats[key] = value


def _patch_run_state(doc: dict[str, Any], *, status: str, error: str | None = None) -> None:
    run_state = _dict(doc, "run_state")
    run_state["status"] = status
    run_state["error"] = error
    _dict(doc, "stats")["runStatus"] = status


def _part(prefix: str, kind: str, meta: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{prefix}-{meta.get('sessionEventSeq')}",
        "type": kind,
        **extra,
        **meta,
    }


def _tool_call_id(payload: dict[str, Any]) -> str:
    return _string(payload, "tool_call_id") or _string(payload, "toolCallId")


def _is_memory_payload(payload: dict[str, Any]) -> bool:
    return payload.get("context_kind") == "memory_injection" or payload.get("schema") == "memory_context.v1"


def _dict(value: dict[str, Any], key: str) -> dict[str, Any]:
    current = value.get(key)
    if not isinstance(current, dict):
        current = {}
        value[key] = current
    return current


def _list(value: dict[str, Any], key: str) -> list[Any]:
    current = value.get(key)
    if not isinstance(current, list):
        current = []
        value[key] = current
    return current


def _list_value(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _string(value: dict[str, Any] | None, key: str) -> str:
    if not isinstance(value, dict):
        return ""
    item = value.get(key)
    return str(item) if item is not None else ""


def _defined(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _timestamp_ms(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return int(datetime.now(timezone.utc).timestamp() * 1000)

"""Session document reducer used as the UI history authority."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from reuleauxcoder.domain.session.locale import session_notice_text

_DEFAULT_SESSION_TITLES = frozenset({"", "新会话"})
_SHELL_OUTPUT_MAX_CHARS = 20000
_SHELL_OUTPUT_TRUNCATION_MARKER = "\n... 输出过长，已截断早期内容，保留最近输出 ...\n"


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
    event_seq = max(0, int(session_event_seq or 0))
    timestamp = created_at or utc_now()
    meta = {
        "eventKey": f"session:{session_id}:{event_seq}",
        "sessionEventSeq": event_seq,
    }
    raw_event_refs = _raw_event_refs_from_payload(payload)
    if raw_event_refs:
        meta["rawEventRefs"] = raw_event_refs
    if session_run_id:
        meta["sessionRunId"] = session_run_id
        payload.setdefault("session_run_id", session_run_id)

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
    elif event_type == "reasoning_delta":
        _upsert_thinking_part(
            doc,
            str(payload.get("content") or ""),
            meta,
        )
    elif event_type == "reasoning_message":
        _finalize_reasoning_part(
            doc,
            str(payload.get("content") or ""),
            str(payload.get("summary") or ""),
            "plain" if str(payload.get("format") or "") == "plain" else "markdown",
            meta,
        )
    elif event_type == "assistant_delta":
        _append_text_part(
            doc,
            str(payload.get("content") or ""),
            "assistant-stream",
            "markdown",
            meta,
            streaming=True,
        )
    elif event_type == "assistant_message":
        _append_text_part(
            doc,
            str(payload.get("content") or ""),
            "assistant-message",
            "markdown",
            meta,
            replace_stream_key="assistant-stream",
            append=False,
        )
    elif event_type == "session_run_end":
        _apply_session_run_end(doc, payload, meta)
    elif event_type in {
        "output",
        "tool_call_delta",
        "tool_call_stream",
        "tool_call_start",
        "tool_call_end",
        "tool_call_protocol_error",
        "approval_request",
        "approval_resolved",
    }:
        _apply_tool_or_output_event(doc, event_type, payload, meta)
    elif event_type in {
        "file_change_started",
        "file_change_patch_updated",
        "file_change_approval_requested",
        "file_change_approval_resolved",
        "file_change_completed",
        "turn_diff_updated",
    }:
        _apply_file_change_event(doc, event_type, payload, meta)
    elif event_type in {
        "document_draft_started",
        "document_draft_commit_requested",
        "document_draft_committed",
        "document_draft_failed",
        "document_draft_cancelled",
    }:
        _apply_document_draft_event(doc, event_type, payload, meta)
    elif event_type == "runtime_status":
        _apply_runtime_status(doc, payload, meta)
    elif event_type == "lifecycle_hook":
        phase = _string(payload, "phase") or "result"
        event_name = _string(payload, "event_name") or "LifecycleHook"
        display_name = _string(payload, "display_name")
        title = display_name or event_name
        if phase == "dispatch_start":
            title = f"{title} started"
        elif phase == "dispatch_failed":
            title = f"{title} failed"
        message = _string(payload, "message")
        artifacts = payload.get("artifacts")
        _append_part(doc, _part("lifecycle-hook", "ui_event", meta, {
            "kind": "lifecycle_hook",
            "level": _string(payload, "level") or (
                "error" if _string(payload, "error") else "info"
            ),
            "title": title,
            **({"message": message} if message else {}),
            **({"artifacts": list(artifacts)} if isinstance(artifacts, list) else {}),
            "payload": payload,
        }))
    elif event_type == "memory_context" or (
        event_type == "context_event" and _is_memory_payload(payload)
    ):
        _append_part(doc, _part("memory", "memory_context", meta, {
            "title": _string(payload, "title") or "注入记忆",
            "payload": payload,
        }))
    elif event_type == "context_event":
        _append_part(doc, _part("context", "context_event", meta, {
            "title": _string(payload, "message") or _string(payload, "phase") or "上下文事件",
            "payload": payload,
        }))
    elif event_type == "workflow_step":
        _append_part(doc, _part("workflow-step", "workflow_step", meta, {
            "lane": "process",
            "workflow": _string(payload, "workflow") or "workflow",
            "stage": _string(payload, "stage") or _string(payload, "phase") or "step",
            "status": _string(payload, "status") or "running",
            "title": _string(payload, "title") or _string(payload, "message"),
            "summary": _string(payload, "summary"),
            "details": payload.get("details") if isinstance(payload.get("details"), dict) else {},
            "payload": payload,
        }))
    elif event_type == "workflow_artifact":
        _append_part(doc, _part("workflow-artifact", "workflow_artifact", meta, {
            "lane": "primary",
            "workflow": _string(payload, "workflow") or "workflow",
            "artifactType": _string(payload, "artifact_type") or _string(payload, "artifactType") or "artifact",
            "title": _string(payload, "title") or _string(payload, "message"),
            "summary": _string(payload, "summary"),
            "artifact": payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {},
            "payload": payload,
        }))
    elif event_type == "workflow_decision":
        _append_part(doc, _part("workflow-decision", "workflow_decision", meta, {
            "lane": "primary",
            "workflow": _string(payload, "workflow") or "workflow",
            "decisionType": _string(payload, "decision_type") or _string(payload, "decisionType") or "decision",
            "status": _string(payload, "status") or "pending",
            "title": _string(payload, "title") or _string(payload, "intent") or _string(payload, "message"),
            "summary": _string(payload, "summary") or _string(payload, "content"),
            "review": payload.get("review") if isinstance(payload.get("review"), dict) else {},
            "actions": payload.get("actions") if isinstance(payload.get("actions"), list) else [],
            "approvalId": _string(payload, "approval_id"),
            "toolCallId": _string(payload, "tool_call_id"),
            "payload": payload,
        }))
    elif event_type == "workflow_result":
        _append_part(doc, _part("workflow-result", "workflow_result", meta, {
            "lane": "primary",
            "workflow": _string(payload, "workflow") or "workflow",
            "resultType": _string(payload, "result_type") or _string(payload, "resultType"),
            "status": _string(payload, "status") or "done",
            "title": _string(payload, "title") or _string(payload, "message"),
            "summary": _string(payload, "summary"),
            "result": payload.get("result") if isinstance(payload.get("result"), dict) else {},
            "payload": payload,
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
            "kind": _string(payload, "kind") or event_type.replace("_event", ""),
            "level": _string(payload, "level") or "info",
            "title": _string(payload, "title") or _string(payload, "message") or "运行事件",
            "payload": payload,
        }))
    elif event_type == "provider_stream_interrupted":
        _append_notice_part(
            doc,
            "warning",
            _session_event_message(
                doc,
                payload,
                "provider_stream_interrupted.recovering",
                "模型输出流中断，正在尝试恢复。",
            ),
            "stream-recovery",
            "plain",
            meta,
        )
    elif event_type == "session_run_interrupted":
        message = _session_event_message(
            doc,
            payload,
            "provider_stream.interrupted_can_continue",
            "模型输出流中断，可继续生成。",
        )
        prefix = _session_event_message(
            doc,
            {"locale": payload.get("locale")} if "locale" in payload else {},
            "provider_stream.interrupted_prefix",
            "输出中断：",
        )
        _append_notice_part(
            doc,
            "warning",
            f"{prefix}{message}",
            "stream-interrupted",
            "plain",
            meta,
        )
        _apply_terminal_session_run_event(
            doc,
            status="interrupted",
            session_state="active",
            error=message,
            meta=meta,
        )
    elif event_type in {"error", "session_run_failed"}:
        message = _session_event_message(doc, payload, "", "unknown error")
        _settle_stale_pending_approvals(
            doc,
            status="denied",
            reason=message,
            meta=meta,
        )
        if event_type == "error" or not _has_notice_level(doc, "error"):
            _append_notice_part(doc, "error", f"错误：{message}", "error", "plain", meta)
        _apply_terminal_session_run_event(
            doc,
            status="error",
            session_state="error",
            error=message,
            meta=meta,
        )
    elif event_type == "session_run_cancel_requested":
        _patch_run_state(
            doc,
            status="stopping",
            error=str(payload.get("reason") or "session_run_cancel_requested"),
        )
    elif event_type == "session_run_cancelled":
        reason = str(payload.get("reason") or "session_run_cancelled")
        _settle_stale_pending_approvals(
            doc,
            status="cancelled",
            reason=reason,
            meta=meta,
        )
        _append_notice_part(doc, "info", "已取消当前请求。", "cancelled", "plain", meta)
        _apply_terminal_session_run_event(
            doc,
            status="cancelled",
            session_state="cancelled",
            error=reason,
            meta=meta,
        )

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
    locale = _string(payload, "locale")
    if locale:
        _dict(doc, "metadata")["locale"] = locale
    user_id = f"user-{meta['sessionEventSeq']}"
    turn = {
        "userMessage": {
            "id": user_id,
            "role": "user",
            "text": prompt,
            "parts": [],
            "timestamp": _timestamp_ms(timestamp),
            **_public_meta(meta),
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


def _session_event_message(
    doc: dict[str, Any],
    payload: dict[str, Any],
    default_key: str,
    default: str,
) -> str:
    message = str(payload.get("message") or "").strip()
    if message:
        return message
    message_key = _string(payload, "message_key") or default_key
    if message_key:
        locale = _string(payload, "locale") or _string(_dict(doc, "metadata"), "locale")
        return session_notice_text(locale, message_key, default)
    return default


def _apply_session_run_end(doc: dict[str, Any], payload: dict[str, Any], meta: dict[str, Any]) -> None:
    response = str(payload.get("response") or "")
    rendered = bool(payload.get("response_rendered"))
    status = str(payload.get("status") or "").strip()
    _settle_stale_pending_approvals(doc, status="denied", reason="session_run_closed", meta=meta)
    if response and not rendered:
        _append_text_part(
            doc,
            response,
            "assistant-message",
            "markdown",
            meta,
            replace_stream_key="assistant-stream",
            append=False,
        )
    if status and status != "done":
        _apply_terminal_session_run_event(
            doc,
            status=status,
            session_state=str(payload.get("session_state") or status),
            error=str(payload.get("error") or response or status),
            meta=meta,
        )
        return
    _apply_terminal_session_run_event(
        doc,
        status="done",
        session_state="success",
        error=None,
        meta=meta,
    )


def _apply_terminal_session_run_event(
    doc: dict[str, Any],
    *,
    status: str,
    session_state: str,
    error: str | None,
    meta: dict[str, Any],
) -> None:
    _finalize_run_transcript_items(doc, status)
    _patch_run_state(doc, status=status, error=error)
    _dict(doc, "session")["state"] = session_state


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
                "title": "终端输出",
                "content": content,
            }))
        else:
            _append_part(doc, _part("output", "notice", meta, {
                "level": _notice_level(payload),
                "text": content,
                "format": "markdown" if str(payload.get("format") or "") == "markdown" else "plain",
            }))
        return

    if event_type == "tool_call_delta":
        index = _number(payload, "index") or 0
        tool_call_id = _tool_call_id(payload) or f"preparing:{_string(payload, 'session_run_id') or 'pending'}:{index}"
        _upsert_tool_part(doc, str(payload.get("tool_name") or "tool"), {
            "status": "preparing",
            "toolCallId": tool_call_id,
            "source": _string(payload, "tool_source"),
            "startedAt": payload.get("started_at"),
            "input": {"arguments_preview": _string(payload, "arguments_preview")}
            if _string(payload, "arguments_preview")
            else None,
            "preparingIndex": index,
        }, meta)
    elif event_type == "tool_call_stream":
        tool_call_id = _tool_call_id(payload)
        if not tool_call_id:
            return
        tool_name = str(payload.get("tool_name") or "tool")
        current = _find_tool_part(doc, tool_call_id) or {}
        content = _strip_ansi(str(payload.get("content") or ""))
        source = _string(payload, "tool_source") or str(current.get("source") or "")
        stream = _normalize_tool_stream(str(payload.get("stream") or "stdout"))
        output_chunks, output_truncated = _append_output_chunk(_list_value(current.get("outputChunks")), stream, content)
        _upsert_tool_part(doc, tool_name, {
            "status": "running",
            "toolCallId": tool_call_id,
            "source": source or None,
            "stream": stream,
            "output": _build_output_text(output_chunks),
            "outputChunks": output_chunks,
            "outputTruncated": output_truncated,
            "outputFormat": _tool_output_format(payload, tool_name, source),
        }, meta)
    elif event_type == "tool_call_start":
        _upsert_tool_part(doc, str(payload.get("tool_name") or "tool"), {
            "status": "running",
            "toolCallId": _tool_call_id(payload),
            "source": _string(payload, "tool_source"),
            "startedAt": payload.get("started_at"),
            "input": payload.get("tool_args") if isinstance(payload.get("tool_args"), dict) else {},
            "preparingIndex": _number(payload, "index"),
            "resultMeta": {},
        }, meta)
    elif event_type == "tool_call_protocol_error":
        code = _string(payload, "code")
        message = str(payload.get("message") or code or "Remote tool protocol error")
        _upsert_tool_part(doc, str(payload.get("tool_name") or "tool"), {
            "status": "protocol_error",
            "toolCallId": _tool_call_id(payload),
            "output": f"[{code}] {message}" if code else message,
            "outputFormat": "plain",
            "resultMeta": {
                "code": code,
                "message": message,
                "failure_kind": _string(payload, "failure_kind"),
                "tool_diagnostics": payload.get("tool_diagnostics")
                if isinstance(payload.get("tool_diagnostics"), list)
                else [],
            },
        }, meta)
    elif event_type == "tool_call_end":
        tool_call_id = _tool_call_id(payload)
        tool_name = str(payload.get("tool_name") or "tool")
        current = _find_tool_part(doc, tool_call_id) or {}
        source = _string(payload, "tool_source") or str(current.get("source") or "")
        final_output = str(payload.get("tool_result") or "")
        is_shell = _is_shell_tool(tool_name, source)
        output_chunks = _list_value(current.get("outputChunks"))
        output = (
            _reconcile_final_output(str(current.get("output") or ""), final_output, bool(output_chunks))
            if is_shell
            else final_output
        )
        if is_shell and not output_chunks:
            output_chunks = _shell_chunks_from_text(output)
        result_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        _upsert_tool_part(doc, tool_name, {
            "status": _tool_end_status(result_meta),
            "toolCallId": tool_call_id,
            "source": source or None,
            "endedAt": payload.get("ended_at"),
            "output": output,
            "outputFormat": _tool_output_format(payload, tool_name, source),
            "outputChunks": output_chunks if is_shell else None,
            "finalOutput": final_output if is_shell else None,
            "preparingIndex": _number(payload, "index"),
            "resultMeta": result_meta,
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
            "source": _string(payload, "tool_source"),
            "input": payload.get("tool_args") if isinstance(payload.get("tool_args"), dict) else {},
            "resultMeta": _approval_request_result_meta(payload),
        }, meta)
    elif event_type == "approval_resolved":
        _patch_tool_part(doc, _tool_call_id(payload), _string(payload, "approval_id"), {
            "approvalDecision": _string(payload, "decision"),
            "approvalResultReason": _string(payload, "reason"),
            "status": "approved" if _string(payload, "decision") == "allow_once" else "denied",
        }, meta)
        _patch_workflow_decision_part(doc, _tool_call_id(payload), _string(payload, "approval_id"), {
            "decision": _string(payload, "decision"),
            "resultReason": _string(payload, "reason"),
            "status": "approved" if _string(payload, "decision") == "allow_once" else "denied",
        }, meta)


def _apply_file_change_event(
    doc: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    item_id = _string(payload, "item_id") or _string(payload, "itemId")
    if not item_id:
        item_id = f"file-change-{meta.get('sessionRunId') or meta.get('sessionEventSeq')}"
    existing = _find_file_change_part(doc, item_id) or {}
    changes = (
        [dict(item) for item in payload.get("changes", []) if isinstance(item, dict)]
        if isinstance(payload.get("changes"), list)
        else _list_value(existing.get("changes"))
    )
    stats = _file_change_diff_stats(changes)
    patch: dict[str, Any] = {
        "itemId": item_id,
        "toolCallId": _tool_call_id(payload) or existing.get("toolCallId"),
        "status": _file_change_status(payload, event_type, existing),
        "changes": changes,
        "diff": _combined_file_change_diff(changes) or existing.get("diff"),
        "addedLines": stats["added"],
        "removedLines": stats["removed"],
        "path": _primary_file_change_path(changes) or existing.get("path"),
        "updatedAt": payload.get("updated_at") or payload.get("updatedAt"),
        "durationMs": payload.get("duration_ms") or payload.get("durationMs"),
        "error": _string(payload, "error") or existing.get("error"),
    }
    if event_type == "file_change_patch_updated":
        patch["patchPreview"] = _string(payload, "patch_preview") or existing.get("patchPreview")
    elif event_type == "file_change_approval_requested":
        patch["approvalId"] = _string(payload, "approval_id")
        patch["approvalReason"] = _string(payload, "reason")
    elif event_type == "file_change_approval_resolved":
        patch["approvalId"] = _string(payload, "approval_id") or existing.get("approvalId")
        patch["approvalDecision"] = _string(payload, "decision")
        patch["approvalResultReason"] = _string(payload, "reason")

    _upsert_file_change_part(doc, item_id, patch, meta)


def _upsert_file_change_part(
    doc: dict[str, Any],
    item_id: str,
    patch: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    assistant = _ensure_assistant_message(doc)
    parts = []
    found = False
    for part in _list_value(assistant.get("parts")):
        if isinstance(part, dict) and part.get("type") == "file_change" and part.get("itemId") == item_id:
            next_part = {**part, **_defined(patch), **_public_meta(meta)}
            raw_event_refs = _merge_raw_event_refs(part.get("rawEventRefs"), meta.get("rawEventRefs"))
            if raw_event_refs:
                next_part["rawEventRefs"] = raw_event_refs
            parts.append(next_part)
            found = True
            continue
        parts.append(part)
    if not found:
        parts.append(
            _part(
                item_id,
                "file_change",
                meta,
                {
                    "title": "文件变更",
                    **_defined(patch),
                },
            )
        )
    assistant["parts"] = parts


def _find_file_change_part(doc: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    if not item_id:
        return None
    for turn in _list(doc, "turns"):
        if not isinstance(turn, dict):
            continue
        for message in _list_value(turn.get("assistantMessages")):
            if not isinstance(message, dict):
                continue
            for part in _list_value(message.get("parts")):
                if (
                    isinstance(part, dict)
                    and part.get("type") == "file_change"
                    and part.get("itemId") == item_id
                ):
                    return part
    return None


def _file_change_status(
    payload: dict[str, Any],
    event_type: str,
    existing: dict[str, Any],
) -> str:
    raw = _string(payload, "status")
    allowed = {"in_progress", "completed", "failed", "declined", "cancelled"}
    if raw in allowed:
        return raw
    if event_type == "file_change_approval_requested":
        return "in_progress"
    if event_type == "file_change_approval_resolved":
        return "declined" if _string(payload, "decision") != "allow_once" else "in_progress"
    if event_type == "file_change_completed":
        return "completed"
    return str(existing.get("status") or "in_progress")


def _primary_file_change_path(changes: list[Any]) -> str:
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = str(change.get("move_path") or change.get("path") or "")
        if path:
            return path
    return ""


def _combined_file_change_diff(changes: list[Any]) -> str:
    diffs = [
        str(change.get("diff") or "")
        for change in changes
        if isinstance(change, dict) and str(change.get("diff") or "")
    ]
    return "\n".join(diffs).strip()


def _file_change_diff_stats(changes: list[Any]) -> dict[str, int]:
    added = 0
    removed = 0
    for diff in _combined_file_change_diff(changes).splitlines():
        if diff.startswith("+++") or diff.startswith("---"):
            continue
        if diff.startswith("+"):
            added += 1
        elif diff.startswith("-"):
            removed += 1
    return {"added": added, "removed": removed}


def _apply_document_draft_event(
    doc: dict[str, Any],
    event_type: str,
    payload: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    draft_id = _string(payload, "draft_id") or _string(payload, "draftId")
    if not draft_id:
        draft_id = f"draft-{meta.get('sessionRunId') or meta.get('sessionEventSeq')}"
    patch = {
        "draftId": draft_id,
        "targetPath": _string(payload, "target_path") or _string(payload, "targetPath") or None,
        "title": _string(payload, "title") or None,
        "format": _string(payload, "format") or None,
        "status": _document_draft_status(payload, event_type),
        "itemId": _string(payload, "item_id") or _string(payload, "itemId") or None,
        "approvalId": _string(payload, "approval_id") or _string(payload, "approvalId") or None,
        "error": _string(payload, "error") or None,
        "reason": _string(payload, "reason") or None,
    }
    _upsert_document_draft_part(doc, draft_id, patch, meta)


def _upsert_document_draft_part(
    doc: dict[str, Any],
    draft_id: str,
    patch: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    assistant = _ensure_assistant_message(doc)
    parts = []
    found = False
    for part in _list_value(assistant.get("parts")):
        if isinstance(part, dict) and part.get("type") == "document_draft" and part.get("draftId") == draft_id:
            parts.append({**part, **_defined(patch), **_public_meta(meta)})
            found = True
            continue
        parts.append(part)
    if not found:
        parts.append(
            _part(
                draft_id,
                "document_draft",
                meta,
                {
                    "title": "文档草稿",
                    **_defined(patch),
                },
            )
        )
    assistant["parts"] = parts


def _document_draft_status(payload: dict[str, Any], event_type: str) -> str:
    raw = _string(payload, "status")
    allowed = {"declared", "streaming", "committing", "committed", "cancelled", "failed"}
    if raw in allowed:
        return raw
    mapping = {
        "document_draft_started": "streaming",
        "document_draft_commit_requested": "committing",
        "document_draft_committed": "committed",
        "document_draft_failed": "failed",
        "document_draft_cancelled": "cancelled",
    }
    return mapping.get(event_type, "streaming")


def _append_text_part(
    doc: dict[str, Any],
    content: str,
    stream_key: str,
    fmt: str,
    meta: dict[str, Any],
    *,
    streaming: bool = False,
    replace_stream_key: str | None = None,
    append: bool = True,
) -> None:
    if not content:
        return
    assistant = _ensure_assistant_message(doc)
    parts = _list_value(assistant.get("parts"))
    for index, current in enumerate(parts):
        if (
            isinstance(current, dict)
            and current.get("type") in {"assistant_text", "text"}
            and current.get("streamKey", current.get("textStreamKey")) in {stream_key, replace_stream_key}
        ):
            next_part = dict(current)
            next_part.update(_public_meta(meta))
            current_content = str(current.get("markdown", current.get("text")) or "")
            next_part["type"] = "assistant_text"
            next_part["markdown"] = f"{current_content}{content}" if append else content
            next_part["format"] = str(current.get("format", current.get("textFormat")) or fmt)
            next_part["streaming"] = streaming
            next_part["streamKey"] = stream_key
            raw_event_refs = _merge_raw_event_refs(current.get("rawEventRefs"), meta.get("rawEventRefs"))
            if raw_event_refs:
                next_part["rawEventRefs"] = raw_event_refs
            next_part.pop("text", None)
            next_part.pop("textFormat", None)
            next_part.pop("textStreamKey", None)
            parts[index] = next_part
            assistant["parts"] = parts
            _refresh_assistant_text(assistant)
            return
    part = _part(stream_key, "assistant_text", meta, {
        "markdown": content,
        "format": fmt,
        "streaming": streaming,
        "streamKey": stream_key,
    })
    if stream_key == "assistant-stream":
        part["id"] = f"assistant-stream-{meta.get('sessionRunId') or meta.get('sessionEventSeq')}"
    assistant["parts"] = [*parts, part]
    _refresh_assistant_text(assistant)


def _upsert_thinking_part(doc: dict[str, Any], content: str, meta: dict[str, Any]) -> None:
    if not content:
        return
    assistant = _ensure_assistant_message(doc)
    parts = _list_value(assistant.get("parts"))
    for index, part in enumerate(parts):
        if isinstance(part, dict) and part.get("type") == "thinking" and part.get("streamKey") == "reasoning-stream":
            current = str(part.get("raw") or "")
            part = dict(part)
            part.update(_public_meta(meta))
            part["title"] = "正在思考"
            part["active"] = True
            part["raw"] = f"{current}{content}"
            part["streamKey"] = "reasoning-stream"
            raw_event_refs = _merge_raw_event_refs(part.get("rawEventRefs"), meta.get("rawEventRefs"))
            if raw_event_refs:
                part["rawEventRefs"] = raw_event_refs
            parts[index] = part
            assistant["parts"] = parts
            return
    part = _part("thinking", "thinking", meta, {
        "title": "正在思考",
        "active": True,
        "raw": content,
        "streamKey": "reasoning-stream",
    })
    part["id"] = f"thinking-{meta.get('sessionRunId') or meta.get('sessionEventSeq')}"
    parts.append(part)
    assistant["parts"] = parts


def _finalize_reasoning_part(
    doc: dict[str, Any],
    content: str,
    summary: str,
    fmt: str,
    meta: dict[str, Any],
) -> None:
    if not content and not summary:
        return
    assistant = _ensure_assistant_message(doc)
    parts = _list_value(assistant.get("parts"))
    for index, part in enumerate(parts):
        if isinstance(part, dict) and part.get("type") == "thinking" and part.get("streamKey") == "reasoning-stream":
            parts[index] = _part_from_existing(part, "reasoning", meta, {
                "summary": summary or None,
                "raw": content or summary,
                "format": fmt,
            })
            assistant["parts"] = parts
            return
    for index, part in enumerate(parts):
        if isinstance(part, dict) and part.get("type") == "reasoning":
            parts[index] = _part_from_existing(part, "reasoning", meta, {
                "summary": summary or part.get("summary"),
                "raw": content or summary,
                "format": fmt,
            })
            assistant["parts"] = parts
            return
    parts.append(_part("reasoning", "reasoning", meta, {
        "summary": summary or None,
        "raw": content or summary,
        "format": fmt,
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
        "title": title or _string(payload, "title") or _string(payload, "message") or "结构化视图",
        "viewType": _string(payload, "view_type") or kind or _string(payload, "kind") or "view",
        "level": _string(payload, "level") or "info",
        "payload": nested,
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
    preparing_index = patch.get("preparingIndex")
    index = next((
        idx for idx, part in enumerate(parts)
        if isinstance(part, dict) and part.get("type") == "tool" and part.get("toolCallId") == tool_call_id
    ), -1)
    if index < 0 and preparing_index is not None:
        index = next((
            idx for idx, part in enumerate(parts)
            if (
                isinstance(part, dict)
                and part.get("type") == "tool"
                and part.get("status") == "preparing"
                and part.get("preparingIndex") == preparing_index
            )
        ), -1)
    current = parts[index] if index >= 0 and isinstance(parts[index], dict) else {
        "id": f"tool-{tool_call_id}",
        "type": "tool",
        "tool": tool_name,
        "toolCallId": tool_call_id,
        "status": "running",
        "output": "",
    }
    raw_event_refs = _merge_raw_event_refs(
        current.get("rawEventRefs"),
        patch.get("rawEventRefs"),
        meta.get("rawEventRefs"),
    )
    next_part = {**current, **_defined(patch), **_public_meta(meta), "type": "tool", "tool": tool_name, "toolCallId": tool_call_id}
    if raw_event_refs:
        next_part["rawEventRefs"] = raw_event_refs
    if index >= 0:
        parts[index] = next_part
    else:
        insert_index = _tool_part_insert_index(parts, preparing_index)
        if insert_index is None:
            parts.append(next_part)
        else:
            parts.insert(insert_index, next_part)
    assistant["parts"] = parts


def _tool_end_status(result_meta: dict[str, Any]) -> str:
    failure_kind = str(result_meta.get("failure_kind") or "").strip()
    if failure_kind:
        return "error"
    diagnostics = result_meta.get("tool_diagnostics")
    if isinstance(diagnostics, list):
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict):
                continue
            if str(diagnostic.get("severity") or "").lower() == "error":
                return "error"
            kind = str(diagnostic.get("kind") or "").strip()
            if kind in {
                "tool_result_error",
                "approval_denied",
                "tool_protocol_error",
                "chat_terminal_error",
            }:
                return "error"
    return "returned"


_APPROVAL_LIFECYCLE_HOOK_FIELDS = (
    "hook_id",
    "display_name",
    "source",
    "handler_type",
    "decision",
    "reason",
)


def _approval_request_result_meta(payload: dict[str, Any]) -> dict[str, Any] | None:
    result_meta = (
        dict(payload.get("meta")) if isinstance(payload.get("meta"), dict) else {}
    )
    if isinstance(payload.get("permission"), dict):
        result_meta["permission"] = dict(payload["permission"])
    lifecycle_event = _string(payload, "lifecycle_event")
    if lifecycle_event:
        result_meta["lifecycle_event"] = lifecycle_event
    lifecycle_hooks = _approval_lifecycle_hooks(payload.get("lifecycle_hooks"))
    if lifecycle_hooks:
        result_meta["lifecycle_hooks"] = lifecycle_hooks
    return result_meta or None


def _approval_lifecycle_hooks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    hooks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        public_item = {
            field: item[field]
            for field in _APPROVAL_LIFECYCLE_HOOK_FIELDS
            if item.get(field) is not None
        }
        if public_item:
            hooks.append(public_item)
    return hooks


def _tool_part_insert_index(
    parts: list[Any],
    preparing_index: Any,
) -> int | None:
    if not isinstance(preparing_index, (int, float)):
        return None
    for idx, part in enumerate(parts):
        if not isinstance(part, dict) or part.get("type") != "tool":
            continue
        existing = part.get("preparingIndex")
        if isinstance(existing, (int, float)) and existing > preparing_index:
            return idx
    return None


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
        next_part = {**part, **_defined(patch), **_public_meta(meta)}
        raw_event_refs = _merge_raw_event_refs(part.get("rawEventRefs"), patch.get("rawEventRefs"), meta.get("rawEventRefs"))
        if raw_event_refs:
            next_part["rawEventRefs"] = raw_event_refs
        parts.append(next_part)
    assistant["parts"] = parts


def _patch_workflow_decision_part(
    doc: dict[str, Any],
    tool_call_id: str,
    approval_id: str,
    patch: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    assistant = _ensure_assistant_message(doc)
    parts = []
    for part in _list_value(assistant.get("parts")):
        if not isinstance(part, dict) or part.get("type") != "workflow_decision":
            parts.append(part)
            continue
        if tool_call_id and part.get("toolCallId") != tool_call_id:
            parts.append(part)
            continue
        if not tool_call_id and approval_id and part.get("approvalId") != approval_id:
            parts.append(part)
            continue
        next_part = {**part, **_defined(patch), **_public_meta(meta)}
        raw_event_refs = _merge_raw_event_refs(part.get("rawEventRefs"), patch.get("rawEventRefs"), meta.get("rawEventRefs"))
        if raw_event_refs:
            next_part["rawEventRefs"] = raw_event_refs
        parts.append(next_part)
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
                    parts.append({**part, **_defined(patch), **_public_meta(meta)})
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
                        **_public_meta(meta),
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
        **_public_meta(meta),
    }


def _public_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in meta.items() if key != "sessionRunId"}


def _part_from_existing(
    current: dict[str, Any],
    kind: str,
    meta: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any]:
    part = {
        "id": str(current.get("id") or f"{kind}-{meta.get('sessionEventSeq')}"),
        "type": kind,
        **_defined(extra),
        **_public_meta(meta),
    }
    raw_event_refs = _merge_raw_event_refs(current.get("rawEventRefs"), meta.get("rawEventRefs"))
    if raw_event_refs:
        part["rawEventRefs"] = raw_event_refs
    return part


def _refresh_assistant_text(assistant: dict[str, Any]) -> None:
    text_parts = []
    for part in _list_value(assistant.get("parts")):
        if isinstance(part, dict) and part.get("type") == "assistant_text":
            text_parts.append(str(part.get("markdown") or ""))
    assistant["text"] = "".join(text_parts)


def _finalize_run_transcript_items(doc: dict[str, Any], status: str) -> None:
    assistant = _ensure_assistant_message(doc)
    trace_node_status = "error" if status == "error" else "cancelled" if status == "cancelled" else "success"
    active_tool_statuses = {"running", "pending", "preparing", "awaiting_approval", "approved"}
    terminal_tool_status = "error" if status == "error" else "cancelled"
    parts = []
    changed = False
    for part in _list_value(assistant.get("parts")):
        if not isinstance(part, dict):
            parts.append(part)
            continue
        next_part = dict(part)
        if next_part.get("type") == "assistant_text" and next_part.get("streamKey") == "assistant-stream":
            next_part["streaming"] = False
            next_part["streamKey"] = "assistant-message"
            changed = True
        if next_part.get("type") == "thinking" and next_part.get("streamKey") == "reasoning-stream":
            next_part["active"] = False
            changed = True
        if (
            next_part.get("type") == "tool"
            and str(next_part.get("status") or "") in active_tool_statuses
        ):
            next_part["status"] = terminal_tool_status
            changed = True
        if next_part.get("traceNodeStatus") != trace_node_status:
            next_part["traceNodeStatus"] = trace_node_status
            changed = True
        parts.append(next_part)
    if changed:
        assistant["parts"] = parts
        _refresh_assistant_text(assistant)


def _append_notice_part(
    doc: dict[str, Any],
    level: str,
    text: str,
    prefix: str,
    fmt: str,
    meta: dict[str, Any],
) -> None:
    content = _strip_ansi(text).strip()
    if not content:
        return
    _append_part(doc, _part(prefix, "notice", meta, {
        "level": level,
        "text": content,
        "format": fmt,
    }))


def _notice_level(payload: dict[str, Any]) -> str:
    level = str(payload.get("level") or "").strip().lower()
    return level if level in {"info", "warning", "error"} else "info"


def _has_notice_level(doc: dict[str, Any], level: str) -> bool:
    assistant = _ensure_assistant_message(doc)
    return any(
        isinstance(part, dict)
        and part.get("type") == "notice"
        and part.get("level") == level
        for part in _list_value(assistant.get("parts"))
    )


def _tool_call_id(payload: dict[str, Any]) -> str:
    return _string(payload, "tool_call_id") or _string(payload, "toolCallId")


def _tool_output_format(payload: dict[str, Any], tool_name: str, tool_source: str = "") -> str:
    explicit = (
        _string(payload, "format")
        or _string(payload, "output_format")
        or _string(payload, "tool_output_format")
        or _string(payload, "tool_result_format")
    )
    if explicit in {"plain", "markdown", "terminal", "json"}:
        return explicit
    if _is_shell_tool(tool_name, tool_source):
        return "terminal"
    normalized_tool = tool_name.lower()
    normalized_source = tool_source.lower()
    if "mcp" in normalized_source or "agent" in normalized_tool or normalized_tool in {"mcp", "delegate_agent"}:
        return "markdown"
    return "plain"


def _is_shell_tool(tool_name: str, tool_source: str = "") -> bool:
    normalized_tool = tool_name.lower()
    normalized_source = tool_source.lower()
    return normalized_tool in {"shell", "execute_command"} or "terminal" in normalized_source


def _normalize_tool_stream(value: str) -> str:
    normalized = value.lower()
    if normalized in {"stderr", "result", "system"}:
        return normalized
    return "stdout"


def _append_output_chunk(
    chunks: list[Any],
    stream: str,
    content: str,
    max_chars: int = _SHELL_OUTPUT_MAX_CHARS,
) -> tuple[list[dict[str, Any]], bool]:
    normalized_stream = _normalize_tool_stream(stream)
    normalized_chunks = [
        {**dict(chunk), "stream": _normalize_tool_stream(str(chunk.get("stream") or "stdout"))}
        for chunk in chunks
        if isinstance(chunk, dict) and isinstance(chunk.get("content"), str)
    ]
    if not content:
        return normalized_chunks, any(chunk.get("truncated") is True for chunk in normalized_chunks)
    if normalized_chunks and normalized_chunks[-1].get("stream") == normalized_stream and not normalized_chunks[-1].get("truncated"):
        last = dict(normalized_chunks[-1])
        last["content"] = f"{last.get('content') or ''}{content}"
        normalized_chunks[-1] = last
        return _limit_output_chunks(normalized_chunks, max_chars)
    return _limit_output_chunks([*normalized_chunks, {"stream": normalized_stream, "content": content}], max_chars)


def _limit_output_chunks(
    chunks: list[dict[str, Any]],
    max_chars: int = _SHELL_OUTPUT_MAX_CHARS,
) -> tuple[list[dict[str, Any]], bool]:
    total = sum(len(str(chunk.get("content") or "")) for chunk in chunks)
    already_truncated = any(chunk.get("truncated") is True for chunk in chunks)
    if total <= max_chars and not already_truncated:
        return [dict(chunk) for chunk in chunks], False

    budget = max(1000, max_chars - len(_SHELL_OUTPUT_TRUNCATION_MARKER))
    kept: list[dict[str, Any]] = []
    used = 0
    for chunk in reversed(chunks):
        if chunk.get("truncated") is True:
            continue
        content = str(chunk.get("content") or "")
        if used + len(content) <= budget:
            kept.insert(0, dict(chunk))
            used += len(content)
            continue
        remaining = budget - used
        if remaining > 0:
            next_chunk = dict(chunk)
            next_chunk["content"] = content[-remaining:]
            kept.insert(0, next_chunk)
        break

    return [
        {"stream": "system", "content": _SHELL_OUTPUT_TRUNCATION_MARKER, "truncated": True},
        *kept,
    ], True


def _build_output_text(chunks: list[Any]) -> str:
    return "".join(str(chunk.get("content") or "") for chunk in chunks if isinstance(chunk, dict))


def _shell_chunks_from_text(text: str) -> list[dict[str, Any]]:
    return [{"stream": "result", "content": text}] if text else []


def _reconcile_final_output(streamed: str, final: str, has_chunks: bool = False) -> str:
    if not streamed:
        return final
    if not final:
        return streamed
    if has_chunks:
        return streamed
    if streamed == final or streamed.strip() == final.strip():
        return streamed
    normalized_streamed = streamed.replace("\r\n", "\n").strip()
    normalized_final = final.replace("\r\n", "\n").strip()
    if normalized_final in normalized_streamed:
        return streamed
    if normalized_streamed in normalized_final:
        return final
    return f"{streamed.rstrip()}\n\n[最终结果]\n{final}"


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


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


def _number(value: dict[str, Any] | None, key: str) -> int | float | None:
    if not isinstance(value, dict):
        return None
    item = value.get(key)
    if isinstance(item, (int, float)):
        return item
    if isinstance(item, str) and item.strip():
        try:
            parsed = float(item)
        except ValueError:
            return None
        return int(parsed) if parsed.is_integer() else parsed
    return None


def _defined(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _raw_event_refs_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    refs = payload.get("raw_event_refs") or payload.get("rawEventRefs")
    if not isinstance(refs, list):
        return []
    return [dict(item) for item in refs if isinstance(item, dict)]


def _merge_raw_event_refs(*values: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        refs = value if isinstance(value, list) else []
        for item in refs:
            if not isinstance(item, dict):
                continue
            key = ":".join(
                [
                    str(item.get("agent_run_id") or ""),
                    str(item.get("seq") if item.get("seq") is not None else ""),
                    str(item.get("type") or ""),
                    str(item.get("id") or ""),
                ]
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
    return merged


def _timestamp_ms(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return int(datetime.now(timezone.utc).timestamp() * 1000)

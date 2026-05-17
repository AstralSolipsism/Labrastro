"""LLM diagnostic dump helpers."""

from __future__ import annotations

import json
import time
import traceback
from pathlib import Path
from typing import Any

from reuleauxcoder.infrastructure.fs.paths import get_diagnostics_dir


MAX_SNAPSHOT_MESSAGES = 10
MAX_CONTENT_CHARS = 500
MAX_TOOL_RESULT_CHARS = 500
MAX_ERROR_BODY_CHARS = 4000
MAX_TRACEBACK_CHARS = 12000
TOOL_ARGUMENT_TELEMETRY_FILE = "tool_argument_validation.jsonl"
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "token",
    "password",
    "secret",
    "peer_token",
    "bootstrap-token",
)


def snapshot_messages(
    messages: list[dict], limit: int = MAX_SNAPSHOT_MESSAGES
) -> list[dict[str, Any]]:
    """Build a compact tail snapshot of messages for diagnostics."""
    tail = messages[-limit:] if len(messages) > limit else list(messages)
    snapshot: list[dict[str, Any]] = []
    start_index = max(0, len(messages) - len(tail))
    for offset, msg in enumerate(tail):
        item: dict[str, Any] = {
            "index": start_index + offset,
            "role": msg.get("role", "?"),
        }
        content = msg.get("content")
        if content is not None:
            text = str(content)
            item["content"] = text[:MAX_CONTENT_CHARS] + (
                "..." if len(text) > MAX_CONTENT_CHARS else ""
            )
        reasoning_content = msg.get("reasoning_content")
        if reasoning_content is not None:
            reasoning_text = str(reasoning_content)
            item["reasoning_content"] = reasoning_text[:MAX_CONTENT_CHARS] + (
                "..." if len(reasoning_text) > MAX_CONTENT_CHARS else ""
            )
        if msg.get("tool_call_id"):
            item["tool_call_id"] = msg.get("tool_call_id")
        if msg.get("tool_calls"):
            item["tool_calls"] = msg.get("tool_calls")
        snapshot.append(item)
    return snapshot


def _is_sensitive_key(key: Any) -> bool:
    lower = str(key).lower()
    return any(part in lower for part in SENSITIVE_KEY_PARTS)


def _redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return repr(value)
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _is_sensitive_key(key) else _redact(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    for method_name in ("model_dump", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _redact(method(), depth=depth + 1)
            except Exception:
                pass
    return repr(value)


def _exception_payload(exc: BaseException, *, relation: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "relation": relation,
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "message": str(exc),
    }
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        payload["status_code"] = status_code
    code = getattr(exc, "code", None)
    if code is not None:
        payload["code"] = code
    body = getattr(exc, "body", None)
    if body is not None:
        body_text = (
            body if isinstance(body, str) else json.dumps(_redact(body), ensure_ascii=False)
        )
        payload["body"] = body_text[:MAX_ERROR_BODY_CHARS]
    return payload


def _cause_chain(error: Exception) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    relation = "self"
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(_exception_payload(current, relation=relation))
        if current.__cause__ is not None:
            current = current.__cause__
            relation = "__cause__"
        elif current.__context__ is not None and not current.__suppress_context__:
            current = current.__context__
            relation = "__context__"
        else:
            current = None
    return chain


def _traceback_text(error: Exception) -> str:
    text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    return text[:MAX_TRACEBACK_CHARS]


def _request_summary(request_params: dict[str, Any]) -> dict[str, Any]:
    tool_schemas = request_params.get("tools") or []
    tool_names: list[str] = []
    for tool in tool_schemas:
        function_def = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function_def, dict):
            name = function_def.get("name")
            if isinstance(name, str) and name:
                tool_names.append(name)
    return {
        "stream": request_params.get("stream"),
        "temperature": request_params.get("temperature"),
        "max_tokens": request_params.get("max_tokens"),
        "max_output_tokens": request_params.get("max_output_tokens"),
        "stream_options": _redact(request_params.get("stream_options")),
        "tool_count": len(tool_schemas),
        "tool_names": tool_names,
        "param_keys": sorted(str(key) for key in request_params.keys()),
    }


def _provider_error_diagnostics(error: Exception) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    for attr, key in (
        ("provider_error_phase", "phase"),
        ("provider_stream_options_enabled", "stream_options_enabled"),
        ("provider_retry_attempts", "retry_attempts"),
        ("provider_debug_http_chunks", "http_chunks"),
    ):
        value = getattr(error, attr, None)
        if value is not None:
            diagnostics[key] = _redact(value)
    return diagnostics


def persist_llm_error_diagnostic(
    *,
    model: str,
    base_url: str | None,
    session_id: str | None,
    request_params: dict[str, Any],
    raw_messages: list[dict],
    sanitized_messages: list[dict],
    error: Exception,
    metadata: dict[str, Any] | None = None,
    provider_id: str | None = None,
    provider_type: str | None = None,
    timeout_sec: int | None = None,
    max_retries: int | None = None,
    duration_ms: int | None = None,
) -> Path:
    """Persist an LLM error diagnostic JSON dump and return the path."""
    diagnostics_dir = get_diagnostics_dir()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    session_slug = session_id or "no_session"
    file_path = diagnostics_dir / f"llm_error_{timestamp}_{session_slug}.json"

    error_payload = {
        "type": type(error).__name__,
        "message": str(error),
        "cause_chain": _cause_chain(error),
        "traceback": _traceback_text(error),
    }
    body = getattr(error, "body", None)
    if body is not None:
        body_text = (
            body if isinstance(body, str) else json.dumps(_redact(body), ensure_ascii=False)
        )
        error_payload["body"] = body_text[:MAX_ERROR_BODY_CHARS]

    resolved_provider_id = provider_id or getattr(error, "provider_id", None)
    resolved_provider_type = provider_type or getattr(error, "provider_type", None)
    resolved_base_url = base_url or getattr(error, "provider_base_url", None)
    resolved_timeout = timeout_sec or getattr(error, "provider_timeout_sec", None)
    resolved_retries = max_retries
    if resolved_retries is None:
        resolved_retries = getattr(error, "provider_max_retries", None)

    payload = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id,
        "model": model,
        "base_url": resolved_base_url,
        "provider": {
            "id": resolved_provider_id,
            "type": resolved_provider_type,
            "base_url": resolved_base_url,
            "timeout_sec": resolved_timeout,
            "max_retries": resolved_retries,
        },
        "duration_ms": duration_ms,
        "error": error_payload,
        "request": _request_summary(request_params),
        "provider_error": _provider_error_diagnostics(error),
        "messages": {
            "raw_count": len(raw_messages),
            "sanitized_count": len(sanitized_messages),
            "raw_tail": snapshot_messages(raw_messages),
            "sanitized_tail": snapshot_messages(sanitized_messages),
        },
        "metadata": _redact(dict(metadata or {})),
    }

    file_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return file_path


def persist_tool_argument_validation_event(
    *,
    validation: Any,
    metadata: dict[str, Any] | None = None,
) -> Path:
    """Append a tool-argument validation telemetry event as JSONL."""
    diagnostics_dir = get_diagnostics_dir()
    file_path = diagnostics_dir / TOOL_ARGUMENT_TELEMETRY_FILE
    validation_payload = (
        validation.to_dict() if hasattr(validation, "to_dict") else dict(validation)
    )
    payload = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "metadata": dict(metadata or {}),
        "validation": validation_payload,
    }
    with file_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return file_path


def aggregate_tool_argument_validation_events(
    path: Path | None = None,
) -> dict[str, int]:
    """Aggregate validation events by model/tool/problem/action/final status."""
    source = path or (get_diagnostics_dir() / TOOL_ARGUMENT_TELEMETRY_FILE)
    counts: dict[str, int] = {}
    if not source.exists():
        return counts
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        metadata = event.get("metadata") if isinstance(event, dict) else {}
        validation = event.get("validation") if isinstance(event, dict) else {}
        if not isinstance(metadata, dict) or not isinstance(validation, dict):
            continue
        model = str(metadata.get("model") or "unknown")
        tool = str(metadata.get("tool") or validation.get("tool_name") or "unknown")
        final_valid = str(bool(validation.get("final_valid"))).lower()
        _inc(counts, f"model={model}|tool={tool}|final_valid={final_valid}")
        for issue in validation.get("initial_issues") or []:
            if isinstance(issue, dict):
                _inc(
                    counts,
                    "issue|"
                    f"model={model}|tool={tool}|code={issue.get('code')}|path={issue.get('path')}",
                )
        for repair in validation.get("repairs") or []:
            if isinstance(repair, dict):
                _inc(
                    counts,
                    "repair|"
                    f"model={model}|tool={tool}|action={repair.get('action')}|path={repair.get('path')}",
                )
    return counts


def summarize_tool_argument_validation_events(
    path: Path | None = None,
    *,
    recent_limit: int = 20,
) -> dict[str, Any]:
    """Return UI-friendly tool-argument validation telemetry statistics."""
    source = path or (get_diagnostics_dir() / TOOL_ARGUMENT_TELEMETRY_FILE)
    if not source.exists():
        return _empty_tool_argument_validation_summary(source)

    by_model: dict[str, dict[str, Any]] = {}
    by_tool: dict[str, dict[str, Any]] = {}
    issues: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    repairs: dict[tuple[str, str, str], dict[str, Any]] = {}
    recent: list[dict[str, Any]] = []
    event_count = 0
    invalid_count = 0
    repaired_count = 0

    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        metadata = event.get("metadata")
        validation = event.get("validation")
        if not isinstance(metadata, dict) or not isinstance(validation, dict):
            continue
        event_count += 1
        model = str(metadata.get("model") or "unknown")
        tool = str(metadata.get("tool") or validation.get("tool_name") or "unknown")
        provider = str(metadata.get("provider_id") or "")
        compat = str(metadata.get("compat") or "")
        final_valid = bool(validation.get("final_valid"))
        repair_items = [item for item in validation.get("repairs") or [] if isinstance(item, dict)]
        issue_items = [
            item
            for item in [
                *(validation.get("initial_issues") or []),
                *(validation.get("final_issues") or []),
            ]
            if isinstance(item, dict)
        ]
        if not final_valid:
            invalid_count += 1
        if repair_items:
            repaired_count += 1
        _bump_bucket(by_model, model, final_valid=final_valid, repaired=bool(repair_items))
        _bump_bucket(by_tool, tool, final_valid=final_valid, repaired=bool(repair_items))
        by_model[model]["provider_id"] = provider
        by_model[model]["compat"] = compat
        for issue in issue_items:
            key = (
                model,
                tool,
                str(issue.get("code") or "unknown"),
                str(issue.get("path") or "$"),
            )
            item = issues.setdefault(
                key,
                {
                    "model": model,
                    "tool": tool,
                    "code": key[2],
                    "path": key[3],
                    "expected": str(issue.get("expected") or ""),
                    "actual": str(issue.get("actual") or ""),
                    "count": 0,
                },
            )
            item["count"] += 1
        for repair in repair_items:
            key = (
                model,
                tool,
                str(repair.get("action") or "unknown"),
            )
            item = repairs.setdefault(
                key,
                {
                    "model": model,
                    "tool": tool,
                    "action": key[2],
                    "path": str(repair.get("path") or "$"),
                    "count": 0,
                },
            )
            item["count"] += 1
        recent.append(
            {
                "timestamp": event.get("timestamp"),
                "model": model,
                "tool": tool,
                "provider_id": provider,
                "compat": compat,
                "final_valid": final_valid,
                "issue_count": len(issue_items),
                "repair_count": len(repair_items),
            }
        )
        recent = recent[-recent_limit:]

    return {
        "path": str(source),
        "exists": True,
        "totals": {
            "events": event_count,
            "invalid": invalid_count,
            "repaired": repaired_count,
        },
        "by_model": _sorted_counts(by_model),
        "by_tool": _sorted_counts(by_tool),
        "issues": sorted(issues.values(), key=lambda item: (-int(item["count"]), item["model"], item["tool"]))[:50],
        "repairs": sorted(repairs.values(), key=lambda item: (-int(item["count"]), item["model"], item["tool"]))[:50],
        "recent": list(reversed(recent)),
    }


def _empty_tool_argument_validation_summary(source: Path) -> dict[str, Any]:
    return {
        "path": str(source),
        "exists": False,
        "totals": {"events": 0, "invalid": 0, "repaired": 0},
        "by_model": [],
        "by_tool": [],
        "issues": [],
        "repairs": [],
        "recent": [],
    }


def _bump_bucket(
    buckets: dict[str, dict[str, Any]],
    key: str,
    *,
    final_valid: bool,
    repaired: bool,
) -> None:
    bucket = buckets.setdefault(
        key,
        {"name": key, "events": 0, "invalid": 0, "repaired": 0},
    )
    bucket["events"] += 1
    if not final_valid:
        bucket["invalid"] += 1
    if repaired:
        bucket["repaired"] += 1


def _sorted_counts(buckets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        buckets.values(),
        key=lambda item: (-int(item.get("events", 0)), str(item.get("name", ""))),
    )


def _inc(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1

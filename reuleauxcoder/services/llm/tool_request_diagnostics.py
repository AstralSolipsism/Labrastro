"""Redacted diagnostics for final LLM tool request shape."""

from __future__ import annotations

from typing import Any
import hashlib
import json


def build_tool_request_snapshot(
    provider_type: str,
    params: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record stable top-level tool names without becoming a tool contract."""

    tools = _provider_tools(provider_type, params)
    names = [_tool_name(tool) for tool in tools]
    names = [name for name in names if name]
    exposure = metadata.get("tool_exposure") if isinstance(metadata, dict) else None
    deferred_count = None
    if isinstance(exposure, dict):
        deferred_count = _optional_int(exposure.get("deferred_tool_count"))
    gateway_history = _gateway_history(params)
    return {
        "schema": "llm_tool_request.v1",
        "provider_type": str(provider_type or ""),
        "top_level_tool_count": len(names),
        "top_level_tool_names": names,
        "top_level_tools_hash": hashlib.sha256(
            json.dumps(names, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "deferred_tool_count": deferred_count,
        **gateway_history,
    }


def _provider_tools(provider_type: str, params: dict[str, Any]) -> list[Any]:
    if not isinstance(params, dict):
        return []
    tools = params.get("tools")
    return list(tools) if isinstance(tools, list) else []


def _tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip()
    return str(tool.get("name") or "").strip()


def _gateway_history(params: dict[str, Any]) -> dict[str, Any]:
    messages = params.get("messages") if isinstance(params, dict) else None
    if not isinstance(messages, list):
        messages = []
    call_names_by_id: dict[str, str] = {}
    tool_search_calls = 0
    capability_execute_calls = 0
    search_hit_tool_ids: list[str] = []
    seen_hit_tool_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in _assistant_tool_calls(message):
            call_id = str(call.get("id") or "").strip()
            name = _tool_call_name(call)
            if call_id and name:
                call_names_by_id[call_id] = name
            if name == "tool_search":
                tool_search_calls += 1
            elif name == "capability_execute":
                capability_execute_calls += 1
        if str(message.get("role") or "") != "tool":
            continue
        result_name = str(message.get("name") or "").strip()
        if not result_name:
            result_name = call_names_by_id.get(
                str(message.get("tool_call_id") or "").strip(),
                "",
            )
        if result_name != "tool_search":
            continue
        for tool_id in _search_result_tool_ids(message.get("content")):
            if tool_id in seen_hit_tool_ids:
                continue
            seen_hit_tool_ids.add(tool_id)
            search_hit_tool_ids.append(tool_id)
    return {
        "gateway_call_count": tool_search_calls + capability_execute_calls,
        "tool_search_call_count": tool_search_calls,
        "capability_execute_call_count": capability_execute_calls,
        "search_hit_count": len(search_hit_tool_ids),
        "search_hit_tool_ids": search_hit_tool_ids,
    }


def _assistant_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    return [dict(item) for item in raw_calls if isinstance(item, dict)]


def _tool_call_name(call: dict[str, Any]) -> str:
    function = call.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip()
    return str(call.get("name") or "").strip()


def _search_result_tool_ids(content: Any) -> list[str]:
    payload = _json_payload(content)
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    tool_ids: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        tool_id = str(item.get("tool_id") or "").strip()
        if tool_id:
            tool_ids.append(tool_id)
    return tool_ids


def _json_payload(content: Any) -> Any:
    if isinstance(content, (dict, list)):
        return content
    if not isinstance(content, str):
        return None
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

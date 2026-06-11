"""Helpers for provider-neutral streamed tool-call preparation events."""

from __future__ import annotations

from typing import Any

from reuleauxcoder.domain.providers.models import ProviderRequest


TOOL_ARGUMENTS_PREVIEW_LIMIT = 240
TOOL_ARGUMENT_CHARS_LIMIT = 128 * 1024


class ToolArgumentLimitExceeded(ValueError):
    """Raised when streamed tool arguments exceed the runtime transport limit."""


def tool_arguments_preview(arguments: str, limit: int = TOOL_ARGUMENTS_PREVIEW_LIMIT) -> str:
    text = str(arguments or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def emit_tool_call_delta(
    request: ProviderRequest,
    *,
    index: int,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    arguments_delta: str | None = None,
    arguments_preview: str | None = None,
) -> None:
    """Emit a UI-only delta when a provider streams structured tool-call data."""
    callback = request.on_tool_call_delta
    if callback is None:
        return
    name = str(tool_name or "")
    delta = str(arguments_delta or "")
    if not name and not delta:
        return
    total_chars = _record_tool_argument_chars(
        request,
        index=index,
        tool_call_id=tool_call_id,
        delta=delta,
    )
    if total_chars > TOOL_ARGUMENT_CHARS_LIMIT:
        raise ToolArgumentLimitExceeded(
            "streamed tool arguments exceeded 128 KiB; use apply_patch for file "
            "changes or draft_document_begin for long markdown documents"
        )
    callback(
        {
            "index": index,
            "tool_call_id": str(tool_call_id or ""),
            "tool_name": name,
            "arguments_delta": delta,
            "arguments_preview": arguments_preview if arguments_preview is not None else delta,
            "status": "preparing",
        }
    )


def _record_tool_argument_chars(
    request: ProviderRequest,
    *,
    index: int,
    tool_call_id: str | None,
    delta: str,
) -> int:
    counters = request.metadata.setdefault("_tool_argument_chars", {})
    if not isinstance(counters, dict):
        counters = {}
        request.metadata["_tool_argument_chars"] = counters
    key = str(tool_call_id or f"index:{index}")
    counters[key] = int(counters.get(key) or 0) + len(delta)
    return counters[key]

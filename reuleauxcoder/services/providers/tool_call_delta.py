"""Helpers for provider-neutral streamed tool-call preparation events."""

from __future__ import annotations

from typing import Any

from reuleauxcoder.domain.providers.models import ProviderRequest


TOOL_ARGUMENTS_PREVIEW_LIMIT = 240


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

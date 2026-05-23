"""Permission event helpers for AgentRun control-plane projections."""

from __future__ import annotations

from typing import Any


BACKGROUND_REVIEW_SOURCES = {
    "taskflow",
    "delegation",
    "environment",
    "capability_ingest",
}


def should_block_waiting_approval(source: object) -> bool:
    return str(getattr(source, "value", source) or "").strip() in BACKGROUND_REVIEW_SOURCES


def blocked_review_event_payload(event_data: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(event_data.get("tool_name") or event_data.get("tool") or "")
    reason = str(
        event_data.get("reason")
        or event_data.get("message")
        or "background execution cannot wait for interactive approval"
    )
    return {
        "tool_name": tool_name,
        "approval_id": str(event_data.get("approval_id") or ""),
        "reason": reason,
        "permission": {
            "action": "blocked_review",
            "authorized": False,
            "reason": reason,
        },
    }

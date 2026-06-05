"""Shared lifecycle gate decision policy."""

from __future__ import annotations

from typing import Any


LIFECYCLE_GATE_EVENTS = frozenset({
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PermissionRequest",
    "PreToolUse",
    "TaskCreated",
    "PreCompact",
})

LIFECYCLE_TERMINAL_DECISIONS = frozenset({"deny", "defer"})


def lifecycle_gate_event_is_gate(event_name: str) -> bool:
    return event_name in LIFECYCLE_GATE_EVENTS


def lifecycle_output_decision(output: Any) -> str:
    value = _output_value(output, "decision", "none")
    return str(value or "none").strip().lower()


def lifecycle_output_continue_flow(output: Any) -> bool:
    value = _output_value(output, "continue_flow", True)
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off"}
    return bool(value)


def lifecycle_gate_output_is_terminal(output: Any) -> bool:
    return (
        lifecycle_output_decision(output) in LIFECYCLE_TERMINAL_DECISIONS
        or lifecycle_output_continue_flow(output) is False
    )


def lifecycle_output_requests_approval(output: Any) -> bool:
    return lifecycle_output_decision(output) == "ask"


def lifecycle_gate_terminal_kind(output: Any) -> str:
    if lifecycle_output_decision(output) == "defer":
        return "defer"
    return "deny"


def lifecycle_output_message(output: Any, *, fallback: str) -> str:
    for field_name in ("user_message", "reason"):
        text = _text(_output_value(output, field_name, ""))
        if text:
            return text
    return fallback


def _output_value(output: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(output, dict):
        return output.get(field_name, default)
    return getattr(output, field_name, default)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()

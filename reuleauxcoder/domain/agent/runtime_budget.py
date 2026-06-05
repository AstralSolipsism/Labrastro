"""Runtime helpers for AgentRun execution budgets."""

from __future__ import annotations

import time
from typing import Any


RUNTIME_BUDGET_FIELDS = {
    "token_budget",
    "max_turns",
    "max_tool_calls",
    "timeout_sec",
}


def positive_budget_int(value: Any) -> int | None:
    """Return a positive int budget value, or None when unset/invalid."""
    if value in (None, ""):
        return None
    try:
        amount = int(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return amount


def normalize_runtime_budget(value: Any) -> dict[str, int]:
    """Normalize known AgentRun budget fields to positive integers."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key, raw in value.items():
        field_name = str(key or "").strip()
        if field_name not in RUNTIME_BUDGET_FIELDS:
            continue
        amount = positive_budget_int(raw)
        if amount is not None:
            result[field_name] = amount
    return result


def runtime_budget(agent: Any) -> dict[str, Any]:
    budget = getattr(agent, "runtime_budget", None)
    return budget if isinstance(budget, dict) else {}


def runtime_budget_int(agent: Any, field_name: str) -> int | None:
    budget = runtime_budget(agent)
    value = budget.get(field_name)
    if value in (None, ""):
        value = getattr(agent, f"runtime_{field_name}", None)
    return positive_budget_int(value)


def runtime_token_usage(agent: Any) -> int:
    state = getattr(agent, "state", None)
    try:
        prompt = int(getattr(state, "total_prompt_tokens", 0) or 0)
    except (TypeError, ValueError):
        prompt = 0
    try:
        completion = int(getattr(state, "total_completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        completion = 0
    return prompt + completion


def runtime_budget_limit(agent: Any) -> dict[str, Any] | None:
    """Return the first exceeded cooperative execution budget."""
    timeout_sec = runtime_budget_int(agent, "timeout_sec")
    deadline = getattr(agent, "runtime_deadline", None)
    if timeout_sec is not None and deadline is not None:
        try:
            if time.monotonic() >= float(deadline):
                return {
                    "field": "timeout_sec",
                    "limit": timeout_sec,
                    "message": f"AgentRun budget exceeded: timeout_sec={timeout_sec}",
                }
        except (TypeError, ValueError):
            pass

    token_budget = runtime_budget_int(agent, "token_budget")
    if token_budget is not None and runtime_token_usage(agent) >= token_budget:
        return {
            "field": "token_budget",
            "limit": token_budget,
            "message": f"AgentRun budget exceeded: token_budget={token_budget}",
        }

    return None


def runtime_budget_limit_message(agent: Any) -> str | None:
    """Return a budget stop message for cooperative execution boundaries."""
    limit = runtime_budget_limit(agent)
    return str(limit.get("message")) if limit else None

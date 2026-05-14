"""Helpers for V1 stale-state propagation."""

from __future__ import annotations

from typing import Any

from labrastro_server.taskflow.domain.time import utc_now
from labrastro_server.taskflow.domain.taskflow_state import TaskflowState, TaskflowStatus


def mark_taskflow_stale(
    state: TaskflowState,
    *,
    reason: str,
    source: str,
    source_refs: list[str] | None = None,
) -> None:
    marker: dict[str, Any] = {
        "reason": reason,
        "source": source,
        "source_refs": list(source_refs or []),
        "marked_at": utc_now(),
    }
    state.compiler.traceability_index["compiler_review_stale"] = True
    state.compiler.traceability_index["dispatch_review_stale"] = True
    state.compiler.traceability_index["stale"] = marker
    if state.meta.status != TaskflowStatus.CANCELLED:
        state.meta.status = TaskflowStatus.CONFIRMED
    for plan in state.outputs.plan_drafts:
        if isinstance(plan, dict):
            plan["stale"] = True
            plan["stale_reason"] = reason
            plan["stale_source"] = source
    for decision in getattr(state.outputs, "compiler_decisions", []):
        if isinstance(decision, dict):
            decision["stale"] = True
            decision["status"] = "stale"
            decision["stale_reason"] = reason
    for dispatch in state.outputs.dispatch_decisions:
        dispatch.metadata["stale"] = True
        dispatch.metadata["stale_reason"] = reason


def clear_taskflow_stale(state: TaskflowState) -> None:
    state.compiler.traceability_index.pop("compiler_review_stale", None)
    state.compiler.traceability_index.pop("dispatch_review_stale", None)
    state.compiler.traceability_index.pop("stale", None)


__all__ = ["clear_taskflow_stale", "mark_taskflow_stale"]

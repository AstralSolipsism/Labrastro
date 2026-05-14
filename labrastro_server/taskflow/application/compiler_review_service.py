"""Compiler-decision projections and review actions for Taskflow V1."""

from __future__ import annotations

from typing import Any

from labrastro_server.taskflow.application.staleness import (
    clear_taskflow_stale,
    mark_taskflow_stale,
)
from labrastro_server.taskflow.compiler.plan_compiler import PlanDraft
from labrastro_server.taskflow.domain.events import append_taskflow_event
from labrastro_server.taskflow.domain.project_state import ProjectState, WorkItem
from labrastro_server.taskflow.domain.taskflow_state import (
    TaskflowEventType,
    TaskflowState,
)


class CompilerReviewService:
    """Build and mutate structured compiler-decision DTOs."""

    def build_decisions(
        self, state: TaskflowState, project: ProjectState, plan: PlanDraft
    ) -> list[dict[str, Any]]:
        clear_taskflow_stale(state)
        decisions = [
            self._decision_from_candidate(project, plan.id, candidate.to_dict())
            for candidate in plan.work_item_candidates
        ]
        state.outputs.compiler_decisions = decisions
        return decisions

    def review_decision(
        self,
        state: TaskflowState,
        *,
        project: ProjectState | None = None,
        decision_id: str,
        action: str,
        actor: str = "user",
        reason: str = "",
        value: Any = None,
    ) -> dict[str, Any]:
        action = str(action or "").strip()
        if action not in {"accept", "reject", "force_create", "force_reuse", "split"}:
            raise ValueError(f"unsupported compiler decision action: {action}")
        if action != "accept" and not str(reason).strip():
            raise ValueError("compiler decision override requires a reason")
        decision = self._find_decision(state, decision_id)
        normalized_value = self._validate_override_value(
            action,
            value,
            project=project,
        )
        decision["status"] = "accepted" if action == "accept" else action
        decision["reviewed_by"] = actor
        decision["review_reason"] = reason
        decision["override"] = {
            "action": action,
            "reason": reason,
            "value": normalized_value,
            "actor": actor,
        }
        append_taskflow_event(
            state,
            TaskflowEventType.COMPILER_DECISION_REVIEWED,
            actor=actor,
            payload={"decision_id": decision_id, "action": action, "reason": reason, "value": normalized_value},
        )
        if action != "accept":
            overrides = state.compiler.traceability_index.setdefault(
                "compiler_review_overrides", {}
            )
            if isinstance(overrides, dict):
                overrides[str(decision.get("candidate_id") or decision_id)] = {
                    "action": action,
                    "reason": reason,
                    "value": normalized_value,
                    "decision_id": decision_id,
                }
            mark_taskflow_stale(
                state,
                reason=f"compiler decision {decision_id} was overridden",
                source="compiler_review",
                source_refs=[decision_id],
            )
        return decision

    def assert_current(self, state: TaskflowState) -> None:
        if state.compiler.traceability_index.get("compiler_review_stale"):
            raise ValueError("compiler review is stale; recompile before dispatch review")
        decisions = getattr(state.outputs, "compiler_decisions", [])
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            if decision.get("stale") or decision.get("status") == "stale":
                raise ValueError("compiler review is stale; recompile before dispatch review")

    def _decision_from_candidate(
        self, project: ProjectState, plan_id: str, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        action = str(candidate.get("action") or "create")
        metadata = candidate.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        derived_from = metadata.get("derived_from")
        override_hint = metadata.get("compiler_review_override")
        display_action = "derive" if action == "create" and derived_from else action
        reused = self._work_item(project, str(candidate.get("reuse_work_item_id") or ""))
        acceptance_refs = _string_list(candidate.get("acceptance_refs"))
        reused_acceptance_refs = list(reused.acceptance_refs) if reused else []
        return {
            "id": f"compiler-decision-{plan_id}-{candidate.get('candidate_id')}",
            "plan_id": plan_id,
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "work_item_id": str(candidate.get("work_item_id") or ""),
            "title": str(candidate.get("title") or ""),
            "action": display_action,
            "compiler_action": action,
            "dedupe_key": candidate.get("dedupe_key"),
            "reason": candidate.get("rationale") or self._reason_for_action(display_action),
            "reuse_work_item_id": candidate.get("reuse_work_item_id"),
            "derived_from": derived_from,
            "depends_on": _string_list(candidate.get("depends_on")),
            "acceptance_boundary_diff": {
                "candidate_refs": acceptance_refs,
                "reused_refs": reused_acceptance_refs,
                "added": sorted(set(acceptance_refs).difference(reused_acceptance_refs)),
                "missing": sorted(set(reused_acceptance_refs).difference(acceptance_refs)),
            },
            "trace_refs": sorted(
                set(
                    _string_list(candidate.get("acceptance_refs"))
                    + _string_list(candidate.get("decision_refs"))
                    + _string_list(candidate.get("artifact_refs"))
                    + _string_list(candidate.get("scenario_refs"))
                    + _string_list(candidate.get("risk_refs"))
                )
            ),
            "status": "accepted",
            "stale": False,
            "override": dict(override_hint) if isinstance(override_hint, dict) else None,
        }

    def _find_decision(self, state: TaskflowState, decision_id: str) -> dict[str, Any]:
        for decision in getattr(state.outputs, "compiler_decisions", []):
            if isinstance(decision, dict) and decision.get("id") == decision_id:
                return decision
        raise ValueError(f"compiler decision not found: {decision_id}")

    def _work_item(self, project: ProjectState, work_item_id: str) -> WorkItem | None:
        if not work_item_id:
            return None
        for item in project.list_work_items():
            if item.id == work_item_id:
                return item
        return None

    def _reason_for_action(self, action: str) -> str:
        if action == "reuse":
            return "Existing WorkItem matched the compiler reuse boundary."
        if action == "derive":
            return "Similar WorkItem exists, but acceptance boundary differs."
        return "No reusable WorkItem matched the compiler boundary."

    def _validate_override_value(
        self,
        action: str,
        value: Any,
        *,
        project: ProjectState | None,
    ) -> Any:
        if action == "force_reuse":
            work_item_id = _override_text(value, "work_item_id")
            if not work_item_id:
                raise ValueError("force_reuse requires a target work_item_id")
            if project is not None and self._work_item(project, work_item_id) is None:
                raise ValueError(f"force_reuse target work item not found: {work_item_id}")
            return {"work_item_id": work_item_id}
        if action == "split":
            description = _override_text(value, "description")
            candidates = []
            if isinstance(value, dict) and isinstance(value.get("candidates"), list):
                candidates = [
                    dict(item)
                    for item in value["candidates"]
                    if isinstance(item, dict)
                ]
            if not description and not candidates:
                raise ValueError("split requires a description or candidate fragments")
            return {"description": description, "candidates": candidates}
        return value


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _override_text(value: Any, key: str) -> str:
    if isinstance(value, dict):
        return str(value.get(key) or value.get("id") or "").strip()
    return str(value or "").strip()


__all__ = ["CompilerReviewService"]

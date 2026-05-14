"""Project Memory V1 view and domain patch proposals."""

from __future__ import annotations

import uuid
from typing import Any

from labrastro_server.taskflow.application.staleness import mark_taskflow_stale
from labrastro_server.taskflow.domain.events import append_taskflow_event
from labrastro_server.taskflow.domain.project_state import (
    Constraint,
    ProjectDecision,
    ProjectState,
    TraceLink,
    WorkItem,
    WorkItemStatus,
)
from labrastro_server.taskflow.domain.taskflow_state import (
    TaskflowEventType,
    TaskflowState,
)
from labrastro_server.taskflow.domain.time import utc_now


class ProjectMemoryService:
    """ProjectState governance operations for V1."""

    def view(self, project: ProjectState) -> dict[str, Any]:
        return {
            "project_id": project.project_id,
            "terms": [
                {"term": key, "definition": value}
                for key, value in sorted(project.knowledge_base.ubiquitous_language.items())
            ],
            "decisions": [
                item.to_dict()
                for item in [
                    *project.decisions.project_decisions,
                    *project.decisions.architecture_decisions,
                    *project.decisions.policy_decisions,
                ]
            ],
            "constraints": [item.to_dict() for item in project.project_profile.constraints],
            "work_items": [item.to_dict() for item in project.list_work_items()],
            "trace_links": [
                item.to_dict()
                for item in [
                    *project.traceability.goal_links,
                    *project.traceability.decision_links,
                    *project.traceability.artifact_links,
                    *project.traceability.task_run_links,
                ]
            ],
            "patch_proposals": [
                dict(item)
                for item in project.projections.reviews
                if item.get("type") == "project_memory_patch_proposal"
            ],
        }

    def preview_patch(
        self,
        project: ProjectState,
        *,
        actor: str,
        reason: str,
        source: str,
        operations: list[Any],
    ) -> dict[str, Any]:
        self._validate_patch(actor=actor, reason=reason, source=source, operations=operations)
        working = ProjectState.from_dict(project.to_dict())
        diffs: list[dict[str, Any]] = []
        for raw in operations:
            op = _dict(raw)
            diffs.append(self._apply_operation(working, op, preview=False))
        return {
            "id": f"project-memory-patch-{uuid.uuid4().hex}",
            "type": "project_memory_patch_proposal",
            "status": "pending",
            "actor": actor,
            "reason": reason,
            "source": source,
            "operations": [_dict(item) for item in operations],
            "diff": diffs,
            "created_at": utc_now(),
        }

    def record_pending_patch(
        self, project: ProjectState, proposal: dict[str, Any]
    ) -> dict[str, Any]:
        proposal = dict(proposal)
        proposal["status"] = "pending"
        self._replace_projection_review(project, proposal)
        project.touch()
        return proposal

    def apply_patch(
        self,
        state: TaskflowState,
        project: ProjectState,
        *,
        proposal_id: str | None = None,
        actor: str,
        reason: str,
        source: str,
        operations: list[Any] | None,
    ) -> dict[str, Any]:
        existing = self._proposal(project, proposal_id or "")
        proposal_actor = actor
        proposal_reason = reason
        proposal_source = source
        if existing is not None:
            proposal_actor = proposal_actor or str(existing.get("actor") or "")
            proposal_reason = proposal_reason or str(existing.get("reason") or "")
            proposal_source = proposal_source or str(existing.get("source") or "")
        proposal_operations = list(operations or (existing.get("operations") if existing else []) or [])
        proposal = dict(existing) if existing else self.preview_patch(
            project,
            actor=proposal_actor,
            reason=proposal_reason,
            source=proposal_source,
            operations=proposal_operations,
        )
        if proposal_id:
            proposal["id"] = proposal_id
        self._validate_patch(
            actor=proposal_actor,
            reason=proposal_reason,
            source=proposal_source,
            operations=proposal_operations,
        )
        diffs: list[dict[str, Any]] = []
        for raw in proposal_operations:
            diffs.append(self._apply_operation(project, _dict(raw), preview=False))
        proposal["status"] = "applied"
        proposal["actor"] = proposal_actor
        proposal["reason"] = proposal_reason
        proposal["source"] = proposal_source
        proposal["operations"] = [_dict(item) for item in proposal_operations]
        proposal["diff"] = diffs
        proposal["applied_at"] = utc_now()
        proposal["applied_by"] = actor
        self._replace_projection_review(project, proposal)
        project.touch()
        append_taskflow_event(
            state,
            TaskflowEventType.PROJECT_MEMORY_PATCH_APPLIED,
            actor=actor,
            payload=proposal,
        )
        mark_taskflow_stale(
            state,
            reason="Project Memory changed",
            source="project_memory_patch",
            source_refs=[proposal["id"]],
        )
        return proposal

    def _validate_patch(
        self,
        *,
        actor: str,
        reason: str,
        source: str,
        operations: list[Any],
    ) -> None:
        if not actor.strip():
            raise ValueError("project memory patch requires actor")
        if not reason.strip():
            raise ValueError("project memory patch requires reason")
        if not source.strip():
            raise ValueError("project memory patch requires source")
        if not operations:
            raise ValueError("project memory patch requires operations")

    def _apply_operation(
        self, project: ProjectState, op: dict[str, Any], *, preview: bool
    ) -> dict[str, Any]:
        op_type = str(op.get("type") or op.get("op") or "").strip()
        if op_type == "upsert_term":
            term = str(op.get("term") or op.get("key") or "").strip()
            if not term:
                raise ValueError("upsert_term requires term")
            before = project.knowledge_base.ubiquitous_language.get(term)
            after = str(op.get("definition") or op.get("value") or "")
            if not preview:
                project.knowledge_base.ubiquitous_language[term] = after
            return _diff(op_type, f"terms.{term}", before, after)
        if op_type == "remove_term":
            term = str(op.get("term") or op.get("key") or "").strip()
            before = project.knowledge_base.ubiquitous_language.get(term)
            if not preview:
                project.knowledge_base.ubiquitous_language.pop(term, None)
            return _diff(op_type, f"terms.{term}", before, None)
        if op_type == "upsert_decision":
            item = ProjectDecision.from_dict(_dict(op.get("decision") or op))
            if not item.id:
                raise ValueError("upsert_decision requires id")
            bucket = self._decision_bucket(project, str(op.get("bucket") or "project_decisions"))
            before = next((existing.to_dict() for existing in bucket if existing.id == item.id), None)
            if not preview:
                _replace_or_append(bucket, item, key=lambda value: value.id)
            return _diff(op_type, f"decisions.{item.id}", before, item.to_dict())
        if op_type == "upsert_constraint":
            item = Constraint.from_dict(_dict(op.get("constraint") or op))
            if not item.id:
                raise ValueError("upsert_constraint requires id")
            before = next((existing.to_dict() for existing in project.project_profile.constraints if existing.id == item.id), None)
            if not preview:
                _replace_or_append(project.project_profile.constraints, item, key=lambda value: value.id)
            return _diff(op_type, f"constraints.{item.id}", before, item.to_dict())
        if op_type == "upsert_work_item":
            item = WorkItem.from_dict(_dict(op.get("work_item") or op))
            if not item.id:
                raise ValueError("upsert_work_item requires id")
            before_item = next((existing for existing in project.list_work_items() if existing.id == item.id), None)
            before = before_item.to_dict() if before_item else None
            if not preview:
                project.upsert_work_item(item)
            return _diff(op_type, f"work_items.{item.id}", before, item.to_dict())
        if op_type == "update_work_item_status":
            work_item_id = str(op.get("work_item_id") or op.get("id") or "").strip()
            status = str(op.get("status") or "").strip()
            if not work_item_id or not status:
                raise ValueError("update_work_item_status requires work_item_id and status")
            item = next((existing for existing in project.list_work_items() if existing.id == work_item_id), None)
            before = item.to_dict() if item else None
            if item is None:
                raise ValueError(f"work item not found: {work_item_id}")
            after = item.to_dict()
            after["status"] = WorkItemStatus(status).value
            if not preview:
                item.status = WorkItemStatus(status)
                item.updated_at = utc_now()
                project.touch()
            return _diff(op_type, f"work_items.{work_item_id}.status", before, after)
        if op_type == "upsert_trace_link":
            item = TraceLink.from_dict(_dict(op.get("trace_link") or op))
            if not item.id:
                raise ValueError("upsert_trace_link requires id")
            before = self._trace_link(project, item.id)
            if not preview:
                self._remove_trace_link(project, item.id)
                project.add_trace_link(item)
            return _diff(op_type, f"trace_links.{item.id}", before, item.to_dict())
        if op_type == "remove_trace_link":
            trace_id = str(op.get("trace_link_id") or op.get("id") or "").strip()
            before = self._trace_link(project, trace_id)
            if not preview:
                self._remove_trace_link(project, trace_id)
            return _diff(op_type, f"trace_links.{trace_id}", before, None)
        raise ValueError(f"unsupported project memory operation: {op_type}")

    def _decision_bucket(
        self, project: ProjectState, bucket: str
    ) -> list[ProjectDecision]:
        if bucket == "architecture_decisions":
            return project.decisions.architecture_decisions
        if bucket == "policy_decisions":
            return project.decisions.policy_decisions
        return project.decisions.project_decisions

    def _trace_link(self, project: ProjectState, trace_id: str) -> dict[str, Any] | None:
        for collection in self._trace_link_buckets(project):
            for item in collection:
                if item.id == trace_id:
                    return item.to_dict()
        return None

    def _remove_trace_link(self, project: ProjectState, trace_id: str) -> None:
        for collection in self._trace_link_buckets(project):
            collection[:] = [item for item in collection if item.id != trace_id]
        project.touch()

    def _trace_link_buckets(self, project: ProjectState) -> list[list[TraceLink]]:
        return [
            project.traceability.decision_links,
            project.traceability.artifact_links,
            project.traceability.task_run_links,
        ]

    def _proposal(
        self, project: ProjectState, proposal_id: str
    ) -> dict[str, Any] | None:
        if not proposal_id:
            return None
        for item in project.projections.reviews:
            if (
                item.get("type") == "project_memory_patch_proposal"
                and item.get("id") == proposal_id
            ):
                return dict(item)
        return None

    def _replace_projection_review(
        self, project: ProjectState, proposal: dict[str, Any]
    ) -> None:
        for index, item in enumerate(project.projections.reviews):
            if (
                item.get("type") == "project_memory_patch_proposal"
                and item.get("id") == proposal.get("id")
            ):
                project.projections.reviews[index] = dict(proposal)
                return
        project.projections.reviews.append(dict(proposal))


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _diff(
    op_type: str, path: str, before: Any, after: Any
) -> dict[str, Any]:
    return {"operation": op_type, "path": path, "before": before, "after": after}


def _replace_or_append(
    values: list[Any], item: Any, *, key: Any
) -> None:
    item_key = key(item)
    for index, existing in enumerate(values):
        if key(existing) == item_key:
            values[index] = item
            return
    values.append(item)


__all__ = ["ProjectMemoryService"]

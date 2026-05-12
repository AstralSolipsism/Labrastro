"""Runtime projection for Taskflow TaskRuns.

Taskflow owns the planning and authorization boundary. Runtime ownership stays
with the AgentRun control plane; this service only joins persisted TaskRun trace
links with the live AgentRun detail when that control plane is available.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from labrastro_server.taskflow.domain.project_state import (
    ProjectState,
    TaskRun,
    TaskRunStatus,
    TraceEntityType,
    WorkItem,
)
from labrastro_server.taskflow.domain.taskflow_state import (
    DispatchDecisionRecord,
    TaskflowState,
)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _normalized_status(value: Any) -> str:
    return _enum_value(value).strip().lower().replace(" ", "_").replace("-", "_")


class TaskRunLivenessService:
    """Classify the UI-facing liveness state for one TaskRun."""

    _QUEUED = {"queued", "pending", "scheduled"}
    _CLAIMED = {"claimed", "assigned", "accepted"}
    _RUNNING = {"running", "in_progress", "executing", "working", "started"}
    _WAITING_USER = {
        "waiting_user",
        "waiting_for_user",
        "requires_action",
        "requires_approval",
        "paused",
        "blocked_by_user",
    }
    _BLOCKED = {"blocked", "stalled", "waiting_dependency"}
    _COMPLETED = {"completed", "complete", "succeeded", "success", "done"}
    _FAILED = {"failed", "error", "cancelled", "canceled", "timed_out", "timeout"}

    def classify(
        self,
        task_run: TaskRun,
        *,
        agent_run: dict[str, Any] | None = None,
        agent_run_id: str | None = None,
        runtime_available: bool = False,
        runtime_error: str = "",
        claim: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_status = _normalized_status(task_run.status)
        agent_status = self._agent_status(agent_run)
        reason = ""
        source = "task_run"

        if agent_status:
            state = self._state_from_agent_status(agent_status)
            if state:
                return self._result(
                    state,
                    reason=self._reason_for_agent_status(state, agent_status),
                    source="agent_run",
                    task_status=task_status,
                    agent_status=agent_status,
                    task_run=task_run,
                )

        claim_status = _normalized_status(_dict(claim).get("status"))
        if claim_status in self._CLAIMED:
            return self._result(
                "claimed",
                reason="AgentRun has an active claim.",
                source="agent_claim",
                task_status=task_status,
                agent_status=agent_status,
                task_run=task_run,
            )

        if task_status == TaskRunStatus.SUCCEEDED.value:
            state = "completed"
            reason = "TaskRun completed successfully."
        elif task_status in {
            TaskRunStatus.FAILED.value,
            TaskRunStatus.CANCELLED.value,
        }:
            state = "failed"
            reason = self._failure_reason(task_run)
        elif task_status == TaskRunStatus.RUNNING.value:
            state = "running"
            reason = "TaskRun is marked running."
        elif agent_run_id and not runtime_available:
            state = "runtime_unavailable"
            reason = runtime_error or "AgentRun exists but runtime detail is unavailable."
        elif task_status == TaskRunStatus.DISPATCHED.value:
            state = "queued" if task_run.metadata.get("selected_executor_id") else "needs_recovery"
            reason = (
                "TaskRun has a selected executor and is waiting for runtime updates."
                if state == "queued"
                else "TaskRun is dispatched but has no linked AgentRun."
            )
        elif task_status == TaskRunStatus.PENDING.value:
            if not task_run.dispatch_ref_id:
                state = "pending_dispatch"
                reason = "TaskRun has not been authorized for dispatch."
            elif not task_run.metadata.get("selected_executor_id"):
                state = "agent_selection_required"
                reason = self._agent_selection_reason(task_run)
            else:
                state = "queued"
                reason = "TaskRun is waiting for runtime claim."
        else:
            state = "needs_recovery"
            reason = f"TaskRun is in an unrecognized state: {task_status or 'unknown'}."

        return self._result(
            state,
            reason=reason,
            source=source,
            task_status=task_status,
            agent_status=agent_status,
            task_run=task_run,
        )

    def _agent_status(self, agent_run: dict[str, Any] | None) -> str:
        if not agent_run:
            return ""
        for key in ("status", "state", "runtime_status", "lifecycle_state"):
            status = _normalized_status(agent_run.get(key))
            if status:
                return status
        return ""

    def _state_from_agent_status(self, agent_status: str) -> str:
        if agent_status in self._QUEUED:
            return "queued"
        if agent_status in self._CLAIMED:
            return "claimed"
        if agent_status in self._RUNNING:
            return "running"
        if agent_status in self._WAITING_USER:
            return "waiting_user"
        if agent_status in self._BLOCKED:
            return "blocked"
        if agent_status in self._COMPLETED:
            return "completed"
        if agent_status in self._FAILED:
            return "failed"
        return ""

    def _reason_for_agent_status(self, state: str, agent_status: str) -> str:
        if state == "blocked":
            return "AgentRun is blocked."
        if state == "waiting_user":
            return "AgentRun is waiting for user input."
        if state == "failed":
            return "AgentRun failed."
        if state == "completed":
            return "AgentRun completed."
        return f"AgentRun status is {agent_status}."

    def _agent_selection_reason(self, task_run: TaskRun) -> str:
        dispatch_result = _dict(task_run.metadata.get("dispatch_result"))
        reason = str(dispatch_result.get("reason") or "").strip()
        if reason:
            return reason
        return "Dispatch was authorized, but no executor or AgentRun has been selected."

    def _failure_reason(self, task_run: TaskRun) -> str:
        metadata = task_run.metadata
        for key in ("failure_reason", "error", "blocked_reason", "reason"):
            reason = str(metadata.get(key) or "").strip()
            if reason:
                return reason
        return "TaskRun failed or was cancelled."

    def _result(
        self,
        state: str,
        *,
        reason: str,
        source: str,
        task_status: str,
        agent_status: str,
        task_run: TaskRun,
    ) -> dict[str, Any]:
        return {
            "state": state,
            "reason": reason,
            "source": source,
            "task_run_status": task_status,
            "agent_run_status": agent_status or None,
            "needs_attention": state
            in {
                "agent_selection_required",
                "waiting_user",
                "blocked",
                "failed",
                "needs_recovery",
                "runtime_unavailable",
            },
            "updated_at": task_run.updated_at,
        }


class TaskflowRuntimeProjectionService:
    """Build a Taskflow runtime view from ProjectState traceability."""

    def __init__(self, *, liveness_service: TaskRunLivenessService | None = None) -> None:
        self.liveness_service = liveness_service or TaskRunLivenessService()

    def project(
        self,
        *,
        taskflow: TaskflowState,
        project: ProjectState,
        runtime_control_plane: Any | None = None,
        event_limit: int = 50,
    ) -> dict[str, Any]:
        task_runs = self._task_runs_for_taskflow(taskflow, project)
        items = [
            self._project_task_run(
                taskflow=taskflow,
                project=project,
                task_run=task_run,
                runtime_control_plane=runtime_control_plane,
                event_limit=event_limit,
            )
            for task_run in task_runs
        ]
        return {
            "ok": True,
            "taskflow_id": taskflow.meta.taskflow_id,
            "task_runs": items,
            "liveness_summary": self._summary(items),
        }

    def _task_runs_for_taskflow(
        self, taskflow: TaskflowState, project: ProjectState
    ) -> list[TaskRun]:
        referenced = list(taskflow.outputs.task_run_refs)
        known = {run.id: run for run in project.traceability.task_runs}
        ordered: list[TaskRun] = [
            known[run_id] for run_id in referenced if run_id in known
        ]
        seen = {run.id for run in ordered}
        for run in project.traceability.task_runs:
            if run.id in seen:
                continue
            if run.metadata.get("taskflow_id") == taskflow.meta.taskflow_id:
                ordered.append(run)
                seen.add(run.id)
                continue
            if run.goal_id and run.goal_id == taskflow.meta.goal_id:
                ordered.append(run)
                seen.add(run.id)
        return ordered

    def _project_task_run(
        self,
        *,
        taskflow: TaskflowState,
        project: ProjectState,
        task_run: TaskRun,
        runtime_control_plane: Any | None,
        event_limit: int,
    ) -> dict[str, Any]:
        agent_run_id = self._linked_agent_run_id(project, task_run)
        runtime_detail = self._load_runtime_detail(
            runtime_control_plane,
            agent_run_id=agent_run_id,
            event_limit=event_limit,
        )
        runtime_error = str(runtime_detail.get("runtime_error") or "")
        agent_run = _dict(runtime_detail.get("agent_run"))
        if not agent_run and agent_run_id:
            agent_run = {"id": agent_run_id}
        events = [_dict(event) for event in _list(runtime_detail.get("events"))]
        artifacts = [
            _dict(artifact) for artifact in _list(runtime_detail.get("artifacts"))
        ]
        liveness = self.liveness_service.classify(
            task_run,
            agent_run=agent_run,
            agent_run_id=agent_run_id,
            runtime_available=bool(runtime_control_plane),
            runtime_error=runtime_error,
            claim=_dict(runtime_detail.get("claim")),
        )
        item = {
            "task_run": task_run.to_dict(),
            "work_item": self._work_item(project, task_run),
            "dispatch_decision": self._dispatch_decision(taskflow, task_run),
            "agent_run": agent_run or None,
            "events": events,
            "artifacts": artifacts,
            "liveness": liveness,
        }
        if runtime_error:
            item["runtime_error"] = runtime_error
        return item

    def _work_item(
        self, project: ProjectState, task_run: TaskRun
    ) -> dict[str, Any] | None:
        for item in project.list_work_items():
            if item.id == task_run.work_item_id:
                return item.to_dict()
        fallback = self._work_item_from_metadata(task_run)
        return fallback.to_dict() if fallback else None

    def _work_item_from_metadata(self, task_run: TaskRun) -> WorkItem | None:
        title = str(task_run.metadata.get("work_item_title") or "").strip()
        if not title:
            return None
        return WorkItem(
            id=task_run.work_item_id,
            project_id=task_run.project_id,
            title=title,
            description=str(task_run.metadata.get("work_item_description") or ""),
            type=str(task_run.metadata.get("work_item_type") or "implementation"),
        )

    def _dispatch_decision(
        self, taskflow: TaskflowState, task_run: TaskRun
    ) -> dict[str, Any] | None:
        decision_id = str(
            task_run.dispatch_ref_id
            or task_run.metadata.get("dispatch_decision_id")
            or ""
        )
        for decision in taskflow.outputs.dispatch_decisions:
            if isinstance(decision, DispatchDecisionRecord) and decision.id == decision_id:
                return decision.to_dict()
        return None

    def _linked_agent_run_id(self, project: ProjectState, task_run: TaskRun) -> str:
        for link in project.traceability.task_run_links:
            if (
                link.source_type == TraceEntityType.TASK_RUN
                and link.source_id == task_run.id
                and link.target_type == TraceEntityType.AGENT_RUN
            ):
                return link.target_id
            if (
                link.target_type == TraceEntityType.TASK_RUN
                and link.target_id == task_run.id
                and link.source_type == TraceEntityType.AGENT_RUN
            ):
                return link.source_id
        ref = _dict(task_run.metadata.get("agent_run_ref"))
        if ref.get("id"):
            return str(ref["id"])
        return ""

    def _load_runtime_detail(
        self,
        runtime_control_plane: Any | None,
        *,
        agent_run_id: str,
        event_limit: int,
    ) -> dict[str, Any]:
        if runtime_control_plane is None or not agent_run_id:
            return {}
        try:
            if hasattr(runtime_control_plane, "load_agent_run_detail"):
                detail = runtime_control_plane.load_agent_run_detail(
                    agent_run_id,
                    event_limit=event_limit,
                )
                return _dict(detail)
            detail: dict[str, Any] = {}
            if hasattr(runtime_control_plane, "agent_run_to_dict"):
                detail["agent_run"] = runtime_control_plane.agent_run_to_dict(
                    agent_run_id
                )
            if hasattr(runtime_control_plane, "list_events"):
                events = runtime_control_plane.list_events(
                    agent_run_id,
                    limit=event_limit,
                )
                detail["events"] = [
                    event.to_dict() if hasattr(event, "to_dict") else event
                    for event in events
                ]
            if hasattr(runtime_control_plane, "artifacts_to_dict"):
                detail["artifacts"] = runtime_control_plane.artifacts_to_dict(
                    agent_run_id
                )
            return detail
        except Exception as exc:  # pragma: no cover - defensive projection path
            return {"runtime_error": str(exc)}

    def _summary(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        counts = Counter(
            str(_dict(item.get("liveness")).get("state") or "unknown")
            for item in items
        )
        needs_attention = sum(
            1
            for item in items
            if bool(_dict(item.get("liveness")).get("needs_attention"))
        )
        return {
            "total": len(items),
            "counts": dict(counts),
            "needs_attention_count": needs_attention,
        }


__all__ = ["TaskRunLivenessService", "TaskflowRuntimeProjectionService"]

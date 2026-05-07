"""Taskflow planning and dispatch service."""

from __future__ import annotations

import uuid
from typing import Any

from reuleauxcoder.domain.taskflow.models import (
    DecisionPoint,
    DispatchDecisionRecord,
    DispatchDecisionStatus,
    GoalRecord,
    GoalStatus,
    IssueDraftRecord,
    PlanBriefRecord,
    PlanStatus,
    TaskDraftRecord,
    TaskDraftStatus,
)
from labrastro_server.services.agent_runtime.control_plane import RuntimeTaskRequest
from labrastro_server.services.taskflow.in_memory_store import InMemoryTaskflowStore
from labrastro_server.services.taskflow.scheduler import TaskflowScheduler
from labrastro_server.services.taskflow.store import TaskflowStore


TASKFLOW_WORKFLOW_MODE = "taskflow"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


class TaskflowService:
    """Service facade for Goal/Plan/IssueDraft/TaskDraft lifecycle."""

    def __init__(
        self,
        store: TaskflowStore | None = None,
        *,
        runtime_control_plane: Any | None = None,
        scheduler: TaskflowScheduler | None = None,
    ) -> None:
        self.store = store or InMemoryTaskflowStore()
        self.runtime_control_plane = runtime_control_plane
        self.scheduler = scheduler or TaskflowScheduler()

    def _assert_goal_access(
        self, goal: GoalRecord, peer_id: str | None = None
    ) -> GoalRecord:
        if peer_id and goal.peer_id and goal.peer_id != peer_id:
            raise PermissionError("taskflow goal belongs to another peer")
        return goal

    def _get_goal_for_peer(
        self, goal_id: str, peer_id: str | None = None
    ) -> GoalRecord:
        return self._assert_goal_access(self.store.get_goal(goal_id), peer_id)

    def _get_draft_for_peer(
        self, draft_id: str, peer_id: str | None = None
    ) -> tuple[TaskDraftRecord, GoalRecord]:
        draft = self.store.get_task_draft(draft_id)
        goal = self._get_goal_for_peer(draft.goal_id, peer_id)
        return draft, goal

    def create_goal(
        self,
        *,
        title: str,
        prompt: str,
        session_id: str | None = None,
        peer_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        goal_id: str | None = None,
    ) -> GoalRecord:
        goal = GoalRecord(
            id=goal_id or _new_id("goal"),
            title=title.strip() or "Untitled Taskflow goal",
            prompt=prompt,
            session_id=session_id,
            peer_id=peer_id,
            metadata=dict(metadata or {}),
        )
        created = self.store.create_goal(goal)
        self.store.append_event(created.id, "goal_created", {"goal": created.to_dict()})
        return created

    def get_goal(self, goal_id: str, *, peer_id: str | None = None) -> GoalRecord:
        return self._get_goal_for_peer(goal_id, peer_id)

    def record_brief(
        self,
        goal_id: str,
        *,
        summary: str,
        decision_points: list[dict[str, Any]] | None = None,
        issue_drafts: list[dict[str, Any]] | None = None,
        task_drafts: list[dict[str, Any]] | None = None,
        ready: bool = False,
        metadata: dict[str, Any] | None = None,
        peer_id: str | None = None,
    ) -> PlanBriefRecord:
        self._get_goal_for_peer(goal_id, peer_id)
        existing = self.store.get_brief(goal_id)
        effective_points = decision_points
        if effective_points is None and existing is not None:
            effective_points = [point.to_dict() for point in existing.decision_points]
        brief = PlanBriefRecord(
            id=_new_id("brief"),
            goal_id=goal_id,
            summary=summary if summary else (existing.summary if existing else ""),
            decision_points=[
                self._decision_point(point)
                for point in list(effective_points or [])
            ],
            status=PlanStatus.READY if ready else PlanStatus.DRAFT,
            metadata=dict(metadata or {}),
        )
        saved = self.store.upsert_brief(brief)
        self.store.append_event(
            goal_id, "brief_recorded", {"brief": saved.to_dict(), "ready": ready}
        )
        for issue_payload in issue_drafts or []:
            issue = self.create_issue_draft(
                goal_id, peer_id=peer_id, **_dict(issue_payload)
            )
            for task_payload in _dict(issue_payload).get("task_drafts") or []:
                payload = _dict(task_payload)
                payload.setdefault("issue_draft_id", issue.id)
                self.create_task_draft(goal_id, peer_id=peer_id, **payload)
        for task_payload in task_drafts or []:
            self.create_task_draft(goal_id, peer_id=peer_id, **_dict(task_payload))
        if ready:
            goal = self.store.get_goal(goal_id)
            goal.status = GoalStatus.READY
            self.store.update_goal(goal)
        return saved

    def create_issue_draft(
        self,
        goal_id: str,
        *,
        title: str,
        description: str = "",
        status: str = "proposed",
        metadata: dict[str, Any] | None = None,
        issue_id: str | None = None,
        id: str | None = None,
        peer_id: str | None = None,
        **_: Any,
    ) -> IssueDraftRecord:
        self._get_goal_for_peer(goal_id, peer_id)
        issue = IssueDraftRecord(
            id=id or issue_id or _new_id("issue-draft"),
            goal_id=goal_id,
            title=title,
            description=description,
            status=status,
            metadata=dict(metadata or {}),
        )
        created = self.store.create_issue_draft(issue)
        self.store.append_event(
            goal_id, "issue_draft_created", {"issue_draft": created.to_dict()}
        )
        return created

    def create_task_draft(
        self,
        goal_id: str,
        *,
        title: str,
        prompt: str,
        issue_draft_id: str | None = None,
        required_capabilities: list[str] | None = None,
        preferred_capabilities: list[str] | None = None,
        task_type: str | None = None,
        workspace_root: str | None = None,
        repo_url: str | None = None,
        execution_location: str | None = None,
        manual_agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        task_id: str | None = None,
        id: str | None = None,
        status: str = TaskDraftStatus.PROPOSED.value,
        peer_id: str | None = None,
        **_: Any,
    ) -> TaskDraftRecord:
        self._get_goal_for_peer(goal_id, peer_id)
        draft = TaskDraftRecord(
            id=id or task_id or _new_id("task-draft"),
            goal_id=goal_id,
            issue_draft_id=issue_draft_id,
            title=title,
            prompt=prompt,
            status=status,
            required_capabilities=_string_list(required_capabilities),
            preferred_capabilities=_string_list(preferred_capabilities),
            task_type=task_type,
            workspace_root=workspace_root,
            repo_url=repo_url,
            execution_location=execution_location,
            manual_agent_id=manual_agent_id,
            metadata=dict(metadata or {}),
        )
        created = self.store.create_task_draft(draft)
        self.store.append_event(
            goal_id, "task_draft_created", {"task_draft": created.to_dict()}
        )
        return created

    def get_issue_draft(
        self, issue_draft_id: str, *, peer_id: str | None = None
    ) -> IssueDraftRecord:
        issue = self.store.get_issue_draft(issue_draft_id)
        self._get_goal_for_peer(issue.goal_id, peer_id)
        return issue

    def get_task_draft(
        self, task_draft_id: str, *, peer_id: str | None = None
    ) -> TaskDraftRecord:
        draft, _goal = self._get_draft_for_peer(task_draft_id, peer_id)
        return draft

    def confirm_goal(
        self, goal_id: str, *, peer_id: str | None = None, confirmed_by: str = "user"
    ) -> GoalRecord:
        goal = self._get_goal_for_peer(goal_id, peer_id)
        if goal.status == GoalStatus.CANCELLED:
            raise ValueError("cancelled taskflow goals cannot be confirmed")
        goal.metadata.setdefault("confirmed_by", confirmed_by)
        goal.status = GoalStatus.CONFIRMED
        saved = self.store.update_goal(goal)
        brief = self.store.get_brief(goal_id)
        if brief is not None:
            brief.status = PlanStatus.CONFIRMED
            self.store.upsert_brief(brief)
        for draft in self.store.list_task_drafts(goal_id):
            if draft.status in {
                TaskDraftStatus.PROPOSED,
                TaskDraftStatus.READY,
            }:
                draft.status = TaskDraftStatus.CONFIRMED
                self.store.update_task_draft(draft)
        self.store.append_event(goal_id, "goal_confirmed", {"goal": saved.to_dict()})
        return saved

    def cancel_goal(
        self,
        goal_id: str,
        *,
        reason: str = "user_cancelled",
        peer_id: str | None = None,
    ) -> GoalRecord:
        goal = self._get_goal_for_peer(goal_id, peer_id)
        goal.status = GoalStatus.CANCELLED
        saved = self.store.update_goal(goal)
        brief = self.store.get_brief(goal_id)
        if brief is not None:
            brief.status = PlanStatus.CANCELLED
            self.store.upsert_brief(brief)
        for draft in self.store.list_task_drafts(goal_id):
            if draft.status not in {
                TaskDraftStatus.DISPATCHED,
                TaskDraftStatus.CANCELLED,
            }:
                draft.status = TaskDraftStatus.CANCELLED
                self.store.update_task_draft(draft)
        self.store.append_event(
            goal_id, "goal_cancelled", {"goal": saved.to_dict(), "reason": reason}
        )
        return saved

    def dispatch_task_draft(
        self,
        draft_id: str,
        *,
        manual_agent_id: str | None = None,
        peer_id: str | None = None,
        source: str = "taskflow",
        metadata: dict[str, Any] | None = None,
    ) -> DispatchDecisionRecord:
        if self.runtime_control_plane is None:
            raise RuntimeError("agent runtime control plane unavailable")
        draft, goal = self._get_draft_for_peer(draft_id, peer_id)
        if goal.status != GoalStatus.CONFIRMED:
            raise ValueError("taskflow goal must be confirmed before dispatch")
        if draft.runtime_task_id:
            raise ValueError("task draft is already dispatched")
        if draft.status not in {
            TaskDraftStatus.CONFIRMED,
            TaskDraftStatus.READY,
            TaskDraftStatus.NEEDS_ASSIGNMENT,
        }:
            raise ValueError(f"task draft is not ready for dispatch: {draft.status.value}")
        if manual_agent_id:
            draft.manual_agent_id = manual_agent_id
            self.store.update_task_draft(draft)

        running = self.runtime_control_plane.list_tasks(limit=500)
        result = self.scheduler.choose_agent(
            draft,
            runtime_snapshot=self.runtime_control_plane.runtime_snapshot,
            running_tasks=running,
            manual_agent_id=manual_agent_id,
        )
        decision = DispatchDecisionRecord(
            id=_new_id("dispatch"),
            task_draft_id=draft.id,
            status=(
                DispatchDecisionStatus.MANUAL_OVERRIDE
                if result.selected and result.manual_override
                else DispatchDecisionStatus.SELECTED
                if result.selected
                else DispatchDecisionStatus.NEEDS_ASSIGNMENT
            ),
            selected_agent_id=result.selected_agent_id,
            candidates=result.candidates,
            filtered=result.filtered,
            score_summary=result.score_summary,
            manual_override=result.manual_override,
            reason=result.reason,
            metadata={"source": source, **dict(metadata or {})},
        )
        if not result.selected_agent_id:
            draft.status = TaskDraftStatus.NEEDS_ASSIGNMENT
            self.store.update_task_draft(draft)
            saved_decision = self.store.append_dispatch_decision(decision)
            self.store.append_event(
                draft.goal_id,
                "task_draft_needs_assignment",
                {"task_draft": draft.to_dict(), "decision": saved_decision.to_dict()},
            )
            return saved_decision

        runtime_task = self.runtime_control_plane.submit_task(
            self._runtime_request(
                draft, result.selected_agent_id, result.candidates, source=source
            )
        )
        draft.status = TaskDraftStatus.DISPATCHED
        draft.runtime_task_id = runtime_task.id
        self.store.update_task_draft(draft)
        decision.runtime_task_id = runtime_task.id
        saved_decision = self.store.append_dispatch_decision(decision)
        self.store.append_event(
            draft.goal_id,
            "task_draft_dispatched",
            {
                "task_draft": draft.to_dict(),
                "decision": saved_decision.to_dict(),
                "runtime_task": self.runtime_control_plane.task_to_dict(
                    runtime_task.id
                ),
            },
        )
        return saved_decision

    def assign_task_draft(
        self, draft_id: str, *, agent_id: str, peer_id: str | None = None
    ) -> DispatchDecisionRecord:
        return self.dispatch_task_draft(
            draft_id, manual_agent_id=agent_id, peer_id=peer_id, source="manual"
        )

    def list_dispatch_decisions(
        self, task_draft_id: str, *, peer_id: str | None = None
    ) -> list[DispatchDecisionRecord]:
        self._get_draft_for_peer(task_draft_id, peer_id)
        return self.store.list_dispatch_decisions(task_draft_id)

    def load_goal_detail(
        self, goal_id: str, *, peer_id: str | None = None
    ) -> dict[str, Any]:
        goal = self._get_goal_for_peer(goal_id, peer_id)
        issues = self.store.list_issue_drafts(goal_id)
        tasks = self.store.list_task_drafts(goal_id)
        task_details = []
        for draft in tasks:
            payload = draft.to_dict()
            if draft.runtime_task_id and self.runtime_control_plane is not None:
                try:
                    payload["runtime_task"] = self.runtime_control_plane.task_to_dict(
                        draft.runtime_task_id
                    )
                except Exception:
                    payload["runtime_task"] = None
            payload["dispatch_decisions"] = [
                decision.to_dict()
                for decision in self.store.list_dispatch_decisions(draft.id)
            ]
            task_details.append(payload)
        return {
            "goal": goal.to_dict(),
            "brief": (
                self.store.get_brief(goal_id).to_dict()
                if self.store.get_brief(goal_id) is not None
                else None
            ),
            "issue_drafts": [issue.to_dict() for issue in issues],
            "task_drafts": task_details,
        }

    def list_events(
        self,
        goal_id: str,
        *,
        after_seq: int = 0,
        timeout_sec: float = 0.0,
        peer_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._get_goal_for_peer(goal_id, peer_id)
        events = self.store.wait_events(
            goal_id, after_seq=after_seq, timeout_sec=timeout_sec
        )
        return [event.to_dict() for event in events]

    def _runtime_request(
        self,
        draft: TaskDraftRecord,
        selected_agent_id: str,
        candidates: list[dict[str, Any]],
        *,
        source: str = "taskflow",
    ) -> RuntimeTaskRequest:
        selected_candidate = next(
            (
                candidate
                for candidate in candidates
                if candidate.get("agent_id") == selected_agent_id
            ),
            {},
        )
        metadata = dict(draft.metadata)
        metadata.setdefault("dispatch_source", source)
        metadata.setdefault("taskflow_goal_id", draft.goal_id)
        metadata.setdefault("taskflow_task_draft_id", draft.id)
        if draft.issue_draft_id:
            metadata.setdefault("taskflow_issue_draft_id", draft.issue_draft_id)
        if draft.workspace_root:
            metadata.setdefault("workspace_root", draft.workspace_root)
        if draft.repo_url:
            metadata.setdefault("repo_url", draft.repo_url)
        metadata.setdefault(
            "dispatch_signals",
            {
                "required_capabilities": list(draft.required_capabilities),
                "preferred_capabilities": list(draft.preferred_capabilities),
                "task_type": draft.task_type,
                "manual_agent_id": draft.manual_agent_id,
            },
        )
        return RuntimeTaskRequest(
            issue_id=draft.issue_draft_id or draft.goal_id,
            agent_id=selected_agent_id,
            prompt=draft.prompt,
            parent_task_id=(
                str(metadata["parent_task_id"])
                if metadata.get("parent_task_id") is not None
                else None
            ),
            trigger_comment_id=(
                str(metadata["trigger_comment_id"])
                if metadata.get("trigger_comment_id") is not None
                else None
            ),
            runtime_profile_id=(
                str(selected_candidate.get("runtime_profile") or "") or None
            ),
            execution_location=draft.execution_location,
            metadata=metadata,
        )

    def _decision_point(self, payload: dict[str, Any]) -> DecisionPoint:
        data = dict(payload)
        data.setdefault("id", _new_id("decision"))
        return DecisionPoint.from_dict(data)


TASKFLOW_SYSTEM_PROMPT = """Taskflow workflow is active.

This is separate from the normal coder/planner/debugger session mode. Focus on
turning the user's goal into a clear long-running task plan before execution.

Work in this order:
1. Clarify the user's goal, scope, constraints, and success criteria.
2. Surface decision points for the user instead of silently guessing.
3. Record a concise brief, decision points, issue drafts, and task drafts with
   the Taskflow planning tool.
4. Do not create runtime tasks before explicit user confirmation.
5. Keep user input simple; infer capabilities and task type as internal routing
   hints only when creating task drafts.
"""

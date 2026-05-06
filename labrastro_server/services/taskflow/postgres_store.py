"""Postgres-backed Taskflow store."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from reuleauxcoder.domain.taskflow.models import (
    DispatchDecisionRecord,
    GoalRecord,
    IssueDraftRecord,
    PlanBriefRecord,
    TaskDraftRecord,
    TaskflowEvent,
)

try:  # pragma: no cover - import availability is environment dependent.
    from sqlalchemy import text
except ImportError:  # pragma: no cover
    text = None


def _require_sqlalchemy() -> None:
    if text is None:
        raise RuntimeError("Postgres taskflow store requires sqlalchemy and psycopg.")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_array(value: Any) -> str:
    return json.dumps(value if isinstance(value, list) else [], ensure_ascii=False)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _row_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: _row_value(value) for key, value in dict(row).items()}


class PostgresTaskflowStore:
    """Taskflow store using the same Postgres control-plane database."""

    def __init__(self, engine: Any) -> None:
        _require_sqlalchemy()
        self.engine = engine

    def create_goal(self, goal: GoalRecord) -> GoalRecord:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_taskflow_goals (
                        id, title, prompt, status, session_id, peer_id, metadata
                    ) VALUES (
                        :id, :title, :prompt, :status, :session_id, :peer_id,
                        CAST(:metadata AS JSONB)
                    )
                    """
                ),
                {
                    "id": goal.id,
                    "title": goal.title,
                    "prompt": goal.prompt,
                    "status": goal.status.value,
                    "session_id": goal.session_id,
                    "peer_id": goal.peer_id,
                    "metadata": _json(goal.metadata),
                },
            )
        return self.get_goal(goal.id)

    def get_goal(self, goal_id: str) -> GoalRecord:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM labrastro_taskflow_goals WHERE id=:goal_id"),
                {"goal_id": goal_id},
            ).mappings().first()
        if row is None:
            raise KeyError(f"taskflow goal not found: {goal_id}")
        return GoalRecord.from_dict(_row_dict(row))

    def update_goal(self, goal: GoalRecord) -> GoalRecord:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE labrastro_taskflow_goals
                    SET title=:title, prompt=:prompt, status=:status,
                        session_id=:session_id, peer_id=:peer_id,
                        metadata=CAST(:metadata AS JSONB), updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": goal.id,
                    "title": goal.title,
                    "prompt": goal.prompt,
                    "status": goal.status.value,
                    "session_id": goal.session_id,
                    "peer_id": goal.peer_id,
                    "metadata": _json(goal.metadata),
                },
            )
        if result.rowcount == 0:
            raise KeyError(f"taskflow goal not found: {goal.id}")
        return self.get_goal(goal.id)

    def upsert_brief(self, brief: PlanBriefRecord) -> PlanBriefRecord:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_taskflow_briefs (
                        id, goal_id, summary, decision_points, status, version,
                        metadata
                    ) VALUES (
                        :id, :goal_id, :summary, CAST(:decision_points AS JSONB),
                        :status, :version, CAST(:metadata AS JSONB)
                    )
                    ON CONFLICT (goal_id) DO UPDATE
                    SET id=EXCLUDED.id,
                        summary=EXCLUDED.summary,
                        decision_points=EXCLUDED.decision_points,
                        status=EXCLUDED.status,
                        version=labrastro_taskflow_briefs.version + 1,
                        metadata=EXCLUDED.metadata,
                        updated_at=now()
                    """
                ),
                {
                    "id": brief.id,
                    "goal_id": brief.goal_id,
                    "summary": brief.summary,
                    "decision_points": _json_array(
                        [point.to_dict() for point in brief.decision_points]
                    ),
                    "status": brief.status.value,
                    "version": brief.version,
                    "metadata": _json(brief.metadata),
                },
            )
        saved = self.get_brief(brief.goal_id)
        if saved is None:
            raise KeyError(f"taskflow goal not found: {brief.goal_id}")
        return saved

    def get_brief(self, goal_id: str) -> PlanBriefRecord | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM labrastro_taskflow_briefs WHERE goal_id=:goal_id"),
                {"goal_id": goal_id},
            ).mappings().first()
        return PlanBriefRecord.from_dict(_row_dict(row)) if row is not None else None

    def create_issue_draft(self, issue: IssueDraftRecord) -> IssueDraftRecord:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_taskflow_issue_drafts (
                        id, goal_id, title, description, status, metadata
                    ) VALUES (
                        :id, :goal_id, :title, :description, :status,
                        CAST(:metadata AS JSONB)
                    )
                    """
                ),
                {
                    "id": issue.id,
                    "goal_id": issue.goal_id,
                    "title": issue.title,
                    "description": issue.description,
                    "status": issue.status,
                    "metadata": _json(issue.metadata),
                },
            )
        return self.get_issue_draft(issue.id)

    def get_issue_draft(self, issue_draft_id: str) -> IssueDraftRecord:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM labrastro_taskflow_issue_drafts WHERE id=:issue_id"),
                {"issue_id": issue_draft_id},
            ).mappings().first()
        if row is None:
            raise KeyError(f"taskflow issue draft not found: {issue_draft_id}")
        return IssueDraftRecord.from_dict(_row_dict(row))

    def list_issue_drafts(self, goal_id: str) -> list[IssueDraftRecord]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_taskflow_issue_drafts
                    WHERE goal_id=:goal_id
                    ORDER BY created_at ASC
                    """
                ),
                {"goal_id": goal_id},
            ).mappings()
            return [IssueDraftRecord.from_dict(_row_dict(row)) for row in rows]

    def create_task_draft(self, draft: TaskDraftRecord) -> TaskDraftRecord:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_taskflow_task_drafts (
                        id, goal_id, issue_draft_id, title, prompt, status,
                        required_capabilities, preferred_capabilities, task_type,
                        workspace_root, repo_url, execution_location,
                        manual_agent_id, runtime_task_id, metadata
                    ) VALUES (
                        :id, :goal_id, :issue_draft_id, :title, :prompt, :status,
                        CAST(:required_capabilities AS JSONB),
                        CAST(:preferred_capabilities AS JSONB), :task_type,
                        :workspace_root, :repo_url, :execution_location,
                        :manual_agent_id, :runtime_task_id,
                        CAST(:metadata AS JSONB)
                    )
                    """
                ),
                self._task_params(draft),
            )
        return self.get_task_draft(draft.id)

    def get_task_draft(self, draft_id: str) -> TaskDraftRecord:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM labrastro_taskflow_task_drafts WHERE id=:draft_id"),
                {"draft_id": draft_id},
            ).mappings().first()
        if row is None:
            raise KeyError(f"taskflow task draft not found: {draft_id}")
        return TaskDraftRecord.from_dict(_row_dict(row))

    def update_task_draft(self, draft: TaskDraftRecord) -> TaskDraftRecord:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE labrastro_taskflow_task_drafts
                    SET issue_draft_id=:issue_draft_id, title=:title,
                        prompt=:prompt, status=:status,
                        required_capabilities=CAST(:required_capabilities AS JSONB),
                        preferred_capabilities=CAST(:preferred_capabilities AS JSONB),
                        task_type=:task_type, workspace_root=:workspace_root,
                        repo_url=:repo_url, execution_location=:execution_location,
                        manual_agent_id=:manual_agent_id,
                        runtime_task_id=:runtime_task_id,
                        metadata=CAST(:metadata AS JSONB), updated_at=now()
                    WHERE id=:id
                    """
                ),
                self._task_params(draft),
            )
        if result.rowcount == 0:
            raise KeyError(f"taskflow task draft not found: {draft.id}")
        return self.get_task_draft(draft.id)

    def list_task_drafts(
        self, goal_id: str, *, issue_draft_id: str | None = None
    ) -> list[TaskDraftRecord]:
        clauses = ["goal_id=:goal_id"]
        params = {"goal_id": goal_id}
        if issue_draft_id is not None:
            clauses.append("issue_draft_id=:issue_draft_id")
            params["issue_draft_id"] = issue_draft_id
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT * FROM labrastro_taskflow_task_drafts
                    WHERE {' AND '.join(clauses)}
                    ORDER BY created_at ASC
                    """
                ),
                params,
            ).mappings()
            return [TaskDraftRecord.from_dict(_row_dict(row)) for row in rows]

    def append_dispatch_decision(
        self, decision: DispatchDecisionRecord
    ) -> DispatchDecisionRecord:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_taskflow_dispatch_decisions (
                        id, task_draft_id, status, selected_agent_id, candidates,
                        filtered, score_summary, manual_override, reason,
                        runtime_task_id, metadata
                    ) VALUES (
                        :id, :task_draft_id, :status, :selected_agent_id,
                        CAST(:candidates AS JSONB), CAST(:filtered AS JSONB),
                        CAST(:score_summary AS JSONB), :manual_override, :reason,
                        :runtime_task_id, CAST(:metadata AS JSONB)
                    )
                    """
                ),
                {
                    "id": decision.id,
                    "task_draft_id": decision.task_draft_id,
                    "status": decision.status.value,
                    "selected_agent_id": decision.selected_agent_id,
                    "candidates": _json_array(decision.candidates),
                    "filtered": _json_array(decision.filtered),
                    "score_summary": _json(decision.score_summary),
                    "manual_override": decision.manual_override,
                    "reason": decision.reason,
                    "runtime_task_id": decision.runtime_task_id,
                    "metadata": _json(decision.metadata),
                },
            )
        return self.list_dispatch_decisions(decision.task_draft_id)[-1]

    def list_dispatch_decisions(
        self, task_draft_id: str
    ) -> list[DispatchDecisionRecord]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_taskflow_dispatch_decisions
                    WHERE task_draft_id=:task_draft_id
                    ORDER BY created_at ASC
                    """
                ),
                {"task_draft_id": task_draft_id},
            ).mappings()
            return [
                DispatchDecisionRecord.from_dict(_row_dict(row)) for row in rows
            ]

    def append_event(
        self, goal_id: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> TaskflowEvent:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    UPDATE labrastro_taskflow_goals
                    SET next_event_seq = next_event_seq + 1
                    WHERE id=:goal_id
                    RETURNING next_event_seq - 1 AS seq
                    """
                ),
                {"goal_id": goal_id},
            ).mappings().first()
            if row is None:
                raise KeyError(f"taskflow goal not found: {goal_id}")
            seq = int(row["seq"])
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_taskflow_events(goal_id, seq, type, payload)
                    VALUES (:goal_id, :seq, :type, CAST(:payload AS JSONB))
                    """
                ),
                {
                    "goal_id": goal_id,
                    "seq": seq,
                    "type": event_type,
                    "payload": _json(payload or {}),
                },
            )
        return self.list_events(goal_id, after_seq=seq - 1)[0]

    def list_events(self, goal_id: str, *, after_seq: int = 0) -> list[TaskflowEvent]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT goal_id, seq, type, payload, created_at
                    FROM labrastro_taskflow_events
                    WHERE goal_id=:goal_id AND seq > :after_seq
                    ORDER BY seq ASC
                    """
                ),
                {"goal_id": goal_id, "after_seq": after_seq},
            ).mappings()
            return [TaskflowEvent.from_dict(_row_dict(row)) for row in rows]

    def wait_events(
        self, goal_id: str, *, after_seq: int = 0, timeout_sec: float = 0.0
    ) -> list[TaskflowEvent]:
        deadline = time.time() + max(0.0, float(timeout_sec or 0.0))
        while True:
            events = self.list_events(goal_id, after_seq=after_seq)
            if events or timeout_sec <= 0:
                return events
            remaining = deadline - time.time()
            if remaining <= 0:
                return []
            time.sleep(min(0.25, remaining))

    def _task_params(self, draft: TaskDraftRecord) -> dict[str, Any]:
        return {
            "id": draft.id,
            "goal_id": draft.goal_id,
            "issue_draft_id": draft.issue_draft_id,
            "title": draft.title,
            "prompt": draft.prompt,
            "status": draft.status.value,
            "required_capabilities": _json_array(draft.required_capabilities),
            "preferred_capabilities": _json_array(draft.preferred_capabilities),
            "task_type": draft.task_type,
            "workspace_root": draft.workspace_root,
            "repo_url": draft.repo_url,
            "execution_location": draft.execution_location,
            "manual_agent_id": draft.manual_agent_id,
            "runtime_task_id": draft.runtime_task_id,
            "metadata": _json(draft.metadata),
        }


__all__ = ["PostgresTaskflowStore"]

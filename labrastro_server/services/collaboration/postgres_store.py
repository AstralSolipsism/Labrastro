"""Postgres-backed Issue Assignment store."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from reuleauxcoder.domain.issue_assignment.models import (
    AssignmentRecord,
    IssueAssignmentEvent,
    IssueRecord,
    MentionRecord,
)

try:  # pragma: no cover - import availability is environment dependent.
    from sqlalchemy import text
except ImportError:  # pragma: no cover
    text = None


def _require_sqlalchemy() -> None:
    if text is None:
        raise RuntimeError(
            "Postgres issue assignment store requires sqlalchemy and psycopg."
        )


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_array(value: Any) -> str:
    return json.dumps(value if isinstance(value, list) else [], ensure_ascii=False)


def _row_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: _row_value(value) for key, value in dict(row).items()}


class PostgresIssueAssignmentStore:
    """Issue/Assignment/Mention store using the control-plane database."""

    def __init__(self, engine: Any) -> None:
        _require_sqlalchemy()
        self.engine = engine

    def create_issue(self, issue: IssueRecord) -> IssueRecord:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_issues (
                        id, title, description, status, peer_id, source,
                        taskflow_goal_id, taskflow_issue_draft_id, metadata
                    ) VALUES (
                        :id, :title, :description, :status, :peer_id, :source,
                        :taskflow_goal_id, :taskflow_issue_draft_id,
                        CAST(:metadata AS JSONB)
                    )
                    """
                ),
                {
                    "id": issue.id,
                    "title": issue.title,
                    "description": issue.description,
                    "status": issue.status.value,
                    "peer_id": issue.peer_id,
                    "source": issue.source,
                    "taskflow_goal_id": issue.taskflow_goal_id,
                    "taskflow_issue_draft_id": issue.taskflow_issue_draft_id,
                    "metadata": _json(issue.metadata),
                },
            )
        return self.get_issue(issue.id)

    def get_issue(self, issue_id: str) -> IssueRecord:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM labrastro_issues WHERE id=:issue_id"),
                {"issue_id": issue_id},
            ).mappings().first()
        if row is None:
            raise KeyError(f"issue not found: {issue_id}")
        return IssueRecord.from_dict(_row_dict(row))

    def update_issue(self, issue: IssueRecord) -> IssueRecord:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE labrastro_issues
                    SET title=:title, description=:description, status=:status,
                        peer_id=:peer_id, source=:source,
                        taskflow_goal_id=:taskflow_goal_id,
                        taskflow_issue_draft_id=:taskflow_issue_draft_id,
                        metadata=CAST(:metadata AS JSONB),
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                {
                    "id": issue.id,
                    "title": issue.title,
                    "description": issue.description,
                    "status": issue.status.value,
                    "peer_id": issue.peer_id,
                    "source": issue.source,
                    "taskflow_goal_id": issue.taskflow_goal_id,
                    "taskflow_issue_draft_id": issue.taskflow_issue_draft_id,
                    "metadata": _json(issue.metadata),
                },
            )
        if result.rowcount == 0:
            raise KeyError(f"issue not found: {issue.id}")
        return self.get_issue(issue.id)

    def list_issues(self, peer_id: str | None = None) -> list[IssueRecord]:
        sql = "SELECT * FROM labrastro_issues"
        params: dict[str, Any] = {}
        if peer_id is not None:
            sql += " WHERE peer_id=:peer_id"
            params["peer_id"] = peer_id
        sql += " ORDER BY created_at ASC"
        with self.engine.begin() as conn:
            rows = conn.execute(text(sql), params).mappings()
            return [IssueRecord.from_dict(_row_dict(row)) for row in rows]

    def create_assignment(self, assignment: AssignmentRecord) -> AssignmentRecord:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_assignments (
                        id, issue_id, status, target_agent_id, source, reason,
                        task_draft_id, dispatch_decision_id, runtime_task_id,
                        metadata
                    ) VALUES (
                        :id, :issue_id, :status, :target_agent_id, :source,
                        :reason, :task_draft_id, :dispatch_decision_id,
                        :runtime_task_id, CAST(:metadata AS JSONB)
                    )
                    """
                ),
                self._assignment_params(assignment),
            )
        return self.get_assignment(assignment.id)

    def get_assignment(self, assignment_id: str) -> AssignmentRecord:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM labrastro_assignments WHERE id=:assignment_id"),
                {"assignment_id": assignment_id},
            ).mappings().first()
        if row is None:
            raise KeyError(f"assignment not found: {assignment_id}")
        return AssignmentRecord.from_dict(_row_dict(row))

    def update_assignment(self, assignment: AssignmentRecord) -> AssignmentRecord:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE labrastro_assignments
                    SET status=:status, target_agent_id=:target_agent_id,
                        source=:source, reason=:reason,
                        task_draft_id=:task_draft_id,
                        dispatch_decision_id=:dispatch_decision_id,
                        runtime_task_id=:runtime_task_id,
                        metadata=CAST(:metadata AS JSONB),
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                self._assignment_params(assignment),
            )
        if result.rowcount == 0:
            raise KeyError(f"assignment not found: {assignment.id}")
        return self.get_assignment(assignment.id)

    def list_assignments(self, issue_id: str) -> list[AssignmentRecord]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_assignments
                    WHERE issue_id=:issue_id
                    ORDER BY created_at ASC
                    """
                ),
                {"issue_id": issue_id},
            ).mappings()
            return [AssignmentRecord.from_dict(_row_dict(row)) for row in rows]

    def create_mention(self, mention: MentionRecord) -> MentionRecord:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_mentions (
                        id, raw_text, status, peer_id, issue_id, assignment_id,
                        context_type, context_id, agent_ref, resolved_agent_id,
                        candidates, reason, source, metadata
                    ) VALUES (
                        :id, :raw_text, :status, :peer_id, :issue_id,
                        :assignment_id, :context_type, :context_id, :agent_ref,
                        :resolved_agent_id, CAST(:candidates AS JSONB),
                        :reason, :source, CAST(:metadata AS JSONB)
                    )
                    """
                ),
                self._mention_params(mention),
            )
        return self.get_mention(mention.id)

    def get_mention(self, mention_id: str) -> MentionRecord:
        with self.engine.begin() as conn:
            row = conn.execute(
                text("SELECT * FROM labrastro_mentions WHERE id=:mention_id"),
                {"mention_id": mention_id},
            ).mappings().first()
        if row is None:
            raise KeyError(f"mention not found: {mention_id}")
        return MentionRecord.from_dict(_row_dict(row))

    def update_mention(self, mention: MentionRecord) -> MentionRecord:
        with self.engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE labrastro_mentions
                    SET raw_text=:raw_text, status=:status, peer_id=:peer_id,
                        issue_id=:issue_id, assignment_id=:assignment_id,
                        context_type=:context_type, context_id=:context_id,
                        agent_ref=:agent_ref,
                        resolved_agent_id=:resolved_agent_id,
                        candidates=CAST(:candidates AS JSONB),
                        reason=:reason, source=:source,
                        metadata=CAST(:metadata AS JSONB),
                        updated_at=now()
                    WHERE id=:id
                    """
                ),
                self._mention_params(mention),
            )
        if result.rowcount == 0:
            raise KeyError(f"mention not found: {mention.id}")
        return self.get_mention(mention.id)

    def list_mentions(
        self, *, peer_id: str | None = None, issue_id: str | None = None
    ) -> list[MentionRecord]:
        filters: list[str] = []
        params: dict[str, Any] = {}
        if peer_id is not None:
            filters.append("peer_id=:peer_id")
            params["peer_id"] = peer_id
        if issue_id is not None:
            filters.append("issue_id=:issue_id")
            params["issue_id"] = issue_id
        sql = "SELECT * FROM labrastro_mentions"
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY created_at ASC"
        with self.engine.begin() as conn:
            rows = conn.execute(text(sql), params).mappings()
            return [MentionRecord.from_dict(_row_dict(row)) for row in rows]

    def append_event(
        self,
        scope: str,
        scope_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> IssueAssignmentEvent:
        with self.engine.begin() as conn:
            seq = conn.execute(
                text(
                    """
                    SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq
                    FROM labrastro_assignment_events
                    WHERE scope=:scope AND scope_id=:scope_id
                    """
                ),
                {"scope": scope, "scope_id": scope_id},
            ).scalar_one()
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_assignment_events (
                        scope, scope_id, seq, type, payload
                    ) VALUES (
                        :scope, :scope_id, :seq, :type,
                        CAST(:payload AS JSONB)
                    )
                    """
                ),
                {
                    "scope": scope,
                    "scope_id": scope_id,
                    "seq": int(seq),
                    "type": event_type,
                    "payload": _json(payload or {}),
                },
            )
        return self.list_events(scope, scope_id, after_seq=int(seq) - 1)[0]

    def list_events(
        self, scope: str, scope_id: str, *, after_seq: int = 0
    ) -> list[IssueAssignmentEvent]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT * FROM labrastro_assignment_events
                    WHERE scope=:scope AND scope_id=:scope_id AND seq>:after_seq
                    ORDER BY seq ASC
                    """
                ),
                {"scope": scope, "scope_id": scope_id, "after_seq": after_seq},
            ).mappings()
            return [IssueAssignmentEvent.from_dict(_row_dict(row)) for row in rows]

    def wait_events(
        self,
        scope: str,
        scope_id: str,
        *,
        after_seq: int = 0,
        timeout_sec: float = 0.0,
    ) -> list[IssueAssignmentEvent]:
        deadline = time.time() + max(0.0, float(timeout_sec or 0.0))
        while True:
            events = self.list_events(scope, scope_id, after_seq=after_seq)
            if events or timeout_sec <= 0:
                return events
            remaining = deadline - time.time()
            if remaining <= 0:
                return []
            time.sleep(min(0.25, remaining))

    def _assignment_params(self, assignment: AssignmentRecord) -> dict[str, Any]:
        return {
            "id": assignment.id,
            "issue_id": assignment.issue_id,
            "status": assignment.status.value,
            "target_agent_id": assignment.target_agent_id,
            "source": assignment.source,
            "reason": assignment.reason,
            "task_draft_id": assignment.task_draft_id,
            "dispatch_decision_id": assignment.dispatch_decision_id,
            "runtime_task_id": assignment.runtime_task_id,
            "metadata": _json(assignment.metadata),
        }

    def _mention_params(self, mention: MentionRecord) -> dict[str, Any]:
        return {
            "id": mention.id,
            "raw_text": mention.raw_text,
            "status": mention.status.value,
            "peer_id": mention.peer_id,
            "issue_id": mention.issue_id,
            "assignment_id": mention.assignment_id,
            "context_type": mention.context_type,
            "context_id": mention.context_id,
            "agent_ref": mention.agent_ref,
            "resolved_agent_id": mention.resolved_agent_id,
            "candidates": _json_array(mention.candidates),
            "reason": mention.reason,
            "source": mention.source,
            "metadata": _json(mention.metadata),
        }

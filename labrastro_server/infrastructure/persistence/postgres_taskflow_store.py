"""Postgres-backed Taskflow ProjectState and TaskflowState store."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from labrastro_server.taskflow.domain.project_state import ProjectState
from labrastro_server.taskflow.domain.taskflow_state import TaskflowState

try:  # pragma: no cover - dependency availability is environment dependent.
    from sqlalchemy import text
except ImportError:  # pragma: no cover
    text = None


def _require_sqlalchemy() -> None:
    if text is None:
        raise RuntimeError("Postgres Taskflow store requires sqlalchemy and psycopg.")


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _row_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: _row_value(value) for key, value in dict(row).items()}


class PostgresTaskflowStore:
    """Durable JSONB snapshot store for Taskflow control-plane state."""

    def __init__(self, engine: Any) -> None:
        _require_sqlalchemy()
        self.engine = engine

    def get_project_state(self, project_id: str) -> ProjectState | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT state FROM labrastro_taskflow_projects
                    WHERE project_id=:project_id
                    """
                ),
                {"project_id": project_id},
            ).mappings().first()
        if row is None:
            return None
        state = row["state"]
        if isinstance(state, str):
            state = json.loads(state)
        return ProjectState.from_dict(dict(state))

    def save_project_state(self, state: ProjectState) -> None:
        payload = state.to_dict()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_taskflow_projects (
                        project_id, state, schema_version, created_at, updated_at
                    ) VALUES (
                        :project_id, CAST(:state AS JSONB), :schema_version, now(), now()
                    )
                    ON CONFLICT (project_id) DO UPDATE
                    SET state=EXCLUDED.state,
                        schema_version=EXCLUDED.schema_version,
                        updated_at=now()
                    """
                ),
                {
                    "project_id": state.project_id,
                    "state": _json(payload),
                    "schema_version": state.schema_version,
                },
            )

    def get_taskflow_state(self, taskflow_id: str) -> TaskflowState:
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT state FROM labrastro_taskflow_states
                    WHERE taskflow_id=:taskflow_id
                    """
                ),
                {"taskflow_id": taskflow_id},
            ).mappings().first()
        if row is None:
            raise KeyError(f"taskflow state not found: {taskflow_id}")
        state = row["state"]
        if isinstance(state, str):
            state = json.loads(state)
        return TaskflowState.from_dict(dict(state))

    def save_taskflow_state(self, state: TaskflowState) -> None:
        payload = state.to_dict()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO labrastro_taskflow_states (
                        taskflow_id, project_id, goal_id, status,
                        state, schema_version, created_at, updated_at
                    ) VALUES (
                        :taskflow_id, :project_id, :goal_id, :status,
                        CAST(:state AS JSONB), :schema_version, now(), now()
                    )
                    ON CONFLICT (taskflow_id) DO UPDATE
                    SET project_id=EXCLUDED.project_id,
                        goal_id=EXCLUDED.goal_id,
                        status=EXCLUDED.status,
                        state=EXCLUDED.state,
                        schema_version=EXCLUDED.schema_version,
                        updated_at=now()
                    """
                ),
                {
                    "taskflow_id": state.meta.taskflow_id,
                    "project_id": state.meta.project_id,
                    "goal_id": state.meta.goal_id,
                    "status": state.status.value,
                    "state": _json(payload),
                    "schema_version": state.meta.schema_version,
                },
            )


__all__ = ["PostgresTaskflowStore"]

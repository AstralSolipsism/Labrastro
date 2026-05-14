from __future__ import annotations

import json
from typing import Any

from labrastro_server.infrastructure.persistence.postgres_taskflow_store import (
    PostgresTaskflowStore,
)
from labrastro_server.taskflow.domain.taskflow_state import (
    TaskflowEventType,
    TaskflowState,
    TaskflowStatus,
)
from labrastro_server.taskflow.domain.events import append_taskflow_event


class _FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def execute(self, statement: Any, params: dict[str, Any]) -> None:
        self.calls.append((statement, params))


class _FakeBegin:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _FakeConnection:
        return self.connection

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _FakeEngine:
    def __init__(self) -> None:
        self.connection = _FakeConnection()

    def begin(self) -> _FakeBegin:
        return _FakeBegin(self.connection)


def test_save_taskflow_state_uses_meta_status_and_serializes_snapshot() -> None:
    engine = _FakeEngine()
    store = PostgresTaskflowStore(engine)
    state = TaskflowState.new(
        taskflow_id="taskflow-unit",
        project_id="project-unit",
        goal_id="goal-unit",
        goal_statement="Stabilize Taskflow persistence.",
    )
    state.meta.status = TaskflowStatus.READY_FOR_DISPATCH
    state.intent.scope_in.append("Persist TaskflowState snapshots")

    store.save_taskflow_state(state)

    assert len(engine.connection.calls) == 1
    _statement, params = engine.connection.calls[0]
    assert params["taskflow_id"] == "taskflow-unit"
    assert params["project_id"] == "project-unit"
    assert params["goal_id"] == "goal-unit"
    assert params["schema_version"] == "taskflow.state.v1"
    assert params["status"] == "ready_for_dispatch"

    snapshot = json.loads(params["state"])
    assert snapshot["meta"]["status"] == "ready_for_dispatch"
    assert snapshot["intent"]["scope_in"] == ["Persist TaskflowState snapshots"]


def test_save_taskflow_state_syncs_append_only_event_ledger() -> None:
    engine = _FakeEngine()
    store = PostgresTaskflowStore(engine)
    state = TaskflowState.new(
        taskflow_id="taskflow-ledger",
        project_id="project-unit",
        goal_id="goal-unit",
        goal_statement="Persist event ledger.",
    )
    append_taskflow_event(
        state,
        TaskflowEventType.PROJECT_MEMORY_PATCH_APPLIED,
        actor="user",
        payload={"proposal_id": "proposal-1"},
    )

    store.save_taskflow_state(state)

    assert len(engine.connection.calls) == 2
    statement, params = engine.connection.calls[1]
    assert "ON CONFLICT (event_id) DO NOTHING" in str(statement)
    events = json.loads(params["events"])
    assert events[0]["type"] == "project_memory_patch_applied"
    assert events[0]["payload"]["proposal_id"] == "proposal-1"

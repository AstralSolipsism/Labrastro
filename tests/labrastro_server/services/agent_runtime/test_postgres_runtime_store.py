from __future__ import annotations

import json
import os

import pytest

from labrastro_server.infrastructure.persistence.db import create_postgres_engine
from labrastro_server.infrastructure.persistence.migration import run_migrations
from labrastro_server.services.agent_runtime.control_plane import (
    AgentRuntimeControlPlane,
    RuntimeTaskRequest,
)
from labrastro_server.services.agent_runtime.executor_backend import ExecutorRunResult
from labrastro_server.services.agent_runtime.postgres_store import PostgresRuntimeStore


pytestmark = pytest.mark.skipif(
    not os.environ.get("LABRASTRO_TEST_DATABASE_URL"),
    reason="LABRASTRO_TEST_DATABASE_URL is not configured",
)


def _control() -> AgentRuntimeControlPlane:
    database_url = os.environ["LABRASTRO_TEST_DATABASE_URL"]
    run_migrations(database_url)
    engine = create_postgres_engine(database_url)
    store = PostgresRuntimeStore(
        engine,
        runtime_snapshot={
            "runtime_profiles": {
                "fake-profile": {
                    "executor": "fake",
                    "execution_location": "daemon_worktree",
                }
            },
            "agents": {
                "pg-agent": {
                    "runtime_profile": "fake-profile",
                    "max_concurrent_tasks": 1,
                }
            },
        },
    )
    return AgentRuntimeControlPlane(store=store)


def test_postgres_runtime_store_claim_complete_and_reload() -> None:
    control = _control()
    task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="pg-issue",
            agent_id="pg-agent",
            prompt="postgres runtime smoke",
        )
    )

    claim = control.claim_task(
        worker_id="pg-worker",
        executors=["fake"],
        peer_features=["agent_runtime.daemon_worktree"],
    )
    assert claim is not None
    assert claim.task.id == task.id
    assert claim.executor_request.executor.value == "fake"

    ok, reason = control.pin_claimed_session(
        request_id=claim.request_id,
        task_id=task.id,
        worker_id="pg-worker",
        workdir="/tmp/pg-worktree",
        branch="agent/pg",
    )
    assert (ok, reason) == (True, "")
    ok, reason, completed = control.complete_claimed_task(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        request_id=claim.request_id,
        worker_id="pg-worker",
    )
    assert ok is True
    assert completed is not None
    assert completed.status.value == "completed"

    reloaded = _control()
    events = reloaded.list_events(task.id, after_seq=0)
    assert [event.type for event in events][0] == "queued"
    assert len(reloaded.list_events(task.id, after_seq=0, limit=1)) == 1
    assert reloaded.task_to_dict(task.id)["status"] == "completed"
    detail = reloaded.load_task_detail(task.id, event_limit=1)
    json.dumps(detail)
    assert detail["session"]["workdir"] == "/tmp/pg-worktree"
    assert detail["claim"]["status"] == "completed"
    assert len(detail["events"]) == 1


def test_postgres_runtime_store_host_restart_fails_running_task() -> None:
    control = _control()
    task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="pg-restart",
            agent_id="pg-agent",
            prompt="restart smoke",
        )
    )
    claim = control.claim_task(
        worker_id="pg-worker",
        executors=["fake"],
        peer_features=["agent_runtime.daemon_worktree"],
    )
    assert claim is not None
    assert control.heartbeat_task(
        request_id=claim.request_id,
        task_id=task.id,
        worker_id="pg-worker",
    )["ok"]

    reloaded = _control()
    assert reloaded.task_to_dict(task.id)["status"] == "failed"
    assert any(
        event.type == "host_recovered_task_failed"
        for event in reloaded.list_events(task.id, after_seq=0)
    )

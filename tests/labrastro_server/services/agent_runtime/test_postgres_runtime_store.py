from __future__ import annotations

import json
import os

import pytest

from labrastro_server.infrastructure.persistence.db import create_postgres_engine
from labrastro_server.infrastructure.persistence.migration import run_migrations
from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunControlPlane,
    AgentRunRequest,
)
from labrastro_server.services.agent_runtime.executor_backend import ExecutorRunResult
from labrastro_server.services.agent_runtime.postgres_store import PostgresAgentRunStore


pytestmark = pytest.mark.skipif(
    not os.environ.get("LABRASTRO_TEST_DATABASE_URL"),
    reason="LABRASTRO_TEST_DATABASE_URL is not configured",
)


def _control() -> AgentRunControlPlane:
    database_url = os.environ["LABRASTRO_TEST_DATABASE_URL"]
    run_migrations(database_url)
    engine = create_postgres_engine(database_url)
    store = PostgresAgentRunStore(
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
    return AgentRunControlPlane(store=store)


def test_postgres_runtime_store_claim_complete_and_reload() -> None:
    control = _control()
    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-issue",
            agent_id="pg-agent",
            prompt="postgres runtime smoke",
        )
    )

    claim = control.claim_agent_run(
        worker_id="pg-worker",
        executors=["fake"],
        peer_features=["agent_runs.daemon_worktree"],
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
    ok, reason, completed = control.complete_claimed_agent_run(
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
    assert reloaded.agent_run_to_dict(task.id)["status"] == "completed"
    detail = reloaded.load_agent_run_detail(task.id, event_limit=1)
    json.dumps(detail)
    assert detail["session"]["workdir"] == "/tmp/pg-worktree"
    assert detail["claim"]["status"] == "completed"
    assert len(detail["events"]) == 1


def test_postgres_runtime_store_host_restart_fails_running_task() -> None:
    control = _control()
    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-restart",
            agent_id="pg-agent",
            prompt="restart smoke",
        )
    )
    claim = control.claim_agent_run(
        worker_id="pg-worker",
        executors=["fake"],
        peer_features=["agent_runs.daemon_worktree"],
    )
    assert claim is not None
    assert control.heartbeat_agent_run(
        request_id=claim.request_id,
        task_id=task.id,
        worker_id="pg-worker",
    )["ok"]

    reloaded = _control()
    task_detail = reloaded.agent_run_to_dict(task.id)
    assert task_detail["status"] == "failed"
    assert task_detail["failure_reason"] == "host_restarted"
    assert any(
        event.type == "host_recovered_task_failed"
        for event in reloaded.list_events(task.id, after_seq=0)
    )


def test_postgres_runtime_store_terminal_reasons_round_trip() -> None:
    control = _control()
    failed = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-failed",
            agent_id="pg-agent",
            prompt="fail smoke",
        )
    )
    control.complete_agent_run(
        failed.id,
        ExecutorRunResult(
            task_id=failed.id,
            status="failed",
            output="",
            error="real postgres failure",
        ),
    )

    cancelled = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-cancelled",
            agent_id="pg-agent",
            prompt="cancel smoke",
        )
    )
    assert control.cancel_agent_run(cancelled.id, reason="user stopped") is True

    reloaded = _control()
    failed_detail = reloaded.agent_run_to_dict(failed.id)
    cancelled_detail = reloaded.agent_run_to_dict(cancelled.id)
    assert failed_detail["status"] == "failed"
    assert failed_detail["output"] == ""
    assert failed_detail["failure_reason"] == "real postgres failure"
    assert failed_detail["cancel_reason"] is None
    assert cancelled_detail["status"] == "cancelled"
    assert cancelled_detail["failure_reason"] == "cancelled"
    assert cancelled_detail["cancel_reason"] == "user stopped"


def test_postgres_runtime_store_assigns_reuleauxcoder_executor_session() -> None:
    database_url = os.environ["LABRASTRO_TEST_DATABASE_URL"]
    run_migrations(database_url)
    engine = create_postgres_engine(database_url)
    store = PostgresAgentRunStore(
        engine,
        runtime_snapshot={
            "runtime_profiles": {
                "packager": {
                    "executor": "reuleauxcoder",
                    "execution_location": "remote_server",
                    "worker_kind": "sandbox_worker",
                }
            },
            "agents": {
                "capability_packager": {
                    "runtime_profile": "packager",
                }
            },
        },
    )
    control = AgentRunControlPlane(store=store)

    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-reuleaux",
            agent_id="capability_packager",
            prompt="draft",
            source="capability_ingest",
        )
    )

    assert task.executor_session_id
    assert task.executor_session_id == f"labrastro-agent-run-{task.id}"
    assert control.agent_run_to_dict(task.id)["executor_session_id"] == task.executor_session_id

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
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.postgres_store import PostgresAgentRunStore
from labrastro_server.services.agent_runtime.session_projection import (
    agent_run_event_to_session_events,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("LABRASTRO_TEST_DATABASE_URL"),
    reason="LABRASTRO_TEST_DATABASE_URL is not configured",
)


@pytest.fixture(autouse=True)
def _reset_agent_run_tables() -> None:
    database_url = os.environ["LABRASTRO_TEST_DATABASE_URL"]
    run_migrations(database_url)
    engine = create_postgres_engine(database_url)
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                TRUNCATE
                    labrastro_agent_run_events,
                    labrastro_agent_run_claims,
                    labrastro_agent_run_sessions,
                    labrastro_agent_run_artifacts,
                    labrastro_agent_run_cancel_requests,
                    labrastro_agent_runs
                RESTART IDENTITY CASCADE
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO labrastro_agent_run_locks(name)
                VALUES ('global_claim')
                ON CONFLICT (name) DO NOTHING
                """
            )
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


def test_control_plane_initializes_postgres_store_runtime_snapshot_from_control_config() -> None:
    database_url = os.environ["LABRASTRO_TEST_DATABASE_URL"]
    run_migrations(database_url)
    engine = create_postgres_engine(database_url)
    store = PostgresAgentRunStore(engine)
    snapshot = {
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
    }
    control = AgentRunControlPlane(store=store, runtime_snapshot=snapshot)

    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-control-store-snapshot",
            agent_id="pg-agent",
            prompt="snapshot smoke",
        )
    )
    claim = control.claim_agent_run(worker_id="pg-worker", executors=["fake"])

    assert store.runtime_snapshot == snapshot
    assert claim is not None
    assert claim.task.id == task.id
    assert claim.executor_request.runtime_profile_id == "fake-profile"


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
    assert claim.executor_request.worktree_role.value == "target"
    assert claim.executor_request.publish_policy.value == "never"
    assert claim.executor_request.metadata["worktree_role"] == "target"
    assert claim.executor_request.metadata["publish_policy"] == "never"

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
    assert reloaded.agent_run_to_dict(task.id)["worktree_role"] == "target"
    assert reloaded.agent_run_to_dict(task.id)["publish_policy"] == "never"
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


def test_postgres_runtime_store_preserves_agent_run_budget() -> None:
    control = _control()
    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-budget",
            agent_id="pg-agent",
            prompt="budget smoke",
            budget={"token_budget": "1200", "max_turns": 2},
        )
    )

    detail = control.agent_run_to_dict(task.id)
    assert detail["budget"] == {"token_budget": 1200, "max_turns": 2}
    assert detail["metadata"]["budget"] == {"token_budget": 1200, "max_turns": 2}


def test_postgres_runtime_store_claim_includes_budget_in_executor_request() -> None:
    control = _control()
    control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-budget-claim",
            agent_id="pg-agent",
            prompt="budget claim smoke",
            executor="reuleauxcoder",
            budget={"max_tool_calls": "2", "timeout_sec": 30},
        )
    )

    claim = control.claim_agent_run(worker_id="pg-worker", executors=["reuleauxcoder"])

    assert claim is not None
    assert claim.executor_request.budget == {"max_tool_calls": 2, "timeout_sec": 30}


def test_postgres_runtime_store_projects_child_terminal_event_to_parent() -> None:
    control = _control()
    parent = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-parent-terminal",
            agent_id="pg-agent",
            prompt="parent",
        )
    )
    child = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-child-terminal",
            agent_id="pg-agent",
            prompt="child",
            parent_run_id=parent.id,
            delegated_by_run_id=parent.id,
            metadata={
                "lifecycle_hook_id": "hook:postgres-lifecycle-agent",
                "lifecycle_hook_source": "admin_managed",
                "parent_session_id": "session-pg",
                "parent_turn_id": "turn-pg",
            },
        )
    )

    control.complete_agent_run(
        child.id,
        ExecutorRunResult(task_id=child.id, status="completed", output="child done"),
    )

    parent_events = control.list_events(parent.id, after_seq=0)
    delegated = [
        event for event in parent_events if event.type == "delegated_run_completed"
    ][0]
    lifecycle_events = [
        event for event in parent_events if event.type == "lifecycle_hook"
    ]
    assert delegated.payload["agent_run_id"] == child.id
    assert delegated.payload["parent_run_id"] == parent.id
    assert delegated.payload["delegated_by_run_id"] == parent.id
    assert delegated.payload["status"] == "completed"
    assert delegated.payload["result"] == "child done"
    assert [event.payload["event_name"] for event in lifecycle_events] == [
        "TaskCompleted",
        "SubagentStop",
    ]
    assert lifecycle_events[0].payload["agent_run_id"] == parent.id
    assert lifecycle_events[0].payload["payload"]["child_agent_run_id"] == child.id
    assert lifecycle_events[0].payload["payload"]["status"] == "completed"
    assert lifecycle_events[0].payload["payload"]["lifecycle_hook_id"] == (
        "hook:postgres-lifecycle-agent"
    )
    assert lifecycle_events[0].payload["payload"]["lifecycle_hook_source"] == (
        "admin_managed"
    )
    assert lifecycle_events[0].payload["payload"]["parent_session_id"] == "session-pg"
    assert lifecycle_events[0].payload["payload"]["parent_turn_id"] == "turn-pg"
    assert lifecycle_events[1].payload["agent_run_id"] == parent.id
    assert lifecycle_events[1].payload["payload"]["child_agent_run_id"] == child.id
    assert lifecycle_events[1].payload["payload"]["status"] == "completed"
    session_lifecycle_events = [
        agent_run_event_to_session_events(event.to_dict())[0]
        for event in lifecycle_events
    ]
    assert [payload["event_name"] for _, payload in session_lifecycle_events] == [
        "TaskCompleted",
        "SubagentStop",
    ]
    assert session_lifecycle_events[0][1]["lifecycle_hook_id"] == (
        "hook:postgres-lifecycle-agent"
    )
    assert session_lifecycle_events[0][1]["lifecycle_hook_source"] == "admin_managed"
    assert session_lifecycle_events[0][1]["parent_session_id"] == "session-pg"
    assert session_lifecycle_events[0][1]["parent_turn_id"] == "turn-pg"


def test_postgres_runtime_store_emits_worktree_create_lifecycle_audit() -> None:
    control = _control()
    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-worktree",
            agent_id="pg-agent",
            prompt="worktree",
            execution_location="daemon_worktree",
            metadata={"worker_kind": "sandbox_worker"},
        )
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent.status(
            "worktree_ready",
            workdir="/runtime/worktrees/ws/pg-agent-task",
            runtime_root="/runtime",
        ),
    )

    events = control.list_events(task.id, after_seq=0)
    lifecycle_events = [
        event for event in events if event.type == "lifecycle_hook"
    ]
    assert [event.payload["event_name"] for event in lifecycle_events] == [
        "WorktreeCreate"
    ]
    payload = lifecycle_events[0].payload["payload"]
    assert payload["workdir"] == "/runtime/worktrees/ws/pg-agent-task"
    assert payload["runtime_root"] == "/runtime"
    assert payload["execution_location"] == "daemon_worktree"
    assert payload["worker_kind"] == "sandbox_worker"
    assert payload["path_space"] == "agent_run_worktree"


def test_postgres_runtime_store_emits_worktree_remove_lifecycle_audit() -> None:
    control = _control()
    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-worktree-remove",
            agent_id="pg-agent",
            prompt="worktree remove",
            execution_location="daemon_worktree",
            metadata={"worker_kind": "sandbox_worker"},
        )
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent.status(
            "worktree_removed",
            workdir="/runtime/worktrees/ws/pg-agent-task",
            runtime_root="/runtime",
        ),
    )

    events = control.list_events(task.id, after_seq=0)
    lifecycle_events = [
        event for event in events if event.type == "lifecycle_hook"
    ]
    assert [event.payload["event_name"] for event in lifecycle_events] == [
        "WorktreeRemove"
    ]
    payload = lifecycle_events[0].payload["payload"]
    assert payload["workdir"] == "/runtime/worktrees/ws/pg-agent-task"
    assert payload["runtime_root"] == "/runtime"
    assert payload["execution_location"] == "daemon_worktree"
    assert payload["worker_kind"] == "sandbox_worker"
    assert payload["path_space"] == "agent_run_worktree"


def test_postgres_runtime_store_cascades_cancel_to_child_sandbox_runs() -> None:
    class FakeSandboxProvider:
        def __init__(self) -> None:
            self.cancelled_sessions: list[str] = []

        def cancel(self, session_id: str) -> bool:
            self.cancelled_sessions.append(session_id)
            return True

        def stop_session(self, session_id: str) -> bool:  # noqa: ARG002
            return True

    control = _control()
    provider = FakeSandboxProvider()
    control.configure_sandbox_provider(provider)
    parent = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-parent-cancel",
            agent_id="pg-agent",
            prompt="parent",
        )
    )
    child = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-child-cancel",
            agent_id="pg-agent",
            prompt="child",
            parent_run_id=parent.id,
            delegated_by_run_id=parent.id,
            sandbox_session_id="ssn-child",
            metadata={
                "lifecycle_hook_id": "hook:postgres-lifecycle-agent",
                "lifecycle_hook_source": "admin_managed",
                "parent_session_id": "session-pg",
                "parent_turn_id": "turn-pg",
            },
        )
    )
    grandchild = control.submit_agent_run(
        AgentRunRequest(
            issue_id="pg-grandchild-cancel",
            agent_id="pg-agent",
            prompt="grandchild",
            parent_task_id=child.id,
            sandbox_session_id="ssn-grandchild",
        )
    )

    assert control.cancel_agent_run(parent.id, reason="user stopped") is True

    child_detail = control.agent_run_to_dict(child.id)
    grandchild_detail = control.agent_run_to_dict(grandchild.id)
    assert child_detail["status"] == "cancelled"
    assert child_detail["cancel_reason"] == "parent_cancelled:user stopped"
    assert grandchild_detail["status"] == "cancelled"
    assert grandchild_detail["cancel_reason"] == "parent_cancelled:user stopped"
    assert provider.cancelled_sessions == ["ssn-child", "ssn-grandchild"]

    child_events = control.list_events(child.id, after_seq=0)
    assert "cancelled" in [event.type for event in child_events]
    assert "parent_cancelled" in [event.type for event in child_events]
    parent_events = control.list_events(parent.id, after_seq=0)
    delegated = [
        event for event in parent_events if event.type == "delegated_run_completed"
    ][0]
    lifecycle_events = [
        event for event in parent_events if event.type == "lifecycle_hook"
    ]
    assert delegated.payload["agent_run_id"] == child.id
    assert delegated.payload["parent_run_id"] == parent.id
    assert delegated.payload["delegated_by_run_id"] == parent.id
    assert delegated.payload["status"] == "cancelled"
    assert [event.payload["event_name"] for event in lifecycle_events] == [
        "TaskCompleted",
        "SubagentStop",
    ]
    assert lifecycle_events[0].payload["payload"]["child_agent_run_id"] == child.id
    assert lifecycle_events[0].payload["payload"]["status"] == "cancelled"
    assert lifecycle_events[0].payload["payload"]["lifecycle_hook_id"] == (
        "hook:postgres-lifecycle-agent"
    )
    assert lifecycle_events[0].payload["payload"]["lifecycle_hook_source"] == (
        "admin_managed"
    )
    assert lifecycle_events[0].payload["payload"]["parent_session_id"] == "session-pg"
    assert lifecycle_events[0].payload["payload"]["parent_turn_id"] == "turn-pg"
    session_lifecycle_events = [
        agent_run_event_to_session_events(event.to_dict())[0]
        for event in lifecycle_events
    ]
    assert session_lifecycle_events[0][1]["lifecycle_hook_id"] == (
        "hook:postgres-lifecycle-agent"
    )
    assert session_lifecycle_events[0][1]["lifecycle_hook_source"] == "admin_managed"
    assert session_lifecycle_events[0][1]["parent_session_id"] == "session-pg"
    assert session_lifecycle_events[0][1]["parent_turn_id"] == "turn-pg"


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

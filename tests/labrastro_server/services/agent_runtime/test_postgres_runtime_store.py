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
from reuleauxcoder.domain.agent_runtime.models import (
    AgentCallGrant,
    AgentRunRelation,
    AgentRunRelationType,
    AgentRunStatus,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("LABRASTRO_TEST_DATABASE_URL"),
    reason="LABRASTRO_TEST_DATABASE_URL is not configured",
)


def _relation(
    owner_agent_run_id: str,
    *,
    relation_type: AgentRunRelationType | str = AgentRunRelationType.AGENT_CALL_EPHEMERAL,
    metadata: dict | None = None,
) -> AgentRunRelation:
    relation_type_value = (
        relation_type.value
        if isinstance(relation_type, AgentRunRelationType)
        else str(relation_type)
    )
    return AgentRunRelation(
        id="",
        owner_agent_run_id=owner_agent_run_id,
        related_agent_run_id="",
        relation_type=AgentRunRelationType(relation_type_value),
        metadata=dict(metadata or {}),
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
                    labrastro_agent_run_activation_claims,
                    labrastro_agent_run_activation_steers,
                    labrastro_agent_run_feedback,
                    labrastro_agent_run_activations,
                    labrastro_agent_thread_bindings,
                    labrastro_agent_call_grants,
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


def _current_activation_id(control: AgentRunControlPlane, task_id: str) -> str:
    return str(control.get_agent_run(task_id).current_activation_id or "")


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
            agent_id="pg-agent",
            prompt="snapshot smoke",
        )
    )
    claim = control.claim_agent_run_activation(worker_id="pg-worker", executors=["fake"])

    assert store.runtime_snapshot == snapshot
    assert claim is not None
    assert claim.task.id == task.id
    assert claim.executor_request.runtime_profile_id == "fake-profile"


def test_postgres_runtime_store_claim_complete_and_reload() -> None:
    control = _control()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="pg-agent",
            prompt="postgres runtime smoke",
        )
    )

    claim = control.claim_agent_run_activation(
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

    ok, reason = control.pin_claimed_activation_session(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="pg-worker",
        workdir="/tmp/pg-worktree",
        branch="agent/pg",
    )
    assert (ok, reason) == (True, "")
    ok, reason, completed = control.complete_claimed_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        request_id=claim.request_id,
        activation_id=claim.activation_id,
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


def test_postgres_agent_thread_binding_reuses_activation() -> None:
    control = _control()
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="pg-agent", prompt="plan"),
        task_id="parent-run",
    )

    first = control.call_persistent_agent(
        owner_agent_run_id=parent.id,
        owner_session_run_id="session-1",
        agent_id="pg-agent",
        prompt="collect project context",
        thread_key="project-context",
        thread_summary="Project context research",
    )
    control.complete_agent_run_activation(
        first.id,
        ExecutorRunResult(task_id=first.id, status="completed", output="context ready"),
        activation_id=_current_activation_id(control, first.id),
    )
    second = control.call_persistent_agent(
        owner_agent_run_id=parent.id,
        owner_session_run_id="session-1",
        agent_id="pg-agent",
        prompt="refresh project context",
        thread_key="project-context",
        thread_summary="Project context research",
    )

    assert second.id == first.id
    detail = control.load_agent_run_detail(first.id)
    assert len(detail["agent_thread_bindings"]) == 1
    assert detail["agent_thread_bindings"][0]["target_agent_run_id"] == first.id
    assert [activation["prompt"] for activation in detail["activations"]] == [
        "collect project context",
        "refresh project context",
    ]
    assert len(detail["events"]) == 1
    parent_detail = control.load_agent_run_detail(parent.id)
    agent_call_events = [
        event for event in parent_detail["events"] if event["type"] == "agent_call_result"
    ]
    assert agent_call_events[0]["payload"]["target_agent_run_id"] == first.id
    assert agent_call_events[0]["payload"]["summary"] == "context ready"
    assert not [
        event
        for event in parent_detail["events"]
        if event["type"] == "agent_relation_completed"
    ]


def test_postgres_complete_agent_run_activation_requires_current_activation_id() -> None:
    control = _control()
    task = control.submit_agent_run(
        AgentRunRequest(agent_id="pg-agent", prompt="run"),
        task_id="pg-activation-lock",
    )
    result = ExecutorRunResult(task_id=task.id, status="completed", output="done")

    with pytest.raises(TypeError):
        control.complete_agent_run_activation(task.id, result)
    with pytest.raises(ValueError, match="activation_id_required"):
        control.complete_agent_run_activation(task.id, result, activation_id="")
    with pytest.raises(ValueError, match="activation_not_found"):
        control.complete_agent_run_activation(
            task.id,
            result,
            activation_id="other-run:activation:1",
        )

    stale_activation_id = _current_activation_id(control, task.id)
    control.complete_agent_run_activation(
        task.id,
        result,
        activation_id=stale_activation_id,
    )
    control.continue_agent_run(
        task.id,
        input_kind="user_feedback",
        input_payload={"feedback_id": "feedback-1"},
        prompt="continue",
    )

    with pytest.raises(ValueError, match="activation_mismatch"):
        control.complete_agent_run_activation(
            task.id,
            ExecutorRunResult(task_id=task.id, status="completed", output="again"),
            activation_id=stale_activation_id,
        )


def test_postgres_waiting_agent_call_target_first_resumes_after_owner_completion() -> None:
    control = _control()
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="pg-agent", prompt="parent"),
        task_id="pg-parent-run",
    )
    child = control.call_persistent_agent(
        owner_agent_run_id=parent.id,
        owner_session_run_id="session-1",
        agent_id="pg-agent",
        prompt="collect context",
        thread_key="project-context",
        thread_summary="Project context research",
        wait=True,
    )
    control.mark_agent_call_waiting(
        parent.id,
        target_agent_run_id=child.id,
        conversation_scope="persistent",
        thread_key="project-context",
        wait=True,
    )

    control.complete_agent_run_activation(
        child.id,
        ExecutorRunResult(task_id=child.id, status="completed", output="context ready"),
        activation_id=_current_activation_id(control, child.id),
    )
    pending_detail = control.load_agent_run_detail(parent.id)
    assert pending_detail["agent_run"]["status"] == AgentRunStatus.WAITING.value
    assert pending_detail["feedback"][0]["requires_activation"] is True
    assert pending_detail["feedback"][0]["consumed_by_activation_id"] is None

    resumed = control.complete_agent_run_activation(
        parent.id,
        ExecutorRunResult(task_id=parent.id, status="completed", output="waiting"),
        activation_id=_current_activation_id(control, parent.id),
    )

    detail = control.load_agent_run_detail(parent.id)
    assert resumed.status == AgentRunStatus.QUEUED
    assert detail["feedback"][0]["consumed_by_activation_id"] == (
        "pg-parent-run:activation:2"
    )
    assert detail["activations"][-1]["input_kind"] == "agent_feedback"
    assert detail["activations"][-1]["input_payload"]["target_agent_run_id"] == child.id


def test_postgres_agent_call_grant_is_bound_to_capability_scope() -> None:
    control = _control()
    grant = AgentCallGrant(
        user_id="user-1",
        grant_scope="workspace:/repo",
        main_agent_id="planner",
        target_agent_id="researcher",
        conversation_scope="persistent",
        capability_scope={"capability_refs": ["research"]},
        target_config_version="version-a",
        granted_at="2026-06-15T00:00:00+00:00",
    )

    control.upsert_agent_call_grant(grant)

    found = control.find_agent_call_grant(
        user_id="user-1",
        grant_scope="workspace:/repo",
        main_agent_id="planner",
        target_agent_id="researcher",
        conversation_scope="persistent",
        capability_scope={"capability_refs": ["research"]},
        target_config_version="version-a",
    )
    mismatched_scope = control.find_agent_call_grant(
        user_id="user-1",
        grant_scope="workspace:/repo",
        main_agent_id="planner",
        target_agent_id="researcher",
        conversation_scope="persistent",
        capability_scope={"capability_refs": ["write"]},
        target_config_version="version-a",
    )

    assert found is not None
    assert found.target_agent_id == "researcher"
    assert found.capability_scope == {"capability_refs": ["research"]}
    assert mismatched_scope is None


def test_postgres_activation_steer_persists_and_queues_feedback() -> None:
    control = _control()
    task = control.submit_agent_run(
        AgentRunRequest(agent_id="pg-agent", prompt="postgres steer"),
        task_id="pg-steer-run",
    )

    steer, feedback = control.append_activation_steer(
        task.id,
        source="user",
        payload={"message": "extra context"},
        metadata={"turn_id": "turn-pg"},
        steer_id="pg-steer-1",
    )

    detail = control.load_agent_run_detail(task.id)
    json.dumps(detail)
    assert steer.activation_id == "pg-steer-run:activation:1"
    assert feedback.metadata["steer_id"] == "pg-steer-1"
    assert [activation["id"] for activation in detail["activations"]] == [
        "pg-steer-run:activation:1"
    ]
    assert detail["activation_steers"][0]["id"] == "pg-steer-1"
    assert detail["activation_steers"][0]["activation_id"] == (
        "pg-steer-run:activation:1"
    )
    assert detail["activation_steers"][0]["payload"] == {"message": "extra context"}
    assert detail["feedback"][0]["metadata"]["fallback_reason"] == (
        "same_activation_steer_delivery_unavailable"
    )
    assert [event["type"] for event in detail["events"]][-2:] == [
        "activation_steer_queued",
        "agent_run_feedback_added",
    ]


def test_postgres_runtime_store_preserves_artifact_append_order_from_completion_transaction() -> None:
    control = _control()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="pg-agent",
            prompt="postgres artifact order",
        )
    )

    control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        artifacts=[
            {
                "artifact_id": "artifact-z",
                "type": "log",
                "status": "generated",
                "content": "first",
            },
            {
                "artifact_id": "artifact-a",
                "type": "log",
                "status": "generated",
                "content": "second",
            },
        ],
        activation_id=_current_activation_id(control, task.id),
    )

    assert [item["id"] for item in control.artifacts_to_dict(task.id)] == [
        "artifact-z",
        "artifact-a",
    ]


def test_postgres_runtime_store_host_restart_fails_running_task() -> None:
    control = _control()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="pg-agent",
            prompt="restart smoke",
        )
    )
    claim = control.claim_agent_run_activation(
        worker_id="pg-worker",
        executors=["fake"],
        peer_features=["agent_runs.daemon_worktree"],
    )
    assert claim is not None
    assert control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
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
            agent_id="pg-agent",
            prompt="fail smoke",
        )
    )
    control.complete_agent_run_activation(
        failed.id,
        ExecutorRunResult(
            task_id=failed.id,
            status="failed",
            output="",
            error="real postgres failure",
        ),
        activation_id=_current_activation_id(control, failed.id),
    )

    cancelled = control.submit_agent_run(
        AgentRunRequest(
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
            agent_id="pg-agent",
            prompt="budget claim smoke",
            executor="reuleauxcoder",
            budget={"max_tool_calls": "2", "timeout_sec": 30},
        )
    )

    claim = control.claim_agent_run_activation(worker_id="pg-worker", executors=["reuleauxcoder"])

    assert claim is not None
    assert claim.executor_request.budget == {"max_tool_calls": 2, "timeout_sec": 30}


def test_postgres_runtime_store_projects_child_terminal_event_to_parent() -> None:
    control = _control()
    parent = control.submit_agent_run(
        AgentRunRequest(
            agent_id="pg-agent",
            prompt="parent",
        )
    )
    child = control.submit_agent_run(
        AgentRunRequest(
            agent_id="pg-agent",
            prompt="child",
            relation=_relation(
                parent.id,
                metadata={
                    "lifecycle_hook_id": "hook:postgres-lifecycle-agent",
                    "lifecycle_hook_source": "admin_managed",
                    "parent_session_id": "session-pg",
                    "parent_turn_id": "turn-pg",
                },
            ),
        )
    )

    control.complete_agent_run_activation(
        child.id,
        ExecutorRunResult(task_id=child.id, status="completed", output="child done"),
        activation_id=_current_activation_id(control, child.id),
    )

    parent_events = control.list_events(parent.id, after_seq=0)
    delegated = [
        event for event in parent_events if event.type == "agent_relation_completed"
    ][0]
    lifecycle_events = [
        event for event in parent_events if event.type == "lifecycle_hook"
    ]
    assert delegated.payload["agent_run_id"] == child.id
    assert delegated.payload["owner_agent_run_id"] == parent.id
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
            agent_id="pg-agent",
            prompt="parent",
        )
    )
    child = control.submit_agent_run(
        AgentRunRequest(
            agent_id="pg-agent",
            prompt="child",
            sandbox_session_id="ssn-child",
            relation=_relation(
                parent.id,
                metadata={
                    "lifecycle_hook_id": "hook:postgres-lifecycle-agent",
                    "lifecycle_hook_source": "admin_managed",
                    "parent_session_id": "session-pg",
                    "parent_turn_id": "turn-pg",
                },
            ),
        )
    )
    grandchild = control.submit_agent_run(
        AgentRunRequest(
            agent_id="pg-agent",
            prompt="grandchild",
            sandbox_session_id="ssn-grandchild",
            relation=_relation(child.id),
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
        event for event in parent_events if event.type == "agent_relation_completed"
    ][0]
    lifecycle_events = [
        event for event in parent_events if event.type == "lifecycle_hook"
    ]
    assert delegated.payload["agent_run_id"] == child.id
    assert delegated.payload["owner_agent_run_id"] == parent.id
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
            agent_id="capability_packager",
            prompt="draft",
            source="capability_ingest",
        )
    )

    assert task.executor_session_id
    assert task.executor_session_id == f"labrastro-agent-run-{task.id}"
    assert control.agent_run_to_dict(task.id)["executor_session_id"] == task.executor_session_id

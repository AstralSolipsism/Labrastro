from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import threading
import time

import pytest

from reuleauxcoder.domain.agent_runtime.models import (
    AgentRunActivationState,
    AgentRunActivationInputKind,
    AgentRun,
    AgentCallGrant,
    AgentConfig,
    AgentRunFeedbackKind,
    AgentRunFeedbackSource,
    AgentRunResumePolicy,
    AgentRunRelation,
    AgentRunRelationType,
    AgentRunSource,
    AgentRunWaitingReason,
    AgentThreadBinding,
    AgentThreadBindingLifetime,
    AgentThreadBindingStatus,
    AgentRunMainlineState,
    SessionRunBindingStatus,
    ExecutionLocation,
    ExecutorType,
    ModelRequestOrigin,
    PublishPolicy,
    TaskSessionRef,
    AgentRunStatus,
    TriggerMode,
    WorkerKind,
    WorktreeRole,
)
from reuleauxcoder.domain.config.models import build_agent_run_snapshot
from reuleauxcoder.domain.hooks.lifecycle import (
    LifecycleHookDeclaration,
    LifecycleHookDispatcher,
    LifecycleHookRegistry,
    bind_lifecycle_runtime_adapters_to_agent,
    build_lifecycle_event_context,
    default_lifecycle_hook_runtime_adapters,
)
from reuleauxcoder.domain.permission_gateway import PermissionAction, PermissionDecision
from reuleauxcoder.services.config.loader import ConfigLoader
from labrastro_server.services.agent_runtime.control_plane import (
    AgentCallDispatchError,
    AgentRunRequest,
    AgentRunControlPlane,
)
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunRequest,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.postgres_store import PostgresAgentRunStore
from labrastro_server.services.agent_runtime.runtime_store import (
    runtime_slot_key_for_agent_run,
)
from labrastro_server.services.agent_runtime.worktree import (
    WorktreeManager,
    WorktreeOwnershipError,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "agent@example.invalid")
    _git(path, "config", "user.name", "Agent Test")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "base")
    return path


class _FakeSandboxProvider:
    def __init__(self) -> None:
        self.stopped_sessions: list[str] = []
        self.cancelled_sessions: list[str] = []

    def stop_session(self, session_id: str) -> bool:
        self.stopped_sessions.append(session_id)
        return True

    def cancel(self, session_id: str) -> bool:
        self.cancelled_sessions.append(session_id)
        return True


class _WaitingActivationCompletionStore:
    def __init__(self) -> None:
        self.max_running_tasks = 4
        self.runtime_snapshot: dict = {}
        self.task = AgentRun(
            id="task-store-waiting",
            agent_id="coder",
            status=AgentRunStatus.WAITING,
            sandbox_session_id="sandbox-store",
            current_activation_id="task-store-waiting:activation:1",
        )

    def configure(
        self,
        *,
        max_running_tasks: int | None = None,
        runtime_snapshot: dict | None = None,
    ) -> None:
        if max_running_tasks is not None:
            self.max_running_tasks = int(max_running_tasks)
        if runtime_snapshot is not None:
            self.runtime_snapshot = dict(runtime_snapshot)

    def complete_agent_run_activation(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        activation_id: str,
        artifacts: list[dict] | None = None,
    ) -> AgentRun:
        return self.task

    def complete_claimed_agent_run_activation(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        request_id: str,
        activation_id: str,
        worker_id: str,
        peer_id: str | None = None,
        artifacts: list[dict] | None = None,
    ) -> tuple[bool, str, AgentRun | None]:
        return True, "", self.task


def _postgres_store_for_resolve_request(
    runtime_snapshot: dict | None = None,
) -> PostgresAgentRunStore:
    store = object.__new__(PostgresAgentRunStore)
    store.runtime_snapshot = dict(runtime_snapshot or {})
    return store


class _CaptureExecuteConn:
    def __init__(self) -> None:
        self.params: list[dict] = []

    def execute(self, _statement, params=None):
        self.params.append(dict(params or {}))
        return None


def _relation(
    owner_agent_run_id: str,
    *,
    relation_type: AgentRunRelationType | str = AgentRunRelationType.AGENT_CALL_EPHEMERAL,
    metadata: dict | None = None,
    payload: dict | None = None,
) -> AgentRunRelation:
    relation_type_value = (
        relation_type.value
        if isinstance(relation_type, AgentRunRelationType)
        else str(relation_type)
    )
    relation_payload = dict(payload or {})
    if not relation_payload:
        if relation_type_value == AgentRunRelationType.AGENT_CALL_PERSISTENT.value:
            relation_payload = {
                "conversation_scope": "persistent",
                "wait": True,
                "thread_key": "",
                "thread_summary": "Persistent thread",
            }
        else:
            relation_payload = {"conversation_scope": "ephemeral", "wait": False}
    return AgentRunRelation(
        id="",
        owner_agent_run_id=owner_agent_run_id,
        related_agent_run_id="",
        relation_type=AgentRunRelationType(relation_type_value),
        payload=relation_payload,
        metadata=dict(metadata or {}),
    )


def test_postgres_session_upsert_uses_task_and_metadata_without_pinned_session() -> None:
    store = object.__new__(PostgresAgentRunStore)
    store._session_metadata = lambda _task, metadata=None: dict(metadata or {})
    conn = _CaptureExecuteConn()
    task = AgentRun(
        id="task-upsert-session",
        agent_id="coder",
        executor=ExecutorType.REULEAUXCODER,
        execution_location=ExecutionLocation.REMOTE_SERVER,
        workdir="/srv/labrastro",
    )

    store._upsert_session_with_conn(
        conn,
        task,
        executor_session_id="server-session-1",
        metadata={"branch": "main"},
    )

    assert conn.params[0]["workdir"] == "/srv/labrastro"
    assert conn.params[0]["branch"] == "main"
    assert conn.params[0]["executor_session_id"] == "server-session-1"
from labrastro_server.services.agent_runtime.session_projection import (
    agent_run_event_to_session_events,
)
from labrastro_server.services.agent_runtime.scheduler import BasicAgentScheduler


def _model_config() -> dict:
    return {
        "providers": {
            "items": {
                "openai": {
                    "type": "openai_chat",
                    "api_key": "key",
                }
            }
        },
        "models": {
            "active_main": "main",
            "profiles": {
                "main": {
                    "provider": "openai",
                    "model": "gpt",
                    "max_tokens": 8192,
                    "max_context_tokens": 128000,
                }
            },
        },
    }


def test_control_plane_appends_named_local_action_event_for_projection() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(agent_id="chat", prompt="read local file"),
        task_id="agent-run-local-action-event",
    )

    event = control.append_agent_run_event(
        task.id,
        "local_action_waiting_peer",
        {
            "local_action_id": "local-action-1",
            "action_kind": "read_workspace_file",
            "workspace_root": "D:\\AboutDEV\\vika_mcp",
            "status": "waiting_peer",
        },
    )

    assert event is not None
    assert event.type == "local_action_waiting_peer"
    projected_type, projected = agent_run_event_to_session_events(event.to_dict())[0]
    assert projected_type == "local_action"
    assert projected["kind"] == "local_action"
    assert projected["status"] == "waiting_peer"


def _current_activation_id(control: AgentRunControlPlane, task_id: str) -> str:
    return str(control.get_agent_run(task_id).current_activation_id or "")


def test_postgres_resolve_request_without_relation_does_not_reference_parent() -> None:
    store = _postgres_store_for_resolve_request()

    request = AgentRunRequest(agent_id="coder", prompt="run")

    resolved = store._resolve_request(request)

    assert resolved.executor == ExecutorType.REULEAUXCODER
    assert resolved.execution_location == ExecutionLocation.REMOTE_SERVER
    assert resolved.executor_session_id is None


def test_postgres_resolve_relation_request_inherits_owner_runtime_session() -> None:
    parent = AgentRun(
        id="parent-run",
        agent_id="coder",
        executor=ExecutorType.CLAUDE,
        execution_location=ExecutionLocation.DAEMON_WORKTREE,
        workdir="/tmp/project",
        executor_session_id="claude-session-1",
    )
    store = _postgres_store_for_resolve_request()
    loaded_parent_ids: list[str] = []

    def get_agent_run(task_id: str) -> AgentRun:
        loaded_parent_ids.append(task_id)
        return parent

    store.get_agent_run = get_agent_run  # type: ignore[method-assign]
    request = AgentRunRequest(
        agent_id="coder",
        prompt="child",
        relation=_relation(parent.id),
    )

    resolved = store._resolve_request(request)

    assert loaded_parent_ids == [parent.id]
    assert resolved.executor == parent.executor
    assert resolved.execution_location == parent.execution_location
    assert resolved.workdir == parent.workdir
    assert resolved.executor_session_id == parent.executor_session_id


def test_control_plane_store_default_runtime_snapshot_is_self_contained() -> None:
    class EmptyRuntimeStore:
        max_running_tasks = 2
        runtime_snapshot: dict = {}

        def configure(self, **kwargs) -> None:
            if kwargs.get("runtime_snapshot") is not None:
                self.runtime_snapshot = dict(kwargs["runtime_snapshot"])

    store = EmptyRuntimeStore()
    control = AgentRunControlPlane(store=store)

    assert "main_chat" in control.runtime_snapshot["agents"]
    assert "environment_local" in control.runtime_snapshot["runtime_profiles"]
    assert store.runtime_snapshot == control.runtime_snapshot


def test_agent_call_grant_is_bound_to_capability_scope_and_config_version() -> None:
    control = AgentRunControlPlane()
    grant = AgentCallGrant(
        user_id="user-1",
        grant_scope="workspace:/repo",
        main_agent_id="planner",
        target_agent_id="researcher",
        conversation_scope="persistent",
        capability_scope={"capability_refs": ["research"], "runtime_profile": "analysis"},
        target_config_version="version-a",
        granted_at="2026-06-15T00:00:00+00:00",
    )

    control.upsert_agent_call_grant(grant)

    assert (
        control.find_agent_call_grant(
            user_id="user-1",
            grant_scope="workspace:/repo",
            main_agent_id="planner",
            target_agent_id="researcher",
            conversation_scope="persistent",
            capability_scope={
                "runtime_profile": "analysis",
                "capability_refs": ["research"],
            },
            target_config_version="version-a",
        )
        == grant
    )
    assert (
        control.find_agent_call_grant(
            user_id="user-1",
            grant_scope="workspace:/repo",
            main_agent_id="planner",
            target_agent_id="researcher",
            conversation_scope="persistent",
            capability_scope={"capability_refs": ["write"]},
            target_config_version="version-a",
        )
        is None
    )
    assert (
        control.find_agent_call_grant(
            user_id="user-1",
            grant_scope="workspace:/repo",
            main_agent_id="planner",
            target_agent_id="researcher",
            conversation_scope="persistent",
            capability_scope=grant.capability_scope,
            target_config_version="version-b",
        )
        is None
    )


def test_task_queue_claim_pin_complete_and_pr_artifact() -> None:
    control = AgentRunControlPlane(
        max_running_tasks=1,
        runtime_snapshot={
            "runtime_profiles": {
                "codex": {
                    "executor": "codex",
                    "execution_location": "daemon_worktree",
                }
            },
            "agents": {"coder": {"runtime_profile": "codex"}},
        },
    )
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="fix tests",
            executor=ExecutorType.CODEX,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
            runtime_profile_id="codex",
            workdir="runtime/worktrees/ws/coder-task",
            model="gpt-5.2",
        ),
        task_id="task-1",
    )

    claim = control.claim_agent_run_activation(worker_id="worker-1", executors=["codex"])

    assert claim is not None
    assert claim.task.id == task.id
    assert claim.activation_id == "task-1:activation:1"
    assert claim.activation is not None
    assert claim.activation.agent_run_id == task.id
    assert claim.activation.status.value == "dispatched"
    assert claim.executor_request.executor == ExecutorType.CODEX
    assert claim.executor_request.metadata["activation_id"] == claim.activation_id
    assert claim.executor_request.model == "gpt-5.2"
    assert claim.executor_request.execution_location == ExecutionLocation.DAEMON_WORKTREE
    assert claim.executor_request.worktree_role == WorktreeRole.TARGET
    assert claim.executor_request.publish_policy == PublishPolicy.NEVER
    assert claim.executor_request.metadata["worktree_role"] == "target"
    assert claim.executor_request.metadata["publish_policy"] == "never"
    assert control.claim_agent_run_activation(worker_id="worker-2", executors=["codex"]) is None

    control.pin_session(
        task.id,
        TaskSessionRef(
            agent_id="coder",
            executor=ExecutorType.CODEX,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
            task_id=task.id,
            workdir="runtime/worktrees/ws/coder-task",
            branch="agent/coder/task-1",
            executor_session_id="codex-thread-1",
        ),
    )
    control.append_executor_event(task.id, ExecutorEvent.text_event("done"))
    control.create_or_update_pr(task.id, diff="diff --git a/file b/file")
    completed = control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(
            task_id=task.id,
            status="completed",
            output="PR created",
            executor_session_id="codex-thread-1",
        ),
        activation_id=_current_activation_id(control, task.id),
    )

    assert completed.status.value == "completed"
    artifacts = control.artifacts_to_dict(task.id)
    assert artifacts[0]["type"] == "pull_request"
    assert artifacts[0]["merge_status"] == "pending_user"
    assert control.list_events(task.id, after_seq=0)[0].type == "queued"
    event_types = [event.type for event in control.list_events(task.id, after_seq=0)]
    assert "activation_queued" in event_types
    assert "activation_completed" in event_types


def test_complete_agent_run_activation_requires_current_activation_id() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(agent_id="coder", prompt="run"),
        task_id="task-activation-lock",
    )
    result = ExecutorRunResult(
        task_id=task.id,
        status="completed",
        output="done",
    )

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
        input_kind=AgentRunActivationInputKind.USER_FEEDBACK,
        input_payload={"feedback_id": "feedback-1"},
        prompt="continue",
    )

    with pytest.raises(ValueError, match="activation_mismatch"):
        control.complete_agent_run_activation(
            task.id,
            ExecutorRunResult(task_id=task.id, status="completed", output="again"),
            activation_id=stale_activation_id,
        )


def test_session_run_binding_selects_single_agent_run_mainline() -> None:
    control = AgentRunControlPlane()
    source = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="first",
            owner_session_run_id="session-run-1",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id="agent-run-main",
    )
    branch = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="branch",
            owner_session_run_id="session-run-1",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id="agent-run-branch",
    )

    main_binding = control.create_session_run_binding(
        session_run_id="session-run-1",
        session_id="chat-session-1",
        peer_id="peer-1",
        agent_run_id=source.id,
        branch_binding_id="main",
        selected=True,
        target_agent_run_id=source.id,
    )
    branch_binding = control.create_session_run_binding(
        session_run_id="session-run-1",
        session_id="chat-session-1",
        peer_id="peer-1",
        agent_run_id=branch.id,
        branch_binding_id="branch-1",
        selected=True,
        parent_branch_binding_id=main_binding.branch_binding_id,
        base_session_item_id="msg-1",
        source_agent_run_id=source.id,
        target_agent_run_id=branch.id,
    )

    selected = control.find_session_run_binding(session_run_id="session-run-1")
    assert selected is not None
    assert selected.id == branch_binding.id
    assert selected.agent_run_id == branch.id
    assert control.find_session_run_binding(
        session_run_id="session-run-1",
        branch_binding_id="main",
        selected_only=False,
    ).selected is False
    assert control.find_session_run_binding(
        session_run_id="session-run-1",
        branch_binding_id="branch-1",
    ).source_agent_run_id == source.id


@pytest.mark.parametrize(
    "closed_status",
    ["failed", "cancelled"],
)
def test_session_run_binding_closes_when_bound_agent_run_closed(
    closed_status: str,
) -> None:
    control = AgentRunControlPlane()
    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="main",
            owner_session_run_id="session-run-terminal",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id=f"agent-run-{closed_status}",
    )
    binding = control.create_session_run_binding(
        session_run_id="session-run-terminal",
        session_id="chat-session-terminal",
        peer_id="peer-1",
        agent_run_id=run.id,
        branch_binding_id="main",
        selected=True,
        target_agent_run_id=run.id,
    )

    if closed_status == "failed":
        control.fail_agent_run(run.id, error="boom")
    elif closed_status == "cancelled":
        assert control.cancel_agent_run(run.id, reason="user_stop") is True

    assert (
        control.find_session_run_binding(
            session_run_id="session-run-terminal",
            branch_binding_id="main",
            selected_only=False,
        )
        is None
    )
    closed = control.find_session_run_binding(
        session_run_id="session-run-terminal",
        branch_binding_id="main",
        selected_only=False,
        include_inactive=True,
    )
    assert closed is not None
    assert closed.id == binding.id
    assert closed.status == SessionRunBindingStatus.CLOSED
    assert closed.metadata["status_reason"] == f"agent_run_closed:{closed_status}"
    assert control.list_session_run_bindings(
        session_run_id="session-run-terminal",
        status=SessionRunBindingStatus.ACTIVE,
    ) == []
    with pytest.raises(ValueError, match="not continuable"):
        control.continue_agent_run(
            run.id,
            input_kind=AgentRunActivationInputKind.USER_REQUEST,
            input_payload={
                "source": "session_run_continue",
                "session_run_id": binding.session_run_id,
                "branch_binding_id": binding.branch_binding_id,
            },
            resume_session=True,
            prompt="ordinary next turn",
        )
    events = [event.to_dict() for event in control.list_events(run.id)]
    closed_events = [
        event for event in events if event["type"] == "session_run_binding_closed"
    ]
    assert closed_events
    assert closed_events[-1]["payload"]["binding_id"] == binding.id


@pytest.mark.parametrize("settled_status", ["completed", "blocked"])
def test_session_run_binding_stays_active_when_activation_settled(
    settled_status: str,
) -> None:
    control = AgentRunControlPlane()
    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="main",
            owner_session_run_id="session-run-settled",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id=f"agent-run-settled-{settled_status}",
    )
    binding = control.create_session_run_binding(
        session_run_id="session-run-settled",
        session_id="chat-session-settled",
        peer_id="peer-1",
        agent_run_id=run.id,
        branch_binding_id="main",
        selected=True,
        target_agent_run_id=run.id,
    )

    completed = control.complete_agent_run_activation(
        run.id,
        ExecutorRunResult(task_id=run.id, status=settled_status, output="done"),
        activation_id=_current_activation_id(control, run.id),
    )

    expected_mainline_state = (
        AgentRunMainlineState.CONTINUABLE
        if settled_status == "completed"
        else AgentRunMainlineState.BLOCKED
    )
    expected_activation_state = (
        AgentRunActivationState.COMPLETED
        if settled_status == "completed"
        else AgentRunActivationState.BLOCKED
    )
    assert completed.mainline_state == expected_mainline_state
    assert completed.activation_state == expected_activation_state

    active = control.find_session_run_binding(
        session_run_id="session-run-settled",
        branch_binding_id="main",
        selected_only=False,
    )

    assert active is not None
    assert active.id == binding.id
    assert active.status == SessionRunBindingStatus.ACTIVE
    assert [
        item.id
        for item in control.list_session_run_bindings(
            session_run_id="session-run-settled",
            status=SessionRunBindingStatus.ACTIVE,
        )
    ] == [binding.id]
    events = [event.to_dict() for event in control.list_events(run.id)]
    assert not [
        event for event in events if event["type"] == "session_run_binding_closed"
    ]


def test_stop_agent_run_activation_settles_mainline_without_closing_binding() -> None:
    control = AgentRunControlPlane()
    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="main",
            owner_session_run_id="session-run-stop",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id="agent-run-stop",
    )
    binding = control.create_session_run_binding(
        session_run_id="session-run-stop",
        session_id="chat-session-stop",
        peer_id="peer-1",
        agent_run_id=run.id,
        branch_binding_id="main",
        selected=True,
        target_agent_run_id=run.id,
    )

    assert control.stop_agent_run_activation(run.id, reason="user_stop") is True
    stopped = control.get_agent_run(run.id)

    assert stopped.status == AgentRunStatus.COMPLETED
    assert stopped.mainline_state == AgentRunMainlineState.CONTINUABLE
    assert stopped.activation_state == AgentRunActivationState.CANCELLED
    assert stopped.cancel_reason == "user_stop"
    stopped_activation_id = stopped.current_activation_id
    active = control.find_session_run_binding(
        session_run_id="session-run-stop",
        branch_binding_id="main",
        selected_only=False,
    )
    assert active is not None
    assert active.id == binding.id
    assert active.status == SessionRunBindingStatus.ACTIVE

    continued = control.continue_agent_run(
        run.id,
        input_kind=AgentRunActivationInputKind.USER_REQUEST,
        input_payload={
            "source": "session_run_continue",
            "session_run_id": binding.session_run_id,
            "branch_binding_id": binding.branch_binding_id,
        },
        resume_session=True,
        prompt="ordinary next turn",
    )
    assert continued.id == run.id
    assert continued.current_activation_id != stopped_activation_id
    assert continued.status == AgentRunStatus.QUEUED


def test_session_run_binding_active_lookup_keeps_settled_continuable_binding() -> None:
    control = AgentRunControlPlane()
    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="main",
            owner_session_run_id="session-run-stale",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id="agent-run-stale-terminal",
    )
    control.complete_agent_run_activation(
        run.id,
        ExecutorRunResult(task_id=run.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, run.id),
    )
    binding = control.create_session_run_binding(
        session_run_id="session-run-stale",
        session_id="chat-session-stale",
        peer_id="peer-1",
        agent_run_id=run.id,
        branch_binding_id="main",
        selected=True,
        target_agent_run_id=run.id,
    )

    active = control.find_session_run_binding(
        session_run_id="session-run-stale",
        branch_binding_id="main",
        selected_only=False,
    )

    assert active is not None
    assert active.id == binding.id
    assert active.status == SessionRunBindingStatus.ACTIVE
    assert [
        item.id
        for item in control.list_session_run_bindings(
            session_run_id="session-run-stale",
            status=SessionRunBindingStatus.ACTIVE,
        )
    ] == [binding.id]


def test_session_run_binding_requires_explicit_branch_binding_id() -> None:
    control = AgentRunControlPlane()
    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="main",
            owner_session_run_id="session-run-1",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id="agent-run-main",
    )

    with pytest.raises(ValueError, match="branch_binding_id is required"):
        control.create_session_run_binding(
            session_run_id="session-run-1",
            session_id="chat-session-1",
            peer_id="peer-1",
            agent_run_id=run.id,
            branch_binding_id="",
            selected=True,
            target_agent_run_id=run.id,
        )


def test_session_run_binding_scope_is_included_in_executor_claim_metadata() -> None:
    control = AgentRunControlPlane()
    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="main",
            owner_session_run_id="session-run-claim",
            source="chat",
            trigger_mode="interactive_chat",
            executor=ExecutorType.REULEAUXCODER,
        ),
        task_id="agent-run-claim",
    )
    binding = control.create_session_run_binding(
        session_run_id="session-run-claim",
        session_id="chat-session-claim",
        peer_id="peer-1",
        agent_run_id=run.id,
        branch_binding_id="branch-claim",
        selected=True,
        target_agent_run_id=run.id,
    )

    claim = control.claim_agent_run_activation(
        worker_id="worker-1",
        executors=["reuleauxcoder"],
    )

    assert claim is not None
    assert claim.executor_request.metadata["session_run_id"] == "session-run-claim"
    assert claim.executor_request.metadata["session_run_binding_id"] == binding.id
    assert claim.executor_request.metadata["branch_binding_id"] == "branch-claim"
    assert claim.executor_request.metadata["binding_agent_run_id"] == run.id


def test_select_session_run_branch_switches_input_target_without_stopping_runs() -> None:
    control = AgentRunControlPlane()
    source = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="first",
            owner_session_run_id="session-run-switch",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id="agent-run-main",
    )
    branch = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="branch",
            owner_session_run_id="session-run-switch",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id="agent-run-branch",
    )
    control.create_session_run_binding(
        session_run_id="session-run-switch",
        session_id="chat-session-switch",
        peer_id="peer-1",
        agent_run_id=source.id,
        branch_binding_id="main",
        selected=True,
        target_agent_run_id=source.id,
    )
    control.create_session_run_binding(
        session_run_id="session-run-switch",
        session_id="chat-session-switch",
        peer_id="peer-1",
        agent_run_id=branch.id,
        branch_binding_id="branch-1",
        selected=False,
        parent_branch_binding_id="main",
        base_session_item_id="msg-1",
        source_agent_run_id=source.id,
        target_agent_run_id=branch.id,
    )

    selected = control.select_session_run_branch(
        session_run_id="session-run-switch",
        branch_binding_id="branch-1",
        peer_id="peer-1",
    )

    assert selected.agent_run_id == branch.id
    assert control.find_session_run_binding(
        session_run_id="session-run-switch",
        branch_binding_id="main",
        selected_only=False,
    ).selected is False
    assert control.get_agent_run(source.id).status == AgentRunStatus.QUEUED
    assert control.get_agent_run(branch.id).status == AgentRunStatus.QUEUED


def test_select_session_run_branch_can_select_closed_history_branch_without_reactivating() -> None:
    control = AgentRunControlPlane()
    source = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="first",
            owner_session_run_id="session-run-history-select",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id="agent-run-history-main",
    )
    branch = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="branch",
            owner_session_run_id="session-run-history-select",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id="agent-run-history-branch",
    )
    control.create_session_run_binding(
        session_run_id="session-run-history-select",
        session_id="chat-session-history-select",
        peer_id="peer-1",
        agent_run_id=source.id,
        branch_binding_id="main",
        selected=True,
        target_agent_run_id=source.id,
    )
    branch_binding = control.create_session_run_binding(
        session_run_id="session-run-history-select",
        session_id="chat-session-history-select",
        peer_id="peer-1",
        agent_run_id=branch.id,
        branch_binding_id="branch-closed",
        selected=False,
        parent_branch_binding_id="main",
        base_session_item_id="msg-1",
        source_agent_run_id=source.id,
        target_agent_run_id=branch.id,
    )
    control.complete_agent_run_activation(
        branch.id,
        ExecutorRunResult(task_id=branch.id, status="cancelled", output="stopped"),
        activation_id=_current_activation_id(control, branch.id),
    )
    assert (
        control.find_session_run_binding(
            session_run_id="session-run-history-select",
            branch_binding_id="branch-closed",
            selected_only=False,
        )
        is None
    )

    selected = control.select_session_run_branch(
        session_run_id="session-run-history-select",
        branch_binding_id="branch-closed",
        peer_id="peer-1",
    )

    assert selected.id == branch_binding.id
    assert selected.selected is True
    assert selected.status == SessionRunBindingStatus.CLOSED
    assert (
        control.find_session_run_binding(
            session_run_id="session-run-history-select",
            branch_binding_id="branch-closed",
            selected_only=False,
        )
        is None
    )
    closed = control.find_session_run_binding(
        session_run_id="session-run-history-select",
        branch_binding_id="branch-closed",
        selected_only=False,
        include_inactive=True,
    )
    assert closed is not None
    assert closed.selected is True
    assert closed.status == SessionRunBindingStatus.CLOSED


def test_session_run_continue_reuses_bound_agent_run_mainline() -> None:
    control = AgentRunControlPlane()
    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="chat",
            prompt="first",
            owner_session_run_id="session-run-1",
            source="chat",
            trigger_mode="interactive_chat",
        ),
        task_id="agent-run-main",
    )
    binding = control.create_session_run_binding(
        session_run_id="session-run-1",
        session_id="chat-session-1",
        peer_id="peer-1",
        agent_run_id=run.id,
        branch_binding_id="main",
        selected=True,
        target_agent_run_id=run.id,
    )
    first_activation_id = _current_activation_id(control, run.id)
    completed = control.complete_agent_run_activation(
        run.id,
        ExecutorRunResult(task_id=run.id, status="completed", output="first done"),
        activation_id=first_activation_id,
    )
    assert completed.mainline_state == AgentRunMainlineState.CONTINUABLE
    assert completed.activation_state == AgentRunActivationState.COMPLETED

    continued = control.continue_agent_run(
        binding.agent_run_id,
        input_kind=AgentRunActivationInputKind.USER_REQUEST,
        input_payload={
            "source": "session_run_continue",
            "session_run_id": binding.session_run_id,
            "branch_binding_id": binding.branch_binding_id,
        },
        resume_session=True,
        prompt="second",
    )

    assert continued.id == run.id
    assert continued.current_activation_id == "agent-run-main:activation:2"
    assert continued.mainline_state == AgentRunMainlineState.EXECUTING
    assert continued.activation_state == AgentRunActivationState.QUEUED
    active_binding = control.find_session_run_binding(
        session_run_id="session-run-1",
        branch_binding_id="main",
        selected_only=False,
    )
    assert active_binding is not None
    assert active_binding.id == binding.id
    assert active_binding.status == SessionRunBindingStatus.ACTIVE
    assert active_binding.metadata["status_reason"] == "session_run_continue"
    assert [item["id"] for item in control.list_agent_runs()] == [run.id]


def test_agent_run_request_projects_source_and_sandbox_fields() -> None:
    control = AgentRunControlPlane()

    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            source="manual",
            sandbox_id="sbx-1",
            sandbox_session_id="ssn-1",
            workspace_ref="repo:example",
        ),
        task_id="run-1",
    )

    assert run.id == "run-1"
    assert run.source.value == "manual"
    assert run.sandbox_id == "sbx-1"
    task = control.agent_run_to_dict(run.id)
    assert task["agent_run_id"] == "run-1"
    assert task["source"] == "manual"
    assert task["sandbox_id"] == "sbx-1"
    assert task["sandbox_session_id"] == "ssn-1"
    assert task["workspace_ref"] == "repo:example"
    assert "delegated_by_run_id" not in task
    assert "parent_run_id" not in task


def test_agent_run_request_normalizes_budget_into_control_plane_metadata() -> None:
    control = AgentRunControlPlane()

    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            budget={
                "token_budget": "1200",
                "max_turns": 3,
                "max_tool_calls": "8",
                "timeout_sec": 60,
            },
        ),
        task_id="run-budget",
    )

    assert run.metadata["budget"] == {
        "token_budget": 1200,
        "max_turns": 3,
        "max_tool_calls": 8,
        "timeout_sec": 60,
    }
    assert control.agent_run_to_dict(run.id)["budget"] == run.metadata["budget"]


def test_agent_run_claim_includes_budget_in_executor_request() -> None:
    control = AgentRunControlPlane()
    control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            executor="reuleauxcoder",
            budget={"max_tool_calls": "2", "timeout_sec": 30},
        ),
        task_id="run-budget",
    )

    claim = control.claim_agent_run_activation(worker_id="worker-1", executors=["reuleauxcoder"])

    assert claim is not None
    assert claim.executor_request.budget == {"max_tool_calls": 2, "timeout_sec": 30}
    raw = claim.executor_request.to_dict()
    assert raw["budget"] == {"max_tool_calls": 2, "timeout_sec": 30}
    assert ExecutorRunRequest.from_dict(raw).budget == {
        "max_tool_calls": 2,
        "timeout_sec": 30,
    }


def test_budget_exceeded_executor_result_blocks_agent_run_with_session_end_audit() -> None:
    control = AgentRunControlPlane()
    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            executor="reuleauxcoder",
            budget={"max_turns": 1},
        ),
        task_id="run-budget-terminal",
    )

    message = "AgentRun budget exceeded: max_turns=1"
    completed = control.complete_agent_run_activation(
        run.id,
        ExecutorRunResult(
            task_id=run.id,
            status="blocked",
            output=f"({message})",
            error=message,
            events=[
                ExecutorEvent.session_run_end(
                    f"({message})",
                    response_rendered=True,
                    status="budget_exceeded",
                    error=message,
                    session_state="budget_exceeded",
                )
            ],
        ),
        activation_id=_current_activation_id(control, run.id),
    )
    events = [event.to_dict() for event in control.list_events(run.id)]

    assert completed.status == AgentRunStatus.BLOCKED
    assert completed.failure_reason == message
    assert control.agent_run_to_dict(run.id)["status"] == "blocked"
    session_end = next(event for event in events if event["type"] == "session_run_end")
    assert session_end["payload"]["data"]["status"] == "budget_exceeded"
    assert session_end["payload"]["data"]["error"] == message
    assert events[-1]["type"] == "blocked"
    assert events[-1]["payload"]["agent_run"]["status"] == "blocked"


def test_session_projection_inverts_render_response_fallback_to_response_rendered() -> None:
    projected = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 7,
            "type": "session_run_end",
            "payload": {
                "data": {
                    "response": "streamed answer",
                    "render_response": False,
                }
            },
        }
    )

    assert projected == [
        (
            "session_run_end",
            {
                "agent_run_id": "run-1",
                "agent_id": "agent",
                "workflow": "agent_run",
                "raw_event_refs": [
                    {"agent_run_id": "run-1", "seq": 7, "type": "session_run_end"}
                ],
                "response": "streamed answer",
                "response_rendered": True,
            },
        )
    ]

    explicit = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 8,
            "type": "session_run_end",
            "payload": {
                "data": {
                    "response": "final answer",
                    "render_response": False,
                    "response_rendered": False,
                }
            },
        }
    )
    assert explicit[0][1]["response_rendered"] is False


def test_agent_run_request_rejects_unknown_budget_fields() -> None:
    with pytest.raises(ValueError, match="unsupported AgentRun budget field"):
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            budget={"made_up": 1},
        )


def test_agent_run_request_rejects_relation_metadata_envelope() -> None:
    with pytest.raises(ValueError, match="relation or external business identity"):
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            metadata={
                "called_by_agent_run_id": "parent-run",
                "relation_type": "agent_call_ephemeral",
            },
        )


def test_cancel_agent_run_cascades_to_child_agent_runs() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="parent"),
        task_id="parent-run",
    )
    running_child = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="child",
            relation=_relation(parent.id),
        ),
        task_id="child-run",
    )
    queued_grandchild = control.submit_agent_run(
        AgentRunRequest(
            agent_id="reviewer",
            prompt="grandchild",
            relation=_relation(running_child.id),
        ),
        task_id="grandchild-run",
    )
    control.append_executor_event(running_child.id, ExecutorEvent.status("running"))

    assert control.cancel_agent_run(parent.id, reason="user_stop") is True

    child = control.get_agent_run(running_child.id)
    grandchild = control.get_agent_run(queued_grandchild.id)
    child_events = [event.to_dict() for event in control.list_events(child.id)]
    grandchild_events = [event.to_dict() for event in control.list_events(grandchild.id)]

    assert child.status == AgentRunStatus.RUNNING
    child_cancel_requested = [
        event for event in child_events if event["type"] == "cancel_requested"
    ][0]
    child_parent_cancelled = [
        event for event in child_events if event["type"] == "parent_cancelled"
    ][0]
    child_agent_call_result = [
        event for event in child_events if event["type"] == "agent_call_failed"
    ][0]
    child_lifecycle_events = [
        event for event in child_events if event["type"] == "lifecycle_hook"
    ]
    assert child_cancel_requested["payload"]["reason"] == "parent_cancelled:user_stop"
    assert child_parent_cancelled["payload"]["owner_agent_run_id"] == parent.id
    assert child_agent_call_result["payload"]["target_agent_run_id"] == (
        queued_grandchild.id
    )
    assert [event["payload"]["event_name"] for event in child_lifecycle_events] == [
        "TaskCompleted",
        "SubagentStop",
    ]
    assert child_lifecycle_events[0]["payload"]["payload"]["child_agent_run_id"] == (
        queued_grandchild.id
    )
    assert child_lifecycle_events[0]["payload"]["payload"]["status"] == "cancelled"
    assert grandchild.status == AgentRunStatus.CANCELLED
    assert grandchild.cancel_reason == "parent_cancelled:user_stop"
    assert grandchild_events[-2]["type"] == "cancelled"
    assert grandchild_events[-1]["type"] == "parent_cancelled"


def test_child_agent_run_terminal_state_is_projected_to_parent_audit() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="parent"),
        task_id="parent-run",
    )
    child = control.submit_agent_run(
        AgentRunRequest(
            agent_id="reviewer",
            prompt="review output",
            relation=_relation(parent.id),
        ),
        task_id="child-run",
    )

    control.complete_agent_run_activation(
        child.id,
        ExecutorRunResult(
            task_id=child.id,
            status="completed",
            output="review passed",
        ),
        activation_id=_current_activation_id(control, child.id),
    )

    parent_events = [event.to_dict() for event in control.list_events(parent.id)]
    agent_call_event = [
        event for event in parent_events if event["type"] == "agent_call_result"
    ][0]
    lifecycle_events = [
        event for event in parent_events if event["type"] == "lifecycle_hook"
    ]
    assert agent_call_event["payload"] == {
        "agent_id": "reviewer",
        "target_agent_run_id": child.id,
        "conversation_scope": "ephemeral",
        "wait": False,
        "thread_key": "",
        "status": "completed",
        "summary": "review passed",
        "evidence_refs": [],
        "artifact_refs": [],
        "metrics": {},
        "error_code": "",
        "message": "",
    }
    assert [event["payload"]["event_name"] for event in lifecycle_events] == [
        "TaskCompleted",
        "SubagentStop",
    ]
    assert lifecycle_events[0]["payload"]["agent_run_id"] == parent.id
    assert lifecycle_events[0]["payload"]["payload"]["child_agent_run_id"] == child.id
    assert lifecycle_events[0]["payload"]["payload"]["status"] == "completed"
    assert lifecycle_events[1]["payload"]["agent_run_id"] == parent.id
    assert lifecycle_events[1]["payload"]["payload"]["child_agent_run_id"] == child.id
    assert lifecycle_events[1]["payload"]["payload"]["status"] == "completed"
    session_lifecycle_events = [
        agent_run_event_to_session_events(event)[0]
        for event in lifecycle_events
    ]
    assert [event_type for event_type, _ in session_lifecycle_events] == [
        "lifecycle_hook",
        "lifecycle_hook",
    ]
    assert [payload["event_name"] for _, payload in session_lifecycle_events] == [
        "TaskCompleted",
        "SubagentStop",
    ]


def test_persistent_agent_terminal_state_projects_summary_to_parent() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="parent"),
        task_id="parent-parallel-run",
    )
    child = control.call_persistent_agent(
        owner_agent_run_id=parent.id,
        owner_session_run_id="session-1",
        agent_id="researcher",
        prompt="collect context",
        thread_key="project-context",
        thread_summary="Project context research",
    )

    control.complete_agent_run_activation(
        child.id,
        ExecutorRunResult(
            task_id=child.id,
            status="completed",
            output="context summary",
        ),
        activation_id=_current_activation_id(control, child.id),
    )

    parent_events = [event.to_dict() for event in control.list_events(parent.id)]
    assert not [
        event for event in parent_events if event["type"] == "agent_relation_completed"
    ]
    agent_call_event = [
        event for event in parent_events if event["type"] == "agent_call_result"
    ][0]
    assert agent_call_event["payload"]["target_agent_run_id"] == child.id
    assert agent_call_event["payload"]["agent_id"] == "researcher"
    assert agent_call_event["payload"]["conversation_scope"] == "persistent"
    assert agent_call_event["payload"]["thread_key"] == "project-context"
    assert agent_call_event["payload"]["summary"] == "context summary"
    projected = agent_run_event_to_session_events(agent_call_event)[0]
    assert projected[0] == "agent_run_event"
    assert projected[1]["phase"] == "agent_call_result"
    assert projected[1]["agent_call"]["target_agent_run_id"] == child.id
    assert projected[1]["agent_call"]["summary"] == "context summary"
    assert "events" not in projected[1]


def test_waiting_agent_call_target_first_resumes_after_owner_completion() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="parent"),
        task_id="parent-run",
    )
    child = control.call_persistent_agent(
        owner_agent_run_id=parent.id,
        owner_session_run_id="session-1",
        agent_id="researcher",
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
        ExecutorRunResult(
            task_id=child.id,
            status="completed",
            output="context summary",
        ),
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
        "parent-run:activation:2"
    )
    assert detail["activations"][-1]["input_kind"] == "agent_feedback"
    assert detail["activations"][-1]["input_payload"]["target_agent_run_id"] == child.id


def test_agent_call_feedback_resume_keeps_sandbox_session_alive() -> None:
    sandbox = _FakeSandboxProvider()
    control = AgentRunControlPlane(max_running_tasks=4, sandbox_provider=sandbox)
    parent = control.submit_agent_run(
        AgentRunRequest(
            agent_id="planner",
            prompt="parent",
            executor_session_id="executor-parent",
            sandbox_session_id="sandbox-parent",
        ),
        task_id="parent-sandbox-run",
    )
    child = control.call_persistent_agent(
        owner_agent_run_id=parent.id,
        owner_session_run_id="session-1",
        agent_id="researcher",
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
        ExecutorRunResult(task_id=child.id, status="completed", output="context summary"),
        activation_id=_current_activation_id(control, child.id),
    )

    resumed = control.complete_agent_run_activation(
        parent.id,
        ExecutorRunResult(task_id=parent.id, status="completed", output="waiting"),
        activation_id=_current_activation_id(control, parent.id),
    )

    assert resumed.status == AgentRunStatus.QUEUED
    assert resumed.sandbox_session_id == "sandbox-parent"
    assert sandbox.stopped_sessions == []


def test_close_agent_thread_binding_cancels_target_and_allows_recreate() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="parent"),
        task_id="parent-close-binding",
    )
    child = control.call_persistent_agent(
        owner_agent_run_id=parent.id,
        owner_session_run_id="session-close-binding",
        agent_id="researcher",
        prompt="collect context",
        thread_key="project",
        thread_summary="Project context",
        wait=False,
    )
    binding = control.load_agent_run_detail(parent.id)["agent_thread_bindings"][0]

    assert control.close_agent_thread_binding(binding["id"], reason="user_closed")

    closed = control.load_agent_run_detail(parent.id)["agent_thread_bindings"][0]
    assert closed["status"] == AgentThreadBindingStatus.CLOSED.value
    assert control.get_agent_run(child.id).status == AgentRunStatus.CANCELLED

    replacement = control.call_persistent_agent(
        owner_agent_run_id=parent.id,
        owner_session_run_id="session-close-binding",
        agent_id="researcher",
        prompt="collect again",
        thread_key="project",
        thread_summary="Project context",
        wait=False,
    )
    reopened = control.load_agent_run_detail(parent.id)["agent_thread_bindings"][0]
    assert replacement.id != child.id
    assert reopened["status"] == AgentThreadBindingStatus.ACTIVE.value
    assert reopened["target_agent_run_id"] == replacement.id


def test_unavailable_agent_thread_binding_blocks_silent_recreate() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="parent"),
        task_id="parent-unavailable-binding",
    )
    child = control.call_persistent_agent(
        owner_agent_run_id=parent.id,
        owner_session_run_id="session-unavailable-binding",
        agent_id="researcher",
        prompt="collect context",
        thread_key="project",
        thread_summary="Project context",
        wait=False,
    )
    binding = control.load_agent_run_detail(parent.id)["agent_thread_bindings"][0]

    assert control.mark_agent_thread_binding_unavailable(
        binding["id"],
        reason="agent_config_unavailable",
        cancel_target=False,
    )

    with pytest.raises(AgentCallDispatchError) as exc_info:
        control.call_persistent_agent(
            owner_agent_run_id=parent.id,
            owner_session_run_id="session-unavailable-binding",
            agent_id="researcher",
            prompt="collect again",
            thread_key="project",
            thread_summary="Project context",
            wait=False,
        )

    assert exc_info.value.code == "agent_thread_unavailable"
    assert control.get_agent_run(child.id).status == AgentRunStatus.QUEUED


def test_delete_owner_session_agent_thread_bindings_cancels_targets() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="parent"),
        task_id="parent-delete-binding",
    )
    child = control.call_persistent_agent(
        owner_agent_run_id=parent.id,
        owner_session_run_id="session-delete-binding",
        agent_id="researcher",
        prompt="collect context",
        thread_key="project",
        thread_summary="Project context",
        wait=False,
    )
    binding = control.load_agent_run_detail(parent.id)["agent_thread_bindings"][0]

    deleted = control.delete_agent_thread_bindings_for_owner_session(
        "session-delete-binding",
        reason="owner_session_deleted",
    )

    assert deleted == [binding["id"]]
    assert control.get_agent_run(child.id).status == AgentRunStatus.CANCELLED
    assert control.load_agent_run_detail(parent.id)["agent_thread_bindings"] == []
    assert control.load_agent_run_detail(child.id)["agent_thread_bindings"] == []


def test_run_lifetime_agent_thread_binding_closes_when_main_run_terminal() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="parent"),
        task_id="parent-run-lifetime-binding",
    )
    child = control.submit_agent_run(
        AgentRunRequest(
            agent_id="researcher",
            prompt="child",
            owner_session_run_id="session-run-lifetime-binding",
        ),
        task_id="child-run-lifetime-binding",
    )
    binding = AgentThreadBinding(
        id="binding-run-lifetime",
        owner_session_run_id="session-run-lifetime-binding",
        main_agent_run_id=parent.id,
        agent_id="researcher",
        target_agent_run_id=child.id,
        thread_key="project",
        thread_summary="Project context",
        binding_lifetime=AgentThreadBindingLifetime.RUN,
    )
    control.upsert_agent_thread_binding(binding)

    control.complete_agent_run_activation(
        parent.id,
        ExecutorRunResult(task_id=parent.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, parent.id),
    )

    updated = control.load_agent_run_detail(parent.id)["agent_thread_bindings"][0]
    assert updated["status"] == AgentThreadBindingStatus.CLOSED.value
    assert control.get_agent_run(child.id).status == AgentRunStatus.CANCELLED


def test_persistent_agent_call_rejects_relation_metadata_override() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="parent"),
        task_id="parent-parallel-run",
    )

    with pytest.raises(ValueError, match="cannot override relation fields"):
        control.call_persistent_agent(
            owner_agent_run_id=parent.id,
            owner_session_run_id="session-1",
            agent_id="researcher",
            prompt="collect context",
            thread_key="project-context",
            thread_summary="Project context research",
            metadata={
                "called_by_agent_run_id": "other-run",
            },
        )

    assert not control.list_agent_runs(agent_id="researcher")
    assert not control.load_agent_run_detail(parent.id)["relations"]


def test_lifecycle_agent_adapter_child_completion_projects_to_parent_session() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="parent"),
        task_id="parent-run",
    )

    class Agent:
        def __init__(self) -> None:
            self.state = SimpleNamespace(current_round=0)
            self.runtime_task_id = parent.id
            self.agent_run_control_plane = control
            self.runtime_config = SimpleNamespace(
                agent_registry=SimpleNamespace(
                    agents={"reviewer": AgentConfig(id="reviewer")}
                )
            )

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

        def _emit_event(self, event) -> None:  # noqa: ARG002
            return None

    declaration = LifecycleHookDeclaration.from_dict(
        "hook:lifecycle-agent-review",
        {
            "event": "Stop",
            "source": "admin_managed",
            "placement": "server",
            "handler_type": "agent",
            "handler_ref": "reviewer",
            "display_name": "Lifecycle reviewer",
            "summary": "Delegates Stop review to a child AgentRun.",
            "permissions": [],
            "trust": "trusted",
            "technical": {"prompt": "review lifecycle output"},
        },
    )
    agent = Agent()
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=default_lifecycle_hook_runtime_adapters(),
    )
    agent.lifecycle_dispatcher = dispatcher
    bind_lifecycle_runtime_adapters_to_agent(agent)

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "Stop",
            agent_run_id=parent.id,
            session_run_id="session-1",
            turn_id="turn-1",
            trigger_source="taskflow",
        )
    )

    assert results[0].output.diagnostics[0]["code"] == "agent_run_submitted"
    child = control.list_agent_runs(agent_id="reviewer")[0]
    assert "parent_run_id" not in child
    assert "lifecycle_hook_id" not in child["metadata"]
    relation = control.load_agent_run_detail(parent.id)["relations"][0]
    assert relation["payload"]["lifecycle_hook_id"] == "hook:lifecycle-agent-review"
    assert relation["payload"]["parent_session_id"] == "session-1"
    assert relation["payload"]["parent_turn_id"] == "turn-1"

    control.complete_agent_run_activation(
        child["id"],
        ExecutorRunResult(
            task_id=child["id"],
            status="completed",
            output="review passed",
        ),
        activation_id=_current_activation_id(control, child["id"]),
    )

    parent_events = [event.to_dict() for event in control.list_events(parent.id)]
    agent_call_event = [
        event for event in parent_events if event["type"] == "agent_call_result"
    ][0]
    lifecycle_events = [
        event for event in parent_events if event["type"] == "lifecycle_hook"
    ]
    session_events = agent_run_event_to_session_events(agent_call_event)
    session_lifecycle_events = [
        agent_run_event_to_session_events(event)[0]
        for event in lifecycle_events
    ]

    assert agent_call_event["payload"]["target_agent_run_id"] == child["id"]
    assert agent_call_event["payload"]["conversation_scope"] == "ephemeral"
    assert session_events[0][0] == "agent_run_event"
    assert session_events[0][1]["phase"] == "agent_call_result"
    assert session_events[0][1]["agent_call"]["target_agent_run_id"] == child["id"]
    assert session_events[0][1]["agent_call"]["summary"] == "review passed"
    assert [event["payload"]["event_name"] for event in lifecycle_events] == [
        "TaskCompleted",
        "SubagentStop",
    ]
    assert lifecycle_events[0]["payload"]["payload"]["lifecycle_hook_id"] == (
        "hook:lifecycle-agent-review"
    )
    assert lifecycle_events[0]["payload"]["payload"]["parent_session_id"] == "session-1"
    assert lifecycle_events[0]["payload"]["payload"]["parent_turn_id"] == "turn-1"
    assert [payload["event_name"] for _, payload in session_lifecycle_events] == [
        "TaskCompleted",
        "SubagentStop",
    ]


def test_activation_and_feedback_events_project_without_internal_payloads() -> None:
    activation_event = {
        "agent_run_id": "run-1",
        "seq": 1,
        "type": "activation_queued",
        "payload": {
            "activation_id": "run-1:activation:2",
            "activation": {
                "id": "run-1:activation:2",
                "agent_run_id": "run-1",
                "seq": 2,
                "input_kind": "server_feedback",
                "status": "queued",
                "prompt": "internal repair prompt",
                "result_payload": {"secret": "nope"},
            },
        },
    }
    feedback_event = {
        "agent_run_id": "run-1",
        "seq": 2,
        "type": "agent_run_feedback_added",
        "payload": {
            "feedback_id": "feedback-1",
            "feedback": {
                "id": "feedback-1",
                "agent_run_id": "run-1",
                "source": "server",
                "kind": "candidate_validation_failed",
                "payload": {"internal": "details"},
                "visibility": "internal",
            },
        },
    }
    steer_event = {
        "agent_run_id": "run-1",
        "seq": 3,
        "type": "activation_steer_queued",
        "payload": {
            "activation_id": "run-1:activation:2",
            "steer_id": "steer-1",
            "steer": {
                "id": "steer-1",
                "activation_id": "run-1:activation:2",
                "source": "user",
                "payload": {"message": "internal same-turn input"},
                "status": "queued",
                "metadata": {"secret": "nope"},
            },
        },
    }
    steer_delivered_event = {
        "agent_run_id": "run-1",
        "seq": 4,
        "type": "activation_steer_delivered",
        "payload": {
            "activation_id": "run-1:activation:2",
            "steer_id": "steer-1",
            "steer": {
                "id": "steer-1",
                "activation_id": "run-1:activation:2",
                "source": "user",
                "payload": {"message": "internal same-turn input"},
                "status": "delivered",
                "metadata": {"secret": "nope"},
            },
        },
    }
    agent_call_failed_event = {
        "agent_run_id": "run-1",
        "seq": 5,
        "type": "agent_run_feedback_added",
        "payload": {
            "feedback_id": "feedback-agent-call-failed",
            "feedback": {
                "id": "feedback-agent-call-failed",
                "agent_run_id": "run-1",
                "source": "system",
                "kind": "agent_call_failed",
                "payload": {
                    "agent_id": "Reviewer",
                    "conversation_scope": "ephemeral",
                    "wait": True,
                    "target_agent_run_id": "",
                    "status": "failed",
                    "message": "AgentConfig not found: Reviewer",
                    "error_code": "agent_not_found",
                    "request_preview": "internal request text",
                },
                "visibility": "user_visible",
            },
        },
    }

    projected_activation = agent_run_event_to_session_events(activation_event)[0]
    projected_feedback = agent_run_event_to_session_events(feedback_event)[0]
    projected_steer = agent_run_event_to_session_events(steer_event)[0]
    projected_steer_delivered = agent_run_event_to_session_events(
        steer_delivered_event
    )[0]
    projected_agent_call_failed = agent_run_event_to_session_events(
        agent_call_failed_event
    )[0]

    assert projected_activation[0] == "agent_run_event"
    assert projected_activation[1]["phase"] == "agent_run_activation_queued"
    assert projected_activation[1]["activation_id"] == "run-1:activation:2"
    assert projected_activation[1]["activation"]["input_kind"] == "server_feedback"
    assert "prompt" not in projected_activation[1]["activation"]
    assert "result_payload" not in projected_activation[1]["activation"]
    assert projected_feedback[0] == "agent_run_event"
    assert projected_feedback[1]["phase"] == "agent_run_feedback_added"
    assert projected_feedback[1]["feedback"]["kind"] == "candidate_validation_failed"
    assert "payload" not in projected_feedback[1]["feedback"]
    assert projected_steer[0] == "agent_run_event"
    assert projected_steer[1]["phase"] == "agent_run_activation_steer_queued"
    assert projected_steer[1]["steer"]["status"] == "queued"
    assert "payload" not in projected_steer[1]["steer"]
    assert "metadata" not in projected_steer[1]["steer"]
    assert projected_steer_delivered[1]["phase"] == (
        "agent_run_activation_steer_delivered"
    )
    assert projected_steer_delivered[1]["steer"]["status"] == "delivered"
    assert "payload" not in projected_steer_delivered[1]["steer"]
    assert "metadata" not in projected_steer_delivered[1]["steer"]
    assert projected_agent_call_failed[0] == "agent_run_event"
    assert projected_agent_call_failed[1]["phase"] == "agent_call_failed"
    assert projected_agent_call_failed[1]["agent_call"]["agent_id"] == "Reviewer"
    assert (
        projected_agent_call_failed[1]["agent_call"]["conversation_scope"]
        == "ephemeral"
    )
    assert "request_preview" not in projected_agent_call_failed[1]["agent_call"]


def test_lifecycle_agent_adapter_child_cancel_projects_to_parent_session() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="parent"),
        task_id="parent-run",
    )

    class Agent:
        def __init__(self) -> None:
            self.state = SimpleNamespace(current_round=0)
            self.runtime_task_id = parent.id
            self.agent_run_control_plane = control
            self.runtime_config = SimpleNamespace(
                agent_registry=SimpleNamespace(
                    agents={"reviewer": AgentConfig(id="reviewer")}
                )
            )

        def evaluate_tool_permission(self, tool, *, tool_call=None, action="execute"):  # noqa: ARG002
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

        def _emit_event(self, event) -> None:  # noqa: ARG002
            return None

    declaration = LifecycleHookDeclaration.from_dict(
        "hook:lifecycle-agent-review",
        {
            "event": "Stop",
            "source": "admin_managed",
            "placement": "server",
            "handler_type": "agent",
            "handler_ref": "reviewer",
            "display_name": "Lifecycle reviewer",
            "summary": "Delegates Stop review to a child AgentRun.",
            "permissions": [],
            "trust": "trusted",
            "technical": {"prompt": "review lifecycle output"},
        },
    )
    agent = Agent()
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=default_lifecycle_hook_runtime_adapters(),
    )
    agent.lifecycle_dispatcher = dispatcher
    bind_lifecycle_runtime_adapters_to_agent(agent)

    results = dispatcher.dispatch(
        build_lifecycle_event_context(
            "Stop",
            agent_run_id=parent.id,
            session_run_id="session-1",
            turn_id="turn-1",
            trigger_source="taskflow",
        )
    )
    child = control.list_agent_runs(agent_id="reviewer")[0]

    assert results[0].output.diagnostics[0]["code"] == "agent_run_submitted"
    assert control.cancel_agent_run(parent.id, reason="user_stop") is True

    cancelled_child = control.get_agent_run(child["id"])
    parent_events = [event.to_dict() for event in control.list_events(parent.id)]
    lifecycle_events = [
        event for event in parent_events if event["type"] == "lifecycle_hook"
    ]
    session_lifecycle_events = [
        agent_run_event_to_session_events(event)[0]
        for event in lifecycle_events
    ]

    assert cancelled_child.status == AgentRunStatus.CANCELLED
    assert cancelled_child.cancel_reason == "parent_cancelled:user_stop"
    assert [event["payload"]["event_name"] for event in lifecycle_events] == [
        "TaskCompleted",
        "SubagentStop",
    ]
    assert lifecycle_events[0]["payload"]["payload"]["child_agent_run_id"] == child["id"]
    assert lifecycle_events[0]["payload"]["payload"]["status"] == "cancelled"
    assert lifecycle_events[0]["payload"]["payload"]["lifecycle_hook_id"] == (
        "hook:lifecycle-agent-review"
    )
    assert lifecycle_events[0]["payload"]["payload"]["parent_session_id"] == "session-1"
    assert lifecycle_events[0]["payload"]["payload"]["parent_turn_id"] == "turn-1"
    assert [payload["event_name"] for _, payload in session_lifecycle_events] == [
        "TaskCompleted",
        "SubagentStop",
    ]
    assert session_lifecycle_events[0][1]["child_agent_run_id"] == child["id"]
    assert session_lifecycle_events[0][1]["status"] == "cancelled"
    assert session_lifecycle_events[0][1]["lifecycle_hook_id"] == (
        "hook:lifecycle-agent-review"
    )
    assert session_lifecycle_events[0][1]["parent_session_id"] == "session-1"
    assert session_lifecycle_events[0][1]["parent_turn_id"] == "turn-1"


def test_agent_run_snapshots_agent_model_binding_for_server_origin() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "packager": {
                    "executor": "reuleauxcoder",
                    "execution_location": "remote_server",
                    "worker_kind": "sandbox_worker",
                    "model_request_origin": "server",
                }
            },
            "agents": {
                "capability_packager": {
                    "visibility": "system",
                    "system_flow_only": ["capability_ingest"],
                    "runtime_profile": "packager",
                    "model": {
                        "provider": "deepseek",
                        "model": "deepseek-v4-pro",
                        "parameters": {
                            "max_tokens": 384000,
                            "max_context_tokens": 1000000,
                        },
                    },
                }
            },
        },
    )

    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="capability_packager",
            prompt="package repo",
            source="capability_ingest",
        ),
        task_id="run-packager",
    )
    claim = control.claim_agent_run_activation(
        worker_id="worker-1",
        worker_kind="sandbox_worker",
        executors=["reuleauxcoder"],
        peer_id="peer-1",
        peer_features=["worker_kind:sandbox_worker"],
    )

    assert claim is not None
    assert run.metadata["model_binding"] == {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "parameters": {
            "max_tokens": 384000,
            "max_context_tokens": 1000000,
        },
    }
    assert claim.executor_request.model == "deepseek-v4-pro"
    assert claim.executor_request.metadata["model_binding"]["provider"] == "deepseek"


def test_reuleauxcoder_agent_run_gets_stable_executor_session_before_claim() -> None:
    control = AgentRunControlPlane()

    run = control.submit_agent_run(
        AgentRunRequest(
            agent_id="capability_packager",
            prompt="package repo",
            executor=ExecutorType.REULEAUXCODER,
            source="capability_ingest",
        ),
        task_id="task-reuleaux",
    )
    claim = control.claim_agent_run_activation(
        worker_id="worker-1",
        executors=["reuleauxcoder"],
    )

    assert run.executor_session_id == "labrastro-agent-run-task-reuleaux"
    assert claim is not None
    assert claim.executor_request.executor_session_id == run.executor_session_id


def test_runtime_events_are_limited_and_task_detail_reads_tail() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="events",
        ),
        task_id="task-events",
    )
    for idx in range(5):
        control.append_executor_event(task.id, ExecutorEvent.text_event(f"event-{idx}"))

    first_page = control.list_events(task.id, after_seq=0, limit=3)
    assert [event.seq for event in first_page] == [1, 2, 3]

    waited = control.wait_events(task.id, after_seq=1, timeout_sec=0, limit=2)
    assert [event.seq for event in waited] == [2, 3]

    detail = control.load_agent_run_detail(task.id, event_limit=2)
    assert [event["seq"] for event in detail["events"]] == [6, 7]
    assert [activation["id"] for activation in detail["activations"]] == [
        "task-events:activation:1"
    ]
    assert detail["activations"][0]["status"] == "queued"


def test_activation_steer_queues_mailbox_item_without_feedback_or_new_activation() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="initial task",
            executor=ExecutorType.CODEX,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
        ),
        task_id="task-steer",
    )
    claim = control.claim_agent_run_activation(worker_id="worker-1", executors=["codex"])
    assert claim is not None
    control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
    )

    steer = control.append_activation_steer(
        task.id,
        source="user",
        payload={"items": [{"type": "text", "text": "add this context"}]},
        metadata={
            "client_steer_id": "client-steer-1",
            "idempotency_key": "client-steer-1",
            "sender": "user-1",
        },
        steer_id="steer-1",
    )

    detail = control.load_agent_run_detail(task.id)
    assert steer.activation_id == "task-steer:activation:1"
    assert detail["agent_run"]["id"] == task.id
    assert [activation["id"] for activation in detail["activations"]] == [
        "task-steer:activation:1"
    ]
    assert detail["activation_steers"] == [
        {
            "id": "steer-1",
            "activation_id": "task-steer:activation:1",
            "source": "user",
            "payload": {"items": [{"type": "text", "text": "add this context"}]},
            "created_at": steer.created_at,
            "delivered_at": None,
            "status": "queued",
            "metadata": {
                "client_steer_id": "client-steer-1",
                "idempotency_key": "client-steer-1",
                "sender": "user-1",
            },
        }
    ]
    assert detail["feedback"] == []
    assert [event["type"] for event in detail["events"]][-1:] == [
        "activation_steer_queued",
    ]


def test_activation_steer_requires_active_worker_claim() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="initial task",
            executor=ExecutorType.CODEX,
        ),
        task_id="task-steer-no-claim",
    )
    control.append_executor_event(task.id, ExecutorEvent.status("running"))

    with pytest.raises(ValueError, match="active worker claim"):
        control.append_activation_steer(
            task.id,
            source="user",
            payload={"items": [{"type": "text", "text": "should wait"}]},
            metadata={
                "idempotency_key": "client-no-claim",
                "sender": "user-1",
            },
        )


def test_activation_steer_idempotency_replays_same_payload_and_rejects_conflict() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="initial task",
            executor=ExecutorType.CODEX,
        ),
        task_id="task-steer-idempotent",
    )
    claim = control.claim_agent_run_activation(worker_id="worker-1", executors=["codex"])
    assert claim is not None
    control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
    )

    metadata = {"idempotency_key": "client-repeat", "sender": "user-1"}
    first = control.append_activation_steer(
        task.id,
        source="user",
        payload={"items": [{"type": "text", "text": "same"}]},
        metadata=metadata,
        steer_id="steer-repeat-1",
    )
    replay = control.append_activation_steer(
        task.id,
        source="user",
        payload={"items": [{"type": "text", "text": "same"}]},
        metadata=metadata,
    )

    assert replay.id == first.id
    assert len(control.load_agent_run_detail(task.id)["activation_steers"]) == 1
    with pytest.raises(ValueError, match="activation_steer_idempotency_conflict"):
        control.append_activation_steer(
            task.id,
            source="user",
            payload={"items": [{"type": "text", "text": "different"}]},
            metadata=metadata,
        )


def test_activation_steer_heartbeat_delivers_and_acknowledges_mailbox_item() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="initial task",
            executor=ExecutorType.CODEX,
        ),
        task_id="task-steer-delivery",
    )
    claim = control.claim_agent_run_activation(worker_id="worker-1", executors=["codex"])
    assert claim is not None
    control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
    )
    control.append_activation_steer(
        task.id,
        source="user",
        payload={
            "items": [
                {"type": "text", "text": "add this"},
                {"type": "image_ref", "uri": "asset://image-1"},
            ]
        },
        steer_id="steer-delivery-1",
    )

    delivery = control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
    )

    assert delivery["cancel_requested"] is False
    assert [item["id"] for item in delivery["activation_steers"]] == [
        "steer-delivery-1"
    ]
    assert delivery["activation_steers"][0]["status"] == "delivering"
    assert delivery["activation_steers"][0]["payload"]["items"][1]["uri"] == (
        "asset://image-1"
    )

    ack = control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
        delivered_steer_ids=["steer-delivery-1"],
    )

    detail = control.load_agent_run_detail(task.id)
    assert ack["activation_steers"] == []
    assert detail["activation_steers"][0]["status"] == "delivered"
    assert detail["activation_steers"][0]["delivered_at"] is not None
    assert [event["type"] for event in detail["events"]][-2:] == [
        "activation_steer_delivering",
        "activation_steer_delivered",
    ]


def test_activation_steer_requeues_delivering_item_after_stale_claim_recovery() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="initial task",
            executor=ExecutorType.CODEX,
        ),
        task_id="task-steer-stale",
    )
    claim = control.claim_agent_run_activation(
        worker_id="worker-1",
        executors=["codex"],
        lease_sec=1,
    )
    assert claim is not None
    control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
    )
    control.append_activation_steer(
        task.id,
        source="user",
        payload={"items": [{"type": "text", "text": "recover me"}]},
        metadata={"idempotency_key": "recover-me", "sender": "user-1"},
        steer_id="steer-recover-1",
    )
    delivery = control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
    )
    assert delivery["activation_steers"][0]["status"] == "delivering"

    assert control.recover_stale_agent_runs(now=9999999999) == [task.id]
    recovered = control.load_agent_run_detail(task.id)["activation_steers"][0]
    assert recovered["status"] == "queued"
    assert "delivering_request_id" not in recovered["metadata"]
    next_claim = control.claim_agent_run_activation(
        worker_id="worker-2",
        executors=["codex"],
    )
    assert next_claim is not None
    redelivery = control.heartbeat_agent_run_activation(
        request_id=next_claim.request_id,
        task_id=task.id,
        activation_id=next_claim.activation_id,
        worker_id="worker-2",
    )
    assert [item["id"] for item in redelivery["activation_steers"]] == [
        "steer-recover-1"
    ]


def test_activation_steer_cancel_requested_takes_priority_over_delivery() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="initial task",
            executor=ExecutorType.CODEX,
        ),
        task_id="task-steer-cancel-priority",
    )
    claim = control.claim_agent_run_activation(worker_id="worker-1", executors=["codex"])
    assert claim is not None
    control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
    )
    control.append_activation_steer(
        task.id,
        source="user",
        payload={"items": [{"type": "text", "text": "too late"}]},
        steer_id="steer-cancel-priority-1",
    )
    assert control.cancel_agent_run(task.id, reason="user_stop") is True

    heartbeat = control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
    )

    detail = control.load_agent_run_detail(task.id)
    assert heartbeat["cancel_requested"] is True
    assert heartbeat["activation_steers"] == []
    assert detail["activation_steers"][0]["status"] == "queued"


def test_activation_steer_rejects_explicit_executor_capability_mismatch() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="initial task",
            executor=ExecutorType.CODEX,
            metadata={"activation_steer_supported": False},
        ),
        task_id="task-steer-unsupported",
    )
    claim = control.claim_agent_run_activation(worker_id="worker-1", executors=["codex"])
    assert claim is not None
    control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
    )

    with pytest.raises(ValueError, match="not supported"):
        control.append_activation_steer(
            task.id,
            source="user",
            payload={"items": [{"type": "text", "text": "blocked"}]},
        )


def test_non_required_feedback_does_not_block_successful_agent_run_completion() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(agent_id="coder", prompt="initial task"),
        task_id="task-feedback-not-required",
    )
    control.append_agent_run_feedback(
        task.id,
        source=AgentRunFeedbackSource.SYSTEM,
        kind=AgentRunFeedbackKind.CANDIDATE_READY,
        payload={"candidate_id": "candidate-1"},
        requires_activation=False,
    )

    completed = control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, task.id),
    )

    assert completed.status == AgentRunStatus.COMPLETED
    assert completed.waiting_reason is None
    assert completed.terminal_result == {"output": "done"}


def test_required_feedback_blocks_successful_agent_run_terminal_state() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(agent_id="coder", prompt="initial task"),
        task_id="task-feedback-required",
    )
    feedback = control.append_agent_run_feedback(
        task.id,
        source=AgentRunFeedbackSource.SYSTEM,
        kind=AgentRunFeedbackKind.CANDIDATE_VALIDATION_FAILED,
        payload={"error": "missing required field"},
        requires_activation=True,
    )

    waiting = control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="draft"),
        activation_id=_current_activation_id(control, task.id),
    )

    assert waiting.status == AgentRunStatus.WAITING
    assert waiting.waiting_reason == AgentRunWaitingReason.SERVER_PROCESSING
    assert waiting.resume_policy == AgentRunResumePolicy.EXTERNAL_EVENT
    assert waiting.terminal_result == {}
    detail = control.load_agent_run_detail(task.id)
    assert detail["feedback"][0]["id"] == feedback.id
    assert detail["feedback"][0]["requires_activation"] is True
    assert detail["events"][-1]["type"] == "waiting"


def test_waiting_activation_completion_keeps_sandbox_session_alive() -> None:
    sandbox = _FakeSandboxProvider()
    control = AgentRunControlPlane(sandbox_provider=sandbox)
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="initial task",
            executor_session_id="executor-feedback",
            sandbox_session_id="sandbox-feedback",
        ),
        task_id="task-feedback-sandbox",
    )
    control.append_agent_run_feedback(
        task.id,
        source=AgentRunFeedbackSource.SYSTEM,
        kind=AgentRunFeedbackKind.CANDIDATE_VALIDATION_FAILED,
        payload={"error": "missing required field"},
        requires_activation=True,
    )

    waiting = control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="draft"),
        activation_id=_current_activation_id(control, task.id),
    )

    assert waiting.status == AgentRunStatus.WAITING
    assert waiting.sandbox_session_id == "sandbox-feedback"
    assert sandbox.stopped_sessions == []


def test_terminal_activation_completion_stops_sandbox_session() -> None:
    sandbox = _FakeSandboxProvider()
    control = AgentRunControlPlane(sandbox_provider=sandbox)
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="initial task",
            sandbox_session_id="sandbox-terminal",
        ),
        task_id="task-terminal-sandbox",
    )

    completed = control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, task.id),
    )

    assert completed.status == AgentRunStatus.COMPLETED
    assert sandbox.stopped_sessions == ["sandbox-terminal"]


@pytest.mark.parametrize("completion_path", ["direct", "claimed"])
def test_store_backed_waiting_activation_completion_keeps_sandbox_session_alive(
    completion_path: str,
) -> None:
    sandbox = _FakeSandboxProvider()
    control = AgentRunControlPlane(
        store=_WaitingActivationCompletionStore(),
        sandbox_provider=sandbox,
    )

    if completion_path == "direct":
        completed = control.complete_agent_run_activation(
            "task-store-waiting",
            ExecutorRunResult(
                task_id="task-store-waiting",
                status="completed",
                output="waiting",
            ),
            activation_id="task-store-waiting:activation:1",
        )
    else:
        ok, reason, completed = control.complete_claimed_agent_run_activation(
            "task-store-waiting",
            ExecutorRunResult(
                task_id="task-store-waiting",
                status="completed",
                output="waiting",
            ),
            request_id="claim-1",
            activation_id="task-store-waiting:activation:1",
            worker_id="worker-1",
        )
        assert ok is True
        assert reason == ""

    assert completed is not None
    assert completed.status == AgentRunStatus.WAITING
    assert sandbox.stopped_sessions == []


def test_activation_steer_rejects_terminal_agent_run() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(agent_id="coder", prompt="initial task"),
        task_id="task-steer-terminal",
    )
    control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, task.id),
    )

    with pytest.raises(ValueError, match="only active AgentRun activations"):
        control.append_activation_steer(
            task.id,
            source="user",
            payload={"message": "too late"},
        )


def test_list_agent_runs_returns_newest_first_like_postgres_store() -> None:
    control = AgentRunControlPlane()
    control.submit_agent_run(
        AgentRunRequest(agent_id="coder", prompt="old"),
        task_id="task-old",
    )
    control.submit_agent_run(
        AgentRunRequest(agent_id="coder", prompt="new"),
        task_id="task-new",
    )

    assert [item["id"] for item in control.list_agent_runs()] == ["task-new", "task-old"]
    assert [item["id"] for item in control.list_agent_runs(limit=1)] == ["task-new"]


def test_postgres_artifact_query_orders_by_insert_sequence() -> None:
    captured_sql: list[str] = []

    class _Rows:
        def mappings(self):
            return []

    class _Conn:
        def execute(self, statement, params):
            del params
            captured_sql.append(str(statement))
            return _Rows()

    class _Begin:
        def __enter__(self):
            return _Conn()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Engine:
        def begin(self):
            return _Begin()

    store = PostgresAgentRunStore.__new__(PostgresAgentRunStore)
    store.engine = _Engine()

    assert store.list_artifacts("task-order") == []
    assert "ORDER BY artifact_seq ASC" in captured_sql[0]
    assert "id ASC" not in captured_sql[0]


def test_artifact_sequence_migration_backfills_in_stable_historical_order() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    migration_path = (
        repo_root
        / "labrastro_server"
        / "infrastructure"
        / "persistence"
        / "migrations"
        / "versions"
        / "0013_agent_run_artifact_sequence.py"
    )

    sql = " ".join(migration_path.read_text(encoding="utf-8").split()).lower()

    assert "row_number() over (order by created_at asc, id asc)" in sql
    assert "where artifact.artifact_seq is null" in sql
    assert "max(artifact_seq)" in sql
    assert "setval(" in sql
    assert "set artifact_seq = nextval" not in sql


def test_failed_agent_run_exposes_terminal_reason_when_output_is_empty() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
        ),
        task_id="task-failed",
    )

    completed = control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(
            task_id=task.id,
            status="failed",
            output="",
            error="real model failure",
        ),
        activation_id=_current_activation_id(control, task.id),
    )

    assert completed.status.value == "failed"
    assert not hasattr(completed, "output")
    assert completed.terminal_result == {"output": ""}
    assert completed.failure_reason == "real model failure"
    detail = control.agent_run_to_dict(task.id)
    assert detail["failure_reason"] == "real model failure"
    assert detail["cancel_reason"] is None


def test_complete_without_session_id_preserves_stream_pinned_session() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            executor="codex",
            execution_location="daemon_worktree",
        ),
        task_id="task-session",
    )
    control.pin_session(
        task.id,
        TaskSessionRef(
            agent_id="coder",
            executor=ExecutorType.CODEX,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
            task_id=task.id,
            workdir="/tmp/work",
            branch="agent/coder/task-session",
            executor_session_id="codex-thread-1",
        ),
    )

    completed = control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, task.id),
    )

    assert completed.executor_session_id == "codex-thread-1"
    assert control.load_agent_run_detail(task.id)["session"]["executor_session_id"] == "codex-thread-1"


def test_followup_task_inherits_parent_session_when_scope_matches() -> None:
    control = AgentRunControlPlane()
    parent = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="parent",
            executor="claude",
            execution_location="daemon_worktree",
            workdir="/tmp/work",
            executor_session_id="claude-session-1",
            metadata={"worktree_branch": "agent/coder/task-parent"},
        ),
        task_id="task-parent",
    )
    control.complete_agent_run_activation(
        parent.id,
        ExecutorRunResult(task_id=parent.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, parent.id),
    )

    followup = control.continue_agent_run(
        parent.id,
        input_kind=AgentRunActivationInputKind.USER_FEEDBACK,
        input_payload={"message": "comment follow up"},
        resume_session=True,
        prompt="follow up",
    )

    assert followup.id == parent.id
    assert followup.executor == parent.executor
    assert followup.runtime_profile_id == parent.runtime_profile_id
    assert followup.workdir == parent.workdir
    assert not hasattr(followup, "branch_name")
    assert not hasattr(followup, "pr_url")
    assert followup.executor_session_id == "claude-session-1"


def test_continue_agent_run_rejects_business_identity_payload_fields() -> None:
    control = AgentRunControlPlane()
    parent = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="parent",
        ),
        task_id="task-business-input",
    )
    control.complete_agent_run_activation(
        parent.id,
        ExecutorRunResult(task_id=parent.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, parent.id),
    )

    with pytest.raises(
        ValueError,
        match="AgentRunActivation.input_payload cannot store taskflow",
    ):
        forbidden_key = "trigger_" + "comment_id"
        control.continue_agent_run(
            parent.id,
            input_kind=AgentRunActivationInputKind.USER_FEEDBACK,
            input_payload={forbidden_key: "comment-1"},
            resume_session=True,
            prompt="follow up",
        )


def test_reuleauxcoder_followup_reuses_parent_executor_session() -> None:
    control = AgentRunControlPlane()
    parent = control.submit_agent_run(
        AgentRunRequest(
            agent_id="capability_packager",
            prompt="draft",
            executor=ExecutorType.REULEAUXCODER,
            execution_location=ExecutionLocation.REMOTE_SERVER,
            workdir="/tmp/capability",
            source="capability_ingest",
            metadata={"worktree_branch": "agent/capability/task-parent"},
        ),
        task_id="task-capability-parent",
    )
    control.complete_agent_run_activation(
        parent.id,
        ExecutorRunResult(task_id=parent.id, status="completed", output="draft"),
        activation_id=_current_activation_id(control, parent.id),
    )

    followup = control.continue_agent_run(
        parent.id,
        input_kind=AgentRunActivationInputKind.SERVER_FEEDBACK,
        input_payload={"kind": "candidate_validation_failed"},
        resume_session=True,
        prompt="revise draft",
    )

    assert parent.executor_session_id == "labrastro-agent-run-task-capability-parent"
    assert followup.id == parent.id
    assert followup.executor_session_id == parent.executor_session_id


def test_continue_agent_run_without_resume_session_clears_executor_session() -> None:
    control = AgentRunControlPlane()
    parent = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="parent",
            executor="claude",
            execution_location="daemon_worktree",
            workdir="/tmp/work",
            executor_session_id="claude-session-1",
            metadata={"worktree_branch": "agent/coder/task-parent"},
        ),
        task_id="task-parent-agent",
    )
    control.complete_agent_run_activation(
        parent.id,
        ExecutorRunResult(task_id=parent.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, parent.id),
    )

    followup = control.continue_agent_run(
        parent.id,
        input_kind=AgentRunActivationInputKind.USER_FEEDBACK,
        input_payload={"message": "continue without session"},
        resume_session=False,
        prompt="follow up",
    )

    assert followup.executor_session_id is None


def test_claim_task_waits_for_wakeup_when_task_is_submitted() -> None:
    control = AgentRunControlPlane()
    claims = []

    def wait_for_claim() -> None:
        claims.append(
            control.claim_agent_run_activation(
                worker_id="worker-wait",
                executors=["fake"],
                wait_sec=2,
            )
        )

    thread = threading.Thread(target=wait_for_claim)
    thread.start()
    time.sleep(0.1)
    control.submit_agent_run(
        AgentRunRequest(
            agent_id="agent",
            prompt="run",
            executor=ExecutorType.FAKE,
        ),
        task_id="task-wakeup",
    )
    thread.join(timeout=2)

    assert claims[0] is not None
    assert claims[0].task.id == "task-wakeup"


def test_environment_runtime_events_are_derived_from_allowlisted_shell_commands() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="environment_configurator",
            prompt="check environment",
            executor=ExecutorType.FAKE,
            trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
            source="environment",
            metadata={
                "workflow": "environment_config",
                "environment_mode": "check",
                "entry_ids": ["envreq:executable:gitnexus"],
                "manifest_hash": "hash",
                "allowed_commands": [
                    {
                        "entry_id": "envreq:executable:gitnexus",
                        "kind": "environment_requirement",
                        "name": "gitnexus",
                        "phase": "check",
                        "command": "gitnexus --version",
                    }
                ],
            },
        ),
        task_id="task-env",
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool": "exec_command",
                "input": {"command": "gitnexus --version"},
            },
        ),
    )
    control.append_executor_event(
        task.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool": "exec_command",
                "input": {"command": "gitnexus --version"},
                "output": {"exit_code": 0, "text": "gitnexus 1.0.0"},
            },
        ),
    )
    control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, task.id),
    )

    events = control.list_events(task.id, after_seq=0)
    event_types = [event.type for event in events]
    assert "environment.entry_started" in event_types
    assert "environment.entry_checked" in event_types
    assert "environment.entry_verified" in event_types
    assert "environment.summary" in event_types


def test_worktree_ready_status_emits_worktree_create_lifecycle_audit() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run in worktree",
            executor=ExecutorType.FAKE,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
            metadata={"worker_kind": "sandbox_worker"},
        ),
        task_id="task-worktree",
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent.status(
            "worktree_ready",
            workdir="/runtime/worktrees/ws/coder-task",
            runtime_root="/runtime",
        ),
    )

    events = [event.to_dict() for event in control.list_events(task.id)]
    lifecycle_events = [
        event for event in events if event["type"] == "lifecycle_hook"
    ]
    assert [event["payload"]["event_name"] for event in lifecycle_events] == [
        "WorktreeCreate"
    ]
    payload = lifecycle_events[0]["payload"]
    assert payload["agent_run_id"] == task.id
    assert payload["trigger_source"] == "manual"
    assert payload["payload"]["workdir"] == "/runtime/worktrees/ws/coder-task"
    assert payload["payload"]["runtime_working_directory"] == (
        "/runtime/worktrees/ws/coder-task"
    )
    assert payload["payload"]["runtime_root"] == "/runtime"
    assert payload["payload"]["execution_location"] == "daemon_worktree"
    assert payload["payload"]["worker_kind"] == "sandbox_worker"
    assert payload["payload"]["path_space"] == "agent_run_worktree"
    session_events = agent_run_event_to_session_events(lifecycle_events[0])
    assert session_events[0][0] == "lifecycle_hook"
    assert session_events[0][1]["event_name"] == "WorktreeCreate"


def test_worktree_removed_status_emits_worktree_remove_lifecycle_audit() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="cleanup worktree",
            executor=ExecutorType.FAKE,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
            metadata={"worker_kind": "sandbox_worker"},
        ),
        task_id="task-worktree-remove",
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent.status(
            "worktree_removed",
            workdir="/runtime/worktrees/ws/coder-task",
            runtime_root="/runtime",
        ),
    )

    events = [event.to_dict() for event in control.list_events(task.id)]
    lifecycle_events = [
        event for event in events if event["type"] == "lifecycle_hook"
    ]
    assert [event["payload"]["event_name"] for event in lifecycle_events] == [
        "WorktreeRemove"
    ]
    payload = lifecycle_events[0]["payload"]
    assert payload["agent_run_id"] == task.id
    assert payload["trigger_source"] == "manual"
    assert payload["payload"]["workdir"] == "/runtime/worktrees/ws/coder-task"
    assert payload["payload"]["runtime_working_directory"] == (
        "/runtime/worktrees/ws/coder-task"
    )
    assert payload["payload"]["runtime_root"] == "/runtime"
    assert payload["payload"]["execution_location"] == "daemon_worktree"
    assert payload["payload"]["worker_kind"] == "sandbox_worker"
    assert payload["payload"]["path_space"] == "agent_run_worktree"
    session_events = agent_run_event_to_session_events(lifecycle_events[0])
    assert session_events[0][0] == "lifecycle_hook"
    assert session_events[0][1]["event_name"] == "WorktreeRemove"


def test_permission_request_terminal_gate_projects_final_permission_audit() -> None:
    projected = agent_run_event_to_session_events(
        {
            "agent_run_id": "agent-run-1",
            "seq": 7,
            "type": "tool_result",
            "payload": {
                "type": "tool_result",
                "text": None,
                "data": {
                    "tool_name": "shell",
                    "tool_call_id": "call-1",
                    "output": "Permission denied",
                    "meta": {
                        "tool_diagnostics": [
                            {
                                "code": "permission_denied",
                                "severity": "error",
                                "metadata": {
                                    "permission": {
                                        "action": "deny",
                                        "authorized": False,
                                        "policy_matched": "lifecycle_hook:deny",
                                        "reason": (
                                            "PermissionRequest lifecycle denied shell."
                                        ),
                                        "audit": {
                                            "lifecycle_event": "PermissionRequest",
                                            "lifecycle_hooks": [
                                                {
                                                    "hook_id": (
                                                        "hook:admin:shell-permission:"
                                                        "PermissionRequest:0"
                                                    ),
                                                    "display_name": (
                                                        "Shell permission guard"
                                                    ),
                                                    "decision": "deny",
                                                    "reason": (
                                                        "Blocks shell in this "
                                                        "workspace."
                                                    ),
                                                }
                                            ],
                                            "pre_tool_lifecycle": {
                                                "hook_id": (
                                                    "hook:skill:pretool:"
                                                    "PreToolUse:0"
                                                ),
                                                "display_name": "PreTool allow observer",
                                                "decision": "allow",
                                            },
                                            "technical": {
                                                "raw_command": "rm -rf private-data"
                                            },
                                        },
                                    }
                                },
                            }
                        ]
                    },
                },
            },
        }
    )

    assert projected[0][0] == "tool_call_end"
    permission = projected[0][1]["meta"]["permission"]
    assert permission == {
        "action": "deny",
        "authorized": False,
        "policy_matched": "lifecycle_hook:deny",
        "reason": "PermissionRequest lifecycle denied shell.",
        "lifecycle_event": "PermissionRequest",
        "lifecycle_hooks": [
            {
                "hook_id": "hook:admin:shell-permission:PermissionRequest:0",
                "display_name": "Shell permission guard",
                "decision": "deny",
                "reason": "Blocks shell in this workspace.",
            }
        ],
    }
    assert "PreToolUse" not in str(permission)
    assert "raw_command" not in str(permission)
    assert "private-data" not in str(permission)


def test_tool_projection_preserves_canonical_tool_spec_metadata() -> None:
    projected_start = agent_run_event_to_session_events(
        {
            "agent_run_id": "agent-run-1",
            "seq": 8,
            "type": "tool_use",
            "payload": {
                "type": "tool_use",
                "data": {
                    "tool_name": "tool_search",
                    "tool_call_id": "search-1",
                    "input": {"query": "docs"},
                    "tool_id": "builtin:tool_search",
                    "risk": "read_only",
                    "exposure": "direct",
                },
            },
        }
    )
    projected_end = agent_run_event_to_session_events(
        {
            "agent_run_id": "agent-run-1",
            "seq": 9,
            "type": "tool_result",
            "payload": {
                "type": "tool_result",
                "data": {
                    "tool_name": "tool_search",
                    "tool_call_id": "search-1",
                    "output": "{\"results\":[]}",
                    "tool_id": "builtin:tool_search",
                    "risk": "read_only",
                    "exposure": "direct",
                    "meta": {
                        "search_trace": {
                            "query": "docs",
                            "result_count": 1,
                            "tool_ids": ["capability:docs:lookup"],
                        },
                    },
                },
            },
        }
    )

    assert projected_start[0][0] == "tool_call_start"
    assert projected_start[0][1] == {
        "agent_run_id": "agent-run-1",
        "agent_id": "agent",
        "workflow": "agent_run",
        "raw_event_refs": [
            {"agent_run_id": "agent-run-1", "seq": 8, "type": "tool_use"}
        ],
        "tool_name": "tool_search",
        "tool_call_id": "search-1",
        "tool_args": {"query": "docs"},
        "tool_id": "builtin:tool_search",
        "risk": "read_only",
        "exposure": "direct",
    }
    assert projected_end[0][0] == "tool_call_end"
    assert projected_end[0][1]["tool_id"] == "builtin:tool_search"
    assert projected_end[0][1]["risk"] == "read_only"
    assert projected_end[0][1]["exposure"] == "direct"
    assert projected_end[0][1]["meta"]["search_trace"] == {
        "query": "docs",
        "result_count": 1,
        "tool_ids": ["capability:docs:lookup"],
    }


def test_session_projection_preserves_capability_target_context() -> None:
    capability_target = {
        "gateway_tool_name": "capability_execute",
        "parent_tool_call_id": "exec-target",
        "target_tool_call_id": "exec-target:capability:docs:lookup",
        "target_tool_id": "capability:docs:lookup",
        "target_tool_name": "docs_lookup",
        "target_arguments": {"query": "cache"},
        "target_exposure": "deferred",
        "target_risk": "read_only",
        "target_permission_policy": "read_only",
    }
    projected_start = agent_run_event_to_session_events(
        {
            "agent_run_id": "agent-run-1",
            "seq": 10,
            "type": "tool_use",
            "payload": {
                "type": "tool_use",
                "data": {
                    "tool_name": "docs_lookup",
                    "tool_call_id": "exec-target:capability:docs:lookup",
                    "input": {"query": "cache"},
                    "tool_id": "capability:docs:lookup",
                    "risk": "read_only",
                    "exposure": "deferred",
                    "capability_target": capability_target,
                },
            },
        }
    )
    projected_end = agent_run_event_to_session_events(
        {
            "agent_run_id": "agent-run-1",
            "seq": 11,
            "type": "tool_result",
            "payload": {
                "type": "tool_result",
                "data": {
                    "tool_name": "docs_lookup",
                    "tool_call_id": "exec-target:capability:docs:lookup",
                    "output": "docs_lookup:cache",
                    "tool_id": "capability:docs:lookup",
                    "risk": "read_only",
                    "exposure": "deferred",
                    "capability_target": capability_target,
                    "meta": {"tool_diagnostics": []},
                },
            },
        }
    )

    start_payload = projected_start[0][1]
    assert start_payload["capability_target"] == capability_target
    assert start_payload["target_tool_id"] == "capability:docs:lookup"
    assert start_payload["target_tool_name"] == "docs_lookup"
    assert start_payload["parent_tool_call_id"] == "exec-target"
    assert start_payload["raw_event_refs"] == [
        {"agent_run_id": "agent-run-1", "seq": 10, "type": "tool_use"}
    ]
    end_payload = projected_end[0][1]
    assert end_payload["capability_target"] == capability_target
    assert end_payload["target_tool_id"] == "capability:docs:lookup"
    assert end_payload["target_tool_name"] == "docs_lookup"
    assert end_payload["parent_tool_call_id"] == "exec-target"
    assert end_payload["meta"]["capability_target"] == capability_target
    assert end_payload["raw_event_refs"] == [
        {"agent_run_id": "agent-run-1", "seq": 11, "type": "tool_result"}
    ]


def test_session_projection_truncates_capability_target_arguments() -> None:
    long_patch = "*** Begin Patch\n" + ("+line\n" * 1200) + "*** End Patch"
    capability_target = {
        "gateway_tool_name": "capability_execute",
        "parent_tool_call_id": "exec-target",
        "target_tool_call_id": "exec-target:capability:docs:workspace_patch",
        "target_tool_id": "capability:docs:workspace_patch",
        "target_tool_name": "apply_patch",
        "target_arguments": {"patch": long_patch},
        "target_exposure": "deferred",
        "target_risk": "file_mutation",
        "target_permission_policy": "workspace_write",
    }

    projected = agent_run_event_to_session_events(
        {
            "agent_run_id": "agent-run-1",
            "seq": 12,
            "type": "tool_use",
            "payload": {
                "type": "tool_use",
                "data": {
                    "tool_name": "apply_patch",
                    "tool_call_id": "exec-target:capability:docs:workspace_patch",
                    "input": {"patch": long_patch},
                    "tool_id": "capability:docs:workspace_patch",
                    "risk": "file_mutation",
                    "exposure": "deferred",
                    "capability_target": capability_target,
                },
            },
        }
    )

    payload = projected[0][1]
    public_target = payload["capability_target"]
    public_patch = public_target["target_arguments"]["patch"]
    assert payload["target_tool_id"] == "capability:docs:workspace_patch"
    assert payload["target_tool_name"] == "apply_patch"
    assert payload["target_tool_call_id"] == (
        "exec-target:capability:docs:workspace_patch"
    )
    assert public_patch != long_patch
    assert "output omitted from the main timeline" in public_patch
    assert "target_arguments.patch" in public_target["truncated_fields"]
    assert public_target["full_payload_source"] == "raw_event"


def test_session_projection_uses_target_identity_for_deferred_tool_events() -> None:
    capability_target = {
        "gateway_tool_name": "capability_execute",
        "parent_tool_call_id": "exec-target",
        "target_tool_call_id": "exec-target:capability:docs:lookup",
        "target_tool_id": "capability:docs:lookup",
        "target_tool_name": "docs_lookup",
        "target_arguments": {"query": "cache"},
        "target_exposure": "deferred",
        "target_risk": "read_only",
        "target_permission_policy": "read_only",
    }

    projected = agent_run_event_to_session_events(
        {
            "agent_run_id": "agent-run-1",
            "seq": 12,
            "type": "tool_use",
            "payload": {
                "type": "tool_use",
                "data": {
                    "tool_name": "capability_execute",
                    "tool_call_id": "exec-target:capability:docs:lookup",
                    "input": {"query": "cache"},
                    "tool_id": "builtin:capability_execute",
                    "risk": "capability",
                    "exposure": "direct",
                    "capability_target": capability_target,
                },
            },
        }
    )

    payload = projected[0][1]
    assert payload["tool_name"] == "docs_lookup"
    assert payload["tool_id"] == "capability:docs:lookup"
    assert payload["risk"] == "read_only"
    assert payload["exposure"] == "deferred"
    assert payload["capability_target"]["gateway_tool_name"] == "capability_execute"


def test_session_projection_keeps_capability_execute_as_gateway_detail_only() -> None:
    projected = agent_run_event_to_session_events(
        {
            "agent_run_id": "agent-run-1",
            "seq": 13,
            "type": "tool_result",
            "payload": {
                "type": "tool_result",
                "data": {
                    "tool_name": "capability_execute",
                    "tool_call_id": "exec-target",
                    "output": "docs_lookup:cache",
                    "tool_id": "builtin:capability_execute",
                    "risk": "capability",
                    "exposure": "direct",
                    "meta": {
                        "execute_trace": {
                            "gateway_tool_name": "capability_execute",
                            "parent_tool_call_id": "exec-target",
                            "target_tool_call_id": "exec-target:capability:docs:lookup",
                            "tool_id": "capability:docs:lookup",
                            "target_tool_id": "capability:docs:lookup",
                            "target_tool_name": "docs_lookup",
                            "target_exposure": "deferred",
                            "target_risk": "read_only",
                            "target_permission_policy": "read_only",
                        }
                    },
                },
            },
        }
    )

    payload = projected[0][1]
    assert payload["tool_name"] == "capability_execute"
    assert payload["tool_id"] == "builtin:capability_execute"
    assert payload["risk"] == "capability"
    assert payload["exposure"] == "direct"
    assert payload["meta"]["execute_trace"]["target_tool_id"] == "capability:docs:lookup"
    assert "capability_target" not in payload


def test_environment_runtime_blocks_non_manifest_shell_command() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="environment_configurator",
            prompt="check environment",
            executor=ExecutorType.FAKE,
            trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
            source="environment",
            metadata={
                "workflow": "environment_config",
                "environment_mode": "check",
                "entry_ids": ["envreq:executable:gitnexus"],
                "manifest_hash": "hash",
                "allowed_commands": [
                    {
                        "entry_id": "envreq:executable:gitnexus",
                        "kind": "environment_requirement",
                        "name": "gitnexus",
                        "phase": "check",
                        "command": "gitnexus --version",
                    }
                ],
            },
        ),
        task_id="task-env-blocked",
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent(
            type="tool_use",
            data={"tool": "exec_command", "input": {"command": "npm install -g x"}},
        ),
    )
    completed = control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, task.id),
    )

    assert completed.status.value == "blocked"
    events = control.list_events(task.id, after_seq=0)
    assert "environment.entry_failed" in [event.type for event in events]


def test_environment_runtime_reports_failed_install_command() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="environment_configurator",
            prompt="configure environment",
            executor=ExecutorType.FAKE,
            trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
            source="environment",
            metadata={
                "workflow": "environment_config",
                "environment_mode": "configure",
                "entry_ids": ["envreq:executable:gitnexus"],
                "manifest_hash": "hash",
                "allowed_commands": [
                    {
                        "entry_id": "envreq:executable:gitnexus",
                        "kind": "environment_requirement",
                        "name": "gitnexus",
                        "phase": "install",
                        "command": "npm install -g gitnexus",
                    }
                ],
            },
        ),
        task_id="task-env-install-failed",
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool": "exec_command",
                "input": {"command": "npm install -g gitnexus"},
                "output": {"exit_code": 1, "text": "install failed"},
            },
        ),
    )

    failed_events = [
        event
        for event in control.list_events(task.id, after_seq=0)
        if event.type == "environment.entry_failed"
    ]
    assert failed_events
    assert failed_events[-1].payload["phase"] == "install"


def test_claim_includes_rendered_prompt_files_from_runtime_snapshot() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "codex": {
                    "executor": "codex",
                    "mcp": {"servers": ["filesystem"]},
                    "credential_refs": {"model": "cred-model"},
                }
            },
            "agents": {
                "coder": {
                    "name": "Coder",
                    "runtime_profile": "codex",
                    "dispatch": {"profile": "Best for coding tasks."},
                    "prompt": {
                        "agent_md": "docs/coder.md",
                        "system_append": "Use the repo conventions.",
                    },
                    "credential_refs": {"git": "cred-git"},
                    "effective_capabilities": {
                        "tools": ["builtin:read_file"],
                        "execution_policies": [
                            {
                                "target": "builtin_tool:read_file",
                                "policy": "allow",
                            }
                        ],
                    },
                }
            },
        }
    )
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="fix",
            executor=ExecutorType.CODEX,
            runtime_profile_id="codex",
        ),
        task_id="task-prompt",
    )

    claim = control.claim_agent_run_activation(worker_id="worker-1", executors=["codex"])

    assert claim is not None
    metadata = claim.executor_request.metadata
    assert "AGENTS.md" in metadata["prompt_files"]
    assert "Use the repo conventions." in metadata["prompt_files"]["AGENTS.md"]
    assert metadata["prompt_metadata"]["credential_refs"] == {
        "model": "cred-model",
        "git": "cred-git",
    }
    assert metadata["system_prompt"] == metadata["prompt_files"]["AGENTS.md"]
    assert metadata["permission_context"] == {
        "agent_id": "coder",
        "source": "manual",
        "interactive": False,
        "runtime_profile_id": "codex",
        "effective_capabilities": {
            "tools": ["builtin:read_file"],
            "execution_policies": [
                {
                    "target": "builtin_tool:read_file",
                    "policy": "allow",
                }
            ],
        },
        "resolved_capabilities": {},
    }
    assert control.get_agent_run(task.id).status.value == "dispatched"


def test_runtime_configure_refreshes_snapshot_without_dropping_tasks() -> None:
    control = AgentRunControlPlane()
    existing = control.submit_agent_run(
        AgentRunRequest(agent_id="legacy", prompt="old"),
        task_id="task-existing",
    )

    control.configure(
        max_running_tasks=3,
        runtime_snapshot={
            "runtime_profiles": {
                "fake_profile": {
                    "executor": "fake",
                    "execution_location": "daemon_worktree",
                }
            },
            "agents": {
                "reviewer": {
                    "runtime_profile": "fake_profile",
                    "dispatch": {"profile": "Best for review tasks."},
                }
            },
        },
    )
    control.submit_agent_run(
        AgentRunRequest(agent_id="reviewer", prompt="new"),
        task_id="task-new",
    )

    assert control.max_running_tasks == 3
    assert control.get_agent_run(existing.id).status.value == "queued"
    claim = control.claim_agent_run_activation(worker_id="worker-1", executors=["fake"])

    assert claim is not None
    assert claim.task.id == "task-new"
    assert "fake_profile" in claim.runtime_snapshot["runtime_profiles"]


def test_submit_resolves_agent_run_profile_defaults() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "fake_profile": {
                    "executor": "fake",
                    "execution_location": "daemon_worktree",
                    "model": "smoke-model",
                }
            },
            "agents": {
                "reviewer": {
                    "name": "Reviewer",
                    "runtime_profile": "fake_profile",
                    "dispatch": {"profile": "Best for repository review tasks."},
                    "prompt": {"system_append": "Review carefully."},
                }
            },
        }
    )

    task = control.submit_agent_run(
        AgentRunRequest(agent_id="reviewer", prompt="review"),
        task_id="task-agent-defaults",
    )

    assert task.executor == ExecutorType.FAKE
    assert task.execution_location == ExecutionLocation.DAEMON_WORKTREE
    assert task.runtime_profile_id == "fake_profile"
    assert task.metadata["model"] == "smoke-model"
    assert task.metadata["worker_kind"] == "server_worker"
    assert task.metadata["model_request_origin"] == "server"
    claim = control.claim_agent_run_activation(worker_id="worker-1", executors=["fake"])
    assert claim is not None
    assert claim.executor_request.runtime_profile_id == "fake_profile"
    assert claim.executor_request.executor == ExecutorType.FAKE
    assert claim.executor_request.worker_kind.value == "server_worker"
    assert claim.executor_request.model_request_origin.value == "server"
    assert "AGENT_RUNTIME.md" in claim.executor_request.metadata["prompt_files"]
    assert (
        "Review carefully."
        in claim.executor_request.metadata["prompt_files"]["AGENT_RUNTIME.md"]
    )


def test_submit_explicit_executor_and_profile_override_agent_defaults() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "codex_profile": {
                    "executor": "codex",
                    "execution_location": "daemon_worktree",
                },
                "fake_profile": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "model": "profile-model",
                },
            },
            "agents": {"coder": {"runtime_profile": "codex_profile"}},
        }
    )

    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            runtime_profile_id="fake_profile",
            executor="claude",
            execution_location="local_workspace",
            model="explicit-model",
        ),
        task_id="task-explicit",
    )

    assert task.runtime_profile_id == "fake_profile"
    assert task.executor == ExecutorType.CLAUDE
    assert task.execution_location == ExecutionLocation.LOCAL_WORKSPACE
    assert task.metadata["model"] == "explicit-model"


def test_submit_rejects_missing_agent_run_profile() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={"agents": {"reviewer": {"runtime_profile": "missing"}}}
    )

    with pytest.raises(ValueError, match="runtime profile not found: missing"):
        control.submit_agent_run(
            AgentRunRequest(agent_id="reviewer", prompt="run"),
            task_id="task-missing-profile",
        )


def test_submit_rejects_user_agent_without_runtime_profile() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "server_default": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "server_worker",
                }
            },
            "agents": {
                "reviewer": {
                    "name": "Reviewer",
                    "visibility": "user",
                    "taskflow_eligible": True,
                }
            },
        }
    )

    with pytest.raises(ValueError, match="requires a runtime_profile"):
        control.submit_agent_run(
            AgentRunRequest(agent_id="reviewer", prompt="run"),
            task_id="task-no-profile",
        )


def test_taskflow_rejects_local_only_runtime_profile() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "local_cli": {
                    "executor": "codex",
                    "execution_location": "local_workspace",
                    "worker_kind": "local_peer",
                }
            },
            "agents": {
                "reviewer": {
                    "visibility": "user",
                    "taskflow_eligible": True,
                    "runtime_profile": "local_cli",
                }
            },
        }
    )

    with pytest.raises(ValueError, match="Taskflow agent requires a server-capable runtime profile"):
        control.submit_agent_run(
            AgentRunRequest(
                agent_id="reviewer",
                prompt="run taskflow",
                source="taskflow",
            ),
            task_id="taskflow-local",
        )


def test_local_peer_cannot_claim_remote_server_agent_run() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "server_fake": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "server_worker",
                }
            },
            "agents": {"reviewer": {"runtime_profile": "server_fake"}},
        }
    )
    task = control.submit_agent_run(
        AgentRunRequest(agent_id="reviewer", prompt="run"),
        task_id="task-remote-server",
    )

    assert (
        control.claim_agent_run_activation(
            worker_id="local-peer",
            worker_kind="local_peer",
            executors=["fake"],
            peer_features=["agent_runs", "agent_runs.local_workspace"],
        )
        is None
    )
    claim = control.claim_agent_run_activation(
        worker_id="server-worker",
        worker_kind="server_worker",
        executors=["fake"],
        peer_features=["agent_runs.remote_server"],
    )

    assert claim is not None
    assert claim.task.id == task.id
    assert claim.executor_request.metadata["worker_kind"] == "server_worker"


def test_local_peer_cannot_claim_ordinary_chat_agent_run() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="main_chat",
            prompt="hello",
            owner_session_run_id="session-run-ordinary-chat",
            source=AgentRunSource.CHAT,
            trigger_mode=TriggerMode.INTERACTIVE_CHAT,
            metadata={"session_hint": "chat-session-ordinary"},
        ),
        task_id="task-ordinary-chat",
    )

    assert task.execution_location == ExecutionLocation.REMOTE_SERVER
    assert task.metadata["worker_kind"] == WorkerKind.SERVER_WORKER.value
    assert task.metadata["model_request_origin"] == ModelRequestOrigin.SERVER.value
    assert (
        control.claim_agent_run_activation(
            worker_id="local-peer",
            worker_kind=WorkerKind.LOCAL_PEER,
            executors=["reuleauxcoder"],
            peer_features=[
                "agent_runs",
                "worker_kind:local_peer",
                "agent_runs.local_workspace",
            ],
            workspace_root="D:\\AboutDEV\\vika_mcp",
        )
        is None
    )

    claim = control.claim_agent_run_activation(
        worker_id="server-worker",
        worker_kind=WorkerKind.SERVER_WORKER,
        executors=["reuleauxcoder"],
        peer_features=[
            "agent_runs",
            "worker_kind:server_worker",
            "agent_runs.remote_server",
        ],
    )

    assert claim is not None
    assert claim.task.id == task.id
    assert claim.executor_request.execution_location == ExecutionLocation.REMOTE_SERVER
    assert claim.executor_request.worker_kind == WorkerKind.SERVER_WORKER
    assert claim.executor_request.model_request_origin == ModelRequestOrigin.SERVER


@pytest.mark.parametrize(
    "execution_location",
    [ExecutionLocation.REMOTE_SERVER, ExecutionLocation.DAEMON_WORKTREE],
)
def test_generic_agent_runs_feature_does_not_claim_non_local_agent_run(
    execution_location: ExecutionLocation,
) -> None:
    control = AgentRunControlPlane()
    control.submit_agent_run(
        AgentRunRequest(
            agent_id="reviewer",
            prompt="run",
            executor=ExecutorType.FAKE,
            execution_location=execution_location,
        ),
        task_id=f"task-{execution_location.value}",
    )

    claim = control.claim_agent_run_activation(
        worker_id="generic-local-peer",
        executors=["fake"],
        peer_features=["agent_runs", "agent_runs.local_workspace"],
        workspace_root="G:/repo/main",
    )

    assert claim is None


def test_local_cli_executor_records_local_model_request_origin() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "local_codex": {
                    "executor": "codex",
                    "execution_location": "local_workspace",
                    "worker_kind": "local_peer",
                }
            },
            "agents": {"coder": {"runtime_profile": "local_codex"}},
        }
    )
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run local cli",
            metadata={"workspace_root": "G:/repo/main"},
        ),
        task_id="task-local-cli",
    )
    claim = control.claim_agent_run_activation(
        worker_id="local-peer",
        worker_kind="local_peer",
        executors=["codex"],
        peer_features=["agent_runs.local_workspace"],
        workspace_root="G:/repo/main",
    )

    assert claim is not None
    assert claim.task.id == task.id
    assert task.metadata["model_request_origin"] == "local_cli"
    assert claim.executor_request.metadata["model_request_origin"] == "local_cli"


@pytest.mark.parametrize(
    ("executor", "worker_kind", "model_request_origin", "message"),
    [
        (
            "codex",
            "server_worker",
            "local_cli",
            "codex runtime profile with server_worker must use model_request_origin=server_worker_cli",
        ),
        (
            "codex",
            "local_peer",
            "server_worker_cli",
            "codex runtime profile with local_peer must use model_request_origin=local_cli",
        ),
        (
            "reuleauxcoder",
            "server_worker",
            "server_worker_cli",
            "reuleauxcoder runtime profile must use model_request_origin=server",
        ),
    ],
)
def test_submit_rejects_inconsistent_model_request_origin(
    executor: str,
    worker_kind: str,
    model_request_origin: str,
    message: str,
) -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "profile": {
                    "executor": executor,
                    "execution_location": (
                        "local_workspace"
                        if worker_kind == "local_peer"
                        else "remote_server"
                    ),
                    "worker_kind": worker_kind,
                    "model_request_origin": model_request_origin,
                }
            },
            "agents": {"coder": {"runtime_profile": "profile"}},
        }
    )

    with pytest.raises(ValueError, match=message):
        control.submit_agent_run(
            AgentRunRequest(agent_id="coder", prompt="run"),
            task_id="task-inconsistent-origin",
        )


def test_runtime_slots_limit_server_worker_runs_independently_from_global_limit() -> None:
    control = AgentRunControlPlane(
        max_running_tasks=3,
        runtime_snapshot={
            "runtime_slots": {
                "server_agent_run_slots": 1,
                "server_sandbox_slots": 1,
                "local_peer_agent_run_slots": 1,
                "model_request_slots": 3,
            },
            "runtime_profiles": {
                "server_fake": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "server_worker",
                },
            },
            "agents": {
                "reviewer": {"runtime_profile": "server_fake"},
                "builder": {"runtime_profile": "server_fake"},
            },
        },
    )
    control.submit_agent_run(AgentRunRequest(agent_id="reviewer", prompt="review"))
    control.submit_agent_run(AgentRunRequest(agent_id="builder", prompt="build"))

    first = control.claim_agent_run_activation(
        worker_id="server-1",
        worker_kind="server_worker",
        executors=["fake"],
        peer_features=["worker_kind:server_worker", "agent_runs.remote_server"],
    )
    second = control.claim_agent_run_activation(
        worker_id="server-2",
        worker_kind="server_worker",
        executors=["fake"],
        peer_features=["worker_kind:server_worker", "agent_runs.remote_server"],
    )

    assert first is not None
    assert second is None


def test_runtime_slots_allow_server_and_local_peer_runs_to_progress_separately() -> None:
    control = AgentRunControlPlane(
        max_running_tasks=1,
        runtime_snapshot={
            "runtime_slots": {
                "server_agent_run_slots": 1,
                "server_sandbox_slots": 1,
                "local_peer_agent_run_slots": 1,
                "model_request_slots": 3,
            },
            "runtime_profiles": {
                "server_fake": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "server_worker",
                },
                "local_fake": {
                    "executor": "fake",
                    "execution_location": "local_workspace",
                    "worker_kind": "local_peer",
                },
            },
            "agents": {
                "remote_agent": {"runtime_profile": "server_fake"},
                "local_agent": {
                    "runtime_profile": "local_fake",
                    "taskflow_eligible": False,
                },
            },
        },
    )
    control.submit_agent_run(AgentRunRequest(agent_id="remote_agent", prompt="remote"))
    control.submit_agent_run(AgentRunRequest(agent_id="local_agent", prompt="local"))

    server_claim = control.claim_agent_run_activation(
        worker_id="server-1",
        worker_kind="server_worker",
        executors=["fake"],
        peer_features=["worker_kind:server_worker", "agent_runs.remote_server"],
    )
    local_claim = control.claim_agent_run_activation(
        worker_id="local-1",
        worker_kind="local_peer",
        executors=["fake"],
        peer_features=["worker_kind:local_peer", "agent_runs.local_workspace"],
    )

    assert server_claim is not None
    assert local_claim is not None


def test_environment_agent_run_uses_local_peer_slot() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "environment_local": {
                    "executor": "fake",
                    "execution_location": "local_workspace",
                    "worker_kind": "local_peer",
                },
            },
            "agents": {
                "environment_configurator": {
                    "visibility": "system",
                    "system_flow_only": ["environment_config"],
                    "runtime_profile": "environment_local",
                    "taskflow_eligible": False,
                },
            },
        },
    )
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="environment_configurator",
            prompt="check environment",
            source="environment",
        ),
        task_id="task-environment-slot",
    )

    assert runtime_slot_key_for_agent_run(task) == "local_peer_agent_run_slots"


def test_default_system_agent_runs_carry_effective_capability_boundaries() -> None:
    config = ConfigLoader()._parse_config(_model_config())
    snapshot = build_agent_run_snapshot(
        agent_registry=config.agent_registry,
        runtime_profiles=config.runtime_profiles,
        run_limits=config.run_limits,
        capability_packages=config.capability_packages,
        capability_components=config.capability_components,
    )
    control = AgentRunControlPlane(runtime_snapshot=snapshot)

    environment_task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="environment_configurator",
            prompt="check",
            source="environment",
        ),
        task_id="task-default-environment",
    )
    packager_task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="capability_packager",
            prompt="draft",
            source="capability_ingest",
        ),
        task_id="task-default-packager",
    )

    assert "tools" not in environment_task.metadata["effective_capabilities"]
    assert "tools" not in packager_task.metadata["effective_capabilities"]
    assert environment_task.metadata["effective_capabilities"][
        "builtin_tool_grants"
    ] == ["shell"]
    assert packager_task.metadata["effective_capabilities"][
        "builtin_tool_grants"
    ] == ["fetch_capabilities", "glob", "grep", "list_file", "read_file"]
    assert [
        item["target_tool_ref"]
        for item in packager_task.metadata["effective_capabilities"]["tool_specs"]
    ] == [
        "builtin:fetch_capabilities",
        "builtin:glob",
        "builtin:grep",
        "builtin:list_file",
        "builtin:read_file",
    ]
    assert packager_task.metadata["worker_kind"] == "sandbox_worker"


def test_sandbox_worker_claims_only_sandbox_managed_runs() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "sandbox_fake": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "sandbox_worker",
                },
            },
            "agents": {"sandbox_agent": {"runtime_profile": "sandbox_fake"}},
        },
    )
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="sandbox_agent",
            prompt="sandbox",
        )
    )

    server_claim = control.claim_agent_run_activation(
        worker_id="server-1",
        worker_kind="server_worker",
        executors=["fake"],
        peer_features=["worker_kind:server_worker", "agent_runs.remote_server"],
    )
    sandbox_claim = control.claim_agent_run_activation(
        worker_id="sandbox-1",
        worker_kind="sandbox_worker",
        executors=["fake"],
        peer_features=["worker_kind:sandbox_worker", "sandbox_worker"],
    )

    assert server_claim is None
    assert sandbox_claim is not None
    assert sandbox_claim.task.id == task.id


def test_waiting_approval_event_updates_task_status() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(agent_id="coder", prompt="run shell"),
        task_id="task-approval",
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent.status(
            "waiting_approval",
            approval_id="approval-1",
            tool_name="shell",
        ),
    )

    updated = control.get_agent_run(task.id)
    assert updated.status == AgentRunStatus.WAITING
    assert updated.waiting_reason.value == "user_approval"


def test_waiting_approval_status_projects_to_session_approval_request() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(agent_id="coder", prompt="run shell"),
        task_id="task-approval-projection",
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent.status(
            "waiting_approval",
            approval_id="approval-1",
            tool_name="shell",
            tool_call_id="call-1",
            reason="PermissionRequest lifecycle asked for shell review.",
            intent="Review shell command",
            tool_args={"command": "npm test"},
            permission={
                "action": "require_approval",
                "authorized": True,
                "policy_matched": "lifecycle_hook:ask",
                "reason": "PermissionRequest lifecycle asked for shell review.",
            },
        ),
    )

    status_event = [
        event.to_dict()
        for event in control.list_events(task.id, after_seq=0)
        if event.type == "status"
    ][0]
    projected = agent_run_event_to_session_events(status_event)

    assert [event_type for event_type, _ in projected] == [
        "agent_run_event",
        "approval_request",
    ]
    assert projected[1][1] == {
        "agent_run_id": task.id,
        "agent_id": "agent",
        "workflow": "agent_run",
        "raw_event_refs": [
            {
                "agent_run_id": task.id,
                "seq": status_event["seq"],
                "type": "status",
            }
        ],
        "approval_id": "approval-1",
        "tool_name": "shell",
        "tool_call_id": "call-1",
        "reason": "PermissionRequest lifecycle asked for shell review.",
        "intent": "Review shell command",
        "tool_args": {"command": "npm test"},
        "permission": {
            "action": "require_approval",
            "authorized": True,
            "policy_matched": "lifecycle_hook:ask",
            "reason": "PermissionRequest lifecycle asked for shell review.",
        },
    }


def test_taskflow_waiting_approval_event_becomes_blocked_review() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "server_fake": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "server_worker",
                }
            },
            "agents": {
                "worker": {
                    "runtime_profile": "server_fake",
                    "taskflow_eligible": True,
                }
            },
        }
    )
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="worker",
            prompt="run background",
            source="taskflow",
        ),
        task_id="taskflow-approval",
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent.status(
            "waiting_approval",
            approval_id="approval-1",
            tool_name="shell",
            reason="shell requires approval",
        ),
    )

    assert control.get_agent_run(task.id).status.value == "blocked"
    events = control.list_events(task.id, after_seq=0)
    blocked = [event for event in events if event.type == "permission.blocked_review"]
    assert blocked
    assert blocked[-1].payload["permission"]["action"] == "blocked_review"
    assert blocked[-1].payload["tool_name"] == "shell"


def test_background_permission_blocked_review_projects_to_session_decision() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "server_fake": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "server_worker",
                }
            },
            "agents": {
                "worker": {
                    "runtime_profile": "server_fake",
                    "taskflow_eligible": True,
                }
            },
        }
    )
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="worker",
            prompt="run background",
            source="taskflow",
        ),
        task_id="taskflow-review",
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent.status(
            "waiting_approval",
            approval_id="approval-1",
            tool_name="shell",
            reason="PermissionRequest lifecycle asked for shell review.",
        ),
    )

    blocked_event = [
        event.to_dict()
        for event in control.list_events(task.id, after_seq=0)
        if event.type == "permission.blocked_review"
    ][0]
    projected = agent_run_event_to_session_events(blocked_event)

    assert projected[0][0] == "workflow_decision"
    assert projected[0][1] == {
        "agent_run_id": task.id,
        "agent_id": "agent",
        "workflow": "agent_run_permission",
        "raw_event_refs": [
            {
                "agent_run_id": task.id,
                "seq": blocked_event["seq"],
                "type": "permission.blocked_review",
            }
        ],
        "decision_type": "permission_review",
        "status": "pending",
        "title": "Permission review required",
        "summary": "PermissionRequest lifecycle asked for shell review.",
        "approval_id": "approval-1",
        "tool_name": "shell",
        "review": {
            "tool_name": "shell",
            "reason": "PermissionRequest lifecycle asked for shell review.",
            "permission": {
                "action": "blocked_review",
                "authorized": False,
                "reason": "PermissionRequest lifecycle asked for shell review.",
            },
        },
    }


def test_delegation_waiting_approval_event_becomes_blocked_review() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="worker",
            prompt="run delegated task",
            source="delegation",
        ),
        task_id="delegation-approval",
    )

    control.append_executor_event(
        task.id,
        ExecutorEvent.status(
            "waiting_approval",
            approval_id="approval-1",
            tool_name="shell",
            reason="shell requires approval",
        ),
    )

    assert control.get_agent_run(task.id).status.value == "blocked"
    events = control.list_events(task.id, after_seq=0)
    blocked = [event for event in events if event.type == "permission.blocked_review"]
    assert blocked
    assert blocked[-1].payload["permission"]["action"] == "blocked_review"
    assert blocked[-1].payload["tool_name"] == "shell"


def test_claim_filters_by_workspace_and_execution_location() -> None:
    control = AgentRunControlPlane()
    local_task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="fix local",
            executor=ExecutorType.CODEX,
            execution_location=ExecutionLocation.LOCAL_WORKSPACE,
            metadata={"workspace_root": "G:/repo/main"},
        ),
        task_id="task-local",
    )

    assert (
        control.claim_agent_run_activation(
            worker_id="worker-shell",
            executors=["codex"],
            peer_features=["shell"],
            workspace_root="G:/repo/main",
        )
        is None
    )
    assert (
        control.claim_agent_run_activation(
            worker_id="worker-no-workspace",
            executors=["codex"],
            peer_features=["agent_runs", "agent_runs.local_workspace"],
        )
        is None
    )
    assert (
        control.claim_agent_run_activation(
            worker_id="worker-other",
            executors=["codex"],
            peer_features=["agent_runs", "agent_runs.local_workspace"],
            workspace_root="G:/repo/other",
        )
        is None
    )
    claim = control.claim_agent_run_activation(
        worker_id="worker-local",
        executors=["codex"],
        peer_features=["agent_runs", "agent_runs.local_workspace"],
        workspace_root="G:\\repo\\main",
    )

    assert claim is not None
    assert claim.task.id == local_task.id

    remote_task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="fix remote",
            executor=ExecutorType.CLAUDE,
            execution_location=ExecutionLocation.REMOTE_SERVER,
        ),
        task_id="task-remote",
    )
    remote_claim = control.claim_agent_run_activation(
        worker_id="worker-remote",
        executors=["claude"],
        peer_features=["agent_runs.remote_server"],
    )

    assert remote_claim is not None
    assert remote_claim.task.id == remote_task.id


def test_heartbeat_cancel_and_stale_recovery() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
            execution_location=ExecutionLocation.LOCAL_WORKSPACE,
            worker_kind=WorkerKind.LOCAL_PEER,
        ),
        task_id="task-heartbeat",
    )
    claim = control.claim_agent_run_activation(
        worker_id="worker-1",
        worker_kind="local_peer",
        executors=["fake"],
        peer_id="peer-1",
        peer_features=["agent_runs.local_workspace"],
        lease_sec=1,
    )

    assert claim is not None
    heartbeat = control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
        peer_id="peer-1",
        lease_sec=5,
    )
    assert heartbeat["ok"] is True
    assert heartbeat["activation_id"] == claim.activation_id
    assert heartbeat["cancel_requested"] is False
    assert control.get_agent_run(task.id).status.value == "running"

    assert control.cancel_agent_run(task.id, reason="stop") is True
    heartbeat = control.heartbeat_agent_run_activation(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    assert heartbeat["cancel_requested"] is True
    assert heartbeat["reason"] == "stop"

    completed = control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(
            task_id=task.id,
            status="cancelled",
            output="",
            error="execution cancelled",
        ),
        activation_id=_current_activation_id(control, task.id),
    )
    assert completed.status.value == "cancelled"
    assert completed.cancel_reason == "stop"
    assert control.agent_run_to_dict(task.id)["cancel_reason"] == "stop"

    missing = control.heartbeat_agent_run_activation(
        request_id="missing-claim",
        task_id="missing-task",
        activation_id=claim.activation_id,
        worker_id="worker-1",
    )
    assert missing["ok"] is False
    assert missing["cancel_requested"] is True
    assert missing["reason"] == "agent_run_not_found"

    stale_task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="stale",
            executor=ExecutorType.FAKE,
            execution_location=ExecutionLocation.LOCAL_WORKSPACE,
            worker_kind=WorkerKind.LOCAL_PEER,
        ),
        task_id="task-stale",
    )
    stale_claim = control.claim_agent_run_activation(
        worker_id="worker-2",
        worker_kind="local_peer",
        executors=["fake"],
        peer_id="peer-2",
        peer_features=["agent_runs.local_workspace"],
        lease_sec=1,
    )

    assert stale_claim is not None
    recovered = control.recover_stale_agent_runs(now=9999999999)
    assert recovered == [stale_task.id]
    assert control.get_agent_run(stale_task.id).status.value == "queued"
    assert any(event.type == "lease_expired" for event in control.list_events(stale_task.id))


def test_claim_owner_validates_session_event_and_complete() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
            execution_location=ExecutionLocation.LOCAL_WORKSPACE,
            worker_kind=WorkerKind.LOCAL_PEER,
        ),
        task_id="task-owner",
    )
    claim = control.claim_agent_run_activation(
        worker_id="worker-1",
        worker_kind="local_peer",
        executors=["fake"],
        peer_id="peer-1",
        peer_features=["agent_runs.local_workspace"],
    )

    assert claim is not None
    assert claim.activation_id == "task-owner:activation:1"
    assert claim.to_dict()["activation"]["activation_id"] == claim.activation_id
    ok, reason = control.pin_claimed_activation_session(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="other-worker",
        peer_id="peer-1",
        workdir="/tmp/work",
    )
    assert ok is False
    assert reason == "worker_mismatch"

    ok, reason = control.pin_claimed_activation_session(
        request_id=claim.request_id,
        task_id=task.id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
        peer_id="peer-1",
        workdir="/tmp/work",
        branch="agent/coder/task-owner",
    )
    assert ok is True
    assert reason == ""
    assert control.get_agent_run(task.id).workdir == "/tmp/work"
    session_events = [
        event
        for event in control.list_events(task.id)
        if event.type == "session_metadata"
    ]
    assert not session_events

    ok, reason = control.append_executor_event(
        task.id,
        ExecutorEvent.status("running"),
        request_id=claim.request_id,
        activation_id=claim.activation_id,
        worker_id="other-worker",
        peer_id="peer-1",
    )
    assert ok is False
    assert reason == "worker_mismatch"

    ok, reason = control.append_executor_event(
        task.id,
        ExecutorEvent.text_event("hello"),
        request_id=claim.request_id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    assert ok is True
    assert reason == ""
    text_event = [
        event for event in control.list_events(task.id) if event.type == "text"
    ][0]
    assert text_event.payload["activation_id"] == claim.activation_id

    ok, reason, completed = control.complete_claimed_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        request_id=claim.request_id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    assert ok is True
    assert reason == ""
    assert completed is not None
    assert completed.status.value == "completed"
    activation_completed = [
        event
        for event in control.list_events(task.id)
        if event.type == "activation_completed"
    ][0]
    assert activation_completed.payload["activation_id"] == claim.activation_id
    assert activation_completed.payload["activation"]["status"] == "completed"


def test_claimed_executor_process_event_is_visible_before_terminal_completion() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.REULEAUXCODER,
            execution_location=ExecutionLocation.REMOTE_SERVER,
        ),
        task_id="task-live-event",
    )
    claim = control.claim_agent_run_activation(
        worker_id="server-session-run-worker",
        worker_kind="server_worker",
        executors=["reuleauxcoder"],
        peer_id="server",
        peer_features=["agent_runs.remote_server"],
    )

    assert claim is not None
    ok, reason = control.append_executor_event(
        task.id,
        ExecutorEvent.status("running", task_id=task.id),
        request_id=claim.request_id,
        activation_id=claim.activation_id,
        worker_id=claim.worker_id,
        peer_id="server",
    )
    assert ok is True
    assert reason == ""
    ok, reason = control.append_executor_event(
        task.id,
        ExecutorEvent.session_run_event("reasoning_delta", {"content": "live"}),
        request_id=claim.request_id,
        activation_id=claim.activation_id,
        worker_id=claim.worker_id,
        peer_id="server",
    )

    assert ok is True
    assert reason == ""
    events = control.list_events(task.id)
    event_types = [event.type for event in events]
    assert "session_run_event" in event_types
    assert not {"activation_completed", "completed", "failed", "cancelled"} & set(
        event_types
    )
    live_event = [
        event for event in events if event.type == "session_run_event"
    ][0]
    assert live_event.payload["activation_id"] == claim.activation_id
    assert live_event.payload["data"] == {
        "event_type": "reasoning_delta",
        "payload": {"content": "live"},
    }
    assert control.get_agent_run(task.id).status == AgentRunStatus.RUNNING


def test_blocked_complete_and_retry_terminal_task() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
            metadata={"repo_url": "file:///repo"},
        ),
        task_id="task-blocked",
    )
    claim = control.claim_agent_run_activation(
        worker_id="worker-1",
        worker_kind="server_worker",
        executors=["fake"],
        peer_id="peer-1",
        peer_features=["agent_runs.daemon_worktree"],
    )

    assert claim is not None
    ok, reason, blocked = control.complete_claimed_agent_run_activation(
        task.id,
        ExecutorRunResult(
            task_id=task.id,
            status="blocked",
            output="",
            error="repo_url missing",
        ),
        request_id=claim.request_id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )

    assert ok is True
    assert reason == ""
    assert blocked is not None
    assert blocked.status.value == "blocked"

    blocked.executor_session_id = "fake-session-1"

    retry = control.retry_agent_run(task.id)

    assert retry.id == task.id
    assert retry.status.value == "queued"
    detail = control.load_agent_run_detail(task.id)
    assert detail["agent_run"]["current_activation_id"] == "task-blocked:activation:2"
    assert detail["activations"][1]["seq"] == 2
    assert detail["activations"][1]["input_kind"] == "admin_resume"
    assert detail["activations"][1]["input_payload"] == {
        "retry_of_activation_id": "task-blocked:activation:1",
        "resume_session": False,
    }
    public_run = control.agent_run_to_dict(task.id)
    assert public_run["current_activation_id"] == "task-blocked:activation:2"
    assert "current_activation_id" not in public_run["metadata"]
    assert "current_activation_seq" not in public_run["metadata"]
    assert "current_activation_input_kind" not in public_run["metadata"]
    assert "current_activation_input_payload" not in public_run["metadata"]
    assert "current_activation_prompt" not in public_run["metadata"]
    assert retry.metadata["repo_url"] == "file:///repo"
    assert retry.executor_session_id is None

    retry.status = AgentRunStatus.BLOCKED
    retry.executor_session_id = "fake-session-1"
    resumed = control.retry_agent_run(
        task.id,
        resume_session=True,
    )
    assert resumed.id == task.id
    assert resumed.executor_session_id == "fake-session-1"
    detail = control.load_agent_run_detail(task.id)
    assert detail["agent_run"]["current_activation_id"] == "task-blocked:activation:3"
    assert [activation["id"] for activation in detail["activations"]] == [
        "task-blocked:activation:1",
        "task-blocked:activation:2",
        "task-blocked:activation:3",
    ]
    assert [activation["status"] for activation in detail["activations"]] == [
        "blocked",
        "queued",
        "queued",
    ]


def test_complete_task_accepts_branch_pr_and_failed_publish_artifacts() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
        ),
        task_id="task-artifacts",
    )
    claim = control.claim_agent_run_activation(
        worker_id="worker-1",
        worker_kind="server_worker",
        executors=["fake"],
        peer_id="peer-1",
        peer_features=["agent_runs.daemon_worktree"],
    )

    assert claim is not None
    ok, reason, completed = control.complete_claimed_agent_run_activation(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        request_id=claim.request_id,
        activation_id=claim.activation_id,
        worker_id="worker-1",
        peer_id="peer-1",
        artifacts=[
            {
                "type": "branch",
                "status": "pushed",
                "branch_name": "agent/coder/task-artifacts",
            },
            {
                "type": "pull_request",
                "status": "pr_created",
                "branch_name": "agent/coder/task-artifacts",
                "pr_url": "https://example.test/pr/1",
            },
            {
                "type": "log",
                "status": "failed",
                "content": "gh pr create failed",
                "metadata": {"stage": "pr_create"},
            },
        ],
    )

    assert ok is True
    assert reason == ""
    assert completed is not None
    assert completed.status.value == "completed"
    assert not hasattr(completed, "branch_name")
    assert not hasattr(completed, "pr_url")
    artifacts = control.artifacts_to_dict(task.id)
    assert [artifact["type"] for artifact in artifacts] == [
        "branch",
        "pull_request",
        "log",
    ]
    assert artifacts[0]["branch_name"] == "agent/coder/task-artifacts"
    assert artifacts[1]["branch_name"] == "agent/coder/task-artifacts"
    assert artifacts[1]["pr_url"] == "https://example.test/pr/1"
    assert artifacts[2]["status"] == "failed"
    assert artifacts[2]["metadata"]["stage"] == "pr_create"


def test_complete_task_persists_lifecycle_overflow_artifacts_without_event_content() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.REULEAUXCODER,
            execution_location=ExecutionLocation.LOCAL_WORKSPACE,
        ),
        task_id="task-lifecycle-overflow-artifact",
    )
    huge = "OVERSIZED_LIFECYCLE_OUTPUT_SECRET" * 500
    artifact_id = "lifecycle-output-overflow:hook-oversized:1"

    control.complete_agent_run_activation(
        task.id,
        ExecutorRunResult(
            task_id=task.id,
            status="completed",
            output="done",
            events=[
                ExecutorEvent(
                    type="lifecycle_hook",
                    data={
                        "phase": "result",
                        "event_name": "UserPromptSubmit",
                        "hook_id": "hook:oversized",
                        "display_name": "Oversized guard",
                        "source": "skill",
                        "handler_type": "prompt",
                        "decision": "deny",
                        "continue_flow": False,
                        "output": {
                            "reason": (
                                "blocked...[truncated; "
                                f"artifact_ref={artifact_id}]"
                            ),
                            "artifacts": [
                                {
                                    "kind": "lifecycle_output_overflow",
                                    "id": artifact_id,
                                    "field": "reason",
                                    "original_chars": len(huge),
                                }
                            ],
                        },
                    },
                )
            ],
            artifacts=[
                {
                    "artifact_id": artifact_id,
                    "type": "log",
                    "status": "generated",
                    "content": huge,
                    "metadata": {
                        "kind": "lifecycle_output_overflow",
                        "hook_id": "hook:oversized",
                        "event_name": "UserPromptSubmit",
                        "field": "reason",
                        "original_chars": len(huge),
                    },
                }
            ],
        ),
        activation_id=_current_activation_id(control, task.id),
    )

    artifacts = control.artifacts_to_dict(task.id)
    assert artifacts == [
        {
            "id": artifact_id,
            "task_id": task.id,
            "type": "log",
            "status": "generated",
            "branch_name": None,
            "pr_url": None,
            "content": huge,
            "path": None,
            "metadata": {
                "kind": "lifecycle_output_overflow",
                "hook_id": "hook:oversized",
                "event_name": "UserPromptSubmit",
                "field": "reason",
                "original_chars": len(huge),
            },
            "merge_status": None,
            "merged_by": None,
        }
    ]
    events_json = json.dumps(
        [event.to_dict() for event in control.list_events(task.id)],
        sort_keys=True,
    )
    assert artifact_id in events_json
    assert "OVERSIZED_LIFECYCLE_OUTPUT_SECRET" not in events_json


def test_worktree_manager_rejects_paths_outside_runtime_root() -> None:
    root = (Path.cwd() / ".agent_run_test_tmp" / "runtime").resolve()
    manager = WorktreeManager(root)
    plan = manager.plan(
        workspace_id="workspace/one",
        task_id="task:123",
        agent_id="coder.bot",
        repo_url="git@github.com:org/repo.git",
    )

    assert plan.branch_name == "agent/coder.bot/task-123"
    assert plan.worktree_path.is_relative_to(root)
    try:
        manager.assert_owned(root.parent / "outside")
    except WorktreeOwnershipError:
        pass
    else:
        raise AssertionError("expected WorktreeOwnershipError")


def test_worktree_manager_creates_and_cleans_real_git_branch_worktree(
    tmp_path: Path,
) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    manager = WorktreeManager(tmp_path / "runtime")
    plan = manager.plan(
        workspace_id="session-1",
        task_id="task-branch",
        agent_id="coder",
    )

    prepared = manager.create_branch_worktree(
        source_repo=repo,
        plan=plan,
        base_ref="HEAD",
    )

    assert prepared.branch_name == "agent/coder/task-branch"
    assert prepared.branch_git_ref == "refs/heads/agent/coder/task-branch"
    assert prepared.worktree_path.is_dir()
    assert _git(repo, "show-ref", "--verify", prepared.branch_git_ref)
    assert Path(
        _git(prepared.worktree_path, "rev-parse", "--show-toplevel")
    ).resolve() == prepared.worktree_path

    cleanup = manager.cleanup_branch_worktree(
        source_repo=repo,
        branch_name=prepared.branch_name,
        worktree_path=prepared.worktree_path,
        delete_branch=True,
    )

    assert cleanup.ok
    assert cleanup.removed_worktree is True
    assert cleanup.deleted_branch is True
    assert not prepared.worktree_path.exists()
    branch_check = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "show-ref",
            "--verify",
            "--quiet",
            prepared.branch_git_ref,
        ],
        check=False,
    )
    assert branch_check.returncode != 0


def test_branch_agent_run_creates_relation_payload_and_cleans_worktree_on_cascade(
    tmp_path: Path,
) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    control = AgentRunControlPlane()
    source = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="base",
            owner_session_run_id="session-branch",
            executor=ExecutorType.CODEX,
            execution_location=ExecutionLocation.LOCAL_WORKSPACE,
            workdir=str(repo),
            executor_session_id="live-source-session",
        ),
        task_id="source-run",
    )
    main_binding = control.create_session_run_binding(
        session_run_id="session-branch",
        session_id="chat-session-branch",
        peer_id="peer-1",
        agent_run_id=source.id,
        branch_binding_id="main",
        selected=True,
        target_agent_run_id=source.id,
    )
    claim = control.claim_agent_run_activation(
        worker_id="worker-source",
        executors=["codex"],
    )
    assert claim is not None
    assert claim.task.id == source.id
    ok, reason = control.append_executor_event(
        source.id,
        ExecutorEvent.status("running"),
        request_id=claim.request_id,
        activation_id=claim.activation_id,
        worker_id=claim.worker_id,
    )
    assert ok, reason

    branch = control.branch_agent_run(
        source_agent_run_id=source.id,
        base_session_item_id="session-item-1",
        runtime_root=str(tmp_path / "runtime"),
        repo_root=str(repo),
        prompt="continue on branch",
        task_id="branch-run",
        branch_binding_id="branch-edit-1",
    )
    detail = control.load_agent_run_detail(branch.id)
    relation = next(
        item
        for item in detail["relations"]
        if item["relation_type"] == AgentRunRelationType.BRANCH.value
    )
    payload = relation["payload"]

    assert branch.owner_session_run_id == source.owner_session_run_id
    assert branch.executor_session_id is None
    assert branch.execution_location == ExecutionLocation.DAEMON_WORKTREE
    assert branch.worktree_role == WorktreeRole.TARGET
    assert branch.publish_policy == PublishPolicy.BRANCH
    assert payload["source_agent_run_id"] == source.id
    assert payload["target_agent_run_id"] == branch.id
    assert payload["base_session_item_id"] == "session-item-1"
    assert payload["branch_name"] == "agent/coder/branch-run"
    assert payload["branch_git_ref"] == "refs/heads/agent/coder/branch-run"
    assert payload["branch_worktree_ref"] == branch.workdir
    assert payload["permission_recompute_policy"] == "recompute_or_reject"
    assert payload["reuse_live_executor_session"] is False
    assert payload["cleanup_policy"] == "delete_with_owner_session"
    assert payload["source_workspace_root"] == str(repo)
    assert Path(payload["branch_worktree_ref"]).is_dir()
    selected_binding = control.find_session_run_binding(session_run_id="session-branch")
    assert selected_binding is not None
    assert selected_binding.branch_binding_id == "branch-edit-1"
    assert selected_binding.agent_run_id == branch.id
    assert selected_binding.parent_branch_binding_id == main_binding.branch_binding_id
    assert selected_binding.base_session_item_id == "session-item-1"
    assert selected_binding.source_agent_run_id == source.id
    assert selected_binding.target_agent_run_id == branch.id
    source_binding = control.find_session_run_binding(
        session_run_id="session-branch",
        branch_binding_id="main",
        selected_only=False,
    )
    assert source_binding is not None
    assert source_binding.selected is False
    assert source_binding.status.value == "active"
    assert control.get_agent_run(source.id).status == AgentRunStatus.RUNNING

    assert control.cancel_agent_run(source.id, reason="delete_owner_session") is True

    assert control.get_agent_run(branch.id).status == AgentRunStatus.CANCELLED
    assert not Path(payload["branch_worktree_ref"]).exists()


def test_fork_agent_run_creates_typed_relation_without_reusing_live_session() -> None:
    control = AgentRunControlPlane()
    source = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="base",
            owner_session_run_id="session-fork",
            executor=ExecutorType.CODEX,
            execution_location=ExecutionLocation.LOCAL_WORKSPACE,
            workdir="/workspace/source",
            executor_session_id="live-source-session",
        ),
        task_id="source-fork-run",
    )
    main_binding = control.create_session_run_binding(
        session_run_id="session-fork",
        session_id="chat-session-fork",
        peer_id="peer-1",
        agent_run_id=source.id,
        branch_binding_id="main",
        selected=True,
        target_agent_run_id=source.id,
    )

    fork = control.fork_agent_run(
        source_agent_run_id=source.id,
        base_session_item_id="session-item-2",
        fork_workspace_ref="fork:workspace:session-item-2",
        target_owner_session_run_id="session-fork-target",
        prompt="continue from fork",
        task_id="fork-run",
        branch_binding_id="fork-edit-1",
        provenance_status="redacted",
    )
    detail = control.load_agent_run_detail(fork.id)
    relation = next(
        item
        for item in detail["relations"]
        if item["relation_type"] == AgentRunRelationType.FORK.value
    )
    payload = relation["payload"]

    assert fork.owner_session_run_id == "session-fork-target"
    assert fork.executor_session_id != source.executor_session_id
    assert fork.workdir is None
    assert fork.workspace_ref == "fork:workspace:session-item-2"
    assert payload == {
        "source_agent_run_id": source.id,
        "target_agent_run_id": fork.id,
        "base_session_item_id": "session-item-2",
        "fork_workspace_ref": "fork:workspace:session-item-2",
        "source_owner_session_run_id": "session-fork",
        "target_owner_session_run_id": "session-fork-target",
        "source_workspace_ref": "",
        "permission_recompute_policy": "recompute_or_reject",
        "reuse_live_executor_session": False,
        "cleanup_policy": "delete_with_owner_session",
        "provenance_status": "redacted",
    }
    selected_binding = control.find_session_run_binding(session_run_id="session-fork-target")
    assert selected_binding is not None
    assert selected_binding.branch_binding_id == "fork-edit-1"
    assert selected_binding.agent_run_id == fork.id
    assert selected_binding.parent_branch_binding_id == main_binding.branch_binding_id
    assert selected_binding.base_session_item_id == "session-item-2"
    assert selected_binding.source_agent_run_id == source.id
    assert selected_binding.target_agent_run_id == fork.id


def test_basic_scheduler_selects_lowest_running_agent() -> None:
    agents = {
        "reviewer": AgentConfig(
            id="reviewer",
            max_concurrent_tasks=1,
        ),
        "coder": AgentConfig(
            id="coder",
            max_concurrent_tasks=2,
        ),
            "capability_packager": AgentConfig(
                id="capability_packager",
                visibility="internal",
                callable_scopes=[],
                taskflow_eligible=False,
            ),
    }
    scheduler = BasicAgentScheduler(agents=agents)

    assert scheduler.choose_agent().agent_id == "reviewer"


def test_control_plane_rejects_internal_agent_outside_declared_system_flow() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "capability_packager_remote": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "sandbox_worker",
                    "worktree_role": "source",
                    "publish_policy": "never",
                    "sandbox": {},
                }
            },
            "agents": {
                    "capability_packager": {
                        "visibility": "internal",
                        "callable_scopes": [],
                        "taskflow_eligible": False,
                        "system_flow_only": ["capability_ingest"],
                        "runtime_profile": "capability_packager_remote",
                }
            }
        }
    )

    with pytest.raises(ValueError, match="restricted to system flows"):
        control.submit_agent_run(
            AgentRunRequest(
                agent_id="capability_packager",
                prompt="run",
                source="manual",
            )
        )


def test_control_plane_allows_internal_agent_for_declared_system_flow() -> None:
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "capability_packager_remote": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "sandbox_worker",
                    "worktree_role": "source",
                    "publish_policy": "never",
                    "sandbox": {},
                }
            },
            "agents": {
                    "capability_packager": {
                        "visibility": "internal",
                        "callable_scopes": [],
                        "taskflow_eligible": False,
                        "system_flow_only": ["capability_ingest"],
                        "runtime_profile": "capability_packager_remote",
                }
            }
        }
    )

    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="capability_packager",
            prompt="package",
            source="capability_ingest",
        )
    )

    assert task.agent_id == "capability_packager"
    assert task.source.value == "capability_ingest"
    assert task.worktree_role == WorktreeRole.SOURCE
    assert task.publish_policy == PublishPolicy.NEVER
    assert task.metadata["worker_kind"] == "sandbox_worker"
    assert task.metadata["worktree_role"] == "source"
    assert task.metadata["publish_policy"] == "never"

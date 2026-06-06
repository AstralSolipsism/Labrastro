from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
    ExecutionLocation,
    ExecutorType,
    PublishPolicy,
    TaskSessionRef,
    TaskStatus,
    TriggerMode,
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
from labrastro_server.services.agent_runtime.session_projection import (
    agent_run_event_to_session_events,
)
from labrastro_server.services.agent_runtime.scheduler import BasicAgentScheduler
from labrastro_server.services.agent_runtime.worktree import (
    WorktreeManager,
    WorktreeOwnershipError,
)


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
            issue_id="issue-1",
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

    claim = control.claim_agent_run(worker_id="worker-1", executors=["codex"])

    assert claim is not None
    assert claim.task.id == task.id
    assert claim.executor_request.executor == ExecutorType.CODEX
    assert claim.executor_request.model == "gpt-5.2"
    assert claim.executor_request.execution_location == ExecutionLocation.DAEMON_WORKTREE
    assert claim.executor_request.worktree_role == WorktreeRole.TARGET
    assert claim.executor_request.publish_policy == PublishPolicy.NEVER
    assert claim.executor_request.metadata["worktree_role"] == "target"
    assert claim.executor_request.metadata["publish_policy"] == "never"
    assert control.claim_agent_run(worker_id="worker-2", executors=["codex"]) is None

    control.pin_session(
        task.id,
        TaskSessionRef(
            agent_id="coder",
            executor=ExecutorType.CODEX,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
            issue_id="issue-1",
            task_id=task.id,
            workdir="runtime/worktrees/ws/coder-task",
            branch="agent/coder/task-1",
            executor_session_id="codex-thread-1",
        ),
    )
    control.append_executor_event(task.id, ExecutorEvent.text_event("done"))
    control.create_or_update_pr(task.id, diff="diff --git a/file b/file")
    completed = control.complete_agent_run(
        task.id,
        ExecutorRunResult(
            task_id=task.id,
            status="completed",
            output="PR created",
            executor_session_id="codex-thread-1",
        ),
    )

    assert completed.status.value == "completed"
    artifacts = control.artifacts_to_dict(task.id)
    assert artifacts[0]["type"] == "pull_request"
    assert artifacts[0]["merge_status"] == "pending_user"
    assert control.list_events(task.id, after_seq=0)[0].type == "queued"


def test_agent_run_request_projects_source_and_sandbox_fields() -> None:
    control = AgentRunControlPlane()

    run = control.submit_agent_run(
        AgentRunRequest(
            issue_id="manual",
            agent_id="coder",
            prompt="run",
            source="manual",
            sandbox_id="sbx-1",
            sandbox_session_id="ssn-1",
            workspace_ref="repo:example",
            delegated_by_run_id="chat:parent",
            parent_run_id="chat:parent",
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
    assert task["delegated_by_run_id"] == "chat:parent"
    assert task["parent_run_id"] == "chat:parent"


def test_agent_run_request_normalizes_budget_into_control_plane_metadata() -> None:
    control = AgentRunControlPlane()

    run = control.submit_agent_run(
        AgentRunRequest(
            issue_id="manual",
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
            issue_id="manual",
            agent_id="coder",
            prompt="run",
            executor="reuleauxcoder",
            budget={"max_tool_calls": "2", "timeout_sec": 30},
        ),
        task_id="run-budget",
    )

    claim = control.claim_agent_run(worker_id="worker-1", executors=["reuleauxcoder"])

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
            issue_id="manual",
            agent_id="coder",
            prompt="run",
            executor="reuleauxcoder",
            budget={"max_turns": 1},
        ),
        task_id="run-budget-terminal",
    )

    message = "AgentRun budget exceeded: max_turns=1"
    completed = control.complete_agent_run(
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
    )
    events = [event.to_dict() for event in control.list_events(run.id)]

    assert completed.status == TaskStatus.BLOCKED
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
            issue_id="manual",
            agent_id="coder",
            prompt="run",
            budget={"made_up": 1},
        )


def test_cancel_agent_run_cascades_to_child_agent_runs() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(issue_id="parent", agent_id="planner", prompt="parent"),
        task_id="parent-run",
    )
    running_child = control.submit_agent_run(
        AgentRunRequest(
            issue_id="child",
            agent_id="coder",
            prompt="child",
            parent_run_id=parent.id,
            delegated_by_run_id=parent.id,
        ),
        task_id="child-run",
    )
    queued_grandchild = control.submit_agent_run(
        AgentRunRequest(
            issue_id="grandchild",
            agent_id="reviewer",
            prompt="grandchild",
            parent_run_id=running_child.id,
        ),
        task_id="grandchild-run",
    )
    control.append_executor_event(running_child.id, ExecutorEvent.status("running"))

    assert control.cancel_agent_run(parent.id, reason="user_stop") is True

    child = control.get_agent_run(running_child.id)
    grandchild = control.get_agent_run(queued_grandchild.id)
    child_events = [event.to_dict() for event in control.list_events(child.id)]
    grandchild_events = [event.to_dict() for event in control.list_events(grandchild.id)]

    assert child.status == TaskStatus.RUNNING
    child_cancel_requested = [
        event for event in child_events if event["type"] == "cancel_requested"
    ][0]
    child_parent_cancelled = [
        event for event in child_events if event["type"] == "parent_cancelled"
    ][0]
    child_delegated_completed = [
        event for event in child_events if event["type"] == "delegated_run_completed"
    ][0]
    child_lifecycle_events = [
        event for event in child_events if event["type"] == "lifecycle_hook"
    ]
    assert child_cancel_requested["payload"]["reason"] == "parent_cancelled:user_stop"
    assert child_parent_cancelled["payload"]["parent_run_id"] == parent.id
    assert child_delegated_completed["payload"]["agent_run_id"] == queued_grandchild.id
    assert [event["payload"]["event_name"] for event in child_lifecycle_events] == [
        "TaskCompleted",
        "SubagentStop",
    ]
    assert child_lifecycle_events[0]["payload"]["payload"]["child_agent_run_id"] == (
        queued_grandchild.id
    )
    assert child_lifecycle_events[0]["payload"]["payload"]["status"] == "cancelled"
    assert grandchild.status == TaskStatus.CANCELLED
    assert grandchild.cancel_reason == "parent_cancelled:user_stop"
    assert grandchild_events[-2]["type"] == "cancelled"
    assert grandchild_events[-1]["type"] == "parent_cancelled"


def test_child_agent_run_terminal_state_is_projected_to_parent_audit() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(issue_id="parent", agent_id="planner", prompt="parent"),
        task_id="parent-run",
    )
    child = control.submit_agent_run(
        AgentRunRequest(
            issue_id="child",
            agent_id="reviewer",
            prompt="review output",
            parent_run_id=parent.id,
            delegated_by_run_id=parent.id,
        ),
        task_id="child-run",
    )

    control.complete_agent_run(
        child.id,
        ExecutorRunResult(
            task_id=child.id,
            status="completed",
            output="review passed",
        ),
    )

    parent_events = [event.to_dict() for event in control.list_events(parent.id)]
    delegated_event = [
        event for event in parent_events if event["type"] == "delegated_run_completed"
    ][0]
    lifecycle_events = [
        event for event in parent_events if event["type"] == "lifecycle_hook"
    ]
    assert delegated_event["payload"] == {
        "run_id": child.id,
        "agent_run_id": child.id,
        "agent_id": "reviewer",
        "task": "review output",
        "status": "completed",
        "result": "review passed",
        "error": "",
        "source": "manual",
        "parent_run_id": parent.id,
        "parent_task_id": None,
        "delegated_by_run_id": parent.id,
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


def test_lifecycle_agent_adapter_child_completion_projects_to_parent_session() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(issue_id="parent", agent_id="planner", prompt="parent"),
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
    assert child["parent_run_id"] == parent.id
    assert child["metadata"]["lifecycle_hook_id"] == "hook:lifecycle-agent-review"
    assert child["metadata"]["parent_session_id"] == "session-1"
    assert child["metadata"]["parent_turn_id"] == "turn-1"

    control.complete_agent_run(
        child["id"],
        ExecutorRunResult(
            task_id=child["id"],
            status="completed",
            output="review passed",
        ),
    )

    parent_events = [event.to_dict() for event in control.list_events(parent.id)]
    delegated_event = [
        event for event in parent_events if event["type"] == "delegated_run_completed"
    ][0]
    lifecycle_events = [
        event for event in parent_events if event["type"] == "lifecycle_hook"
    ]
    session_events = agent_run_event_to_session_events(delegated_event)
    session_lifecycle_events = [
        agent_run_event_to_session_events(event)[0]
        for event in lifecycle_events
    ]

    assert delegated_event["payload"]["agent_run_id"] == child["id"]
    assert delegated_event["payload"]["parent_run_id"] == parent.id
    assert session_events[0][0] == "context_event"
    assert session_events[0][1]["phase"] == "delegated_run_completed"
    assert session_events[0][1]["child_agent_run_id"] == child["id"]
    assert session_events[0][1]["result"] == "review passed"
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


def test_lifecycle_agent_adapter_child_cancel_projects_to_parent_session() -> None:
    control = AgentRunControlPlane(max_running_tasks=4)
    parent = control.submit_agent_run(
        AgentRunRequest(issue_id="parent", agent_id="planner", prompt="parent"),
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

    assert cancelled_child.status == TaskStatus.CANCELLED
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
            issue_id="capability",
            agent_id="capability_packager",
            prompt="package repo",
            source="capability_ingest",
        ),
        task_id="run-packager",
    )
    claim = control.claim_agent_run(
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
            issue_id="agent-run-session",
            agent_id="capability_packager",
            prompt="package repo",
            executor=ExecutorType.REULEAUXCODER,
        ),
        task_id="task-reuleaux",
    )
    claim = control.claim_agent_run(
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
            issue_id="issue-events",
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
    assert [event["seq"] for event in detail["events"]] == [5, 6]


def test_list_agent_runs_returns_newest_first_like_postgres_store() -> None:
    control = AgentRunControlPlane()
    control.submit_agent_run(
        AgentRunRequest(issue_id="issue-order", agent_id="coder", prompt="old"),
        task_id="task-old",
    )
    control.submit_agent_run(
        AgentRunRequest(issue_id="issue-order", agent_id="coder", prompt="new"),
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
            issue_id="issue-failed",
            agent_id="coder",
            prompt="run",
        ),
        task_id="task-failed",
    )

    completed = control.complete_agent_run(
        task.id,
        ExecutorRunResult(
            task_id=task.id,
            status="failed",
            output="",
            error="real model failure",
        ),
    )

    assert completed.status.value == "failed"
    assert completed.output == ""
    assert completed.failure_reason == "real model failure"
    detail = control.agent_run_to_dict(task.id)
    assert detail["failure_reason"] == "real model failure"
    assert detail["cancel_reason"] is None


def test_complete_without_session_id_preserves_stream_pinned_session() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="issue-session",
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
            issue_id="issue-session",
            task_id=task.id,
            workdir="/tmp/work",
            branch="agent/coder/task-session",
            executor_session_id="codex-thread-1",
        ),
    )

    completed = control.complete_agent_run(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
    )

    assert completed.executor_session_id == "codex-thread-1"
    assert control.load_agent_run_detail(task.id)["session"]["executor_session_id"] == "codex-thread-1"


def test_followup_task_inherits_parent_session_when_scope_matches() -> None:
    control = AgentRunControlPlane()
    parent = control.submit_agent_run(
        AgentRunRequest(
            issue_id="issue-followup",
            agent_id="coder",
            prompt="parent",
            executor="claude",
            execution_location="daemon_worktree",
            workdir="/tmp/work",
            branch_name="agent/coder/task-parent",
            pr_url="https://example.test/pr/1",
            executor_session_id="claude-session-1",
        ),
        task_id="task-parent",
    )

    followup = control.submit_agent_run(
        AgentRunRequest(
            issue_id="issue-followup",
            agent_id="coder",
            prompt="follow up",
            parent_task_id=parent.id,
            trigger_comment_id="comment-1",
        ),
        task_id="task-followup",
    )

    assert followup.executor == parent.executor
    assert followup.runtime_profile_id == parent.runtime_profile_id
    assert followup.workdir == parent.workdir
    assert followup.branch_name == parent.branch_name
    assert followup.pr_url == parent.pr_url
    assert followup.executor_session_id == "claude-session-1"


def test_reuleauxcoder_followup_reuses_parent_executor_session() -> None:
    control = AgentRunControlPlane()
    parent = control.submit_agent_run(
        AgentRunRequest(
            issue_id="capability-followup",
            agent_id="capability_packager",
            prompt="draft",
            executor=ExecutorType.REULEAUXCODER,
            execution_location=ExecutionLocation.REMOTE_SERVER,
            workdir="/tmp/capability",
            branch_name="agent/capability/task-parent",
        ),
        task_id="task-capability-parent",
    )

    followup = control.submit_agent_run(
        AgentRunRequest(
            issue_id="capability-followup",
            agent_id="capability_packager",
            prompt="revise draft",
            parent_task_id=parent.id,
        ),
        task_id="task-capability-followup",
    )

    assert parent.executor_session_id == "labrastro-agent-run-task-capability-parent"
    assert followup.executor_session_id == parent.executor_session_id


def test_followup_task_does_not_inherit_session_for_different_agent() -> None:
    control = AgentRunControlPlane()
    parent = control.submit_agent_run(
        AgentRunRequest(
            issue_id="issue-followup",
            agent_id="coder",
            prompt="parent",
            executor="claude",
            execution_location="daemon_worktree",
            workdir="/tmp/work",
            branch_name="agent/coder/task-parent",
            executor_session_id="claude-session-1",
        ),
        task_id="task-parent-agent",
    )

    followup = control.submit_agent_run(
        AgentRunRequest(
            issue_id="issue-followup",
            agent_id="reviewer",
            prompt="follow up",
            parent_task_id=parent.id,
        ),
        task_id="task-followup-agent",
    )

    assert followup.executor_session_id is None


def test_claim_task_waits_for_wakeup_when_task_is_submitted() -> None:
    control = AgentRunControlPlane()
    claims = []

    def wait_for_claim() -> None:
        claims.append(
            control.claim_agent_run(
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
            issue_id="issue-1",
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
            issue_id="environment-check",
            agent_id="environment_configurator",
            prompt="check environment",
            executor=ExecutorType.FAKE,
            trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
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
    control.complete_agent_run(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
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
            issue_id="worktree",
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
            issue_id="worktree-remove",
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


def test_environment_runtime_blocks_non_manifest_shell_command() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="environment-check",
            agent_id="environment_configurator",
            prompt="check environment",
            executor=ExecutorType.FAKE,
            trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
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
    completed = control.complete_agent_run(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
    )

    assert completed.status.value == "blocked"
    events = control.list_events(task.id, after_seq=0)
    assert "environment.entry_failed" in [event.type for event in events]


def test_environment_runtime_reports_failed_install_command() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="environment-configure",
            agent_id="environment_configurator",
            prompt="configure environment",
            executor=ExecutorType.FAKE,
            trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
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
            issue_id="issue-1",
            agent_id="coder",
            prompt="fix",
            executor=ExecutorType.CODEX,
            runtime_profile_id="codex",
        ),
        task_id="task-prompt",
    )

    claim = control.claim_agent_run(worker_id="worker-1", executors=["codex"])

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
    }
    assert control.get_agent_run(task.id).status.value == "dispatched"


def test_runtime_configure_refreshes_snapshot_without_dropping_tasks() -> None:
    control = AgentRunControlPlane()
    existing = control.submit_agent_run(
        AgentRunRequest(issue_id="issue-1", agent_id="legacy", prompt="old"),
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
        AgentRunRequest(issue_id="issue-2", agent_id="reviewer", prompt="new"),
        task_id="task-new",
    )

    assert control.max_running_tasks == 3
    assert control.get_agent_run(existing.id).status.value == "queued"
    claim = control.claim_agent_run(worker_id="worker-1", executors=["fake"])

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
        AgentRunRequest(issue_id="issue-1", agent_id="reviewer", prompt="review"),
        task_id="task-agent-defaults",
    )

    assert task.executor == ExecutorType.FAKE
    assert task.execution_location == ExecutionLocation.DAEMON_WORKTREE
    assert task.runtime_profile_id == "fake_profile"
    assert task.metadata["model"] == "smoke-model"
    assert task.metadata["worker_kind"] == "server_worker"
    assert task.metadata["model_request_origin"] == "server"
    claim = control.claim_agent_run(worker_id="worker-1", executors=["fake"])
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
            issue_id="issue-1",
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
            AgentRunRequest(issue_id="issue-1", agent_id="reviewer", prompt="run"),
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
            AgentRunRequest(issue_id="issue-1", agent_id="reviewer", prompt="run"),
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
                issue_id="taskflow-1",
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
        AgentRunRequest(issue_id="issue-1", agent_id="reviewer", prompt="run"),
        task_id="task-remote-server",
    )

    assert (
        control.claim_agent_run(
            worker_id="local-peer",
            worker_kind="local_peer",
            executors=["fake"],
            peer_features=["agent_runs", "agent_runs.local_workspace"],
        )
        is None
    )
    claim = control.claim_agent_run(
        worker_id="server-worker",
        worker_kind="server_worker",
        executors=["fake"],
        peer_features=["agent_runs.remote_server"],
    )

    assert claim is not None
    assert claim.task.id == task.id
    assert claim.executor_request.metadata["worker_kind"] == "server_worker"


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
            issue_id="issue-1",
            agent_id="reviewer",
            prompt="run",
            executor=ExecutorType.FAKE,
            execution_location=execution_location,
        ),
        task_id=f"task-{execution_location.value}",
    )

    claim = control.claim_agent_run(
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
            issue_id="issue-1",
            agent_id="coder",
            prompt="run local cli",
            metadata={"workspace_root": "G:/repo/main"},
        ),
        task_id="task-local-cli",
    )
    claim = control.claim_agent_run(
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
            AgentRunRequest(issue_id="issue-1", agent_id="coder", prompt="run"),
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
    control.submit_agent_run(AgentRunRequest(issue_id="slot-1", agent_id="reviewer", prompt="review"))
    control.submit_agent_run(AgentRunRequest(issue_id="slot-2", agent_id="builder", prompt="build"))

    first = control.claim_agent_run(
        worker_id="server-1",
        worker_kind="server_worker",
        executors=["fake"],
        peer_features=["worker_kind:server_worker", "agent_runs.remote_server"],
    )
    second = control.claim_agent_run(
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
    control.submit_agent_run(AgentRunRequest(issue_id="slot-remote", agent_id="remote_agent", prompt="remote"))
    control.submit_agent_run(AgentRunRequest(issue_id="slot-local", agent_id="local_agent", prompt="local"))

    server_claim = control.claim_agent_run(
        worker_id="server-1",
        worker_kind="server_worker",
        executors=["fake"],
        peer_features=["worker_kind:server_worker", "agent_runs.remote_server"],
    )
    local_claim = control.claim_agent_run(
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
            issue_id="environment",
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
            issue_id="environment",
            agent_id="environment_configurator",
            prompt="check",
            source="environment",
        ),
        task_id="task-default-environment",
    )
    packager_task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="capability-ingest",
            agent_id="capability_packager",
            prompt="draft",
            source="capability_ingest",
        ),
        task_id="task-default-packager",
    )

    assert environment_task.metadata["effective_capabilities"]["tools"] == [
        "builtin:shell"
    ]
    assert packager_task.metadata["effective_capabilities"]["tools"] == [
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
            issue_id="sandbox-claim",
            agent_id="sandbox_agent",
            prompt="sandbox",
        )
    )

    server_claim = control.claim_agent_run(
        worker_id="server-1",
        worker_kind="server_worker",
        executors=["fake"],
        peer_features=["worker_kind:server_worker", "agent_runs.remote_server"],
    )
    sandbox_claim = control.claim_agent_run(
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
        AgentRunRequest(issue_id="issue-1", agent_id="coder", prompt="run shell"),
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

    assert control.get_agent_run(task.id).status.value == "waiting_approval"


def test_waiting_approval_status_projects_to_session_approval_request() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(issue_id="issue-1", agent_id="coder", prompt="run shell"),
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
        "context_event",
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
            issue_id="taskflow-1",
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
            issue_id="taskflow-1",
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
            issue_id="delegation-1",
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
            issue_id="issue-1",
            agent_id="coder",
            prompt="fix local",
            executor=ExecutorType.CODEX,
            execution_location=ExecutionLocation.LOCAL_WORKSPACE,
            metadata={"workspace_root": "G:/repo/main"},
        ),
        task_id="task-local",
    )

    assert (
        control.claim_agent_run(
            worker_id="worker-shell",
            executors=["codex"],
            peer_features=["shell"],
            workspace_root="G:/repo/main",
        )
        is None
    )
    assert (
        control.claim_agent_run(
            worker_id="worker-no-workspace",
            executors=["codex"],
            peer_features=["agent_runs", "agent_runs.local_workspace"],
        )
        is None
    )
    assert (
        control.claim_agent_run(
            worker_id="worker-other",
            executors=["codex"],
            peer_features=["agent_runs", "agent_runs.local_workspace"],
            workspace_root="G:/repo/other",
        )
        is None
    )
    claim = control.claim_agent_run(
        worker_id="worker-local",
        executors=["codex"],
        peer_features=["agent_runs", "agent_runs.local_workspace"],
        workspace_root="G:\\repo\\main",
    )

    assert claim is not None
    assert claim.task.id == local_task.id

    remote_task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="issue-2",
            agent_id="coder",
            prompt="fix remote",
            executor=ExecutorType.CLAUDE,
            execution_location=ExecutionLocation.REMOTE_SERVER,
        ),
        task_id="task-remote",
    )
    remote_claim = control.claim_agent_run(
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
            issue_id="issue-1",
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
        ),
        task_id="task-heartbeat",
    )
    claim = control.claim_agent_run(
        worker_id="worker-1",
        worker_kind="local_peer",
        executors=["fake"],
        peer_id="peer-1",
        peer_features=["agent_runs.local_workspace"],
        lease_sec=1,
    )

    assert claim is not None
    heartbeat = control.heartbeat_agent_run(
        request_id=claim.request_id,
        task_id=task.id,
        worker_id="worker-1",
        peer_id="peer-1",
        lease_sec=5,
    )
    assert heartbeat["ok"] is True
    assert heartbeat["cancel_requested"] is False
    assert control.get_agent_run(task.id).status.value == "running"

    assert control.cancel_agent_run(task.id, reason="stop") is True
    heartbeat = control.heartbeat_agent_run(
        request_id=claim.request_id,
        task_id=task.id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    assert heartbeat["cancel_requested"] is True
    assert heartbeat["reason"] == "stop"

    completed = control.complete_agent_run(
        task.id,
        ExecutorRunResult(
            task_id=task.id,
            status="cancelled",
            output="",
            error="execution cancelled",
        ),
    )
    assert completed.status.value == "cancelled"
    assert completed.cancel_reason == "stop"
    assert control.agent_run_to_dict(task.id)["cancel_reason"] == "stop"

    missing = control.heartbeat_agent_run(
        request_id="missing-claim",
        task_id="missing-task",
        worker_id="worker-1",
    )
    assert missing["ok"] is False
    assert missing["cancel_requested"] is True
    assert missing["reason"] == "agent_run_not_found"

    stale_task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="issue-2",
            agent_id="coder",
            prompt="stale",
            executor=ExecutorType.FAKE,
        ),
        task_id="task-stale",
    )
    stale_claim = control.claim_agent_run(
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
            issue_id="issue-1",
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
        ),
        task_id="task-owner",
    )
    claim = control.claim_agent_run(
        worker_id="worker-1",
        worker_kind="local_peer",
        executors=["fake"],
        peer_id="peer-1",
        peer_features=["agent_runs.local_workspace"],
    )

    assert claim is not None
    ok, reason = control.pin_claimed_session(
        request_id=claim.request_id,
        task_id=task.id,
        worker_id="other-worker",
        peer_id="peer-1",
        workdir="/tmp/work",
    )
    assert ok is False
    assert reason == "worker_mismatch"

    ok, reason = control.pin_claimed_session(
        request_id=claim.request_id,
        task_id=task.id,
        worker_id="worker-1",
        peer_id="peer-1",
        workdir="/tmp/work",
        branch="agent/coder/task-owner",
    )
    assert ok is True
    assert reason == ""
    assert control.get_agent_run(task.id).workdir == "/tmp/work"

    ok, reason = control.append_executor_event(
        task.id,
        ExecutorEvent.status("running"),
        request_id=claim.request_id,
        worker_id="other-worker",
        peer_id="peer-1",
    )
    assert ok is False
    assert reason == "worker_mismatch"

    ok, reason = control.append_executor_event(
        task.id,
        ExecutorEvent.text_event("hello"),
        request_id=claim.request_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    assert ok is True
    assert reason == ""

    ok, reason, completed = control.complete_claimed_agent_run(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        request_id=claim.request_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    assert ok is True
    assert reason == ""
    assert completed is not None
    assert completed.status.value == "completed"


def test_blocked_complete_and_retry_terminal_task() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="issue-1",
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
            metadata={"repo_url": "file:///repo"},
        ),
        task_id="task-blocked",
    )
    claim = control.claim_agent_run(
        worker_id="worker-1",
        worker_kind="server_worker",
        executors=["fake"],
        peer_id="peer-1",
        peer_features=["agent_runs.daemon_worktree"],
    )

    assert claim is not None
    ok, reason, blocked = control.complete_claimed_agent_run(
        task.id,
        ExecutorRunResult(
            task_id=task.id,
            status="blocked",
            output="",
            error="repo_url missing",
        ),
        request_id=claim.request_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )

    assert ok is True
    assert reason == ""
    assert blocked is not None
    assert blocked.status.value == "blocked"

    blocked.executor_session_id = "fake-session-1"

    retry = control.retry_agent_run(task.id, new_agent_run_id="task-retry")

    assert retry.status.value == "queued"
    assert retry.metadata["retry_of"] == task.id
    assert retry.metadata["repo_url"] == "file:///repo"
    assert retry.executor_session_id is None

    resumed = control.retry_agent_run(
        task.id,
        new_agent_run_id="task-retry-resume",
        resume_session=True,
    )
    assert resumed.executor_session_id == "fake-session-1"


def test_complete_task_accepts_branch_pr_and_failed_publish_artifacts() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="issue-1",
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
        ),
        task_id="task-artifacts",
    )
    claim = control.claim_agent_run(
        worker_id="worker-1",
        worker_kind="server_worker",
        executors=["fake"],
        peer_id="peer-1",
        peer_features=["agent_runs.daemon_worktree"],
    )

    assert claim is not None
    ok, reason, completed = control.complete_claimed_agent_run(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
        request_id=claim.request_id,
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
    assert completed.branch_name == "agent/coder/task-artifacts"
    assert completed.pr_url == "https://example.test/pr/1"
    artifacts = control.artifacts_to_dict(task.id)
    assert [artifact["type"] for artifact in artifacts] == [
        "branch",
        "pull_request",
        "log",
    ]
    assert artifacts[2]["status"] == "failed"
    assert artifacts[2]["metadata"]["stage"] == "pr_create"


def test_complete_task_persists_lifecycle_overflow_artifacts_without_event_content() -> None:
    control = AgentRunControlPlane()
    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="issue-1",
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.REULEAUXCODER,
            execution_location=ExecutionLocation.LOCAL_WORKSPACE,
        ),
        task_id="task-lifecycle-overflow-artifact",
    )
    huge = "OVERSIZED_LIFECYCLE_OUTPUT_SECRET" * 500
    artifact_id = "lifecycle-output-overflow:hook-oversized:1"

    control.complete_agent_run(
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
            delegable=False,
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
                    "delegable": False,
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
                issue_id="manual",
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
                    "delegable": False,
                    "taskflow_eligible": False,
                    "system_flow_only": ["capability_ingest"],
                    "runtime_profile": "capability_packager_remote",
                }
            }
        }
    )

    task = control.submit_agent_run(
        AgentRunRequest(
            issue_id="ingest",
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

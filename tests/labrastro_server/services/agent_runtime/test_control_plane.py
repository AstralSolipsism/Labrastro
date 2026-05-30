from __future__ import annotations

from pathlib import Path
import threading
import time

import pytest

from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
    ExecutionLocation,
    ExecutorType,
    TaskSessionRef,
    TriggerMode,
)
from reuleauxcoder.domain.config.models import build_agent_run_snapshot
from reuleauxcoder.services.config.loader import ConfigLoader
from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunRequest,
    AgentRunControlPlane,
    AgentRunRequest,
)
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.runtime_store import (
    runtime_slot_key_for_agent_run,
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
    assert task.metadata["worker_kind"] == "sandbox_worker"

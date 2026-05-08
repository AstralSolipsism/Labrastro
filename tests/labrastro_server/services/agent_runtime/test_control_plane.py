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
from labrastro_server.services.agent_runtime.control_plane import (
    AgentRuntimeControlPlane,
    RuntimeTaskRequest,
)
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.scheduler import BasicAgentScheduler
from labrastro_server.services.agent_runtime.worktree import (
    WorktreeManager,
    WorktreeOwnershipError,
)


def test_task_queue_claim_pin_complete_and_pr_artifact() -> None:
    control = AgentRuntimeControlPlane(
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
    task = control.submit_task(
        RuntimeTaskRequest(
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

    claim = control.claim_task(worker_id="worker-1", executors=["codex"])

    assert claim is not None
    assert claim.task.id == task.id
    assert claim.executor_request.executor == ExecutorType.CODEX
    assert claim.executor_request.model == "gpt-5.2"
    assert claim.executor_request.execution_location == ExecutionLocation.DAEMON_WORKTREE
    assert control.claim_task(worker_id="worker-2", executors=["codex"]) is None

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
    completed = control.complete_task(
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


def test_runtime_events_are_limited_and_task_detail_reads_tail() -> None:
    control = AgentRuntimeControlPlane()
    task = control.submit_task(
        RuntimeTaskRequest(
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

    detail = control.load_task_detail(task.id, event_limit=2)
    assert [event["seq"] for event in detail["events"]] == [5, 6]


def test_complete_without_session_id_preserves_stream_pinned_session() -> None:
    control = AgentRuntimeControlPlane()
    task = control.submit_task(
        RuntimeTaskRequest(
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

    completed = control.complete_task(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
    )

    assert completed.executor_session_id == "codex-thread-1"
    assert control.load_task_detail(task.id)["session"]["executor_session_id"] == "codex-thread-1"


def test_followup_task_inherits_parent_session_when_scope_matches() -> None:
    control = AgentRuntimeControlPlane()
    parent = control.submit_task(
        RuntimeTaskRequest(
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

    followup = control.submit_task(
        RuntimeTaskRequest(
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
    control = AgentRuntimeControlPlane()
    parent = control.submit_task(
        RuntimeTaskRequest(
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

    followup = control.submit_task(
        RuntimeTaskRequest(
            issue_id="issue-followup",
            agent_id="reviewer",
            prompt="follow up",
            parent_task_id=parent.id,
        ),
        task_id="task-followup-agent",
    )

    assert followup.executor_session_id is None


def test_claim_task_waits_for_wakeup_when_task_is_submitted() -> None:
    control = AgentRuntimeControlPlane()
    claims = []

    def wait_for_claim() -> None:
        claims.append(
            control.claim_task(
                worker_id="worker-wait",
                executors=["fake"],
                wait_sec=2,
            )
        )

    thread = threading.Thread(target=wait_for_claim)
    thread.start()
    time.sleep(0.1)
    control.submit_task(
        RuntimeTaskRequest(
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
    control = AgentRuntimeControlPlane()
    task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="environment-check",
            agent_id="environment_configurator",
            prompt="check environment",
            executor=ExecutorType.FAKE,
            trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
            metadata={
                "workflow": "environment_config",
                "environment_mode": "check",
                "entry_ids": ["cli:gitnexus"],
                "manifest_hash": "hash",
                "allowed_commands": [
                    {
                        "entry_id": "cli:gitnexus",
                        "kind": "cli",
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
    control.complete_task(
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
    control = AgentRuntimeControlPlane()
    task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="environment-check",
            agent_id="environment_configurator",
            prompt="check environment",
            executor=ExecutorType.FAKE,
            trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
            metadata={
                "workflow": "environment_config",
                "environment_mode": "check",
                "entry_ids": ["cli:gitnexus"],
                "manifest_hash": "hash",
                "allowed_commands": [
                    {
                        "entry_id": "cli:gitnexus",
                        "kind": "cli",
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
    completed = control.complete_task(
        task.id,
        ExecutorRunResult(task_id=task.id, status="completed", output="done"),
    )

    assert completed.status.value == "blocked"
    events = control.list_events(task.id, after_seq=0)
    assert "environment.entry_failed" in [event.type for event in events]


def test_environment_runtime_reports_failed_install_command() -> None:
    control = AgentRuntimeControlPlane()
    task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="environment-configure",
            agent_id="environment_configurator",
            prompt="configure environment",
            executor=ExecutorType.FAKE,
            trigger_mode=TriggerMode.ENVIRONMENT_CONFIG,
            metadata={
                "workflow": "environment_config",
                "environment_mode": "configure",
                "entry_ids": ["cli:gitnexus"],
                "manifest_hash": "hash",
                "allowed_commands": [
                    {
                        "entry_id": "cli:gitnexus",
                        "kind": "cli",
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
    control = AgentRuntimeControlPlane(
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
                    "capabilities": ["code"],
                    "prompt": {
                        "agent_md": "docs/coder.md",
                        "system_append": "Use the repo conventions.",
                    },
                    "mcp": {"servers": ["github"]},
                    "credential_refs": {"git": "cred-git"},
                }
            },
        }
    )
    task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="issue-1",
            agent_id="coder",
            prompt="fix",
            executor=ExecutorType.CODEX,
            runtime_profile_id="codex",
        ),
        task_id="task-prompt",
    )

    claim = control.claim_task(worker_id="worker-1", executors=["codex"])

    assert claim is not None
    metadata = claim.executor_request.metadata
    assert "AGENTS.md" in metadata["prompt_files"]
    assert "Use the repo conventions." in metadata["prompt_files"]["AGENTS.md"]
    assert metadata["prompt_metadata"]["credential_refs"] == {
        "model": "cred-model",
        "git": "cred-git",
    }
    assert metadata["system_prompt"] == metadata["prompt_files"]["AGENTS.md"]
    assert control.get_task(task.id).status.value == "dispatched"


def test_runtime_configure_refreshes_snapshot_without_dropping_tasks() -> None:
    control = AgentRuntimeControlPlane()
    existing = control.submit_task(
        RuntimeTaskRequest(issue_id="issue-1", agent_id="legacy", prompt="old"),
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
                    "capabilities": ["review"],
                }
            },
        },
    )
    control.submit_task(
        RuntimeTaskRequest(issue_id="issue-2", agent_id="reviewer", prompt="new"),
        task_id="task-new",
    )

    assert control.max_running_tasks == 3
    assert control.get_task(existing.id).status.value == "queued"
    claim = control.claim_task(worker_id="worker-1", executors=["fake"])

    assert claim is not None
    assert claim.task.id == "task-new"
    assert "fake_profile" in claim.runtime_snapshot["runtime_profiles"]


def test_submit_resolves_agent_runtime_profile_defaults() -> None:
    control = AgentRuntimeControlPlane(
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
                    "capabilities": ["read_repo", "code_review"],
                    "prompt": {"system_append": "Review carefully."},
                }
            },
        }
    )

    task = control.submit_task(
        RuntimeTaskRequest(issue_id="issue-1", agent_id="reviewer", prompt="review"),
        task_id="task-agent-defaults",
    )

    assert task.executor == ExecutorType.FAKE
    assert task.execution_location == ExecutionLocation.DAEMON_WORKTREE
    assert task.runtime_profile_id == "fake_profile"
    assert task.metadata["model"] == "smoke-model"
    claim = control.claim_task(worker_id="worker-1", executors=["fake"])
    assert claim is not None
    assert claim.executor_request.runtime_profile_id == "fake_profile"
    assert claim.executor_request.executor == ExecutorType.FAKE
    assert "AGENT_RUNTIME.md" in claim.executor_request.metadata["prompt_files"]
    assert (
        "Review carefully."
        in claim.executor_request.metadata["prompt_files"]["AGENT_RUNTIME.md"]
    )


def test_submit_explicit_executor_and_profile_override_agent_defaults() -> None:
    control = AgentRuntimeControlPlane(
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

    task = control.submit_task(
        RuntimeTaskRequest(
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


def test_submit_rejects_missing_agent_runtime_profile() -> None:
    control = AgentRuntimeControlPlane(
        runtime_snapshot={"agents": {"reviewer": {"runtime_profile": "missing"}}}
    )

    with pytest.raises(ValueError, match="runtime profile not found: missing"):
        control.submit_task(
            RuntimeTaskRequest(issue_id="issue-1", agent_id="reviewer", prompt="run"),
            task_id="task-missing-profile",
        )


def test_waiting_approval_event_updates_task_status() -> None:
    control = AgentRuntimeControlPlane()
    task = control.submit_task(
        RuntimeTaskRequest(issue_id="issue-1", agent_id="coder", prompt="run shell"),
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

    assert control.get_task(task.id).status.value == "waiting_approval"


def test_claim_filters_by_workspace_and_execution_location() -> None:
    control = AgentRuntimeControlPlane()
    local_task = control.submit_task(
        RuntimeTaskRequest(
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
        control.claim_task(
            worker_id="worker-shell",
            executors=["codex"],
            peer_capabilities=["shell"],
            workspace_root="G:/repo/main",
        )
        is None
    )
    assert (
        control.claim_task(
            worker_id="worker-no-workspace",
            executors=["codex"],
            peer_capabilities=["agent_runtime", "agent_runtime.local_workspace"],
        )
        is None
    )
    assert (
        control.claim_task(
            worker_id="worker-other",
            executors=["codex"],
            peer_capabilities=["agent_runtime", "agent_runtime.local_workspace"],
            workspace_root="G:/repo/other",
        )
        is None
    )
    claim = control.claim_task(
        worker_id="worker-local",
        executors=["codex"],
        peer_capabilities=["agent_runtime", "agent_runtime.local_workspace"],
        workspace_root="G:\\repo\\main",
    )

    assert claim is not None
    assert claim.task.id == local_task.id

    remote_task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="issue-2",
            agent_id="coder",
            prompt="fix remote",
            executor=ExecutorType.CLAUDE,
            execution_location=ExecutionLocation.REMOTE_SERVER,
        ),
        task_id="task-remote",
    )
    remote_claim = control.claim_task(
        worker_id="worker-remote",
        executors=["claude"],
        peer_capabilities=["agent_runtime.remote_server"],
    )

    assert remote_claim is not None
    assert remote_claim.task.id == remote_task.id


def test_heartbeat_cancel_and_stale_recovery() -> None:
    control = AgentRuntimeControlPlane()
    task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="issue-1",
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
        ),
        task_id="task-heartbeat",
    )
    claim = control.claim_task(
        worker_id="worker-1",
        executors=["fake"],
        peer_id="peer-1",
        peer_capabilities=["agent_runtime"],
        lease_sec=1,
    )

    assert claim is not None
    heartbeat = control.heartbeat_task(
        request_id=claim.request_id,
        task_id=task.id,
        worker_id="worker-1",
        peer_id="peer-1",
        lease_sec=5,
    )
    assert heartbeat["ok"] is True
    assert heartbeat["cancel_requested"] is False
    assert control.get_task(task.id).status.value == "running"

    assert control.cancel_task(task.id, reason="stop") is True
    heartbeat = control.heartbeat_task(
        request_id=claim.request_id,
        task_id=task.id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    assert heartbeat["cancel_requested"] is True
    assert heartbeat["reason"] == "stop"

    completed = control.complete_task(
        task.id,
        ExecutorRunResult(
            task_id=task.id,
            status="cancelled",
            output="",
            error="execution cancelled",
        ),
    )
    assert completed.status.value == "cancelled"

    missing = control.heartbeat_task(
        request_id="missing-claim",
        task_id="missing-task",
        worker_id="worker-1",
    )
    assert missing["ok"] is False
    assert missing["cancel_requested"] is True
    assert missing["reason"] == "task_not_found"

    stale_task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="issue-2",
            agent_id="coder",
            prompt="stale",
            executor=ExecutorType.FAKE,
        ),
        task_id="task-stale",
    )
    stale_claim = control.claim_task(
        worker_id="worker-2",
        executors=["fake"],
        peer_id="peer-2",
        peer_capabilities=["agent_runtime"],
        lease_sec=1,
    )

    assert stale_claim is not None
    recovered = control.recover_stale_tasks(now=9999999999)
    assert recovered == [stale_task.id]
    assert control.get_task(stale_task.id).status.value == "queued"
    assert any(event.type == "lease_expired" for event in control.list_events(stale_task.id))


def test_claim_owner_validates_session_event_and_complete() -> None:
    control = AgentRuntimeControlPlane()
    task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="issue-1",
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
        ),
        task_id="task-owner",
    )
    claim = control.claim_task(
        worker_id="worker-1",
        executors=["fake"],
        peer_id="peer-1",
        peer_capabilities=["agent_runtime"],
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
    assert control.get_task(task.id).workdir == "/tmp/work"

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

    ok, reason, completed = control.complete_claimed_task(
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
    control = AgentRuntimeControlPlane()
    task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="issue-1",
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
            metadata={"repo_url": "file:///repo"},
        ),
        task_id="task-blocked",
    )
    claim = control.claim_task(
        worker_id="worker-1",
        executors=["fake"],
        peer_id="peer-1",
        peer_capabilities=["agent_runtime"],
    )

    assert claim is not None
    ok, reason, blocked = control.complete_claimed_task(
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

    retry = control.retry_task(task.id, new_task_id="task-retry")

    assert retry.status.value == "queued"
    assert retry.metadata["retry_of"] == task.id
    assert retry.metadata["repo_url"] == "file:///repo"
    assert retry.executor_session_id is None

    resumed = control.retry_task(
        task.id,
        new_task_id="task-retry-resume",
        resume_session=True,
    )
    assert resumed.executor_session_id == "fake-session-1"


def test_complete_task_accepts_branch_pr_and_failed_publish_artifacts() -> None:
    control = AgentRuntimeControlPlane()
    task = control.submit_task(
        RuntimeTaskRequest(
            issue_id="issue-1",
            agent_id="coder",
            prompt="run",
            executor=ExecutorType.FAKE,
            execution_location=ExecutionLocation.DAEMON_WORKTREE,
        ),
        task_id="task-artifacts",
    )
    claim = control.claim_task(
        worker_id="worker-1",
        executors=["fake"],
        peer_id="peer-1",
        peer_capabilities=["agent_runtime"],
    )

    assert claim is not None
    ok, reason, completed = control.complete_claimed_task(
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
    root = (Path.cwd() / ".agent_runtime_test_tmp" / "runtime").resolve()
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


def test_basic_scheduler_respects_capability_and_agent_limit() -> None:
    agents = {
        "reviewer": AgentConfig(
            id="reviewer",
            capabilities=["review"],
            max_concurrent_tasks=1,
        ),
        "coder": AgentConfig(
            id="coder",
            capabilities=["code"],
            max_concurrent_tasks=2,
        ),
    }
    scheduler = BasicAgentScheduler(agents=agents)

    assert scheduler.choose_agent(required_capability="code").agent_id == "coder"

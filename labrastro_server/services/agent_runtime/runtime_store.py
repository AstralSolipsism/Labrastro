"""Storage protocols for Agent Runtime control-plane state."""

from __future__ import annotations

from typing import Any, Protocol

from reuleauxcoder.domain.agent_runtime.models import (
    TaskArtifact,
    AgentRunRecord,
    ExecutionLocation,
    ModelRequestOrigin,
    TaskSessionRef,
    TaskStatus,
    WorkerKind,
)
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)

DEFAULT_RUNTIME_EVENT_LIMIT = 200
MAX_RUNTIME_EVENT_LIMIT = 1000
RUNTIME_SLOT_KEYS = {
    "server_agent_run_slots",
    "server_sandbox_slots",
    "local_peer_agent_run_slots",
    "model_request_slots",
}
RUNNING_AGENT_RUN_STATUSES = {
    TaskStatus.DISPATCHED,
    TaskStatus.RUNNING,
    TaskStatus.WAITING_APPROVAL,
}

_WORKTREE_LIFECYCLE_STATUS = {
    "worktree_ready": (
        "WorktreeCreate",
        "agent_run_worktree_ready",
        "AgentRun worktree ready",
    ),
    "worktree_removed": (
        "WorktreeRemove",
        "agent_run_worktree_removed",
        "AgentRun worktree removed",
    ),
}


def parent_agent_run_id(task: AgentRunRecord) -> str:
    return str(
        task.parent_run_id
        or task.parent_task_id
        or task.delegated_by_run_id
        or ""
    )


def artifact_attached_event_payload(artifact: TaskArtifact) -> dict[str, Any]:
    metadata = dict(artifact.metadata)
    artifact_payload = {
        "id": artifact.id,
        "task_id": artifact.task_id,
        "type": artifact.type.value,
        "status": artifact.status.value,
        "branch_name": artifact.branch_name,
        "pr_url": artifact.pr_url,
        "content": artifact.content,
        "path": artifact.path,
        "metadata": metadata,
        "merge_status": artifact.merge_status.value if artifact.merge_status else None,
        "merged_by": artifact.merged_by,
    }
    if metadata.get("kind") == "lifecycle_output_overflow":
        artifact_payload["content"] = None
        artifact_payload["metadata"] = {
            **metadata,
            "content_stored": True,
            "content_omitted_from_event": True,
        }
    return {"artifact": artifact_payload}


def executor_result_artifacts(
    result: ExecutorRunResult,
    artifacts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*(artifacts or []), *(getattr(result, "artifacts", []) or [])]:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("artifact_id") or item.get("id") or "")
        if artifact_id and artifact_id in seen:
            continue
        if artifact_id:
            seen.add(artifact_id)
        combined.append(dict(item))
    return combined


def delegated_run_completed_payload(task: AgentRunRecord) -> dict[str, Any]:
    status = task.status.value
    return {
        "run_id": task.id,
        "agent_run_id": task.id,
        "agent_id": task.agent_id,
        "task": task.prompt,
        "status": status,
        "result": task.output or "",
        "error": "" if status == TaskStatus.COMPLETED.value else task.failure_reason or "",
        "source": task.source.value,
        "parent_run_id": task.parent_run_id,
        "parent_task_id": task.parent_task_id,
        "delegated_by_run_id": task.delegated_by_run_id,
    }


def delegated_terminal_lifecycle_events(
    task: AgentRunRecord,
) -> list[tuple[str, dict[str, Any]]]:
    parent_run_id = parent_agent_run_id(task)
    if not parent_run_id or parent_run_id == task.id:
        return []
    terminal_payload = delegated_run_completed_payload(task)
    metadata = task.metadata if isinstance(task.metadata, dict) else {}
    status = terminal_payload["status"]
    level = (
        "info"
        if status == TaskStatus.COMPLETED.value
        else "warning"
        if status == TaskStatus.CANCELLED.value
        else "error"
    )
    message = f"{task.agent_id or 'delegated agent'} {status}"
    payload = {
        **terminal_payload,
        "child_agent_run_id": task.id,
        "child_agent_id": task.agent_id,
        "parent_session_id": str(metadata.get("parent_session_id") or ""),
        "parent_turn_id": str(metadata.get("parent_turn_id") or ""),
        "lifecycle_hook_id": str(metadata.get("lifecycle_hook_id") or ""),
        "lifecycle_hook_source": str(metadata.get("lifecycle_hook_source") or ""),
    }
    return [
        (
            "lifecycle_hook",
            _delegated_terminal_lifecycle_payload(
                task,
                event_name=event_name,
                parent_run_id=parent_run_id,
                level=level,
                message=message,
                payload=payload,
            ),
        )
        for event_name in ("TaskCompleted", "SubagentStop")
    ]


def worktree_lifecycle_events(
    task: AgentRunRecord,
    event: ExecutorEvent,
) -> list[tuple[str, dict[str, Any]]]:
    if event.type.value != "status":
        return []
    status = str(event.data.get("status") or "").strip()
    if status not in _WORKTREE_LIFECYCLE_STATUS:
        return []
    workdir = str(event.data.get("workdir") or task.workdir or "").strip()
    if not workdir:
        return []
    event_name, diagnostic_code, message_prefix = _WORKTREE_LIFECYCLE_STATUS[status]
    hook_id = f"agent_run_control_plane:{event_name}"
    message = f"{message_prefix}: {workdir}"
    metadata = task.metadata if isinstance(task.metadata, dict) else {}
    runtime_root = str(event.data.get("runtime_root") or metadata.get("runtime_root") or "")
    execution_location = (
        task.execution_location.value if task.execution_location else ""
    )
    path_space = (
        "agent_run_worktree"
        if execution_location == ExecutionLocation.DAEMON_WORKTREE.value
        else "agent_run_workspace"
    )
    payload = {
        "workdir": workdir,
        "runtime_working_directory": workdir,
        "runtime_workspace_root": task.workdir or workdir,
        "runtime_root": runtime_root,
        "execution_location": execution_location,
        "worker_kind": str(metadata.get("worker_kind") or ""),
        "worktree_role": str(metadata.get("worktree_role") or ""),
        "agent_id": task.agent_id,
        "agent_run_id": task.id,
        "path_space": path_space,
        "source_event": status,
    }
    return [
        (
            "lifecycle_hook",
            {
                "phase": "result",
                "event_name": event_name,
                "placement": "server",
                "session_run_id": str(task.executor_session_id or ""),
                "agent_run_id": task.id,
                "turn_id": "",
                "trigger_source": task.source.value,
                "hook_id": hook_id,
                "display_name": event_name,
                "source": "agent_run_control_plane",
                "handler_type": "internal",
                "decision": "none",
                "continue_flow": True,
                "diagnostics": [
                    {
                        "code": diagnostic_code,
                        "message": message,
                        "level": "info",
                        "event_name": event_name,
                        "handler_type": "internal",
                        "hook_id": hook_id,
                    }
                ],
                "level": "info",
                "title": event_name,
                "message": message,
                "user_message": message,
                "payload": payload,
            },
        )
    ]


def _delegated_terminal_lifecycle_payload(
    task: AgentRunRecord,
    *,
    event_name: str,
    parent_run_id: str,
    level: str,
    message: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    metadata = task.metadata if isinstance(task.metadata, dict) else {}
    session_run_id = str(metadata.get("parent_session_id") or "")
    turn_id = str(metadata.get("parent_turn_id") or "")
    hook_id = f"agent_run_control_plane:{event_name}"
    return {
        "phase": "result",
        "event_name": event_name,
        "placement": "server",
        "session_run_id": session_run_id,
        "agent_run_id": parent_run_id,
        "turn_id": turn_id,
        "trigger_source": task.source.value,
        "hook_id": hook_id,
        "display_name": event_name,
        "source": "agent_run_control_plane",
        "handler_type": "internal",
        "decision": "none",
        "continue_flow": True,
        "diagnostics": [
            {
                "code": "delegated_agent_run_terminal",
                "message": message,
                "level": level,
                "event_name": event_name,
                "handler_type": "internal",
                "hook_id": hook_id,
            }
        ],
        "level": level,
        "title": event_name,
        "message": message,
        "user_message": message,
        "payload": payload,
    }


def clamp_event_limit(
    limit: int | None,
    *,
    default: int = DEFAULT_RUNTIME_EVENT_LIMIT,
) -> int:
    if limit is None:
        return default
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_RUNTIME_EVENT_LIMIT, value))


def runtime_slot_limits(
    runtime_snapshot: dict[str, Any],
    *,
    max_running_tasks: int,
) -> dict[str, int]:
    fallback = max(1, int(max_running_tasks or 1))
    raw = runtime_snapshot.get("runtime_slots")
    slots = raw if isinstance(raw, dict) else {}
    return {
        key: max(1, int(slots.get(key, fallback) or fallback))
        for key in RUNTIME_SLOT_KEYS
    }


def runtime_slot_key_for_agent_run(task: AgentRunRecord) -> str:
    metadata = task.metadata if isinstance(task.metadata, dict) else {}
    worker_kind = str(metadata.get("worker_kind") or "").strip()
    if not worker_kind:
        location = task.execution_location
        location_value = location.value if isinstance(location, ExecutionLocation) else str(location or "")
        worker_kind = (
            WorkerKind.LOCAL_PEER.value
            if location_value == ExecutionLocation.LOCAL_WORKSPACE.value
            else WorkerKind.SERVER_WORKER.value
        )
    if worker_kind == WorkerKind.SANDBOX_WORKER.value:
        return "server_sandbox_slots"
    if worker_kind == WorkerKind.LOCAL_PEER.value:
        return "local_peer_agent_run_slots"
    return "server_agent_run_slots"


def agent_run_uses_model_request_slot(task: AgentRunRecord) -> bool:
    metadata = task.metadata if isinstance(task.metadata, dict) else {}
    origin = str(metadata.get("model_request_origin") or "").strip()
    return origin in {
        ModelRequestOrigin.SERVER.value,
        ModelRequestOrigin.SERVER_WORKER_CLI.value,
    }


def runtime_slots_allow_agent_run_claim(
    running_tasks: list[AgentRunRecord],
    candidate: AgentRunRecord,
    runtime_snapshot: dict[str, Any],
    *,
    max_running_tasks: int,
) -> bool:
    limits = runtime_slot_limits(
        runtime_snapshot,
        max_running_tasks=max_running_tasks,
    )
    slot_key = runtime_slot_key_for_agent_run(candidate)
    slot_count = sum(
        1
        for task in running_tasks
        if task.status in RUNNING_AGENT_RUN_STATUSES
        and runtime_slot_key_for_agent_run(task) == slot_key
    )
    if slot_count >= limits[slot_key]:
        return False
    if agent_run_uses_model_request_slot(candidate):
        model_count = sum(
            1
            for task in running_tasks
            if task.status in RUNNING_AGENT_RUN_STATUSES
            and agent_run_uses_model_request_slot(task)
        )
        if model_count >= limits["model_request_slots"]:
            return False
    return True


class AgentRunQueueStore(Protocol):
    max_running_tasks: int
    runtime_snapshot: dict[str, Any]

    def configure(
        self,
        *,
        max_running_tasks: int | None = None,
        runtime_snapshot: dict[str, Any] | None = None,
    ) -> None: ...

    def submit_agent_run(self, request: Any, *, task_id: str | None = None) -> AgentRunRecord: ...

    def get_agent_run(self, task_id: str) -> AgentRunRecord: ...

    def agent_run_to_dict(self, task_id: str) -> dict[str, Any]: ...

    def list_agent_runs(self, **filters: Any) -> list[dict[str, Any]]: ...

    def load_agent_run_detail(
        self,
        task_id: str,
        *,
        event_limit: int = DEFAULT_RUNTIME_EVENT_LIMIT,
    ) -> dict[str, Any]: ...


class ClaimLeaseStore(Protocol):
    def claim_agent_run(
        self,
        *,
        worker_id: str,
        worker_kind: Any | None = None,
        executors: list[Any] | None = None,
        peer_id: str | None = None,
        peer_features: list[str] | None = None,
        workspace_root: str | None = None,
        lease_sec: int = 15,
    ) -> Any | None: ...

    def heartbeat_agent_run(
        self,
        *,
        request_id: str,
        task_id: str,
        worker_id: str,
        peer_id: str | None = None,
        lease_sec: int | None = None,
    ) -> dict[str, Any]: ...

    def validate_claim_owner(
        self,
        *,
        request_id: str,
        task_id: str,
        worker_id: str,
        peer_id: str | None = None,
    ) -> tuple[bool, str]: ...

    def recover_stale_agent_runs(self, *, now: float | None = None) -> list[str]: ...


class AgentRunEventLog(Protocol):
    def append_executor_event(
        self,
        task_id: str,
        event: ExecutorEvent,
        *,
        request_id: str | None = None,
        worker_id: str | None = None,
        peer_id: str | None = None,
    ) -> tuple[bool, str]: ...

    def list_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
        limit: int = DEFAULT_RUNTIME_EVENT_LIMIT,
    ) -> list[Any]: ...


class AgentRunArtifactStore(Protocol):
    def attach_artifact(self, task_id: str, **kwargs: Any) -> TaskArtifact: ...

    def list_artifacts(self, task_id: str) -> list[TaskArtifact]: ...

    def artifacts_to_dict(self, task_id: str) -> list[dict[str, Any]]: ...


class AgentRunSessionStore(Protocol):
    def pin_session(self, task_id: str, session: TaskSessionRef) -> None: ...

    def pin_claimed_session(
        self,
        *,
        request_id: str,
        task_id: str,
        worker_id: str,
        peer_id: str | None = None,
        workdir: str | None = None,
        branch: str | None = None,
        executor_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[bool, str]: ...


class EnvironmentWorkflow(Protocol):
    def append_executor_event(
        self,
        task_id: str,
        event: ExecutorEvent,
        *,
        request_id: str | None = None,
        worker_id: str | None = None,
        peer_id: str | None = None,
    ) -> tuple[bool, str]: ...


class GitHubPRLifecycle(Protocol):
    def create_or_update_pr(self, task_id: str, *, diff: str = "") -> TaskArtifact: ...


class AgentRunStore(
    AgentRunQueueStore,
    ClaimLeaseStore,
    AgentRunEventLog,
    AgentRunArtifactStore,
    AgentRunSessionStore,
    EnvironmentWorkflow,
    GitHubPRLifecycle,
    Protocol,
):
    def complete_claimed_agent_run(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        request_id: str,
        worker_id: str,
        peer_id: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str, AgentRunRecord | None]: ...

    def complete_agent_run(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> AgentRunRecord: ...

    def retry_agent_run(
        self,
        task_id: str,
        *,
        new_agent_run_id: str | None = None,
        resume_session: bool = False,
    ) -> AgentRunRecord: ...

    def fail_agent_run(self, task_id: str, *, error: str) -> AgentRunRecord: ...

    def cancel_agent_run(self, task_id: str, *, reason: str = "user_cancelled") -> bool: ...

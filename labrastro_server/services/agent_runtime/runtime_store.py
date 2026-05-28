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

"""Storage protocols for Agent Runtime control-plane state."""

from __future__ import annotations

from typing import Any, Protocol

from reuleauxcoder.domain.agent_runtime.models import (
    TaskArtifact,
    TaskRecord,
    TaskSessionRef,
)
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)

DEFAULT_RUNTIME_EVENT_LIMIT = 200
MAX_RUNTIME_EVENT_LIMIT = 1000


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


class TaskQueueStore(Protocol):
    max_running_tasks: int
    runtime_snapshot: dict[str, Any]

    def configure(
        self,
        *,
        max_running_tasks: int | None = None,
        runtime_snapshot: dict[str, Any] | None = None,
    ) -> None: ...

    def submit_task(self, request: Any, *, task_id: str | None = None) -> TaskRecord: ...

    def get_task(self, task_id: str) -> TaskRecord: ...

    def task_to_dict(self, task_id: str) -> dict[str, Any]: ...

    def list_tasks(self, **filters: Any) -> list[dict[str, Any]]: ...

    def load_task_detail(
        self,
        task_id: str,
        *,
        event_limit: int = DEFAULT_RUNTIME_EVENT_LIMIT,
    ) -> dict[str, Any]: ...


class ClaimLeaseStore(Protocol):
    def claim_task(
        self,
        *,
        worker_id: str,
        executors: list[Any] | None = None,
        peer_id: str | None = None,
        peer_features: list[str] | None = None,
        workspace_root: str | None = None,
        lease_sec: int = 15,
    ) -> Any | None: ...

    def heartbeat_task(
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

    def recover_stale_tasks(self, *, now: float | None = None) -> list[str]: ...


class RuntimeEventLog(Protocol):
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


class RuntimeArtifactStore(Protocol):
    def attach_artifact(self, task_id: str, **kwargs: Any) -> TaskArtifact: ...

    def list_artifacts(self, task_id: str) -> list[TaskArtifact]: ...

    def artifacts_to_dict(self, task_id: str) -> list[dict[str, Any]]: ...


class RuntimeSessionStore(Protocol):
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


class RuntimeStore(
    TaskQueueStore,
    ClaimLeaseStore,
    RuntimeEventLog,
    RuntimeArtifactStore,
    RuntimeSessionStore,
    EnvironmentWorkflow,
    GitHubPRLifecycle,
    Protocol,
):
    def complete_claimed_task(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        request_id: str,
        worker_id: str,
        peer_id: str | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str, TaskRecord | None]: ...

    def complete_task(
        self,
        task_id: str,
        result: ExecutorRunResult,
        *,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> TaskRecord: ...

    def retry_task(
        self,
        task_id: str,
        *,
        new_task_id: str | None = None,
        resume_session: bool = False,
    ) -> TaskRecord: ...

    def fail_task(self, task_id: str, *, error: str) -> TaskRecord: ...

    def cancel_task(self, task_id: str, *, reason: str = "user_cancelled") -> bool: ...

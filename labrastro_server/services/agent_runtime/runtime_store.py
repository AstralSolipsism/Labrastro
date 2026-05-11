"""Storage protocols for Agent Runtime control-plane state."""

from __future__ import annotations

from typing import Any, Protocol

from reuleauxcoder.domain.agent_runtime.models import (
    TaskArtifact,
    AgentRunRecord,
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

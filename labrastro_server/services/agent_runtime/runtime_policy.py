"""Shared AgentRun runtime policy rules.

This module is the single authority for runtime-profile inference, runtime
policy validation, and worker claim matching. In-memory and Postgres-backed
stores must call these helpers instead of carrying parallel copies.
"""

from __future__ import annotations

from typing import Any

from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
    AgentRun,
    AgentRunSource,
    ExecutionLocation,
    ExecutorType,
    ModelRequestOrigin,
    PublishPolicy,
    WorkerKind,
    WorktreeRole,
)


def dict_from(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def optional_worker_kind(value: WorkerKind | str | None) -> WorkerKind | None:
    if isinstance(value, WorkerKind):
        return value
    if value is None or str(value).strip() == "":
        return None
    return WorkerKind(str(value))


def optional_model_request_origin(
    value: ModelRequestOrigin | str | None,
) -> ModelRequestOrigin | None:
    if isinstance(value, ModelRequestOrigin):
        return value
    if value is None or str(value).strip() == "":
        return None
    return ModelRequestOrigin(str(value))


def optional_worktree_role(value: WorktreeRole | str | None) -> WorktreeRole | None:
    if isinstance(value, WorktreeRole):
        return value
    if value is None or str(value).strip() == "":
        return None
    return WorktreeRole(str(value))


def optional_publish_policy(value: PublishPolicy | str | None) -> PublishPolicy | None:
    if isinstance(value, PublishPolicy):
        return value
    if value is None or str(value).strip() == "":
        return None
    return PublishPolicy(str(value))


def worker_kind_for_runtime(
    raw_profile: dict[str, Any],
    execution_location: ExecutionLocation,
) -> WorkerKind:
    explicit = optional_worker_kind(raw_profile.get("worker_kind"))
    if explicit is not None:
        return explicit
    if dict_from(raw_profile.get("sandbox")):
        return WorkerKind.SANDBOX_WORKER
    if execution_location == ExecutionLocation.LOCAL_WORKSPACE:
        return WorkerKind.LOCAL_PEER
    return WorkerKind.SERVER_WORKER


def model_request_origin_for_runtime(
    raw_profile: dict[str, Any],
    *,
    executor: ExecutorType,
    worker_kind: WorkerKind,
) -> ModelRequestOrigin:
    explicit = optional_model_request_origin(raw_profile.get("model_request_origin"))
    if explicit is not None:
        return explicit
    if executor in {ExecutorType.CODEX, ExecutorType.CLAUDE, ExecutorType.GEMINI}:
        if worker_kind == WorkerKind.LOCAL_PEER:
            return ModelRequestOrigin.LOCAL_CLI
        return ModelRequestOrigin.SERVER_WORKER_CLI
    return ModelRequestOrigin.SERVER


def server_capable_worker(worker_kind: WorkerKind) -> bool:
    return worker_kind in {WorkerKind.SERVER_WORKER, WorkerKind.SANDBOX_WORKER}


def validate_runtime_profile_model_request_origin(
    *,
    executor: ExecutorType,
    worker_kind: WorkerKind,
    model_request_origin: ModelRequestOrigin,
) -> None:
    if executor in {ExecutorType.CODEX, ExecutorType.CLAUDE, ExecutorType.GEMINI}:
        expected = (
            ModelRequestOrigin.LOCAL_CLI
            if worker_kind == WorkerKind.LOCAL_PEER
            else ModelRequestOrigin.SERVER_WORKER_CLI
        )
        if model_request_origin != expected:
            raise ValueError(
                f"{executor.value} runtime profile with {worker_kind.value} "
                f"must use model_request_origin={expected.value}"
            )
        return
    if model_request_origin != ModelRequestOrigin.SERVER:
        raise ValueError(
            f"{executor.value} runtime profile must use model_request_origin=server"
        )


def system_flow_for_source(source: AgentRunSource) -> str:
    if source == AgentRunSource.ENVIRONMENT:
        return "environment_config"
    if source == AgentRunSource.CAPABILITY_INGEST:
        return "capability_ingest"
    return source.value


def validate_agent_run_runtime_policy(
    request: Any,
    *,
    agent_config: AgentConfig | None,
) -> None:
    worker_kind = request.worker_kind or WorkerKind.LOCAL_PEER
    location = request.execution_location or ExecutionLocation.LOCAL_WORKSPACE
    validate_runtime_profile_model_request_origin(
        executor=request.executor,
        worker_kind=worker_kind,
        model_request_origin=request.model_request_origin,
    )
    if request.source == AgentRunSource.TASKFLOW and (
        not server_capable_worker(worker_kind)
        or location == ExecutionLocation.LOCAL_WORKSPACE
    ):
        raise ValueError("Taskflow agent requires a server-capable runtime profile")
    if request.source == AgentRunSource.CAPABILITY_INGEST and (
        not server_capable_worker(worker_kind)
        or location == ExecutionLocation.LOCAL_WORKSPACE
    ):
        raise ValueError(
            "capability package generation requires a server-capable runtime profile"
        )
    if request.source == AgentRunSource.ENVIRONMENT and (
        worker_kind != WorkerKind.LOCAL_PEER
        or location != ExecutionLocation.LOCAL_WORKSPACE
    ):
        raise ValueError(
            "environment configuration requires a local peer runtime profile"
        )
    if (
        request.source == AgentRunSource.TASKFLOW
        and agent_config is not None
        and not agent_config.can_run_taskflow
    ):
        raise ValueError(f"agent {request.agent_id} is not eligible for Taskflow")


def workspace_key(value: str | None) -> str:
    return str(value or "").strip().replace("\\", "/").rstrip("/").lower()


def worker_kind_from_features(
    features: set[str] | None,
    *,
    location: ExecutionLocation,
) -> WorkerKind | None:
    if not features:
        return None
    for kind in WorkerKind:
        if f"worker_kind:{kind.value}" in features:
            return kind
    if (
        "agent_runs.local_workspace" in features
        and location == ExecutionLocation.LOCAL_WORKSPACE
    ):
        return WorkerKind.LOCAL_PEER
    if (
        location != ExecutionLocation.LOCAL_WORKSPACE
        and f"agent_runs.{location.value}" in features
    ):
        return WorkerKind.SERVER_WORKER
    return None


def worker_matches_agent_run(
    task: AgentRun,
    *,
    worker_kind: WorkerKind | None,
    features: set[str] | None,
    workspace_root: str | None,
) -> bool:
    location = task.execution_location or ExecutionLocation.LOCAL_WORKSPACE
    expected_worker = optional_worker_kind(task.metadata.get("worker_kind"))
    if expected_worker is None:
        expected_worker = (
            WorkerKind.LOCAL_PEER
            if location == ExecutionLocation.LOCAL_WORKSPACE
            else WorkerKind.SERVER_WORKER
        )
    effective_worker = worker_kind or worker_kind_from_features(
        features,
        location=location,
    )
    if effective_worker is not None and effective_worker != expected_worker:
        return False
    if features is None:
        bound_workspace = str(task.metadata.get("workspace_root") or "").strip()
        if location == ExecutionLocation.LOCAL_WORKSPACE and bound_workspace:
            return bool(workspace_root) and (
                workspace_key(bound_workspace) == workspace_key(workspace_root)
            )
        return True
    if effective_worker is None and features is not None:
        return False
    if location == ExecutionLocation.LOCAL_WORKSPACE:
        if expected_worker != WorkerKind.LOCAL_PEER:
            return False
        if "agent_runs.local_workspace" not in features:
            return False
        bound_workspace = str(task.metadata.get("workspace_root") or "").strip()
        if bound_workspace:
            return bool(workspace_root) and (
                workspace_key(bound_workspace) == workspace_key(workspace_root)
            )
        return True
    if expected_worker == WorkerKind.SANDBOX_WORKER:
        return effective_worker == WorkerKind.SANDBOX_WORKER
    if expected_worker != WorkerKind.SERVER_WORKER:
        return False
    if features is None:
        return worker_kind == WorkerKind.SERVER_WORKER
    location_feature = f"agent_runs.{location.value}"
    return location_feature in features

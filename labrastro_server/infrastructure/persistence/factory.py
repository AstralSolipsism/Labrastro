"""Persistence factory functions for optional Postgres-backed stores."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reuleauxcoder.domain.config.models import Config, PersistenceConfig
from labrastro_server.infrastructure.persistence.db import create_postgres_engine
from labrastro_server.infrastructure.persistence.migration import run_migrations
from labrastro_server.infrastructure.persistence.postgres_session_store import (
    PostgresSessionStore,
)
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from labrastro_server.services.agent_runtime.control_plane import AgentRuntimeControlPlane
from labrastro_server.services.agent_runtime.postgres_store import PostgresRuntimeStore
from labrastro_server.services.auth.file_store import FileAuthStore
from labrastro_server.services.auth.postgres_store import PostgresAuthStore
from labrastro_server.services.collaboration.in_memory_store import (
    InMemoryIssueAssignmentStore,
)
from labrastro_server.services.collaboration.postgres_store import (
    PostgresIssueAssignmentStore,
)
from labrastro_server.services.collaboration.service import IssueAssignmentService
from labrastro_server.services.github.auth import GitHubInstallationTokenProvider
from labrastro_server.services.github.client import GitHubClient
from labrastro_server.services.github.postgres_store import PostgresGitHubStore
from labrastro_server.services.github.service import PullRequestService
from labrastro_server.services.taskflow.in_memory_store import InMemoryTaskflowStore
from labrastro_server.services.taskflow.postgres_store import PostgresTaskflowStore
from labrastro_server.services.taskflow.service import TaskflowService


def should_use_postgres(persistence: PersistenceConfig) -> bool:
    if persistence.backend == "memory":
        return False
    if persistence.backend == "postgres":
        return True
    return bool(persistence.database_url)


def _engine_for(config: Config) -> Any | None:
    persistence = config.persistence
    if not should_use_postgres(persistence):
        return None
    if not persistence.database_url:
        raise RuntimeError("persistence.database_url is required for Postgres backend")
    if persistence.auto_migrate:
        run_migrations(persistence.database_url)
    return create_postgres_engine(persistence.database_url)


def create_runtime_control_plane(config: Config) -> AgentRuntimeControlPlane:
    engine = _engine_for(config)
    if engine is None or not config.persistence.runtime_enabled:
        return AgentRuntimeControlPlane(
            max_running_tasks=config.agent_runtime.max_running_agents,
            runtime_snapshot=config.agent_runtime.to_runtime_snapshot(),
        )
    store = PostgresRuntimeStore(
        engine,
        max_running_tasks=config.agent_runtime.max_running_agents,
        runtime_snapshot=config.agent_runtime.to_runtime_snapshot(),
    )
    return AgentRuntimeControlPlane(
        max_running_tasks=config.agent_runtime.max_running_agents,
        runtime_snapshot=config.agent_runtime.to_runtime_snapshot(),
        store=store,
    )


def create_taskflow_service(
    config: Config, *, runtime_control_plane: AgentRuntimeControlPlane | None = None
) -> TaskflowService:
    engine = _engine_for(config)
    store = PostgresTaskflowStore(engine) if engine is not None else InMemoryTaskflowStore()
    return TaskflowService(store, runtime_control_plane=runtime_control_plane)


def create_issue_assignment_service(
    config: Config, *, taskflow_service: TaskflowService
) -> IssueAssignmentService:
    engine = _engine_for(config)
    store = (
        PostgresIssueAssignmentStore(engine)
        if engine is not None
        else InMemoryIssueAssignmentStore()
    )
    return IssueAssignmentService(store, taskflow_service=taskflow_service)


def create_github_pull_request_service(
    config: Config,
    *,
    runtime_control_plane: AgentRuntimeControlPlane,
    issue_assignment_service: IssueAssignmentService | None = None,
) -> PullRequestService | None:
    if not config.github.enabled:
        return None
    engine = _engine_for(config)
    if engine is None:
        raise RuntimeError("github.enabled=true requires Postgres persistence")
    token_provider = GitHubInstallationTokenProvider(config.github)
    client = GitHubClient(config.github, token_provider=token_provider)
    return PullRequestService(
        config=config.github,
        store=PostgresGitHubStore(engine),
        client=client,
        runtime_control_plane=runtime_control_plane,
        issue_assignment_service=issue_assignment_service,
    )


def create_auth_store(config: Config) -> Any:
    backend = str(config.auth.store_backend or "auto")
    use_postgres = backend == "postgres" or (
        backend == "auto" and should_use_postgres(config.persistence)
    )
    if use_postgres:
        engine = _engine_for(config)
        if engine is None:
            raise RuntimeError("auth.store_backend=postgres requires Postgres persistence")
        return PostgresAuthStore(engine)
    store_path = Path(config.auth.store_path).expanduser()
    if not store_path.is_absolute():
        store_path = Path.cwd() / store_path
    return FileAuthStore(store_path)


def create_session_store(config: Config, sessions_dir: Path | None) -> Any:
    engine = _engine_for(config)
    if engine is None or not config.persistence.sessions_enabled:
        return SessionStore(sessions_dir)
    return PostgresSessionStore(engine)


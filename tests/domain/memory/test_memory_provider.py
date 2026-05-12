from __future__ import annotations

import pytest

from reuleauxcoder.domain.memory import (
    MemoryCaptureEvent,
    MemoryItem,
    MemoryProvider,
    MemoryProvideRequest,
    MemoryQuery,
    MemoryScope,
    SQLiteMemoryRepository,
)


def _scope(
    agent_id: str,
    *,
    project_id: str = "project-1",
    workspace_id: str = "workspace-1",
) -> MemoryScope:
    return MemoryScope(
        owner_agent_id=agent_id,
        memory_namespace=agent_id,
        project_id=project_id,
        workspace_id=workspace_id,
    )


def test_memory_scope_requires_owner_agent_id() -> None:
    with pytest.raises(ValueError, match="owner_agent_id"):
        MemoryScope(owner_agent_id="", memory_namespace="agent-a")


def test_sqlite_repository_retrieval_never_crosses_agent_boundary(tmp_path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    scope_a = _scope("agent-a")
    scope_b = _scope("agent-b")

    repo.upsert(
        MemoryItem.create(
            scope=scope_a,
            type="project",
            content="Agent A believes the service uses FastAPI.",
            abstract="FastAPI service",
        )
    )
    repo.upsert(
        MemoryItem.create(
            scope=scope_b,
            type="project",
            content="Agent B believes the service uses Flask.",
            abstract="Flask service",
        )
    )

    results_a = repo.search(scope_a, MemoryQuery(query="service", limit=10))
    results_b = repo.search(scope_b, MemoryQuery(query="service", limit=10))

    assert [item.owner_agent_id for item in results_a] == ["agent-a"]
    assert "Flask" not in results_a[0].content
    assert [item.owner_agent_id for item in results_b] == ["agent-b"]
    assert "FastAPI" not in results_b[0].content


def test_repository_scope_version_is_local_to_agent_namespace(tmp_path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    scope_a = _scope("agent-a")
    scope_b = _scope("agent-b")

    assert repo.scope_version(scope_a) == 0
    assert repo.scope_version(scope_b) == 0

    repo.upsert(
        MemoryItem.create(
            scope=scope_a,
            type="preference",
            content="Prefer async tests.",
            abstract="Async tests",
        )
    )

    assert repo.scope_version(scope_a) == 1
    assert repo.scope_version(scope_b) == 0


def test_sqlite_repository_keeps_same_agent_projects_isolated(tmp_path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    project_one = _scope("agent-a", project_id="project-1")
    project_two = _scope("agent-a", project_id="project-2")

    repo.upsert(
        MemoryItem.create(
            scope=project_one,
            type="project",
            content="Project one uses FastAPI.",
            abstract="Project one stack",
        )
    )
    repo.upsert(
        MemoryItem.create(
            scope=project_two,
            type="project",
            content="Project two uses Flask.",
            abstract="Project two stack",
        )
    )

    results = repo.search(project_one, MemoryQuery(query="project", limit=10))

    assert [item.project_id for item in results] == ["project-1"]
    assert "Flask" not in results[0].content


def test_memory_provider_enqueues_capture_jobs_idempotently_per_scope(tmp_path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    provider = MemoryProvider(repo)
    scope = _scope("agent-a")
    event = MemoryCaptureEvent(
        kind="session_save",
        payload={
            "session_id": "session-1",
            "messages": [{"role": "user", "content": "hi"}],
        },
        idempotency_key="session-save:session-1",
    )

    first = provider.capture(scope, event)
    second = provider.capture(scope, event)
    jobs = repo.list_capture_jobs(scope)

    assert first.job_id == second.job_id
    assert len(jobs) == 1
    assert jobs[0].owner_agent_id == "agent-a"
    assert jobs[0].payload["session_id"] == "session-1"


def test_memory_provider_bundle_cache_invalidates_when_scope_version_changes(
    tmp_path,
) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    provider = MemoryProvider(repo)
    scope = _scope("agent-a")
    request = MemoryProvideRequest(query="tests", token_budget=200)

    assert provider.provide(scope, request).items == []

    repo.upsert(
        MemoryItem.create(
            scope=scope,
            type="pattern",
            content="Run pytest before reporting completion.",
            abstract="Verification pattern",
        )
    )

    bundle = provider.provide(scope, request)

    assert [item.abstract for item in bundle.items] == ["Verification pattern"]
    assert bundle.provenance["scope_version"] == 1

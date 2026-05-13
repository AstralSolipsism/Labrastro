from __future__ import annotations

from types import SimpleNamespace

from reuleauxcoder.domain.memory import (
    MemoryItem,
    MemoryProvideRequest,
    MemoryProvider,
    MemoryScope,
    SQLiteMemoryRepository,
)
from reuleauxcoder.domain.memory.runtime import (
    GLOBAL_MEMORY_PROJECT_ID,
    MAIN_CHAT_MEMORY_NAMESPACE,
    bind_main_chat_memory_scope_to_agent,
    memory_metadata_from_agent,
)


def _account_scope(project_id: str = GLOBAL_MEMORY_PROJECT_ID) -> MemoryScope:
    return MemoryScope(
        owner_agent_id="account:u1",
        memory_namespace=MAIN_CHAT_MEMORY_NAMESPACE,
        project_id=project_id,
    )


def test_main_chat_scope_uses_account_identity_without_workspace_fallback() -> None:
    agent = SimpleNamespace(runtime_working_directory="/tmp/workspace")
    peer = SimpleNamespace(
        meta={
            "auth_principal": {
                "user_id": "u1",
                "username": "alice",
                "device_id": "d1",
            }
        }
    )

    assert bind_main_chat_memory_scope_to_agent(agent, peer_info=peer) is True

    metadata = memory_metadata_from_agent(agent)
    assert metadata["owner_agent_id"] == "account:u1"
    assert metadata["memory_namespace"] == MAIN_CHAT_MEMORY_NAMESPACE
    assert metadata["project_id"] == GLOBAL_MEMORY_PROJECT_ID
    assert "workspace_id" not in metadata
    assert "repo_id" not in metadata


def test_main_chat_scope_falls_back_when_peer_has_no_account() -> None:
    agent = SimpleNamespace(runtime_working_directory="/tmp/workspace")
    peer = SimpleNamespace(meta={})

    assert bind_main_chat_memory_scope_to_agent(agent, peer_info=peer) is False


def test_main_chat_global_scope_reads_only_global_memory(tmp_path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    provider = MemoryProvider(repo)
    repo.upsert(
        MemoryItem.create(
            scope=_account_scope(),
            type="preference",
            abstract="global",
            content="memory global",
        )
    )
    repo.upsert(
        MemoryItem.create(
            scope=_account_scope("project-a"),
            type="project",
            abstract="project",
            content="memory project",
        )
    )

    bundle = provider.provide(
        _account_scope(),
        MemoryProvideRequest(query="memory", limit=8, token_budget=800),
    )

    assert [item.project_id for item in bundle.items] == [GLOBAL_MEMORY_PROJECT_ID]


def test_main_chat_project_scope_reads_global_current_and_related_projects(tmp_path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    provider = MemoryProvider(repo)
    for project_id, content in [
        (GLOBAL_MEMORY_PROJECT_ID, "memory global"),
        ("project-a", "memory current"),
        ("project-b", "memory related"),
    ]:
        repo.upsert(
            MemoryItem.create(
                scope=_account_scope(project_id),
                type="note",
                abstract=project_id,
                content=content,
            )
        )

    bundle = provider.provide(
        _account_scope("project-a"),
        MemoryProvideRequest(query="memory", limit=8, token_budget=800),
    )

    assert [item.project_id for item in bundle.items] == [
        GLOBAL_MEMORY_PROJECT_ID,
        "project-a",
        "project-b",
    ]


def test_non_main_chat_agent_keeps_strict_agent_project_scope(tmp_path) -> None:
    repo = SQLiteMemoryRepository(tmp_path / "memory.sqlite3")
    provider = MemoryProvider(repo)
    project_a = MemoryScope(
        owner_agent_id="agent-a", memory_namespace="agent-a", project_id="project-a"
    )
    project_b = MemoryScope(
        owner_agent_id="agent-a", memory_namespace="agent-a", project_id="project-b"
    )
    repo.upsert(
        MemoryItem.create(
            scope=project_a,
            type="note",
            abstract="project-a",
            content="memory current",
        )
    )
    repo.upsert(
        MemoryItem.create(
            scope=project_b,
            type="note",
            abstract="project-b",
            content="memory related",
        )
    )

    bundle = provider.provide(
        project_a,
        MemoryProvideRequest(query="memory", limit=8, token_budget=800),
    )

    assert [item.project_id for item in bundle.items] == ["project-a"]

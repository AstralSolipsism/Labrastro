from __future__ import annotations

from types import SimpleNamespace

from reuleauxcoder.domain.memory.runtime import (
    GLOBAL_MEMORY_PROJECT_ID,
    MAIN_CHAT_MEMORY_NAMESPACE,
    bind_main_chat_memory_scope_to_agent,
    memory_metadata_from_agent,
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


def test_memory_metadata_includes_agent_memory_policy_when_configured() -> None:
    policy = SimpleNamespace(
        to_dict=lambda: {
            "primary_provider": "agentmemory",
            "read_providers": ["agentmemory"],
            "inject": True,
            "capture": False,
        }
    )
    config = SimpleNamespace(
        memory=SimpleNamespace(default_agent_id="core", default_namespace=""),
        agent_registry=SimpleNamespace(
            agents={"reviewer": SimpleNamespace(memory=policy)}
        ),
    )
    agent = SimpleNamespace(
        runtime_config=config,
        agent_config_id="reviewer",
        agent_id="reviewer",
    )

    metadata = memory_metadata_from_agent(agent)

    assert metadata["owner_agent_id"] == "reviewer"
    assert metadata["memory_policy"]["primary_provider"] == "agentmemory"
    assert metadata["memory_policy"]["capture"] is False

from __future__ import annotations

from copy import deepcopy
import os

from labrastro_server.services.admin import service as admin_service
from labrastro_server.services.admin.service import RemoteAdminConfigManager
from labrastro_server.services.capability_packages import CapabilityPackageInstallResult
from reuleauxcoder.domain.agent_runtime.models import CapabilityPackageConfig
from reuleauxcoder.domain.config.models import (
    build_agent_run_snapshot,
    resolve_agent_effective_capability_scope,
)
from reuleauxcoder.domain.memory.registry import MemoryProviderRegistry, MemorySourceRegistry
from reuleauxcoder.interfaces.entrypoint.runner import build_capability_catalog
from reuleauxcoder.services.config.loader import ConfigLoader


def _model_config() -> dict:
    return {
        "providers": {
            "items": {
                "openai": {
                    "type": "openai_chat",
                    "api_key": "key",
                }
            }
        },
        "models": {
            "active_main": "main",
            "profiles": {
                "main": {
                    "provider": "openai",
                    "model": "gpt",
                    "max_tokens": 8192,
                    "max_context_tokens": 128000,
                }
            },
        },
    }


def _apply_install_candidate(
    manager: RemoteAdminConfigManager,
    payload: dict[str, object],
) -> admin_service.AdminConfigResult:
    built = manager.build_capability_install_candidate(payload)
    if not built.ok:
        return built
    return manager.apply_capability_install_candidate(
        {
            "candidate_id": built.payload["candidate_id"],
            "candidate_hash": built.payload["candidate_hash"],
        }
    )


def _effective_tool_refs(snapshot: dict, agent_id: str) -> list[str]:
    effective = snapshot["agents"][agent_id]["effective_capabilities"]
    assert "tools" not in effective
    return [item["target_tool_ref"] for item in effective["tool_specs"]]


def test_parse_config_reads_agent_registry_profiles_and_limits() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "run_limits": {
                "max_running_agents": 8,
                "max_shells_per_agent": 2,
                "server_agent_run_slots": 6,
                "server_sandbox_slots": 3,
                "local_peer_agent_run_slots": 2,
                "model_request_slots": 7,
            },
            "runtime_profiles": {
                "codex_remote": {
                    "executor": "codex",
                    "execution_location": "remote_server",
                    "model": "gpt-5.2-codex",
                    "command": "codex",
                    "runtime_home_policy": "per_task",
                    "config_isolation": "per_agent",
                    "credential_refs": {
                        "model": "cred_codex_team",
                        "git": "cred_github_repo_writer",
                    },
                }
            },
            "agent_registry": {
                "agents": {
                    "code_reviewer": {
                        "name": "Code Reviewer",
                        "runtime_profile": "codex_remote",
                        "dispatch": {
                            "profile": "Best for repository review and regression risk analysis.",
                            "examples": ["Review backend changes"],
                            "avoid": ["Deploy production services"],
                        },
                        "capability_refs": ["github-review"],
                        "max_concurrent_tasks": 2,
                    }
                },
            },
            "capability_packages": {
                "github-review": {
                    "name": "GitHub Review",
                    "components": ["mcp:github"],
                    "permissions": ["repo.read"],
                }
            },
            "capability_components": {
                "mcp:github": {
                    "kind": "mcp",
                    "name": "github",
                    "config": {"command": "github-mcp-server"},
                },
            },
        }
    )

    assert config.run_limits.max_running_agents == 8
    assert config.run_limits.max_shells_per_agent == 2
    assert config.run_limits.server_agent_run_slots == 6
    assert config.run_limits.server_sandbox_slots == 3
    assert config.run_limits.local_peer_agent_run_slots == 2
    assert config.run_limits.model_request_slots == 7
    removed_slot = "local_" + "environment_action_slots"
    assert removed_slot not in config.run_limits.to_dict()
    assert config.runtime_profiles.profiles["codex_remote"].executor.value == "codex"
    assert (
        config.runtime_profiles.profiles["codex_remote"]
        .execution_location.value
        == "remote_server"
    )
    assert (
        config.runtime_profiles.profiles["codex_remote"].credential_refs["model"]
        == "cred_codex_team"
    )
    assert config.agent_registry.agents["code_reviewer"].runtime_profile == "codex_remote"
    assert (
        config.agent_registry.agents["code_reviewer"].dispatch.profile
        == "Best for repository review and regression risk analysis."
    )
    assert config.agent_registry.agents["code_reviewer"].capability_refs == [
        "github-review",
    ]
    assert "permissions" not in config.capability_packages["github-review"].to_dict()
    assert config.capability_packages["github-review"].components == ["mcp:github"]
    assert config.capability_components["mcp:github"].name == "github"
    assert "main_chat" in config.agent_registry.agents
    assert config.agent_registry.agents["main_chat"].chat_entrypoint is True
    assert config.agent_registry.agents["main_chat"].can_delegate is False
    assert "core_builtin_tools" in config.capability_packages
    assert config.capability_packages["core_builtin_tools"].effective_capabilities
    assert "builtin_tool:tool_search" in config.capability_components
    assert "builtin_tool:capability_execute" in config.capability_components
    assert "builtin_tool:fetch_capabilities" in config.capability_components
    assert "environment_local" in config.runtime_profiles.profiles
    assert "agent_remote" in config.runtime_profiles.profiles
    assert "environment_configurator" in config.agent_registry.agents
    assert "capability_packager_remote" in config.runtime_profiles.profiles
    packager_profile = config.runtime_profiles.profiles["capability_packager_remote"]
    assert packager_profile.worker_kind.value == "sandbox_worker"
    assert packager_profile.worktree_role.value == "source"
    assert packager_profile.publish_policy.value == "never"
    assert packager_profile.sandbox == {}
    assert packager_profile.timeout_sec == 86400
    assert packager_profile.step_timeout_sec == 3600
    assert "capability_packager" in config.agent_registry.agents


def test_parse_config_forces_capability_packager_sandbox_worker_profile() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "runtime_profiles": {
                "capability_packager_remote": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "server_worker",
                    "sandbox": {"workspace_volume_prefix": "old-workspaces"},
                }
            },
        }
    )

    profile = config.runtime_profiles.profiles["capability_packager_remote"]
    assert profile.executor.value == "reuleauxcoder"
    assert profile.execution_location.value == "remote_server"
    assert profile.worker_kind.value == "sandbox_worker"
    assert profile.model_request_origin.value == "server"
    assert profile.worktree_role.value == "source"
    assert profile.publish_policy.value == "never"
    assert profile.sandbox == {}
    assert profile.timeout_sec == 86400
    assert profile.step_timeout_sec == 3600


def test_parse_config_injects_environment_configurator_by_default() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "runtime_profiles": {
                "codex_remote": {
                    "executor": "codex",
                    "execution_location": "remote_server",
                }
            },
            "agent_registry": {
                "agents": {},
            },
        }
    )

    profile = config.runtime_profiles.profiles["environment_local"]
    assert profile.executor.value == "reuleauxcoder"
    assert profile.execution_location.value == "local_workspace"
    assert profile.runtime_home_policy == "per_task"
    assert profile.approval_mode == "full"
    main_agent = config.agent_registry.agents["main_chat"]
    assert main_agent.visibility == "user"
    assert main_agent.chat_entrypoint is True
    assert main_agent.can_delegate is False
    assert main_agent.can_run_taskflow is False
    assert main_agent.runtime_profile == "agent_remote"
    assert main_agent.capability_refs == ["core_builtin_tools"]
    assert "read_file" in {
        component.name for component in config.capability_components.values()
    }
    agent = config.agent_registry.agents["environment_configurator"]
    assert agent.runtime_profile == "environment_local"
    assert agent.visibility == "system"
    assert agent.can_delegate is False
    assert agent.can_run_taskflow is False
    assert agent.allows_system_flow("environment_config") is True
    assert "environment manifest" in agent.dispatch.profile
    assert agent.capability_refs == ["environment"]
    packager = config.agent_registry.agents["capability_packager"]
    assert packager.runtime_profile == "capability_packager_remote"
    assert packager.visibility == "internal"
    assert packager.can_delegate is False
    assert packager.can_run_taskflow is False
    assert packager.allows_system_flow("capability_ingest") is True
    assert "structured draft" in packager.dispatch.profile
    assert packager.capability_refs == ["capability_packager_builtin_tools"]
    assert "environment" in config.capability_packages
    assert config.capability_packages["environment"].components == ["builtin_tool:shell"]
    assert (
        config.capability_packages["capability_packager_builtin_tools"].components
        == [
            "builtin_tool:fetch_capabilities",
            "builtin_tool:glob",
            "builtin_tool:grep",
            "builtin_tool:list_file",
            "builtin_tool:read_file",
        ]
    )
    assert "permissions" not in config.capability_packages["environment"].to_dict()


def test_default_system_agents_have_explicit_effective_tool_scopes() -> None:
    config = ConfigLoader()._parse_config(_model_config())

    snapshot = build_agent_run_snapshot(
        agent_registry=config.agent_registry,
        runtime_profiles=config.runtime_profiles,
        run_limits=config.run_limits,
        capability_packages=config.capability_packages,
        capability_components=config.capability_components,
    )

    environment_tools = _effective_tool_refs(snapshot, "environment_configurator")
    packager_tools = _effective_tool_refs(snapshot, "capability_packager")
    main_tools = _effective_tool_refs(snapshot, "main_chat")

    assert environment_tools == ["builtin:shell"]
    assert "builtin:apply_patch" not in environment_tools
    assert "builtin:apply_patch" not in environment_tools
    assert "builtin:delegate_agent" not in environment_tools
    assert packager_tools == [
        "builtin:fetch_capabilities",
        "builtin:glob",
        "builtin:grep",
        "builtin:list_file",
        "builtin:read_file",
    ]
    assert "builtin:shell" not in packager_tools
    assert "builtin:apply_patch" not in packager_tools
    assert "builtin:apply_patch" not in packager_tools
    assert "builtin:delegate_agent" not in packager_tools
    assert "builtin:tool_search" in main_tools
    assert "builtin:capability_execute" in main_tools


def test_builtin_capability_packages_are_forced_enabled() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "capability_packages": {
                "environment": {"enabled": False, "components": []},
                "core_builtin_tools": {"enabled": False},
                "capability_packager_builtin_tools": {
                    "enabled": False,
                    "components": [],
                },
            },
        }
    )

    assert config.capability_packages["environment"].enabled is True
    assert config.capability_packages["environment"].components == [
        "builtin_tool:shell"
    ]
    assert config.capability_packages["core_builtin_tools"].enabled is True
    assert (
        config.capability_packages["capability_packager_builtin_tools"].enabled
        is True
    )
    assert config.capability_packages["capability_packager_builtin_tools"].components == [
        "builtin_tool:fetch_capabilities",
        "builtin_tool:glob",
        "builtin_tool:grep",
        "builtin_tool:list_file",
        "builtin_tool:read_file",
    ]

    snapshot = build_agent_run_snapshot(
        agent_registry=config.agent_registry,
        runtime_profiles=config.runtime_profiles,
        run_limits=config.run_limits,
        capability_packages=config.capability_packages,
        capability_components=config.capability_components,
    )
    assert _effective_tool_refs(snapshot, "environment_configurator") == [
        "builtin:shell"
    ]
    assert _effective_tool_refs(snapshot, "capability_packager") == [
        "builtin:fetch_capabilities",
        "builtin:glob",
        "builtin:grep",
        "builtin:list_file",
        "builtin:read_file",
    ]


def test_builtin_capability_components_are_forced_enabled_and_restored() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "capability_components": {
                "builtin_tool:shell": {
                    "kind": "skill",
                    "name": "disabled-shell",
                    "enabled": False,
                    "description": "broken",
                    "risk_level": "low",
                    "execution_policy": "deny",
                    "registry_path": "builtin:disabled_shell",
                    "package_ids": [],
                },
                "builtin_tool:fetch_capabilities": {
                    "kind": "mcp",
                    "name": "fetch-capabilities-broken",
                    "enabled": False,
                    "registry_path": "mcp:fetch-capabilities-broken",
                    "package_ids": [],
                },
            },
        }
    )

    shell = config.capability_components["builtin_tool:shell"]
    assert shell.enabled is True
    assert shell.kind == "builtin_tool"
    assert shell.name == "shell"
    assert shell.registry_path == "builtin:shell"
    assert shell.execution_policy == "inherit"
    assert shell.risk_level == "high"
    assert "environment" in shell.package_ids

    fetch = config.capability_components["builtin_tool:fetch_capabilities"]
    assert fetch.enabled is True
    assert fetch.kind == "builtin_tool"
    assert fetch.name == "fetch_capabilities"
    assert fetch.registry_path == "builtin:fetch_capabilities"
    assert fetch.execution_policy == "allow"
    assert set(fetch.package_ids) == {
        "core_builtin_tools",
        "capability_packager_builtin_tools",
    }

    snapshot = build_agent_run_snapshot(
        agent_registry=config.agent_registry,
        runtime_profiles=config.runtime_profiles,
        run_limits=config.run_limits,
        capability_packages=config.capability_packages,
        capability_components=config.capability_components,
    )

    assert _effective_tool_refs(snapshot, "environment_configurator") == [
        "builtin:shell"
    ]
    assert _effective_tool_refs(snapshot, "capability_packager") == [
        "builtin:fetch_capabilities",
        "builtin:glob",
        "builtin:grep",
        "builtin:list_file",
        "builtin:read_file",
    ]


def test_merge_dicts_merges_agent_registry_maps_by_id() -> None:
    loader = ConfigLoader()

    merged = loader._merge_dicts(
        {
            "runtime_profiles": {
                "codex_remote": {
                    "executor": "codex",
                    "model": "old-model",
                    "credential_refs": {"model": "cred-old"},
                }
            },
            "agent_registry": {
                "agents": {
                    "reviewer": {
                        "runtime_profile": "codex_remote",
                        "dispatch": {"profile": "Old review profile"},
                    }
                },
            }
        },
        {
            "runtime_profiles": {
                "codex_remote": {
                    "model": "new-model",
                    "credential_refs": {"git": "cred-git"},
                },
                "claude_remote": {"executor": "claude"},
            },
            "agent_registry": {
                "agents": {
                    "reviewer": {"dispatch": {"profile": "New review profile"}},
                    "builder": {"runtime_profile": "claude_remote"},
                },
            }
        },
    )

    codex = merged["runtime_profiles"]["codex_remote"]
    assert codex["executor"] == "codex"
    assert codex["model"] == "new-model"
    assert codex["credential_refs"] == {"model": "cred-old", "git": "cred-git"}
    assert "claude_remote" in merged["runtime_profiles"]
    assert merged["agent_registry"]["agents"]["reviewer"]["dispatch"]["profile"] == "New review profile"
    assert "builder" in merged["agent_registry"]["agents"]


def test_agent_run_snapshot_keeps_credential_refs_but_not_plaintext_secrets() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "runtime_profiles": {
                "codex_remote": {
                    "executor": "codex",
                    "credential_refs": {"model": "cred_codex_team"},
                }
            },
            "agent_registry": {
                "agents": {
                    "reviewer": {
                        "runtime_profile": "codex_remote",
                        "credential_refs": {"github": "cred_repo_writer"},
                    }
                },
            },
        }
    )

    snapshot = build_agent_run_snapshot(
        agent_registry=config.agent_registry,
        runtime_profiles=config.runtime_profiles,
        run_limits=config.run_limits,
        capability_packages=config.capability_packages,
        capability_components=config.capability_components,
    )

    assert "cred_codex_team" in str(snapshot)
    assert "cred_repo_writer" in str(snapshot)
    assert snapshot["runtime_slots"]["server_agent_run_slots"] == 4
    assert snapshot["runtime_slots"]["local_peer_agent_run_slots"] == 1
    removed_slot = "local_" + "environment_action_slots"
    assert removed_slot not in snapshot["runtime_slots"]
    assert "OPENAI_API_KEY" not in str(snapshot)
    assert "sk-" not in str(snapshot)


def test_agent_run_snapshot_resolves_capability_component_overlay() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "runtime_profiles": {
                "codex_remote": {
                    "executor": "codex",
                    "credential_refs": {"model": "cred_codex_team"},
                }
            },
            "agent_registry": {
                "agents": {
                    "reviewer": {
                        "runtime_profile": "codex_remote",
                        "capability_refs": ["github-review"],
                        "dispatch": {"profile": "Reviews GitHub pull requests."},
                    }
                },
            },
            "capability_packages": {
                "github-review": {
                    "name": "GitHub Review",
                    "components": [
                        "mcp:github",
                        "skill:code-review",
                        "envreq:executable:gh",
                    ],
                    "source": {
                        "type": "github_repo",
                        "url": "https://github.com/example/review-tools",
                    },
                }
            },
            "capability_components": {
                "mcp:github": {
                    "kind": "mcp",
                    "name": "github",
                    "config": {"command": "github-mcp-server"},
                },
                "skill:code-review": {
                    "kind": "skill",
                    "name": "code-review",
                    "config": {"path_hint": "/skills/code-review"},
                },
                "envreq:executable:gh": {
                    "kind": "environment_requirement",
                    "name": "gh",
                    "config": {
                        "kind": "executable",
                        "command": "gh",
                        "check": "gh --version",
                        "env": {"GH_CONFIG_DIR": ".gh"},
                    },
                },
            },
        }
    )

    snapshot = build_agent_run_snapshot(
        agent_registry=config.agent_registry,
        runtime_profiles=config.runtime_profiles,
        run_limits=config.run_limits,
        capability_packages=config.capability_packages,
        capability_components=config.capability_components,
    )

    resolved = snapshot["agents"]["reviewer"]["resolved_capabilities"]
    assert resolved["mcp_servers"] == ["github"]
    assert resolved["skills"] == ["code-review"]
    assert resolved["environment_requirements"][0]["id"] == "envreq:executable:gh"
    assert resolved["environment_requirements"][0]["kind"] == "executable"
    assert resolved["capability_overlay"]["component_ids"] == [
        "mcp:github",
        "skill:code-review",
        "envreq:executable:gh",
    ]
    assert resolved["capability_overlay"]["mcp"]["servers"]["github"]["command"] == "github-mcp-server"
    assert resolved["capability_overlay"]["skill_roots"] == ["/skills/code-review"]
    assert resolved["capability_overlay"]["env"] == {"GH_CONFIG_DIR": ".gh"}
    catalog = build_capability_catalog(config)
    assert "`github-review`" in catalog
    assert "`envreq:executable:gh` [environment_requirement] gh" in catalog
    assert "command `gh`" in catalog


def test_agent_effective_capability_scope_excludes_disabled_package_runtime_resources() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "runtime_profiles": {"agent_remote": {"executor": "reuleauxcoder"}},
            "agent_registry": {
                "agents": {
                    "reviewer": {
                        "runtime_profile": "agent_remote",
                        "capability_refs": ["review", "disabled-review", "inactive-review"],
                    }
                }
            },
            "capability_packages": {
                "review": {
                    "enabled": True,
                    "components": [
                        "mcp:github",
                        "skill:code-review",
                        "envreq:executable:gh",
                    ],
                },
                "disabled-review": {
                    "enabled": False,
                    "components": [
                        "mcp:disabled-github",
                        "skill:disabled-review",
                        "envreq:executable:disabled-gh",
                    ],
                },
                "inactive-review": {
                    "enabled": True,
                    "status": "installed",
                    "state": {"activation_state": "inactive"},
                    "components": [
                        "skill:inactive-review",
                    ],
                },
            },
            "capability_components": {
                "mcp:github": {
                    "kind": "mcp",
                    "name": "github",
                    "config": {"command": "github-mcp"},
                },
                "skill:code-review": {
                    "kind": "skill",
                    "name": "code-review",
                    "config": {"path_hint": "/srv/skills/packages/review/code-review/SKILL.md"},
                },
                "envreq:executable:gh": {
                    "kind": "environment_requirement",
                    "name": "gh",
                    "config": {"kind": "executable", "command": "gh"},
                },
                "mcp:disabled-github": {
                    "kind": "mcp",
                    "name": "disabled-github",
                    "config": {"command": "disabled-github-mcp"},
                },
                "skill:disabled-review": {
                    "kind": "skill",
                    "name": "disabled-review",
                    "config": {
                        "path_hint": "/srv/skills/packages/disabled-review/disabled-review/SKILL.md"
                    },
                },
                "skill:inactive-review": {
                    "kind": "skill",
                    "name": "inactive-review",
                    "config": {
                        "path_hint": "/srv/skills/packages/inactive-review/inactive-review/SKILL.md"
                    },
                },
                "envreq:executable:disabled-gh": {
                    "kind": "environment_requirement",
                    "name": "disabled-gh",
                    "config": {"kind": "executable", "command": "disabled-gh"},
                },
            },
            "mcp": {
                "servers": {
                    "github": {"command": "github-mcp"},
                    "disabled-github": {"command": "disabled-github-mcp"},
                }
            },
            "skills": {
                "items": {
                    "code-review": {
                        "path_hint": "/srv/skills/packages/review/code-review/SKILL.md"
                    },
                    "disabled-review": {
                        "path_hint": "/srv/skills/packages/disabled-review/disabled-review/SKILL.md"
                    },
                    "inactive-review": {
                        "path_hint": "/srv/skills/packages/inactive-review/inactive-review/SKILL.md"
                    },
                }
            },
            "environment": {
                "requirements": {
                    "envreq:executable:gh": {
                        "kind": "executable",
                        "name": "gh",
                        "command": "gh",
                    },
                    "envreq:executable:disabled-gh": {
                        "kind": "executable",
                        "name": "disabled-gh",
                        "command": "disabled-gh",
                    },
                }
            },
        }
    )

    scope = resolve_agent_effective_capability_scope(config, "reviewer")

    assert [server.name for server in scope.mcp_servers] == ["github"]
    assert list(scope.skills.items) == ["code-review"]
    assert list(scope.environment.requirements) == ["envreq:executable:gh"]
    assert "disabled-review" not in scope.capability_catalog
    assert "inactive-review" not in scope.capability_catalog
    catalog = build_capability_catalog(config, agent_id="reviewer")
    assert "`review`" in catalog
    assert "disabled-review" not in catalog
    assert "inactive-review" not in catalog
    fallback_catalog = build_capability_catalog(config)
    assert "inactive-review" not in fallback_catalog


def test_agent_run_snapshot_keeps_worker_capabilities_separate_from_main_chat() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "runtime_profiles": {"codex_remote": {"executor": "codex"}},
            "agent_registry": {
                "agents": {
                    "main_chat": {
                        "runtime_profile": "codex_remote",
                        "capability_refs": ["repo-admin"],
                    },
                    "reviewer": {
                        "runtime_profile": "codex_remote",
                        "capability_refs": ["review"],
                    },
                },
            },
            "capability_packages": {
                "repo-admin": {
                    "components": ["builtin_tool:shell"],
                },
                "review": {
                    "components": ["builtin_tool:read_file"],
                },
            },
            "capability_components": {
                "builtin_tool:shell": {
                    "kind": "builtin_tool",
                    "name": "shell",
                },
                "builtin_tool:read_file": {
                    "kind": "builtin_tool",
                    "name": "read_file",
                },
            },
        }
    )

    snapshot = build_agent_run_snapshot(
        agent_registry=config.agent_registry,
        runtime_profiles=config.runtime_profiles,
        run_limits=config.run_limits,
        capability_packages=config.capability_packages,
        capability_components=config.capability_components,
    )

    assert "tools" not in snapshot["agents"]["main_chat"]["effective_capabilities"]
    assert [
        item["tool_id"]
        for item in snapshot["agents"]["main_chat"]["resolved_capabilities"]["tool_specs"]
    ] == ["capability:repo-admin:builtin_tool:shell"]
    assert "tools" not in snapshot["agents"]["reviewer"]["effective_capabilities"]
    assert [
        item["tool_id"]
        for item in snapshot["agents"]["reviewer"]["resolved_capabilities"]["tool_specs"]
    ] == ["capability:review:builtin_tool:read_file"]


def test_config_validate_rejects_agent_referencing_missing_runtime_profile() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "agent_registry": {
                "agents": {
                    "reviewer": {
                        "runtime_profile": "missing_profile",
                        "dispatch": {"profile": "Review repository changes"},
                    }
                }
            },
        }
    )

    errors = config.validate()

    assert (
        "agent_registry.agents[reviewer].runtime_profile must exist in runtime_profiles"
        in errors
    )


def test_config_validate_rejects_ambiguous_or_internal_chat_entrypoints() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "agent_registry": {
                "agents": {
                    "reviewer": {
                        "chat_entrypoint": True,
                        "dispatch": {"profile": "Review repository changes"},
                    },
                    "internal_entrypoint": {
                        "visibility": "internal",
                        "chat_entrypoint": True,
                        "dispatch": {"profile": "Internal only"},
                    },
                }
            },
        }
    )

    errors = config.validate()

    assert (
        "agent_registry.agents[internal_entrypoint].chat_entrypoint requires visibility=user"
        in errors
    )
    assert any(
        error.startswith("agent_registry.agents chat_entrypoint must be unique")
        and "main_chat" in error
        and "reviewer" in error
        and "internal_entrypoint" in error
        for error in errors
    )


def test_parse_config_reads_persistence_settings() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "persistence": {
                "backend": "postgres",
                "database_url": "postgresql://user:pass@localhost/labrastro",
                "auto_migrate": False,
                "runtime_enabled": True,
                "sessions_enabled": True,
                "retention_days": 30,
                "event_payload_compress_threshold_bytes": 4096,
                "maintenance_interval_sec": 120,
            },
        }
    )

    assert config.persistence.backend == "postgres"
    assert config.persistence.database_url == "postgresql://user:pass@localhost/labrastro"
    assert config.persistence.auto_migrate is False
    assert config.persistence.retention_days == 30
    assert config.persistence.event_payload_compress_threshold_bytes == 4096
    assert config.persistence.maintenance_interval_sec == 120


def test_missing_persistence_database_url_env_is_optional() -> None:
    os.environ.pop("LABRASTRO_TEST_MISSING_DATABASE_URL", None)
    loader = ConfigLoader()
    data = loader._expand_env_refs(
        {
            **_model_config(),
            "persistence": {
                "backend": "auto",
                "database_url": "${LABRASTRO_TEST_MISSING_DATABASE_URL}",
            },
        }
    )
    config = loader._parse_config(data)

    assert config.persistence.database_url == ""


def test_admin_server_settings_update_preserves_runtime_profiles_and_agents() -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=None)
            self.data = {
                "run_limits": {
                    "max_running_agents": 4,
                    "max_shells_per_agent": 1,
                },
                "runtime_profiles": {
                    "codex_remote": {
                        "executor": "codex",
                        "model": "old-model",
                        "credential_refs": {"model": "cred-model"},
                    }
                },
                "agent_registry": {
                    "agents": {
                        "reviewer": {
                            "runtime_profile": "codex_remote",
                            "dispatch": {"profile": "Reviews code changes."},
                        }
                    },
                }
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del previous_data
            self.data = deepcopy(data)
            return None

    manager = MemoryAdminManager()

    result = manager.update_server_settings(
        {"run_limits": {"max_running_agents": 2}}
    )

    assert result.ok is True
    assert manager.data["run_limits"]["max_running_agents"] == 2
    assert manager.data["runtime_profiles"]["codex_remote"]["executor"] == "codex"
    assert manager.data["runtime_profiles"]["codex_remote"]["credential_refs"] == {
        "model": "cred-model"
    }
    assert manager.data["agent_registry"]["agents"]["reviewer"]["runtime_profile"] == "codex_remote"


def test_admin_server_settings_rejects_unregistered_memory_provider_adapter() -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=None)
            self.data: dict = {}

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del previous_data
            self.data = deepcopy(data)
            return None

    MemoryProviderRegistry.clear_registered_adapters()
    manager = MemoryAdminManager()

    result = manager.update_server_settings(
        {
            "memory": {
                "enabled": True,
                "default_provider": "agentmemory",
                "providers": {
                    "agentmemory": {
                        "adapter": "missing_adapter",
                    }
                },
            }
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_memory"
    assert "adapter is not registered" in result.payload["message"]
    assert manager.data == {}


def test_admin_status_explains_memory_provider_agent_and_source_state() -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=None)
            self.data = {
                "memory": {
                    "enabled": True,
                    "default_provider": "agentmemory",
                    "default_agent_id": "core",
                    "runtime": {
                        "inject_default": True,
                        "capture_default": False,
                        "token_budget_default": 640,
                    },
                    "providers": {
                        "agentmemory": {
                            "adapter": "fake_memory",
                        },
                        "archived": {
                            "adapter": "missing_memory",
                        },
                    },
                    "sources": {
                        "github_project": {
                            "adapter": "fake_source",
                            "target_provider": "agentmemory",
                            "sync_mode": "manual",
                        },
                        "archived_project": {
                            "adapter": "fake_source",
                            "target_provider": "archived",
                            "sync_mode": "manual",
                        }
                    },
                    "tools": {
                        "enabled": True,
                        "provider": "agentmemory",
                        "allowed_agents": ["reviewer"],
                        "remember": True,
                        "list": True,
                    },
                },
                "agent_registry": {
                    "agents": {
                        "reviewer": {
                            "name": "Reviewer",
                            "memory": {
                                "primary_provider": "agentmemory",
                                "capture": True,
                                "token_budget": 320,
                            },
                        },
                        "researcher": {
                            "name": "Researcher",
                        },
                    }
                },
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

    MemoryProviderRegistry.clear_registered_adapters()
    MemorySourceRegistry.clear_registered_adapters()
    MemoryProviderRegistry.register_adapter("fake_memory", lambda provider_id, config: None)
    MemorySourceRegistry.register_adapter("fake_source", lambda source_id, config: None)
    try:
        memory_status = MemoryAdminManager().read_server_settings()["settings"][
            "memory_status"
        ]

        providers = {item["id"]: item for item in memory_status["providers"]}
        assert memory_status["default_provider_available"] is True
        assert providers["agentmemory"]["status"] == "available"
        assert providers["agentmemory"]["adapter_registered"] is True
        assert providers["archived"]["status"] == "adapter_missing"
        assert providers["archived"]["adapter_registered"] is False

        policies = {
            item["agent_id"]: item for item in memory_status["agent_policies"]
        }
        assert policies["reviewer"]["policy_source"] == "overridden"
        assert policies["reviewer"]["capture"] is True
        assert policies["reviewer"]["token_budget"] == 320
        assert policies["researcher"]["policy_source"] == "default"
        assert policies["researcher"]["capture"] is False
        assert policies["researcher"]["token_budget"] == 640

        sources = {item["id"]: item for item in memory_status["sources"]}
        assert sources["github_project"]["role"] == "external_source_connector"
        assert sources["github_project"]["status"] == "configured"
        assert sources["github_project"]["target_provider_configured"] is True
        assert sources["github_project"]["target_provider_available"] is True
        assert sources["github_project"]["target_provider_status"] == "available"
        assert sources["archived_project"]["status"] == "target_unavailable"
        assert sources["archived_project"]["target_provider_configured"] is True
        assert sources["archived_project"]["target_provider_enabled"] is True
        assert sources["archived_project"]["target_provider_available"] is False
        assert sources["archived_project"]["target_provider_status"] == "adapter_missing"

        tools = memory_status["tools"]
        assert tools["status"] == "policy_only"
        assert tools["provider_available"] is True
        assert tools["configured_operations"]["remember"] is True
        assert tools["configured_operations"]["list"] is True
    finally:
        MemoryProviderRegistry.clear_registered_adapters()
        MemorySourceRegistry.clear_registered_adapters()


def test_admin_status_exposes_provider_model_catalog_and_agent_default() -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=None)
            self.data = {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "api_key": "sk-ds",
                            "models": [
                                {"id": "V4FLASH", "display_name": "V4 Flash"},
                                {"id": "V4PRO", "display_name": "V4 Pro"},
                            ],
                        }
                    }
                },
                "modes": {"active": "coder", "profiles": {"coder": {}}},
                "agent_registry": {
                    "agents": {
                        "coder": {
                            "name": "Coder",
                            "model": {
                                "provider": "deepseek",
                                "model": "V4PRO",
                                "display_name": "V4 Pro",
                            },
                        }
                    }
                },
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

    status = MemoryAdminManager().status()

    models = {
        (item["provider_id"], item["model_id"])
        for item in status["provider_model_catalog"]
    }
    assert ("deepseek", "V4FLASH") in models
    assert ("deepseek", "V4PRO") in models
    assert status["providers"][0]["models"] == [
        {"id": "V4FLASH", "display_name": "V4 Flash"},
        {"id": "V4PRO", "display_name": "V4 Pro"},
    ]
    assert status["active_agent_model"] == {
        "provider": "deepseek",
        "model": "V4PRO",
        "display_name": "V4 Pro",
        "parameters": {},
    }
    assert "environment_configurator" in status["agent_profiles"]
    environment_agent = status["server_settings"]["agent_registry"]["agents"][
        "environment_configurator"
    ]
    assert environment_agent["runtime_profile"] == "environment_local"


def test_admin_chat_config_exposes_only_chat_selection_state() -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=None)
            self.data = {
                "providers": {
                    "items": {
                        "deepseek": {
                            "type": "openai_chat",
                            "api_key": "sk-ds",
                            "models": [{"id": "V4PRO", "display_name": "V4 Pro"}],
                        }
                    }
                },
                "models": {
                    "active_main": "deepseek-main",
                    "active_sub": "deepseek-sub",
                    "profiles": {
                        "deepseek-main": {
                            "provider": "deepseek",
                            "model": "V4PRO",
                            "max_tokens": 8192,
                            "max_context_tokens": 128000,
                        },
                        "deepseek-sub": {
                            "provider": "deepseek",
                            "model": "V4FLASH",
                            "max_tokens": 4096,
                            "max_context_tokens": 64000,
                        },
                    },
                },
                "modes": {"active": "planner", "profiles": {"planner": {}}},
                "agent_registry": {
                    "agents": {
                        "planner": {
                            "name": "Planner",
                            "model": {
                                "provider": "deepseek",
                                "model": "V4PRO",
                                "display_name": "V4 Pro",
                            },
                        }
                    }
                },
                "model_capabilities": {"enabled": False},
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

    chat_config = MemoryAdminManager().chat_config()

    assert chat_config["active_mode"] == "planner"
    assert {mode["name"] for mode in chat_config["modes"]} >= {"coder", "planner"}
    assert chat_config["active_main"] == "deepseek-main"
    assert chat_config["active_sub"] == "deepseek-sub"
    assert [profile["id"] for profile in chat_config["model_profiles"]] == [
        "deepseek-main",
        "deepseek-sub",
    ]
    assert chat_config["active_agent_model"] == {
        "provider": "deepseek",
        "model": "V4PRO",
        "display_name": "V4 Pro",
        "parameters": {},
    }
    assert "providers" not in chat_config
    assert "provider_model_catalog" not in chat_config
    assert "server_settings" not in chat_config
    assert "model_capabilities" not in chat_config
    assert "agent_runs" not in chat_config


def test_admin_server_settings_update_replace_removes_runtime_profiles_and_agents() -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=None)
            self.data = {
                "run_limits": {
                    "max_running_agents": 4,
                    "max_shells_per_agent": 2,
                },
                "runtime_profiles": {
                    "codex_remote": {
                        "executor": "codex",
                        "execution_location": "remote_server",
                    }
                },
                "agent_registry": {
                    "agents": {
                        "reviewer": {
                            "runtime_profile": "codex_remote",
                            "dispatch": {"profile": "Reviews code changes."},
                        }
                    },
                }
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del previous_data
            self.data = deepcopy(data)
            return None

    manager = MemoryAdminManager()

    result = manager.update_server_settings(
        {
            "run_limits": {
                "max_running_agents": 3,
            },
            "runtime_profiles": {
                "fake_daemon": {
                    "executor": "fake",
                    "execution_location": "daemon_worktree",
                }
            },
            "agent_registry": {
                "agents": {
                    "smoke": {
                        "runtime_profile": "fake_daemon",
                        "dispatch": {"profile": "Runs fake smoke tasks."},
                    }
                },
            },
        }
    )

    assert result.ok is True
    assert manager.data["run_limits"]["max_running_agents"] == 3
    assert manager.data["run_limits"]["max_shells_per_agent"] == 2
    assert set(manager.data["runtime_profiles"]) == {
        "fake_daemon",
        "environment_local",
        "agent_remote",
        "capability_packager_remote",
    }
    assert set(manager.data["agent_registry"]["agents"]) == {
        "main_chat",
        "smoke",
        "environment_configurator",
        "capability_packager",
    }


def test_accept_and_delete_capability_package_manages_shared_components(tmp_path) -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=tmp_path / "config.yaml")
            self.data = {
                **_model_config(),
                "capability_packages": {},
                "capability_components": {},
                "mcp": {
                    "servers": {
                        "standalone": {
                            "command": "standalone-mcp",
                            "managed_by": "user",
                        }
                    }
                },
                "skills": {
                    "items": {
                        "standalone-review": {
                            "path_hint": "/skills/standalone-review/SKILL.md",
                            "managed_by": "user",
                        }
                    }
                },
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del previous_data
            self.data = deepcopy(data)
            return None

    manager = MemoryAdminManager()
    component = {
        "id": "envreq:executable:gh",
        "kind": "environment_requirement",
        "name": "gh",
        "config": {"kind": "executable", "command": "gh", "check": "gh --version"},
    }
    mcp_component = {
        "id": "mcp_server:github",
        "kind": "mcp_server",
        "name": "github",
        "config": {
            "command": "github-mcp",
            "placement": "peer",
            "environment_requirement_refs": ["envreq:executable:gh"],
        },
    }
    skill_component = {
        "id": "skill:code-review",
        "kind": "skill",
        "name": "code-review",
        "config": {
            "path_hint": "/skills/code-review/SKILL.md",
            "source_path": "/skills/code-review",
            "description": "Review code changes.",
            "skill_content": "Review code changes.\n",
        },
    }

    first = _apply_install_candidate(
        manager,
        {
            "draft": {
                "id": "review",
                "name": "Review",
                "source": {
                    "type": "github_repo",
                    "url": "https://github.com/example/review-tools",
                },
                "contributions": {
                    "environment_requirements": [component],
                    "mcp_servers": [mcp_component],
                    "skills": [skill_component],
                },
                "effective_capabilities": ["Inspect pull request metadata with gh."],
                "install_plan": ["Install gh."],
                "usage": ["Use gh pr view."],
                "evidence": [
                    {"title": "Docs", "excerpt": "Run gh --version and github-mcp."}
                ],
                "risk_level": "medium",
            }
        }
    )
    assert first.ok is True
    assert set(manager.data["capability_packages"]["review"]["components"]) == {
        "envreq:executable:gh",
        "mcp_server:github",
        "skill:code-review",
    }
    assert manager.data["capability_packages"]["review"]["effective_capabilities"] == [
        "Inspect pull request metadata with gh."
    ]
    assert manager.data["capability_components"]["envreq:executable:gh"]["package_ids"] == ["review"]
    assert (
        manager.data["environment"]["requirements"]["envreq:executable:gh"]["component_id"]
        == "envreq:executable:gh"
    )
    assert manager.data["mcp"]["servers"]["github"]["component_id"] == "mcp_server:github"
    assert manager.data["mcp"]["servers"]["github"]["environment_requirement_refs"] == [
        "envreq:executable:gh"
    ]
    assert manager.data["skills"]["items"]["code-review"]["component_id"] == "skill:code-review"
    assert manager.data["skills"]["items"]["code-review"]["managed_by"] == "capability_package"
    assert manager.data["skills"]["items"]["standalone-review"]["managed_by"] == "user"
    installed_path = (
        tmp_path
        / "skills"
        / "packages"
        / "components"
        / "skill-code-review"
        / "SKILL.md"
    )
    assert installed_path.read_text(encoding="utf-8") == "Review code changes.\n"

    second = _apply_install_candidate(
        manager,
        {
            "draft": {
                "id": "pr",
                "name": "Pull Request",
                "source": {
                    "type": "github_repo",
                    "url": "https://github.com/example/pr-tools",
                },
                "contributions": {
                    "environment_requirements": [component],
                    "skills": [skill_component],
                },
                "evidence": [{"title": "Docs", "excerpt": "Run gh --version."}],
                "risk_level": "medium",
            }
        }
    )
    assert second.ok is True
    assert manager.data["capability_components"]["envreq:executable:gh"]["package_ids"] == [
        "review",
        "pr",
    ]
    assert manager.data["capability_components"]["skill:code-review"]["package_ids"] == [
        "review",
        "pr",
    ]

    deleted_review = manager.delete_capability_package({"package_id": "review"})
    assert deleted_review.ok is True
    assert manager.data["capability_components"]["envreq:executable:gh"]["package_ids"] == ["pr"]
    assert manager.data["capability_components"]["skill:code-review"]["package_ids"] == ["pr"]
    assert "envreq:executable:gh" in manager.data["environment"]["requirements"]
    assert "code-review" in manager.data["skills"]["items"]
    assert manager.data["skills"]["items"]["code-review"]["package_ids"] == ["pr"]
    assert installed_path.exists()

    deleted_pr = manager.delete_capability_package({"package_id": "pr"})
    assert deleted_pr.ok is True
    assert "envreq:executable:gh" not in manager.data["capability_components"]
    assert "mcp_server:github" not in manager.data["capability_components"]
    assert "skill:code-review" not in manager.data["capability_components"]
    assert "envreq:executable:gh" not in manager.data["environment"]["requirements"]
    assert "github" not in manager.data["mcp"]["servers"]
    assert "code-review" not in manager.data["skills"]["items"]
    assert not installed_path.exists()
    assert not installed_path.parent.exists()
    assert "standalone" in manager.data["mcp"]["servers"]
    assert "standalone-review" in manager.data["skills"]["items"]


def test_admin_rejects_builtin_capability_package_disable_and_delete(tmp_path) -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=tmp_path / "config.yaml")
            self.data = {
                **_model_config(),
                "capability_packages": {
                    "environment": {"enabled": False, "components": []},
                    "core_builtin_tools": {"enabled": False},
                    "capability_packager_builtin_tools": {"enabled": False},
                    "review": {"enabled": True, "components": []},
                },
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del previous_data
            self.data = deepcopy(data)
            return None

    manager = MemoryAdminManager()

    for package_id in (
        "environment",
        "core_builtin_tools",
        "capability_packager_builtin_tools",
    ):
        disabled = manager.enable_capability_package(
            {"package_id": package_id, "enabled": False}
        )
        assert disabled.ok is False
        assert disabled.status == 400
        assert disabled.payload["error"] == "builtin_capability_package"

        deleted = manager.delete_capability_package({"package_id": package_id})
        assert deleted.ok is False
        assert deleted.status == 400
        assert deleted.payload["error"] == "builtin_capability_package"

        healed = manager.enable_capability_package(
            {"package_id": package_id, "enabled": True}
        )
        assert healed.ok is True
        assert healed.payload["capability_package"]["enabled"] is True

    disabled_review = manager.enable_capability_package(
        {"package_id": "review", "enabled": False}
    )
    assert disabled_review.ok is True
    assert disabled_review.payload["capability_package"]["enabled"] is False
    deleted_review = manager.delete_capability_package({"package_id": "review"})
    assert deleted_review.ok is True


def test_admin_accept_does_not_write_skill_file_when_config_commit_fails(tmp_path) -> None:
    class FailingCommitManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=tmp_path / "config.yaml")
            self.data = {
                **_model_config(),
                "capability_packages": {},
                "capability_components": {},
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del data, previous_data
            return admin_service.AdminConfigResult(
                False,
                {"error": "config_reload_failed", "message": "reload failed"},
                500,
            )

    manager = FailingCommitManager()
    installed_path = (
        tmp_path
        / "skills"
        / "packages"
        / "components"
        / "skill-code-review"
        / "SKILL.md"
    )

    result = _apply_install_candidate(
        manager,
        {
            "draft": {
                "id": "review",
                "components": [
                    {
                        "id": "skill:code-review",
                        "kind": "skill",
                        "name": "code-review",
                        "skill_content": "Review code changes.\n",
                    }
                ],
                "evidence": [{"title": "Docs", "excerpt": "Review code."}],
                "risk_level": "medium",
            }
        }
    )

    assert result.ok is False
    assert result.payload["error"] == "config_reload_failed"
    assert not installed_path.exists()
    assert manager.data["capability_packages"] == {}
    assert manager.data["capability_components"] == {}


def test_admin_delete_does_not_remove_skill_file_when_config_commit_fails(tmp_path) -> None:
    installed_path = (
        tmp_path
        / "skills"
        / "packages"
        / "components"
        / "skill-code-review"
        / "SKILL.md"
    )
    installed_path.parent.mkdir(parents=True)
    installed_path.write_text("Review code changes.\n", encoding="utf-8")

    class FailingCommitManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=tmp_path / "config.yaml")
            self.data = {
                **_model_config(),
                "capability_packages": {
                    "review": {
                        "enabled": True,
                        "components": ["skill:code-review"],
                    }
                },
                "capability_components": {
                    "skill:code-review": {
                        "kind": "skill",
                        "name": "code-review",
                        "package_ids": ["review"],
                        "config": {
                            "path_hint": str(installed_path),
                            "skill_content": "Review code changes.\n",
                        },
                    }
                },
                "skills": {
                    "items": {
                        "code-review": {
                            "path_hint": str(installed_path),
                            "component_id": "skill:code-review",
                            "managed_by": "capability_package",
                            "package_ids": ["review"],
                        }
                    }
                },
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del data, previous_data
            return admin_service.AdminConfigResult(
                False,
                {"error": "config_reload_failed", "message": "reload failed"},
                500,
            )

    result = FailingCommitManager().delete_capability_package({"package_id": "review"})

    assert result.ok is False
    assert result.payload["error"] == "config_reload_failed"
    assert installed_path.read_text(encoding="utf-8") == "Review code changes.\n"


def test_capability_package_rejects_materialized_resource_slot_conflicts(tmp_path) -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=tmp_path / "config.yaml")
            self.data = {
                **_model_config(),
                "capability_packages": {},
                "capability_components": {},
                "skills": {
                    "items": {
                        "code-review": {
                            "path_hint": "/skills/user/code-review/SKILL.md",
                            "managed_by": "user",
                        }
                    }
                },
                "mcp": {
                    "servers": {
                        "github": {
                            "command": "user-github-mcp",
                            "managed_by": "user",
                        }
                    }
                },
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del previous_data
            self.data = deepcopy(data)
            return None

    manager = MemoryAdminManager()
    previous = deepcopy(manager.data)

    skill_conflict = _apply_install_candidate(
        manager,
        {
            "draft": {
                "id": "review-a",
                "name": "Review A",
                "contributions": {
                    "skills": [
                        {
                            "id": "skill:code-review",
                            "kind": "skill",
                            "name": "code-review",
                            "config": {
                                "path_hint": "/skills/review-a/SKILL.md",
                                "skill_content": "Review A.\n",
                            },
                        }
                    ],
                },
                "evidence": [{"title": "Docs", "excerpt": "Review A."}],
                "risk_level": "medium",
            }
        }
    )
    assert skill_conflict.ok is False
    assert skill_conflict.status == 409
    assert skill_conflict.payload["error"] == "capability_resource_conflict"
    assert manager.data["capability_packages"] == previous["capability_packages"]
    assert manager.data["capability_components"] == previous["capability_components"]
    assert manager.data["skills"] == previous["skills"]
    assert manager.data["mcp"] == previous["mcp"]

    mcp_conflict = _apply_install_candidate(
        manager,
        {
            "draft": {
                "id": "github-b",
                "name": "GitHub B",
                "contributions": {
                    "mcp_servers": [
                        {
                            "id": "mcp_server:github",
                            "kind": "mcp_server",
                            "name": "github",
                            "config": {"command": "github-b-mcp"},
                        }
                    ]
                },
                "evidence": [{"title": "Docs", "excerpt": "Start github-b-mcp."}],
                "risk_level": "medium",
            }
        }
    )
    assert mcp_conflict.ok is False
    assert mcp_conflict.status == 409
    assert mcp_conflict.payload["error"] == "capability_resource_conflict"
    assert manager.data["capability_packages"] == previous["capability_packages"]
    assert manager.data["capability_components"] == previous["capability_components"]
    assert manager.data["skills"] == previous["skills"]
    assert manager.data["mcp"] == previous["mcp"]


def test_admin_skill_resource_crud_uses_user_lifecycle_and_blocks_package_managed() -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=None)
            self.data = {
                "skills": {
                    "items": {
                        "package-review": {
                            "enabled": True,
                            "path_hint": "/skills/package-review/SKILL.md",
                            "component_id": "skill:package-review",
                            "package_ids": ["review"],
                            "managed_by": "capability_package",
                        }
                    }
                }
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del previous_data
            self.data = deepcopy(data)
            return None

    manager = MemoryAdminManager()

    created = manager.record_skill(
        {
            "name": "standalone-review",
            "enabled": True,
            "path_hint": "/skills/standalone-review/SKILL.md",
            "source_path": "/skills/standalone-review",
            "description": "Standalone review helper.",
            "docs": [{"title": "Skill docs", "url": "https://example.test/skill"}],
            "evidence": [{"field": "path_hint", "excerpt": "SKILL.md exists."}],
            "install_prompt": "Install standalone-review.",
            "verify_prompt": "Open SKILL.md.",
        }
    )

    assert created.ok is True
    assert created.payload["skill"]["name"] == "standalone-review"
    assert created.payload["skill"]["managed_by"] == "user"
    assert (
        manager.data["skills"]["items"]["standalone-review"]["path_hint"]
        == "/skills/standalone-review/SKILL.md"
    )

    skills = {item["name"]: item for item in manager.list_skills()["skills"]}
    assert skills["standalone-review"]["source_path"] == "/skills/standalone-review"
    dashboard = {item["id"]: item for item in manager.skills_dashboard()["items"]}
    assert dashboard["skill:standalone-review"]["path_hint"] == "/skills/standalone-review/SKILL.md"
    assert dashboard["skill:standalone-review"]["managed_by"] == "user"

    disabled = manager.enable_skill({"name": "standalone-review", "enabled": False})
    assert disabled.ok is True
    assert disabled.payload["skill"]["enabled"] is False

    for blocked in [
        manager.record_skill(
            {"name": "package-review", "path_hint": "/tmp/override/SKILL.md"}
        ),
        manager.enable_skill({"name": "package-review", "enabled": False}),
        manager.delete_skill({"name": "package-review"}),
    ]:
        assert blocked.ok is False
        assert blocked.status == 409
        assert blocked.payload["error"] == "capability_package_managed_resource"
        assert blocked.payload["package_ids"] == ["review"]

    deleted = manager.delete_skill({"name": "standalone-review"})
    assert deleted.ok is True
    assert "standalone-review" not in manager.data["skills"]["items"]
    assert "package-review" in manager.data["skills"]["items"]


def test_admin_accept_delegates_to_capability_package_installer(monkeypatch) -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=None)
            self.data = {
                **_model_config(),
                "capability_packages": {},
                "capability_components": {},
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del previous_data
            self.data = deepcopy(data)
            return None

    class FakeInstaller:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict, str]] = []
            self.skill_file_operations: list[dict] = []

        def install_candidate(
            self,
            data: dict,
            raw_candidate: dict,
        ) -> CapabilityPackageInstallResult:
            package_id = (
                str(raw_candidate.get("package_id") or "")
                if isinstance(raw_candidate, dict)
                else str(getattr(raw_candidate, "package_id", "") or "")
            )
            self.calls.append((data, raw_candidate, package_id))
            data["capability_components"] = {
                "envreq:executable:gh": {
                    "kind": "environment_requirement",
                    "name": "gh",
                    "enabled": True,
                    "package_ids": ["review"],
                    "source": {"type": "project_notes"},
                    "config": {"kind": "executable", "command": "gh"},
                    "managed_by": "capability_package",
                    "status": "installed",
                }
            }
            data["capability_packages"] = {
                "review": {
                    "enabled": True,
                    "status": "installed",
                    "source": {"type": "project_notes"},
                    "components": ["envreq:executable:gh"],
                    "generated_by": "capability_packager",
                    "name": "Review",
                }
            }
            package = CapabilityPackageConfig.from_dict(
                "review",
                data["capability_packages"]["review"],
            )
            return CapabilityPackageInstallResult(
                package_id="review",
                package=package,
                component_ids=["envreq:executable:gh"],
            )

        def materialize_component(self, data: dict, component) -> None:
            del data, component

    installer = FakeInstaller()
    monkeypatch.setattr(admin_service, "CapabilityPackageInstaller", lambda **_: installer)

    result = _apply_install_candidate(
        MemoryAdminManager(),
        {
            "draft": {
                "id": "review",
                "contributions": {
                    "environment_requirements": [
                        {"kind": "executable", "name": "gh", "command": "gh"}
                    ]
                },
                "evidence": [{"title": "Docs", "excerpt": "Run gh."}],
                "risk_level": "low",
            }
        }
    )

    assert result.ok is True
    assert installer.calls
    assert installer.calls[0][2] == "review"
    assert result.payload["package_id"] == "review"


def test_admin_build_capability_install_candidate_enforces_validation_messages() -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=None)
            self.data = {
                **_model_config(),
                "capability_packages": {},
                "capability_components": {},
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del data, previous_data
            raise AssertionError("invalid drafts must not be committed")

    result = MemoryAdminManager().build_capability_install_candidate(
        {
            "draft": {
                "id": "review",
                "components": [
                    {
                        "id": "envreq:executable:gh",
                        "kind": "environment_requirement",
                        "name": "gh",
                        "command": "gh",
                    }
                ],
            }
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "capability_install_candidate_not_ready"
    assert result.payload["reason"] == "draft_invalid"
    assert "draft.evidence is required" in result.payload["messages"]
    assert "risk_level is required" in result.payload["messages"]
    assert "envreq:executable:gh command lacks evidence: gh" in result.payload["messages"]


def test_admin_build_capability_install_candidate_rejects_skill_without_installable_content() -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=None)
            self.data = {
                **_model_config(),
                "capability_packages": {},
                "capability_components": {},
            }

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del data, previous_data
            raise AssertionError("invalid skill drafts must not be committed")

    result = MemoryAdminManager().build_capability_install_candidate(
        {
            "draft": {
                "id": "review",
                "components": [
                    {
                        "id": "skill:code-review",
                        "kind": "skill",
                        "name": "code-review",
                        "path_hint": "/external/code-review/SKILL.md",
                    }
                ],
                "evidence": [{"title": "Docs", "excerpt": "Review code."}],
                "risk_level": "medium",
            }
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "capability_install_candidate_not_ready"
    assert result.payload["reason"] == "draft_invalid"
    assert "skill component 'code-review' requires skill_content" in result.payload["messages"]


def test_admin_server_settings_update_rejects_missing_agent_profile() -> None:
    class MemoryAdminManager(RemoteAdminConfigManager):
        def __init__(self) -> None:
            super().__init__(config_path=None)
            self.data = {"agent_registry": {}, "runtime_profiles": {}}

        def _load_data(self) -> dict:
            return deepcopy(self.data)

        def _commit_config(self, data: dict, previous_data: dict):
            del data, previous_data
            raise AssertionError("invalid config should not be committed")

    result = MemoryAdminManager().update_server_settings(
        {
            "runtime_profiles": {},
            "agent_registry": {
                "agents": {"reviewer": {"runtime_profile": "missing"}}
            },
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_agent_registry"
    assert "reviewer" in result.payload["message"]

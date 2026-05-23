from __future__ import annotations

from copy import deepcopy
import os

from labrastro_server.services.admin import service as admin_service
from labrastro_server.services.admin.service import RemoteAdminConfigManager
from labrastro_server.services.capability_packages import CapabilityPackageInstallResult
from reuleauxcoder.domain.agent_runtime.models import CapabilityPackageConfig
from reuleauxcoder.domain.config.models import build_agent_run_snapshot
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


def test_parse_config_reads_agent_registry_profiles_and_limits() -> None:
    config = ConfigLoader()._parse_config(
        {
            **_model_config(),
            "run_limits": {
                "max_running_agents": 8,
                "max_shells_per_agent": 2,
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
    assert "builtin_tool:fetch_capabilities" in config.capability_components
    assert "environment_local" in config.runtime_profiles.profiles
    assert "environment_configurator" in config.agent_registry.agents
    assert "capability_packager_local" in config.runtime_profiles.profiles
    assert "capability_packager" in config.agent_registry.agents


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
    assert packager.runtime_profile == "capability_packager_local"
    assert packager.visibility == "internal"
    assert packager.can_delegate is False
    assert packager.can_run_taskflow is False
    assert packager.allows_system_flow("capability_ingest") is True
    assert "structured draft" in packager.dispatch.profile
    assert "environment" in config.capability_packages
    assert "permissions" not in config.capability_packages["environment"].to_dict()


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
                    "components": ["mcp:github", "skill:code-review", "cli:gh"],
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
                "cli:gh": {
                    "kind": "cli",
                    "name": "gh",
                    "config": {
                        "command": "gh",
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
    assert resolved["cli_tools"] == ["gh"]
    assert resolved["capability_overlay"]["component_ids"] == [
        "mcp:github",
        "skill:code-review",
        "cli:gh",
    ]
    assert resolved["capability_overlay"]["mcp"]["servers"]["github"]["command"] == "github-mcp-server"
    assert resolved["capability_overlay"]["skill_roots"] == ["/skills/code-review"]
    assert resolved["capability_overlay"]["env"] == {"GH_CONFIG_DIR": ".gh"}
    catalog = build_capability_catalog(config)
    assert "`github-review`" in catalog
    assert "`cli:gh` [cli] gh" in catalog
    assert "command `gh`" in catalog


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

    assert "builtin:shell" in snapshot["agents"]["main_chat"]["effective_capabilities"]["tools"]
    assert snapshot["agents"]["reviewer"]["effective_capabilities"]["tools"] == [
        "builtin:read_file"
    ]


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
        "capability_packager_local",
    }
    assert set(manager.data["agent_registry"]["agents"]) == {
        "main_chat",
        "smoke",
        "environment_configurator",
        "capability_packager",
    }


def test_accept_and_delete_capability_package_manages_shared_components() -> None:
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

    manager = MemoryAdminManager()
    component = {
        "id": "cli:gh",
        "kind": "cli",
        "name": "gh",
        "config": {"command": "gh", "check": "gh --version"},
    }

    first = manager.accept_capability_package_draft(
        {
            "draft": {
                "id": "review",
                "name": "Review",
                "source": {
                    "type": "github_repo",
                    "url": "https://github.com/example/review-tools",
                },
                "components": [component],
                "effective_capabilities": ["Inspect pull request metadata with gh."],
                "install_plan": ["Install gh."],
                "usage": ["Use gh pr view."],
            }
        }
    )
    assert first.ok is True
    assert manager.data["capability_packages"]["review"]["components"] == ["cli:gh"]
    assert manager.data["capability_packages"]["review"]["effective_capabilities"] == [
        "Inspect pull request metadata with gh."
    ]
    assert manager.data["capability_components"]["cli:gh"]["package_ids"] == ["review"]
    assert manager.data["environment"]["cli_tools"]["gh"]["component_id"] == "cli:gh"

    second = manager.accept_capability_package_draft(
        {
            "draft": {
                "id": "pr",
                "name": "Pull Request",
                "source": {
                    "type": "github_repo",
                    "url": "https://github.com/example/pr-tools",
                },
                "components": [component],
            }
        }
    )
    assert second.ok is True
    assert manager.data["capability_components"]["cli:gh"]["package_ids"] == [
        "review",
        "pr",
    ]

    deleted_review = manager.delete_capability_package({"package_id": "review"})
    assert deleted_review.ok is True
    assert manager.data["capability_components"]["cli:gh"]["package_ids"] == ["pr"]
    assert "gh" in manager.data["environment"]["cli_tools"]

    deleted_pr = manager.delete_capability_package({"package_id": "pr"})
    assert deleted_pr.ok is True
    assert "cli:gh" not in manager.data["capability_components"]
    assert "gh" not in manager.data["environment"]["cli_tools"]


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

        def install_draft(
            self,
            data: dict,
            raw_draft: dict,
            *,
            package_id: str = "",
        ) -> CapabilityPackageInstallResult:
            self.calls.append((data, raw_draft, package_id))
            data["capability_components"] = {
                "cli:gh": {
                    "kind": "cli",
                    "name": "gh",
                    "enabled": True,
                    "package_ids": ["review"],
                    "source": {"type": "project_notes"},
                    "config": {"command": "gh"},
                    "managed_by": "capability_package",
                    "status": "installed",
                }
            }
            data["capability_packages"] = {
                "review": {
                    "enabled": True,
                    "status": "installed",
                    "source": {"type": "project_notes"},
                    "components": ["cli:gh"],
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
                component_ids=["cli:gh"],
            )

    installer = FakeInstaller()
    monkeypatch.setattr(admin_service, "CapabilityPackageInstaller", lambda: installer)

    result = MemoryAdminManager().accept_capability_package_draft(
        {"draft": {"id": "review", "components": [{"kind": "cli", "name": "gh"}]}}
    )

    assert result.ok is True
    assert installer.calls
    assert installer.calls[0][2] == "review"
    assert result.payload["package_id"] == "review"


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

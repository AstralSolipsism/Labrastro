from __future__ import annotations

from copy import deepcopy
import os

from labrastro_server.services.admin.service import RemoteAdminConfigManager
from reuleauxcoder.domain.config.models import build_agent_run_snapshot
from reuleauxcoder.services.config.loader import ConfigLoader


def test_parse_config_reads_agent_registry_profiles_and_limits() -> None:
    config = ConfigLoader()._parse_config(
        {
            "models": {
                "active_main": "main",
                "profiles": {"main": {"model": "gpt", "api_key": "key"}},
            },
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
                    "permissions": ["repo.read"],
                }
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
    assert "environment_local" in config.runtime_profiles.profiles
    assert "environment_configurator" in config.agent_registry.agents


def test_parse_config_injects_environment_configurator_by_default() -> None:
    config = ConfigLoader()._parse_config(
        {
            "models": {
                "active_main": "main",
                "profiles": {"main": {"model": "gpt", "api_key": "key"}},
            },
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
    agent = config.agent_registry.agents["environment_configurator"]
    assert agent.runtime_profile == "environment_local"
    assert "environment manifest" in agent.dispatch.profile
    assert agent.capability_refs == ["environment"]
    assert "environment" in config.capability_packages


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
            "models": {
                "active_main": "main",
                "profiles": {"main": {"model": "gpt", "api_key": "key"}},
            },
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
    )

    assert "cred_codex_team" in str(snapshot)
    assert "cred_repo_writer" in str(snapshot)
    assert "OPENAI_API_KEY" not in str(snapshot)
    assert "sk-" not in str(snapshot)


def test_config_validate_rejects_agent_referencing_missing_runtime_profile() -> None:
    config = ConfigLoader()._parse_config(
        {
            "models": {
                "active_main": "main",
                "profiles": {"main": {"model": "gpt", "api_key": "key"}},
            },
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


def test_parse_config_reads_persistence_settings() -> None:
    config = ConfigLoader()._parse_config(
        {
            "models": {
                "active_main": "main",
                "profiles": {"main": {"model": "gpt", "api_key": "key"}},
            },
            "persistence": {
                "backend": "postgres",
                "database_url": "postgresql://user:pass@localhost/labrastro",
                "auto_migrate": False,
                "runtime_enabled": True,
                "sessions_enabled": True,
                "retention_days": 30,
                "snapshot_max_versions_per_session": 12,
                "snapshot_compress_threshold_bytes": 4096,
                "maintenance_interval_sec": 120,
            },
        }
    )

    assert config.persistence.backend == "postgres"
    assert config.persistence.database_url == "postgresql://user:pass@localhost/labrastro"
    assert config.persistence.auto_migrate is False
    assert config.persistence.retention_days == 30
    assert config.persistence.snapshot_max_versions_per_session == 12
    assert config.persistence.snapshot_compress_threshold_bytes == 4096
    assert config.persistence.maintenance_interval_sec == 120


def test_missing_persistence_database_url_env_is_optional() -> None:
    os.environ.pop("LABRASTRO_TEST_MISSING_DATABASE_URL", None)
    loader = ConfigLoader()
    data = loader._expand_env_refs(
        {
            "models": {
                "active_main": "main",
                "profiles": {"main": {"model": "gpt", "api_key": "key"}},
            },
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
    assert set(manager.data["runtime_profiles"]) == {"fake_daemon", "environment_local"}
    assert set(manager.data["agent_registry"]["agents"]) == {"smoke", "environment_configurator"}


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

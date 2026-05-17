from pathlib import Path
from unittest.mock import patch

import pytest

from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.services.config.loader import ConfigEnvironmentError
from reuleauxcoder.services.config.loader import DeprecatedConfigError
from reuleauxcoder.services.config.loader import ExampleConfigError


def test_load_yaml_returns_empty_dict_for_missing_file(tmp_path: Path) -> None:
    loader = ConfigLoader()
    assert loader._load_yaml(tmp_path / "missing.yaml") == {}


def test_load_yaml_returns_empty_dict_for_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("foo: [unterminated", encoding="utf-8")

    loader = ConfigLoader()
    assert loader._load_yaml(path) == {}


def test_merge_dicts_recursively_merges_nested_dicts() -> None:
    loader = ConfigLoader()
    merged = loader._merge_dicts(
        {"diagnostics": {"llm_trace": {"enabled": False, "raw_chunks": False}}},
        {"diagnostics": {"llm_trace": {"raw_chunks": True}}},
    )
    assert merged == {
        "diagnostics": {"llm_trace": {"enabled": False, "raw_chunks": True}}
    }


def test_merge_dicts_merges_profile_maps_by_name() -> None:
    loader = ConfigLoader()
    merged = loader._merge_dicts(
        {
            "models": {
                "active_main": "main",
                "profiles": {
                    "main": {
                        "provider": "openai",
                        "model": "gpt-4o",
                        "max_tokens": 32768,
                        "max_context_tokens": 128000,
                    },
                    "sub": {
                        "provider": "openai",
                        "model": "gpt-4o-mini",
                        "max_tokens": 16384,
                        "max_context_tokens": 128000,
                    },
                },
            }
        },
        {
            "models": {
                "active_main": "sub",
                "profiles": {
                    "main": {"temperature": 0.2},
                    "extra": {
                        "provider": "openai",
                        "model": "x",
                        "max_tokens": 8192,
                        "max_context_tokens": 32000,
                    },
                },
            }
        },
    )

    assert merged["models"]["active_main"] == "sub"
    assert merged["models"]["profiles"]["main"] == {
        "provider": "openai",
        "model": "gpt-4o",
        "max_tokens": 32768,
        "max_context_tokens": 128000,
        "temperature": 0.2,
    }
    assert "sub" in merged["models"]["profiles"]
    assert "extra" in merged["models"]["profiles"]


def test_parse_config_defaults_session_auto_save_enabled() -> None:
    config = ConfigLoader()._parse_config({})

    assert config.session_auto_save is True


def test_parse_config_reads_tool_argument_diagnostics_settings() -> None:
    config = ConfigLoader()._parse_config(
        {
            "diagnostics": {
                "tool_argument_validation": {
                    "enabled": False,
                    "record_clean": True,
                }
            }
        }
    )

    settings = config.diagnostics.tool_argument_validation
    assert settings.enabled is False
    assert settings.record_clean is True


def test_parse_config_reads_llm_trace_diagnostics_settings() -> None:
    config = ConfigLoader()._parse_config(
        {
            "diagnostics": {
                "llm_trace": {
                    "enabled": True,
                    "raw_chunks": True,
                }
            }
        }
    )

    settings = config.diagnostics.llm_trace
    assert settings.enabled is True
    assert settings.raw_chunks is True
    assert config.llm_debug_trace is True
    assert config.llm_debug_trace_authoritative is True
    assert config.llm_debug_raw_chunks is True


def test_parse_config_rejects_deprecated_llm_config_paths() -> None:
    with pytest.raises(DeprecatedConfigError) as exc:
        ConfigLoader()._parse_config(
            {
                "app": {"model": "gpt-4o"},
                "model": "gpt-4o",
                "llm_debug_trace": True,
                "models": {
                    "profiles": {
                        "main": {
                            "provider": "openai",
                            "model": "gpt-4o",
                            "api_key": "sk-old",
                            "base_url": "https://api.openai.com/v1",
                        }
                    }
                },
            }
        )

    message = str(exc.value)
    assert "app was removed" in message
    assert "model was removed" in message
    assert "llm_debug_trace was removed; use diagnostics.llm_trace" in message
    assert "models.profiles.main.api_key was removed" in message
    assert "models.profiles.main.base_url was removed" in message


def test_load_explicit_config_ignores_global_example(
    tmp_path: Path,
    monkeypatch,
) -> None:
    global_path = tmp_path / "global.yaml"
    workspace_path = tmp_path / "workspace.yaml"
    explicit_path = tmp_path / "host.yaml"
    global_path.write_text("meta:\n  example: true\n", encoding="utf-8")
    explicit_path.write_text(
        """
remote_exec:
  enabled: true
  host_mode: true
auth:
  token_secret: test-secret
providers:
  items: {}
models:
  profiles: {}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigLoader, "GLOBAL_CONFIG_PATH", global_path)
    monkeypatch.setattr(ConfigLoader, "WORKSPACE_CONFIG_PATH", workspace_path)

    config = ConfigLoader(explicit_path).load()

    assert config.remote_exec.enabled is True
    assert config.remote_exec.host_mode is True
    assert not workspace_path.exists()


def test_load_explicit_config_ignores_global_and_workspace_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    global_path = tmp_path / "global.yaml"
    workspace_path = tmp_path / "workspace" / ".rcoder" / "config.yaml"
    explicit_path = tmp_path / "host.yaml"
    global_path.write_text(
        """
providers:
  items:
    global-provider:
      type: openai_chat
      api_key: global-key
models:
  profiles:
    global-main:
      provider: global-provider
      model: global-model
      max_tokens: 4096
      max_context_tokens: 128000
""".strip(),
        encoding="utf-8",
    )
    workspace_path.parent.mkdir(parents=True)
    workspace_path.write_text(
        """
providers:
  items:
    workspace-provider:
      type: openai_chat
      api_key: workspace-key
models:
  profiles:
    workspace-main:
      provider: workspace-provider
      model: workspace-model
      max_tokens: 8192
      max_context_tokens: 256000
modes:
  active: workspace-only
  profiles:
    workspace-only:
      name: workspace-only
      description: Workspace-only mode
""".strip(),
        encoding="utf-8",
    )
    explicit_path.write_text(
        """
remote_exec:
  enabled: true
  host_mode: true
auth:
  token_secret: test-secret
providers:
  items:
    host-provider:
      type: openai_chat
      api_key: host-key
models:
  profiles:
    main:
      provider: host-provider
      model: host-model
      max_tokens: 123
      max_context_tokens: 456
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ConfigLoader, "GLOBAL_CONFIG_PATH", global_path)
    monkeypatch.setattr(ConfigLoader, "WORKSPACE_CONFIG_PATH", workspace_path)

    config = ConfigLoader(explicit_path).load()

    assert set(config.providers.items) == {"host-provider"}
    assert set(config.model_profiles) == {"main"}
    assert config.model_profiles["main"].model == "host-model"
    assert config.model_profiles["main"].max_tokens == 123
    assert config.active_mode == "coder"
    assert "workspace-only" not in config.modes


def test_parse_config_reads_memory_provider_settings() -> None:
    config = ConfigLoader()._parse_config(
        {
            "memory": {
                "enabled": True,
                "backend": "sqlite",
                "store_path": ".rcoder/test-memory.sqlite3",
                "default_agent_id": "core",
                "default_namespace": "core-private",
                "token_budget": 512,
                "capture_enabled": True,
            }
        }
    )

    assert config.memory.enabled is True
    assert config.memory.backend == "sqlite"
    assert config.memory.store_path == ".rcoder/test-memory.sqlite3"
    assert config.memory.default_agent_id == "core"
    assert config.memory.default_namespace == "core-private"
    assert config.memory.token_budget == 512
    assert config.memory.capture_enabled is True


def test_host_config_keeps_session_auto_save_enabled() -> None:
    config_path = Path(__file__).resolve().parents[3] / "docker" / "config.host.yaml"
    data = ConfigLoader()._load_yaml(config_path)

    assert data["session"]["auto_save"] is True


def test_host_config_uses_auto_persistence_without_database_url(monkeypatch) -> None:
    monkeypatch.delenv("RCODER_MODEL", raising=False)
    monkeypatch.delenv("RCODER_API_KEY", raising=False)
    monkeypatch.delenv("RCODER_BASE_URL", raising=False)
    monkeypatch.setenv("LABRASTRO_AUTH_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("LABRASTRO_SUPERADMIN_USERNAME", "admin")
    monkeypatch.setenv("LABRASTRO_SUPERADMIN_PASSWORD", "plain-admin-password")
    monkeypatch.setenv(
        "LABRASTRO_SANDBOX_HOST_BASE_URL",
        "http://labrastro-host:8765",
    )
    monkeypatch.delenv("LABRASTRO_DATABASE_URL", raising=False)
    config_path = Path(__file__).resolve().parents[3] / "docker" / "config.host.yaml"

    loader = ConfigLoader()
    data = loader._expand_env_refs(loader._load_yaml(config_path))
    config = loader._parse_config(data)

    assert config.auth.store_backend == "auto"
    assert config.persistence.backend == "auto"
    assert config.persistence.database_url == ""
    assert config.auth.superadmins[0].password == "plain-admin-password"
    assert config.model_profiles == {}
    assert "api_key is required" not in config.validate()
    assert "persistence.database_url is required when backend is postgres" not in config.validate()


def test_parse_config_selects_active_profiles_and_modes() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "providers": {
                "items": {
                    "main-provider": {
                        "type": "openai_chat",
                        "api_key": "main-key",
                    },
                    "sub-provider": {
                        "type": "openai_chat",
                        "api_key": "sub-key",
                    },
                }
            },
            "models": {
                "active_main": "main",
                "active_sub": "sub",
                "profiles": {
                    "main": {
                        "provider": "main-provider",
                        "model": "gpt-main",
                        "max_tokens": 8192,
                        "max_context_tokens": 64000,
                        "temperature": 0.1,
                        "preserve_reasoning_content": True,
                        "backfill_reasoning_content_for_tool_calls": True,
                    },
                    "sub": {
                        "provider": "sub-provider",
                        "model": "gpt-sub",
                        "max_tokens": 4096,
                        "max_context_tokens": 32000,
                        "temperature": 0.2,
                    },
                },
            },
            "modes": {
                "active": "coder",
                "profiles": {
                    "coder": {
                        "description": "Code mode",
                        "tools": ["shell", "read_file"],
                    }
                },
            },
            "approval": {
                "default_mode": "warn",
                "rules": [{"tool_name": "shell", "action": "deny"}],
            },
            "skills": {"enabled": True, "scan_project": False, "disabled": ["demo"]},
            "prompt": {"system_append": "Always answer in Chinese."},
        }
    )

    assert config.model == "gpt-main"
    assert config.api_key == "main-key"
    assert config.active_main_model_profile == "main"
    assert config.active_sub_model_profile == "sub"
    assert config.active_mode == "coder"
    assert config.modes["coder"].tools == ["shell", "read_file"]
    assert config.approval.default_mode == "warn"
    assert config.approval.rules[0].tool_name == "shell"
    assert config.skills.scan_project is False
    assert config.skills.disabled == ["demo"]
    assert config.prompt.system_append == "Always answer in Chinese."
    assert config.preserve_reasoning_content is True
    assert config.backfill_reasoning_content_for_tool_calls is True


def test_parse_config_reads_provider_backed_profiles() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "providers": {
                "items": {
                    "anthropic-main": {
                        "type": "anthropic_messages",
                        "compat": "deepseek",
                        "api_key": "sk-ant",
                        "base_url": "https://api.anthropic.com",
                        "api_features": {"thinking": True},
                    }
                }
            },
            "models": {
                "active_main": "main",
                "profiles": {
                    "main": {
                        "provider": "anthropic-main",
                        "model": "claude-sonnet",
                        "max_tokens": 8192,
                        "max_context_tokens": 200000,
                    }
                },
            },
            "modes": {"profiles": {"coder": {}}},
        }
    )

    assert config.providers.items["anthropic-main"].type == "anthropic_messages"
    assert config.providers.items["anthropic-main"].compat == "deepseek"
    assert config.model_profiles["main"].provider == "anthropic-main"
    assert "coder" not in config.agent_registry.agents
    assert config.api_key == "sk-ant"
    assert config.base_url == "https://api.anthropic.com"


def test_parse_config_keeps_existing_agent_default_model() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
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
            "models": {
                "active_main": "legacy-main",
                "profiles": {
                    "legacy-main": {
                        "provider": "deepseek",
                        "model": "V4FLASH",
                        "max_tokens": 384000,
                        "max_context_tokens": 1000000,
                    }
                },
            },
            "modes": {"profiles": {"coder": {}}},
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
    )

    coder_model = config.agent_registry.agents["coder"].model
    assert coder_model.provider == "deepseek"
    assert coder_model.model == "V4PRO"
    assert coder_model.display_name == "V4 Pro"


def test_expand_env_refs_expands_provider_runtime_fields(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LABRASTRO_PROVIDER_KEY", "sk-env")
    monkeypatch.setenv("LABRASTRO_BASE_URL", "https://env.example/v1")

    expanded = ConfigLoader()._expand_env_refs(
        {
            "providers": {
                "items": {
                    "openai": {
                        "type": "openai_chat",
                        "api_key": "${LABRASTRO_PROVIDER_KEY}",
                        "base_url": "${LABRASTRO_BASE_URL}",
                    }
                }
            },
        }
    )

    assert expanded["providers"]["items"]["openai"]["api_key"] == "sk-env"
    assert expanded["providers"]["items"]["openai"]["base_url"] == "https://env.example/v1"


def test_expand_env_refs_reports_missing_env_var() -> None:
    loader = ConfigLoader()

    with pytest.raises(ConfigEnvironmentError) as exc:
        loader._expand_env_refs(
            {
                "providers": {
                    "items": {
                        "openai": {
                            "type": "openai_chat",
                            "api_key": "${LABRASTRO_MISSING_KEY}",
                        }
                    }
                }
            }
        )

    assert "LABRASTRO_MISSING_KEY" in str(exc.value)


def test_parse_config_reads_remote_exec_settings() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "modes": {"profiles": {"coder": {}}},
            "remote_exec": {
                "enabled": True,
                "host_mode": True,
                "relay_bind": "0.0.0.0:9999",
                "bootstrap_token_ttl_sec": 111,
                "peer_token_ttl_sec": 222,
                "heartbeat_interval_sec": 7,
                "heartbeat_timeout_sec": 21,
                "default_tool_timeout_sec": 44,
                "shell_timeout_sec": 155,
            },
            "auth": {
                "enabled": True,
                "token_secret": "token-secret",
                "store_backend": "postgres",
                "password_min_length": 12,
                "password_max_length": 128,
                "login_rate_limit_count": 3,
                "login_rate_limit_window_sec": 600,
                "superadmins": [
                    {
                        "username": "admin",
                        "password": "plain-admin-password",
                    }
                ],
            },
        }
    )

    assert config.remote_exec.enabled is True
    assert config.remote_exec.host_mode is True
    assert config.remote_exec.relay_bind == "0.0.0.0:9999"
    assert config.remote_exec.bootstrap_token_ttl_sec == 111
    assert config.remote_exec.peer_token_ttl_sec == 222
    assert config.remote_exec.heartbeat_interval_sec == 7
    assert config.remote_exec.heartbeat_timeout_sec == 21
    assert config.remote_exec.default_tool_timeout_sec == 44
    assert config.remote_exec.shell_timeout_sec == 155
    assert config.auth.enabled is True
    assert config.auth.token_secret == "token-secret"
    assert config.auth.store_backend == "postgres"
    assert config.auth.password_min_length == 12
    assert config.auth.password_max_length == 128
    assert config.auth.login_rate_limit_count == 3
    assert config.auth.login_rate_limit_window_sec == 600
    assert config.auth.superadmins[0].username == "admin"
    assert config.auth.superadmins[0].password == "plain-admin-password"


def test_parse_config_reads_peer_mcp_artifacts() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "mcp": {
                "artifact_root": "/srv/rcoder/mcp-artifacts",
                "servers": {
                    "local-filesystem": {
                        "placement": "peer",
                        "version": "1.0.0",
                        "launch": {
                            "command": "{{bundle}}/filesystem-mcp",
                            "args": ["--root", "{{workspace}}"],
                            "env": {"MODE": "local"},
                        },
                        "artifacts": {
                            "linux-amd64": {
                                "path": "local-filesystem/1.0.0/linux-amd64.tar.gz",
                                "sha256": "abc123",
                                "launch": {"command": "{{bundle}}/run.sh"},
                            }
                        },
                        "requirements": {"node": "required", "npm": "required"},
                        "build": {"type": "node", "package": "@demo/filesystem"},
                        "permissions": {
                            "tools": {"write_file": "require_approval"}
                        },
                    }
                },
            },
            "modes": {"profiles": {"coder": {}}},
        }
    )

    assert config.mcp_artifact_root == "/srv/rcoder/mcp-artifacts"
    server = config.mcp_servers[0]
    assert server.placement == "peer"
    assert server.distribution == "artifact"
    assert server.version == "1.0.0"
    assert server.launch is not None
    assert server.launch.command == "{{bundle}}/filesystem-mcp"
    assert server.artifacts["linux-amd64"].launch is not None
    assert server.artifacts["linux-amd64"].launch.command == "{{bundle}}/run.sh"
    assert server.artifacts["linux-amd64"].sha256 == "abc123"
    assert server.requirements["node"] == "required"
    assert server.build["type"] == "node"
    assert server.permissions["tools"]["write_file"] == "require_approval"


def test_parse_config_reads_command_mcp_manifest_fields() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "mcp": {
                "servers": {
                    "gitnexus": {
                        "command": "gitnexus",
                        "args": ["mcp"],
                        "placement": "peer",
                        "distribution": "command",
                        "version": "1.6.3",
                        "check": "gitnexus --version",
                        "install": "npm install -g gitnexus@1.6.3",
                        "source": "npm:gitnexus",
                        "description": "Repository indexing MCP server",
                        "requirements": {"node": ">=20", "npm": "required"},
                        "docs": [{"title": "GitNexus MCP", "url": "https://example.test/mcp"}],
                        "install_prompt": "Install through npm.",
                        "verify_prompt": "Verify mcp startup.",
                        "notes": ["Do not install node automatically."],
                    }
                }
            },
            "modes": {"profiles": {"coder": {}}},
        }
    )

    server = config.mcp_servers[0]
    assert server.name == "gitnexus"
    assert server.distribution == "command"
    assert server.args == ["mcp"]
    assert server.check == "gitnexus --version"
    assert server.install == "npm install -g gitnexus@1.6.3"
    assert server.source == "npm:gitnexus"
    assert server.description == "Repository indexing MCP server"
    assert server.requirements["node"] == ">=20"
    assert server.docs[0]["title"] == "GitNexus MCP"
    assert server.install_prompt == "Install through npm."
    assert server.verify_prompt == "Verify mcp startup."
    assert server.notes == ["Do not install node automatically."]


def test_parse_config_reads_environment_cli_tools() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "modes": {"profiles": {"coder": {}}},
            "environment": {
                "cli_tools": {
                    "gitnexus": {
                        "command": "gitnexus",
                        "tags": ["repo_index"],
                        "check": "gitnexus --version",
                        "install": "npm install -g gitnexus",
                        "version": "latest",
                        "source": "npm",
                        "description": "Repository graph CLI",
                        "docs": [{"title": "GitNexus", "url": "https://example.test/gitnexus"}],
                        "install_prompt": "Use configured npm command.",
                        "verify_prompt": "Run version check.",
                        "notes": ["PATH changes need approval."],
                    }
                }
            },
        }
    )

    tool = config.environment.cli_tools["gitnexus"]
    assert tool.command == "gitnexus"
    assert tool.tags == ["repo_index"]
    assert tool.check == "gitnexus --version"
    assert tool.install == "npm install -g gitnexus"
    assert tool.version == "latest"
    assert tool.source == "npm"
    assert tool.description == "Repository graph CLI"
    assert tool.docs[0]["url"] == "https://example.test/gitnexus"
    assert tool.install_prompt == "Use configured npm command."
    assert tool.verify_prompt == "Run version check."
    assert tool.notes == ["PATH changes need approval."]


def test_parse_config_reads_environment_skills() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "modes": {"profiles": {"coder": {}}},
            "environment": {
                "skills": {
                    "collaborating-with-claude": {
                        "scope": "user",
                        "check": "Test-Path ~/.agents/skills/collaborating-with-claude/SKILL.md",
                        "install": "python install-skill.py",
                        "version": "1.0.0",
                        "source": "github",
                        "description": "Claude bridge skill",
                        "path_hint": "~/.agents/skills/collaborating-with-claude/SKILL.md",
                        "docs": [{"title": "Claude skill", "url": "https://example.test/skill"}],
                        "install_prompt": "Install the skill files.",
                        "verify_prompt": "Check SKILL.md exists.",
                        "notes": ["Use user scope."],
                    }
                }
            },
        }
    )

    skill = config.environment.skills["collaborating-with-claude"]
    assert skill.scope == "user"
    assert skill.check == "Test-Path ~/.agents/skills/collaborating-with-claude/SKILL.md"
    assert skill.install == "python install-skill.py"
    assert skill.version == "1.0.0"
    assert skill.source == "github"
    assert skill.description == "Claude bridge skill"
    assert skill.path_hint == "~/.agents/skills/collaborating-with-claude/SKILL.md"
    assert skill.docs[0]["title"] == "Claude skill"
    assert skill.install_prompt == "Install the skill files."
    assert skill.verify_prompt == "Check SKILL.md exists."
    assert skill.notes == ["Use user scope."]


def test_parse_config_falls_back_when_active_profile_missing() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "providers": {
                "items": {
                    "openai": {
                        "type": "openai_chat",
                        "api_key": "key-1",
                    }
                }
            },
            "models": {
                "active_main": "missing",
                "profiles": {
                    "first": {
                        "provider": "openai",
                        "model": "gpt-first",
                        "max_tokens": 8192,
                        "max_context_tokens": 128000,
                    },
                },
            },
            "modes": {"profiles": {"coder": {}}},
        }
    )

    assert config.active_main_model_profile == "first"
    assert config.active_sub_model_profile == "first"
    assert config.model == "gpt-first"


def test_merge_dicts_preserves_active_main_and_active_sub_across_layers() -> None:
    """Workspace active_main / active_sub must override global values."""
    loader = ConfigLoader()

    # Simulate global config
    global_data = {
        "models": {
            "active_main": "glm-5",
            "profiles": {
                "glm-5": {"provider": "zhipu", "model": "glm-5"},
                "ds-v4-pro": {"provider": "deepseek", "model": "deepseek-v4-pro"},
                "ds-v4-flash": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                },
            },
        }
    }

    # Simulate workspace override
    workspace_data = {
        "models": {
            "active_main": "ds-v4-pro",
            "active_sub": "ds-v4-flash",
        }
    }

    merged = loader._merge_dicts(global_data, workspace_data)

    assert merged["models"]["active_main"] == "ds-v4-pro"
    assert merged["models"]["active_sub"] == "ds-v4-flash"
    # Profiles from global should survive
    assert "glm-5" in merged["models"]["profiles"]
    assert merged["models"]["profiles"]["ds-v4-pro"]["model"] == "deepseek-v4-pro"


def test_merge_dicts_preserves_mcp_scalar_fields_across_layers() -> None:
    """MCP scalar fields such as artifact_root must merge with override priority."""
    loader = ConfigLoader()

    global_data = {
        "mcp": {
            "artifact_root": "/srv/rcoder/artifacts",
            "servers": {"filesystem": {"command": "node", "args": ["server.js"]}},
        }
    }
    workspace_data = {"mcp": {"artifact_root": ".rcoder/mcp-artifacts"}}

    merged = loader._merge_dicts(global_data, workspace_data)

    assert merged["mcp"]["artifact_root"] == ".rcoder/mcp-artifacts"
    assert "filesystem" in merged["mcp"]["servers"]


def test_is_example_config_detects_example_flag() -> None:
    """Global config with meta.example should be detected as example."""
    assert ConfigLoader._is_example_config({"meta": {"example": True}})
    assert ConfigLoader._is_example_config({"meta": {"example": True, "other": 1}})
    assert not ConfigLoader._is_example_config({})
    assert not ConfigLoader._is_example_config({"meta": {}})
    assert not ConfigLoader._is_example_config({"meta": {"example": False}})
    assert not ConfigLoader._is_example_config({"models": {"profiles": {}}})


def test_load_allows_explicit_remote_host_without_models(tmp_path: Path) -> None:
    config_path = tmp_path / "host.yaml"
    config_path.write_text(
        """
remote_exec:
  enabled: true
  host_mode: true
auth:
  enabled: true
  token_secret: test-secret
  superadmins:
    - username: admin
      password: plain-admin-password
sandbox_provider:
  type: docker
  host_base_url: http://labrastro-host:8765
""".strip(),
        encoding="utf-8",
    )
    global_path = tmp_path / "home" / "config.yaml"
    workspace_path = tmp_path / "workspace" / ".rcoder" / "config.yaml"

    with patch.object(ConfigLoader, "GLOBAL_CONFIG_PATH", global_path), patch.object(
        ConfigLoader, "WORKSPACE_CONFIG_PATH", workspace_path
    ):
        config = ConfigLoader(config_path).load()

    assert config.remote_exec.enabled is True
    assert config.remote_exec.host_mode is True
    assert config.model_profiles == {}
    assert "api_key is required" not in config.validate()


def test_load_still_requires_runtime_config_for_non_host_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "local.yaml"
    config_path.write_text("session:\n  auto_save: true\n", encoding="utf-8")
    global_path = tmp_path / "home" / "config.yaml"
    workspace_path = tmp_path / "workspace" / ".rcoder" / "config.yaml"

    with patch.object(ConfigLoader, "GLOBAL_CONFIG_PATH", global_path), patch.object(
        ConfigLoader, "WORKSPACE_CONFIG_PATH", workspace_path
    ):
        with pytest.raises(ExampleConfigError):
            ConfigLoader(config_path).load()


def test_generate_example_config_creates_valid_yaml(tmp_path: Path) -> None:
    """Generated example config should be syntactically correct."""
    from unittest.mock import patch

    loader = ConfigLoader()
    example_path = tmp_path / "config.yaml"

    with patch.object(ConfigLoader, "GLOBAL_CONFIG_PATH", example_path):
        loader._generate_example_global_config()

    assert example_path.exists()
    data = loader._load_yaml(example_path)
    assert data["meta"]["example"] is True
    assert data["providers"]["items"] == {}
    assert "models" in data
    assert "profiles" in data["models"]
    assert data["models"]["profiles"] == {}
    assert "modes" in data
    assert data["modes"]["active"] == "coder"
    runtime_profiles = data["runtime_profiles"]
    agent_registry = data["agent_registry"]
    assert runtime_profiles["environment_local"]["executor"] == "reuleauxcoder"
    assert (
        runtime_profiles["environment_local"]["execution_location"]
        == "local_workspace"
    )
    assert runtime_profiles["environment_local"]["runtime_home_policy"] == "per_task"
    assert runtime_profiles["environment_local"]["approval_mode"] == "full"
    assert "environment_configurator" in agent_registry["agents"]
    assert "server manifest" in agent_registry["agents"]["environment_configurator"]["dispatch"]["profile"]
    assert agent_registry["agents"]["environment_configurator"]["capability_refs"] == [
        "environment"
    ]


def test_load_does_not_copy_global_environment_manifest_into_workspace(
    tmp_path: Path,
) -> None:
    global_path = tmp_path / "home" / "config.yaml"
    workspace_path = tmp_path / "workspace" / ".rcoder" / "config.yaml"
    global_path.parent.mkdir(parents=True)
    global_path.write_text(
        """
providers:
  items:
    openai:
      type: openai_chat
      api_key: key
models:
  profiles:
    main:
      provider: openai
      model: gpt-main
      max_tokens: 8192
      max_context_tokens: 128000
environment:
  cli_tools:
    gitnexus:
      command: gitnexus
      check: gitnexus --version
""".strip(),
        encoding="utf-8",
    )

    with patch.object(ConfigLoader, "GLOBAL_CONFIG_PATH", global_path), patch.object(
        ConfigLoader, "WORKSPACE_CONFIG_PATH", workspace_path
    ):
        config = ConfigLoader().load()

    workspace_data = ConfigLoader()._load_yaml(workspace_path)
    assert "gitnexus" in config.environment.cli_tools
    assert "environment" not in workspace_data
    assert workspace_data["meta"]["workspace_bootstrapped"] is True
    assert "modes" in workspace_data

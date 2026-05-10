from pathlib import Path
from unittest.mock import patch

import pytest

from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.services.config.loader import ConfigEnvironmentError


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
        {"app": {"model": "a", "temperature": 0.0}},
        {"app": {"temperature": 0.5}},
    )
    assert merged == {"app": {"model": "a", "temperature": 0.5}}


def test_merge_dicts_merges_profile_maps_by_name() -> None:
    loader = ConfigLoader()
    merged = loader._merge_dicts(
        {
            "models": {
                "active_main": "main",
                "profiles": {
                    "main": {"model": "gpt-4o", "api_key": "k1"},
                    "sub": {"model": "gpt-4o-mini", "api_key": "k2"},
                },
            }
        },
        {
            "models": {
                "active_main": "sub",
                "profiles": {
                    "main": {"temperature": 0.2},
                    "extra": {"model": "x", "api_key": "k3"},
                },
            }
        },
    )

    assert merged["models"]["active_main"] == "sub"
    assert merged["models"]["profiles"]["main"] == {
        "model": "gpt-4o",
        "api_key": "k1",
        "temperature": 0.2,
    }
    assert "sub" in merged["models"]["profiles"]
    assert "extra" in merged["models"]["profiles"]


def test_parse_config_defaults_session_auto_save_enabled() -> None:
    config = ConfigLoader()._parse_config({})

    assert config.session_auto_save is True


def test_host_config_keeps_session_auto_save_enabled() -> None:
    config_path = Path(__file__).resolve().parents[3] / "docker" / "config.host.yaml"
    data = ConfigLoader()._load_yaml(config_path)

    assert data["session"]["auto_save"] is True


def test_parse_config_selects_active_profiles_and_modes() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "models": {
                "active_main": "main",
                "active_sub": "sub",
                "profiles": {
                    "main": {
                        "model": "gpt-main",
                        "api_key": "main-key",
                        "temperature": 0.1,
                        "preserve_reasoning_content": True,
                        "backfill_reasoning_content_for_tool_calls": True,
                    },
                    "sub": {
                        "model": "gpt-sub",
                        "api_key": "sub-key",
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
                    }
                },
            },
            "modes": {"profiles": {"coder": {}}},
        }
    )

    assert config.providers.items["anthropic-main"].type == "anthropic_messages"
    assert config.providers.items["anthropic-main"].compat == "deepseek"
    assert config.model_profiles["main"].provider == "anthropic-main"
    assert "coder" not in config.agent_runtime.agents
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
                    }
                },
            },
            "modes": {"profiles": {"coder": {}}},
            "agent_runtime": {
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

    coder_model = config.agent_runtime.agents["coder"].model
    assert coder_model.provider == "deepseek"
    assert coder_model.model == "V4PRO"
    assert coder_model.display_name == "V4 Pro"


def test_expand_env_refs_expands_provider_and_profile_runtime_fields(
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
                    }
                }
            },
            "models": {
                "profiles": {
                    "main": {
                        "model": "gpt",
                        "api_key": "${LABRASTRO_PROVIDER_KEY}",
                        "base_url": "${LABRASTRO_BASE_URL}",
                    }
                }
            },
        }
    )

    assert expanded["providers"]["items"]["openai"]["api_key"] == "sk-env"
    assert expanded["models"]["profiles"]["main"]["api_key"] == "sk-env"
    assert expanded["models"]["profiles"]["main"]["base_url"] == "https://env.example/v1"


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
            "app": {"api_key": "key"},
            "models": {
                "profiles": {"main": {"model": "gpt-main", "api_key": "main-key"}}
            },
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
                        "password_hash": "pbkdf2_sha256$260000$salt$hash",
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


def test_parse_config_reads_peer_mcp_artifacts() -> None:
    loader = ConfigLoader()
    config = loader._parse_config(
        {
            "models": {
                "profiles": {"main": {"model": "gpt-main", "api_key": "key"}}
            },
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
            "models": {
                "profiles": {"main": {"model": "gpt-main", "api_key": "key"}}
            },
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
            "models": {
                "profiles": {"main": {"model": "gpt-main", "api_key": "key"}}
            },
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
            "models": {
                "profiles": {"main": {"model": "gpt-main", "api_key": "key"}}
            },
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
            "models": {
                "active_main": "missing",
                "profiles": {
                    "first": {"model": "gpt-first", "api_key": "key-1"},
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
                "glm-5": {"model": "glm-5", "api_key": "k"},
                "ds-v4-pro": {"model": "deepseek-v4-pro", "api_key": "k"},
                "ds-v4-flash": {"model": "deepseek-v4-flash", "api_key": "k"},
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
    assert "models" in data
    assert "profiles" in data["models"]
    assert "default" in data["models"]["profiles"]
    assert data["models"]["profiles"]["default"]["api_key"] == "your-api-key-here"
    assert "modes" in data
    assert data["modes"]["active"] == "coder"
    runtime = data["agent_runtime"]
    assert runtime["runtime_profiles"]["environment_local"]["executor"] == "reuleauxcoder"
    assert (
        runtime["runtime_profiles"]["environment_local"]["execution_location"]
        == "local_workspace"
    )
    assert runtime["runtime_profiles"]["environment_local"]["runtime_home_policy"] == "per_task"
    assert runtime["runtime_profiles"]["environment_local"]["approval_mode"] == "full"
    assert "environment_configurator" in runtime["agents"]
    assert "server manifest" in runtime["agents"]["environment_configurator"]["dispatch"]["profile"]
    assert runtime["agents"]["environment_configurator"]["capability_refs"] == [
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
models:
  profiles:
    main:
      model: gpt-main
      api_key: key
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

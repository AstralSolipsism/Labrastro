"""Configuration schema - YAML structure definition."""

# Expected YAML structure for config.yaml
CONFIG_SCHEMA = {
    "models": {
        "active_main": "string (optional, defaults to first profile key)",
        "active_sub": "string (optional, defaults to active_main)",
        "profiles": {
            "profile_name": {
                "model": "string (required)",
                "provider": "string (optional, references providers.items.<provider_id>)",
                "api_key": "string (required)",
                "base_url": "string (optional)",
                "max_tokens": "int (default: 4096)",
                "temperature": "float (default: 0.0)",
                "max_context_tokens": "int (default: 128000)",
                "preserve_reasoning_content": "bool (default: true, persist/round-trip provider reasoning_content)",
                "backfill_reasoning_content_for_tool_calls": "bool (default: false, inject empty reasoning_content for assistant tool calls when missing)",
                "thinking_enabled": "bool (optional, enable provider thinking/reasoning mode for this profile)",
                "reasoning_effort": "string (optional, provider-specific reasoning effort, e.g. high/max)",
                "reasoning_replay_mode": "string (optional, one of: none, tool_calls; controls which historical assistant reasoning_content is replayed)",
                "reasoning_replay_placeholder": "string (optional, placeholder text injected when backfilling missing reasoning_content; default: [PLACE_HOLDER])",
            }
        },
    },
    "providers": {
        "items": {
            "provider_id": {
                "type": "string (one of openai_chat, anthropic_messages, openai_responses)",
                "compat": "string (optional, one of generic, deepseek, kimi, glm, qwen, zenmux; inferred from base_url when omitted)",
                "api_key": "string (optional, supports ${ENV_NAME})",
                "base_url": "string (optional, supports ${ENV_NAME})",
                "headers": "dict of strings (optional)",
                "timeout_sec": "int (default: 120)",
                "max_retries": "int (default: 3)",
                "api_features": "dict of booleans (optional)",
                "extra": "dict (optional, provider-specific settings)",
            }
        }
    },
    "modes": {
        "active": "string (optional, default: coder)",
        "profiles": {
            "mode_name": {
                "description": "string (optional)",
                "tools": "list of strings (optional, default: all tools)",
                "prompt_append": "string (optional)",
            }
        },
    },
    "approval": {
        "default_mode": "string (default: require_approval, one of allow/warn/require_approval/deny)",
        "rules": [
            {
                "tool_name": "string (optional)",
                "tool_source": "string (optional)",
                "mcp_server": "string (optional)",
                "effect_class": "string (optional)",
                "profile": "string (optional)",
                "action": "string (required, one of allow/warn/require_approval/deny)",
            }
        ],
    },
    "tool_output": {
        "max_chars": "int (default: 12000)",
        "max_lines": "int (default: 120)",
        "store_full_output": "bool (default: true)",
        "store_dir": "string (optional, default: ./.rcoder/tool-outputs, fallback ~/.rcoder/tool-outputs)",
    },
    "session": {
        "auto_save": "bool (default: true)",
        "dir": "string (optional, default: ./.rcoder/sessions, fallback ~/.rcoder/sessions)",
    },
    "cli": {
        "history_file": "string (optional, default: ~/.rcoder/history)",
    },
    "skills": {
        "enabled": "bool (default: true)",
        "scan_project": "bool (default: true)",
        "scan_user": "bool (default: true)",
        "disabled": ["skill-name", "..."],
    },
    "prompt": {
        "system_append": "string (optional, appended to system prompt as user/workspace instructions)",
    },
    "context": {
        "snip_keep_recent_tools": "int (default: 5, number of recent tool calls to protect from snipping)",
        "snip_threshold_chars": "int (default: 1500, min content length to trigger snip)",
        "snip_min_lines": "int (default: 6, min line count to trigger snip)",
        "summarize_keep_recent_turns": "int (default: 5, number of recent user turns to protect during summarize)",
    },
    "mcp": {
        "servers": {
            "server_name": {
                "command": "string (required)",
                "args": "list of strings (optional)",
                "env": "dict of strings (optional)",
                "cwd": "string (optional)",
                "enabled": "bool (optional, default: true)",
                "placement": "string (optional, one of server, peer, both)",
                "distribution": "string (optional, one of command, artifact)",
                "requirements": "dict of strings (optional)",
                "check": "string (optional)",
                "install": "string (optional)",
                "version": "string (optional)",
                "source": "string (optional)",
                "description": "string (optional)",
                "repo_url": "string (optional)",
                "docs": [{"title": "string", "url": "string"}],
                "evidence": [{"field": "string", "title": "string", "url": "string", "excerpt": "string"}],
                "install_prompt": "string (optional)",
                "verify_prompt": "string (optional)",
                "notes": ["string", "..."],
                "credentials": ["credential-name", "..."],
                "risk_level": "string (optional)",
            }
        }
    },
    "remote_exec": {
        "enabled": "bool (default: false)",
        "host_mode": "bool (default: false)",
        "relay_bind": "string (default: 127.0.0.1:8765)",
        "bootstrap_token_ttl_sec": "int (default: 300)",
        "peer_token_ttl_sec": "int (default: 3600)",
        "heartbeat_interval_sec": "int (default: 10)",
        "heartbeat_timeout_sec": "int (default: 30)",
        "default_tool_timeout_sec": "int (default: 30)",
        "shell_timeout_sec": "int (default: 120)",
    },
    "auth": {
        "enabled": "bool (required for remote host mode)",
        "token_secret": "string (required when auth.enabled=true)",
        "access_token_ttl_sec": "int (default: 900)",
        "refresh_token_ttl_sec": "int (default: 2592000)",
        "password_hash_iterations": "int (default: 260000)",
        "password_min_length": "int (default: 6)",
        "password_max_length": "int (default: 256)",
        "login_rate_limit_count": "int (default: 5)",
        "login_rate_limit_window_sec": "int (default: 900)",
        "store_backend": "string (one of auto, file, postgres; default auto)",
        "store_path": "string (default: .rcoder/auth.json)",
        "superadmins": [
            {
                "username": "string",
                "password_hash": "string (pbkdf2_sha256 hash)",
            }
        ],
    },
    "run_limits": {
        "max_running_agents": "int (default: 4, global server-side Agent concurrency limit)",
        "max_shells_per_agent": "int (default: 1, per-Agent shell concurrency limit)",
    },
    "runtime_profiles": {
        "profile_id": {
            "executor": "string (one of reuleauxcoder, fake, codex, claude, gemini)",
            "execution_location": "string (one of remote_server, local_workspace)",
            "command": "string (optional, CLI command for external executors)",
            "args": "list of strings (optional)",
            "env": "dict of strings (optional, non-secret process env)",
            "runtime_home_policy": "string (optional, e.g. per_task)",
            "approval_mode": "string (optional, e.g. autonomous)",
            "config_isolation": "string (optional, e.g. per_agent)",
            "credential_refs": "dict of strings (optional, references server-managed secrets)",
            "mcp": "dict (optional, executor-native MCP settings rendered from platform config)",
        },
    },
    "agent_registry": {
        "agents": {
            "agent_id": {
                "name": "string (optional)",
                "description": "string (optional)",
                "runtime_profile": "string (required when task-dispatched, references runtime_profiles)",
                "model": {
                    "provider": "string (optional, references providers.items.<provider_id>)",
                    "model": "string (optional, provider model id)",
                    "display_name": "string (optional)",
                    "parameters": "dict (optional, model runtime parameters such as max_tokens/temperature/max_context_tokens)",
                },
                "dispatch": {
                    "profile": "string (optional, user-authored Agent routing profile)",
                    "examples": "list of strings (optional, example tasks this Agent fits)",
                    "avoid": "list of strings (optional, tasks this Agent should not receive)",
                },
                "capability_refs": "list of strings (optional, references capability_packages)",
                "prompt": {
                    "agent_md": "string (optional)",
                    "system_append": "string (optional)",
                },
                "max_concurrent_tasks": "int (optional)",
                "credential_refs": "dict of strings (optional)",
            }
        },
    },
    "capability_packages": {
        "package_id": {
            "name": "string (optional)",
            "description": "string (optional)",
            "mcp_servers": ["server-name", "..."],
            "skills": ["skill-name", "..."],
            "cli_tools": ["tool-name", "..."],
            "permissions": ["permission-name", "..."],
            "source": "string (optional, e.g. builtin/local/market)",
            "market": "dict (optional, package-market metadata)",
        }
    },
    "persistence": {
        "backend": "string (optional, one of auto, memory, postgres; default auto)",
        "database_url": "string (optional, Postgres URL; supports environment expansion)",
        "auto_migrate": "bool (default: true)",
        "runtime_enabled": "bool (default: true)",
        "sessions_enabled": "bool (default: true)",
        "retention_days": "int (default: 0, zero keeps runtime events and snapshots forever)",
    },
    "environment": {
        "cli_tools": {
            "tool_name": {
                "command": "string (required)",
                "enabled": "bool (optional, default: true)",
                "placement": "string (optional, one of server, local, both)",
                "tags": ["component-tag", "..."],
                "requirements": {"dependency": "version/range"},
                "check": "string (required)",
                "install": "string (optional)",
                "version": "string (optional)",
                "source": "string (optional)",
                "description": "string (optional)",
                "repo_url": "string (optional)",
                "docs": [{"title": "string", "url": "string"}],
                "evidence": [{"field": "string", "title": "string", "url": "string", "excerpt": "string"}],
                "install_prompt": "string (optional)",
                "verify_prompt": "string (optional)",
                "notes": ["string", "..."],
                "credentials": ["credential-name", "..."],
                "risk_level": "string (optional)",
            }
        },
        "skills": {
            "skill_name": {
                "enabled": "bool (optional, default: true)",
                "scope": "string (optional, default: project)",
                "check": "string (required)",
                "install": "string (optional)",
                "version": "string (optional)",
                "source": "string (optional)",
                "description": "string (optional)",
                "path_hint": "string (optional)",
                "requirements": {"dependency": "version/range"},
                "repo_url": "string (optional)",
                "docs": [{"title": "string", "url": "string"}],
                "evidence": [{"field": "string", "title": "string", "url": "string", "excerpt": "string"}],
                "install_prompt": "string (optional)",
                "verify_prompt": "string (optional)",
                "notes": ["string", "..."],
                "credentials": ["credential-name", "..."],
                "risk_level": "string (optional)",
            }
        },
    },
}

# Default values for configuration
BUILTIN_MODES = {
    "coder": {
        "description": "Default coding mode with full tool access.",
        "tools": ["*"],
        "prompt_append": (
            "Prioritize making concrete code changes and verifying them with commands/tests "
            "when appropriate."
        ),
    },
    "planner": {
        "description": "Planning-first mode; focus on analysis and implementation plans.",
        "tools": ["read_file", "glob", "grep"],
        "prompt_append": (
            "Focus on analysis, architecture, and step-by-step plans. Avoid file mutations "
            "unless explicitly requested."
        ),
    },
    "debugger": {
        "description": "Debugging mode focused on diagnosis and verification.",
        "tools": ["read_file", "glob", "grep", "shell"],
        "prompt_append": (
            "Focus on root-cause analysis, minimal repro steps, and targeted fixes with "
            "clear verification."
        ),
    },
    "taskflow": {
        "description": "Background long-task planning and dispatch mode.",
        "tools": ["read_file", "glob", "grep"],
        "prompt_append": (
            "Guide the user from a fuzzy goal into decisions, acceptance criteria, "
            "issue drafts, task drafts, dispatch, and completion review."
        ),
    },
}

DEFAULT_ACTIVE_MODE = "coder"


# Default values for configuration
DEFAULTS = {
    "model": "gpt-4o",
    "max_tokens": 4096,
    "temperature": 0.0,
    "max_context_tokens": 128_000,
    "approval_default_mode": "require_approval",
    "approval_rules": [
        {"tool_name": "read_file", "action": "allow"},
        {"tool_name": "glob", "action": "allow"},
        {"tool_name": "grep", "action": "allow"},
        {"tool_name": "write_file", "action": "require_approval"},
        {"tool_name": "edit_file", "action": "require_approval"},
        {"tool_name": "shell", "action": "require_approval"},
        {"tool_name": "delegate_agent", "action": "require_approval"},
        {"tool_source": "mcp", "mcp_server": "filesystem", "action": "warn"},
        {"tool_source": "mcp", "action": "require_approval"},
    ],
    "tool_output_max_chars": 12_000,
    "tool_output_max_lines": 120,
    "tool_output_store_full": True,
    "tool_output_store_dir": None,
    "session_auto_save": True,
    "session_dir": None,  # Will be computed at runtime
    "history_file": None,  # Will be computed at runtime
    "llm_debug_trace": False,
    "snip_keep_recent_tools": 5,
    "snip_threshold_chars": 1500,
    "snip_min_lines": 6,
    "summarize_keep_recent_turns": 5,
    "token_fudge_factor": 1.1,
}

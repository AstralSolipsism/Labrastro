"""Configuration schema - YAML structure definition."""

# Expected YAML structure for config.yaml
CONFIG_SCHEMA = {
    "models": {
        "active_main": "string (optional, defaults to first profile key)",
        "active_sub": "string (optional, defaults to active_main)",
        "profiles": {
            "profile_name": {
                "model": "string (required)",
                "provider": "string (required, references providers.items.<provider_id>)",
                "max_tokens": "int (required; use model capabilities or explicit user value)",
                "temperature": "float (default: 0.0)",
                "max_context_tokens": "int (required; use model capabilities or explicit user value)",
                "preserve_reasoning_content": "bool (default: true, persist/round-trip provider reasoning_content)",
                "backfill_reasoning_content_for_tool_calls": "bool (default: false, inject empty reasoning_content for assistant tool calls when missing)",
                "thinking_enabled": "bool (optional, enable provider thinking/reasoning mode for this profile)",
                "reasoning_effort": "string (optional, provider-specific reasoning effort, e.g. high/max)",
                "reasoning_replay_mode": "string (optional, one of: none, tool_calls; controls which historical assistant reasoning_content is replayed)",
                "reasoning_replay_placeholder": "string (optional, placeholder text injected when backfilling missing reasoning_content; default: [PLACE_HOLDER])",
                "capability_user_configured": "bool (default: false, true when token limits are explicitly user-entered rather than capability defaults)",
                "capability_source": "string (optional, source label for the applied model capability defaults)",
                "capability_applied_at": "string (optional, ISO timestamp when model capability defaults were applied)",
            }
        },
    },
    "providers": {
        "items": {
            "provider_id": {
                "type": "string (one of openai_chat, anthropic_messages, openai_responses)",
                "compat": "string (optional, one of generic, deepseek, kimi, glm, qwen, zenmux; inferred from base_url when omitted)",
                "enabled": "bool (default: true)",
                "api_key": "string (optional, supports ${ENV_NAME})",
                "base_url": "string (optional, supports ${ENV_NAME})",
                "headers": "dict of strings (optional)",
                "timeout_sec": "int (default: 120)",
                "max_retries": "int (default: 3)",
                "api_features": "dict of booleans (optional)",
                "stream_recovery": "dict (optional, provider stream retry/continue policy)",
                "models": "list (optional, cached/discovered provider model metadata)",
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
        "snip_keep_recent_tools": "int (default: 2, number of recent agent rounds to protect from snipping)",
        "snip_threshold_chars": "int (default: 1500, min content length to trigger snip)",
        "snip_min_lines": "int (default: 6, min line count to trigger snip)",
        "summarize_keep_recent_turns": "int (default: 5, number of recent user turns to protect during summarize)",
    },
    "diagnostics": {
        "tool_diagnostics": {
            "enabled": "bool (default: true, record tool lifecycle diagnostics)",
            "record_clean": "bool (default: false, also record clean tool executions)",
        },
        "llm_trace": {
            "enabled": "bool (default: false, persist LLM debug traces)",
            "raw_chunks": "bool (default: false, include raw streaming chunks in traces)",
        },
    },
    "memory": {
        "enabled": "bool (default: false, enable provider-backed memory runtime)",
        "default_provider": "string (required when enabled, provider id from memory.providers)",
        "default_agent_id": "string (default: core, stable owner for direct core chats)",
        "default_namespace": "string (optional, defaults to owner_agent_id)",
        "runtime": {
            "inject_default": "bool (default: true, automatic context injection)",
            "capture_default": "bool (default: true, capture session/tool events)",
            "token_budget_default": "int (default: 800, max memory tokens injected per request)",
            "fail_mode": "string (open or closed; default open)",
            "trace_enabled": "bool (default: true, emit diagnostics/trace metadata)",
            "trust_policy": "string (default: wrap_external)",
        },
        "providers": {
            "provider_id": {
                "adapter": "string (required, registered memory provider adapter)",
                "enabled": "bool (optional, default: true)",
                "...": "adapter-specific fields",
            }
        },
        "sources": {
            "source_id": {
                "adapter": "string (required, registered source connector adapter)",
                "enabled": "bool (optional, default: true)",
                "target_provider": "string (provider id to receive synced data)",
                "sync_mode": "string (manual/scheduled/watch; adapter-specific)",
                "...": "adapter-specific fields",
            }
        },
        "tools": {
            "enabled": "bool (default: false, expose memory tools to agents)",
            "provider": "string (provider id used by memory tools)",
            "allowed_agents": "list of agent ids",
            "recall": "bool",
            "remember": "bool",
            "forget": "bool",
            "list": "bool",
        },
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
                "environment_requirement_refs": "list of envreq:<kind>:<name> ids (optional)",
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
    "lsp": {
        "enabled": "bool (default: true)",
        "poll_timeout_ms": "int (default: 5000)",
        "max_diagnostics": "int (default: 20)",
        "include_warnings": "bool (default: false)",
        "servers": {
            "language": {
                "cmd": "string (optional, language-server executable override)",
                "args": "list of strings (optional)",
                "workspace_root": "string (optional)",
                "init_opts": "dict (optional)",
            }
        },
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
                "password": "string (plain login password)",
            }
        ],
    },
    "run_limits": {
        "max_running_agents": "int (default: 4, legacy aggregate display limit; runtime_slots define execution capacity)",
        "max_shells_per_agent": "int (default: 1, per-Agent shell concurrency limit)",
        "server_agent_run_slots": "int (default: max_running_agents, server worker AgentRun capacity)",
        "server_sandbox_slots": "int (default: 2, server-managed sandbox AgentRun capacity)",
        "local_peer_agent_run_slots": "int (default: 1, VSIX local peer AgentRun capacity)",
        "model_request_slots": "int (default: max_running_agents, server-origin model request capacity)",
    },
    "model_capabilities": {
        "enabled": "bool (default: true, periodically sync model capability catalog)",
        "interval_sec": "int (default: 86400, background sync interval in seconds)",
    },
    "runtime_profiles": {
        "profile_id": {
            "executor": "string (one of reuleauxcoder, fake, codex, claude, gemini)",
            "execution_location": "string (one of remote_server, local_workspace, daemon_worktree)",
            "worker_kind": "string (one of local_peer, server_worker, sandbox_worker)",
            "model_request_origin": "string (one of server, server_worker_cli, local_cli)",
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
                "visibility": "string (user/system/internal; default user)",
                "chat_entrypoint": "bool (default false; true only for the ChatView main agent)",
                "delegable": "bool (default true for user agents, false for system/internal)",
                "taskflow_eligible": "bool (default true for user agents, false for system/internal)",
                "system_flow_only": "list of system flow ids allowed to invoke internal/system agents",
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
                "memory": {
                    "enabled": "bool (default true, use global provider defaults unless false)",
                    "primary_provider": "string (optional, provider used for writes)",
                    "read_providers": "list of provider ids (optional)",
                    "inject": "bool (default true, automatic context injection)",
                    "capture": "bool (default true, capture runtime events)",
                    "token_budget": "int (optional, overrides memory.runtime token budget)",
                    "scope_mode": "string (default isolated)",
                    "expose_tools": "bool (default false, opt into memory tool surface)",
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
            "source": {
                "type": "string (github_repo/docs_url/project_notes/builtin)",
                "url": "string (optional)",
                "notes": "string (optional)",
            },
            "components": ["envreq:executable:gh", "mcp_server:github", "skill:code-review"],
            "enabled": "bool (default true)",
            "install_plan": "list of strings (optional)",
            "usage": "list of strings (optional)",
            "effective_capabilities": "list of plain-language capability summaries added to an Agent",
            "evidence": "list of dicts with url/title/excerpt (optional)",
            "credentials": "list of credential names required by package (optional)",
            "risk_level": "string (optional)",
            "execution_policy": "string (allow/deny/require_user/escalate/inherit; default inherit)",
        }
    },
    "capability_components": {
        "component_id": {
            "kind": "string (builtin_tool/credential/environment_requirement/mcp_server/mcp_tool/prompt_fragment/skill)",
            "name": "string",
            "description": "string (optional)",
            "enabled": "bool (default true)",
            "package_ids": ["package-id", "..."],
            "source": "same shape as capability_packages.source",
            "config": "dict (component-specific command/path/MCP config)",
            "access": "string (read/write/both; optional)",
            "risk_level": "string (low/medium/high; optional)",
            "execution_policy": "string (allow/deny/require_user/escalate/inherit; default inherit)",
            "registry_path": "string (optional, source registry identifier)",
            "source_path": "string (optional, docs/plugin/source pointer)",
            "managed_by": "string (capability_package/manual)",
            "status": "string (optional)",
        }
    },
    "persistence": {
        "backend": "string (optional, one of auto, memory, postgres; default auto)",
        "database_url": "string (optional, Postgres URL; supports environment expansion)",
        "auto_migrate": "bool (default: true)",
        "runtime_enabled": "bool (default: true)",
        "sessions_enabled": "bool (default: true)",
        "retention_days": "int (default: 0, zero keeps runtime events forever)",
        "event_payload_compress_threshold_bytes": "int (default: 262144, compress large event payloads)",
        "maintenance_interval_sec": "int (default: 3600)",
    },
    "environment": {
        "requirements": {
            "requirement_id": {
                "id": "string (envreq:<kind>:<name>)",
                "kind": "string (executable/runtime/sdk/service/env_var/credential/path/project_file/container)",
                "name": "string",
                "command": "string (optional, executable launch command)",
                "enabled": "bool (optional, default: true)",
                "placement": "string (optional, one of server, peer, both)",
                "tags": ["component-tag", "..."],
                "requirements": {"dependency": "version/range"},
                "check": "string (optional)",
                "install": "string (optional)",
                "configure": "string (optional)",
                "version": "string (optional)",
                "runtime": "string (optional)",
                "language": "string (optional)",
                "path": "string (optional)",
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
        "tools": ["read_file", "list_file", "glob", "grep", "lsp"],
        "prompt_append": (
            "Focus on analysis, architecture, and step-by-step plans. Avoid file mutations "
            "unless explicitly requested."
        ),
    },
    "debugger": {
        "description": "Debugging mode focused on diagnosis and verification.",
        "tools": ["read_file", "list_file", "glob", "grep", "lsp", "shell"],
        "prompt_append": (
            "Focus on root-cause analysis, minimal repro steps, and targeted fixes with "
            "clear verification."
        ),
    },
    "taskflow": {
        "description": "Background long-task planning and dispatch mode.",
        "tools": ["read_file", "list_file", "glob", "grep", "lsp"],
        "prompt_append": (
            "Guide the user from a fuzzy goal into decisions, acceptance criteria, "
            "issue drafts, task drafts, dispatch, and completion review."
        ),
    },
}

DEFAULT_ACTIVE_MODE = "coder"


# Default values for configuration
DEFAULTS = {
    "temperature": 0.0,
    "approval_default_mode": "require_approval",
    "approval_rules": [
        {"tool_name": "read_file", "action": "allow"},
        {"tool_name": "glob", "action": "allow"},
        {"tool_name": "grep", "action": "allow"},
        {"tool_name": "list_file", "action": "allow"},
        {"tool_name": "lsp", "action": "allow"},
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
    "snip_keep_recent_tools": 2,
    "snip_threshold_chars": 1500,
    "snip_min_lines": 6,
    "summarize_keep_recent_turns": 5,
    "token_fudge_factor": 1.1,
}

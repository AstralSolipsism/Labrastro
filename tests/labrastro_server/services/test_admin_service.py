from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from labrastro_server.services.admin.service import (
    AdminConfigResult,
    RemoteAdminConfigManager,
    lifecycle_hook_recent_results_from_agent_runs,
    parse_mcp_servers_config,
)
from reuleauxcoder.domain.hooks.lifecycle import lifecycle_registry_from_config
from reuleauxcoder.services.config.loader import ConfigLoader


class MemoryAdminManager(RemoteAdminConfigManager):
    def __init__(self, config_path: Path, **kwargs) -> None:
        super().__init__(config_path=config_path, **kwargs)
        self.data: dict[str, object] = {}

    def _load_data(self) -> dict:
        return deepcopy(self.data)

    def _commit_config(self, data: dict, previous_data: dict):
        del previous_data
        self.data = deepcopy(data)
        return None


class FailingCommitAdminManager(MemoryAdminManager):
    def _commit_config(self, data: dict, previous_data: dict):
        del data, previous_data
        return AdminConfigResult(
            False,
            {"error": "config_reload_failed", "message": "reload failed"},
            500,
        )


def test_record_skill_installs_pasted_skill_content_to_standard_path(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    skill_content = (
        "---\n"
        "name: code-review\n"
        "description: Review code changes before merging.\n"
        "---\n\n"
        "Use the repository review checklist.\n"
    )

    result = manager.record_skill(
        {
            "skill_content": skill_content,
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "enabled": True,
        }
    )

    installed_path = tmp_path / "skills" / "user" / "code-review" / "SKILL.md"
    assert result.ok is True
    assert installed_path.read_text(encoding="utf-8") == skill_content
    skill = manager.data["skills"]["items"]["code-review"]
    assert skill["display_name"] == "Code review"
    assert skill["summary"] == "Review repository changes before merging."
    assert skill["description"] == "Review code changes before merging."
    assert skill["path_hint"] == str(installed_path)
    assert "skill_content" not in skill
    assert result.payload["skill"]["display_name"] == "Code review"
    assert result.payload["skill"]["summary"] == "Review repository changes before merging."


def test_record_skill_does_not_auto_grant_agent_capability_refs(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.data["agents"] = {
        "coder": {
            "id": "coder",
            "capability_refs": [],
        }
    }
    skill_content = (
        "---\n"
        "name: code-review\n"
        "description: Review code changes before merging.\n"
        "---\n\n"
        "Use the repository review checklist.\n"
    )

    result = manager.record_skill(
        {
            "skill_content": skill_content,
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "enabled": True,
        }
    )

    assert result.ok is True
    assert manager.data["agents"]["coder"]["capability_refs"] == []


def test_record_skill_preserves_lifecycle_hooks(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    hooks = [
        {
            "event": "UserPromptSubmit",
            "handler_type": "prompt",
            "handler_ref": "skills/code-review/SKILL.md",
            "display_name": "Code review prompt context",
            "summary": "Adds code review context.",
            "permissions": [],
            "trust": "trusted",
        }
    ]

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
            "hooks": hooks,
        }
    )

    assert result.ok is True
    expected_hooks = [dict(hooks[0], placement="server", trust="pending_review")]
    assert manager.data["skills"]["items"]["code-review"]["hooks"] == expected_hooks
    assert result.payload["skill"]["hooks"] == expected_hooks


def test_record_skill_keeps_lifecycle_placement_separate_from_runtime_footprint(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "runtime_footprint": {
                "runs_on": "local_peer",
                "install_required_on": ["local_peer"],
                "config_required_on": ["local_peer"],
            },
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "handler_type": "prompt",
                    "display_name": "Code review prompt context",
                    "summary": "Adds code review context.",
                    "permissions": [],
                }
            ],
        }
    )

    skill = manager.data["skills"]["items"]["code-review"]
    assert result.ok is True
    assert skill["runtime_footprint"]["runs_on"] == "local_peer"
    assert skill["hooks"][0]["placement"] == "server"
    assert result.payload["skill"]["hooks"][0]["placement"] == "server"


def test_record_skill_rejects_invalid_lifecycle_hook_before_config_write(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "handler_type": "prompt",
                    "display_name": "Code review prompt context",
                    "summary": "Adds code review context.",
                    "permissions": [],
                    "matcher": {"tool": {"name": "shell"}},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "matcher" in result.payload["message"]
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_record_skill_rejects_internal_lifecycle_handler_before_config_write(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "handler_type": "internal",
                    "handler_ref": "ProjectContextStartupNotifier",
                    "display_name": "Unsafe internal hook",
                    "summary": "Attempts to call a Python hook from public skill config.",
                    "permissions": [],
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "internal handlers" in result.payload["message"]
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_record_skill_rejects_legacy_lifecycle_tool_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    legacy_field = "tool_" + "name"

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
            "hooks": [
                {
                    "event": "PreToolUse",
                    "handler_type": "prompt",
                    "display_name": "Code review tool guard",
                    "summary": "Adds code review context for matching tools.",
                    "permissions": [],
                    "matcher": {legacy_field: "shell"},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert legacy_field in result.payload["message"]
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_record_skill_accepts_lifecycle_tool_names_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
            "hooks": [
                {
                    "event": "PreToolUse",
                    "handler_type": "prompt",
                    "display_name": "Code review tool guard",
                    "summary": "Adds code review context for matching tools.",
                    "permissions": [],
                    "matcher": {"tool_names": "shell"},
                }
            ],
        }
    )

    assert result.ok is True
    hooks = manager.data["skills"]["items"]["code-review"]["hooks"]
    assert hooks[0]["matcher"] == {"tool_names": "shell"}


def test_record_skill_rejects_invalid_pasted_skill_content(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_skill({"skill_content": "missing frontmatter"})

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_skill_content"
    assert "frontmatter" in result.payload["message"]
    assert not (tmp_path / "skills").exists()


def test_record_skill_does_not_commit_config_when_skill_file_install_fails(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    blocked_parent = tmp_path / "blocked-parent"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    manager._standalone_skill_install_path = lambda _skill_name: blocked_parent / "SKILL.md"  # type: ignore[method-assign]

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
        }
    )

    assert result.ok is False
    assert result.status == 500
    assert result.payload["error"] == "skill_content_install_failed"
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_record_skill_removes_new_skill_file_when_config_commit_fails(tmp_path: Path) -> None:
    manager = FailingCommitAdminManager(tmp_path / "config.yaml")
    skill_content = (
        "---\n"
        "name: code-review\n"
        "description: Review code changes before merging.\n"
        "---\n\n"
        "Use the repository review checklist.\n"
    )
    installed_path = tmp_path / "skills" / "user" / "code-review" / "SKILL.md"

    result = manager.record_skill({"skill_content": skill_content})

    assert result.ok is False
    assert result.payload["error"] == "config_reload_failed"
    assert not installed_path.exists()
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_record_skill_restores_existing_skill_file_when_config_commit_fails(tmp_path: Path) -> None:
    manager = FailingCommitAdminManager(tmp_path / "config.yaml")
    installed_path = tmp_path / "skills" / "user" / "code-review" / "SKILL.md"
    installed_path.parent.mkdir(parents=True)
    previous_content = (
        "---\n"
        "name: code-review\n"
        "description: Previous description.\n"
        "---\n\n"
        "Previous body.\n"
    )
    installed_path.write_text(previous_content, encoding="utf-8")

    result = manager.record_skill(
        {
            "skill_content": (
                "---\n"
                "name: code-review\n"
                "description: Review code changes before merging.\n"
                "---\n\n"
                "Use the repository review checklist.\n"
            ),
        }
    )

    assert result.ok is False
    assert result.payload["error"] == "config_reload_failed"
    assert installed_path.read_text(encoding="utf-8") == previous_content
    assert "code-review" not in manager.data.get("skills", {}).get("items", {})


def test_parse_mcp_servers_config_reads_standard_json() -> None:
    raw = """
    {
      "mcpServers": {
        "edgeone-pages-mcp-server": {
          "command": "npx",
          "args": ["edgeone-pages-mcp"],
          "env": {"EDGEONE_TOKEN": "${EDGEONE_TOKEN}"}
        }
      }
    }
    """

    drafts = parse_mcp_servers_config(raw)

    assert drafts == [
        {
            "name": "edgeone-pages-mcp-server",
            "command": "npx",
            "args": ["edgeone-pages-mcp"],
            "env": {"EDGEONE_TOKEN": "${EDGEONE_TOKEN}"},
        }
    ]


def test_record_mcp_server_installs_from_standard_mcp_json(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    raw = {
        "mcpServers": {
            "edgeone-pages-mcp-server": {
                "command": "npx",
                "args": ["edgeone-pages-mcp"],
            }
        }
    }

    result = manager.record_mcp_server(
        {
            "name": "",
            "mcp_config": raw,
            "display_name": "EdgeOne Pages",
            "summary": "Deploy pages through EdgeOne.",
            "runtime_footprint": {
                "runs_on": "local_peer",
                "install_required_on": ["local_peer"],
                "config_required_on": ["local_peer"],
            },
        }
    )

    assert result.ok is True
    server = manager.data["mcp"]["servers"]["edgeone-pages-mcp-server"]
    assert server["command"] == "npx"
    assert server["args"] == ["edgeone-pages-mcp"]
    assert server["display_name"] == "EdgeOne Pages"
    assert server["summary"] == "Deploy pages through EdgeOne."
    assert server["placement"] == "peer"
    assert server["runtime_footprint"] == {
        "runs_on": "local_peer",
        "install_required_on": ["local_peer"],
        "config_required_on": ["local_peer"],
        "user_message": "需要在本机安装/配置",
    }
    assert "mcp_config" not in server
    assert "mcpServers" not in server


def test_record_mcp_server_preserves_lifecycle_hooks(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    hooks = [
        {
            "event": "PostToolUse",
            "handler_type": "mcp_tool",
            "handler_ref": "github.audit",
            "display_name": "GitHub audit",
            "summary": "Records GitHub MCP tool results.",
            "permissions": ["audit.write"],
            "trust": "trusted",
        }
    ]

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "hooks": hooks,
        }
    )

    assert result.ok is True
    expected_hooks = [dict(hooks[0], placement="server", trust="pending_review")]
    assert manager.data["mcp"]["servers"]["github"]["hooks"] == expected_hooks
    assert result.payload["mcp_server"]["hooks"] == expected_hooks


def test_record_mcp_server_keeps_lifecycle_placement_separate_from_runtime_footprint(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "runtime_footprint": {
                "runs_on": "local_peer",
                "install_required_on": ["local_peer"],
                "config_required_on": ["local_peer"],
            },
            "hooks": [
                {
                    "event": "PostToolUse",
                    "handler_type": "mcp_tool",
                    "handler_ref": "github.audit",
                    "display_name": "GitHub audit",
                    "summary": "Records GitHub MCP tool results.",
                    "permissions": [],
                }
            ],
        }
    )

    server = manager.data["mcp"]["servers"]["github"]
    assert result.ok is True
    assert server["placement"] == "peer"
    assert server["runtime_footprint"]["runs_on"] == "local_peer"
    assert server["hooks"][0]["placement"] == "server"
    assert result.payload["mcp_server"]["hooks"][0]["placement"] == "server"


def test_record_mcp_server_rejects_invalid_lifecycle_hook_before_config_write(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "hooks": [
                {
                    "event": "PostToolUse",
                    "placement": "local_peer",
                    "handler_type": "mcp_tool",
                    "display_name": "GitHub audit",
                    "summary": "Records GitHub MCP tool results.",
                    "permissions": [],
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "placement" in result.payload["message"]
    assert "github" not in manager.data.get("mcp", {}).get("servers", {})


def test_record_mcp_server_rejects_internal_lifecycle_handler_before_config_write(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "hooks": [
                {
                    "event": "PostToolUse",
                    "handler_type": "internal",
                    "handler_ref": "memory_context",
                    "display_name": "Unsafe internal hook",
                    "summary": "Attempts to call a Python hook from public MCP config.",
                    "permissions": [],
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "internal handlers" in result.payload["message"]
    assert "github" not in manager.data.get("mcp", {}).get("servers", {})


def test_record_mcp_server_rejects_legacy_lifecycle_tool_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    legacy_field = "tool_" + "source"

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "hooks": [
                {
                    "event": "PostToolUse",
                    "handler_type": "mcp_tool",
                    "display_name": "GitHub audit",
                    "summary": "Records GitHub MCP tool results.",
                    "permissions": [],
                    "matcher": {legacy_field: "mcp"},
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert legacy_field in result.payload["message"]
    assert "github" not in manager.data.get("mcp", {}).get("servers", {})


def test_record_mcp_server_accepts_lifecycle_tool_names_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_mcp_server(
        {
            "name": "github",
            "command": "github-mcp-server",
            "hooks": [
                {
                    "event": "PostToolUse",
                    "handler_type": "mcp_tool",
                    "display_name": "GitHub audit",
                    "summary": "Records GitHub MCP tool results.",
                    "permissions": [],
                    "matcher": {"tool_names": ["search", "list_issues"]},
                }
            ],
        }
    )

    assert result.ok is True
    hooks = manager.data["mcp"]["servers"]["github"]["hooks"]
    assert hooks[0]["matcher"] == {"tool_names": ["search", "list_issues"]}


def test_parse_mcp_servers_config_reads_lifecycle_hooks_from_extended_json() -> None:
    raw = {
        "mcpServers": {
            "github": {
                "command": "github-mcp-server",
                "hooks": [
                    {
                        "event": "PostToolUse",
                        "handler_type": "mcp_tool",
                        "handler_ref": "github.audit",
                        "display_name": "GitHub audit",
                        "summary": "Records GitHub MCP tool results.",
                        "permissions": ["audit.write"],
                    }
                ],
            }
        }
    }

    drafts = parse_mcp_servers_config(raw)

    assert drafts[0]["hooks"] == raw["mcpServers"]["github"]["hooks"]


def test_record_mcp_server_rejects_invalid_mcp_json(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.record_mcp_server({"mcp_config": "{}"})

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_mcp_config"


def test_skill_and_mcp_dashboard_return_user_display_fields(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "path_hint": "/skills/code-review/SKILL.md",
        }
    )
    manager.record_mcp_server(
        {
            "name": "github",
            "display_name": "GitHub tools",
            "summary": "Read issues and pull requests.",
            "command": "github-mcp-server",
        }
    )

    skills = {item["name"]: item for item in manager.list_skills()["skills"]}
    mcps = {item["name"]: item for item in manager.list_mcp_servers()["mcp_servers"]}
    skill_rows = {item["id"]: item for item in manager.skills_dashboard()["items"]}
    mcp_rows = {item["id"]: item for item in manager.mcp_servers_dashboard()["items"]}

    assert skills["code-review"]["display_name"] == "Code review"
    assert skills["code-review"]["summary"] == "Review repository changes before merging."
    assert mcps["github"]["display_name"] == "GitHub tools"
    assert mcps["github"]["summary"] == "Read issues and pull requests."
    assert skill_rows["skill:code-review"]["display_name"] == "Code review"
    assert skill_rows["skill:code-review"]["summary"] == "Review repository changes before merging."
    assert mcp_rows["mcp:github"]["display_name"] == "GitHub tools"
    assert mcp_rows["mcp:github"]["summary"] == "Read issues and pull requests."
    assert skill_rows["skill:code-review"]["runtime_footprint"]["runs_on"] == "agent_only"
    assert mcp_rows["mcp:github"]["runtime_footprint"]["runs_on"] == "server"


def test_mcp_dashboard_keeps_lifecycle_hooks_separate_from_mcp_memory_provider_status(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_mcp_server(
        {
            "name": "github",
            "display_name": "GitHub tools",
            "summary": "Read issues and pull requests.",
            "command": "github-mcp-server",
            "hooks": [
                {
                    "event": "PostToolUse",
                    "handler_type": "mcp_tool",
                    "handler_ref": "github.audit",
                    "display_name": "GitHub audit",
                    "summary": "Records GitHub MCP tool results.",
                    "permissions": ["audit.write"],
                }
            ],
        }
    )
    manager.data["memory"] = {
        "enabled": True,
        "default_provider": "github_memory",
        "providers": {
            "github_memory": {
                "adapter": "mcp_memory",
                "mcp_server": "github",
                "hooks": [
                    {
                        "event": "UserPromptSubmit",
                        "handler_type": "mcp_tool",
                        "handler_ref": "github.memory_audit",
                        "display_name": "Memory audit",
                        "summary": "This must stay out of lifecycle hook views.",
                        "permissions": ["memory.read"],
                    }
                ],
            }
        },
        "sources": {
            "github_source": {
                "adapter": "mcp_memory",
                "target_provider": "github_memory",
                "mcp_server": "github",
            }
        },
        "tools": {
            "enabled": True,
            "provider": "github_memory",
            "recall": True,
        },
    }

    mcp_row = manager.mcp_servers_dashboard()["items"][0]
    settings = manager.read_server_settings()["settings"]
    memory_status = settings["memory_status"]

    assert [hook["id"] for hook in mcp_row["hook_views"]] == [
        "hook:mcp_server:github:PostToolUse:0"
    ]
    assert mcp_row["hook_views"][0]["source"] == "mcp_server"
    assert "github_memory" not in str(mcp_row["hook_views"])
    assert "memory_provider" not in str(mcp_row["hook_views"])

    assert memory_status["providers"] == [
        {
            "id": "github_memory",
            "is_default": True,
            "provider": "github_memory",
            "adapter": "mcp_memory",
            "configured": True,
            "enabled": True,
            "adapter_registered": False,
            "available": False,
            "status": "adapter_missing",
        }
    ]
    assert memory_status["sources"][0]["id"] == "github_source"
    assert memory_status["sources"][0]["target_provider"] == "github_memory"
    assert memory_status["tools"]["provider"] == "github_memory"
    assert "hook:" not in str(memory_status)
    assert "Memory audit" not in str(memory_status)


def test_mcp_dashboard_renders_lifecycle_hook_trust_risk_and_recent_result(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(
        tmp_path / "config.yaml",
        lifecycle_hook_results_provider=lambda source, owner_id: {
            "hook:mcp_server:github:PreToolUse:0": {
                "status": "denied",
                "summary": f"{source}:{owner_id} denied GitHub mutation",
                "agent_run_id": "agent-run-mcp-1",
            }
        },
    )
    manager.record_mcp_server(
        {
            "name": "github",
            "display_name": "GitHub tools",
            "summary": "Read issues and pull requests.",
            "command": "github-mcp-server",
            "args": ["stdio", "--verbose"],
            "env": {"GITHUB_TOKEN": "${env:GITHUB_TOKEN}"},
            "hooks": [
                {
                    "event": "PreToolUse",
                    "placement": "server",
                    "handler_type": "mcp_tool",
                    "handler_ref": "github.guard",
                    "display_name": "GitHub MCP guard",
                    "summary": "Guards GitHub MCP tool calls.",
                    "permissions": ["mcp.invoke"],
                    "credentials": ["GITHUB_TOKEN"],
                    "risk_level": "high",
                    "trust": "trusted",
                }
            ],
        }
    )
    trust_result = manager.update_lifecycle_hook_trust(
        {"hook_id": "hook:mcp_server:github:PreToolUse:0", "trust": "trusted"}
    )

    row = manager.mcp_servers_dashboard()["items"][0]
    hook = row["hook_views"][0]

    assert trust_result.ok is True
    assert row["command"] == "github-mcp-server"
    assert row["args"] == ["stdio", "--verbose"]
    assert row["env"] == {"GITHUB_TOKEN": "${env:GITHUB_TOKEN}"}
    assert hook["id"] == "hook:mcp_server:github:PreToolUse:0"
    assert hook["source"] == "mcp_server"
    assert hook["trust"] == "trusted"
    assert hook["credentials"] == ["GITHUB_TOKEN"]
    assert hook["risk_level"] == "high"
    assert hook["recent_result"] == {
        "status": "denied",
        "summary": "mcp_server:github denied GitHub mutation",
        "agent_run_id": "agent-run-mcp-1",
    }


def test_skill_dashboard_returns_lifecycle_hook_summary_with_technical_details(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "handler_ref": "skills/code-review/SKILL.md",
                    "matcher": {"trigger_source": "chat"},
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                    "risk_level": "low",
                    "trust": "trusted",
                }
            ],
        }
    )

    row = manager.skills_dashboard()["items"][0]

    assert row["hook_views"] == [
        {
            "id": "hook:skill:code-review:UserPromptSubmit:0",
            "event": "UserPromptSubmit",
            "source": "skill",
            "owner_id": "code-review",
            "owner_enabled": True,
            "owner_status": "installed",
            "placement": "server",
            "handler_type": "prompt",
            "display_name": "Review prompt guard",
            "summary": "Checks review prompts before the model sees them.",
            "trust": "pending_review",
            "enabled": False,
            "executable": False,
            "can_manage": True,
            "management_actions": [
                {
                    "trust": "trusted",
                    "label": "Trust hook",
                    "endpoint": "admin.lifecycle_hooks.trust",
                },
                {
                    "trust": "disabled",
                    "label": "Disable hook",
                    "endpoint": "admin.lifecycle_hooks.trust",
                },
                {
                    "trust": "blocked",
                    "label": "Block hook",
                    "endpoint": "admin.lifecycle_hooks.trust",
                },
            ],
            "unavailable_reason": "trust:pending_review",
            "placement_runtime": {
                "server": {
                    "executable": False,
                    "unavailable_reason": "trust:pending_review",
                },
            },
            "runtime_context_required": ["prompt_model"],
            "permissions": ["prompt.read"],
            "credentials": [],
            "risk_level": "low",
            "recent_result": {
                "status": "unrecorded",
                "summary": "No lifecycle executions recorded.",
            },
            "technical": {
                "handler_ref": "skills/code-review/SKILL.md",
                "matcher": {"trigger_source": "chat"},
            },
        }
    ]
    assert "handler_ref" not in row["hook_views"][0]


def test_skill_dashboard_returns_lifecycle_hook_credentials_and_recent_result(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "PreToolUse",
                    "placement": "server",
                    "handler_type": "command",
                    "handler_ref": "python scripts/check.py",
                    "display_name": "Review shell guard",
                    "summary": "Checks shell commands.",
                    "permissions": ["shell.read"],
                    "credentials": ["GITHUB_TOKEN"],
                    "risk_level": "high",
                    "trust": "trusted",
                }
            ],
            "lifecycle_hook_results": {
                "hook:skill:code-review:PreToolUse:0": {
                    "status": "denied",
                    "summary": "Denied shell command",
                    "session_run_id": "session-run-1",
                }
            },
        }
    )

    hook = manager.skills_dashboard()["items"][0]["hook_views"][0]

    assert hook["credentials"] == ["GITHUB_TOKEN"]
    assert hook["risk_level"] == "high"
    assert hook["recent_result"] == {
        "status": "denied",
        "summary": "Denied shell command",
        "session_run_id": "session-run-1",
    }


def test_skill_dashboard_uses_runtime_lifecycle_hook_recent_result_provider(tmp_path: Path) -> None:
    manager = MemoryAdminManager(
        tmp_path / "config.yaml",
        lifecycle_hook_results_provider=lambda source, owner_id: {
            "hook:skill:code-review:PreToolUse:0": {
                "status": "success",
                "summary": f"{source}:{owner_id} allowed shell command",
                "agent_run_id": "agent-run-1",
            }
        },
    )
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "PreToolUse",
                    "placement": "server",
                    "handler_type": "command",
                    "handler_ref": "python scripts/check.py",
                    "display_name": "Review shell guard",
                    "summary": "Checks shell commands.",
                    "permissions": ["shell.read"],
                    "trust": "trusted",
                }
            ],
        }
    )

    hook = manager.skills_dashboard()["items"][0]["hook_views"][0]

    assert hook["recent_result"] == {
        "status": "success",
        "summary": "skill:code-review allowed shell command",
        "agent_run_id": "agent-run-1",
    }


def test_lifecycle_hook_recent_results_from_agent_runs_filters_owner_and_keeps_latest() -> None:
    class _Event:
        def __init__(self, seq: int, payload: dict) -> None:
            self.seq = seq
            self.type = "lifecycle_hook"
            self.payload = payload

    class _Runtime:
        def list_agent_runs(self, *, limit: int = 50):
            del limit
            return [{"id": "run-new"}, {"id": "run-old"}]

        def list_events(self, task_id: str, *, after_seq: int = 0, limit: int = 500):
            del after_seq, limit
            if task_id == "run-old":
                return [
                    _Event(1, {
                        "data": {
                            "hook_id": "hook:skill:code-review:PreToolUse:0",
                            "source": "skill",
                            "event_name": "PreToolUse",
                            "display_name": "Review shell guard",
                            "decision": "allow",
                            "continue_flow": True,
                        }
                    })
                ]
            return [
                _Event(1, {
                    "data": {
                        "hook_id": "hook:mcp_server:github:PreToolUse:0",
                        "source": "mcp_server",
                        "event_name": "PreToolUse",
                    }
                }),
                _Event(2, {
                    "data": {
                        "hook_id": "hook:skill:code-review:PreToolUse:0",
                        "source": "skill",
                        "event_name": "PreToolUse",
                        "display_name": "Review shell guard",
                        "decision": "deny",
                        "continue_flow": False,
                        "message": "Denied shell command",
                    }
                }),
            ]

    results = lifecycle_hook_recent_results_from_agent_runs(
        _Runtime(),
        "skill",
        "code-review",
    )

    assert results == {
        "hook:skill:code-review:PreToolUse:0": {
            "status": "denied",
            "summary": "Denied shell command",
            "decision": "deny",
            "event": "PreToolUse",
            "agent_run_id": "run-new",
        }
    }


def test_behavior_catalog_exposes_lifecycle_event_wiring_status(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    catalog = manager.behavior_catalog()

    events = {item["event"]: item for item in catalog["lifecycle_hook_events"]}
    assert events["PermissionDenied"]["external_config_supported"] is True
    assert events["PermissionDenied"]["runtime_status"] == "external_config_supported"
    assert events["SessionStart"]["external_config_supported"] is False
    assert events["SessionStart"]["runtime_status"] == "external_event_unwired"
    assert events["UserPromptExpansion"]["external_config_supported"] is True
    assert events["UserPromptExpansion"]["runtime_status"] == "external_config_supported"
    assert events["TaskCreated"]["external_config_supported"] is True
    assert events["SubagentStart"]["external_config_supported"] is True
    assert events["PreCompact"]["external_config_supported"] is True
    assert events["PostCompact"]["external_config_supported"] is True
    assert events["ConfigChange"]["external_config_supported"] is True
    assert events["CwdChanged"]["external_config_supported"] is True
    assert events["FileChanged"]["external_config_supported"] is True
    assert events["Notification"]["external_config_supported"] is True
    assert events["WorktreeCreate"]["external_config_supported"] is False
    assert events["WorktreeCreate"]["runtime_status"] == "control_plane_audit_only"
    assert events["WorktreeCreate"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )
    assert events["WorktreeRemove"]["external_config_supported"] is False
    assert events["WorktreeRemove"]["runtime_status"] == "control_plane_audit_only"
    assert events["WorktreeRemove"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )
    assert events["TaskCompleted"]["external_config_supported"] is False
    assert events["TaskCompleted"]["runtime_status"] == "control_plane_audit_only"
    assert events["TaskCompleted"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )
    assert events["SubagentStop"]["external_config_supported"] is False
    assert events["SubagentStop"]["runtime_status"] == "control_plane_audit_only"
    assert events["SubagentStop"]["runtime_reason"] == (
        "emitted_by_agent_run_control_plane"
    )


def test_admin_commit_emits_config_change_lifecycle_after_successful_reload(
    tmp_path: Path,
) -> None:
    reloads: list[str] = []
    changes: list[dict] = []
    config_path = tmp_path / "config.yaml"
    manager = RemoteAdminConfigManager(
        config_path=config_path,
        reload_handler=lambda: reloads.append("reload"),
        config_change_handler=lambda event: changes.append(event),
    )

    result = manager._commit_config(
        {"models": {"active_main": "main"}},
        {"models": {"active_main": "old"}},
    )

    assert result is None
    assert reloads == ["reload"]
    assert len(changes) == 1
    event = changes[0]
    assert event["event_name"] == "ConfigChange"
    assert event["status"] == "committed"
    assert event["config_path"] == str(config_path)
    assert event["previous_etag"] != event["current_etag"]
    assert event["path_space"] == "server_config"


def test_lifecycle_hook_trust_update_changes_config_and_dashboard_execution_state(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                    "trust": "trusted",
                }
            ],
        }
    )

    hook_id = "hook:skill:code-review:UserPromptSubmit:0"
    before = manager.skills_dashboard()["items"][0]["hook_views"][0]
    assert before["trust"] == "pending_review"
    assert before["executable"] is False

    result = manager.update_lifecycle_hook_trust(
        {"hook_id": hook_id, "trust": "trusted"}
    )

    assert result.ok is True
    assert manager.data["skills"]["items"]["code-review"]["hooks"][0]["trust"] == "trusted"
    after = manager.skills_dashboard()["items"][0]["hook_views"][0]
    assert after["trust"] == "trusted"
    assert after["executable"] is False
    assert after["unavailable_reason"] == "handler_ref_unavailable:prompt"


def test_lifecycle_hook_trust_update_persists_all_states_and_runtime_filter(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "handler_ref": "Review prompts before model input.",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                }
            ],
        }
    )
    hook_id = "hook:skill:code-review:UserPromptSubmit:0"

    for trust in ("pending_review", "trusted", "disabled", "blocked"):
        result = manager.update_lifecycle_hook_trust(
            {"hook_id": hook_id, "trust": trust}
        )

        assert result.ok is True
        stored = manager.data["skills"]["items"]["code-review"]["hooks"][0]
        hook = manager.skills_dashboard()["items"][0]["hook_views"][0]
        assert stored["trust"] == trust
        assert hook["trust"] == trust
        if trust == "trusted":
            assert hook["executable"] is True
            assert hook["unavailable_reason"] == ""
        else:
            assert hook["executable"] is False
            assert hook["unavailable_reason"] == f"trust:{trust}"


def test_lifecycle_hook_dashboard_reports_both_placement_runtime_independently(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "summary": "Review repository changes before merging.",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "both",
                    "handler_type": "prompt",
                    "handler_ref": "Review prompts before model input.",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                }
            ],
        }
    )
    hook_id = "hook:skill:code-review:UserPromptSubmit:0"
    result = manager.update_lifecycle_hook_trust(
        {"hook_id": hook_id, "trust": "trusted"}
    )

    assert result.ok is True
    hook = manager.skills_dashboard()["items"][0]["hook_views"][0]
    assert hook["placement"] == "both"
    assert hook["executable"] is True
    assert hook["unavailable_reason"] == ""
    assert hook["placement_runtime"]["server"] == {
        "executable": True,
        "unavailable_reason": "",
    }
    assert hook["placement_runtime"]["peer"] == {
        "executable": False,
        "unavailable_reason": "peer_runtime_unavailable",
    }


def test_lifecycle_hook_dashboard_reports_both_placement_for_all_public_resource_views(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    hook = {
        "event": "UserPromptSubmit",
        "placement": "both",
        "handler_type": "prompt",
        "handler_ref": "Review prompts before model input.",
        "display_name": "Review prompt guard",
        "summary": "Checks review prompts before the model sees them.",
        "permissions": ["prompt.read"],
        "trust": "trusted",
    }
    manager.record_mcp_server(
        {
            "name": "github",
            "display_name": "GitHub tools",
            "summary": "Read issues and pull requests.",
            "command": "github-mcp-server",
            "hooks": [hook],
        }
    )
    manager.update_lifecycle_hook_trust(
        {
            "hook_id": "hook:mcp_server:github:UserPromptSubmit:0",
            "trust": "trusted",
        }
    )
    manager.data["capability_packages"] = {
        "review": {
            "enabled": True,
            "status": "installed",
            "hooks": [dict(hook)],
        }
    }
    manager.data["capability_components"] = {
        "mcp_server:github": {
            "kind": "mcp_server",
            "name": "github",
            "enabled": True,
            "status": "installed",
            "hooks": [dict(hook)],
        }
    }

    mcp_hook = manager.mcp_servers_dashboard()["items"][0]["hook_views"][0]
    settings = manager.read_server_settings()["settings"]
    package_hook = settings["capability_packages"]["review"]["hook_views"][0]
    component_hook = settings["capability_components"]["mcp_server:github"][
        "hook_views"
    ][0]

    for hook_view in (mcp_hook, package_hook, component_hook):
        assert hook_view["placement"] == "both"
        assert hook_view["executable"] is True
        assert hook_view["unavailable_reason"] == ""
        assert hook_view["placement_runtime"]["server"] == {
            "executable": True,
            "unavailable_reason": "",
        }
        assert hook_view["placement_runtime"]["peer"] == {
            "executable": False,
            "unavailable_reason": "peer_runtime_unavailable",
        }


def test_lifecycle_hook_dashboard_exposes_management_actions(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "handler_ref": "skills/code-review/SKILL.md",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                }
            ],
        }
    )

    hook = manager.skills_dashboard()["items"][0]["hook_views"][0]

    assert hook["can_manage"] is True
    assert {item["trust"] for item in hook["management_actions"]} >= {
        "trusted",
        "disabled",
        "blocked",
    }
    assert all(item["endpoint"] == "admin.lifecycle_hooks.trust" for item in hook["management_actions"])


def test_lifecycle_hook_dashboard_marks_trusted_prompt_with_handler_ref_executable(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_skill(
        {
            "name": "code-review",
            "hooks": [
                {
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "handler_ref": "skills/code-review/SKILL.md",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": ["prompt.read"],
                }
            ],
        }
    )
    hook_id = "hook:skill:code-review:UserPromptSubmit:0"

    result = manager.update_lifecycle_hook_trust(
        {"hook_id": hook_id, "trust": "trusted"}
    )

    assert result.ok is True
    hook = manager.skills_dashboard()["items"][0]["hook_views"][0]
    assert hook["trust"] == "trusted"
    assert hook["executable"] is True
    assert hook["unavailable_reason"] == ""
    assert hook["runtime_context_required"] == ["prompt_model"]
    assert hook["placement_runtime"]["server"]["executable"] is True


def test_record_skill_rejects_raw_lifecycle_hook_id(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    result = manager.record_skill(
        {
            "name": "code-review",
            "hooks": [
                {
                    "id": "raw-review-id",
                    "event": "UserPromptSubmit",
                    "placement": "server",
                    "handler_type": "prompt",
                    "display_name": "Review prompt guard",
                    "summary": "Checks review prompts before the model sees them.",
                    "permissions": [],
                }
            ],
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "id" in result.payload["message"]
    assert "skills" not in manager.data


def test_server_settings_returns_canonical_package_and_component_hook_views(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "enabled": True,
                        "status": "installed",
                        "hooks": [
                            {
                                "event": "UserPromptSubmit",
                                "placement": "server",
                                "handler_type": "prompt",
                                "display_name": "Review package prompt",
                                "summary": "Adds review prompt context.",
                                "permissions": [],
                                "trust": "trusted",
                            }
                        ],
                    }
                },
                "capability_components": {
                    "skill:review": {
                        "kind": "skill",
                        "name": "review",
                        "enabled": False,
                        "hooks": [
                            {
                                "event": "PreToolUse",
                                "placement": "server",
                                "handler_type": "agent",
                                "display_name": "Review tool policy",
                                "summary": "Checks review tool use.",
                                "permissions": [],
                                "trust": "trusted",
                            }
                        ],
                    }
                },
            }
        }
    )
    assert result.ok is True

    settings = manager.read_server_settings()["settings"]
    package_config_hook = settings["capability_packages"]["review"]["hooks"][0]
    component_config_hook = settings["capability_components"]["skill:review"]["hooks"][0]
    package_hook = settings["capability_packages"]["review"]["hook_views"][0]
    component_hook = settings["capability_components"]["skill:review"]["hook_views"][0]

    assert "id" not in package_config_hook
    assert "owner_id" not in package_config_hook
    assert package_config_hook["event"] == "UserPromptSubmit"
    assert component_config_hook["event"] == "PreToolUse"
    assert package_hook["id"] == "hook:capability_package:review:UserPromptSubmit:0"
    assert package_hook["owner_id"] == "review"
    assert package_hook["trust"] == "trusted"
    assert package_hook["executable"] is False
    assert package_hook["unavailable_reason"] == "handler_ref_unavailable:prompt"
    assert component_hook["id"] == "hook:skill:skill:review:PreToolUse:0"
    assert component_hook["owner_id"] == "skill:review"
    assert component_hook["owner_enabled"] is False
    assert component_hook["executable"] is False
    assert component_hook["unavailable_reason"] == "owner_disabled"


def test_server_settings_package_and_component_hook_views_include_recent_results(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(
        tmp_path / "config.yaml",
        lifecycle_hook_results_provider=lambda source, owner_id: {
            f"hook:{source}:{owner_id}:PreToolUse:0": {
                "status": "denied",
                "summary": f"{source}:{owner_id} denied tool use",
                "agent_run_id": "agent-run-component",
            },
            f"hook:{source}:{owner_id}:UserPromptSubmit:0": {
                "status": "success",
                "summary": f"{source}:{owner_id} accepted prompt",
                "agent_run_id": "agent-run-package",
            },
        },
    )
    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "enabled": True,
                        "status": "installed",
                        "hooks": [
                            {
                                "event": "UserPromptSubmit",
                                "placement": "server",
                                "handler_type": "prompt",
                                "display_name": "Review package prompt",
                                "summary": "Adds review prompt context.",
                                "permissions": [],
                                "trust": "trusted",
                            }
                        ],
                    }
                },
                "capability_components": {
                    "skill:review": {
                        "kind": "skill",
                        "name": "review",
                        "hooks": [
                            {
                                "event": "PreToolUse",
                                "placement": "server",
                                "handler_type": "agent",
                                "display_name": "Review tool policy",
                                "summary": "Checks review tool use.",
                                "permissions": [],
                                "trust": "trusted",
                            }
                        ],
                    }
                },
            }
        }
    )

    assert result.ok is True
    settings = manager.read_server_settings()["settings"]
    package_hook = settings["capability_packages"]["review"]["hook_views"][0]
    component_hook = settings["capability_components"]["skill:review"]["hook_views"][0]

    assert package_hook["recent_result"] == {
        "status": "success",
        "summary": "capability_package:review accepted prompt",
        "agent_run_id": "agent-run-package",
    }
    assert component_hook["recent_result"] == {
        "status": "denied",
        "summary": "skill:skill:review denied tool use",
        "agent_run_id": "agent-run-component",
    }


def test_server_settings_rejects_hook_views_written_back_to_config(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.data = {
        "capability_packages": {
            "review": {
                "enabled": True,
                "status": "installed",
                "hooks": [
                    {
                        "event": "PostToolUse",
                        "placement": "server",
                        "handler_type": "prompt",
                        "handler_ref": "review.audit",
                        "matcher": {"tool_names": "read_file"},
                        "display_name": "Review audit",
                        "summary": "Audit tools.",
                        "permissions": [],
                        "trust": "trusted",
                    }
                ],
            }
        }
    }

    settings = manager.read_server_settings()["settings"]
    package = settings["capability_packages"]["review"]

    assert package["hooks"][0]["handler_ref"] == "review.audit"
    assert package["hooks"][0]["matcher"] == {"tool_names": "read_file"}
    assert package["hook_views"][0]["id"] == "hook:capability_package:review:PostToolUse:0"
    assert package["hook_views"][0]["technical"]["matcher"] == {"tool_names": "read_file"}

    result = manager.update_server_settings(
        {"capability_packages": settings["capability_packages"]}
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "hook_views" in result.payload["message"]

    clean_package = dict(package)
    clean_package.pop("hook_views")
    result = manager.update_server_settings(
        {"capability_packages": {"review": clean_package}}
    )

    assert result.ok is True
    stored_hook = manager.data["capability_packages"]["review"]["hooks"][0]
    assert stored_hook["handler_ref"] == "review.audit"
    assert stored_hook["matcher"] == {"tool_names": "read_file"}
    assert "technical" not in stored_hook
    config = ConfigLoader()._parse_config(manager.data)
    declarations = lifecycle_registry_from_config(config).query(
        event="PostToolUse",
        placement="server",
        trust="trusted",
    )
    assert len(declarations) == 1
    assert declarations[0].handler_ref == "review.audit"
    assert declarations[0].matcher == {"tool_names": "read_file"}


def test_server_settings_rejects_dashboard_hook_view_inside_config_hooks(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    dashboard_hook_view = {
        "id": "hook:capability_package:review:PostToolUse:0",
        "event": "PostToolUse",
        "source": "capability_package",
        "owner_id": "review",
        "owner_enabled": True,
        "owner_status": "installed",
        "placement": "server",
        "handler_type": "prompt",
        "display_name": "Review audit",
        "summary": "Audit tools.",
        "permissions": [],
        "trust": "trusted",
        "enabled": True,
        "executable": True,
        "can_manage": True,
        "unavailable_reason": "",
        "technical": {
            "handler_ref": "review.audit",
            "matcher": {"tool_names": "read_file"},
        },
    }

    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "hooks": [dashboard_hook_view],
                    }
                },
            }
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "id" in result.payload["message"]
    assert "review" not in manager.data.get("capability_packages", {})


def test_server_settings_rejects_invalid_lifecycle_hook_before_config_write(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "hooks": [
                        {
                            "event": "UserPromptSubmit",
                            "placement": "server",
                            "handler_type": "python_class",
                            "display_name": "Review package startup",
                                "summary": "Adds review startup context.",
                                "permissions": [],
                            }
                        ],
                    }
                },
            }
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert "handler_type" in result.payload["message"]
    assert "review" not in manager.data.get("capability_packages", {})


def test_server_settings_rejects_legacy_lifecycle_tool_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    legacy_field = "mcp_" + "server"

    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "hooks": [
                            {
                                "event": "PostToolUse",
                                "placement": "server",
                                "handler_type": "prompt",
                                "display_name": "Review package tool hook",
                                "summary": "Adds review context for matching tools.",
                                "permissions": [],
                                "matcher": {legacy_field: "github"},
                            }
                        ],
                    }
                },
            }
        }
    )

    assert result.ok is False
    assert result.status == 400
    assert result.payload["error"] == "invalid_lifecycle_hook"
    assert legacy_field in result.payload["message"]
    assert "review" not in manager.data.get("capability_packages", {})


def test_server_settings_accepts_lifecycle_tool_names_matcher_field(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "hooks": [
                            {
                                "event": "PostToolUse",
                                "placement": "server",
                                "handler_type": "prompt",
                                "display_name": "Review package tool hook",
                                "summary": "Adds review context for matching tools.",
                                "permissions": [],
                                "matcher": {"tool_names": ["read_file", "write_file"]},
                            }
                        ],
                    }
                },
            }
        }
    )

    assert result.ok is True
    settings = manager.read_server_settings()["settings"]
    hook = settings["capability_packages"]["review"]["hooks"][0]
    hook_view = settings["capability_packages"]["review"]["hook_views"][0]
    assert hook["matcher"] == {"tool_names": ["read_file", "write_file"]}
    assert hook_view["technical"]["matcher"] == {"tool_names": ["read_file", "write_file"]}


def test_server_settings_keeps_lifecycle_placement_separate_from_runtime_footprint(
    tmp_path: Path,
) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")

    result = manager.update_server_settings(
        {
            "settings": {
                "capability_packages": {
                    "review": {
                        "runtime_footprint": {
                            "runs_on": "local_peer",
                            "install_required_on": ["local_peer"],
                            "config_required_on": ["local_peer"],
                        },
                        "hooks": [
                            {
                                "event": "UserPromptSubmit",
                                "handler_type": "prompt",
                                "display_name": "Review package startup",
                                "summary": "Adds review startup context.",
                                "permissions": [],
                            }
                        ],
                    }
                },
                "capability_components": {
                    "skill:review": {
                        "kind": "skill",
                        "name": "review",
                        "runtime_footprint": {
                            "runs_on": "local_peer",
                            "install_required_on": ["local_peer"],
                            "config_required_on": ["local_peer"],
                        },
                        "hooks": [
                            {
                                "event": "PostToolUse",
                                "handler_type": "prompt",
                                "display_name": "Review component tool hook",
                                "summary": "Adds review context for matching tools.",
                                "permissions": [],
                            }
                        ],
                    }
                },
            }
        }
    )

    assert result.ok is True
    package = manager.data["capability_packages"]["review"]
    component = manager.data["capability_components"]["skill:review"]
    assert package["runtime_footprint"]["runs_on"] == "local_peer"
    assert component["runtime_footprint"]["runs_on"] == "local_peer"
    assert package["hooks"][0]["placement"] == "server"
    assert component["hooks"][0]["placement"] == "server"


def test_skill_dashboard_aggregates_runtime_from_environment_refs(tmp_path: Path) -> None:
    manager = MemoryAdminManager(tmp_path / "config.yaml")
    manager.record_environment_requirement(
        {
            "id": "envreq:executable:gh",
            "kind": "executable",
            "name": "gh",
            "command": "gh",
            "placement": "peer",
        }
    )
    manager.record_skill(
        {
            "name": "code-review",
            "display_name": "Code review",
            "environment_requirement_refs": ["envreq:executable:gh"],
        }
    )

    skills = {item["name"]: item for item in manager.list_skills()["skills"]}
    rows = {item["id"]: item for item in manager.skills_dashboard()["items"]}

    assert skills["code-review"]["environment_requirement_refs"] == ["envreq:executable:gh"]
    assert skills["code-review"]["runtime_footprint"]["runs_on"] == "local_peer"
    assert rows["skill:code-review"]["runtime_footprint"]["runs_on"] == "local_peer"

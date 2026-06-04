from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from labrastro_server.services.admin.service import (
    AdminConfigResult,
    RemoteAdminConfigManager,
    parse_mcp_servers_config,
)
from reuleauxcoder.domain.hooks.lifecycle import lifecycle_registry_from_config
from reuleauxcoder.services.config.loader import ConfigLoader


class MemoryAdminManager(RemoteAdminConfigManager):
    def __init__(self, config_path: Path) -> None:
        super().__init__(config_path=config_path)
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
            "unavailable_reason": "trust:pending_review",
            "placement_runtime": {
                "server": {
                    "executable": False,
                    "unavailable_reason": "trust:pending_review",
                },
            },
            "runtime_context_required": ["prompt_model"],
            "permissions": ["prompt.read"],
            "risk_level": "low",
            "technical": {
                "handler_ref": "skills/code-review/SKILL.md",
                "matcher": {"trigger_source": "chat"},
            },
        }
    ]
    assert "handler_ref" not in row["hook_views"][0]


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

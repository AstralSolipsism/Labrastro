from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from labrastro_server.services.admin.service import (
    AdminConfigResult,
    RemoteAdminConfigManager,
    parse_mcp_servers_config,
)


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

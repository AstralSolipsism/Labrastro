from pathlib import Path

from types import SimpleNamespace

from reuleauxcoder.domain.config.models import MCPServerConfig
from reuleauxcoder.extensions.mcp.manifest import (
    MCPManifestManager,
    run_mcp_record_cli,
)
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config


def test_record_mcp_server_creates_command_manifest(tmp_path: Path) -> None:
    config_path = tmp_path / ".rcoder" / "config.yaml"
    manager = MCPManifestManager(config_path)

    result = manager.record_server(
        MCPServerConfig(
            name="gitnexus",
            command="gitnexus",
            args=["mcp"],
            placement="peer",
            distribution="command",
            version="1.6.3",
            environment_requirement_refs=[
                "envreq:runtime:node",
                "envreq:executable:npm",
            ],
            check="gitnexus --version",
            install="npm install -g gitnexus@1.6.3",
            source="npm:gitnexus",
            description="Repository indexing MCP server",
        )
    )

    data = load_yaml_config(config_path)
    server = data["mcp"]["servers"]["gitnexus"]
    assert result.created is True
    assert server["distribution"] == "command"
    assert server["command"] == "gitnexus"
    assert server["args"] == ["mcp"]
    assert server["environment_requirement_refs"] == [
        "envreq:runtime:node",
        "envreq:executable:npm",
    ]
    assert server["check"] == "gitnexus --version"
    assert server["install"] == "npm install -g gitnexus@1.6.3"


def test_record_mcp_server_preserves_existing_artifacts(tmp_path: Path) -> None:
    config_path = tmp_path / ".rcoder" / "config.yaml"
    save_yaml_config(
        config_path,
        {
            "mcp": {
                "servers": {
                    "gitnexus": {
                        "command": "old",
                        "artifacts": {
                            "linux-amd64": {
                                "path": "gitnexus/1.6.3/linux-amd64.tar.gz",
                                "sha256": "abc",
                            }
                        },
                    }
                }
            }
        },
    )

    result = MCPManifestManager(config_path).record_server(
        MCPServerConfig(
            name="gitnexus",
            command="gitnexus",
            args=["mcp"],
            placement="peer",
            distribution="command",
        )
    )

    data = load_yaml_config(config_path)
    server = data["mcp"]["servers"]["gitnexus"]
    assert result.created is False
    assert server["distribution"] == "command"
    assert "linux-amd64" in server["artifacts"]


def test_run_mcp_record_cli_requires_authoritative_config(
    capsys, monkeypatch
) -> None:
    monkeypatch.delenv("RCODER_CONFIG_PATH", raising=False)

    result = run_mcp_record_cli(
        SimpleNamespace(
            config=None,
            server_name="gitnexus",
            mcp_tool_command="gitnexus",
            mcp_arg=["mcp"],
            env=[],
            placement="peer",
            distribution="command",
            version=None,
            environment_requirement_ref=[],
            check=None,
            install=None,
            source=None,
            description=None,
        )
    )

    assert result == 1
    assert "requires --config or RCODER_CONFIG_PATH" in capsys.readouterr().out

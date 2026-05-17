from pathlib import Path
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.config.models import EnvironmentCLIToolConfig
from reuleauxcoder.extensions.environment.manifest import (
    EnvironmentManifestManager,
    run_env_record_cli,
)
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader


def test_record_cli_tool_writes_global_manifest_shape(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    manager = EnvironmentManifestManager(config_path)

    result = manager.record_cli_tool(
        EnvironmentCLIToolConfig(
            name="gitnexus",
            command="gitnexus",
            tags=["repo_index"],
            check="gitnexus --version",
            install="npm install -g gitnexus",
            source="npm",
        )
    )

    data = load_yaml_config(config_path)
    tool = data["environment"]["cli_tools"]["gitnexus"]
    assert result.created is True
    assert tool["command"] == "gitnexus"
    assert tool["tags"] == ["repo_index"]
    assert tool["check"] == "gitnexus --version"
    assert tool["install"] == "npm install -g gitnexus"
    assert tool["source"] == "npm"


def test_record_cli_tool_updates_existing_entry(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    manager = EnvironmentManifestManager(config_path)

    manager.record_cli_tool(
        EnvironmentCLIToolConfig(
            name="beads",
            command="beads",
            check="beads --version",
        )
    )
    result = manager.record_cli_tool(
        EnvironmentCLIToolConfig(
            name="beads",
            command="bds",
            check="bds --version",
        )
    )

    data = load_yaml_config(config_path)
    assert result.created is False
    assert data["environment"]["cli_tools"]["beads"]["command"] == "bds"
    assert data["environment"]["cli_tools"]["beads"]["check"] == "bds --version"


def test_record_cli_tool_requires_command_and_check(tmp_path: Path) -> None:
    manager = EnvironmentManifestManager(tmp_path / "config.yaml")

    with pytest.raises(ValueError, match="tool command is required"):
        manager.record_cli_tool(EnvironmentCLIToolConfig(name="gitnexus"))

    with pytest.raises(ValueError, match="tool check command is required"):
        manager.record_cli_tool(
            EnvironmentCLIToolConfig(name="gitnexus", command="gitnexus")
        )


def test_run_env_record_cli_writes_explicit_manifest(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.yaml"

    exit_code = run_env_record_cli(
        SimpleNamespace(
            config=str(config_path),
            tool_name="gitnexus",
            tool_command="gitnexus",
            tag=["repo_index"],
            check="gitnexus --version",
            install="npm install -g gitnexus",
            version="1.2.3",
            source="npm",
            description="Repository index CLI",
        )
    )

    data = load_yaml_config(config_path)
    tool = data["environment"]["cli_tools"]["gitnexus"]
    assert exit_code == 0
    assert tool["command"] == "gitnexus"
    assert tool["version"] == "1.2.3"
    assert "Created CLI environment entry 'gitnexus'" in capsys.readouterr().out


def test_run_env_record_cli_uses_rcoder_config_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config_path = tmp_path / "host.yaml"
    monkeypatch.setenv("RCODER_CONFIG_PATH", str(config_path))

    exit_code = run_env_record_cli(
        SimpleNamespace(
            config=None,
            tool_name="beads",
            tool_command="beads",
            tag=[],
            check="beads --version",
            install=None,
            version=None,
            source=None,
            description=None,
        )
    )

    data = load_yaml_config(config_path)
    assert exit_code == 0
    assert data["environment"]["cli_tools"]["beads"]["command"] == "beads"
    assert str(config_path) in capsys.readouterr().out


def test_run_env_record_cli_requires_authoritative_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    global_path = tmp_path / "global.yaml"
    monkeypatch.delenv("RCODER_CONFIG_PATH", raising=False)
    monkeypatch.setattr(ConfigLoader, "GLOBAL_CONFIG_PATH", global_path)

    exit_code = run_env_record_cli(
        SimpleNamespace(
            config=None,
            tool_name="gitnexus",
            tool_command="gitnexus",
            tag=[],
            check="gitnexus --version",
            install=None,
            version=None,
            source=None,
            description=None,
        )
    )

    assert exit_code == 1
    assert not global_path.exists()
    assert "requires --config or RCODER_CONFIG_PATH" in capsys.readouterr().out

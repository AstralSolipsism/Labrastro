from pathlib import Path
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.config.models import EnvironmentRequirementConfig
from reuleauxcoder.extensions.environment.manifest import (
    EnvironmentManifestManager,
    run_env_record_cli,
)
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader


def test_record_requirement_writes_global_manifest_shape(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    manager = EnvironmentManifestManager(config_path)

    result = manager.record_requirement(
        EnvironmentRequirementConfig(
            id="envreq:executable:gitnexus",
            kind="executable",
            name="gitnexus",
            command="gitnexus",
            tags=["repo_index"],
            check="gitnexus --version",
            install="npm install -g gitnexus",
            source="npm",
        )
    )

    data = load_yaml_config(config_path)
    requirement = data["environment"]["requirements"]["envreq:executable:gitnexus"]
    assert result.created is True
    assert requirement["kind"] == "executable"
    assert requirement["command"] == "gitnexus"
    assert requirement["tags"] == ["repo_index"]
    assert requirement["check"] == "gitnexus --version"
    assert requirement["install"] == "npm install -g gitnexus"
    assert requirement["source"] == "npm"


def test_record_requirement_updates_existing_entry(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    manager = EnvironmentManifestManager(config_path)

    manager.record_requirement(
        EnvironmentRequirementConfig(
            id="envreq:executable:beads",
            kind="executable",
            name="beads",
            command="beads",
            check="beads --version",
        )
    )
    result = manager.record_requirement(
        EnvironmentRequirementConfig(
            id="envreq:executable:beads",
            kind="executable",
            name="beads",
            command="bds",
            check="bds --version",
        )
    )

    data = load_yaml_config(config_path)
    assert result.created is False
    assert data["environment"]["requirements"]["envreq:executable:beads"]["command"] == "bds"
    assert data["environment"]["requirements"]["envreq:executable:beads"]["check"] == "bds --version"


def test_record_requirement_requires_name_and_allows_declarative_entry(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    manager = EnvironmentManifestManager(config_path)

    with pytest.raises(ValueError, match="requirement name is required"):
        manager.record_requirement(
            EnvironmentRequirementConfig(
                id="envreq:executable:gitnexus",
                kind="executable",
                name="",
            )
        )

    result = manager.record_requirement(
        EnvironmentRequirementConfig(
            id="envreq:runtime:dotnet-sdk",
            kind="runtime",
            name="dotnet-sdk",
            placement="peer",
            description="Required by C# skills.",
        )
    )

    data = load_yaml_config(config_path)
    requirement = data["environment"]["requirements"]["envreq:runtime:dotnet-sdk"]
    assert result.created is True
    assert requirement["kind"] == "runtime"
    assert requirement["placement"] == "peer"
    assert "check" not in requirement
    assert "install" not in requirement
    assert "configure" not in requirement


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
    requirement = data["environment"]["requirements"]["envreq:executable:gitnexus"]
    assert exit_code == 0
    assert requirement["kind"] == "executable"
    assert requirement["command"] == "gitnexus"
    assert requirement["version"] == "1.2.3"
    assert "Created environment requirement 'envreq:executable:gitnexus'" in capsys.readouterr().out


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
    assert data["environment"]["requirements"]["envreq:executable:beads"]["command"] == "beads"
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

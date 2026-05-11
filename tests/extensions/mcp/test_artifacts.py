from __future__ import annotations

import os
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

from reuleauxcoder.extensions.mcp.artifacts import (
    MCPArtifactManager,
    run_mcp_artifact_cli,
)
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config


def test_import_artifact_updates_config_and_verify(tmp_path: Path) -> None:
    config_path = tmp_path / ".rcoder" / "config.yaml"
    save_yaml_config(
        config_path,
        {"mcp": {"artifact_root": str(tmp_path / "artifacts"), "servers": {}}},
    )
    archive = tmp_path / "manual.zip"
    archive.write_bytes(b"artifact")

    manager = MCPArtifactManager(config_path)
    record = manager.import_artifact("filesystem", "1.0.0", "windows-amd64", archive)

    assert record.verified is True
    data = load_yaml_config(config_path)
    server = data["mcp"]["servers"]["filesystem"]
    assert server["placement"] == "peer"
    assert server["distribution"] == "artifact"
    assert server["version"] == "1.0.0"
    artifact = server["artifacts"]["windows-amd64"]
    assert artifact["path"] == "filesystem/1.0.0/windows-amd64.zip"
    verified = manager.verify_artifacts("filesystem")
    assert len(verified) == 1
    assert verified[0].verified is True


def test_artifact_list_marks_command_distribution_retained_artifacts(
    tmp_path: Path, capsys
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifact_path = artifact_root / "gitnexus" / "1.6.3" / "linux-amd64.tar.gz"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(b"artifact")
    config_path = tmp_path / ".rcoder" / "config.yaml"
    save_yaml_config(
        config_path,
        {
            "mcp": {
                "artifact_root": str(artifact_root),
                "servers": {
                    "gitnexus": {
                        "distribution": "command",
                        "version": "1.6.3",
                        "artifacts": {
                            "linux-amd64": {
                                "path": "gitnexus/1.6.3/linux-amd64.tar.gz",
                                "sha256": "unused",
                            }
                        },
                    }
                },
            }
        },
    )

    exit_code = run_mcp_artifact_cli(
        SimpleNamespace(
            artifact_command="list",
            server_name="gitnexus",
            config=str(config_path),
        )
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "distribution=command" in out
    assert "usage=retained-not-default" in out


def test_build_node_creates_platform_artifacts_and_updates_config(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / ".rcoder" / "config.yaml"
    artifact_root = tmp_path / "artifacts"
    save_yaml_config(
        config_path,
        {"mcp": {"artifact_root": str(artifact_root), "servers": {}}},
    )
    fake_npm = _write_fake_npm(tmp_path)
    monkeypatch.setenv(
        "PATH", str(fake_npm.parent) + os.pathsep + os.environ.get("PATH", "")
    )

    manager = MCPArtifactManager(config_path, npm_cmd=fake_npm.name)
    records = manager.build_node(
        "browser-tools",
        "@demo/browser-mcp@latest",
        "browser-mcp",
        ["windows-amd64", "linux-amd64"],
    )

    assert {record.platform for record in records} == {"windows-amd64", "linux-amd64"}
    data = load_yaml_config(config_path)
    server = data["mcp"]["servers"]["browser-tools"]
    assert server["placement"] == "peer"
    assert server["distribution"] == "artifact"
    assert server["version"] == "1.2.3"
    assert server["requirements"] == {"node": "required", "npm": "required"}
    assert server["build"]["type"] == "node"
    assert server["build"]["package"] == "@demo/browser-mcp"
    assert server["build"]["package_version"] == "1.2.3"

    windows = server["artifacts"]["windows-amd64"]
    assert windows["launch"]["command"] == "{{bundle}}/run.cmd"
    with zipfile.ZipFile(artifact_root / windows["path"]) as zf:
        names = set(zf.namelist())
    assert "run.cmd" in names
    assert "package/package.json" in names
    assert "package/package-lock.json" in names
    assert "npm-cache/cache-marker" in names
    with zipfile.ZipFile(artifact_root / windows["path"]) as zf:
        run_cmd = zf.read("run.cmd").decode("utf-8")
    assert ".install-complete" in run_cmd

    linux = server["artifacts"]["linux-amd64"]
    assert linux["launch"]["command"] == "{{bundle}}/run.sh"
    with tarfile.open(artifact_root / linux["path"], "r:gz") as tf:
        names = {member.name for member in tf.getmembers()}
        run_sh = tf.extractfile("run.sh").read().decode("utf-8")  # type: ignore[union-attr]
    assert "run.sh" in names
    assert "package/package.json" in names
    assert "npm-cache/cache-marker" in names
    assert ".install-complete" in run_sh

    verified = manager.verify_artifacts()
    assert len(verified) == 2
    assert all(record.verified for record in verified)


def test_install_node_server_writes_pinned_npx_config(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / ".rcoder" / "config.yaml"
    save_yaml_config(
        config_path,
        {"mcp": {"artifact_root": str(tmp_path / "artifacts"), "servers": {}}},
    )
    fake_npm = _write_fake_npm(tmp_path)
    monkeypatch.setenv(
        "PATH", str(fake_npm.parent) + os.pathsep + os.environ.get("PATH", "")
    )

    manager = MCPArtifactManager(config_path, npm_cmd=fake_npm.name)
    result = manager.install_node(
        "github",
        "@demo/github@latest",
        "github-mcp",
        args=["--read-only"],
        env={"TOKEN": "secret"},
    )

    assert result.version == "1.2.3"
    assert result.placement == "server"
    assert result.artifacts == []
    server = load_yaml_config(config_path)["mcp"]["servers"]["github"]
    assert server["placement"] == "server"
    assert server["distribution"] == "command"
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "@demo/github@1.2.3", "--read-only"]
    assert server["env"] == {"TOKEN": "secret"}
    assert server["build"]["bin"] == "github-mcp"


def test_install_node_peer_defaults_to_windows_and_linux_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / ".rcoder" / "config.yaml"
    artifact_root = tmp_path / "artifacts"
    save_yaml_config(
        config_path,
        {"mcp": {"artifact_root": str(artifact_root), "servers": {}}},
    )
    fake_npm = _write_fake_npm(tmp_path)
    monkeypatch.setenv(
        "PATH", str(fake_npm.parent) + os.pathsep + os.environ.get("PATH", "")
    )

    manager = MCPArtifactManager(config_path, npm_cmd=fake_npm.name)
    result = manager.install_node(
        "filesystem",
        "@demo/filesystem@latest",
        "filesystem-mcp",
        placement="peer",
        args=["--root", "{{workspace}}"],
        env={"MODE": "local"},
    )

    assert {record.platform for record in result.artifacts} == {
        "windows-amd64",
        "linux-amd64",
    }
    server = load_yaml_config(config_path)["mcp"]["servers"]["filesystem"]
    assert server["placement"] == "peer"
    assert server["distribution"] == "artifact"
    assert server["version"] == "1.2.3"
    assert server["requirements"] == {"node": "required", "npm": "required"}
    assert set(server["artifacts"]) == {"windows-amd64", "linux-amd64"}
    assert server["artifacts"]["windows-amd64"]["launch"]["args"] == [
        "--root",
        "{{workspace}}",
    ]
    assert server["artifacts"]["linux-amd64"]["launch"]["env"] == {"MODE": "local"}


def test_install_node_both_writes_server_config_and_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / ".rcoder" / "config.yaml"
    save_yaml_config(
        config_path,
        {"mcp": {"artifact_root": str(tmp_path / "artifacts"), "servers": {}}},
    )
    fake_npm = _write_fake_npm(tmp_path)
    monkeypatch.setenv(
        "PATH", str(fake_npm.parent) + os.pathsep + os.environ.get("PATH", "")
    )

    manager = MCPArtifactManager(config_path, npm_cmd=fake_npm.name)
    result = manager.install_node(
        "browser",
        "@demo/browser@latest",
        "browser-mcp",
        placement="both",
        platforms=["windows-amd64"],
        args=["--port", "9222"],
    )

    assert [record.platform for record in result.artifacts] == ["windows-amd64"]
    server = load_yaml_config(config_path)["mcp"]["servers"]["browser"]
    assert server["placement"] == "both"
    assert server["distribution"] == "artifact"
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "@demo/browser@1.2.3", "--port", "9222"]
    assert server["artifacts"]["windows-amd64"]["launch"]["args"] == [
        "--port",
        "9222",
    ]


def test_install_node_accepts_repeated_platform_groups(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / ".rcoder" / "config.yaml"
    save_yaml_config(
        config_path,
        {"mcp": {"artifact_root": str(tmp_path / "artifacts"), "servers": {}}},
    )
    fake_npm = _write_fake_npm(tmp_path)
    monkeypatch.setenv(
        "PATH", str(fake_npm.parent) + os.pathsep + os.environ.get("PATH", "")
    )

    manager = MCPArtifactManager(config_path, npm_cmd=fake_npm.name)
    result = manager.install_node(
        "filesystem",
        "@demo/filesystem@latest",
        "filesystem-mcp",
        placement="peer",
        platforms=[["linux-amd64"], ["windows-amd64"]],  # type: ignore[list-item]
    )

    assert [record.platform for record in result.artifacts] == [
        "linux-amd64",
        "windows-amd64",
    ]


def _write_fake_npm(tmp_path: Path) -> Path:
    script = tmp_path / "fake_npm.py"
    script.write_text(
        """
import json
import pathlib
import sys

args = sys.argv[1:]
if args[:1] == ["view"]:
    print(json.dumps("1.2.3"))
    raise SystemExit(0)
if args[:1] == ["install"]:
    cwd = pathlib.Path.cwd()
    (cwd / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": {}}),
        encoding="utf-8",
    )
    cache = pathlib.Path(args[args.index("--cache") + 1])
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "cache-marker").write_text("cached", encoding="utf-8")
    (cwd / "node_modules" / ".bin").mkdir(parents=True, exist_ok=True)
    raise SystemExit(0)
print("unexpected npm args: " + " ".join(args), file=sys.stderr)
raise SystemExit(2)
""".lstrip(),
        encoding="utf-8",
    )
    if os.name == "nt":
        cmd = tmp_path / "npm.cmd"
        cmd.write_text(f'@echo off\r\n"{sys_executable()}" "{script}" %*\r\n', encoding="utf-8")
        return cmd
    npm = tmp_path / "npm"
    npm.write_text(f'#!/usr/bin/env sh\n"{sys_executable()}" "{script}" "$@"\n', encoding="utf-8")
    npm.chmod(0o755)
    return npm


def sys_executable() -> str:
    import sys

    return sys.executable

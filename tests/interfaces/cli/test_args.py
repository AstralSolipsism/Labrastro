import sys

import pytest

from reuleauxcoder.interfaces.cli.args import parse_args


def test_parse_args_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["rcoder"])
    args = parse_args()
    assert args.config is None
    assert args.model is None
    assert args.prompt is None
    assert args.resume is None


def test_parse_args_all_supported_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rcoder",
            "-c",
            "config.yaml",
            "-m",
            "gpt-4o",
            "-p",
            "hello",
            "-r",
            "session-1",
        ],
    )
    args = parse_args()
    assert args.config == "config.yaml"
    assert args.model == "gpt-4o"
    assert args.prompt == "hello"
    assert args.resume == "session-1"


def test_parse_mcp_artifact_build_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rcoder",
            "-c",
            "config.yaml",
            "mcp",
            "artifact",
            "build-node",
            "filesystem",
            "--package",
            "@demo/filesystem@latest",
            "--bin",
            "filesystem-mcp",
            "--platform",
            "windows-amd64",
            "linux-amd64",
        ],
    )

    args = parse_args()

    assert args.config == "config.yaml"
    assert args.command == "mcp"
    assert args.mcp_command == "artifact"
    assert args.artifact_command == "build-node"
    assert args.server_name == "filesystem"
    assert args.package == "@demo/filesystem@latest"
    assert args.bin == "filesystem-mcp"
    assert args.platform == ["windows-amd64", "linux-amd64"]


def test_parse_env_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rcoder",
            "env",
            "record",
            "gitnexus",
            "--command",
            "gitnexus",
            "--check",
            "gitnexus --version",
            "--install",
            "npm install -g gitnexus",
            "--tag",
            "repo_index",
            "--source",
            "npm",
        ],
    )

    args = parse_args()

    assert args.command == "env"
    assert args.env_command == "record"
    assert args.tool_name == "gitnexus"
    assert args.tool_command == "gitnexus"
    assert args.check == "gitnexus --version"
    assert args.install == "npm install -g gitnexus"
    assert args.tag == ["repo_index"]
    assert args.source == "npm"


def test_parse_provider_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rcoder",
            "-c",
            "config.yaml",
            "provider",
            "record",
            "anthropic-main",
            "--type",
            "anthropic_messages",
            "--compat",
            "deepseek",
            "--api-key-env",
            "ANTHROPIC_API_KEY",
            "--base-url",
            "https://api.anthropic.com",
            "--api-feature",
            "thinking=true",
        ],
    )

    args = parse_args()

    assert args.config == "config.yaml"
    assert args.command == "provider"
    assert args.provider_command == "record"
    assert args.provider_id == "anthropic-main"
    assert args.provider_type == "anthropic_messages"
    assert args.compat == "deepseek"
    assert args.api_key_env == "ANTHROPIC_API_KEY"
    assert args.api_feature == ["thinking=true"]


def test_parse_provider_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["rcoder", "provider", "list"])

    args = parse_args()

    assert args.command == "provider"
    assert args.provider_command == "list"


def test_parse_provider_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rcoder",
            "provider",
            "test",
            "openai-main",
            "--model",
            "gpt-demo",
            "--prompt",
            "hello",
        ],
    )

    args = parse_args()

    assert args.command == "provider"
    assert args.provider_command == "test"
    assert args.provider_id == "openai-main"
    assert args.model == "gpt-demo"
    assert args.prompt == "hello"


def test_parse_mcp_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rcoder",
            "-c",
            "config.yaml",
            "mcp",
            "record",
            "gitnexus",
            "--command",
            "gitnexus",
            "--arg",
            "mcp",
            "--placement",
            "peer",
            "--distribution",
            "command",
            "--version",
            "1.6.3",
            "--check",
            "gitnexus --version",
            "--install",
            "npm install -g gitnexus@1.6.3",
            "--requirement",
            "node=>=20",
            "--requirement",
            "npm=required",
            "--source",
            "npm:gitnexus",
        ],
    )

    args = parse_args()

    assert args.config == "config.yaml"
    assert args.command == "mcp"
    assert args.mcp_command == "record"
    assert args.server_name == "gitnexus"
    assert args.mcp_tool_command == "gitnexus"
    assert args.mcp_arg == ["mcp"]
    assert args.placement == "peer"
    assert args.distribution == "command"
    assert args.requirement == ["node=>=20", "npm=required"]


def test_parse_mcp_install_node_defaults_to_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rcoder",
            "mcp",
            "install-node",
            "github",
            "--package",
            "@demo/github@latest",
            "--bin",
            "github-mcp",
        ],
    )

    args = parse_args()

    assert args.command == "mcp"
    assert args.mcp_command == "install-node"
    assert args.server_name == "github"
    assert args.placement == "server"
    assert args.platform is None
    assert args.node_arg == []
    assert args.env == []


def test_parse_mcp_install_node_peer_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rcoder",
            "mcp",
            "install-node",
            "filesystem",
            "--package",
            "@demo/filesystem@latest",
            "--bin",
            "filesystem-mcp",
            "--placement",
            "both",
            "--platform",
            "windows-amd64",
            "--arg=--root",
            "--arg",
            "{{workspace}}",
            "--env",
            "MODE=local",
        ],
    )

    args = parse_args()

    assert args.placement == "both"
    assert args.platform == [["windows-amd64"]]
    assert args.node_arg == ["--root", "{{workspace}}"]
    assert args.env == ["MODE=local"]


def test_parse_mcp_install_node_repeated_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rcoder",
            "mcp",
            "install-node",
            "filesystem",
            "--package",
            "@demo/filesystem@latest",
            "--bin",
            "filesystem-mcp",
            "--placement",
            "peer",
            "--platform",
            "linux-amd64",
            "--platform",
            "windows-amd64",
        ],
    )

    args = parse_args()

    assert args.platform == [["linux-amd64"], ["windows-amd64"]]


def test_parse_args_version_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["rcoder", "--version"])
    with pytest.raises(SystemExit):
        parse_args()

"""Lightweight CLI environment manifest recording."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reuleauxcoder.domain.config.models import EnvironmentCLIToolConfig
from reuleauxcoder.extensions.config_target import resolve_cli_config_path
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader


@dataclass(slots=True)
class EnvironmentRecordResult:
    name: str
    path: Path
    created: bool


class EnvironmentManifestManager:
    """Record server-authoritative CLI environment entries.

    This manager intentionally does not scan, verify, install, or inspect the local
    machine. It only updates the manifest that environment-capable Agent runtime
    tasks consume.
    """

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or ConfigLoader.GLOBAL_CONFIG_PATH

    def record_cli_tool(self, tool: EnvironmentCLIToolConfig) -> EnvironmentRecordResult:
        if not tool.name.strip():
            raise ValueError("tool name is required")
        if not tool.command.strip():
            raise ValueError("tool command is required")
        if not tool.check.strip():
            raise ValueError("tool check command is required")

        data = self._load_data()
        env_data = data.setdefault("environment", {})
        if not isinstance(env_data, dict):
            env_data = {}
            data["environment"] = env_data
        cli_tools = env_data.setdefault("cli_tools", {})
        if not isinstance(cli_tools, dict):
            cli_tools = {}
            env_data["cli_tools"] = cli_tools

        created = tool.name not in cli_tools
        cli_tools[tool.name] = tool.to_dict()
        save_yaml_config(self.config_path, data)
        return EnvironmentRecordResult(
            name=tool.name,
            path=self.config_path,
            created=created,
        )

    def _load_data(self) -> dict:
        try:
            data = load_yaml_config(self.config_path)
        except FileNotFoundError:
            data = {}
        return data if isinstance(data, dict) else {}


def run_env_record_cli(args) -> int:
    try:
        tool = EnvironmentCLIToolConfig(
            name=str(args.tool_name),
            command=str(args.tool_command),
            tags=[str(item) for item in args.tag],
            check=str(args.check),
            install=str(args.install or ""),
            version=str(args.version) if args.version else None,
            source=str(args.source or ""),
            description=str(args.description or ""),
        )
        config_path = resolve_cli_config_path(args, require=True, purpose="env record")
        result = EnvironmentManifestManager(config_path).record_cli_tool(tool)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    verb = "Created" if result.created else "Updated"
    print(f"{verb} CLI environment entry '{result.name}' in {result.path}")
    return 0

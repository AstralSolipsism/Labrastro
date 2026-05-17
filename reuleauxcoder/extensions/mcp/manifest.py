"""Lightweight MCP manifest recording."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reuleauxcoder.domain.config.models import MCPServerConfig
from reuleauxcoder.extensions.config_target import resolve_cli_config_path
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader


@dataclass(slots=True)
class MCPRecordResult:
    name: str
    path: Path
    created: bool


class MCPManifestManager:
    """Record server-authoritative MCP entries without installing or scanning."""

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or ConfigLoader.GLOBAL_CONFIG_PATH

    def record_server(self, server: MCPServerConfig) -> MCPRecordResult:
        if not server.name.strip():
            raise ValueError("MCP server name is required")
        if not server.command.strip():
            raise ValueError("MCP server command is required")

        data = self._load_data()
        mcp_data = data.setdefault("mcp", {})
        if not isinstance(mcp_data, dict):
            mcp_data = {}
            data["mcp"] = mcp_data
        servers = mcp_data.setdefault("servers", {})
        if not isinstance(servers, dict):
            servers = {}
            mcp_data["servers"] = servers

        created = server.name not in servers
        existing = servers.get(server.name)
        existing_artifacts = {}
        if isinstance(existing, dict) and isinstance(existing.get("artifacts"), dict):
            existing_artifacts = dict(existing["artifacts"])

        entry = server.to_dict()
        if existing_artifacts:
            entry["artifacts"] = existing_artifacts
        servers[server.name] = entry
        save_yaml_config(self.config_path, data)
        return MCPRecordResult(
            name=server.name,
            path=self.config_path,
            created=created,
        )

    def _load_data(self) -> dict:
        try:
            data = load_yaml_config(self.config_path)
        except FileNotFoundError:
            data = {}
        return data if isinstance(data, dict) else {}


def run_mcp_record_cli(args) -> int:
    try:
        server = MCPServerConfig(
            name=str(args.server_name),
            command=str(args.mcp_tool_command),
            args=[str(item) for item in args.mcp_arg],
            env=_parse_env_entries(list(args.env or [])),
            enabled=True,
            placement=args.placement,
            distribution=args.distribution,
            version=str(args.version) if args.version else None,
            requirements=_parse_requirement_entries(list(args.requirement or [])),
            check=str(args.check or ""),
            install=str(args.install or ""),
            source=str(args.source or ""),
            description=str(args.description or ""),
        )
        config_path = resolve_cli_config_path(args, require=True, purpose="mcp record")
        result = MCPManifestManager(config_path).record_server(server)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    verb = "Created" if result.created else "Updated"
    print(f"{verb} MCP manifest entry '{result.name}' in {result.path}")
    return 0


def _parse_env_entries(entries: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError(f"invalid --env entry, expected KEY=VALUE: {entry}")
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"invalid --env entry, empty key: {entry}")
        env[key] = value
    return env


def _parse_requirement_entries(entries: list[str]) -> dict[str, str]:
    requirements: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError(
                f"invalid --requirement entry, expected NAME=VALUE: {entry}"
            )
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"invalid --requirement entry, empty name: {entry}")
        requirements[key] = value
    return requirements


__all__ = [
    "MCPManifestManager",
    "MCPRecordResult",
    "run_mcp_record_cli",
]

"""Remote relay protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _string_list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


@dataclass
class MCPArtifactManifest:
    platform: str
    path: str
    sha256: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "path": self.path,
            "sha256": self.sha256,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MCPArtifactManifest":
        return cls(
            platform=str(d.get("platform", "")),
            path=str(d.get("path", "")),
            sha256=str(d.get("sha256", "")),
            url=str(d.get("url", "")),
        )

@dataclass
class MCPLaunchManifest:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "cwd": self.cwd,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MCPLaunchManifest":
        raw_args = d.get("args", [])
        raw_env = d.get("env", {})
        return cls(
            command=str(d.get("command", "")),
            args=[str(arg) for arg in raw_args] if isinstance(raw_args, list) else [],
            env=(
                {str(k): str(v) for k, v in raw_env.items()}
                if isinstance(raw_env, dict)
                else {}
            ),
            cwd=str(d["cwd"]) if d.get("cwd") is not None else None,
        )

@dataclass
class MCPServerManifest:
    name: str = ""
    version: str = ""
    distribution: str = "artifact"
    artifact: MCPArtifactManifest | None = None
    launch: MCPLaunchManifest = field(
        default_factory=lambda: MCPLaunchManifest(command="")
    )
    permissions: dict[str, Any] = field(default_factory=dict)
    environment_requirement_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "distribution": self.distribution,
            "artifact": self.artifact.to_dict() if self.artifact is not None else None,
            "launch": self.launch.to_dict(),
            "permissions": self.permissions,
            "environment_requirement_refs": list(self.environment_requirement_refs),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MCPServerManifest":
        raw_artifact = d.get("artifact")
        return cls(
            name=str(d.get("name", "")),
            version=str(d.get("version", "")),
            distribution=str(d.get("distribution", "artifact") or "artifact"),
            artifact=(
                MCPArtifactManifest.from_dict(raw_artifact)
                if isinstance(raw_artifact, dict)
                else None
            ),
            launch=MCPLaunchManifest.from_dict(d.get("launch", {})),
            permissions=(
                dict(d.get("permissions", {}))
                if isinstance(d.get("permissions", {}), dict)
                else {}
            ),
            environment_requirement_refs=_string_list_value(
                d.get("environment_requirement_refs", [])
            ),
        )

@dataclass
class MCPManifestRequest:
    peer_token: str
    os: str
    arch: str
    workspace: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "os": self.os,
            "arch": self.arch,
            "workspace": self.workspace,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MCPManifestRequest":
        return cls(
            peer_token=d["peer_token"],
            os=str(d.get("os", "")),
            arch=str(d.get("arch", "")),
            workspace=str(d.get("workspace", "")),
        )

@dataclass
class MCPManifestResponse:
    servers: list[MCPServerManifest] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "servers": [server.to_dict() for server in self.servers],
            "diagnostics": self.diagnostics,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MCPManifestResponse":
        return cls(
            servers=[
                MCPServerManifest.from_dict(item)
                for item in d.get("servers", [])
                if isinstance(item, dict)
            ],
            diagnostics=[
                dict(item)
                for item in d.get("diagnostics", [])
                if isinstance(item, dict)
            ],
        )

@dataclass
class RemoteMCPToolInfo:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    server_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "server_name": self.server_name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RemoteMCPToolInfo":
        input_schema = d.get("input_schema", {})
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}
        return cls(
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
            input_schema=input_schema,
            server_name=str(d.get("server_name", "")),
        )

@dataclass
class PeerMCPToolsReport:
    peer_token: str
    tools: list[RemoteMCPToolInfo] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "tools": [tool.to_dict() for tool in self.tools],
            "diagnostics": self.diagnostics,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PeerMCPToolsReport":
        return cls(
            peer_token=d["peer_token"],
            tools=[
                RemoteMCPToolInfo.from_dict(item)
                for item in d.get("tools", [])
                if isinstance(item, dict)
            ],
            diagnostics=[
                dict(item)
                for item in d.get("diagnostics", [])
                if isinstance(item, dict)
            ],
        )

__all__ = [
    "MCPArtifactManifest",
    "MCPLaunchManifest",
    "MCPServerManifest",
    "MCPManifestRequest",
    "MCPManifestResponse",
    "RemoteMCPToolInfo",
    "PeerMCPToolsReport",
]

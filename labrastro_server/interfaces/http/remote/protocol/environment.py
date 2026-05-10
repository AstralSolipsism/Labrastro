"""Remote relay protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _bool_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _string_list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _docs_value(value: Any) -> list[dict[str, str]]:
    docs: list[dict[str, str]] = []
    if not isinstance(value, list):
        return docs
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        if not title and not url:
            continue
        docs.append({"title": title, "url": url})
    return docs


def _string_dict_list_value(value: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not isinstance(value, list):
        return items
    for item in value:
        if not isinstance(item, dict):
            continue
        normalized = {
            str(key): str(val).strip()
            for key, val in item.items()
            if val is not None and str(val).strip()
        }
        if normalized:
            items.append(normalized)
    return items


@dataclass
class EnvironmentCLIToolManifest:
    name: str
    command: str = ""
    enabled: bool = True
    placement: str = "local"
    tags: list[str] = field(default_factory=list)
    requirements: dict[str, str] = field(default_factory=dict)
    check: str = ""
    install: str = ""
    version: str | None = None
    source: str = ""
    description: str = ""
    repo_url: str = ""
    docs: list[dict[str, str]] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    install_prompt: str = ""
    verify_prompt: str = ""
    notes: list[str] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)
    risk_level: str = ""
    last_action: str = ""
    last_updated: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "command": self.command,
            "enabled": self.enabled,
            "placement": self.placement,
            "tags": self.tags,
            "requirements": dict(self.requirements),
            "check": self.check,
            "install": self.install,
            "source": self.source,
            "description": self.description,
            "repo_url": self.repo_url,
            "docs": [dict(item) for item in self.docs],
            "evidence": [dict(item) for item in self.evidence],
            "install_prompt": self.install_prompt,
            "verify_prompt": self.verify_prompt,
            "notes": list(self.notes),
            "credentials": list(self.credentials),
            "risk_level": self.risk_level,
            "last_action": self.last_action,
            "last_updated": self.last_updated,
        }
        if self.version is not None:
            data["version"] = self.version
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EnvironmentCLIToolManifest":
        if "capabilities" in d:
            raise ValueError(
                "environment CLI tool manifest field capabilities was removed; use tags"
            )
        raw_tags = d.get("tags", [])
        raw_requirements = d.get("requirements", {})
        return cls(
            name=str(d.get("name", "")),
            command=str(d.get("command", "")),
            enabled=_bool_value(d.get("enabled", True)),
            placement=str(d.get("placement", "local") or "local"),
            tags=(
                [str(item) for item in raw_tags]
                if isinstance(raw_tags, list)
                else []
            ),
            requirements=(
                {str(k): str(v) for k, v in raw_requirements.items()}
                if isinstance(raw_requirements, dict)
                else {}
            ),
            check=str(d.get("check", "")),
            install=str(d.get("install", "")),
            version=str(d["version"]) if d.get("version") is not None else None,
            source=str(d.get("source", "")),
            description=str(d.get("description", "")),
            repo_url=str(d.get("repo_url", "")),
            docs=_docs_value(d.get("docs", [])),
            evidence=_string_dict_list_value(d.get("evidence", [])),
            install_prompt=str(d.get("install_prompt", "")),
            verify_prompt=str(d.get("verify_prompt", "")),
            notes=_string_list_value(d.get("notes", [])),
            credentials=_string_list_value(d.get("credentials", [])),
            risk_level=str(d.get("risk_level", "")),
            last_action=str(d.get("last_action", "")),
            last_updated=str(d.get("last_updated", "")),
        )

@dataclass
class EnvironmentMCPServerManifest:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    placement: str = "peer"
    distribution: str = "command"
    requirements: dict[str, str] = field(default_factory=dict)
    check: str = ""
    install: str = ""
    version: str | None = None
    source: str = ""
    description: str = ""
    repo_url: str = ""
    docs: list[dict[str, str]] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    install_prompt: str = ""
    verify_prompt: str = ""
    notes: list[str] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)
    risk_level: str = ""
    last_action: str = ""
    last_updated: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "cwd": self.cwd,
            "placement": self.placement,
            "distribution": self.distribution,
            "requirements": self.requirements,
            "check": self.check,
            "install": self.install,
            "source": self.source,
            "description": self.description,
            "repo_url": self.repo_url,
            "docs": [dict(item) for item in self.docs],
            "evidence": [dict(item) for item in self.evidence],
            "install_prompt": self.install_prompt,
            "verify_prompt": self.verify_prompt,
            "notes": list(self.notes),
            "credentials": list(self.credentials),
            "risk_level": self.risk_level,
            "last_action": self.last_action,
            "last_updated": self.last_updated,
        }
        if self.version is not None:
            data["version"] = self.version
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EnvironmentMCPServerManifest":
        raw_args = d.get("args", [])
        raw_env = d.get("env", {})
        raw_requirements = d.get("requirements", {})
        return cls(
            name=str(d.get("name", "")),
            command=str(d.get("command", "")),
            args=[str(item) for item in raw_args] if isinstance(raw_args, list) else [],
            env=(
                {str(k): str(v) for k, v in raw_env.items()}
                if isinstance(raw_env, dict)
                else {}
            ),
            cwd=str(d["cwd"]) if d.get("cwd") is not None else None,
            placement=str(d.get("placement", "peer") or "peer"),
            distribution=str(d.get("distribution", "command") or "command"),
            requirements=(
                {str(k): str(v) for k, v in raw_requirements.items()}
                if isinstance(raw_requirements, dict)
                else {}
            ),
            check=str(d.get("check", "")),
            install=str(d.get("install", "")),
            version=str(d["version"]) if d.get("version") is not None else None,
            source=str(d.get("source", "")),
            description=str(d.get("description", "")),
            repo_url=str(d.get("repo_url", "")),
            docs=_docs_value(d.get("docs", [])),
            evidence=_string_dict_list_value(d.get("evidence", [])),
            install_prompt=str(d.get("install_prompt", "")),
            verify_prompt=str(d.get("verify_prompt", "")),
            notes=_string_list_value(d.get("notes", [])),
            credentials=_string_list_value(d.get("credentials", [])),
            risk_level=str(d.get("risk_level", "")),
            last_action=str(d.get("last_action", "")),
            last_updated=str(d.get("last_updated", "")),
        )

@dataclass
class EnvironmentSkillManifest:
    name: str
    enabled: bool = True
    scope: str = "project"
    check: str = ""
    install: str = ""
    version: str | None = None
    source: str = ""
    description: str = ""
    path_hint: str | None = None
    requirements: dict[str, str] = field(default_factory=dict)
    repo_url: str = ""
    docs: list[dict[str, str]] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    install_prompt: str = ""
    verify_prompt: str = ""
    notes: list[str] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)
    risk_level: str = ""
    last_action: str = ""
    last_updated: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "enabled": self.enabled,
            "scope": self.scope,
            "check": self.check,
            "install": self.install,
            "source": self.source,
            "description": self.description,
            "requirements": dict(self.requirements),
            "repo_url": self.repo_url,
            "docs": [dict(item) for item in self.docs],
            "evidence": [dict(item) for item in self.evidence],
            "install_prompt": self.install_prompt,
            "verify_prompt": self.verify_prompt,
            "notes": list(self.notes),
            "credentials": list(self.credentials),
            "risk_level": self.risk_level,
            "last_action": self.last_action,
            "last_updated": self.last_updated,
        }
        if self.version is not None:
            data["version"] = self.version
        if self.path_hint is not None:
            data["path_hint"] = self.path_hint
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EnvironmentSkillManifest":
        raw_requirements = d.get("requirements", {})
        return cls(
            name=str(d.get("name", "")),
            enabled=_bool_value(d.get("enabled", True)),
            scope=str(d.get("scope", "project") or "project"),
            check=str(d.get("check", "")),
            install=str(d.get("install", "")),
            version=str(d["version"]) if d.get("version") is not None else None,
            source=str(d.get("source", "")),
            description=str(d.get("description", "")),
            path_hint=(
                str(d["path_hint"]) if d.get("path_hint") is not None else None
            ),
            requirements=(
                {str(k): str(v) for k, v in raw_requirements.items()}
                if isinstance(raw_requirements, dict)
                else {}
            ),
            repo_url=str(d.get("repo_url", "")),
            docs=_docs_value(d.get("docs", [])),
            evidence=_string_dict_list_value(d.get("evidence", [])),
            install_prompt=str(d.get("install_prompt", "")),
            verify_prompt=str(d.get("verify_prompt", "")),
            notes=_string_list_value(d.get("notes", [])),
            credentials=_string_list_value(d.get("credentials", [])),
            risk_level=str(d.get("risk_level", "")),
            last_action=str(d.get("last_action", "")),
            last_updated=str(d.get("last_updated", "")),
        )

@dataclass
class EnvironmentManifestRequest:
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
    def from_dict(cls, d: dict[str, Any]) -> "EnvironmentManifestRequest":
        return cls(
            peer_token=d["peer_token"],
            os=str(d.get("os", "")),
            arch=str(d.get("arch", "")),
            workspace=str(d.get("workspace", "")),
        )

@dataclass
class EnvironmentManifestResponse:
    cli_tools: list[EnvironmentCLIToolManifest] = field(default_factory=list)
    mcp_servers: list[EnvironmentMCPServerManifest] = field(default_factory=list)
    skills: list[EnvironmentSkillManifest] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cli_tools": [tool.to_dict() for tool in self.cli_tools],
            "mcp_servers": [server.to_dict() for server in self.mcp_servers],
            "skills": [skill.to_dict() for skill in self.skills],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EnvironmentManifestResponse":
        return cls(
            cli_tools=[
                EnvironmentCLIToolManifest.from_dict(item)
                for item in d.get("cli_tools", [])
                if isinstance(item, dict)
            ],
            mcp_servers=[
                EnvironmentMCPServerManifest.from_dict(item)
                for item in d.get("mcp_servers", [])
                if isinstance(item, dict)
            ],
            skills=[
                EnvironmentSkillManifest.from_dict(item)
                for item in d.get("skills", [])
                if isinstance(item, dict)
            ],
        )


# ---------------------------------------------------------------------------
# Chat proxy (interactive peer -> host agent)
# ---------------------------------------------------------------------------

__all__ = [
    "EnvironmentCLIToolManifest",
    "EnvironmentMCPServerManifest",
    "EnvironmentSkillManifest",
    "EnvironmentManifestRequest",
    "EnvironmentManifestResponse",
]

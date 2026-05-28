"""Remote relay protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from reuleauxcoder.domain.environment_requirements import (
    environment_requirement_kind_from_id,
    environment_requirement_name_from_id,
    normalize_environment_placement,
    normalize_environment_requirement_id,
    normalize_environment_requirement_kind,
)


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
class EnvironmentRequirementManifest:
    id: str
    kind: str
    name: str
    command: str = ""
    enabled: bool = True
    placement: str = "peer"
    tags: list[str] = field(default_factory=list)
    requirements: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    check: str = ""
    install: str = ""
    configure: str = ""
    version: str | None = None
    runtime: str = ""
    language: str = ""
    scope: str = ""
    path: str = ""
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
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "command": self.command,
            "enabled": self.enabled,
            "placement": self.placement,
            "tags": self.tags,
            "requirements": dict(self.requirements),
            "args": list(self.args),
            "env": dict(self.env),
            "check": self.check,
            "install": self.install,
            "configure": self.configure,
            "runtime": self.runtime,
            "language": self.language,
            "scope": self.scope,
            "path": self.path,
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
        if self.cwd is not None:
            data["cwd"] = self.cwd
        if self.version is not None:
            data["version"] = self.version
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EnvironmentRequirementManifest":
        raw_tags = d.get("tags", [])
        raw_requirements = d.get("requirements", {})
        raw_args = d.get("args", [])
        raw_env = d.get("env", {})
        raw_id = str(d.get("id") or "")
        kind = normalize_environment_requirement_kind(
            d.get("kind")
            or d.get("resource_kind")
            or environment_requirement_kind_from_id(raw_id)
        )
        name = str(d.get("name") or environment_requirement_name_from_id(raw_id)).strip()
        requirement_id = normalize_environment_requirement_id(raw_id, kind=kind, name=name)
        return cls(
            id=requirement_id,
            kind=kind,
            name=name,
            command=str(d.get("command", "")),
            enabled=_bool_value(d.get("enabled", True)),
            placement=normalize_environment_placement(d.get("placement", "peer")),
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
            args=[str(item) for item in raw_args] if isinstance(raw_args, list) else [],
            env=(
                {str(k): str(v) for k, v in raw_env.items()}
                if isinstance(raw_env, dict)
                else {}
            ),
            cwd=str(d["cwd"]) if d.get("cwd") is not None else None,
            check=str(d.get("check", "")),
            install=str(d.get("install", "")),
            configure=str(d.get("configure", "")),
            version=str(d["version"]) if d.get("version") is not None else None,
            runtime=str(d.get("runtime", "")),
            language=str(d.get("language", "")),
            scope=str(d.get("scope", "")),
            path=str(d.get("path") or d.get("path_hint") or ""),
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
class EnvironmentManifestRequest:
    peer_token: str
    os: str
    arch: str
    workspace: str = ""
    agent_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "os": self.os,
            "arch": self.arch,
            "workspace": self.workspace,
            "agent_id": self.agent_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EnvironmentManifestRequest":
        return cls(
            peer_token=d["peer_token"],
            os=str(d.get("os", "")),
            arch=str(d.get("arch", "")),
            workspace=str(d.get("workspace", "")),
            agent_id=str(d.get("agent_id") or d.get("agentId") or ""),
        )


@dataclass
class EnvironmentManifestResponse:
    environment_requirements: list[EnvironmentRequirementManifest] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment_requirements": [
                requirement.to_dict() for requirement in self.environment_requirements
            ],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EnvironmentManifestResponse":
        return cls(
            environment_requirements=[
                EnvironmentRequirementManifest.from_dict(item)
                for item in d.get("environment_requirements", [])
                if isinstance(item, dict)
            ],
        )


# ---------------------------------------------------------------------------
# Chat proxy (interactive peer -> host agent)
# ---------------------------------------------------------------------------

__all__ = [
    "EnvironmentRequirementManifest",
    "EnvironmentManifestRequest",
    "EnvironmentManifestResponse",
]

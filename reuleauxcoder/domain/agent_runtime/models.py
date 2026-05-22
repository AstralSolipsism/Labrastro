"""Domain models for configurable Agent runtime execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecutorType(str, Enum):
    """Supported Agent executor families."""

    REULEAUXCODER = "reuleauxcoder"
    FAKE = "fake"
    CODEX = "codex"
    CLAUDE = "claude"
    GEMINI = "gemini"


class ExecutionLocation(str, Enum):
    """Where an Agent task runs."""

    REMOTE_SERVER = "remote_server"
    LOCAL_WORKSPACE = "local_workspace"
    DAEMON_WORKTREE = "daemon_worktree"


class TriggerMode(str, Enum):
    """How an Agent execution was triggered."""

    INTERACTIVE_CHAT = "interactive_chat"
    ISSUE_TASK = "issue_task"
    ENVIRONMENT_CONFIG = "environment_config"


class AgentRunSource(str, Enum):
    """Product-facing source for one Agent execution record."""

    CHAT = "chat"
    DELEGATION = "delegation"
    TASKFLOW = "taskflow"
    ENVIRONMENT = "environment"
    CAPABILITY_INGEST = "capability_ingest"
    MANUAL = "manual"


class TaskStatus(str, Enum):
    """Task execution lifecycle status."""

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class ArtifactType(str, Enum):
    """Deliverable type produced by a task."""

    BRANCH = "branch"
    PULL_REQUEST = "pull_request"
    TRANSCRIPT = "transcript"
    LOG = "log"
    DIFF = "diff"
    TEST_RESULT = "test_result"
    FINAL_REPORT = "final_report"
    REPORT = "report"
    COMMENT = "comment"
    DOCUMENT = "document"
    PLAN = "plan"


class ArtifactStatus(str, Enum):
    """Lifecycle status for a task artifact."""

    NONE = "none"
    GENERATED = "generated"
    BRANCH_CREATED = "branch_created"
    PUSHED = "pushed"
    PR_CREATED = "pr_created"
    PR_REVIEWING = "pr_reviewing"
    PR_CHANGES_REQUESTED = "pr_changes_requested"
    PR_APPROVED = "pr_approved"
    MERGED = "merged"
    CLOSED = "closed"
    FAILED = "failed"


class MergeStatus(str, Enum):
    """User-facing merge gate status for pull request artifacts."""

    PENDING_USER = "pending_user"
    MERGED_BY_USER = "merged_by_user"
    CLOSED = "closed"


def _enum_value(value: Enum | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(val)
        for key, val in value.items()
        if str(key).strip() and val is not None
    }


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


CAPABILITY_COMPONENT_KINDS = {
    "builtin_tool",
    "cli",
    "cli_tool",
    "credential",
    "env",
    "mcp",
    "mcp_server",
    "mcp_tool",
    "skill",
}
AGENT_VISIBILITIES = {"user", "system", "internal"}
EXECUTION_POLICIES = {"allow", "deny", "require_user", "escalate", "inherit"}


def _choice(value: Any, allowed: set[str], fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else fallback


def _bool_value(value: Any, fallback: bool) -> bool:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _reject_plaintext_secret_container(data: dict[str, Any], *, owner: str) -> None:
    secret_keys = {"secret", "secrets", "api_key", "api_keys", "token", "tokens"}
    for key in data:
        if str(key).strip().lower() in secret_keys:
            raise ValueError(
                f"{owner} must reference secrets through credential_refs, not plaintext secrets"
            )


@dataclass
class CapabilitySourceConfig:
    """Source material used to generate a capability package."""

    type: str = "manual"
    url: str = ""
    ref: str = ""
    paths: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "CapabilitySourceConfig":
        if isinstance(value, dict):
            return cls(
                type=str(value.get("type", "manual") or "manual"),
                url=str(value.get("url", "") or ""),
                ref=str(value.get("ref", "") or ""),
                paths=_string_list(value.get("paths", [])),
                notes=str(value.get("notes", "") or ""),
            )
        if value is None:
            return cls()
        text = str(value).strip()
        if text.startswith("http://") or text.startswith("https://"):
            return cls(type="docs_url", url=text)
        return cls(type=text or "manual")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type or "manual"}
        if self.url:
            result["url"] = self.url
        if self.ref:
            result["ref"] = self.ref
        if self.paths:
            result["paths"] = list(self.paths)
        if self.notes:
            result["notes"] = self.notes
        return result


@dataclass
class CapabilityComponentConfig:
    """Shared installed component referenced by one or more capability packages."""

    id: str
    kind: str
    name: str
    description: str = ""
    enabled: bool = True
    package_ids: list[str] = field(default_factory=list)
    source: CapabilitySourceConfig = field(default_factory=CapabilitySourceConfig)
    config: dict[str, Any] = field(default_factory=dict)
    managed_by: str = "capability_package"
    status: str = "installed"
    access: str = ""
    risk_level: str = ""
    execution_policy: str = "inherit"
    registry_path: str = ""
    source_path: str = ""

    @classmethod
    def from_dict(
        cls, component_id: str, data: dict[str, Any] | None
    ) -> "CapabilityComponentConfig":
        if not isinstance(data, dict):
            data = {}
        _reject_plaintext_secret_container(data, owner="capability component")
        raw_config = _dict_value(data.get("config", {}))
        _reject_plaintext_secret_container(raw_config, owner="capability component config")
        raw_kind = str(data.get("kind", data.get("type", "")) or "").strip().lower()
        kind = raw_kind if raw_kind in CAPABILITY_COMPONENT_KINDS else ""
        name = str(data.get("name", "") or "").strip()
        if not kind or not name:
            parsed_kind, parsed_name = _split_component_id(component_id)
            kind = kind or parsed_kind
            name = name or parsed_name
        return cls(
            id=str(component_id),
            kind=kind,
            name=name,
            description=str(data.get("description", "") or ""),
            enabled=bool(data.get("enabled", True)),
            package_ids=_string_list(data.get("package_ids", [])),
            source=CapabilitySourceConfig.from_value(data.get("source", {})),
            config=raw_config,
            managed_by=str(data.get("managed_by", "capability_package") or "capability_package"),
            status=str(data.get("status", "installed") or "installed"),
            access=_choice(data.get("access"), {"read", "write", "both"}, ""),
            risk_level=str(data.get("risk") or data.get("risk_level") or "").strip().lower(),
            execution_policy=_choice(
                data.get("execution_policy"),
                EXECUTION_POLICIES,
                "inherit",
            ),
            registry_path=str(data.get("registry_path", "") or ""),
            source_path=str(data.get("source_path", "") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "enabled": self.enabled,
            "package_ids": list(self.package_ids),
            "source": self.source.to_dict(),
            "config": dict(self.config),
            "managed_by": self.managed_by,
            "status": self.status,
        }
        if self.access:
            result["access"] = self.access
        if self.risk_level:
            result["risk_level"] = self.risk_level
        if self.execution_policy and self.execution_policy != "inherit":
            result["execution_policy"] = self.execution_policy
        if self.registry_path:
            result["registry_path"] = self.registry_path
        if self.source_path:
            result["source_path"] = self.source_path
        return result


@dataclass
class CapabilityPackageDraft:
    """Agent-generated capability package proposal awaiting user confirmation."""

    id: str
    name: str = ""
    description: str = ""
    source: CapabilitySourceConfig = field(default_factory=CapabilitySourceConfig)
    components: list[dict[str, Any]] = field(default_factory=list)
    install_plan: list[str] = field(default_factory=list)
    usage: list[str] = field(default_factory=list)
    effective_capabilities: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)
    risk_level: str = ""
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, package_id: str, data: dict[str, Any] | None) -> "CapabilityPackageDraft":
        if not isinstance(data, dict):
            data = {}
        _reject_plaintext_secret_container(data, owner="capability package draft")
        raw_components = data.get("components", [])
        components = [
            dict(item)
            for item in (raw_components if isinstance(raw_components, list) else [])
            if isinstance(item, dict)
        ]
        return cls(
            id=str(data.get("id") or package_id),
            name=str(data.get("name", "") or ""),
            description=str(data.get("description", "") or ""),
            source=CapabilitySourceConfig.from_value(data.get("source", {})),
            components=components,
            install_plan=_string_list(data.get("install_plan", [])),
            usage=_string_list(data.get("usage", [])),
            effective_capabilities=_string_list(data.get("effective_capabilities", [])),
            evidence=_string_dict_list(data.get("evidence", [])),
            credentials=_string_list(data.get("credentials", [])),
            risk_level=str(data.get("risk_level", "") or ""),
            notes=_string_list(data.get("notes", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source": self.source.to_dict(),
            "components": [dict(item) for item in self.components],
            "install_plan": list(self.install_plan),
            "usage": list(self.usage),
            "effective_capabilities": list(self.effective_capabilities),
            "evidence": [dict(item) for item in self.evidence],
            "credentials": list(self.credentials),
            "risk_level": self.risk_level,
            "notes": list(self.notes),
        }


@dataclass
class CapabilityPackageConfig:
    """Confirmed capability package generated from source docs or repositories."""

    id: str
    name: str = ""
    description: str = ""
    source: CapabilitySourceConfig = field(default_factory=CapabilitySourceConfig)
    components: list[str] = field(default_factory=list)
    enabled: bool = True
    status: str = "installed"
    install_plan: list[str] = field(default_factory=list)
    usage: list[str] = field(default_factory=list)
    effective_capabilities: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)
    risk_level: str = ""
    execution_policy: str = "inherit"
    generated_by: str = "capability_packager"
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(
        cls, package_id: str, data: dict[str, Any] | None
    ) -> "CapabilityPackageConfig":
        if not isinstance(data, dict):
            data = {}
        _reject_plaintext_secret_container(data, owner="capability package")
        component_refs = data.get("components", data.get("component_refs", []))
        return cls(
            id=str(package_id),
            name=str(data.get("name", "") or ""),
            description=str(data.get("description", "") or ""),
            source=CapabilitySourceConfig.from_value(data.get("source", {})),
            components=_string_list(component_refs),
            enabled=bool(data.get("enabled", True)),
            status=str(data.get("status", "installed") or "installed"),
            install_plan=_string_list(data.get("install_plan", [])),
            usage=_string_list(data.get("usage", [])),
            effective_capabilities=_string_list(data.get("effective_capabilities", [])),
            evidence=_string_dict_list(data.get("evidence", [])),
            credentials=_string_list(data.get("credentials", [])),
            risk_level=str(data.get("risk_level", "") or ""),
            execution_policy=_choice(
                data.get("execution_policy"),
                EXECUTION_POLICIES,
                "inherit",
            ),
            generated_by=str(data.get("generated_by", "capability_packager") or "capability_packager"),
            notes=_string_list(data.get("notes", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": self.enabled,
            "status": self.status,
            "source": self.source.to_dict(),
            "components": list(self.components),
            "generated_by": self.generated_by,
        }
        if self.name:
            result["name"] = self.name
        if self.description:
            result["description"] = self.description
        if self.install_plan:
            result["install_plan"] = list(self.install_plan)
        if self.usage:
            result["usage"] = list(self.usage)
        if self.effective_capabilities:
            result["effective_capabilities"] = list(self.effective_capabilities)
        if self.evidence:
            result["evidence"] = [dict(item) for item in self.evidence]
        if self.credentials:
            result["credentials"] = list(self.credentials)
        if self.risk_level:
            result["risk_level"] = self.risk_level
        if self.execution_policy and self.execution_policy != "inherit":
            result["execution_policy"] = self.execution_policy
        if self.notes:
            result["notes"] = list(self.notes)
        return result


def resolve_capability_refs(
    capability_refs: list[str],
    packages: dict[str, CapabilityPackageConfig],
    components: dict[str, CapabilityComponentConfig] | None = None,
) -> dict[str, Any]:
    """Resolve package refs into executor-facing package and component overlay."""

    resolved_packages: list[dict[str, Any]] = []
    resolved_components: list[dict[str, Any]] = []
    mcp_servers: list[str] = []
    mcp_tools: list[str] = []
    skills: list[str] = []
    cli_tools: list[str] = []
    tools: list[str] = []
    credentials: list[str] = []
    execution_policies: list[dict[str, Any]] = []
    capability_summaries: list[str] = []
    overlay_mcp_servers: dict[str, Any] = {}
    overlay_env: dict[str, str] = {}
    overlay_skill_roots: list[str] = []
    component_map = components or {}
    for package_id in capability_refs:
        package = packages.get(package_id)
        if package is None or not package.enabled:
            continue
        package_dict = package.to_dict()
        package_dict["id"] = package.id
        resolved_packages.append(package_dict)
        capability_summaries.extend(package.effective_capabilities)
        credentials.extend(package.credentials)
        if package.execution_policy and package.execution_policy != "inherit":
            execution_policies.append(
                {
                    "target": package.id,
                    "target_type": "capability_package",
                    "policy": package.execution_policy,
                    "risk_level": package.risk_level,
                }
            )
        for component_id in package.components:
            component = component_map.get(component_id)
            if component is None or not component.enabled:
                continue
            component_dict = component.to_dict()
            component_dict["id"] = component.id
            resolved_components.append(component_dict)
            execution_policies.append(
                _component_execution_policy(component, package=package)
            )
            if component.kind in {"mcp", "mcp_server"}:
                mcp_servers.append(component.name)
                tools.append(component.registry_path or f"mcp:{component.name}")
                overlay_mcp_servers[component.name] = _mcp_overlay_config(component)
            elif component.kind == "mcp_tool":
                mcp_tools.append(component.name)
                tools.append(component.registry_path or f"mcp_tool:{component.name}")
            elif component.kind == "skill":
                skills.append(component.name)
                path_hint = str(component.config.get("path_hint") or "").strip()
                if path_hint:
                    overlay_skill_roots.append(path_hint)
            elif component.kind in {"cli", "cli_tool"}:
                cli_tools.append(component.name)
                tools.append(component.registry_path or f"cli:{component.name}")
                for key, value in _dict_value(component.config.get("env", {})).items():
                    overlay_env[str(key)] = str(value)
            elif component.kind == "builtin_tool":
                tools.append(component.registry_path or f"builtin:{component.name}")
            elif component.kind == "env":
                value = str(component.config.get("value") or "").strip()
                if component.name and value:
                    overlay_env[component.name] = value
            elif component.kind == "credential":
                credentials.append(component.name)
    effective_capabilities = {
        "tools": _dedupe_strings(tools),
        "mcp_servers": _dedupe_strings(mcp_servers),
        "mcp_tools": _dedupe_strings(mcp_tools),
        "skills": _dedupe_strings(skills),
        "cli_tools": _dedupe_strings(cli_tools),
        "env": dict(overlay_env),
        "credentials": _dedupe_strings(credentials),
        "summaries": _dedupe_strings(capability_summaries),
        "execution_policies": _dedupe_policy_records(execution_policies),
    }
    return {
        "packages": resolved_packages,
        "components": _dedupe_components(resolved_components),
        "tools": effective_capabilities["tools"],
        "mcp_servers": _dedupe_strings(mcp_servers),
        "mcp_tools": effective_capabilities["mcp_tools"],
        "skills": _dedupe_strings(skills),
        "cli_tools": _dedupe_strings(cli_tools),
        "credentials": effective_capabilities["credentials"],
        "execution_policies": effective_capabilities["execution_policies"],
        "effective_capabilities": effective_capabilities,
        "capability_overlay": {
            "component_ids": _dedupe_strings(
                [str(item.get("id") or "") for item in resolved_components]
            ),
            "mcp": {"servers": overlay_mcp_servers},
            "skill_roots": _dedupe_strings(overlay_skill_roots),
            "env": overlay_env,
            "cli_tools": [
                item
                for item in _dedupe_components(resolved_components)
                if item.get("kind") == "cli"
            ],
        },
    }


def _split_component_id(component_id: str) -> tuple[str, str]:
    kind, sep, name = str(component_id).partition(":")
    if sep and kind in CAPABILITY_COMPONENT_KINDS:
        return kind, name
    return "", str(component_id)


def _string_dict_list(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        mapped = {
            str(key): str(val)
            for key, val in item.items()
            if str(key).strip() and val is not None
        }
        if mapped:
            result.append(mapped)
    return result


def _dedupe_components(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        component_id = str(value.get("id") or "").strip()
        if not component_id or component_id in seen:
            continue
        seen.add(component_id)
        result.append(value)
    return result


def _component_execution_policy(
    component: CapabilityComponentConfig,
    *,
    package: CapabilityPackageConfig,
) -> dict[str, Any]:
    policy = component.execution_policy or package.execution_policy or "inherit"
    if policy == "inherit" and package.execution_policy != "inherit":
        policy = package.execution_policy
    return {
        "target": component.id,
        "target_type": "capability_component",
        "kind": component.kind,
        "policy": policy,
        "risk_level": component.risk_level or package.risk_level,
        "access": component.access,
    }


def _dedupe_policy_records(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        target = str(value.get("target") or "").strip()
        target_type = str(value.get("target_type") or "").strip()
        if not target:
            continue
        key = f"{target_type}:{target}"
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                key: val
                for key, val in value.items()
                if val not in ("", None, [], {})
            }
        )
    return result


def _mcp_overlay_config(component: CapabilityComponentConfig) -> dict[str, Any]:
    config = dict(component.config)
    result: dict[str, Any] = {}
    command = str(config.get("command") or "").strip()
    if command:
        result["command"] = command
    args = _string_list(config.get("args", []))
    if args:
        result["args"] = args
    env = _dict_value(config.get("env", {}))
    if env:
        result["env"] = {str(key): str(value) for key, value in env.items()}
    cwd = str(config.get("cwd") or "").strip()
    if cwd:
        result["cwd"] = cwd
    return result


@dataclass
class AgentDispatchConfig:
    """Open-ended user-authored dispatch profile for long-lived Agents."""

    profile: str = ""
    examples: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AgentDispatchConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(
            profile=str(data.get("profile", "") or ""),
            examples=_string_list(data.get("examples", [])),
            avoid=_string_list(data.get("avoid", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.profile:
            result["profile"] = self.profile
        if self.examples:
            result["examples"] = list(self.examples)
        if self.avoid:
            result["avoid"] = list(self.avoid)
        return result


@dataclass
class AgentPromptConfig:
    """Prompt references and append-only instructions for an Agent."""

    agent_md: str | None = None
    system_append: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AgentPromptConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(
            agent_md=str(data["agent_md"]) if data.get("agent_md") is not None else None,
            system_append=str(data.get("system_append", "") or ""),
        )

    def to_dict(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.agent_md:
            result["agent_md"] = self.agent_md
        if self.system_append:
            result["system_append"] = self.system_append
        return result


@dataclass
class AgentModelConfig:
    """Default model binding for an Agent profile."""

    provider: str = ""
    model: str = ""
    display_name: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AgentModelConfig":
        if not isinstance(data, dict):
            return cls()
        parameters = data.get("parameters", {})
        return cls(
            provider=str(
                data.get("provider")
                or data.get("provider_id")
                or data.get("providerId")
                or ""
            ),
            model=str(
                data.get("model")
                or data.get("model_id")
                or data.get("modelId")
                or ""
            ),
            display_name=str(
                data.get("display_name") or data.get("displayName") or ""
            ),
            parameters=dict(parameters) if isinstance(parameters, dict) else {},
        )

    @property
    def configured(self) -> bool:
        return bool(self.provider and self.model)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.provider:
            result["provider"] = self.provider
        if self.model:
            result["model"] = self.model
        if self.display_name:
            result["display_name"] = self.display_name
        if self.parameters:
            result["parameters"] = dict(self.parameters)
        return result


@dataclass
class RuntimeProfileConfig:
    """Runtime profile describing how to launch an Agent executor."""

    id: str
    executor: ExecutorType = ExecutorType.REULEAUXCODER
    execution_location: ExecutionLocation = ExecutionLocation.REMOTE_SERVER
    model: str = ""
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    runtime_home_policy: str = ""
    approval_mode: str = ""
    config_isolation: str = ""
    credential_refs: dict[str, str] = field(default_factory=dict)
    mcp: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls, profile_id: str, data: dict[str, Any] | None
    ) -> "RuntimeProfileConfig":
        if not isinstance(data, dict):
            data = {}
        _reject_plaintext_secret_container(data, owner="runtime profile")
        return cls(
            id=str(profile_id),
            executor=ExecutorType(str(data.get("executor", "reuleauxcoder"))),
            execution_location=ExecutionLocation(
                str(data.get("execution_location", "remote_server"))
            ),
            model=str(data.get("model", "") or ""),
            command=str(data["command"]) if data.get("command") is not None else None,
            args=_string_list(data.get("args", [])),
            env=_string_dict(data.get("env", {})),
            runtime_home_policy=str(data.get("runtime_home_policy", "") or ""),
            approval_mode=str(data.get("approval_mode", "") or ""),
            config_isolation=str(data.get("config_isolation", "") or ""),
            credential_refs=_string_dict(data.get("credential_refs", {})),
            mcp=_dict_value(data.get("mcp", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "executor": self.executor.value,
            "execution_location": self.execution_location.value,
        }
        if self.command is not None:
            result["command"] = self.command
        if self.model:
            result["model"] = self.model
        if self.args:
            result["args"] = list(self.args)
        if self.env:
            result["env"] = dict(self.env)
        if self.runtime_home_policy:
            result["runtime_home_policy"] = self.runtime_home_policy
        if self.approval_mode:
            result["approval_mode"] = self.approval_mode
        if self.config_isolation:
            result["config_isolation"] = self.config_isolation
        if self.credential_refs:
            result["credential_refs"] = dict(self.credential_refs)
        if self.mcp:
            result["mcp"] = dict(self.mcp)
        return result


@dataclass
class AgentConfig:
    """Server-authoritative Agent configuration."""

    id: str
    name: str = ""
    description: str = ""
    role: str = ""
    entrypoint: bool = False
    visibility: str = "user"
    chat_entrypoint: bool = False
    delegable: bool = True
    taskflow_eligible: bool = True
    system_flow_only: list[str] = field(default_factory=list)
    runtime_profile: str = ""
    dispatch: AgentDispatchConfig = field(default_factory=AgentDispatchConfig)
    capability_refs: list[str] = field(default_factory=list)
    model: AgentModelConfig = field(default_factory=AgentModelConfig)
    prompt: AgentPromptConfig = field(default_factory=AgentPromptConfig)
    max_concurrent_tasks: int | None = None
    credential_refs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, agent_id: str, data: dict[str, Any] | None) -> "AgentConfig":
        if not isinstance(data, dict):
            data = {}
        _reject_plaintext_secret_container(data, owner="agent config")
        removed_fields = [
            key
            for key in ("capabilities", "mcp", "skills", "dispatch_tags")
            if key in data
        ]
        if removed_fields:
            raise ValueError(
                "agent config fields "
                + ", ".join(sorted(removed_fields))
                + " were removed; use dispatch.profile/examples/avoid for "
                + "Agent routing profile and capability_refs for capability packages"
            )
        raw_max = data.get("max_concurrent_tasks")
        max_concurrent_tasks = int(raw_max) if raw_max is not None else None
        visibility = _choice(data.get("visibility"), AGENT_VISIBILITIES, "user")
        user_visible = visibility == "user"
        entrypoint = bool(data.get("entrypoint", False))
        chat_entrypoint = _bool_value(
            data.get("chat_entrypoint", data.get("entrypoint")),
            entrypoint,
        )
        return cls(
            id=str(agent_id),
            name=str(data.get("name", "") or ""),
            description=str(data.get("description", "") or ""),
            role=str(data.get("role", "") or ""),
            entrypoint=entrypoint,
            visibility=visibility,
            chat_entrypoint=chat_entrypoint,
            delegable=_bool_value(data.get("delegable"), user_visible),
            taskflow_eligible=_bool_value(data.get("taskflow_eligible"), user_visible),
            system_flow_only=_string_list(data.get("system_flow_only", [])),
            runtime_profile=str(data.get("runtime_profile", "") or ""),
            dispatch=AgentDispatchConfig.from_dict(data.get("dispatch")),
            capability_refs=_string_list(data.get("capability_refs", [])),
            model=AgentModelConfig.from_dict(data.get("model")),
            prompt=AgentPromptConfig.from_dict(data.get("prompt")),
            max_concurrent_tasks=max_concurrent_tasks,
            credential_refs=_string_dict(data.get("credential_refs", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.name:
            result["name"] = self.name
        if self.description:
            result["description"] = self.description
        if self.role:
            result["role"] = self.role
        if self.visibility != "user":
            result["visibility"] = self.visibility
        if self.entrypoint:
            result["entrypoint"] = self.entrypoint
        if self.chat_entrypoint:
            result["chat_entrypoint"] = self.chat_entrypoint
        if self.delegable != (self.visibility == "user"):
            result["delegable"] = self.delegable
        if self.taskflow_eligible != (self.visibility == "user"):
            result["taskflow_eligible"] = self.taskflow_eligible
        if self.system_flow_only:
            result["system_flow_only"] = list(self.system_flow_only)
        if self.runtime_profile:
            result["runtime_profile"] = self.runtime_profile
        dispatch = self.dispatch.to_dict()
        if dispatch:
            result["dispatch"] = dispatch
        if self.capability_refs:
            result["capability_refs"] = list(self.capability_refs)
        model = self.model.to_dict()
        if model:
            result["model"] = model
        prompt = self.prompt.to_dict()
        if prompt:
            result["prompt"] = prompt
        if self.max_concurrent_tasks is not None:
            result["max_concurrent_tasks"] = self.max_concurrent_tasks
        if self.credential_refs:
            result["credential_refs"] = dict(self.credential_refs)
        return result

    @property
    def user_visible(self) -> bool:
        return self.visibility == "user"

    @property
    def can_delegate(self) -> bool:
        return self.user_visible and self.delegable

    @property
    def can_run_taskflow(self) -> bool:
        return self.user_visible and self.taskflow_eligible

    def allows_system_flow(self, flow: str) -> bool:
        return str(flow).strip() in set(self.system_flow_only)


@dataclass
class AgentRunRecord:
    """One execution attempt by an Agent.

    Chat, delegation, TaskFlow, environment, and manual execution all converge
    here so every Agent execution has the same durable shape.
    """

    id: str
    issue_id: str
    agent_id: str
    source: AgentRunSource = AgentRunSource.MANUAL
    trigger_mode: TriggerMode = TriggerMode.ISSUE_TASK
    status: TaskStatus = TaskStatus.QUEUED
    prompt: str = ""
    runtime_profile_id: str | None = None
    executor: ExecutorType | None = None
    execution_location: ExecutionLocation | None = None
    output: str | None = None
    parent_task_id: str | None = None
    trigger_comment_id: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    worker_id: str | None = None
    executor_session_id: str | None = None
    workdir: str | None = None
    sandbox_id: str | None = None
    sandbox_session_id: str | None = None
    workspace_ref: str | None = None
    delegated_by_run_id: str | None = None
    parent_run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source = AgentRunSource(_enum_value(self.source) or AgentRunSource.MANUAL)
        self.trigger_mode = TriggerMode(_enum_value(self.trigger_mode))
        self.status = TaskStatus(_enum_value(self.status))
        if self.executor is not None:
            self.executor = ExecutorType(_enum_value(self.executor))
        if self.execution_location is not None:
            self.execution_location = ExecutionLocation(
                _enum_value(self.execution_location)
            )

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
        }

@dataclass
class TaskArtifact:
    """Artifact produced by a task."""

    id: str
    task_id: str
    type: ArtifactType
    status: ArtifactStatus = ArtifactStatus.NONE
    branch_name: str | None = None
    pr_url: str | None = None
    content: str | None = None
    path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    merge_status: MergeStatus | None = None
    merged_by: str | None = None

    def __post_init__(self) -> None:
        self.type = ArtifactType(_enum_value(self.type))
        self.status = ArtifactStatus(_enum_value(self.status))
        if self.merge_status is not None:
            self.merge_status = MergeStatus(_enum_value(self.merge_status))
        elif self.type == ArtifactType.PULL_REQUEST:
            self.merge_status = MergeStatus.PENDING_USER

    @property
    def requires_user_merge(self) -> bool:
        return (
            self.type == ArtifactType.PULL_REQUEST
            and self.status not in {ArtifactStatus.MERGED, ArtifactStatus.CLOSED}
            and self.merge_status == MergeStatus.PENDING_USER
        )


@dataclass
class TaskSessionRef:
    """Opaque executor session reference bound to a task."""

    agent_id: str
    executor: ExecutorType
    execution_location: ExecutionLocation
    issue_id: str
    task_id: str
    workdir: str | None = None
    branch: str | None = None
    executor_session_id: str | None = None

    def __post_init__(self) -> None:
        self.executor = ExecutorType(_enum_value(self.executor))
        self.execution_location = ExecutionLocation(_enum_value(self.execution_location))

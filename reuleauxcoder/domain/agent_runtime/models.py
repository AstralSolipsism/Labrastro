"""Domain models for configurable Agent runtime execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from reuleauxcoder.domain.capability_packages import (
    CapabilityCredentialBinding,
    CapabilityCredentialRequirement,
    capability_package_is_active,
    capability_package_state_projection,
)
from reuleauxcoder.domain.environment_requirements import (
    normalize_environment_requirement_id,
    resolve_environment_requirement_kind,
)
from reuleauxcoder.domain.runtime_footprint import (
    aggregate_runtime_footprint,
    normalize_runtime_footprint,
    runtime_footprint_for_component,
)


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


class WorkerKind(str, Enum):
    """Runtime worker identity allowed to claim an AgentRun."""

    LOCAL_PEER = "local_peer"
    SERVER_WORKER = "server_worker"
    SANDBOX_WORKER = "sandbox_worker"


class ModelRequestOrigin(str, Enum):
    """Where LLM provider requests originate for a resolved AgentRun."""

    SERVER = "server"
    SERVER_WORKER_CLI = "server_worker_cli"
    LOCAL_CLI = "local_cli"


class WorktreeRole(str, Enum):
    """How an AgentRun is allowed to treat a prepared worktree."""

    SOURCE = "source"
    TARGET = "target"


class PublishPolicy(str, Enum):
    """Post-run publication behavior for a worktree-backed AgentRun."""

    NEVER = "never"
    BRANCH = "branch"
    PR = "pr"


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


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _positive_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


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
    "credential",
    "environment_requirement",
    "mcp",
    "mcp_server",
    "mcp_tool",
    "prompt_fragment",
    "skill",
}
AGENT_VISIBILITIES = {"user", "system", "internal"}
EXECUTION_POLICIES = {"allow", "deny", "require_user", "escalate", "inherit"}
AGENT_CALLABLE_SCOPES = {"ephemeral", "persistent"}
AGENT_RUN_METADATA_ACTIVATION_KEYS = {
    "current_activation_id",
    "current_activation_seq",
    "current_activation_input_kind",
    "current_activation_input_payload",
    "current_activation_prompt",
    "previous_activation_id",
}
AGENT_RUN_METADATA_FORBIDDEN_KEYS = {
    "agent_run_source",
    "assignment_id",
    "branch_name",
    "call_agent_mode",
    "called_by_agent_run_id",
    "comment_id",
    "created_by_activation_id",
    "dispatch_decision_id",
    "issue_id",
    "lifecycle_event",
    "lifecycle_handler_type",
    "lifecycle_hook_id",
    "lifecycle_hook_source",
    "mention_id",
    "parent_session_id",
    "parent_task_id",
    "parent_turn_id",
    "pr_id",
    "pr_url",
    "purpose_key",
    "relation_type",
    "task_run_id",
    "trigger_source",
    "trigger_comment_id",
    "workflow_id",
    "workflow_node_id",
} | AGENT_RUN_METADATA_ACTIVATION_KEYS


def _string_choices(
    value: Any,
    allowed: set[str],
    field_name: str,
    default: list[str] | None = None,
) -> list[str]:
    if value is None:
        return list(default or [])
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError(f"{field_name} must be a string or list of strings")

    result: list[str] = []
    for raw_item in values:
        item = str(raw_item).strip().lower()
        if not item:
            continue
        if item not in allowed:
            allowed_values = ", ".join(sorted(allowed))
            raise ValueError(
                f"{field_name} contains unsupported value {item}; "
                f"allowed values: {allowed_values}"
            )
        if item not in result:
            result.append(item)
    return result


def _choice(value: Any, allowed: set[str], fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else fallback


def _runtime_worker_kind(
    value: Any,
    *,
    execution_location: ExecutionLocation,
    sandbox: dict[str, Any] | None = None,
) -> WorkerKind:
    text = str(value or "").strip()
    if text:
        return WorkerKind(text)
    if sandbox:
        return WorkerKind.SANDBOX_WORKER
    if execution_location == ExecutionLocation.LOCAL_WORKSPACE:
        return WorkerKind.LOCAL_PEER
    return WorkerKind.SERVER_WORKER


def _runtime_model_request_origin(
    value: Any,
    *,
    executor: ExecutorType,
    worker_kind: WorkerKind,
) -> ModelRequestOrigin:
    text = str(value or "").strip()
    if text:
        return ModelRequestOrigin(text)
    if executor in {ExecutorType.CODEX, ExecutorType.CLAUDE, ExecutorType.GEMINI}:
        if worker_kind == WorkerKind.LOCAL_PEER:
            return ModelRequestOrigin.LOCAL_CLI
        return ModelRequestOrigin.SERVER_WORKER_CLI
    return ModelRequestOrigin.SERVER


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
    display_name: str = ""
    summary: str = ""
    runtime_footprint: dict[str, Any] = field(default_factory=dict)
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
    hooks: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.runtime_footprint = runtime_footprint_for_component(self)

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
            display_name=str(data.get("display_name", "") or ""),
            summary=str(data.get("summary", "") or ""),
            runtime_footprint=(
                normalize_runtime_footprint(data.get("runtime_footprint"))
                if isinstance(data.get("runtime_footprint"), dict)
                else raw_config.get("runtime_footprint", {})
            ),
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
            hooks=_dict_list(data.get("hooks", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "display_name": self.display_name,
            "summary": self.summary,
            "runtime_footprint": dict(self.runtime_footprint),
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
        if self.hooks:
            result["hooks"] = [dict(item) for item in self.hooks]
        result["state"] = capability_package_state_projection(result)
        return result


@dataclass
class CapabilityPackageDraft:
    """Agent-generated capability package proposal awaiting user confirmation."""

    id: str
    name: str = ""
    description: str = ""
    source: CapabilitySourceConfig = field(default_factory=CapabilitySourceConfig)
    components: list[dict[str, Any]] = field(default_factory=list)
    contributions: dict[str, Any] = field(default_factory=dict)
    install_plan: list[str] = field(default_factory=list)
    usage: list[str] = field(default_factory=list)
    effective_capabilities: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)
    credential_requirements: list[dict[str, Any]] = field(default_factory=list)
    credential_bindings: list[dict[str, Any]] = field(default_factory=list)
    optional_features: list[dict[str, Any]] = field(default_factory=list)
    risk_level: str = ""
    notes: list[str] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, package_id: str, data: dict[str, Any] | None) -> "CapabilityPackageDraft":
        if not isinstance(data, dict):
            data = {}
        _reject_plaintext_secret_container(data, owner="capability package draft")
        contributions = _normalize_contributions(data.get("contributions", {}))
        components = _components_from_manifest(data, contributions)
        return cls(
            id=str(data.get("id") or package_id),
            name=str(data.get("name", "") or ""),
            description=str(data.get("description", "") or ""),
            source=CapabilitySourceConfig.from_value(data.get("source", {})),
            components=components,
            contributions=contributions or _contributions_from_components(components),
            install_plan=_string_list(data.get("install_plan", [])),
            usage=_string_list(data.get("usage", [])),
            effective_capabilities=_string_list(data.get("effective_capabilities", [])),
            evidence=_string_dict_list(data.get("evidence", [])),
            credentials=_string_list(data.get("credentials", [])),
            credential_requirements=[
                item.to_dict()
                for item in (
                    CapabilityCredentialRequirement.from_dict(raw_item)
                    for raw_item in _dict_list(data.get("credential_requirements", []))
                )
            ],
            credential_bindings=[
                item.to_dict()
                for item in (
                    CapabilityCredentialBinding.from_dict(raw_item)
                    for raw_item in _dict_list(data.get("credential_bindings", []))
                )
            ],
            optional_features=_dict_list(data.get("optional_features", [])),
            risk_level=str(data.get("risk_level", "") or ""),
            notes=_string_list(data.get("notes", [])),
            hooks=_dict_list(data.get("hooks", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source": self.source.to_dict(),
            "contributions": dict(
                self.contributions or _contributions_from_components(self.components)
            ),
            "install_plan": list(self.install_plan),
            "usage": list(self.usage),
            "effective_capabilities": list(self.effective_capabilities),
            "evidence": [dict(item) for item in self.evidence],
            "credentials": list(self.credentials),
            "credential_requirements": [
                dict(item) for item in self.credential_requirements
            ],
            "credential_bindings": [
                dict(item) for item in self.credential_bindings
            ],
            "optional_features": [dict(item) for item in self.optional_features],
            "risk_level": self.risk_level,
            "notes": list(self.notes),
            "hooks": [dict(item) for item in self.hooks],
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
    credential_requirements: list[dict[str, Any]] = field(default_factory=list)
    credential_bindings: list[dict[str, Any]] = field(default_factory=list)
    risk_level: str = ""
    execution_policy: str = "inherit"
    generated_by: str = "capability_packager"
    notes: list[str] = field(default_factory=list)
    runtime_footprint: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    source_snapshot: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    rollback: dict[str, Any] = field(default_factory=dict)
    upstream_version: str = ""
    last_update: dict[str, Any] = field(default_factory=dict)
    hooks: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.runtime_footprint = normalize_runtime_footprint(
            self.runtime_footprint,
            default_runs_on=(
                str(self.runtime_footprint.get("runs_on"))
                if isinstance(self.runtime_footprint, dict) and self.runtime_footprint.get("runs_on")
                else "agent_only"
            ),
        )

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
            credential_requirements=[
                item.to_dict()
                for item in (
                    CapabilityCredentialRequirement.from_dict(raw_item)
                    for raw_item in _dict_list(data.get("credential_requirements", []))
                )
            ],
            credential_bindings=[
                item.to_dict()
                for item in (
                    CapabilityCredentialBinding.from_dict(raw_item)
                    for raw_item in _dict_list(data.get("credential_bindings", []))
                )
            ],
            risk_level=str(data.get("risk_level", "") or ""),
            execution_policy=_choice(
                data.get("execution_policy"),
                EXECUTION_POLICIES,
                "inherit",
            ),
            generated_by=str(data.get("generated_by", "capability_packager") or "capability_packager"),
            notes=_string_list(data.get("notes", [])),
            runtime_footprint=normalize_runtime_footprint(
                data.get("runtime_footprint", {}),
            ),
            state=dict(data.get("state") or {}) if isinstance(data.get("state"), dict) else {},
            source_snapshot=dict(data.get("source_snapshot") or {})
            if isinstance(data.get("source_snapshot"), dict)
            else {},
            manifest=dict(data.get("manifest") or {})
            if isinstance(data.get("manifest"), dict)
            else {},
            rollback=dict(data.get("rollback") or {})
            if isinstance(data.get("rollback"), dict)
            else {},
            upstream_version=str(data.get("upstream_version") or ""),
            last_update=dict(data.get("last_update") or {})
            if isinstance(data.get("last_update"), dict)
            else {},
            hooks=_dict_list(data.get("hooks", [])),
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
        if self.credential_requirements:
            result["credential_requirements"] = [
                dict(item) for item in self.credential_requirements
            ]
        if self.credential_bindings:
            result["credential_bindings"] = [
                dict(item) for item in self.credential_bindings
            ]
        if self.risk_level:
            result["risk_level"] = self.risk_level
        if self.execution_policy and self.execution_policy != "inherit":
            result["execution_policy"] = self.execution_policy
        if self.notes:
            result["notes"] = list(self.notes)
        result["runtime_footprint"] = dict(self.runtime_footprint)
        if self.state:
            result["state"] = dict(self.state)
        if self.source_snapshot:
            result["source_snapshot"] = dict(self.source_snapshot)
        if self.manifest:
            result["manifest"] = dict(self.manifest)
        if self.rollback:
            result["rollback"] = dict(self.rollback)
        if self.upstream_version:
            result["upstream_version"] = self.upstream_version
        if self.last_update:
            result["last_update"] = dict(self.last_update)
        if self.hooks:
            result["hooks"] = [dict(item) for item in self.hooks]
        result["state"] = capability_package_state_projection(result)
        return result


@dataclass
class CapabilityContribution:
    """Normalized contribution exposed by an installed capability package."""

    id: str
    kind: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_component(
        cls, component: CapabilityComponentConfig
    ) -> "CapabilityContribution":
        return cls(
            id=component.id,
            kind=component.kind,
            name=component.name,
            config=dict(component.config),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "config": dict(self.config),
        }


@dataclass
class ResolvedCapabilitySet:
    """Executor-facing resolved capability package view."""

    packages: list[dict[str, Any]] = field(default_factory=list)
    contributions: list[dict[str, Any]] = field(default_factory=list)
    environment_requirements: list[dict[str, Any]] = field(default_factory=list)
    skill_roots: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    builtin_tool_grants: list[str] = field(default_factory=list)
    tool_specs: list[dict[str, Any]] = field(default_factory=list)
    credential_refs: list[str] = field(default_factory=list)
    prompt_fragments: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "packages": [dict(item) for item in self.packages],
            "contributions": [dict(item) for item in self.contributions],
            "environment_requirements": [
                dict(item) for item in self.environment_requirements
            ],
            "skill_roots": list(self.skill_roots),
            "mcp_servers": list(self.mcp_servers),
            "builtin_tool_grants": list(self.builtin_tool_grants),
            "tool_specs": [dict(item) for item in self.tool_specs],
            "credential_refs": list(self.credential_refs),
            "prompt_fragments": [dict(item) for item in self.prompt_fragments],
        }


def resolve_capability_refs(
    capability_refs: list[str],
    packages: dict[str, CapabilityPackageConfig],
    components: dict[str, CapabilityComponentConfig] | None = None,
) -> dict[str, Any]:
    """Resolve package refs into executor-facing package and component overlay."""

    resolved_packages: list[dict[str, Any]] = []
    resolved_components: list[dict[str, Any]] = []
    resolved_contributions: list[dict[str, Any]] = []
    mcp_servers: list[str] = []
    mcp_tools: list[str] = []
    skills: list[str] = []
    credentials: list[str] = []
    builtin_tool_grants: list[str] = []
    tool_specs: list[dict[str, Any]] = []
    prompt_fragments: list[dict[str, Any]] = []
    environment_requirements: list[dict[str, Any]] = []
    execution_policies: list[dict[str, Any]] = []
    capability_summaries: list[str] = []
    overlay_mcp_servers: dict[str, Any] = {}
    overlay_env: dict[str, str] = {}
    overlay_skill_roots: list[str] = []
    component_map = components or {}
    for package_id in capability_refs:
        package = packages.get(package_id)
        if package is None or not capability_package_is_active(package.to_dict()):
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
            resolved_contributions.append(
                CapabilityContribution.from_component(component).to_dict()
            )
            execution_policies.append(
                _component_execution_policy(component, package=package)
            )
            if component.kind in {"mcp", "mcp_server"}:
                mcp_servers.append(component.name)
                overlay_mcp_servers[component.name] = _mcp_overlay_config(component)
            elif component.kind == "mcp_tool":
                target_tool_ref = str(component.registry_path or "").strip()
                if not _is_server_scoped_mcp_tool_ref(target_tool_ref):
                    raise ValueError(
                        f"mcp_tool component '{component.id}' must define registry_path as mcp:<server>:<tool>"
                    )
                mcp_tools.append(target_tool_ref)
                tool_specs.append(
                    _capability_tool_spec(
                        package=package,
                        component=component,
                        source_type="mcp_tool",
                        target_tool_ref=target_tool_ref,
                    )
                )
            elif component.kind == "skill":
                skills.append(component.name)
                path_hint = str(component.config.get("path_hint") or "").strip()
                if path_hint:
                    overlay_skill_roots.append(path_hint)
            elif component.kind == "builtin_tool":
                builtin_tool_grants.append(component.name)
                target_tool_ref = component.registry_path or f"builtin:{component.name}"
                tool_specs.append(
                    _capability_tool_spec(
                        package=package,
                        component=component,
                        source_type="builtin_tool",
                        target_tool_ref=target_tool_ref,
                    )
                )
            elif component.kind == "credential":
                credentials.append(component.name)
            elif component.kind == "prompt_fragment":
                prompt_fragments.append(_prompt_fragment_from_component(component))
            elif component.kind == "environment_requirement":
                requirement = _environment_requirement_from_component(component)
                environment_requirements.append(requirement)
                for key, value in _dict_value(requirement.get("env", {})).items():
                    overlay_env[str(key)] = str(value)
                if requirement.get("kind") == "env_var":
                    value = str(requirement.get("value") or "").strip()
                    if component.name and value:
                        overlay_env[component.name] = value
    effective_capabilities = {
        "mcp_servers": _dedupe_strings(mcp_servers),
        "mcp_tools": _dedupe_strings(mcp_tools),
        "skills": _dedupe_strings(skills),
        "builtin_tool_grants": _dedupe_strings(builtin_tool_grants),
        "tool_specs": _dedupe_tool_specs(tool_specs),
        "environment_requirements": _dedupe_requirements(environment_requirements),
        "prompt_fragments": _dedupe_components(prompt_fragments),
        "env": dict(overlay_env),
        "credentials": _dedupe_strings(credentials),
        "summaries": _dedupe_strings(capability_summaries),
        "execution_policies": _dedupe_policy_records(execution_policies),
    }
    return {
        "packages": resolved_packages,
        "components": _dedupe_components(resolved_components),
        "contributions": _dedupe_components(resolved_contributions),
        "mcp_servers": _dedupe_strings(mcp_servers),
        "mcp_tools": effective_capabilities["mcp_tools"],
        "skills": _dedupe_strings(skills),
        "skill_roots": _dedupe_strings(overlay_skill_roots),
        "builtin_tool_grants": effective_capabilities["builtin_tool_grants"],
        "tool_specs": effective_capabilities["tool_specs"],
        "environment_requirements": effective_capabilities["environment_requirements"],
        "prompt_fragments": effective_capabilities["prompt_fragments"],
        "credentials": effective_capabilities["credentials"],
        "credential_refs": effective_capabilities["credentials"],
        "execution_policies": effective_capabilities["execution_policies"],
        "effective_capabilities": effective_capabilities,
        "capability_overlay": {
            "component_ids": _dedupe_strings(
                [str(item.get("id") or "") for item in resolved_components]
            ),
            "mcp": {"servers": overlay_mcp_servers},
            "skill_roots": _dedupe_strings(overlay_skill_roots),
            "env": overlay_env,
            "environment_requirements": effective_capabilities["environment_requirements"],
            "tool_specs": effective_capabilities["tool_specs"],
        },
    }


def _split_component_id(component_id: str) -> tuple[str, str]:
    kind, sep, name = str(component_id).partition(":")
    if sep and kind == "envreq":
        _, _, requirement_name = name.partition(":")
        return "environment_requirement", requirement_name or name
    if sep and kind in CAPABILITY_COMPONENT_KINDS:
        return kind, name
    return "", str(component_id)


def _is_server_scoped_mcp_tool_ref(value: str) -> bool:
    parts = str(value or "").strip().split(":", 2)
    return len(parts) == 3 and parts[0] == "mcp" and bool(parts[1]) and bool(parts[2])


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


_CONTRIBUTION_KIND_BY_SECTION = {
    "skills": "skill",
    "mcp_servers": "mcp_server",
    "mcp_tools": "mcp_tool",
    "builtin_tools": "builtin_tool",
    "prompt_fragments": "prompt_fragment",
    "credential_refs": "credential",
    "environment_requirements": "environment_requirement",
}


def _normalize_contributions(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in _CONTRIBUTION_KIND_BY_SECTION:
        raw_items = value.get(key, [])
        if not isinstance(raw_items, list):
            continue
        items = [dict(item) for item in raw_items if isinstance(item, dict)]
        if items:
            result[key] = items
    return result


def _components_from_manifest(
    data: dict[str, Any],
    contributions: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_components = data.get("components", [])
    components = [
        dict(item)
        for item in (raw_components if isinstance(raw_components, list) else [])
        if isinstance(item, dict)
    ]
    for section, component_kind in _CONTRIBUTION_KIND_BY_SECTION.items():
        raw_items = contributions.get(section, [])
        if not isinstance(raw_items, list):
            continue
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            item.setdefault("kind", component_kind)
            if component_kind == "environment_requirement":
                item = _environment_requirement_component_item(item)
            components.append(item)
    return components


def _environment_requirement_component_item(item: dict[str, Any]) -> dict[str, Any]:
    config = _dict_value(item.get("config", {}))
    requirement_kind = _environment_requirement_kind(item, config)
    name = str(item.get("name") or config.get("name") or "").strip()
    if not name:
        raw_id = str(item.get("id") or "").strip()
        if raw_id.startswith("envreq:"):
            _, _, rest = raw_id.partition(":")
            _, _, name = rest.partition(":")
        elif ":" in raw_id:
            _, _, name = raw_id.partition(":")
    item["kind"] = "environment_requirement"
    if requirement_kind:
        config.setdefault("kind", requirement_kind)
    if name:
        item["name"] = name
        config.setdefault("name", name)
    if not item.get("id") and requirement_kind and name:
        item["id"] = normalize_environment_requirement_id(
            kind=requirement_kind,
            name=name,
        )
    item["config"] = config
    return item


def _environment_requirement_kind(
    item: dict[str, Any],
    config: dict[str, Any],
) -> str:
    candidates = [
        item.get("resource_kind"),
        item.get("requirement_kind"),
        config.get("resource_kind"),
        config.get("requirement_kind"),
        config.get("kind"),
        config.get("type"),
    ]
    item_kind = str(item.get("kind") or "").strip().lower()
    if item_kind != "environment_requirement":
        candidates.append(item_kind)
    return resolve_environment_requirement_kind(
        item.get("id"),
        candidates=tuple(candidates),
        command=config.get("command"),
    )


def _contributions_from_components(components: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, list[dict[str, Any]]] = {
        key: [] for key in _CONTRIBUTION_KIND_BY_SECTION
    }
    section_by_kind = {
        kind: section for section, kind in _CONTRIBUTION_KIND_BY_SECTION.items()
    }
    for component in components:
        if not isinstance(component, dict):
            continue
        kind = str(component.get("kind") or "").strip()
        section = section_by_kind.get(kind)
        if section is None:
            continue
        result[section].append(dict(component))
    return {key: value for key, value in result.items() if value}


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


def _dedupe_requirements(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        requirement_id = str(value.get("id") or "").strip()
        if not requirement_id:
            requirement_id = _environment_requirement_id(value)
            value["id"] = requirement_id
        if not requirement_id or requirement_id in seen:
            continue
        seen.add(requirement_id)
        result.append(value)
    return result


def _environment_requirement_from_component(
    component: CapabilityComponentConfig,
) -> dict[str, Any]:
    config = dict(component.config)
    requirement_kind = _environment_requirement_kind(
        {
            "id": component.id,
            "kind": config.get("kind"),
            "name": component.name,
            "resource_kind": config.get("resource_kind"),
            "requirement_kind": config.get("requirement_kind"),
        },
        config,
    )
    name = str(config.get("name") or component.name).strip()
    result: dict[str, Any] = {
        "id": _environment_requirement_id(
            {"id": component.id, "kind": requirement_kind, "name": name}
        ),
        "kind": requirement_kind,
        "name": name,
        "enabled": component.enabled,
        "component_id": component.id,
        "package_ids": list(component.package_ids),
        "managed_by": component.managed_by,
    }
    for field_name in (
        "command",
        "args",
        "env",
        "cwd",
        "placement",
        "requirements",
        "check",
        "install",
        "configure",
        "version",
        "runtime",
        "language",
        "scope",
        "path",
        "source",
        "description",
        "repo_url",
        "docs",
        "evidence",
        "credentials",
        "risk_level",
        "install_prompt",
        "verify_prompt",
        "notes",
        "value",
    ):
        if field_name in config and config[field_name] not in (None, "", [], {}):
            result[field_name] = config[field_name]
    return result


def _environment_requirement_id(value: dict[str, Any]) -> str:
    return normalize_environment_requirement_id(
        value.get("id"),
        kind=value.get("kind"),
        name=value.get("name"),
    )


def _prompt_fragment_from_component(
    component: CapabilityComponentConfig,
) -> dict[str, Any]:
    config = dict(component.config)
    return {
        key: value
        for key, value in {
            "id": component.id,
            "name": component.name,
            "description": component.description,
            "content": config.get("content"),
            "path": config.get("path"),
        }.items()
        if value not in ("", None, [], {})
    }


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


def _capability_tool_spec(
    *,
    package: CapabilityPackageConfig,
    component: CapabilityComponentConfig,
    source_type: str,
    target_tool_ref: str,
) -> dict[str, Any]:
    policy = _component_execution_policy(component, package=package)
    config = dict(component.config or {})
    input_schema = config.get("input_schema") or config.get("parameters")
    if not isinstance(input_schema, dict):
        input_schema = {"type": "object", "properties": {}}
    description = (
        str(component.description or "").strip()
        or str(component.summary or "").strip()
        or str(package.description or "").strip()
        or f"Capability tool {component.name}"
    )
    tool_id = f"capability:{package.id}:{component.id}"
    metadata = {
        "package_id": package.id,
        "component_id": component.id,
        "source_type": source_type,
        "target_tool_ref": target_tool_ref,
        "execution_policy": policy.get("policy", "inherit"),
        "risk_level": policy.get("risk_level", ""),
        "access": policy.get("access", ""),
    }
    return {
        "tool_id": tool_id,
        "name": component.name,
        "namespace": "capability",
        "description": description,
        "input_schema": dict(input_schema),
        "output_schema": None,
        "output_strategy": "text",
        "risk": "capability",
        "exposure": "deferred",
        "search_text": "\n".join(
            item
            for item in (
                component.name,
                component.display_name,
                description,
                package.name,
                package.description,
                target_tool_ref,
            )
            if str(item or "").strip()
        ),
        "search_keywords": [component.name, package.id, source_type],
        "permission": {"policy": str(policy.get("policy") or "inherit")},
        "mutation": {
            "modifies_files": False,
            "preview_required": False,
            "approved_save_candidate_required": False,
        },
        "execution": {
            "executor_ref": target_tool_ref,
            "backend_dispatch": True,
            "supports_parallel": False,
        },
        "provider_surface": "function",
        "source_type": source_type,
        "target_tool_ref": target_tool_ref,
        "metadata": {key: value for key, value in metadata.items() if value not in ("", None)},
    }


def _dedupe_tool_specs(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        tool_id = str(value.get("tool_id") or "").strip()
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        result.append(dict(value))
    return result


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
    worker_kind: WorkerKind = WorkerKind.SERVER_WORKER
    model_request_origin: ModelRequestOrigin = ModelRequestOrigin.SERVER
    worktree_role: WorktreeRole = WorktreeRole.TARGET
    publish_policy: PublishPolicy = PublishPolicy.NEVER
    model: str = ""
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    runtime_home_policy: str = ""
    approval_mode: str = ""
    config_isolation: str = ""
    timeout_sec: int | None = None
    step_timeout_sec: int | None = None
    credential_refs: dict[str, str] = field(default_factory=dict)
    mcp: dict[str, Any] = field(default_factory=dict)
    sandbox: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls, profile_id: str, data: dict[str, Any] | None
    ) -> "RuntimeProfileConfig":
        if not isinstance(data, dict):
            data = {}
        _reject_plaintext_secret_container(data, owner="runtime profile")
        executor = ExecutorType(str(data.get("executor", "reuleauxcoder")))
        execution_location = ExecutionLocation(
            str(data.get("execution_location", "remote_server"))
        )
        sandbox = _dict_value(data.get("sandbox", {}))
        worker_kind = _runtime_worker_kind(
            data.get("worker_kind"),
            execution_location=execution_location,
            sandbox=sandbox,
        )
        model_request_origin = _runtime_model_request_origin(
            data.get("model_request_origin"),
            executor=executor,
            worker_kind=worker_kind,
        )
        return cls(
            id=str(profile_id),
            executor=executor,
            execution_location=execution_location,
            worker_kind=worker_kind,
            model_request_origin=model_request_origin,
            worktree_role=WorktreeRole(str(data.get("worktree_role", "target"))),
            publish_policy=PublishPolicy(str(data.get("publish_policy", "never"))),
            model=str(data.get("model", "") or ""),
            command=str(data["command"]) if data.get("command") is not None else None,
            args=_string_list(data.get("args", [])),
            env=_string_dict(data.get("env", {})),
            runtime_home_policy=str(data.get("runtime_home_policy", "") or ""),
            approval_mode=str(data.get("approval_mode", "") or ""),
            config_isolation=str(data.get("config_isolation", "") or ""),
            timeout_sec=_positive_int_or_none(data.get("timeout_sec")),
            step_timeout_sec=_positive_int_or_none(data.get("step_timeout_sec")),
            credential_refs=_string_dict(data.get("credential_refs", {})),
            mcp=_dict_value(data.get("mcp", {})),
            sandbox=sandbox,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "executor": self.executor.value,
            "execution_location": self.execution_location.value,
            "worker_kind": self.worker_kind.value,
            "model_request_origin": self.model_request_origin.value,
            "worktree_role": self.worktree_role.value,
            "publish_policy": self.publish_policy.value,
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
        if self.timeout_sec is not None:
            result["timeout_sec"] = self.timeout_sec
        if self.step_timeout_sec is not None:
            result["step_timeout_sec"] = self.step_timeout_sec
        if self.credential_refs:
            result["credential_refs"] = dict(self.credential_refs)
        if self.mcp:
            result["mcp"] = dict(self.mcp)
        if self.sandbox or self.worker_kind == WorkerKind.SANDBOX_WORKER:
            result["sandbox"] = dict(self.sandbox)
        return result


@dataclass
class AgentMemoryPolicyConfig:
    """Agent-level memory provider policy."""

    enabled: bool = True
    primary_provider: str = ""
    read_providers: list[str] = field(default_factory=list)
    inject: bool = True
    capture: bool = True
    token_budget: int | None = None
    scope_mode: str = "isolated"
    expose_tools: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AgentMemoryPolicyConfig":
        if not isinstance(data, dict):
            return cls()
        token_budget_raw = data.get("token_budget")
        token_budget = int(token_budget_raw) if token_budget_raw is not None else None
        return cls(
            enabled=_bool_value(data.get("enabled"), True),
            primary_provider=str(data.get("primary_provider", "") or "").strip(),
            read_providers=_string_list(data.get("read_providers", [])),
            inject=_bool_value(data.get("inject"), True),
            capture=_bool_value(data.get("capture"), True),
            token_budget=token_budget,
            scope_mode=str(data.get("scope_mode", "isolated") or "isolated"),
            expose_tools=_bool_value(data.get("expose_tools"), False),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if not self.enabled:
            result["enabled"] = self.enabled
        if self.primary_provider:
            result["primary_provider"] = self.primary_provider
        if self.read_providers:
            result["read_providers"] = list(self.read_providers)
        if not self.inject:
            result["inject"] = self.inject
        if not self.capture:
            result["capture"] = self.capture
        if self.token_budget is not None:
            result["token_budget"] = self.token_budget
        if self.scope_mode != "isolated":
            result["scope_mode"] = self.scope_mode
        if self.expose_tools:
            result["expose_tools"] = self.expose_tools
        return result


class AgentRunStatus(str, Enum):
    """Durable lifecycle status for a long-lived AgentRun mainline."""

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class AgentRunWaitingReason(str, Enum):
    """Reason a long-lived AgentRun is waiting for its next activation."""

    SERVER_PROCESSING = "server_processing"
    USER_APPROVAL = "user_approval"
    USER_INPUT = "user_input"
    AGENT_CALL = "agent_call"
    WORKER_RECONNECT = "worker_reconnect"
    SCHEDULED_RESUME = "scheduled_resume"
    ADMIN_ACTION = "admin_action"


class AgentRunResumePolicy(str, Enum):
    """Source allowed to resume a waiting AgentRun."""

    AUTOMATIC = "automatic"
    USER_ACTION = "user_action"
    EXTERNAL_EVENT = "external_event"
    MANUAL_ADMIN = "manual_admin"


class AgentRunActivationInputKind(str, Enum):
    """Structured input source for one AgentRun activation."""

    USER_REQUEST = "user_request"
    SERVER_FEEDBACK = "server_feedback"
    USER_FEEDBACK = "user_feedback"
    USER_APPROVAL = "user_approval"
    AGENT_FEEDBACK = "agent_feedback"
    SCHEDULED_RESUME = "scheduled_resume"
    ADMIN_RESUME = "admin_resume"


class AgentRunActivationStatus(str, Enum):
    """Lifecycle status for a single AgentRun activation."""

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class AgentRunFeedbackSource(str, Enum):
    """Structured feedback source that can resume the same AgentRun."""

    SERVER = "server"
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    ADMIN = "admin"


class AgentRunFeedbackKind(str, Enum):
    """Structured feedback type consumed by a later activation."""

    CANDIDATE_VALIDATION_FAILED = "candidate_validation_failed"
    CANDIDATE_READY = "candidate_ready"
    APPROVAL_RESOLVED = "approval_resolved"
    USER_MESSAGE = "user_message"
    AGENT_CALL_RESULT = "agent_call_result"
    AGENT_CALL_FAILED = "agent_call_failed"
    ENVIRONMENT_EVENT = "environment_event"
    RETRY_INSTRUCTION = "retry_instruction"


class AgentRunFeedbackVisibility(str, Enum):
    """Whether feedback is intended for transcript projection."""

    INTERNAL = "internal"
    USER_VISIBLE = "user_visible"
    AUDIT_ONLY = "audit_only"


class ActivationSteerSource(str, Enum):
    """Source of same-activation steering input."""

    USER = "user"
    ADMIN = "admin"
    SYSTEM = "system"


class ActivationSteerStatus(str, Enum):
    """Delivery state for same-activation steering input."""

    QUEUED = "queued"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRunRelationType(str, Enum):
    """Relationship between two AgentRun mainlines."""

    AGENT_CALL_EPHEMERAL = "agent_call_ephemeral"
    AGENT_CALL_PERSISTENT = "agent_call_persistent"
    FORK = "fork"
    REVIEW = "review"
    DIAGNOSTIC_PROBE = "diagnostic_probe"


class AgentRunRelationStatus(str, Enum):
    """Lifecycle status for an AgentRun relation."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNAVAILABLE = "unavailable"


class AgentThreadBindingLifetime(str, Enum):
    """Lifetime that controls Agent thread binding cleanup."""

    SESSION = "session"
    RUN = "run"


class AgentThreadBindingStatus(str, Enum):
    """Lifecycle status for a long-lived Agent thread binding."""

    ACTIVE = "active"
    CLOSED = "closed"
    UNAVAILABLE = "unavailable"


class AgentThreadWorkdirPolicy(str, Enum):
    """Workspace policy for an Agent thread binding."""

    INHERIT_MAIN = "inherit_main"


class AgentCallableExposure(str, Enum):
    """How a callable Agent is exposed to the model."""

    DIRECT = "direct"
    DEFERRED = "deferred"
    HIDDEN = "hidden"
    SYSTEM_ONLY = "system-only"


class AgentCallableAuthorizationStatus(str, Enum):
    """Authorization state for invoking a projected Agent."""

    ALLOWED = "allowed"
    REQUIRES_APPROVAL = "requires_approval"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


def _optional_enum(enum_type: type[Enum], value: Any) -> Enum | None:
    if value is None or value == "":
        return None
    return enum_type(_enum_value(value))


@dataclass
class AgentRun:
    """Long-lived Agent mainline; not a worker claim or single model call."""

    id: str
    agent_id: str
    kind: str = "agent_run"
    owner_session_run_id: str = ""
    source: AgentRunSource = AgentRunSource.MANUAL
    trigger_mode: TriggerMode = TriggerMode.ISSUE_TASK
    status: AgentRunStatus = AgentRunStatus.QUEUED
    waiting_reason: AgentRunWaitingReason | None = None
    resume_policy: AgentRunResumePolicy | None = None
    runtime_profile_id: str | None = None
    executor: ExecutorType | None = None
    execution_location: ExecutionLocation | None = None
    worktree_role: WorktreeRole | None = None
    publish_policy: PublishPolicy | None = None
    workdir: str | None = None
    workspace_ref: str | None = None
    executor_session_id: str | None = None
    current_activation_id: str | None = None
    sandbox_id: str | None = None
    sandbox_session_id: str | None = None
    retention_scope: str = "session"
    cleanup_policy: str = "delete_with_owner_session"
    terminal_result: dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None
    cancel_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source = AgentRunSource(_enum_value(self.source) or AgentRunSource.MANUAL)
        self.trigger_mode = TriggerMode(_enum_value(self.trigger_mode))
        self.status = AgentRunStatus(_enum_value(self.status) or AgentRunStatus.QUEUED)
        self.waiting_reason = _optional_enum(AgentRunWaitingReason, self.waiting_reason)
        self.resume_policy = _optional_enum(AgentRunResumePolicy, self.resume_policy)
        if self.executor is not None:
            self.executor = ExecutorType(_enum_value(self.executor))
        if self.execution_location is not None:
            self.execution_location = ExecutionLocation(
                _enum_value(self.execution_location)
            )
        if self.worktree_role is not None:
            self.worktree_role = WorktreeRole(_enum_value(self.worktree_role))
        if self.publish_policy is not None:
            self.publish_policy = PublishPolicy(_enum_value(self.publish_policy))
        self.terminal_result = _dict_value(self.terminal_result)
        self.metadata = _dict_value(self.metadata)
        forbidden_metadata = sorted(
            key for key in self.metadata if key in AGENT_RUN_METADATA_FORBIDDEN_KEYS
        )
        if forbidden_metadata:
            raise ValueError(
                "AgentRun.metadata cannot store taskflow or external business "
                "identity fields: "
                + ", ".join(forbidden_metadata)
            )

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
            AgentRunStatus.BLOCKED,
        }


@dataclass
class AgentRunActivation:
    """One wake/execute turn inside a long-lived AgentRun."""

    id: str
    agent_run_id: str
    seq: int
    input_kind: AgentRunActivationInputKind
    input_payload: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    status: AgentRunActivationStatus = AgentRunActivationStatus.QUEUED
    output: str | None = None
    result_payload: dict[str, Any] = field(default_factory=dict)
    worker_id: str | None = None
    request_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.seq = int(self.seq)
        self.input_kind = AgentRunActivationInputKind(_enum_value(self.input_kind))
        self.status = AgentRunActivationStatus(
            _enum_value(self.status) or AgentRunActivationStatus.QUEUED
        )
        self.input_payload = _dict_value(self.input_payload)
        self.result_payload = _dict_value(self.result_payload)
        self.metadata = _dict_value(self.metadata)


@dataclass
class ActivationSteer:
    """Same-activation steering input; does not create a new activation."""

    id: str
    activation_id: str
    source: ActivationSteerSource
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    delivered_at: str | None = None
    status: ActivationSteerStatus = ActivationSteerStatus.QUEUED
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source = ActivationSteerSource(_enum_value(self.source))
        self.status = ActivationSteerStatus(
            _enum_value(self.status) or ActivationSteerStatus.QUEUED
        )
        self.payload = _dict_value(self.payload)
        self.metadata = _dict_value(self.metadata)


@dataclass
class ExecutorSession:
    """Opaque execution-site reference bound to an AgentRun, not a session kernel."""

    id: str
    agent_run_id: str
    agent_id: str
    executor: ExecutorType
    execution_location: ExecutionLocation
    workdir: str | None = None
    branch: str | None = None
    sandbox_session_id: str | None = None
    provider_session_id: str | None = None
    state_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.executor = ExecutorType(_enum_value(self.executor))
        self.execution_location = ExecutionLocation(_enum_value(self.execution_location))
        self.metadata = _dict_value(self.metadata)


@dataclass
class AgentRunFeedback:
    """Structured input that can resume the same AgentRun."""

    id: str
    agent_run_id: str
    source: AgentRunFeedbackSource
    kind: AgentRunFeedbackKind
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    consumed_by_activation_id: str | None = None
    visibility: AgentRunFeedbackVisibility = AgentRunFeedbackVisibility.INTERNAL
    requires_activation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.source = AgentRunFeedbackSource(_enum_value(self.source))
        self.kind = AgentRunFeedbackKind(_enum_value(self.kind))
        self.visibility = AgentRunFeedbackVisibility(
            _enum_value(self.visibility) or AgentRunFeedbackVisibility.INTERNAL
        )
        self.requires_activation = _bool_value(self.requires_activation, False)
        self.payload = _dict_value(self.payload)
        self.metadata = _dict_value(self.metadata)


@dataclass
class AgentRunRelation:
    """Relationship index between AgentRun mainlines."""

    id: str
    owner_agent_run_id: str
    related_agent_run_id: str
    relation_type: AgentRunRelationType
    relation_scope: str = "session"
    created_by_activation_id: str | None = None
    status: AgentRunRelationStatus = AgentRunRelationStatus.ACTIVE
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.relation_type = AgentRunRelationType(_enum_value(self.relation_type))
        self.status = AgentRunRelationStatus(
            _enum_value(self.status) or AgentRunRelationStatus.ACTIVE
        )
        self.relation_scope = str(self.relation_scope or "session")
        self.metadata = _dict_value(self.metadata)


@dataclass
class AgentThreadBinding:
    """Persistent Agent thread binding; target Agent owns its own runtime session."""

    id: str
    owner_session_run_id: str
    main_agent_run_id: str
    agent_id: str
    target_agent_run_id: str
    thread_key: str = ""
    thread_summary: str = ""
    binding_lifetime: AgentThreadBindingLifetime = AgentThreadBindingLifetime.SESSION
    workdir_policy: AgentThreadWorkdirPolicy = AgentThreadWorkdirPolicy.INHERIT_MAIN
    visibility: str = "hidden_from_user_transcript"
    status: AgentThreadBindingStatus = AgentThreadBindingStatus.ACTIVE
    cleanup_policy: str = "delete_with_owner_session"
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.thread_key = str(self.thread_key or "")
        self.thread_summary = str(self.thread_summary or "")
        self.binding_lifetime = AgentThreadBindingLifetime(
            _enum_value(self.binding_lifetime)
        )
        self.workdir_policy = AgentThreadWorkdirPolicy(
            _enum_value(self.workdir_policy)
        )
        self.status = AgentThreadBindingStatus(
            _enum_value(self.status) or AgentThreadBindingStatus.ACTIVE
        )
        self.metadata = _dict_value(self.metadata)


@dataclass
class AgentCallGrant:
    """Reusable grant for one main Agent invoking one target Agent scope."""

    user_id: str
    grant_scope: str
    main_agent_id: str
    target_agent_id: str
    conversation_scope: str
    capability_scope: dict[str, Any] = field(default_factory=dict)
    target_config_version: str = ""
    granted_at: str | None = None
    expires_at: str | None = None
    revoked_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        scopes = _string_choices(
            [self.conversation_scope],
            AGENT_CALLABLE_SCOPES,
            "AgentCallGrant.conversation_scope",
        )
        self.conversation_scope = scopes[0]
        self.grant_scope = str(self.grant_scope or "")
        self.capability_scope = _dict_value(self.capability_scope)
        self.metadata = _dict_value(self.metadata)


@dataclass
class AgentCallableProjectionEntry:
    """Model-visible projection of a callable Agent."""

    agent_id: str
    display_name: str = ""
    summary: str = ""
    exposure: AgentCallableExposure = AgentCallableExposure.DEFERRED
    callable_scopes: list[str] = field(default_factory=list)
    authorization_status: AgentCallableAuthorizationStatus = (
        AgentCallableAuthorizationStatus.UNAVAILABLE
    )
    existing_threads: list[dict[str, Any]] = field(default_factory=list)
    capability_scope: dict[str, Any] = field(default_factory=dict)
    source: str = "agent_registry"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.exposure = AgentCallableExposure(_enum_value(self.exposure))
        self.callable_scopes = _string_choices(
            self.callable_scopes,
            AGENT_CALLABLE_SCOPES,
            "AgentCallableProjectionEntry.callable_scopes",
        )
        self.authorization_status = AgentCallableAuthorizationStatus(
            _enum_value(self.authorization_status)
        )
        self.existing_threads = _dict_list(self.existing_threads)
        self.capability_scope = _dict_value(self.capability_scope)
        self.metadata = _dict_value(self.metadata)


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
    callable_scopes: list[str] | None = None
    taskflow_eligible: bool = True
    system_flow_only: list[str] = field(default_factory=list)
    runtime_profile: str = ""
    dispatch: AgentDispatchConfig = field(default_factory=AgentDispatchConfig)
    capability_refs: list[str] = field(default_factory=list)
    model: AgentModelConfig = field(default_factory=AgentModelConfig)
    prompt: AgentPromptConfig = field(default_factory=AgentPromptConfig)
    memory: AgentMemoryPolicyConfig = field(default_factory=AgentMemoryPolicyConfig)
    max_concurrent_tasks: int | None = None
    credential_refs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, agent_id: str, data: dict[str, Any] | None) -> "AgentConfig":
        if not isinstance(data, dict):
            data = {}
        _reject_plaintext_secret_container(data, owner="agent config")
        removed_fields = [
            key
            for key in (
                "capabilities",
                "callable_modes",
                "delegable",
                "mcp",
                "skills",
                "dispatch_tags",
            )
            if key in data
        ]
        if removed_fields:
            raise ValueError(
                "agent config fields "
                + ", ".join(sorted(removed_fields))
                + " were removed; use callable_scopes for Agent invocation scopes, "
                + "dispatch.profile/examples/avoid for Agent routing profile, "
                + "and capability_refs for capability packages"
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
            callable_scopes=_string_choices(
                data.get("callable_scopes"),
                AGENT_CALLABLE_SCOPES,
                "agent config callable_scopes",
                default=["ephemeral", "persistent"] if user_visible else [],
            ),
            taskflow_eligible=_bool_value(data.get("taskflow_eligible"), user_visible),
            system_flow_only=_string_list(data.get("system_flow_only", [])),
            runtime_profile=str(data.get("runtime_profile", "") or ""),
            dispatch=AgentDispatchConfig.from_dict(data.get("dispatch")),
            capability_refs=_string_list(data.get("capability_refs", [])),
            model=AgentModelConfig.from_dict(data.get("model")),
            prompt=AgentPromptConfig.from_dict(data.get("prompt")),
            memory=AgentMemoryPolicyConfig.from_dict(data.get("memory")),
            max_concurrent_tasks=max_concurrent_tasks,
            credential_refs=_string_dict(data.get("credential_refs", {})),
        )

    def __post_init__(self) -> None:
        self.visibility = _choice(self.visibility, AGENT_VISIBILITIES, "user")
        if self.callable_scopes is None:
            default_scopes = (
                ["ephemeral", "persistent"] if self.visibility == "user" else []
            )
            self.callable_scopes = list(default_scopes)
        else:
            self.callable_scopes = _string_choices(
                self.callable_scopes,
                AGENT_CALLABLE_SCOPES,
                "agent config callable_scopes",
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
        default_scopes = ["ephemeral", "persistent"] if self.visibility == "user" else []
        if self.callable_scopes != default_scopes:
            result["callable_scopes"] = list(self.callable_scopes or [])
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
        memory = self.memory.to_dict()
        if memory:
            result["memory"] = memory
        if self.max_concurrent_tasks is not None:
            result["max_concurrent_tasks"] = self.max_concurrent_tasks
        if self.credential_refs:
            result["credential_refs"] = dict(self.credential_refs)
        return result

    @property
    def user_visible(self) -> bool:
        return self.visibility == "user"

    @property
    def can_call_ephemeral(self) -> bool:
        return self.user_visible and "ephemeral" in set(self.callable_scopes or [])

    @property
    def can_call_persistent(self) -> bool:
        return self.user_visible and "persistent" in set(self.callable_scopes or [])

    @property
    def can_run_taskflow(self) -> bool:
        return self.user_visible and self.taskflow_eligible

    def allows_system_flow(self, flow: str) -> bool:
        return str(flow).strip() in set(self.system_flow_only)


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
    """Opaque executor session/workdir reference used to resume a task activation."""

    agent_id: str
    executor: ExecutorType
    execution_location: ExecutionLocation
    task_id: str
    workdir: str | None = None
    branch: str | None = None
    executor_session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.executor = ExecutorType(_enum_value(self.executor))
        self.execution_location = ExecutionLocation(_enum_value(self.execution_location))
        self.metadata = _dict_value(self.metadata)

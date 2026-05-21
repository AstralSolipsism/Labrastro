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


def _reject_plaintext_secret_container(data: dict[str, Any], *, owner: str) -> None:
    secret_keys = {"secret", "secrets", "api_key", "api_keys", "token", "tokens"}
    for key in data:
        if str(key).strip().lower() in secret_keys:
            raise ValueError(
                f"{owner} must reference secrets through credential_refs, not plaintext secrets"
            )


@dataclass
class CapabilityPackageConfig:
    """Reusable Agent capability package assembled from component inventories."""

    id: str
    name: str = ""
    description: str = ""
    mcp_servers: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    cli_tools: list[str] = field(default_factory=list)
    source: str = ""
    market: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls, package_id: str, data: dict[str, Any] | None
    ) -> "CapabilityPackageConfig":
        if not isinstance(data, dict):
            data = {}
        _reject_plaintext_secret_container(data, owner="capability package")
        return cls(
            id=str(package_id),
            name=str(data.get("name", "") or ""),
            description=str(data.get("description", "") or ""),
            mcp_servers=_string_list(data.get("mcp_servers", [])),
            skills=_string_list(data.get("skills", [])),
            cli_tools=_string_list(data.get("cli_tools", [])),
            source=str(data.get("source", "") or ""),
            market=_dict_value(data.get("market", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.name:
            result["name"] = self.name
        if self.description:
            result["description"] = self.description
        if self.mcp_servers:
            result["mcp_servers"] = list(self.mcp_servers)
        if self.skills:
            result["skills"] = list(self.skills)
        if self.cli_tools:
            result["cli_tools"] = list(self.cli_tools)
        if self.source:
            result["source"] = self.source
        if self.market:
            result["market"] = dict(self.market)
        return result


def resolve_capability_refs(
    capability_refs: list[str],
    packages: dict[str, CapabilityPackageConfig],
) -> dict[str, Any]:
    """Resolve package refs into executor-facing capability components."""

    resolved_packages: list[dict[str, Any]] = []
    mcp_servers: list[str] = []
    skills: list[str] = []
    cli_tools: list[str] = []
    for package_id in capability_refs:
        package = packages.get(package_id)
        if package is None:
            continue
        package_dict = package.to_dict()
        package_dict["id"] = package.id
        resolved_packages.append(package_dict)
        mcp_servers.extend(package.mcp_servers)
        skills.extend(package.skills)
        cli_tools.extend(package.cli_tools)
    return {
        "packages": resolved_packages,
        "mcp_servers": _dedupe_strings(mcp_servers),
        "skills": _dedupe_strings(skills),
        "cli_tools": _dedupe_strings(cli_tools),
    }


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
        return cls(
            id=str(agent_id),
            name=str(data.get("name", "") or ""),
            description=str(data.get("description", "") or ""),
            role=str(data.get("role", "") or ""),
            entrypoint=bool(data.get("entrypoint", False)),
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
        if self.entrypoint:
            result["entrypoint"] = self.entrypoint
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

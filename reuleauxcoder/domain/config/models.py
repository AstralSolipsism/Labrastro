"""Configuration models - domain layer configuration abstractions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
    CapabilityPackageConfig,
    RuntimeProfileConfig,
    resolve_capability_refs,
)


MCPPlacement = Literal["server", "peer", "both"]
MCPDistribution = Literal["command", "artifact"]
CLIPlacement = Literal["server", "local", "both"]
ProviderType = Literal["openai_chat", "anthropic_messages", "openai_responses"]
ProviderCompat = Literal["generic", "deepseek", "kimi", "glm", "qwen", "zenmux"]

SUPPORTED_PROVIDER_COMPATS = {"generic", "deepseek", "kimi", "glm", "qwen", "zenmux"}

DEFAULT_ENVIRONMENT_RUNTIME_PROFILE_ID = "environment_local"
DEFAULT_ENVIRONMENT_AGENT_ID = "environment_configurator"
DEFAULT_ENVIRONMENT_RUNTIME_PROFILE: dict[str, Any] = {
    "executor": "reuleauxcoder",
    "execution_location": "local_workspace",
    "runtime_home_policy": "per_task",
    "approval_mode": "full",
}
DEFAULT_ENVIRONMENT_AGENT: dict[str, Any] = {
    "name": "Environment Configurator",
    "description": "Checks and configures the local workspace environment from the server manifest.",
    "runtime_profile": DEFAULT_ENVIRONMENT_RUNTIME_PROFILE_ID,
    "dispatch": {
        "profile": (
            "Best for reading the environment manifest and checking or configuring "
            "local workspace CLI, MCP, and Skill components from that manifest."
        ),
        "examples": [
            "Check whether the current workspace satisfies the server environment manifest",
            "Install or configure missing local tools from the manifest",
        ],
        "avoid": [
            "General code review or product implementation tasks",
        ],
    },
    "capability_refs": ["environment"],
}
DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE: dict[str, Any] = {
    "name": "Environment Tools",
    "description": "Read and configure the local workspace environment manifest.",
    "permissions": [
        "environment.check",
        "environment.configure",
        "environment.manifest.read",
    ],
    "source": "builtin",
}


def ensure_default_environment_agent_registry(
    agent_registry_data: dict[str, Any] | None,
    runtime_profiles_data: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return Agent Registry and runtime profile data with the environment Agent present."""

    registry = deepcopy(agent_registry_data) if isinstance(agent_registry_data, dict) else {}
    profiles = (
        deepcopy(runtime_profiles_data)
        if isinstance(runtime_profiles_data, dict)
        else {}
    )
    profile = profiles.get(DEFAULT_ENVIRONMENT_RUNTIME_PROFILE_ID)
    if not isinstance(profile, dict):
        profiles[DEFAULT_ENVIRONMENT_RUNTIME_PROFILE_ID] = deepcopy(
            DEFAULT_ENVIRONMENT_RUNTIME_PROFILE
        )
    else:
        for key, value in DEFAULT_ENVIRONMENT_RUNTIME_PROFILE.items():
            profile.setdefault(key, deepcopy(value))

    raw_agents = registry.get("agents")
    agents = raw_agents if isinstance(raw_agents, dict) else {}
    registry["agents"] = agents
    agent = agents.get(DEFAULT_ENVIRONMENT_AGENT_ID)
    if not isinstance(agent, dict):
        agents[DEFAULT_ENVIRONMENT_AGENT_ID] = deepcopy(DEFAULT_ENVIRONMENT_AGENT)
    else:
        for key, value in DEFAULT_ENVIRONMENT_AGENT.items():
            if key not in agent:
                agent[key] = deepcopy(value)
        if not isinstance(agent.get("dispatch"), dict):
            agent["dispatch"] = deepcopy(DEFAULT_ENVIRONMENT_AGENT["dispatch"])
        if not isinstance(agent.get("capability_refs"), list):
            agent["capability_refs"] = list(DEFAULT_ENVIRONMENT_AGENT["capability_refs"])
    return registry, profiles


def ensure_default_capability_packages(
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return capability package data with built-in packages present."""

    packages = deepcopy(data) if isinstance(data, dict) else {}
    package = packages.get("environment")
    if not isinstance(package, dict):
        packages["environment"] = deepcopy(DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE)
    else:
        for key, value in DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE.items():
            package.setdefault(key, deepcopy(value))
    return packages


def normalize_provider_compat(value: Any) -> ProviderCompat:
    """Normalize a configured provider compatibility profile."""

    normalized = str(value or "generic").strip().lower()
    if normalized in SUPPORTED_PROVIDER_COMPATS:
        return normalized  # type: ignore[return-value]
    return "generic"


def infer_provider_compat(base_url: str | None) -> ProviderCompat:
    """Infer a provider compat profile from a known service endpoint."""

    url = str(base_url or "").lower()
    if "api.deepseek.com" in url:
        return "deepseek"
    if "moonshot" in url or "kimi" in url:
        return "kimi"
    if "bigmodel.cn" in url or "zhipu" in url or "z.ai" in url:
        return "glm"
    if "dashscope" in url or "aliyuncs.com" in url or "bailian" in url:
        return "qwen"
    if "zenmux.ai" in url:
        return "zenmux"
    return "generic"


@dataclass
class ProviderApiFeatures:
    """Declared LLM provider API features used for request shaping."""

    chat: bool = True
    streaming: bool = True
    tools: bool = True
    parallel_tools: bool = True
    tool_choice_required: bool = False
    reasoning_effort: bool = False
    thinking: bool = False
    thinking_signature: bool = False
    image_input: bool = False
    responses_api: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "chat": self.chat,
            "streaming": self.streaming,
            "tools": self.tools,
            "parallel_tools": self.parallel_tools,
            "tool_choice_required": self.tool_choice_required,
            "reasoning_effort": self.reasoning_effort,
            "thinking": self.thinking,
            "thinking_signature": self.thinking_signature,
            "image_input": self.image_input,
            "responses_api": self.responses_api,
        }

    @classmethod
    def defaults_for(cls, provider_type: str) -> "ProviderApiFeatures":
        normalized = provider_type.strip().lower()
        if normalized == "anthropic_messages":
            return cls(
                tools=True,
                parallel_tools=True,
                tool_choice_required=True,
                thinking=True,
                thinking_signature=True,
            )
        if normalized == "openai_responses":
            return cls(
                tools=True,
                parallel_tools=True,
                tool_choice_required=True,
                reasoning_effort=True,
                responses_api=True,
            )
        return cls(
            tools=True,
            parallel_tools=True,
            tool_choice_required=False,
            reasoning_effort=True,
            thinking=True,
        )

    @classmethod
    def from_dict(
        cls, d: dict[str, Any] | None, *, provider_type: str = "openai_chat"
    ) -> "ProviderApiFeatures":
        defaults = cls.defaults_for(provider_type)
        if not isinstance(d, dict):
            return defaults
        data = defaults.to_dict()
        for key, value in d.items():
            if key in data:
                data[key] = bool(value)
        return cls(**data)


@dataclass
class ProviderConfig:
    """Server-side LLM provider configuration."""

    id: str
    type: ProviderType = "openai_chat"
    compat: ProviderCompat = "generic"
    enabled: bool = True
    api_key: str = ""
    base_url: Optional[str] = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_sec: int = 120
    max_retries: int = 3
    api_features: ProviderApiFeatures = field(
        default_factory=lambda: ProviderApiFeatures.defaults_for("openai_chat")
    )
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "compat": self.compat,
            "enabled": self.enabled,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "headers": dict(self.headers),
            "timeout_sec": self.timeout_sec,
            "max_retries": self.max_retries,
            "api_features": self.api_features.to_dict(),
            "extra": dict(self.extra),
        }
        return data

    @classmethod
    def from_dict(cls, provider_id: str, d: dict[str, Any]) -> "ProviderConfig":
        if "capabilities" in d:
            raise ValueError(
                "provider config field capabilities was removed; use api_features"
            )
        raw_type = str(d.get("type", "openai_chat")).strip().lower()
        provider_type: ProviderType
        if raw_type in {"anthropic_messages", "openai_responses"}:
            provider_type = raw_type  # type: ignore[assignment]
        else:
            provider_type = "openai_chat"
        raw_headers = d.get("headers", {})
        raw_extra = d.get("extra", {})
        base_url = str(d["base_url"]) if d.get("base_url") is not None else None
        compat = (
            normalize_provider_compat(d.get("compat"))
            if d.get("compat") is not None
            else infer_provider_compat(base_url)
        )
        return cls(
            id=provider_id,
            type=provider_type,
            compat=compat,
            enabled=bool(d.get("enabled", True)),
            api_key=str(d.get("api_key", "") or ""),
            base_url=base_url,
            headers=(
                {str(k): str(v) for k, v in raw_headers.items()}
                if isinstance(raw_headers, dict)
                else {}
            ),
            timeout_sec=int(d.get("timeout_sec", 120) or 120),
            max_retries=int(d.get("max_retries", 3) or 3),
            api_features=ProviderApiFeatures.from_dict(
                d.get("api_features"), provider_type=provider_type
            ),
            extra=dict(raw_extra) if isinstance(raw_extra, dict) else {},
        )


@dataclass
class ProvidersConfig:
    """Configured LLM providers keyed by provider id."""

    items: dict[str, ProviderConfig] = field(default_factory=dict)


@dataclass
class MCPLaunchConfig:
    """Launch command for a peer-hosted MCP server."""

    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "cwd": self.cwd,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MCPLaunchConfig":
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
class MCPArtifactConfig:
    """Versioned artifact for a peer-hosted MCP server."""

    path: str
    sha256: str
    launch: MCPLaunchConfig | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path, "sha256": self.sha256}
        if self.launch is not None:
            data["launch"] = self.launch.to_dict()
        return data

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MCPArtifactConfig":
        raw_launch = d.get("launch")
        return cls(
            path=str(d.get("path", "")),
            sha256=str(d.get("sha256", "")),
            launch=(
                MCPLaunchConfig.from_dict(raw_launch)
                if isinstance(raw_launch, dict)
                else None
            ),
        )


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server."""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    enabled: bool = True
    placement: MCPPlacement = "server"
    distribution: MCPDistribution = "command"
    version: Optional[str] = None
    launch: MCPLaunchConfig | None = None
    artifacts: dict[str, MCPArtifactConfig] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    requirements: dict[str, str] = field(default_factory=dict)
    build: dict[str, Any] = field(default_factory=dict)
    check: str = ""
    install: str = ""
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

    def to_dict(self) -> dict:
        """Convert to dictionary format for serialization."""
        return {
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "cwd": self.cwd,
            "enabled": self.enabled,
            "placement": self.placement,
            "distribution": self.distribution,
            "version": self.version,
            "launch": self.launch.to_dict() if self.launch else None,
            "artifacts": {
                platform: artifact.to_dict()
                for platform, artifact in self.artifacts.items()
            },
            "permissions": self.permissions,
            "requirements": self.requirements,
            "build": self.build,
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

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "MCPServerConfig":
        """Create from dictionary format."""
        raw_placement = str(d.get("placement", "server")).lower()
        placement: MCPPlacement
        if raw_placement in {"peer", "both"}:
            placement = raw_placement  # type: ignore[assignment]
        else:
            placement = "server"
        raw_distribution = str(d.get("distribution", "")).lower()
        raw_artifacts = d.get("artifacts", {})
        artifacts = (
            {
                str(platform): MCPArtifactConfig.from_dict(artifact)
                for platform, artifact in raw_artifacts.items()
                if isinstance(artifact, dict)
            }
            if isinstance(raw_artifacts, dict)
            else {}
        )
        distribution: MCPDistribution
        if raw_distribution in {"command", "artifact"}:
            distribution = raw_distribution  # type: ignore[assignment]
        elif artifacts:
            distribution = "artifact"
        else:
            distribution = "command"
        raw_launch = d.get("launch")
        launch = (
            MCPLaunchConfig.from_dict(raw_launch)
            if isinstance(raw_launch, dict)
            else None
        )
        raw_permissions = d.get("permissions", {})
        raw_requirements = d.get("requirements", {})
        raw_build = d.get("build", {})
        raw_args = d.get("args", [])
        raw_env = d.get("env", {})
        return cls(
            name=name,
            command=str(d.get("command", "")),
            args=[str(arg) for arg in raw_args] if isinstance(raw_args, list) else [],
            env=(
                {str(k): str(v) for k, v in raw_env.items()}
                if isinstance(raw_env, dict)
                else {}
            ),
            cwd=d.get("cwd"),
            enabled=_bool_config_value(d.get("enabled", True)),
            placement=placement,
            distribution=distribution,
            version=str(d["version"]) if d.get("version") is not None else None,
            launch=launch,
            artifacts=artifacts,
            permissions=(
                dict(raw_permissions) if isinstance(raw_permissions, dict) else {}
            ),
            requirements=(
                {str(k): str(v) for k, v in raw_requirements.items()}
                if isinstance(raw_requirements, dict)
                else {}
            ),
            build=dict(raw_build) if isinstance(raw_build, dict) else {},
            check=str(d.get("check", "")),
            install=str(d.get("install", "")),
            source=str(d.get("source", "")),
            description=str(d.get("description", "")),
            repo_url=str(d.get("repo_url", "")),
            docs=_docs_config_value(d.get("docs", [])),
            evidence=_string_dict_list_config_value(d.get("evidence", [])),
            install_prompt=str(d.get("install_prompt", "")),
            verify_prompt=str(d.get("verify_prompt", "")),
            notes=_string_list_config_value(d.get("notes", [])),
            credentials=_string_list_config_value(d.get("credentials", [])),
            risk_level=str(d.get("risk_level", "")),
            last_action=str(d.get("last_action", "")),
            last_updated=str(d.get("last_updated", "")),
        )


@dataclass
class ModelProfileConfig:
    """Named model/runtime profile used by ``/model`` switching."""

    name: str
    model: str
    api_key: str
    provider: Optional[str] = None
    base_url: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.0
    max_context_tokens: int = 128_000
    preserve_reasoning_content: bool = True
    backfill_reasoning_content_for_tool_calls: bool = False
    reasoning_effort: Optional[str] = None
    thinking_enabled: Optional[bool] = None
    reasoning_replay_mode: Optional[str] = None
    reasoning_replay_placeholder: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary format for serialization."""
        return {
            "model": self.model,
            "api_key": self.api_key,
            "provider": self.provider,
            "base_url": self.base_url,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "max_context_tokens": self.max_context_tokens,
            "preserve_reasoning_content": self.preserve_reasoning_content,
            "backfill_reasoning_content_for_tool_calls": self.backfill_reasoning_content_for_tool_calls,
            "reasoning_effort": self.reasoning_effort,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_replay_mode": self.reasoning_replay_mode,
            "reasoning_replay_placeholder": self.reasoning_replay_placeholder,
        }

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "ModelProfileConfig":
        """Create from dictionary format."""
        return cls(
            name=name,
            model=d.get("model", "gpt-4o"),
            api_key=d.get("api_key", ""),
            provider=str(d["provider"]) if d.get("provider") is not None else None,
            base_url=d.get("base_url"),
            max_tokens=d.get("max_tokens", 4096),
            temperature=d.get("temperature", 0.0),
            max_context_tokens=d.get("max_context_tokens", 128_000),
            preserve_reasoning_content=d.get("preserve_reasoning_content", True),
            backfill_reasoning_content_for_tool_calls=d.get(
                "backfill_reasoning_content_for_tool_calls", False
            ),
            reasoning_effort=d.get("reasoning_effort"),
            thinking_enabled=d.get("thinking_enabled"),
            reasoning_replay_mode=d.get("reasoning_replay_mode"),
            reasoning_replay_placeholder=d.get("reasoning_replay_placeholder"),
        )


@dataclass
class ModeConfig:
    """Configuration for one agent mode."""

    name: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    prompt_append: str = ""

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "ModeConfig":
        """Create from dictionary format."""
        tools = d.get("tools", [])
        return cls(
            name=name,
            description=d.get("description", "") or "",
            tools=[str(t) for t in tools] if isinstance(tools, list) else [],
            prompt_append=d.get("prompt_append", "") or "",
        )


ApprovalAction = Literal["allow", "warn", "require_approval", "deny"]


@dataclass
class ApprovalRuleConfig:
    """User-configurable approval rule."""

    tool_name: Optional[str] = None
    tool_source: Optional[str] = None
    mcp_server: Optional[str] = None
    effect_class: Optional[str] = None
    profile: Optional[str] = None
    action: ApprovalAction = "require_approval"


@dataclass
class ApprovalConfig:
    """Approval policy configuration."""

    default_mode: ApprovalAction = "require_approval"
    rules: list[ApprovalRuleConfig] = field(default_factory=list)


@dataclass
class SkillsConfig:
    """Skills discovery/runtime configuration."""

    enabled: bool = True
    scan_project: bool = True
    scan_user: bool = True
    disabled: list[str] = field(default_factory=list)


@dataclass
class PromptConfig:
    """User/workspace prompt customization."""

    system_append: str = ""


@dataclass
class ContextConfig:
    """Context compression configuration."""

    snip_keep_recent_tools: int = 5
    snip_threshold_chars: int = 1500
    snip_min_lines: int = 6
    summarize_keep_recent_turns: int = 5
    token_fudge_factor: float = 1.1


@dataclass
class MemoryConfig:
    """Agent-scoped private memory provider configuration."""

    enabled: bool = True
    backend: str = "sqlite"
    store_path: str = ".rcoder/memory.sqlite3"
    default_agent_id: str = "core"
    default_namespace: str = ""
    token_budget: int = 800
    capture_enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MemoryConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=bool(data.get("enabled", True)),
            backend=str(data.get("backend", "sqlite") or "sqlite"),
            store_path=str(data.get("store_path", ".rcoder/memory.sqlite3") or ".rcoder/memory.sqlite3"),
            default_agent_id=str(data.get("default_agent_id", "core") or "core"),
            default_namespace=str(data.get("default_namespace", "") or ""),
            token_budget=int(data.get("token_budget", 800) or 800),
            capture_enabled=bool(data.get("capture_enabled", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "backend": self.backend,
            "store_path": self.store_path,
            "default_agent_id": self.default_agent_id,
            "default_namespace": self.default_namespace,
            "token_budget": self.token_budget,
            "capture_enabled": self.capture_enabled,
        }


@dataclass
class RemoteExecConfig:
    """Remote execution relay configuration."""

    enabled: bool = False
    host_mode: bool = False
    relay_bind: str = "127.0.0.1:8765"
    bootstrap_token_ttl_sec: int = 300
    peer_token_ttl_sec: int = 3600
    heartbeat_interval_sec: int = 10
    heartbeat_timeout_sec: int = 30
    default_tool_timeout_sec: int = 30
    shell_timeout_sec: int = 120


@dataclass
class AuthSuperadminConfig:
    """Configured superadmin account."""

    username: str = ""
    password: str = ""
    role: str = "superadmin"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AuthSuperadminConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(
            username=str(data.get("username", "") or ""),
            password=str(data.get("password", "") or ""),
            role=str(data.get("role", "superadmin") or "superadmin"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "password": self.password,
            "role": self.role,
        }


@dataclass
class AuthConfig:
    """Remote host user authentication settings."""

    enabled: bool = False
    token_secret: str = ""
    access_token_ttl_sec: int = 900
    refresh_token_ttl_sec: int = 2_592_000
    password_hash_iterations: int = 260_000
    password_min_length: int = 6
    password_max_length: int = 256
    login_rate_limit_count: int = 5
    login_rate_limit_window_sec: int = 900
    store_backend: str = "auto"
    store_path: str = ".rcoder/auth.json"
    superadmins: list[AuthSuperadminConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AuthConfig":
        if not isinstance(data, dict):
            return cls()
        raw_superadmins = data.get("superadmins", [])
        superadmins = (
            [
                AuthSuperadminConfig.from_dict(item)
                for item in raw_superadmins
                if isinstance(item, dict)
            ]
            if isinstance(raw_superadmins, list)
            else []
        )
        return cls(
            enabled=bool(data.get("enabled", False)),
            token_secret=str(data.get("token_secret", "") or ""),
            access_token_ttl_sec=int(data.get("access_token_ttl_sec", 900) or 900),
            refresh_token_ttl_sec=int(
                data.get("refresh_token_ttl_sec", 2_592_000) or 2_592_000
            ),
            password_hash_iterations=int(
                data.get("password_hash_iterations", 260_000) or 260_000
            ),
            password_min_length=int(data.get("password_min_length", 6) or 6),
            password_max_length=int(data.get("password_max_length", 256) or 256),
            login_rate_limit_count=int(data.get("login_rate_limit_count", 5) or 5),
            login_rate_limit_window_sec=int(
                data.get("login_rate_limit_window_sec", 900) or 900
            ),
            store_backend=str(data.get("store_backend", "auto") or "auto"),
            store_path=str(data.get("store_path", ".rcoder/auth.json") or ".rcoder/auth.json"),
            superadmins=superadmins,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "token_secret": self.token_secret,
            "access_token_ttl_sec": self.access_token_ttl_sec,
            "refresh_token_ttl_sec": self.refresh_token_ttl_sec,
            "password_hash_iterations": self.password_hash_iterations,
            "password_min_length": self.password_min_length,
            "password_max_length": self.password_max_length,
            "login_rate_limit_count": self.login_rate_limit_count,
            "login_rate_limit_window_sec": self.login_rate_limit_window_sec,
            "store_backend": self.store_backend,
            "store_path": self.store_path,
            "superadmins": [item.to_dict() for item in self.superadmins],
        }


@dataclass
class RunLimitsConfig:
    """Concurrency limits for interactive AgentRun and shell slots."""

    max_running_agents: int = 4
    max_shells_per_agent: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RunLimitsConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(
            max_running_agents=int(data.get("max_running_agents", 4) or 4),
            max_shells_per_agent=int(data.get("max_shells_per_agent", 1) or 1),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_running_agents": self.max_running_agents,
            "max_shells_per_agent": self.max_shells_per_agent,
        }


@dataclass
class RuntimeProfilesConfig:
    """Top-level runtime profile registry."""

    profiles: dict[str, RuntimeProfileConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RuntimeProfilesConfig":
        profiles = (
            {
                str(profile_id): RuntimeProfileConfig.from_dict(
                    str(profile_id), profile_data
                )
                for profile_id, profile_data in data.items()
                if isinstance(profile_data, dict)
            }
            if isinstance(data, dict)
            else {}
        )
        return cls(profiles=profiles)

    def to_dict(self) -> dict[str, Any]:
        return {
            profile_id: profile.to_dict()
            for profile_id, profile in self.profiles.items()
        }


@dataclass
class AgentRegistryConfig:
    """Persistent Agent registry."""

    agents: dict[str, AgentConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AgentRegistryConfig":
        raw_agents = data.get("agents", {}) if isinstance(data, dict) else {}
        agents = (
            {
                str(agent_id): AgentConfig.from_dict(str(agent_id), agent_data)
                for agent_id, agent_data in raw_agents.items()
                if isinstance(agent_data, dict)
            }
            if isinstance(raw_agents, dict)
            else {}
        )
        return cls(agents=agents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": {
                agent_id: agent.to_dict() for agent_id, agent in self.agents.items()
            }
        }


def build_agent_run_snapshot(
    *,
    agent_registry: AgentRegistryConfig,
    runtime_profiles: RuntimeProfilesConfig,
    run_limits: RunLimitsConfig,
    capability_packages: dict[str, CapabilityPackageConfig] | None = None,
) -> dict[str, Any]:
    """Return the server-authoritative AgentRun snapshot for executors."""

    packages = dict(capability_packages or {})
    if "environment" not in packages:
        packages["environment"] = CapabilityPackageConfig.from_dict(
            "environment",
            DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE,
        )
    agents: dict[str, dict[str, Any]] = {}
    for agent_id, agent in agent_registry.agents.items():
        agent_dict = agent.to_dict()
        agent_dict["resolved_capabilities"] = resolve_capability_refs(
            agent.capability_refs,
            packages,
        )
        agents[agent_id] = agent_dict
    return {
        "max_running_agents": run_limits.max_running_agents,
        "max_shells_per_agent": run_limits.max_shells_per_agent,
        "runtime_profiles": runtime_profiles.to_dict(),
        "agents": agents,
        "capability_packages": {
            package_id: package.to_dict()
            for package_id, package in packages.items()
        },
    }


@dataclass
class PersistenceConfig:
    """Durable persistence settings for runtime and session state."""

    backend: str = "auto"
    database_url: str = ""
    auto_migrate: bool = True
    runtime_enabled: bool = True
    sessions_enabled: bool = True
    retention_days: int = 0
    snapshot_max_versions_per_session: int = 20
    snapshot_compress_threshold_bytes: int = 262144
    maintenance_interval_sec: int = 3600

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PersistenceConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(
            backend=str(data.get("backend", "auto") or "auto"),
            database_url=str(data.get("database_url", "") or ""),
            auto_migrate=bool(data.get("auto_migrate", True)),
            runtime_enabled=bool(data.get("runtime_enabled", True)),
            sessions_enabled=bool(data.get("sessions_enabled", True)),
            retention_days=int(data.get("retention_days", 0) or 0),
            snapshot_max_versions_per_session=int(
                data.get("snapshot_max_versions_per_session", 20) or 20
            ),
            snapshot_compress_threshold_bytes=int(
                data.get("snapshot_compress_threshold_bytes", 262144) or 262144
            ),
            maintenance_interval_sec=int(
                data.get("maintenance_interval_sec", 3600) or 3600
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "database_url": self.database_url,
            "auto_migrate": self.auto_migrate,
            "runtime_enabled": self.runtime_enabled,
            "sessions_enabled": self.sessions_enabled,
            "retention_days": self.retention_days,
            "snapshot_max_versions_per_session": self.snapshot_max_versions_per_session,
            "snapshot_compress_threshold_bytes": self.snapshot_compress_threshold_bytes,
            "maintenance_interval_sec": self.maintenance_interval_sec,
        }


@dataclass
class ToolArgumentValidationDiagnosticsConfig:
    """Tool argument validation telemetry settings."""

    enabled: bool = True
    record_clean: bool = False

    @classmethod
    def from_dict(
        cls, data: dict[str, Any] | None
    ) -> "ToolArgumentValidationDiagnosticsConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=bool(data.get("enabled", True)),
            record_clean=bool(data.get("record_clean", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "record_clean": self.record_clean,
        }


@dataclass
class DiagnosticsConfig:
    """Diagnostics and telemetry settings."""

    tool_argument_validation: ToolArgumentValidationDiagnosticsConfig = field(
        default_factory=ToolArgumentValidationDiagnosticsConfig
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DiagnosticsConfig":
        raw = data.get("tool_argument_validation", {}) if isinstance(data, dict) else {}
        return cls(
            tool_argument_validation=ToolArgumentValidationDiagnosticsConfig.from_dict(
                raw if isinstance(raw, dict) else {}
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_argument_validation": self.tool_argument_validation.to_dict(),
        }


@dataclass
class SandboxProviderConfig:
    """Execution-room provider used for on-demand AgentRun sessions."""

    type: str = "none"
    host_base_url: str = ""
    worker_image: str = "labrastro-host:test"
    workspace_volume_root: str = "ezcode-workspaces"
    network: str = ""
    cpu_limit: str = ""
    memory_limit: str = ""
    idle_ttl_seconds: int = 3600

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SandboxProviderConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(
            type=str(data.get("type", "none") or "none"),
            host_base_url=str(data.get("host_base_url", "") or ""),
            worker_image=str(
                data.get("worker_image", "labrastro-host:test")
                or "labrastro-host:test"
            ),
            workspace_volume_root=str(
                data.get("workspace_volume_root", "ezcode-workspaces")
                or "ezcode-workspaces"
            ),
            network=str(data.get("network", "") or ""),
            cpu_limit=str(data.get("cpu_limit", "") or ""),
            memory_limit=str(data.get("memory_limit", "") or ""),
            idle_ttl_seconds=int(data.get("idle_ttl_seconds", 3600) or 3600),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "type": self.type,
            "host_base_url": self.host_base_url,
            "worker_image": self.worker_image,
            "workspace_volume_root": self.workspace_volume_root,
            "idle_ttl_seconds": self.idle_ttl_seconds,
        }
        if self.network:
            data["network"] = self.network
        if self.cpu_limit:
            data["cpu_limit"] = self.cpu_limit
        if self.memory_limit:
            data["memory_limit"] = self.memory_limit
        return data


@dataclass
class GitHubConfig:
    """GitHub App integration settings for PR lifecycle management."""

    enabled: bool = False
    app_id: str = ""
    installation_id: str = ""
    private_key_path: str = ""
    webhook_secret: str = ""
    api_base_url: str = "https://api.github.com"
    web_base_url: str = "https://github.com"
    reconcile_interval_sec: int = 300

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GitHubConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=bool(data.get("enabled", False)),
            app_id=str(data.get("app_id", "") or ""),
            installation_id=str(data.get("installation_id", "") or ""),
            private_key_path=str(data.get("private_key_path", "") or ""),
            webhook_secret=str(data.get("webhook_secret", "") or ""),
            api_base_url=str(data.get("api_base_url", "https://api.github.com") or ""),
            web_base_url=str(data.get("web_base_url", "https://github.com") or ""),
            reconcile_interval_sec=int(data.get("reconcile_interval_sec", 300) or 300),
        )

    def to_dict(self, *, mask_secret: bool = False) -> dict[str, Any]:
        data = {
            "enabled": self.enabled,
            "app_id": self.app_id,
            "installation_id": self.installation_id,
            "private_key_path": self.private_key_path,
            "api_base_url": self.api_base_url,
            "web_base_url": self.web_base_url,
            "reconcile_interval_sec": self.reconcile_interval_sec,
        }
        if mask_secret:
            data["webhook_secret_hint"] = _mask_secret_hint(self.webhook_secret)
        else:
            data["webhook_secret"] = self.webhook_secret
        return data


def _mask_secret_hint(value: str) -> str:
    if not value:
        return "(empty)"
    if value.startswith("${") and value.endswith("}"):
        return value
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


@dataclass
class EnvironmentCLIToolConfig:
    """Declarative CLI tool entry consumed by environment-capable Agents."""

    name: str
    command: str = ""
    enabled: bool = True
    placement: CLIPlacement = "local"
    tags: list[str] = field(default_factory=list)
    requirements: dict[str, str] = field(default_factory=dict)
    check: str = ""
    install: str = ""
    version: Optional[str] = None
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
            "command": self.command,
            "enabled": self.enabled,
            "placement": self.placement,
            "tags": list(self.tags),
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
    def from_dict(cls, name: str, d: dict[str, Any]) -> "EnvironmentCLIToolConfig":
        if "capabilities" in d:
            raise ValueError(
                "environment.cli_tools capabilities was removed; use tags for component labeling"
            )
        raw_tags = d.get("tags", [])
        raw_requirements = d.get("requirements", {})
        raw_placement = str(d.get("placement", "local")).lower()
        placement: CLIPlacement
        if raw_placement in {"server", "both"}:
            placement = raw_placement  # type: ignore[assignment]
        else:
            placement = "local"
        return cls(
            name=name,
            command=str(d.get("command", "")),
            enabled=_bool_config_value(d.get("enabled", True)),
            placement=placement,
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
            docs=_docs_config_value(d.get("docs", [])),
            evidence=_string_dict_list_config_value(d.get("evidence", [])),
            install_prompt=str(d.get("install_prompt", "")),
            verify_prompt=str(d.get("verify_prompt", "")),
            notes=_string_list_config_value(d.get("notes", [])),
            credentials=_string_list_config_value(d.get("credentials", [])),
            risk_level=str(d.get("risk_level", "")),
            last_action=str(d.get("last_action", "")),
            last_updated=str(d.get("last_updated", "")),
        )


@dataclass
class EnvironmentSkillConfig:
    """Declarative skill entry consumed by environment-capable Agents."""

    name: str
    enabled: bool = True
    scope: str = "project"
    check: str = ""
    install: str = ""
    version: Optional[str] = None
    source: str = ""
    description: str = ""
    path_hint: Optional[str] = None
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
    def from_dict(cls, name: str, d: dict[str, Any]) -> "EnvironmentSkillConfig":
        raw_requirements = d.get("requirements", {})
        return cls(
            name=name,
            enabled=_bool_config_value(d.get("enabled", True)),
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
            docs=_docs_config_value(d.get("docs", [])),
            evidence=_string_dict_list_config_value(d.get("evidence", [])),
            install_prompt=str(d.get("install_prompt", "")),
            verify_prompt=str(d.get("verify_prompt", "")),
            notes=_string_list_config_value(d.get("notes", [])),
            credentials=_string_list_config_value(d.get("credentials", [])),
            risk_level=str(d.get("risk_level", "")),
            last_action=str(d.get("last_action", "")),
            last_updated=str(d.get("last_updated", "")),
        )


@dataclass
class EnvironmentConfig:
    """Server-authoritative lightweight CLI environment manifest."""

    cli_tools: dict[str, EnvironmentCLIToolConfig] = field(default_factory=dict)
    skills: dict[str, EnvironmentSkillConfig] = field(default_factory=dict)


def _bool_config_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _string_list_config_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None or value == "":
        return []
    return [str(value)]


def _docs_config_value(value: Any) -> list[dict[str, str]]:
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


def _string_dict_list_config_value(value: Any) -> list[dict[str, str]]:
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
class Config:
    """Main configuration model for ReuleauxCoder."""

    model: str = "gpt-4o"
    api_key: str = ""
    base_url: Optional[str] = None
    max_tokens: int = 4096
    temperature: float = 0.0
    max_context_tokens: int = 128_000
    preserve_reasoning_content: bool = True
    backfill_reasoning_content_for_tool_calls: bool = False
    reasoning_effort: Optional[str] = None
    thinking_enabled: Optional[bool] = None
    reasoning_replay_mode: Optional[str] = None
    reasoning_replay_placeholder: Optional[str] = None
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    mcp_artifact_root: str = ".rcoder/mcp-artifacts"
    model_profiles: dict[str, ModelProfileConfig] = field(default_factory=dict)
    providers: ProvidersConfig = field(default_factory=ProvidersConfig)
    active_main_model_profile: Optional[str] = None
    active_sub_model_profile: Optional[str] = None

    # Mode settings
    modes: dict[str, ModeConfig] = field(default_factory=dict)
    active_mode: Optional[str] = None

    # Tool output settings
    tool_output_max_chars: int = 12_000
    tool_output_max_lines: int = 120
    tool_output_store_full: bool = True
    tool_output_store_dir: Optional[str] = None

    # Session settings
    session_auto_save: bool = True
    session_dir: Optional[str] = None

    # CLI settings
    history_file: Optional[str] = None
    llm_debug_trace: bool = False

    # Approval settings
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)

    # Skills settings
    skills: SkillsConfig = field(default_factory=SkillsConfig)

    # Prompt settings
    prompt: PromptConfig = field(default_factory=PromptConfig)

    # Context compression settings
    context: ContextConfig = field(default_factory=ContextConfig)

    # Agent-scoped private memory settings
    memory: MemoryConfig = field(default_factory=MemoryConfig)

    # Remote execution settings
    remote_exec: RemoteExecConfig = field(default_factory=RemoteExecConfig)

    # Remote host authentication settings
    auth: AuthConfig = field(default_factory=AuthConfig)

    # Persistent Agent definitions, runtime profiles, and AgentRun limits.
    agent_registry: AgentRegistryConfig = field(default_factory=AgentRegistryConfig)
    runtime_profiles: RuntimeProfilesConfig = field(default_factory=RuntimeProfilesConfig)
    run_limits: RunLimitsConfig = field(default_factory=RunLimitsConfig)

    # Reusable capability packages installed from local config or a future market.
    capability_packages: dict[str, CapabilityPackageConfig] = field(default_factory=dict)

    # Durable persistence settings
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)

    # Diagnostics and telemetry settings
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)

    # On-demand execution-room provider.
    sandbox_provider: SandboxProviderConfig = field(default_factory=SandboxProviderConfig)

    # GitHub App pull request lifecycle settings
    github: GitHubConfig = field(default_factory=GitHubConfig)

    # Server-authoritative environment manifest
    environment: EnvironmentConfig = field(default_factory=EnvironmentConfig)

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors."""
        errors = []
        is_remote_host_mode = self.remote_exec.enabled and self.remote_exec.host_mode
        has_provider_backed_key = any(
            provider.api_key for provider in self.providers.items.values()
        )
        if not is_remote_host_mode and not self.api_key and not has_provider_backed_key:
            errors.append("api_key is required")
        if self.max_tokens < 1:
            errors.append("max_tokens must be positive")
        if self.temperature < 0 or self.temperature > 2:
            errors.append("temperature must be between 0 and 2")
        if self.tool_output_max_chars < 1:
            errors.append("tool_output_max_chars must be positive")
        if self.tool_output_max_lines < 1:
            errors.append("tool_output_max_lines must be positive")
        if self.memory.backend not in {"sqlite", "postgres", "memory"}:
            errors.append("memory.backend must be one of sqlite, postgres, memory")
        if self.memory.enabled and not self.memory.default_agent_id.strip():
            errors.append("memory.default_agent_id is required when memory.enabled is true")
        if (
            self.memory.enabled
            and self.memory.backend == "postgres"
            and not self.persistence.database_url
        ):
            errors.append("memory.backend postgres requires persistence.database_url")
        if self.memory.token_budget < 1:
            errors.append("memory.token_budget must be positive")
        if self.run_limits.max_running_agents < 1:
            errors.append("run_limits.max_running_agents must be positive")
        if self.run_limits.max_shells_per_agent < 1:
            errors.append("run_limits.max_shells_per_agent must be positive")
        if self.remote_exec.enabled and self.remote_exec.host_mode:
            if not self.auth.enabled:
                errors.append("auth.enabled is required for remote host mode")
            if not self.auth.superadmins:
                errors.append("auth.superadmins is required for remote host mode")
        if self.auth.enabled:
            if not self.auth.token_secret:
                errors.append("auth.token_secret is required when auth.enabled is true")
            if self.auth.access_token_ttl_sec < 1:
                errors.append("auth.access_token_ttl_sec must be positive")
            if self.auth.refresh_token_ttl_sec < 1:
                errors.append("auth.refresh_token_ttl_sec must be positive")
            if self.auth.password_hash_iterations < 1:
                errors.append("auth.password_hash_iterations must be positive")
            if self.auth.password_min_length < 1:
                errors.append("auth.password_min_length must be positive")
            if self.auth.password_max_length < self.auth.password_min_length:
                errors.append("auth.password_max_length must be >= auth.password_min_length")
            if self.auth.login_rate_limit_count < 1:
                errors.append("auth.login_rate_limit_count must be positive")
            if self.auth.login_rate_limit_window_sec < 1:
                errors.append("auth.login_rate_limit_window_sec must be positive")
            if self.auth.store_backend not in {"auto", "file", "postgres"}:
                errors.append("auth.store_backend must be one of auto, file, postgres")
            for superadmin in self.auth.superadmins:
                if not superadmin.username.strip():
                    errors.append("auth.superadmins.username is required")
                if not superadmin.password:
                    errors.append("auth.superadmins.password is required")
                if superadmin.role != "superadmin":
                    errors.append("auth.superadmins entries must use role superadmin")
        valid_persistence_backends = {"auto", "memory", "postgres"}
        if self.persistence.backend not in valid_persistence_backends:
            errors.append("persistence.backend must be one of auto, memory, postgres")
        if self.persistence.backend == "postgres" and not self.persistence.database_url:
            errors.append("persistence.database_url is required when backend is postgres")
        if self.persistence.retention_days < 0:
            errors.append("persistence.retention_days must be zero or positive")
        if self.persistence.snapshot_max_versions_per_session < 1:
            errors.append(
                "persistence.snapshot_max_versions_per_session must be positive"
            )
        if self.persistence.snapshot_compress_threshold_bytes < 1:
            errors.append(
                "persistence.snapshot_compress_threshold_bytes must be positive"
            )
        if self.persistence.maintenance_interval_sec < 1:
            errors.append("persistence.maintenance_interval_sec must be positive")
        if self.sandbox_provider.type not in {"docker", "external", "k8s", "none"}:
            errors.append("sandbox_provider.type must be one of docker, external, k8s, none")
        if self.sandbox_provider.idle_ttl_seconds < 1:
            errors.append("sandbox_provider.idle_ttl_seconds must be positive")
        if self.sandbox_provider.type == "docker" and not self.sandbox_provider.host_base_url:
            errors.append(
                "sandbox_provider.host_base_url is required when sandbox_provider.type is docker"
            )
        if self.github.enabled:
            if self.persistence.backend == "memory" or not self.persistence.database_url:
                errors.append(
                    "github.enabled requires Postgres persistence.database_url"
                )
            if not self.github.app_id:
                errors.append("github.app_id is required when github.enabled is true")
            if not self.github.installation_id:
                errors.append(
                    "github.installation_id is required when github.enabled is true"
                )
            if not self.github.private_key_path:
                errors.append(
                    "github.private_key_path is required when github.enabled is true"
                )
            if not self.github.webhook_secret:
                errors.append(
                    "github.webhook_secret is required when github.enabled is true"
                )
            if self.github.reconcile_interval_sec < 1:
                errors.append("github.reconcile_interval_sec must be positive")
        capability_packages = dict(self.capability_packages)
        if "environment" not in capability_packages:
            capability_packages["environment"] = CapabilityPackageConfig.from_dict(
                "environment",
                DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE,
            )
        for agent_id, agent in self.agent_registry.agents.items():
            if (
                agent.runtime_profile
                and agent.runtime_profile not in self.runtime_profiles.profiles
            ):
                errors.append(
                    f"agent_registry.agents[{agent_id}].runtime_profile must exist in runtime_profiles"
                )
            if agent.model.provider and agent.model.provider not in self.providers.items:
                errors.append(
                    f"agent_registry.agents[{agent_id}].model.provider must exist in providers.items"
                )
            if agent.model.provider and not agent.model.model:
                errors.append(
                    f"agent_registry.agents[{agent_id}].model.model is required when provider is set"
                )
            for package_ref in agent.capability_refs:
                if package_ref not in capability_packages:
                    errors.append(
                        f"agent_registry.agents[{agent_id}].capability_refs references missing capability package {package_ref}"
                    )
        mcp_names = {server.name for server in self.mcp_servers}
        cli_tool_names = set(self.environment.cli_tools)
        skill_names = set(self.environment.skills)
        for package_id, package in capability_packages.items():
            for server_name in package.mcp_servers:
                if server_name not in mcp_names:
                    errors.append(
                        f"capability_packages[{package_id}].mcp_servers references missing mcp.servers entry {server_name}"
                    )
            for tool_name in package.cli_tools:
                if tool_name not in cli_tool_names:
                    errors.append(
                        f"capability_packages[{package_id}].cli_tools references missing environment.cli_tools entry {tool_name}"
                    )
            for skill_name in package.skills:
                if skill_name not in skill_names:
                    errors.append(
                        f"capability_packages[{package_id}].skills references missing environment.skills entry {skill_name}"
                    )
        valid_actions = {"allow", "warn", "require_approval", "deny"}
        if (
            self.active_main_model_profile
            and self.active_main_model_profile not in self.model_profiles
        ):
            errors.append("active_main_model_profile must exist in model_profiles")
        if (
            self.active_sub_model_profile
            and self.active_sub_model_profile not in self.model_profiles
        ):
            errors.append("active_sub_model_profile must exist in model_profiles")
        for name, profile in self.model_profiles.items():
            if profile.provider:
                if profile.provider not in self.providers.items:
                    errors.append(
                        f"model_profiles[{name}].provider must exist in providers.items"
                    )
            elif not profile.api_key:
                errors.append(f"model_profiles[{name}].api_key is required")
            if profile.max_tokens < 1:
                errors.append(f"model_profiles[{name}].max_tokens must be positive")
            if profile.max_context_tokens < 1:
                errors.append(
                    f"model_profiles[{name}].max_context_tokens must be positive"
                )
            if profile.temperature < 0 or profile.temperature > 2:
                errors.append(
                    f"model_profiles[{name}].temperature must be between 0 and 2"
                )

        for provider_id, provider in self.providers.items.items():
            if provider.compat not in SUPPORTED_PROVIDER_COMPATS:
                errors.append(
                    f"providers.items[{provider_id}].compat must be one of deepseek, generic, glm, kimi, qwen, zenmux"
                )
            if provider.type not in {
                "openai_chat",
                "anthropic_messages",
                "openai_responses",
            }:
                errors.append(
                    f"providers.items[{provider_id}].type must be one of openai_chat, anthropic_messages, openai_responses"
                )
            if provider.timeout_sec < 1:
                errors.append(
                    f"providers.items[{provider_id}].timeout_sec must be positive"
                )
            if provider.max_retries < 0:
                errors.append(
                    f"providers.items[{provider_id}].max_retries must be non-negative"
                )

        if self.active_mode and self.active_mode not in self.modes:
            errors.append("active_mode must exist in modes")
        for mode_name, mode in self.modes.items():
            if not mode.name:
                errors.append(f"modes[{mode_name}] must have a name")

        if self.approval.default_mode not in valid_actions:
            errors.append(
                "approval.default_mode must be one of allow, warn, require_approval, deny"
            )
        for i, rule in enumerate(self.approval.rules):
            if rule.action not in valid_actions:
                errors.append(
                    f"approval.rules[{i}].action must be one of allow, warn, require_approval, deny"
                )
        return errors

    def is_valid(self) -> bool:
        """Check if configuration is valid."""
        return len(self.validate()) == 0

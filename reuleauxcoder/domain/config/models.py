"""Configuration models - domain layer configuration abstractions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
    CapabilityComponentConfig,
    CapabilityPackageConfig,
    RuntimeProfileConfig,
    resolve_capability_refs,
)
from reuleauxcoder.domain.capability_packages import (
    capability_package_is_active,
    capability_package_state_projection,
)
from reuleauxcoder.domain.environment_requirements import (
    EnvironmentPlacement,
    environment_requirement_name_from_id,
    normalize_environment_placement,
    normalize_environment_requirement_id,
    resolve_environment_requirement_kind,
)
from reuleauxcoder.domain.memory.registry import (
    MemoryProviderRegistry,
    MemorySourceRegistry,
)
from reuleauxcoder.domain.hooks.lifecycle import lifecycle_declarations_from_config_hooks
from reuleauxcoder.domain.runtime_footprint import (
    normalize_runtime_footprint,
    runtime_footprint_for_environment_requirement,
    runtime_footprint_for_mcp,
    runtime_footprint_for_skill,
    runs_on_to_environment_placement,
    runs_on_to_mcp_placement,
)


MCPPlacement = Literal["server", "peer", "both"]
MCPDistribution = Literal["command", "artifact"]
ProviderType = Literal[
    "openai_chat",
    "anthropic_messages",
    "openai_responses",
    "labrastro_server",
]
ProviderCompat = Literal["generic", "deepseek", "kimi", "glm", "qwen", "zenmux"]

SUPPORTED_PROVIDER_COMPATS = {"generic", "deepseek", "kimi", "glm", "qwen", "zenmux"}
PROVIDER_CONFIG_FIELDS: tuple[str, ...] = (
    "type",
    "compat",
    "enabled",
    "api_key",
    "base_url",
    "headers",
    "timeout_sec",
    "max_retries",
    "api_features",
    "stream_recovery",
    "extra",
)
MODEL_PROFILE_CONFIG_FIELDS: tuple[str, ...] = (
    "model",
    "provider",
    "max_tokens",
    "temperature",
    "max_context_tokens",
    "preserve_reasoning_content",
    "backfill_reasoning_content_for_tool_calls",
    "reasoning_effort",
    "thinking_enabled",
    "reasoning_replay_mode",
    "reasoning_replay_placeholder",
    "capability_user_configured",
    "capability_source",
    "capability_applied_at",
)
MODEL_PROFILE_ADMIN_INPUT_FIELDS: tuple[str, ...] = (
    "id",
    "profile_id",
    *(
        field
        for field in MODEL_PROFILE_CONFIG_FIELDS
        if field not in {"capability_source", "capability_applied_at"}
    ),
)
DIAGNOSTICS_CONFIG_FIELDS: tuple[str, ...] = ("tool_diagnostics", "llm_trace")
LLM_TRACE_DIAGNOSTICS_CONFIG_FIELDS: tuple[str, ...] = ("enabled", "raw_chunks")
TOOL_DIAGNOSTICS_CONFIG_FIELDS: tuple[str, ...] = ("enabled", "record_clean")

DEFAULT_ENVIRONMENT_RUNTIME_PROFILE_ID = "environment_local"
DEFAULT_USER_RUNTIME_PROFILE_ID = "agent_remote"
DEFAULT_MAIN_CHAT_AGENT_ID = "main_chat"
DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE_ID = "environment"
DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID = "core_builtin_tools"
DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID = (
    "capability_packager_builtin_tools"
)
BUILTIN_CAPABILITY_PACKAGE_IDS = frozenset(
    {
        DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE_ID,
        DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID,
        DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID,
    }
)
DEFAULT_ENVIRONMENT_AGENT_ID = "environment_configurator"
DEFAULT_CAPABILITY_PACKAGER_AGENT_ID = "capability_packager"
DEFAULT_CAPABILITY_PACKAGER_RUNTIME_PROFILE_ID = "capability_packager_remote"
DEFAULT_ENVIRONMENT_RUNTIME_PROFILE: dict[str, Any] = {
    "executor": "reuleauxcoder",
    "execution_location": "local_workspace",
    "worker_kind": "local_peer",
    "model_request_origin": "server",
    "worktree_role": "target",
    "publish_policy": "never",
    "runtime_home_policy": "per_task",
    "approval_mode": "full",
}
DEFAULT_USER_RUNTIME_PROFILE: dict[str, Any] = {
    "executor": "reuleauxcoder",
    "execution_location": "remote_server",
    "worker_kind": "server_worker",
    "model_request_origin": "server",
    "worktree_role": "target",
    "publish_policy": "never",
    "runtime_home_policy": "per_task",
    "approval_mode": "full",
}
DEFAULT_CAPABILITY_PACKAGER_RUNTIME_PROFILE: dict[str, Any] = {
    "executor": "reuleauxcoder",
    "execution_location": "remote_server",
    "worker_kind": "sandbox_worker",
    "model_request_origin": "server",
    "worktree_role": "source",
    "publish_policy": "never",
    "runtime_home_policy": "per_task",
    "approval_mode": "full",
    "timeout_sec": 86_400,
    "step_timeout_sec": 3_600,
    "sandbox": {},
}
DEFAULT_MAIN_CHAT_AGENT: dict[str, Any] = {
    "name": "Main Chat Agent",
    "description": "Direct ChatView entrypoint agent for user conversation, slash commands, mentions, and confirmations.",
    "role": "main_chat",
    "visibility": "user",
    "chat_entrypoint": True,
    "delegable": False,
    "taskflow_eligible": False,
    "dispatch": {
        "profile": "Directly handles the user-facing ChatView conversation and delegates work to configured worker Agents when needed.",
        "examples": [
            "Answer the user in the ChatView",
            "Use registered chat commands and reference-only mentions",
            "Delegate bounded background work to a worker Agent",
        ],
        "avoid": [
            "Being selected as a delegated worker Agent",
            "Running as an unattended taskflow worker",
        ],
    },
    "capability_refs": [DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID],
}
DEFAULT_ENVIRONMENT_AGENT: dict[str, Any] = {
    "name": "Environment Configurator",
    "description": "Checks and configures the local workspace environment from the server manifest.",
    "role": "environment",
    "visibility": "system",
    "delegable": False,
    "taskflow_eligible": False,
    "system_flow_only": ["environment_config"],
    "runtime_profile": DEFAULT_ENVIRONMENT_RUNTIME_PROFILE_ID,
    "dispatch": {
        "profile": (
            "Best for reading the environment manifest and checking or configuring "
            "local workspace environment requirements from that manifest."
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
DEFAULT_CAPABILITY_PACKAGER_AGENT: dict[str, Any] = {
    "name": "Capability Packager",
    "description": "Reads repositories and documentation to generate reviewable capability package drafts.",
    "role": "capability_packager",
    "visibility": "internal",
    "delegable": False,
    "taskflow_eligible": False,
    "system_flow_only": ["capability_ingest"],
    "runtime_profile": DEFAULT_CAPABILITY_PACKAGER_RUNTIME_PROFILE_ID,
    "dispatch": {
        "profile": (
            "Best for analyzing README, docs, and project notes to extract skills, "
            "MCP servers, and environment requirements into a structured draft."
        ),
        "examples": [
            "Generate a capability package draft from a GitHub repository README",
            "Extract MCP server launch and install instructions from official docs",
        ],
        "avoid": [
            "Installing tools or mutating the workspace during discovery",
        ],
    },
    "capability_refs": [DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID],
}
DEFAULT_BUILTIN_TOOL_COMPONENTS: dict[str, dict[str, Any]] = {
    "builtin_tool:delegate_agent": {
        "kind": "builtin_tool",
        "name": "delegate_agent",
        "description": "Delegate bounded background work to a configured worker Agent.",
        "access": "write",
        "risk_level": "medium",
        "execution_policy": "inherit",
        "registry_path": "builtin:delegate_agent",
        "package_ids": [DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID],
    },
    "builtin_tool:edit_file": {
        "kind": "builtin_tool",
        "name": "edit_file",
        "description": "Edit files in the active workspace.",
        "access": "write",
        "risk_level": "medium",
        "execution_policy": "inherit",
        "registry_path": "builtin:edit_file",
        "package_ids": [DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID],
    },
    "builtin_tool:fetch_capabilities": {
        "kind": "builtin_tool",
        "name": "fetch_capabilities",
        "description": "Read-only source fetcher for capability package evidence.",
        "access": "read",
        "risk_level": "low",
        "execution_policy": "allow",
        "registry_path": "builtin:fetch_capabilities",
        "package_ids": [
            DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID,
            DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID,
        ],
    },
    "builtin_tool:glob": {
        "kind": "builtin_tool",
        "name": "glob",
        "description": "Find files by glob pattern.",
        "access": "read",
        "risk_level": "low",
        "execution_policy": "allow",
        "registry_path": "builtin:glob",
        "package_ids": [
            DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID,
            DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID,
        ],
    },
    "builtin_tool:grep": {
        "kind": "builtin_tool",
        "name": "grep",
        "description": "Search workspace text by pattern.",
        "access": "read",
        "risk_level": "low",
        "execution_policy": "allow",
        "registry_path": "builtin:grep",
        "package_ids": [
            DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID,
            DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID,
        ],
    },
    "builtin_tool:list_file": {
        "kind": "builtin_tool",
        "name": "list_file",
        "description": "List files and directories.",
        "access": "read",
        "risk_level": "low",
        "execution_policy": "allow",
        "registry_path": "builtin:list_file",
        "package_ids": [
            DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID,
            DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID,
        ],
    },
    "builtin_tool:lsp": {
        "kind": "builtin_tool",
        "name": "lsp",
        "description": "Query language server information.",
        "access": "read",
        "risk_level": "low",
        "execution_policy": "allow",
        "registry_path": "builtin:lsp",
        "package_ids": [DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID],
    },
    "builtin_tool:read_file": {
        "kind": "builtin_tool",
        "name": "read_file",
        "description": "Read workspace file contents.",
        "access": "read",
        "risk_level": "low",
        "execution_policy": "allow",
        "registry_path": "builtin:read_file",
        "package_ids": [
            DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID,
            DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID,
        ],
    },
    "builtin_tool:shell": {
        "kind": "builtin_tool",
        "name": "shell",
        "description": "Run shell commands in the active workspace.",
        "access": "both",
        "risk_level": "high",
        "execution_policy": "inherit",
        "registry_path": "builtin:shell",
        "package_ids": [
            DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID,
            DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE_ID,
        ],
    },
    "builtin_tool:write_file": {
        "kind": "builtin_tool",
        "name": "write_file",
        "description": "Write files in the active workspace.",
        "access": "write",
        "risk_level": "medium",
        "execution_policy": "inherit",
        "registry_path": "builtin:write_file",
        "package_ids": [DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID],
    },
}
BUILTIN_TOOL_COMPONENT_FORCED_FIELDS: tuple[str, ...] = (
    "kind",
    "name",
    "description",
    "access",
    "risk_level",
    "execution_policy",
    "registry_path",
)
DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE: dict[str, Any] = {
    "name": "Environment Tools",
    "description": "Read and configure the local workspace environment manifest.",
    "source": {"type": "builtin"},
    "components": ["builtin_tool:shell"],
    "usage": [
        "Mounted by the environment_configurator system Agent to run manifest-defined check, install, and configure commands.",
    ],
    "effective_capabilities": [
        "Allows environment checks and configuration through the shell tool only.",
    ],
    "risk_level": "high",
    "execution_policy": "inherit",
    "generated_by": "system",
}
DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE: dict[str, Any] = {
    "name": "Core Built-in Tools",
    "description": "Built-in tools available to the main ChatView Agent through explicit capability_refs authorization.",
    "source": {"type": "builtin"},
    "components": sorted(DEFAULT_BUILTIN_TOOL_COMPONENTS),
    "usage": [
        "Mounted by the main_chat Agent so ChatView tools are authorized through capability_refs.",
        "Runtime filters builtin tools against the resolved effective_capabilities result.",
    ],
    "effective_capabilities": [
        "Allows the main ChatView Agent to use registered built-in tools through capability_refs.",
        "Keeps tool availability explicit so runtime enforcement can filter unregistered tools.",
    ],
    "risk_level": "high",
    "execution_policy": "inherit",
    "generated_by": "system",
}
DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE: dict[str, Any] = {
    "name": "Capability Packager Built-in Tools",
    "description": "Read-only built-in tools used by the capability packager system Agent.",
    "source": {"type": "builtin"},
    "components": [
        "builtin_tool:fetch_capabilities",
        "builtin_tool:glob",
        "builtin_tool:grep",
        "builtin_tool:list_file",
        "builtin_tool:read_file",
    ],
    "usage": [
        "Mounted by the capability_packager Agent so package discovery can read sources without mutating the workspace.",
    ],
    "effective_capabilities": [
        "Allows read-only source collection and local file inspection for capability package draft generation.",
    ],
    "risk_level": "low",
    "execution_policy": "allow",
    "generated_by": "system",
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
    user_profile = profiles.get(DEFAULT_USER_RUNTIME_PROFILE_ID)
    if not isinstance(user_profile, dict):
        profiles[DEFAULT_USER_RUNTIME_PROFILE_ID] = deepcopy(
            DEFAULT_USER_RUNTIME_PROFILE
        )
    else:
        for key, value in DEFAULT_USER_RUNTIME_PROFILE.items():
            user_profile.setdefault(key, deepcopy(value))
    packager_profile = profiles.get(DEFAULT_CAPABILITY_PACKAGER_RUNTIME_PROFILE_ID)
    if not isinstance(packager_profile, dict):
        profiles[DEFAULT_CAPABILITY_PACKAGER_RUNTIME_PROFILE_ID] = deepcopy(
            DEFAULT_CAPABILITY_PACKAGER_RUNTIME_PROFILE
        )
    else:
        forced_packager_keys = {
            "executor",
            "execution_location",
            "worker_kind",
            "model_request_origin",
            "worktree_role",
            "publish_policy",
            "runtime_home_policy",
            "approval_mode",
            "sandbox",
        }
        for key, value in DEFAULT_CAPABILITY_PACKAGER_RUNTIME_PROFILE.items():
            if key in forced_packager_keys:
                packager_profile[key] = deepcopy(value)
            else:
                packager_profile.setdefault(key, deepcopy(value))

    raw_agents = registry.get("agents")
    agents = raw_agents if isinstance(raw_agents, dict) else {}
    registry["agents"] = agents
    main_agent = agents.get(DEFAULT_MAIN_CHAT_AGENT_ID)
    if not isinstance(main_agent, dict):
        agents[DEFAULT_MAIN_CHAT_AGENT_ID] = deepcopy(DEFAULT_MAIN_CHAT_AGENT)
        main_agent = agents[DEFAULT_MAIN_CHAT_AGENT_ID]
    else:
        for key, value in DEFAULT_MAIN_CHAT_AGENT.items():
            if key not in main_agent:
                main_agent[key] = deepcopy(value)
        if not isinstance(main_agent.get("dispatch"), dict):
            main_agent["dispatch"] = deepcopy(DEFAULT_MAIN_CHAT_AGENT["dispatch"])
    if not isinstance(main_agent.get("capability_refs"), list):
        main_agent["capability_refs"] = list(DEFAULT_MAIN_CHAT_AGENT["capability_refs"])
    agent = agents.get(DEFAULT_ENVIRONMENT_AGENT_ID)
    if not isinstance(agent, dict):
        agents[DEFAULT_ENVIRONMENT_AGENT_ID] = deepcopy(DEFAULT_ENVIRONMENT_AGENT)
        agent = agents[DEFAULT_ENVIRONMENT_AGENT_ID]
    else:
        for key, value in DEFAULT_ENVIRONMENT_AGENT.items():
            if key not in agent:
                agent[key] = deepcopy(value)
        if not isinstance(agent.get("dispatch"), dict):
            agent["dispatch"] = deepcopy(DEFAULT_ENVIRONMENT_AGENT["dispatch"])
    if not isinstance(agent.get("capability_refs"), list):
        agent["capability_refs"] = list(DEFAULT_ENVIRONMENT_AGENT["capability_refs"])
    packager_agent = agents.get(DEFAULT_CAPABILITY_PACKAGER_AGENT_ID)
    if not isinstance(packager_agent, dict):
        agents[DEFAULT_CAPABILITY_PACKAGER_AGENT_ID] = deepcopy(
            DEFAULT_CAPABILITY_PACKAGER_AGENT
        )
        packager_agent = agents[DEFAULT_CAPABILITY_PACKAGER_AGENT_ID]
    else:
        for key, value in DEFAULT_CAPABILITY_PACKAGER_AGENT.items():
            if key not in packager_agent:
                packager_agent[key] = deepcopy(value)
        if not isinstance(packager_agent.get("dispatch"), dict):
            packager_agent["dispatch"] = deepcopy(
                DEFAULT_CAPABILITY_PACKAGER_AGENT["dispatch"]
            )
    if not isinstance(packager_agent.get("capability_refs"), list):
        packager_agent["capability_refs"] = list(
            DEFAULT_CAPABILITY_PACKAGER_AGENT["capability_refs"]
        )
    for value in agents.values():
        if not isinstance(value, dict):
            continue
        visibility = str(value.get("visibility") or "user").strip().lower()
        if visibility == "user" and not str(value.get("runtime_profile") or "").strip():
            value["runtime_profile"] = DEFAULT_USER_RUNTIME_PROFILE_ID
    return registry, profiles


def ensure_default_capability_packages(
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return capability package data with built-in packages present."""

    packages = deepcopy(data) if isinstance(data, dict) else {}
    package = packages.get(DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE_ID)
    if not isinstance(package, dict):
        packages[DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE_ID] = deepcopy(
            DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE
        )
    else:
        for key, value in DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE.items():
            package.setdefault(key, deepcopy(value))
        package["enabled"] = True
        package["components"] = list(DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE["components"])
    core_package = packages.get(DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID)
    if not isinstance(core_package, dict):
        packages[DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID] = deepcopy(
            DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE
        )
    else:
        for key, value in DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE.items():
            core_package.setdefault(key, deepcopy(value))
        core_package["enabled"] = True
        core_package["components"] = list(
            DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE["components"]
        )
    packager_package = packages.get(
        DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID
    )
    if not isinstance(packager_package, dict):
        packages[DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID] = deepcopy(
            DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE
        )
    else:
        for key, value in DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE.items():
            packager_package.setdefault(key, deepcopy(value))
        packager_package["enabled"] = True
        packager_package["components"] = list(
            DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE["components"]
        )
    return packages


def ensure_default_capability_components(
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return capability component data with built-in tool components present."""

    components = deepcopy(data) if isinstance(data, dict) else {}
    for component_id, defaults in DEFAULT_BUILTIN_TOOL_COMPONENTS.items():
        component = components.get(component_id)
        if not isinstance(component, dict):
            component = deepcopy(defaults)
            components[component_id] = component
        for key in BUILTIN_TOOL_COMPONENT_FORCED_FIELDS:
            if key in defaults:
                component[key] = deepcopy(defaults[key])
        component["enabled"] = True
        component["package_ids"] = _merge_string_lists(
            component.get("package_ids"),
            defaults.get("package_ids"),
        )
    return components


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
class StreamRecoveryConfig:
    """Provider stream recovery policy."""

    enabled: bool = True
    max_continue_attempts: int = 1
    retry_empty_once: bool = True
    retry_tool_delta_once: bool = True
    fallback_models: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_continue_attempts": self.max_continue_attempts,
            "retry_empty_once": self.retry_empty_once,
            "retry_tool_delta_once": self.retry_tool_delta_once,
            "fallback_models": [dict(item) for item in self.fallback_models],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "StreamRecoveryConfig":
        if not isinstance(d, dict):
            return cls()
        fallbacks = d.get("fallback_models")
        return cls(
            enabled=bool(d.get("enabled", True)),
            max_continue_attempts=max(0, int(d.get("max_continue_attempts", 1) or 0)),
            retry_empty_once=bool(d.get("retry_empty_once", True)),
            retry_tool_delta_once=bool(d.get("retry_tool_delta_once", True)),
            fallback_models=[
                dict(item) for item in fallbacks if isinstance(item, dict)
            ]
            if isinstance(fallbacks, list)
            else [],
        )


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
    stream_recovery: StreamRecoveryConfig = field(default_factory=StreamRecoveryConfig)
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
            "stream_recovery": self.stream_recovery.to_dict(),
            "extra": dict(self.extra),
        }
        return data

    @classmethod
    def from_dict(cls, provider_id: str, d: dict[str, Any]) -> "ProviderConfig":
        raw_type = str(d.get("type", "openai_chat")).strip().lower()
        provider_type: ProviderType
        if raw_type in {"anthropic_messages", "openai_responses", "labrastro_server"}:
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
            stream_recovery=StreamRecoveryConfig.from_dict(d.get("stream_recovery")),
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
    display_name: str = ""
    summary: str = ""
    runtime_footprint: dict[str, Any] = field(default_factory=dict)
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
    environment_requirement_refs: list[str] = field(default_factory=list)
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
    component_id: str = ""
    package_ids: list[str] = field(default_factory=list)
    managed_by: str = ""
    hooks: list[dict[str, Any]] = field(default_factory=list)
    lifecycle_hook_results: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.runtime_footprint = runtime_footprint_for_mcp(self)
        self.placement = runs_on_to_mcp_placement(
            str(self.runtime_footprint.get("runs_on") or "server")
        )  # type: ignore[assignment]

    def to_dict(self) -> dict:
        """Convert to dictionary format for serialization."""
        return {
            "command": self.command,
            "display_name": self.display_name,
            "summary": self.summary,
            "runtime_footprint": dict(self.runtime_footprint),
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
            "environment_requirement_refs": list(self.environment_requirement_refs),
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
            "component_id": self.component_id,
            "package_ids": list(self.package_ids),
            "managed_by": self.managed_by,
            "hooks": [dict(item) for item in self.hooks],
            "lifecycle_hook_results": dict(self.lifecycle_hook_results),
        }

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "MCPServerConfig":
        """Create from dictionary format."""
        raw_runtime_footprint = d.get("runtime_footprint")
        if isinstance(raw_runtime_footprint, dict):
            footprint = normalize_runtime_footprint(
                raw_runtime_footprint,
                default_runs_on="server",
            )
            raw_placement = runs_on_to_mcp_placement(str(footprint["runs_on"]))
        else:
            footprint = {}
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
        raw_environment_requirement_refs = d.get("environment_requirement_refs", [])
        raw_build = d.get("build", {})
        raw_args = d.get("args", [])
        raw_env = d.get("env", {})
        return cls(
            name=name,
            command=str(d.get("command", "")),
            display_name=str(d.get("display_name", "") or ""),
            summary=str(d.get("summary", "") or ""),
            runtime_footprint=footprint,
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
            environment_requirement_refs=_string_list_config_value(
                raw_environment_requirement_refs
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
            component_id=str(d.get("component_id", "")),
            package_ids=_string_list_config_value(d.get("package_ids", [])),
            managed_by=str(d.get("managed_by", "")),
            hooks=_dict_list_config_value(d.get("hooks", [])),
            lifecycle_hook_results=(
                dict(d.get("lifecycle_hook_results"))
                if isinstance(d.get("lifecycle_hook_results"), dict)
                else {}
            ),
        )


@dataclass
class ModelProfileConfig:
    """Named model/runtime profile used by ``/model`` switching."""

    name: str
    model: str = ""
    provider: Optional[str] = None
    max_tokens: int = 0
    temperature: float = 0.0
    max_context_tokens: int = 0
    preserve_reasoning_content: bool = True
    backfill_reasoning_content_for_tool_calls: bool = False
    reasoning_effort: Optional[str] = None
    thinking_enabled: Optional[bool] = None
    reasoning_replay_mode: Optional[str] = None
    reasoning_replay_placeholder: Optional[str] = None
    capability_user_configured: bool = False
    capability_source: Optional[str] = None
    capability_applied_at: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary format for serialization."""
        return {
            "model": self.model,
            "provider": self.provider,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "max_context_tokens": self.max_context_tokens,
            "preserve_reasoning_content": self.preserve_reasoning_content,
            "backfill_reasoning_content_for_tool_calls": self.backfill_reasoning_content_for_tool_calls,
            "reasoning_effort": self.reasoning_effort,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_replay_mode": self.reasoning_replay_mode,
            "reasoning_replay_placeholder": self.reasoning_replay_placeholder,
            "capability_user_configured": self.capability_user_configured,
            "capability_source": self.capability_source,
            "capability_applied_at": self.capability_applied_at,
        }

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "ModelProfileConfig":
        """Create from dictionary format."""
        return cls(
            name=name,
            model=str(d.get("model", "") or ""),
            provider=str(d["provider"]) if d.get("provider") is not None else None,
            max_tokens=int(d.get("max_tokens", 0) or 0),
            temperature=d.get("temperature", 0.0),
            max_context_tokens=int(d.get("max_context_tokens", 0) or 0),
            preserve_reasoning_content=d.get("preserve_reasoning_content", True),
            backfill_reasoning_content_for_tool_calls=d.get(
                "backfill_reasoning_content_for_tool_calls", False
            ),
            reasoning_effort=d.get("reasoning_effort"),
            thinking_enabled=d.get("thinking_enabled"),
            reasoning_replay_mode=d.get("reasoning_replay_mode"),
            reasoning_replay_placeholder=d.get("reasoning_replay_placeholder"),
            capability_user_configured=bool(d.get("capability_user_configured", False)),
            capability_source=(
                str(d["capability_source"])
                if d.get("capability_source") is not None
                else None
            ),
            capability_applied_at=(
                str(d["capability_applied_at"])
                if d.get("capability_applied_at") is not None
                else None
            ),
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
class SkillRegistrationConfig:
    """Registered Skill resource managed by capability settings."""

    name: str
    enabled: bool = True
    display_name: str = ""
    summary: str = ""
    runtime_footprint: dict[str, Any] = field(default_factory=dict)
    path_hint: str = ""
    source_path: str = ""
    description: str = ""
    source: str = ""
    repo_url: str = ""
    docs: list[dict[str, str]] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    install_prompt: str = ""
    verify_prompt: str = ""
    notes: list[str] = field(default_factory=list)
    environment_requirement_refs: list[str] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)
    risk_level: str = ""
    last_action: str = ""
    last_updated: str = ""
    component_id: str = ""
    package_ids: list[str] = field(default_factory=list)
    managed_by: str = "user"
    hooks: list[dict[str, Any]] = field(default_factory=list)
    lifecycle_hook_results: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.runtime_footprint = runtime_footprint_for_skill(self)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any] | None) -> "SkillRegistrationConfig":
        if not isinstance(data, dict):
            data = {}
        resolved_name = str(data.get("name") or name or "").strip()
        if not resolved_name:
            resolved_name = str(name or "").strip()
        return cls(
            name=resolved_name,
            enabled=_bool_config_value(data.get("enabled", True)),
            display_name=str(data.get("display_name") or ""),
            summary=str(data.get("summary") or ""),
            runtime_footprint=normalize_runtime_footprint(
                data.get("runtime_footprint", {}),
                default_runs_on="agent_only",
            ),
            path_hint=str(data.get("path_hint") or data.get("path") or ""),
            source_path=str(data.get("source_path") or ""),
            description=str(data.get("description") or ""),
            source=str(data.get("source") or ""),
            repo_url=str(data.get("repo_url") or ""),
            docs=_docs_config_value(data.get("docs", [])),
            evidence=_string_dict_list_config_value(data.get("evidence", [])),
            install_prompt=str(data.get("install_prompt") or ""),
            verify_prompt=str(data.get("verify_prompt") or ""),
            notes=_string_list_config_value(data.get("notes", [])),
            environment_requirement_refs=_string_list_config_value(
                data.get("environment_requirement_refs", [])
            ),
            credentials=_string_list_config_value(data.get("credentials", [])),
            risk_level=str(data.get("risk_level") or ""),
            last_action=str(data.get("last_action") or ""),
            last_updated=str(data.get("last_updated") or ""),
            component_id=str(data.get("component_id") or ""),
            package_ids=_string_list_config_value(data.get("package_ids", [])),
            managed_by=str(data.get("managed_by") or "user"),
            hooks=_dict_list_config_value(data.get("hooks", [])),
            lifecycle_hook_results=(
                dict(data.get("lifecycle_hook_results"))
                if isinstance(data.get("lifecycle_hook_results"), dict)
                else {}
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "display_name": self.display_name,
            "summary": self.summary,
            "runtime_footprint": dict(self.runtime_footprint),
            "path_hint": self.path_hint,
            "source_path": self.source_path,
            "description": self.description,
            "source": self.source,
            "repo_url": self.repo_url,
            "docs": [dict(item) for item in self.docs],
            "evidence": [dict(item) for item in self.evidence],
            "install_prompt": self.install_prompt,
            "verify_prompt": self.verify_prompt,
            "notes": list(self.notes),
            "environment_requirement_refs": list(self.environment_requirement_refs),
            "credentials": list(self.credentials),
            "risk_level": self.risk_level,
            "last_action": self.last_action,
            "last_updated": self.last_updated,
            "component_id": self.component_id,
            "package_ids": list(self.package_ids),
            "managed_by": self.managed_by,
            "hooks": [dict(item) for item in self.hooks],
            "lifecycle_hook_results": dict(self.lifecycle_hook_results),
        }


@dataclass
class SkillsConfig:
    """Skills discovery/runtime configuration."""

    enabled: bool = True
    scan_project: bool = True
    scan_user: bool = True
    disabled: list[str] = field(default_factory=list)
    items: dict[str, SkillRegistrationConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SkillsConfig":
        if not isinstance(data, dict):
            data = {}
        raw_items = data.get("items", {})
        items: dict[str, SkillRegistrationConfig] = {}
        if isinstance(raw_items, dict):
            for name, item in raw_items.items():
                if not isinstance(item, dict):
                    continue
                skill = SkillRegistrationConfig.from_dict(str(name), item)
                if skill.name:
                    items[skill.name] = skill
        return cls(
            enabled=_bool_config_value(data.get("enabled", True)),
            scan_project=_bool_config_value(data.get("scan_project", True)),
            scan_user=_bool_config_value(data.get("scan_user", True)),
            disabled=_string_list_config_value(data.get("disabled", [])),
            items=items,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "scan_project": self.scan_project,
            "scan_user": self.scan_user,
            "disabled": list(self.disabled),
            "items": {
                name: item.to_dict()
                for name, item in sorted(self.items.items())
            },
        }


@dataclass
class PromptConfig:
    """User/workspace prompt customization."""

    system_append: str = ""


@dataclass
class ContextConfig:
    """Context compression configuration."""

    snip_keep_recent_tools: int = 2
    snip_threshold_chars: int = 1500
    snip_min_lines: int = 6
    summarize_keep_recent_turns: int = 5
    token_fudge_factor: float = 1.1


@dataclass
class MemoryProviderInstanceConfig:
    """One configured memory provider adapter instance."""

    adapter: str
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls, provider_id: str, data: dict[str, Any] | None
    ) -> "MemoryProviderInstanceConfig":
        if not isinstance(data, dict):
            raise ValueError(f"memory.providers.{provider_id} must be an object")
        adapter = str(data.get("adapter", "") or "").strip()
        if not adapter:
            raise ValueError(f"memory.providers.{provider_id}.adapter is required")
        extra = {
            str(key): value
            for key, value in data.items()
            if key not in {"adapter", "enabled"}
        }
        return cls(
            adapter=adapter,
            enabled=bool(data.get("enabled", True)),
            config=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {"adapter": self.adapter, "enabled": self.enabled}
        data.update(dict(self.config))
        return data


@dataclass
class MemorySourceConfig:
    """One configured memory source connector."""

    adapter: str
    enabled: bool = True
    target_provider: str = ""
    sync_mode: str = "manual"
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(
        cls, source_id: str, data: dict[str, Any] | None
    ) -> "MemorySourceConfig":
        if not isinstance(data, dict):
            raise ValueError(f"memory.sources.{source_id} must be an object")
        adapter = str(data.get("adapter", "") or "").strip()
        if not adapter:
            raise ValueError(f"memory.sources.{source_id}.adapter is required")
        extra = {
            str(key): value
            for key, value in data.items()
            if key not in {"adapter", "enabled", "target_provider", "sync_mode"}
        }
        return cls(
            adapter=adapter,
            enabled=bool(data.get("enabled", True)),
            target_provider=str(data.get("target_provider", "") or "").strip(),
            sync_mode=str(data.get("sync_mode", "manual") or "manual"),
            config=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "adapter": self.adapter,
            "enabled": self.enabled,
            "target_provider": self.target_provider,
            "sync_mode": self.sync_mode,
        }
        data.update(dict(self.config))
        return data


@dataclass
class MemoryRuntimeConfig:
    """Runtime policy for automatic memory injection and capture."""

    inject_default: bool = True
    capture_default: bool = True
    token_budget_default: int = 800
    fail_mode: str = "open"
    trace_enabled: bool = True
    trust_policy: str = "wrap_external"

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MemoryRuntimeConfig":
        raw = data if isinstance(data, dict) else {}
        return cls(
            inject_default=bool(raw.get("inject_default", True)),
            capture_default=bool(raw.get("capture_default", True)),
            token_budget_default=int(raw.get("token_budget_default", 800) or 800),
            fail_mode=str(raw.get("fail_mode", "open") or "open"),
            trace_enabled=bool(raw.get("trace_enabled", True)),
            trust_policy=str(
                raw.get("trust_policy", "wrap_external") or "wrap_external"
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "inject_default": self.inject_default,
            "capture_default": self.capture_default,
            "token_budget_default": self.token_budget_default,
            "fail_mode": self.fail_mode,
            "trace_enabled": self.trace_enabled,
            "trust_policy": self.trust_policy,
        }


@dataclass
class MemoryToolsConfig:
    """Agent-visible memory tools policy."""

    enabled: bool = False
    provider: str = ""
    allowed_agents: list[str] = field(default_factory=list)
    recall: bool = False
    remember: bool = False
    forget: bool = False
    list: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MemoryToolsConfig":
        raw = data if isinstance(data, dict) else {}
        return cls(
            enabled=bool(raw.get("enabled", False)),
            provider=str(raw.get("provider", "") or "").strip(),
            allowed_agents=_string_list_config_value(raw.get("allowed_agents", [])),
            recall=bool(raw.get("recall", False)),
            remember=bool(raw.get("remember", False)),
            forget=bool(raw.get("forget", False)),
            list=bool(raw.get("list", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "allowed_agents": list(self.allowed_agents),
            "recall": self.recall,
            "remember": self.remember,
            "forget": self.forget,
            "list": self.list,
        }


@dataclass
class MemoryConfig:
    """Memory provider contract configuration."""

    enabled: bool = False
    default_provider: str = ""
    default_agent_id: str = "core"
    default_namespace: str = ""
    runtime: MemoryRuntimeConfig = field(default_factory=MemoryRuntimeConfig)
    providers: dict[str, MemoryProviderInstanceConfig] = field(default_factory=dict)
    sources: dict[str, MemorySourceConfig] = field(default_factory=dict)
    tools: MemoryToolsConfig = field(default_factory=MemoryToolsConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MemoryConfig":
        if not isinstance(data, dict):
            return cls()
        removed_fields = sorted(
            key
            for key in ("backend", "store_path", "token_budget", "capture_enabled")
            if key in data
        )
        if removed_fields:
            raise ValueError(
                "memory config fields "
                + ", ".join(removed_fields)
                + " were removed; use memory.providers and memory.runtime"
            )
        providers_raw = data.get("providers", {})
        providers = {
            str(provider_id): MemoryProviderInstanceConfig.from_dict(
                str(provider_id), provider_data
            )
            for provider_id, provider_data in (
                providers_raw.items() if isinstance(providers_raw, dict) else []
            )
        }
        sources_raw = data.get("sources", {})
        sources = {
            str(source_id): MemorySourceConfig.from_dict(str(source_id), source_data)
            for source_id, source_data in (
                sources_raw.items() if isinstance(sources_raw, dict) else []
            )
        }
        return cls(
            enabled=bool(data.get("enabled", False)),
            default_provider=str(data.get("default_provider", "") or "").strip(),
            default_agent_id=str(data.get("default_agent_id", "core") or "core"),
            default_namespace=str(data.get("default_namespace", "") or ""),
            runtime=MemoryRuntimeConfig.from_dict(data.get("runtime")),
            providers=providers,
            sources=sources,
            tools=MemoryToolsConfig.from_dict(data.get("tools")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "default_provider": self.default_provider,
            "default_agent_id": self.default_agent_id,
            "default_namespace": self.default_namespace,
            "runtime": self.runtime.to_dict(),
            "providers": {
                provider_id: provider.to_dict()
                for provider_id, provider in sorted(self.providers.items())
            },
            "sources": {
                source_id: source.to_dict()
                for source_id, source in sorted(self.sources.items())
            },
            "tools": self.tools.to_dict(),
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
    """Concurrency limits for separate AgentRun runtime resource pools."""

    max_running_agents: int = 4
    max_shells_per_agent: int = 1
    server_agent_run_slots: int = 4
    server_sandbox_slots: int = 2
    local_peer_agent_run_slots: int = 1
    model_request_slots: int = 4

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RunLimitsConfig":
        if not isinstance(data, dict):
            return cls()
        max_running_agents = int(data.get("max_running_agents", 4) or 4)
        return cls(
            max_running_agents=max_running_agents,
            max_shells_per_agent=int(data.get("max_shells_per_agent", 1) or 1),
            server_agent_run_slots=int(
                data.get("server_agent_run_slots", max_running_agents)
                or max_running_agents
            ),
            server_sandbox_slots=int(data.get("server_sandbox_slots", 2) or 2),
            local_peer_agent_run_slots=int(
                data.get("local_peer_agent_run_slots", 1) or 1
            ),
            model_request_slots=int(
                data.get("model_request_slots", max_running_agents)
                or max_running_agents
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_running_agents": self.max_running_agents,
            "max_shells_per_agent": self.max_shells_per_agent,
            "server_agent_run_slots": self.server_agent_run_slots,
            "server_sandbox_slots": self.server_sandbox_slots,
            "local_peer_agent_run_slots": self.local_peer_agent_run_slots,
            "model_request_slots": self.model_request_slots,
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
    capability_components: dict[str, CapabilityComponentConfig] | None = None,
) -> dict[str, Any]:
    """Return the server-authoritative AgentRun snapshot for executors."""

    packages = dict(capability_packages or {})
    components = {
        component_id: CapabilityComponentConfig.from_dict(component_id, component_data)
        for component_id, component_data in ensure_default_capability_components(
            {
                component_id: component.to_dict()
                for component_id, component in (capability_components or {}).items()
            }
        ).items()
        if isinstance(component_data, dict)
    }
    if DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE_ID not in packages:
        packages[DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE_ID] = (
            CapabilityPackageConfig.from_dict(
                DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE_ID,
                DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE,
            )
        )
    if DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID not in packages:
        packages[DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID] = (
            CapabilityPackageConfig.from_dict(
                DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID,
                DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE,
            )
        )
    if DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID not in packages:
        packages[DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID] = (
            CapabilityPackageConfig.from_dict(
                DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID,
                DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE,
            )
        )
    agents: dict[str, dict[str, Any]] = {}
    for agent_id, agent in agent_registry.agents.items():
        agent_dict = agent.to_dict()
        agent_dict["resolved_capabilities"] = resolve_capability_refs(
            agent.capability_refs,
            packages,
            components,
        )
        agent_dict["effective_capabilities"] = agent_dict[
            "resolved_capabilities"
        ].get("effective_capabilities", {})
        agents[agent_id] = agent_dict
    return {
        "max_running_agents": run_limits.max_running_agents,
        "max_shells_per_agent": run_limits.max_shells_per_agent,
        "runtime_slots": {
            "server_agent_run_slots": run_limits.server_agent_run_slots,
            "server_sandbox_slots": run_limits.server_sandbox_slots,
            "local_peer_agent_run_slots": run_limits.local_peer_agent_run_slots,
            "model_request_slots": run_limits.model_request_slots,
        },
        "runtime_profiles": runtime_profiles.to_dict(),
        "agents": agents,
        "capability_packages": {
            package_id: package.to_dict()
            for package_id, package in packages.items()
        },
        "capability_components": {
            component_id: component.to_dict()
            for component_id, component in components.items()
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
    event_payload_compress_threshold_bytes: int = 262144
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
            event_payload_compress_threshold_bytes=int(
                data.get("event_payload_compress_threshold_bytes", 262144) or 262144
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
            "event_payload_compress_threshold_bytes": self.event_payload_compress_threshold_bytes,
            "maintenance_interval_sec": self.maintenance_interval_sec,
        }


@dataclass
class ToolDiagnosticsConfig:
    """Tool lifecycle diagnostic telemetry settings."""

    enabled: bool = True
    record_clean: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ToolDiagnosticsConfig":
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
class LLMTraceDiagnosticsConfig:
    """LLM trace settings."""

    enabled: bool = False
    raw_chunks: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "LLMTraceDiagnosticsConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=bool(data.get("enabled", False)),
            raw_chunks=bool(data.get("raw_chunks", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "raw_chunks": self.raw_chunks,
        }


@dataclass
class DiagnosticsConfig:
    """Diagnostics and telemetry settings."""

    tool_diagnostics: ToolDiagnosticsConfig = field(
        default_factory=ToolDiagnosticsConfig
    )
    llm_trace: LLMTraceDiagnosticsConfig = field(
        default_factory=LLMTraceDiagnosticsConfig
    )

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DiagnosticsConfig":
        raw_tool_diagnostics = data.get("tool_diagnostics", {}) if isinstance(data, dict) else {}
        raw_llm_trace = data.get("llm_trace", {}) if isinstance(data, dict) else {}
        return cls(
            tool_diagnostics=ToolDiagnosticsConfig.from_dict(
                raw_tool_diagnostics if isinstance(raw_tool_diagnostics, dict) else {}
            ),
            llm_trace=LLMTraceDiagnosticsConfig.from_dict(
                raw_llm_trace if isinstance(raw_llm_trace, dict) else {}
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_diagnostics": self.tool_diagnostics.to_dict(),
            "llm_trace": self.llm_trace.to_dict(),
        }


@dataclass
class SandboxProviderConfig:
    """Execution-room provider used for on-demand AgentRun sessions."""

    type: str = "none"
    host_base_url: str = ""
    worker_image: str = "labrastro-host:test"
    workspace_volume_root: str = "labrastro-workspaces"
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
                data.get("workspace_volume_root", "labrastro-workspaces")
                or "labrastro-workspaces"
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
class EnvironmentRequirementConfig:
    """Declarative environment requirement consumed by environment-capable Agents."""

    id: str
    kind: str
    name: str
    enabled: bool = True
    placement: EnvironmentPlacement = "peer"
    runtime_footprint: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    requirements: dict[str, str] = field(default_factory=dict)
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    check: str = ""
    install: str = ""
    configure: str = ""
    version: Optional[str] = None
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
    component_id: str = ""
    package_ids: list[str] = field(default_factory=list)
    managed_by: str = ""

    def __post_init__(self) -> None:
        self.runtime_footprint = runtime_footprint_for_environment_requirement(self)
        self.placement = runs_on_to_environment_placement(
            str(self.runtime_footprint.get("runs_on") or "local_peer")
        )  # type: ignore[assignment]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "enabled": self.enabled,
            "placement": self.placement,
            "runtime_footprint": dict(self.runtime_footprint),
        }
        if self.tags:
            data["tags"] = list(self.tags)
        if self.requirements:
            data["requirements"] = dict(self.requirements)
        if self.command:
            data["command"] = self.command
        if self.args:
            data["args"] = list(self.args)
        if self.env:
            data["env"] = dict(self.env)
        if self.cwd is not None:
            data["cwd"] = self.cwd
        if self.check:
            data["check"] = self.check
        if self.install:
            data["install"] = self.install
        if self.configure:
            data["configure"] = self.configure
        if self.version is not None:
            data["version"] = self.version
        if self.runtime:
            data["runtime"] = self.runtime
        if self.language:
            data["language"] = self.language
        if self.scope:
            data["scope"] = self.scope
        if self.path:
            data["path"] = self.path
        if self.source:
            data["source"] = self.source
        if self.description:
            data["description"] = self.description
        if self.repo_url:
            data["repo_url"] = self.repo_url
        if self.docs:
            data["docs"] = [dict(item) for item in self.docs]
        if self.evidence:
            data["evidence"] = [dict(item) for item in self.evidence]
        if self.install_prompt:
            data["install_prompt"] = self.install_prompt
        if self.verify_prompt:
            data["verify_prompt"] = self.verify_prompt
        if self.notes:
            data["notes"] = list(self.notes)
        if self.credentials:
            data["credentials"] = list(self.credentials)
        if self.risk_level:
            data["risk_level"] = self.risk_level
        if self.last_action:
            data["last_action"] = self.last_action
        if self.last_updated:
            data["last_updated"] = self.last_updated
        if self.component_id:
            data["component_id"] = self.component_id
        if self.package_ids:
            data["package_ids"] = list(self.package_ids)
        if self.managed_by:
            data["managed_by"] = self.managed_by
        return data

    @classmethod
    def from_dict(cls, requirement_id: str, d: dict[str, Any]) -> "EnvironmentRequirementConfig":
        raw_tags = d.get("tags", [])
        raw_requirements = d.get("requirements", {})
        raw_args = d.get("args", [])
        raw_env = d.get("env", {})
        raw_requirement_id = str(d.get("id") or requirement_id)
        raw_runtime_footprint = d.get("runtime_footprint")
        if isinstance(raw_runtime_footprint, dict):
            footprint = normalize_runtime_footprint(
                raw_runtime_footprint,
                default_runs_on="local_peer",
            )
            placement = normalize_environment_placement(
                runs_on_to_environment_placement(str(footprint["runs_on"]))
            )
        else:
            footprint = {}
            placement = normalize_environment_placement(d.get("placement", "peer"))
        kind = resolve_environment_requirement_kind(
            raw_requirement_id,
            candidates=(
                d.get("kind"),
                d.get("resource_kind"),
                d.get("requirement_kind"),
            ),
            command=d.get("command"),
        )
        name = str(
            d.get("name") or environment_requirement_name_from_id(raw_requirement_id)
        ).strip()
        if not name:
            name = raw_requirement_id.strip()
        normalized_id = normalize_environment_requirement_id(
            raw_requirement_id,
            kind=kind,
            name=name,
        )
        return cls(
            id=normalized_id,
            kind=kind,
            name=name,
            enabled=_bool_config_value(d.get("enabled", True)),
            placement=placement,
            runtime_footprint=footprint,
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
            command=str(d.get("command", "")),
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
            docs=_docs_config_value(d.get("docs", [])),
            evidence=_string_dict_list_config_value(d.get("evidence", [])),
            install_prompt=str(d.get("install_prompt", "")),
            verify_prompt=str(d.get("verify_prompt", "")),
            notes=_string_list_config_value(d.get("notes", [])),
            credentials=_string_list_config_value(d.get("credentials", [])),
            risk_level=str(d.get("risk_level", "")),
            last_action=str(d.get("last_action", "")),
            last_updated=str(d.get("last_updated", "")),
            component_id=str(d.get("component_id", "")),
            package_ids=_string_list_config_value(d.get("package_ids", [])),
            managed_by=str(d.get("managed_by", "")),
        )


@dataclass
class EnvironmentConfig:
    """Server-authoritative environment requirements manifest."""

    requirements: dict[str, EnvironmentRequirementConfig] = field(default_factory=dict)


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


def _merge_string_lists(*values: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _string_list_config_value(value):
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return merged


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


def _dict_list_config_value(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


@dataclass
class Config:
    """Main configuration model for ReuleauxCoder."""

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
    llm_trace_authoritative: bool = False

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

    # Language Server Protocol settings
    lsp: dict[str, Any] = field(default_factory=dict)

    # Remote host authentication settings
    auth: AuthConfig = field(default_factory=AuthConfig)

    # Persistent Agent definitions, runtime profiles, and AgentRun limits.
    agent_registry: AgentRegistryConfig = field(default_factory=AgentRegistryConfig)
    runtime_profiles: RuntimeProfilesConfig = field(default_factory=RuntimeProfilesConfig)
    run_limits: RunLimitsConfig = field(default_factory=RunLimitsConfig)

    # Confirmed capability packages and their shared installed components.
    capability_packages: dict[str, CapabilityPackageConfig] = field(default_factory=dict)
    capability_components: dict[str, CapabilityComponentConfig] = field(default_factory=dict)

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
        for name, profile in self.model_profiles.items():
            prefix = f"models.profiles.{name}"
            if not profile.provider:
                errors.append(f"{prefix}.provider is required")
            elif profile.provider not in self.providers.items:
                errors.append(f"{prefix}.provider references unknown provider")
            if not profile.model:
                errors.append(f"{prefix}.model is required")
            if profile.max_tokens < 1:
                errors.append(f"{prefix}.max_tokens must be positive")
            if profile.max_context_tokens < 1:
                errors.append(f"{prefix}.max_context_tokens must be positive")
            if profile.temperature < 0 or profile.temperature > 2:
                errors.append(f"{prefix}.temperature must be between 0 and 2")
        if self.tool_output_max_chars < 1:
            errors.append("tool_output_max_chars must be positive")
        if self.tool_output_max_lines < 1:
            errors.append("tool_output_max_lines must be positive")
        errors.extend(_validate_lifecycle_hooks_for_config(self))
        if self.memory.runtime.fail_mode not in {"open", "closed"}:
            errors.append("memory.runtime.fail_mode must be one of open, closed")
        if self.memory.runtime.token_budget_default < 1:
            errors.append("memory.runtime.token_budget_default must be positive")
        for provider_id, provider in self.memory.providers.items():
            if not provider.adapter.strip():
                errors.append(f"memory.providers.{provider_id}.adapter is required")
            elif not MemoryProviderRegistry.is_adapter_registered(provider.adapter):
                errors.append(
                    f"memory.providers.{provider_id}.adapter is not registered"
                )
        if self.memory.enabled:
            if not self.memory.default_provider.strip():
                errors.append("memory.default_provider is required when memory.enabled is true")
            elif self.memory.default_provider not in self.memory.providers:
                errors.append("memory.default_provider references unknown provider")
            elif not self.memory.providers[self.memory.default_provider].enabled:
                errors.append("memory.default_provider references disabled provider")
            if not self.memory.default_agent_id.strip():
                errors.append("memory.default_agent_id is required when memory.enabled is true")
        for source_id, source in self.memory.sources.items():
            if source.enabled and source.target_provider not in self.memory.providers:
                errors.append(
                    f"memory.sources.{source_id}.target_provider references unknown provider"
                )
            if source.enabled and source.target_provider in self.memory.providers:
                if not self.memory.providers[source.target_provider].enabled:
                    errors.append(
                        f"memory.sources.{source_id}.target_provider references disabled provider"
                    )
            if source.enabled and not MemorySourceRegistry.is_adapter_registered(source.adapter):
                errors.append(
                    f"memory.sources.{source_id}.adapter is not registered"
                )
        if self.memory.tools.enabled:
            if not self.memory.tools.provider:
                errors.append("memory.tools.provider is required when memory.tools.enabled is true")
            elif self.memory.tools.provider not in self.memory.providers:
                errors.append("memory.tools.provider references unknown provider")
            elif not self.memory.providers[self.memory.tools.provider].enabled:
                errors.append("memory.tools.provider references disabled provider")
        if self.run_limits.max_running_agents < 1:
            errors.append("run_limits.max_running_agents must be positive")
        if self.run_limits.max_shells_per_agent < 1:
            errors.append("run_limits.max_shells_per_agent must be positive")
        for field_name in (
            "server_agent_run_slots",
            "server_sandbox_slots",
            "local_peer_agent_run_slots",
            "model_request_slots",
        ):
            if int(getattr(self.run_limits, field_name)) < 1:
                errors.append(f"run_limits.{field_name} must be positive")
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
        if self.persistence.event_payload_compress_threshold_bytes < 1:
            errors.append(
                "persistence.event_payload_compress_threshold_bytes must be positive"
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
        if DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE_ID not in capability_packages:
            capability_packages[DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE_ID] = (
                CapabilityPackageConfig.from_dict(
                    DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE_ID,
                    DEFAULT_ENVIRONMENT_CAPABILITY_PACKAGE,
                )
            )
        if DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID not in capability_packages:
            capability_packages[DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID] = (
                CapabilityPackageConfig.from_dict(
                    DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID,
                    DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE,
                )
            )
        if (
            DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID
            not in capability_packages
        ):
            capability_packages[
                DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID
            ] = CapabilityPackageConfig.from_dict(
                DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE_ID,
                DEFAULT_CAPABILITY_PACKAGER_BUILTIN_CAPABILITY_PACKAGE,
            )
        built_in_component_ids = set(DEFAULT_BUILTIN_TOOL_COMPONENTS)
        chat_entrypoint_agent_ids: list[str] = []
        for agent_id, agent in self.agent_registry.agents.items():
            if agent.chat_entrypoint:
                chat_entrypoint_agent_ids.append(agent_id)
                if agent.visibility != "user":
                    errors.append(
                        f"agent_registry.agents[{agent_id}].chat_entrypoint requires visibility=user"
                    )
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
            agent_memory_configured = bool(agent.memory.to_dict())
            if agent_memory_configured and agent.memory.enabled:
                primary_provider = (
                    agent.memory.primary_provider or self.memory.default_provider
                )
                if (agent.memory.inject or agent.memory.capture) and not primary_provider:
                    errors.append(
                        f"agent_registry.agents[{agent_id}].memory.primary_provider is required when memory is enabled"
                    )
                if primary_provider and primary_provider not in self.memory.providers:
                    errors.append(
                        f"agent_registry.agents[{agent_id}].memory.primary_provider references unknown memory provider"
                    )
                for provider_id in agent.memory.read_providers:
                    if provider_id not in self.memory.providers:
                        errors.append(
                            f"agent_registry.agents[{agent_id}].memory.read_providers references unknown memory provider {provider_id}"
                        )
                if agent.memory.token_budget is not None and agent.memory.token_budget < 1:
                    errors.append(
                        f"agent_registry.agents[{agent_id}].memory.token_budget must be positive"
                    )
        if len(chat_entrypoint_agent_ids) > 1:
            errors.append(
                "agent_registry.agents chat_entrypoint must be unique; found "
                + ", ".join(sorted(chat_entrypoint_agent_ids))
            )
        for package_id, package in capability_packages.items():
            for component_id in package.components:
                if (
                    component_id not in self.capability_components
                    and component_id not in built_in_component_ids
                ):
                    errors.append(
                        f"capability_packages[{package_id}].components references missing capability_components entry {component_id}"
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
            if not profile.provider:
                errors.append(f"model_profiles[{name}].provider is required")
            elif profile.provider not in self.providers.items:
                errors.append(
                    f"model_profiles[{name}].provider must exist in providers.items"
                )
            if not profile.model:
                errors.append(f"model_profiles[{name}].model is required")
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
                "labrastro_server",
            }:
                errors.append(
                    f"providers.items[{provider_id}].type must be one of openai_chat, anthropic_messages, openai_responses, labrastro_server"
                )
            if provider.timeout_sec < 1:
                errors.append(
                    f"providers.items[{provider_id}].timeout_sec must be positive"
                )
            if provider.max_retries < 0:
                errors.append(
                    f"providers.items[{provider_id}].max_retries must be non-negative"
                )
            if provider.stream_recovery.max_continue_attempts < 0:
                errors.append(
                    f"providers.items[{provider_id}].stream_recovery.max_continue_attempts must be non-negative"
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


def _validate_lifecycle_hooks_for_config(config: Config) -> list[str]:
    errors: list[str] = []

    for name, skill in config.skills.items.items():
        errors.extend(
            _validate_owner_lifecycle_hooks(
                prefix=f"skills.{name}.hooks",
                owner_id=skill.name or name,
                source="skill",
                hooks=skill.hooks,
                owner_enabled=skill.enabled,
                owner_status=getattr(skill, "status", "installed"),
            )
        )

    for server in config.mcp_servers:
        errors.extend(
            _validate_owner_lifecycle_hooks(
                prefix=f"mcp.servers.{server.name}.hooks",
                owner_id=server.name,
                source="mcp_server",
                hooks=server.hooks,
                owner_enabled=server.enabled,
                owner_status=getattr(server, "status", "installed"),
            )
        )

    for package_id, package in config.capability_packages.items():
        package_data = package.to_dict()
        package_state = capability_package_state_projection(package_data)
        errors.extend(
            _validate_owner_lifecycle_hooks(
                prefix=f"capability_packages.{package_id}.hooks",
                owner_id=package.id or package_id,
                source="capability_package",
                hooks=package.hooks,
                owner_enabled=capability_package_is_active(package_data),
                owner_status=package.status,
                owner_activation_state=package_state.get("activation_state", "active"),
            )
        )

    for component_id, component in config.capability_components.items():
        errors.extend(
            _validate_owner_lifecycle_hooks(
                prefix=f"capability_components.{component_id}.hooks",
                owner_id=component.id or component_id,
                source=_component_lifecycle_source(component),
                hooks=component.hooks,
                owner_enabled=component.enabled,
                owner_status=component.status,
            )
        )

    return errors


def _validate_owner_lifecycle_hooks(
    *,
    prefix: str,
    owner_id: str,
    source: str,
    hooks: list[dict[str, Any]],
    owner_enabled: bool,
    owner_status: str,
    owner_activation_state: str = "active",
) -> list[str]:
    try:
        lifecycle_declarations_from_config_hooks(
            owner_id=owner_id,
            source=source,
            hooks=hooks,
            owner_enabled=owner_enabled,
            owner_status=owner_status,
            owner_activation_state=owner_activation_state,
        )
    except Exception as exc:
        return [f"{prefix}: {exc}"]
    return []


def _component_lifecycle_source(component: CapabilityComponentConfig) -> str:
    if component.kind == "skill":
        return "skill"
    if component.kind in {"mcp", "mcp_server", "mcp_tool"}:
        return "mcp_server"
    return "capability_package"


@dataclass(frozen=True)
class AgentEffectiveCapabilityScope:
    """Runtime resources visible to one configured Agent."""

    agent_id: str
    found: bool
    resolved_capabilities: dict[str, Any] = field(default_factory=dict)
    effective_capabilities: dict[str, Any] = field(default_factory=dict)
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    environment: EnvironmentConfig = field(default_factory=EnvironmentConfig)
    capability_catalog: str = ""


def resolve_agent_effective_capability_scope(
    config: Config,
    agent_id: str,
) -> AgentEffectiveCapabilityScope:
    """Return the capability package resources authorized for one Agent.

    Settings/admin screens still use the full config. Runtime callers use this
    scope so disabled or unreferenced package-managed resources do not leak into
    prompts, MCP initialization, Skill discovery, or environment views.
    """

    resolved_agent_id = str(agent_id or "").strip()
    agent = config.agent_registry.agents.get(resolved_agent_id)
    if agent is None:
        return AgentEffectiveCapabilityScope(agent_id=resolved_agent_id, found=False)

    packages = _runtime_capability_packages(config)
    components = _runtime_capability_components(config)
    resolved = resolve_capability_refs(agent.capability_refs, packages, components)
    effective = resolved.get("effective_capabilities")
    effective_capabilities = effective if isinstance(effective, dict) else {}
    mcp_server_names = set(_string_list_config_value(effective_capabilities.get("mcp_servers")))
    skill_names = set(_string_list_config_value(effective_capabilities.get("skills")))
    environment_requirement_ids = {
        str(item.get("id") or "").strip()
        for item in effective_capabilities.get("environment_requirements", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    scoped_mcp_servers = [
        server
        for server in config.mcp_servers
        if getattr(server, "enabled", True)
        and str(getattr(server, "name", "") or "") in mcp_server_names
    ]
    scoped_skills = {
        name: skill
        for name, skill in config.skills.items.items()
        if getattr(skill, "enabled", True)
        and (name in skill_names or str(getattr(skill, "name", "") or "") in skill_names)
    }
    scoped_environment = {
        requirement_id: requirement
        for requirement_id, requirement in config.environment.requirements.items()
        if getattr(requirement, "enabled", True)
        and (
            requirement_id in environment_requirement_ids
            or str(getattr(requirement, "id", "") or "") in environment_requirement_ids
        )
    }
    return AgentEffectiveCapabilityScope(
        agent_id=resolved_agent_id,
        found=True,
        resolved_capabilities=resolved,
        effective_capabilities=effective_capabilities,
        mcp_servers=scoped_mcp_servers,
        skills=SkillsConfig(
            enabled=config.skills.enabled,
            scan_project=False,
            scan_user=False,
            disabled=list(config.skills.disabled),
            items=scoped_skills,
        ),
        environment=EnvironmentConfig(requirements=scoped_environment),
        capability_catalog=capability_catalog_from_resolved(resolved),
    )


def resolve_agent_environment_requirement_scope_ids(
    config: Config,
) -> dict[str, set[str]]:
    """Return Agent -> environment requirement ids visible at runtime."""

    scopes: dict[str, set[str]] = {}
    for agent_id in sorted(config.agent_registry.agents):
        scope = resolve_agent_effective_capability_scope(config, agent_id)
        if not scope.found:
            continue
        requirement_ids = set(scope.environment.requirements)
        for requirement in scope.environment.requirements.values():
            requirement_id = str(getattr(requirement, "id", "") or "").strip()
            if requirement_id:
                requirement_ids.add(requirement_id)
        scopes[agent_id] = requirement_ids
    return scopes


def capability_catalog_from_resolved(resolved_capabilities: dict[str, Any]) -> str:
    """Render an Agent-scoped capability catalog for prompts."""

    raw_packages = resolved_capabilities.get("packages", [])
    raw_components = resolved_capabilities.get("components", [])
    components = {
        str(item.get("id") or ""): item
        for item in raw_components
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    lines: list[str] = []
    package_items = raw_packages if isinstance(raw_packages, list) else []
    for raw_package in package_items:
        if not isinstance(raw_package, dict):
            continue
        package_id = str(raw_package.get("id") or "").strip()
        if not package_id or package_id == "environment":
            continue
        title = str(raw_package.get("name") or package_id)
        description = (
            f" - {raw_package.get('description')}"
            if str(raw_package.get("description") or "").strip()
            else ""
        )
        lines.append(f"- `{package_id}`: {title}{description}")
        for component_id in _string_list_config_value(raw_package.get("components")):
            component = components.get(component_id)
            if not isinstance(component, dict):
                continue
            config = component.get("config")
            details = _capability_component_details(
                config if isinstance(config, dict) else {}
            )
            suffix = f" ({details})" if details else ""
            lines.append(
                f"  - `{component_id}` [{component.get('kind')}] {component.get('name')}{suffix}"
            )
        usage = raw_package.get("usage")
        if isinstance(usage, list) and usage:
            lines.append("  - usage: " + " | ".join(str(item) for item in usage[:3]))
    return "\n".join(lines)


def _runtime_capability_packages(config: Config) -> dict[str, CapabilityPackageConfig]:
    package_data = ensure_default_capability_packages(
        {
            package_id: package.to_dict()
            for package_id, package in config.capability_packages.items()
        }
    )
    return {
        package_id: CapabilityPackageConfig.from_dict(package_id, value)
        for package_id, value in package_data.items()
        if isinstance(value, dict)
    }


def _runtime_capability_components(config: Config) -> dict[str, CapabilityComponentConfig]:
    component_data = ensure_default_capability_components(
        {
            component_id: component.to_dict()
            for component_id, component in config.capability_components.items()
        }
    )
    return {
        component_id: CapabilityComponentConfig.from_dict(component_id, value)
        for component_id, value in component_data.items()
        if isinstance(value, dict)
    }


def _capability_component_details(config: dict[str, Any]) -> str:
    parts: list[str] = []
    command = str(config.get("command") or "").strip()
    path_hint = str(config.get("path_hint") or "").strip()
    check = str(config.get("check") or "").strip()
    env = config.get("env")
    if command:
        parts.append(f"command `{command}`")
    if path_hint:
        parts.append(f"path `{path_hint}`")
    if check:
        parts.append(f"check `{check}`")
    if isinstance(env, dict) and env:
        parts.append("env " + ", ".join(sorted(str(key) for key in env)))
    return "; ".join(parts)

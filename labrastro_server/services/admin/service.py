"""Admin helpers for the remote relay HTTP service."""

from __future__ import annotations

import logging
import threading
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, cast

from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.app.commands.specs import TriggerKind
from reuleauxcoder.app.runtime.agent_runtime import get_interactive_run_limiter
from reuleauxcoder.domain.approval_engine import (
    ApprovalPolicyEngine,
    ToolApprovalContext,
    ToolSource,
)
from reuleauxcoder.domain.config.models import (
    AgentRegistryConfig,
    ApprovalAction,
    ApprovalConfig,
    ApprovalRuleConfig,
    CapabilityComponentConfig,
    CapabilityPackageConfig,
    ContextConfig,
    DEFAULT_BUILTIN_TOOL_COMPONENTS,
    DiagnosticsConfig,
    EnvironmentRequirementConfig,
    GitHubConfig,
    MemoryConfig,
    MCPServerConfig,
    ModeConfig,
    MODEL_PROFILE_ADMIN_INPUT_FIELDS,
    ModelProfileConfig,
    PersistenceConfig,
    PromptConfig,
    ProviderApiFeatures,
    ProviderConfig,
    RunLimitsConfig,
    RuntimeProfilesConfig,
    SandboxProviderConfig,
    SkillsConfig,
    StreamRecoveryConfig,
    build_agent_run_snapshot,
    ensure_default_capability_components,
    ensure_default_capability_packages,
    ensure_default_environment_agent_registry,
    infer_provider_compat,
)
from labrastro_server.services.capability_packages import (
    CapabilityPackageIngestError,
    CapabilityPackageInstaller,
)
from reuleauxcoder.domain.config.schema import BUILTIN_MODES, DEFAULTS, DEFAULT_ACTIVE_MODE
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.permission_gateway import (
    PermissionGateway,
    PermissionRequest,
    PermissionSubject,
    PermissionTarget,
)
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.services.llm.diagnostics import summarize_tool_diagnostic_events
from reuleauxcoder.services.providers.model_capabilities import (
    ModelCapabilityCatalogService,
    capability_recommendation,
    capability_source_label,
    utc_now_iso,
)
from reuleauxcoder.services.providers.manager import ProviderManager
from reuleauxcoder.extensions.tools.registry import build_tools
from reuleauxcoder.interfaces.vscode.registration import VSCODE_CHAT_PROFILE


ProviderTestHandler = Callable[[ProviderConfig, str, str], dict[str, Any]]
ProviderModelsHandler = Callable[[ProviderConfig], dict[str, Any]]
ConfigReloadHandler = Callable[[], None]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AdminConfigResult:
    ok: bool
    payload: dict[str, Any]
    status: int = 200


SETTINGS_UI_ACTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "settings.environment_requirements.refresh_manifest",
        "feature_id": "environment_requirements",
        "description": "Refresh the environment capability manifest in settings.",
        "triggers": [{"kind": "button", "value": "refreshEnvironmentManifest"}],
    },
    {
        "id": "settings.environment_requirements.run_check",
        "feature_id": "environment_requirements",
        "description": "Run the environment checker agent from settings.",
        "triggers": [{"kind": "button", "value": "runEnvironment(check)"}],
    },
    {
        "id": "settings.environment_requirements.run_configure",
        "feature_id": "environment_requirements",
        "description": "Run the environment configurator agent from settings.",
        "triggers": [{"kind": "button", "value": "runEnvironment(configure)"}],
    },
    {
        "id": "settings.capability_packages.ingest",
        "feature_id": "capability_packages",
        "description": "Start capability package ingest from source material.",
        "triggers": [{"kind": "button", "value": "startCapabilityPackageIngest"}],
    },
    {
        "id": "settings.capability_packages.accept_draft",
        "feature_id": "capability_packages",
        "description": "Accept a generated capability package draft.",
        "triggers": [{"kind": "button", "value": "acceptCapabilityPackageDraft"}],
    },
)


class RemoteAdminConfigManager:
    """Read and update host-owned provider and model-profile config."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        *,
        reload_handler: ConfigReloadHandler | None = None,
        provider_test_handler: ProviderTestHandler | None = None,
        provider_models_handler: ProviderModelsHandler | None = None,
    ) -> None:
        self.config_path = Path(config_path or ConfigLoader.GLOBAL_CONFIG_PATH)
        self.reload_handler = reload_handler
        self.provider_test_handler = provider_test_handler
        self.provider_models_handler = provider_models_handler
        self._lock = threading.Lock()
        self.model_capability_catalog = ModelCapabilityCatalogService(
            self.config_path.parent / "model-capabilities"
        )

    def status(self) -> dict[str, Any]:
        modes = self.list_modes()
        data = self._load_data()
        agents = self._agent_profile_views(data, modes["active_mode"])
        return {
            "providers": self.list_providers()["providers"],
            "provider_model_catalog": self.list_provider_model_catalog(data)["models"],
            "agent_profiles": agents,
            "active_agent_model": self._active_agent_model(agents, modes["active_mode"]),
            "model_profiles": self.list_model_profiles()["model_profiles"],
            "active_main": self._models_data().get("active_main"),
            "active_sub": self._models_data().get("active_sub"),
            "modes": modes["modes"],
            "active_mode": modes["active_mode"],
            "server_settings": self.read_server_settings()["settings"],
            "model_capabilities": self.model_capabilities_status()["model_capabilities"],
            "agent_runs": get_interactive_run_limiter().snapshot(),
        }

    def chat_config(self) -> dict[str, Any]:
        modes = self.list_modes()
        data = self._load_data()
        agents = self._agent_profile_views(data, modes["active_mode"])
        model_profiles = self._model_profile_state(data)
        return {
            "modes": modes["modes"],
            "active_mode": modes["active_mode"],
            "model_profiles": model_profiles["model_profiles"],
            "active_main": model_profiles["active_main"],
            "active_sub": model_profiles["active_sub"],
            "active_agent_model": self._active_agent_model(
                agents,
                modes["active_mode"],
            ),
        }

    def config_etag(self, data: dict[str, Any] | None = None) -> str:
        payload = json.dumps(
            data if data is not None else self._load_data(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        import hashlib

        return f'"sha256-{hashlib.sha256(payload).hexdigest()}"'

    def model_capabilities_settings(
        self, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = data if data is not None else self._load_data()
        raw = data.get("model_capabilities", {})
        raw = raw if isinstance(raw, dict) else {}
        return {
            "enabled": bool(raw.get("enabled", True)),
            "interval_sec": max(60, int(raw.get("interval_sec", 86400) or 86400)),
        }

    def model_capabilities_status(self) -> dict[str, Any]:
        settings = self.model_capabilities_settings()
        return {
            "model_capabilities": self.model_capability_catalog.status(
                enabled=bool(settings["enabled"]),
                interval_sec=int(settings["interval_sec"]),
            )
        }

    def list_model_capabilities(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "model_capabilities": {
                **self.model_capabilities_status()["model_capabilities"],
                "models": self.model_capability_catalog.list_capabilities(
                    provider=str(payload.get("provider") or payload.get("provider_id") or ""),
                    model=str(payload.get("model") or payload.get("model_id") or ""),
                ),
            }
        }

    def refresh_model_capabilities(self) -> AdminConfigResult:
        result = self.model_capability_catalog.refresh()
        status = 200 if result.get("ok") is True else 502
        return AdminConfigResult(bool(result.get("ok")), result, status)

    def apply_model_capability_recommendation(
        self, payload: dict[str, Any]
    ) -> AdminConfigResult:
        raw_ids = payload.get("profile_ids")
        profile_ids = (
            [str(item).strip() for item in raw_ids if str(item).strip()]
            if isinstance(raw_ids, list)
            else [str(payload.get("profile_id") or payload.get("id") or "").strip()]
        )
        profile_ids = [item for item in dict.fromkeys(profile_ids) if item]
        if not profile_ids:
            return AdminConfigResult(False, {"error": "profile_id_required"}, 400)

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            profiles = self._model_profiles(data)
            updated: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            for profile_id in profile_ids:
                item = profiles.get(profile_id)
                if not isinstance(item, dict):
                    errors.append({"profile_id": profile_id, "error": "profile_not_found"})
                    continue
                provider = self._provider_config_for_profile(data, item)
                capability = self.model_capability_catalog.lookup(
                    provider or str(item.get("provider") or ""),
                    str(item.get("model") or profile_id),
                )
                if capability is None:
                    errors.append({"profile_id": profile_id, "error": "capability_not_found"})
                    continue
                if capability.max_output_tokens:
                    item["max_tokens"] = capability.max_output_tokens
                if capability.max_context_tokens:
                    item["max_context_tokens"] = capability.max_context_tokens
                applied_at = utc_now_iso()
                source = (
                    capability_source_label(capability)
                    if capability is not None
                    else "catalog"
                )
                profile = ModelProfileConfig.from_dict(profile_id, item)
                profiles[profile_id] = {
                    **profile.to_dict(),
                    "capability_user_configured": False,
                    "capability_applied_at": applied_at,
                    "capability_source": source,
                }
                updated.append(self._profile_view(profile_id, profiles[profile_id], data=data))
            if not updated:
                return AdminConfigResult(
                    False,
                    {"error": "capability_apply_failed", "errors": errors},
                    404,
                )
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "updated_profiles": updated,
                    "errors": errors,
                    **self.list_model_profiles(),
                },
            )

    def list_modes(self) -> dict[str, Any]:
        data = self._load_data()
        raw_modes = data.get("modes", {})
        modes_data = raw_modes if isinstance(raw_modes, dict) else {}
        profiles = modes_data.get("profiles", {})
        custom_profiles = profiles if isinstance(profiles, dict) else {}
        profile_items = deepcopy(BUILTIN_MODES)
        for name, value in custom_profiles.items():
            base = profile_items.get(name)
            if isinstance(base, dict) and isinstance(value, dict):
                merged = deepcopy(base)
                merged.update(value)
                profile_items[name] = merged
            else:
                profile_items[name] = value
        modes: list[dict[str, Any]] = []
        for name in sorted(profile_items):
            item = profile_items.get(name)
            mode = item if isinstance(item, dict) else {}
            tools = mode.get("tools", [])
            modes.append(
                {
                    "name": str(name),
                    "description": str(mode.get("description") or ""),
                    "tools": [str(tool) for tool in tools] if isinstance(tools, list) else [],
                    "prompt_append": str(mode.get("prompt_append") or ""),
                }
            )
        active_mode = str(modes_data.get("active") or "")
        if not active_mode and DEFAULT_ACTIVE_MODE in profile_items:
            active_mode = DEFAULT_ACTIVE_MODE
        if active_mode and active_mode not in profile_items:
            active_mode = ""
        return {"modes": modes, "active_mode": active_mode or None}

    def _agent_settings_from_data(
        self,
        data: dict[str, Any],
    ) -> tuple[AgentRegistryConfig, RuntimeProfilesConfig, RunLimitsConfig]:
        registry_data, profiles_data = ensure_default_environment_agent_registry(
            data.get("agent_registry", {})
            if isinstance(data.get("agent_registry"), dict)
            else {},
            data.get("runtime_profiles", {})
            if isinstance(data.get("runtime_profiles"), dict)
            else {},
        )
        return (
            AgentRegistryConfig.from_dict(registry_data),
            RuntimeProfilesConfig.from_dict(profiles_data),
            RunLimitsConfig.from_dict(
                data.get("run_limits", {})
                if isinstance(data.get("run_limits"), dict)
                else {}
            ),
        )

    def _tool_output_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        raw = data.get("tool_output", {})
        raw = raw if isinstance(raw, dict) else {}
        return {
            "max_chars": int(raw.get("max_chars", DEFAULTS["tool_output_max_chars"]) or DEFAULTS["tool_output_max_chars"]),
            "max_lines": int(raw.get("max_lines", DEFAULTS["tool_output_max_lines"]) or DEFAULTS["tool_output_max_lines"]),
            "store_full_output": bool(raw.get("store_full_output", DEFAULTS["tool_output_store_full"])),
            "store_dir": raw.get("store_dir", DEFAULTS["tool_output_store_dir"]),
        }

    def _context_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        raw = data.get("context", {})
        raw = raw if isinstance(raw, dict) else {}
        config = ContextConfig(
            snip_keep_recent_tools=int(raw.get("snip_keep_recent_tools", DEFAULTS["snip_keep_recent_tools"]) or 0),
            snip_threshold_chars=int(raw.get("snip_threshold_chars", DEFAULTS["snip_threshold_chars"]) or 0),
            snip_min_lines=int(raw.get("snip_min_lines", DEFAULTS["snip_min_lines"]) or 0),
            summarize_keep_recent_turns=int(raw.get("summarize_keep_recent_turns", DEFAULTS["summarize_keep_recent_turns"]) or 0),
            token_fudge_factor=float(raw.get("token_fudge_factor", DEFAULTS["token_fudge_factor"]) or 0),
        )
        return {
            "snip_keep_recent_tools": config.snip_keep_recent_tools,
            "snip_threshold_chars": config.snip_threshold_chars,
            "snip_min_lines": config.snip_min_lines,
            "summarize_keep_recent_turns": config.summarize_keep_recent_turns,
            "token_fudge_factor": config.token_fudge_factor,
        }

    def _approval_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        raw = data.get("approval", {})
        raw = raw if isinstance(raw, dict) else {}
        rules = []
        raw_rules = raw.get("rules", DEFAULTS["approval_rules"])
        if isinstance(raw_rules, list):
            for item in raw_rules:
                if not isinstance(item, dict):
                    continue
                rule = ApprovalRuleConfig(
                    tool_name=item.get("tool_name"),
                    tool_source=item.get("tool_source"),
                    mcp_server=item.get("mcp_server"),
                    effect_class=item.get("effect_class"),
                    profile=item.get("profile"),
                    action=item.get("action", "require_approval"),
                )
                rules.append(
                    {
                        "tool_name": rule.tool_name,
                        "tool_source": rule.tool_source,
                        "mcp_server": rule.mcp_server,
                        "effect_class": rule.effect_class,
                        "profile": rule.profile,
                        "action": rule.action,
                    }
                )
        approval = ApprovalConfig(
            default_mode=cast(
                ApprovalAction,
                str(raw.get("default_mode", DEFAULTS["approval_default_mode"]) or "require_approval"),
            ),
            rules=[
                ApprovalRuleConfig(
                    tool_name=rule.get("tool_name"),
                    tool_source=rule.get("tool_source"),
                    mcp_server=rule.get("mcp_server"),
                    effect_class=rule.get("effect_class"),
                    profile=rule.get("profile"),
                    action=rule.get("action", "require_approval"),
                )
                for rule in rules
            ],
        )
        return {
            "default_mode": approval.default_mode,
            "rules": [
                {
                    "tool_name": rule.tool_name,
                    "tool_source": rule.tool_source,
                    "mcp_server": rule.mcp_server,
                    "effect_class": rule.effect_class,
                    "profile": rule.profile,
                    "action": rule.action,
                }
                for rule in approval.rules
            ],
        }

    def _skills_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        raw = data.get("skills", {})
        raw = raw if isinstance(raw, dict) else {}
        config = SkillsConfig(
            enabled=bool(raw.get("enabled", True)),
            scan_project=bool(raw.get("scan_project", True)),
            scan_user=bool(raw.get("scan_user", True)),
            disabled=[
                str(name)
                for name in raw.get("disabled", [])
                if str(name).strip()
            ]
            if isinstance(raw.get("disabled", []), list)
            else [],
        )
        return {
            "enabled": config.enabled,
            "scan_project": config.scan_project,
            "scan_user": config.scan_user,
            "disabled": list(config.disabled),
        }

    def _prompt_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        raw = data.get("prompt", {})
        raw = raw if isinstance(raw, dict) else {}
        config = PromptConfig(system_append=str(raw.get("system_append", "") or ""))
        return {"system_append": config.system_append}

    def _modes_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        listed = self.list_modes()
        profiles = {
            str(item["name"]): {
                "description": str(item.get("description") or ""),
                "tools": [str(tool) for tool in item.get("tools", [])]
                if isinstance(item.get("tools", []), list)
                else [],
                "prompt_append": str(item.get("prompt_append") or ""),
            }
            for item in listed["modes"]
            if isinstance(item, dict) and item.get("name")
        }
        return {"active": listed["active_mode"], "profiles": profiles}

    def _memory_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        raw = data.get("memory", {})
        return MemoryConfig.from_dict(raw if isinstance(raw, dict) else {}).to_dict()

    def _persistence_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        raw = data.get("persistence", {})
        return PersistenceConfig.from_dict(raw if isinstance(raw, dict) else {}).to_dict()

    def read_server_settings(self) -> dict[str, Any]:
        data = self._load_data()
        raw_capability_packages = data.get("capability_packages", {})
        raw_capability_components = data.get("capability_components", {})
        agent_registry, runtime_profiles, run_limits = self._agent_settings_from_data(data)
        capability_packages = {
            str(package_id): CapabilityPackageConfig.from_dict(
                str(package_id), package_data
            ).to_dict()
            for package_id, package_data in ensure_default_capability_packages(
                raw_capability_packages
                if isinstance(raw_capability_packages, dict)
                else {}
            ).items()
            if isinstance(package_data, dict)
        }
        capability_components = {
            str(component_id): CapabilityComponentConfig.from_dict(
                str(component_id), component_data
            ).to_dict()
            for component_id, component_data in ensure_default_capability_components(
                raw_capability_components
                if isinstance(raw_capability_components, dict)
                else {}
            ).items()
            if isinstance(component_data, dict)
        }
        raw_github = data.get("github", {})
        github = GitHubConfig.from_dict(raw_github if isinstance(raw_github, dict) else {})
        raw_sandbox = data.get("sandbox_provider", {})
        sandbox_provider = SandboxProviderConfig.from_dict(
            raw_sandbox if isinstance(raw_sandbox, dict) else {}
        )
        model_capabilities = self.model_capabilities_settings(data)
        raw_diagnostics = data.get("diagnostics", {})
        diagnostics = DiagnosticsConfig.from_dict(
            raw_diagnostics if isinstance(raw_diagnostics, dict) else {}
        )
        return {
            "settings": {
                "agent_registry": agent_registry.to_dict(),
                "runtime_profiles": runtime_profiles.to_dict(),
                "run_limits": run_limits.to_dict(),
                "capability_packages": capability_packages,
                "capability_components": capability_components,
                "tool_output": self._tool_output_settings(data),
                "context": self._context_settings(data),
                "memory": self._memory_settings(data),
                "approval": self._approval_settings(data),
                "modes": self._modes_settings(data),
                "skills": self._skills_settings(data),
                "prompt": self._prompt_settings(data),
                "persistence": self._persistence_settings(data),
                "github": github.to_dict(mask_secret=True),
                "sandbox_provider": sandbox_provider.to_dict(),
                "model_capabilities": {
                    **model_capabilities,
                    "status": self.model_capability_catalog.status(
                        enabled=bool(model_capabilities["enabled"]),
                        interval_sec=int(model_capabilities["interval_sec"]),
                    ),
                },
                "diagnostics": diagnostics.to_dict(),
            },
            "agent_runs": get_interactive_run_limiter().snapshot(),
            "config_etag": self.config_etag(data),
        }

    def tool_diagnostic_stats(self) -> dict[str, Any]:
        return summarize_tool_diagnostic_events()

    def update_server_settings(self, payload: dict[str, Any]) -> AdminConfigResult:
        raw_settings = payload.get("settings")
        raw_agent_registry = payload.get("agent_registry")
        raw_runtime_profiles = payload.get("runtime_profiles")
        raw_run_limits = payload.get("run_limits")
        raw_capability_packages = payload.get("capability_packages")
        raw_capability_components = payload.get("capability_components")
        raw_tool_output = payload.get("tool_output")
        raw_context = payload.get("context")
        raw_memory = payload.get("memory")
        raw_approval = payload.get("approval")
        raw_modes = payload.get("modes")
        raw_skills = payload.get("skills")
        raw_prompt = payload.get("prompt")
        raw_persistence = payload.get("persistence")
        raw_github = payload.get("github")
        raw_sandbox = payload.get("sandbox_provider")
        raw_model_capabilities = payload.get("model_capabilities")
        raw_diagnostics = payload.get("diagnostics")
        if isinstance(raw_settings, dict) and raw_agent_registry is None:
            raw_agent_registry = raw_settings.get("agent_registry")
        if isinstance(raw_settings, dict) and raw_runtime_profiles is None:
            raw_runtime_profiles = raw_settings.get("runtime_profiles")
        if isinstance(raw_settings, dict) and raw_run_limits is None:
            raw_run_limits = raw_settings.get("run_limits")
        if isinstance(raw_settings, dict) and raw_capability_packages is None:
            raw_capability_packages = raw_settings.get("capability_packages")
        if isinstance(raw_settings, dict) and raw_capability_components is None:
            raw_capability_components = raw_settings.get("capability_components")
        if isinstance(raw_settings, dict) and raw_tool_output is None:
            raw_tool_output = raw_settings.get("tool_output")
        if isinstance(raw_settings, dict) and raw_context is None:
            raw_context = raw_settings.get("context")
        if isinstance(raw_settings, dict) and raw_memory is None:
            raw_memory = raw_settings.get("memory")
        if isinstance(raw_settings, dict) and raw_approval is None:
            raw_approval = raw_settings.get("approval")
        if isinstance(raw_settings, dict) and raw_modes is None:
            raw_modes = raw_settings.get("modes")
        if isinstance(raw_settings, dict) and raw_skills is None:
            raw_skills = raw_settings.get("skills")
        if isinstance(raw_settings, dict) and raw_prompt is None:
            raw_prompt = raw_settings.get("prompt")
        if isinstance(raw_settings, dict) and raw_persistence is None:
            raw_persistence = raw_settings.get("persistence")
        if isinstance(raw_settings, dict) and raw_github is None:
            raw_github = raw_settings.get("github")
        if isinstance(raw_settings, dict) and raw_sandbox is None:
            raw_sandbox = raw_settings.get("sandbox_provider")
        if isinstance(raw_settings, dict) and raw_model_capabilities is None:
            raw_model_capabilities = raw_settings.get("model_capabilities")
        if isinstance(raw_settings, dict) and raw_diagnostics is None:
            raw_diagnostics = raw_settings.get("diagnostics")
        if (
            not isinstance(raw_agent_registry, dict)
            and not isinstance(raw_runtime_profiles, dict)
            and not isinstance(raw_run_limits, dict)
            and not isinstance(raw_capability_packages, dict)
            and not isinstance(raw_capability_components, dict)
            and not isinstance(raw_tool_output, dict)
            and not isinstance(raw_context, dict)
            and not isinstance(raw_memory, dict)
            and not isinstance(raw_approval, dict)
            and not isinstance(raw_modes, dict)
            and not isinstance(raw_skills, dict)
            and not isinstance(raw_prompt, dict)
            and not isinstance(raw_persistence, dict)
            and not isinstance(raw_github, dict)
            and not isinstance(raw_sandbox, dict)
            and not isinstance(raw_model_capabilities, dict)
            and not isinstance(raw_diagnostics, dict)
        ):
            return AdminConfigResult(False, {"error": "server_settings_required"}, 400)
        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            run_limits = RunLimitsConfig.from_dict(
                previous_data.get("run_limits", {})
                if isinstance(previous_data.get("run_limits"), dict)
                else {}
            )
            previous_packages = (
                previous_data.get("capability_packages", {})
                if isinstance(previous_data.get("capability_packages"), dict)
                else {}
            )
            previous_components = (
                previous_data.get("capability_components", {})
                if isinstance(previous_data.get("capability_components"), dict)
                else {}
            )
            raw_components_for_parse = (
                raw_capability_components
                if isinstance(raw_capability_components, dict)
                else previous_components
            )
            try:
                capability_components = {
                    str(component_id): CapabilityComponentConfig.from_dict(
                        str(component_id), component_data
                    )
                    for component_id, component_data in raw_components_for_parse.items()
                    if isinstance(component_data, dict)
                }
            except Exception as exc:
                return AdminConfigResult(
                    False,
                    {"error": "invalid_capability_components", "message": str(exc)},
                    400,
                )
            raw_packages_for_parse = (
                raw_capability_packages
                if isinstance(raw_capability_packages, dict)
                else previous_packages
            )
            try:
                capability_packages = {
                    str(package_id): CapabilityPackageConfig.from_dict(
                        str(package_id), package_data
                    )
                    for package_id, package_data in ensure_default_capability_packages(
                        raw_packages_for_parse
                    ).items()
                    if isinstance(package_data, dict)
                }
            except Exception as exc:
                return AdminConfigResult(
                    False,
                    {"error": "invalid_capability_packages", "message": str(exc)},
                    400,
                )
            if isinstance(raw_capability_packages, dict):
                data["capability_packages"] = {
                    package_id: package.to_dict()
                    for package_id, package in capability_packages.items()
                }
            if isinstance(raw_capability_components, dict):
                data["capability_components"] = {
                    component_id: component.to_dict()
                    for component_id, component in capability_components.items()
                }
            if isinstance(raw_tool_output, dict):
                previous_tool_output = (
                    previous_data.get("tool_output", {})
                    if isinstance(previous_data.get("tool_output"), dict)
                    else {}
                )
                merged_tool_output = ConfigLoader()._merge_dicts(
                    previous_tool_output,
                    raw_tool_output,
                )
                try:
                    tool_output = {
                        "max_chars": int(
                            merged_tool_output.get(
                                "max_chars", DEFAULTS["tool_output_max_chars"]
                            )
                            or DEFAULTS["tool_output_max_chars"]
                        ),
                        "max_lines": int(
                            merged_tool_output.get(
                                "max_lines", DEFAULTS["tool_output_max_lines"]
                            )
                            or DEFAULTS["tool_output_max_lines"]
                        ),
                        "store_full_output": bool(
                            merged_tool_output.get(
                                "store_full_output",
                                DEFAULTS["tool_output_store_full"],
                            )
                        ),
                        "store_dir": merged_tool_output.get("store_dir"),
                    }
                except Exception as exc:
                    return AdminConfigResult(
                        False,
                        {"error": "invalid_tool_output", "message": str(exc)},
                        400,
                    )
                if tool_output["max_chars"] < 1 or tool_output["max_lines"] < 1:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_tool_output",
                            "message": "tool_output.max_chars and max_lines must be positive",
                        },
                        400,
                    )
                if tool_output["store_dir"] is not None:
                    tool_output["store_dir"] = str(tool_output["store_dir"])
                data["tool_output"] = tool_output
            if isinstance(raw_context, dict):
                previous_context = (
                    previous_data.get("context", {})
                    if isinstance(previous_data.get("context"), dict)
                    else {}
                )
                merged_context = ConfigLoader()._merge_dicts(
                    previous_context,
                    raw_context,
                )
                try:
                    context = {
                        "snip_keep_recent_tools": int(
                            merged_context.get(
                                "snip_keep_recent_tools",
                                DEFAULTS["snip_keep_recent_tools"],
                            )
                            or 0
                        ),
                        "snip_threshold_chars": int(
                            merged_context.get(
                                "snip_threshold_chars",
                                DEFAULTS["snip_threshold_chars"],
                            )
                            or 0
                        ),
                        "snip_min_lines": int(
                            merged_context.get(
                                "snip_min_lines", DEFAULTS["snip_min_lines"]
                            )
                            or 0
                        ),
                        "summarize_keep_recent_turns": int(
                            merged_context.get(
                                "summarize_keep_recent_turns",
                                DEFAULTS["summarize_keep_recent_turns"],
                            )
                            or 0
                        ),
                        "token_fudge_factor": float(
                            merged_context.get(
                                "token_fudge_factor",
                                DEFAULTS["token_fudge_factor"],
                            )
                            or 0
                        ),
                    }
                except Exception as exc:
                    return AdminConfigResult(
                        False,
                        {"error": "invalid_context", "message": str(exc)},
                        400,
                    )
                if (
                    context["snip_keep_recent_tools"] < 0
                    or context["snip_threshold_chars"] < 1
                    or context["snip_min_lines"] < 1
                    or context["summarize_keep_recent_turns"] < 0
                    or context["token_fudge_factor"] <= 0
                ):
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_context",
                            "message": "context values must be positive where applicable",
                        },
                        400,
                    )
                data["context"] = context
            if isinstance(raw_memory, dict):
                previous_memory = (
                    previous_data.get("memory", {})
                    if isinstance(previous_data.get("memory"), dict)
                    else {}
                )
                merged_memory = ConfigLoader()._merge_dicts(
                    previous_memory,
                    raw_memory,
                )
                try:
                    memory = MemoryConfig.from_dict(merged_memory)
                except Exception as exc:
                    return AdminConfigResult(
                        False,
                        {"error": "invalid_memory", "message": str(exc)},
                        400,
                    )
                if memory.backend not in {"sqlite", "postgres", "memory"}:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_memory",
                            "message": "memory.backend must be one of sqlite, postgres, memory",
                        },
                        400,
                    )
                if memory.enabled and not memory.default_agent_id.strip():
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_memory",
                            "message": "memory.default_agent_id is required when memory.enabled is true",
                        },
                        400,
                    )
                if memory.token_budget < 1:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_memory",
                            "message": "memory.token_budget must be positive",
                        },
                        400,
                    )
                data["memory"] = memory.to_dict()
            if isinstance(raw_approval, dict):
                previous_approval = (
                    previous_data.get("approval", {})
                    if isinstance(previous_data.get("approval"), dict)
                    else {}
                )
                merged_approval = ConfigLoader()._merge_dicts(
                    previous_approval,
                    raw_approval,
                )
                valid_actions = {"allow", "warn", "require_approval", "deny"}
                default_mode = str(
                    merged_approval.get(
                        "default_mode", DEFAULTS["approval_default_mode"]
                    )
                    or "require_approval"
                )
                if default_mode not in valid_actions:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_approval",
                            "message": "approval.default_mode must be one of allow, warn, require_approval, deny",
                        },
                        400,
                    )
                rules: list[dict[str, Any]] = []
                raw_rules = merged_approval.get("rules", [])
                if not isinstance(raw_rules, list):
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_approval",
                            "message": "approval.rules must be a list",
                        },
                        400,
                    )
                for index, item in enumerate(raw_rules):
                    if not isinstance(item, dict):
                        return AdminConfigResult(
                            False,
                            {
                                "error": "invalid_approval",
                                "message": f"approval.rules[{index}] must be an object",
                            },
                            400,
                        )
                    action = str(item.get("action") or "require_approval")
                    if action not in valid_actions:
                        return AdminConfigResult(
                            False,
                            {
                                "error": "invalid_approval",
                                "message": f"approval.rules[{index}].action must be one of allow, warn, require_approval, deny",
                            },
                            400,
                        )
                    rule: dict[str, Any] = {"action": action}
                    for field in (
                        "tool_name",
                        "tool_source",
                        "mcp_server",
                        "effect_class",
                        "profile",
                    ):
                        value = item.get(field)
                        if value is not None and str(value).strip():
                            rule[field] = str(value).strip()
                    rules.append(rule)
                data["approval"] = {"default_mode": default_mode, "rules": rules}
            if isinstance(raw_modes, dict):
                previous_modes = (
                    previous_data.get("modes", {})
                    if isinstance(previous_data.get("modes"), dict)
                    else {}
                )
                merged_modes = dict(previous_modes)
                for key, value in raw_modes.items():
                    if key != "profiles":
                        merged_modes[key] = value
                if "profiles" in raw_modes:
                    merged_modes["profiles"] = raw_modes.get("profiles")
                profiles_raw = merged_modes.get("profiles", {})
                if not isinstance(profiles_raw, dict) or not profiles_raw:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_modes",
                            "message": "modes.profiles must be a non-empty object",
                        },
                        400,
                    )
                profiles: dict[str, Any] = {}
                for mode_name, mode_data in profiles_raw.items():
                    if not isinstance(mode_data, dict):
                        return AdminConfigResult(
                            False,
                            {
                                "error": "invalid_modes",
                                "message": f"modes.profiles.{mode_name} must be an object",
                            },
                            400,
                        )
                    mode = ModeConfig.from_dict(str(mode_name), mode_data)
                    profiles[str(mode_name)] = {
                        "description": mode.description,
                        "tools": list(mode.tools),
                        "prompt_append": mode.prompt_append,
                    }
                active = str(merged_modes.get("active") or "").strip()
                if not active:
                    active = DEFAULT_ACTIVE_MODE if DEFAULT_ACTIVE_MODE in profiles else next(iter(profiles))
                if active not in profiles:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_modes",
                            "message": "modes.active must exist in modes.profiles",
                        },
                        400,
                    )
                data["modes"] = {"active": active, "profiles": profiles}
            if isinstance(raw_skills, dict):
                previous_skills = (
                    previous_data.get("skills", {})
                    if isinstance(previous_data.get("skills"), dict)
                    else {}
                )
                merged_skills = ConfigLoader()._merge_dicts(
                    previous_skills,
                    raw_skills,
                )
                disabled_raw = merged_skills.get("disabled", [])
                if disabled_raw is None:
                    disabled_raw = []
                if not isinstance(disabled_raw, list):
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_skills",
                            "message": "skills.disabled must be a list",
                        },
                        400,
                    )
                data["skills"] = {
                    "enabled": bool(merged_skills.get("enabled", True)),
                    "scan_project": bool(merged_skills.get("scan_project", True)),
                    "scan_user": bool(merged_skills.get("scan_user", True)),
                    "disabled": [
                        str(name).strip()
                        for name in disabled_raw
                        if str(name).strip()
                    ],
                }
            if isinstance(raw_prompt, dict):
                previous_prompt = (
                    previous_data.get("prompt", {})
                    if isinstance(previous_data.get("prompt"), dict)
                    else {}
                )
                merged_prompt = ConfigLoader()._merge_dicts(
                    previous_prompt,
                    raw_prompt,
                )
                data["prompt"] = {
                    "system_append": str(merged_prompt.get("system_append", "") or "")
                }
            if isinstance(raw_persistence, dict):
                blocked = [
                    field
                    for field in ("backend", "database_url", "auto_migrate")
                    if field in raw_persistence
                ]
                if blocked:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "read_only_persistence_field",
                            "fields": blocked,
                            "message": "persistence backend, database_url, and auto_migrate are deployment settings",
                        },
                        400,
                    )
                previous_persistence = (
                    previous_data.get("persistence", {})
                    if isinstance(previous_data.get("persistence"), dict)
                    else {}
                )
                merged_persistence = ConfigLoader()._merge_dicts(
                    previous_persistence,
                    raw_persistence,
                )
                try:
                    persistence = PersistenceConfig.from_dict(merged_persistence)
                except Exception as exc:
                    return AdminConfigResult(
                        False,
                        {"error": "invalid_persistence", "message": str(exc)},
                        400,
                    )
                if persistence.retention_days < 0:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_persistence",
                            "message": "persistence.retention_days must be zero or positive",
                        },
                        400,
                    )
                if (
                    persistence.event_payload_compress_threshold_bytes < 1
                    or persistence.maintenance_interval_sec < 1
                ):
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_persistence",
                            "message": "persistence event payload and maintenance values must be positive",
                        },
                        400,
                    )
                data["persistence"] = persistence.to_dict()
            agent_settings_changed = any(
                isinstance(value, dict)
                for value in (raw_agent_registry, raw_runtime_profiles, raw_run_limits)
            )
            if agent_settings_changed:
                if raw_agent_registry is not None and not isinstance(raw_agent_registry, dict):
                    return AdminConfigResult(
                        False,
                        {"error": "invalid_agent_registry", "message": "agent_registry must be an object"},
                        400,
                    )
                if raw_runtime_profiles is not None and not isinstance(raw_runtime_profiles, dict):
                    return AdminConfigResult(
                        False,
                        {"error": "invalid_runtime_profiles", "message": "runtime_profiles must be an object"},
                        400,
                    )
                if raw_run_limits is not None and not isinstance(raw_run_limits, dict):
                    return AdminConfigResult(
                        False,
                        {"error": "invalid_run_limits", "message": "run_limits must be an object"},
                        400,
                    )
                merged_registry = (
                    raw_agent_registry
                    if isinstance(raw_agent_registry, dict)
                    else (
                        previous_data.get("agent_registry", {})
                        if isinstance(previous_data.get("agent_registry"), dict)
                        else {}
                    )
                )
                merged_profiles = (
                    raw_runtime_profiles
                    if isinstance(raw_runtime_profiles, dict)
                    else (
                        previous_data.get("runtime_profiles", {})
                        if isinstance(previous_data.get("runtime_profiles"), dict)
                        else {}
                    )
                )
                merged_limits = ConfigLoader()._merge_dicts(
                    previous_data.get("run_limits", {})
                    if isinstance(previous_data.get("run_limits"), dict)
                    else {},
                    raw_run_limits if isinstance(raw_run_limits, dict) else {},
                )
                registry_data, profile_data = ensure_default_environment_agent_registry(
                    merged_registry,
                    merged_profiles,
                )
                try:
                    agent_registry = AgentRegistryConfig.from_dict(registry_data)
                    runtime_profiles = RuntimeProfilesConfig.from_dict(profile_data)
                    run_limits = RunLimitsConfig.from_dict(merged_limits)
                except Exception as exc:
                    return AdminConfigResult(
                        False,
                        {"error": "invalid_agent_settings", "message": str(exc)},
                        400,
                    )
                invalid_runtime = self._validate_agent_settings(
                    agent_registry,
                    runtime_profiles,
                    run_limits,
                    capability_packages=capability_packages,
                )
                if invalid_runtime is not None:
                    return invalid_runtime
                data["agent_registry"] = agent_registry.to_dict()
                data["runtime_profiles"] = runtime_profiles.to_dict()
                data["run_limits"] = run_limits.to_dict()
            if isinstance(raw_github, dict):
                previous_github = (
                    previous_data.get("github", {})
                    if isinstance(previous_data.get("github"), dict)
                    else {}
                )
                merged_github = ConfigLoader()._merge_dicts(
                    previous_github,
                    raw_github,
                )
                try:
                    github = GitHubConfig.from_dict(merged_github)
                except Exception as exc:
                    return AdminConfigResult(
                        False,
                        {"error": "invalid_github", "message": str(exc)},
                        400,
                    )
                if github.reconcile_interval_sec < 1:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_github",
                            "message": "github.reconcile_interval_sec must be positive",
                        },
                        400,
                    )
                if github.enabled:
                    missing_fields = [
                        field
                        for field, value in (
                            ("app_id", github.app_id),
                            ("installation_id", github.installation_id),
                            ("private_key_path", github.private_key_path),
                            ("webhook_secret", github.webhook_secret),
                        )
                        if not value
                    ]
                    if missing_fields:
                        return AdminConfigResult(
                            False,
                            {
                                "error": "invalid_github",
                                "fields": missing_fields,
                                "message": "github enabled requires app_id, installation_id, private_key_path, and webhook_secret",
                            },
                            400,
                        )
                    persistence_data = (
                        data.get("persistence", {})
                        if isinstance(data.get("persistence"), dict)
                        else {}
                    )
                    persistence = PersistenceConfig.from_dict(persistence_data)
                    if persistence.backend == "memory" or not persistence.database_url:
                        return AdminConfigResult(
                            False,
                            {
                                "error": "invalid_github",
                                "message": "github enabled requires Postgres persistence.database_url",
                            },
                            400,
                        )
                data["github"] = github.to_dict()
            if isinstance(raw_sandbox, dict):
                previous_sandbox = (
                    previous_data.get("sandbox_provider", {})
                    if isinstance(previous_data.get("sandbox_provider"), dict)
                    else {}
                )
                merged_sandbox = ConfigLoader()._merge_dicts(
                    previous_sandbox,
                    raw_sandbox,
                )
                try:
                    sandbox_provider = SandboxProviderConfig.from_dict(merged_sandbox)
                except Exception as exc:
                    return AdminConfigResult(
                        False,
                        {"error": "invalid_sandbox_provider", "message": str(exc)},
                        400,
                    )
                if sandbox_provider.type not in {"docker", "external", "k8s", "none"}:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_sandbox_provider",
                            "message": "sandbox_provider.type must be one of docker, external, k8s, none",
                        },
                        400,
                    )
                if sandbox_provider.idle_ttl_seconds < 1:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_sandbox_provider",
                            "message": "sandbox_provider.idle_ttl_seconds must be positive",
                        },
                        400,
                    )
                if sandbox_provider.type == "docker" and not sandbox_provider.host_base_url:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_sandbox_provider",
                            "message": "sandbox_provider.host_base_url is required when sandbox_provider.type is docker",
                        },
                        400,
                    )
                data["sandbox_provider"] = sandbox_provider.to_dict()
            if isinstance(raw_model_capabilities, dict):
                previous_model_capabilities = (
                    previous_data.get("model_capabilities", {})
                    if isinstance(previous_data.get("model_capabilities"), dict)
                    else {}
                )
                merged_model_capabilities = ConfigLoader()._merge_dicts(
                    previous_model_capabilities,
                    raw_model_capabilities,
                )
                try:
                    model_capabilities = {
                        "enabled": bool(merged_model_capabilities.get("enabled", True)),
                        "interval_sec": max(
                            60,
                            int(
                                merged_model_capabilities.get("interval_sec", 86400)
                                or 86400
                            ),
                        ),
                    }
                except Exception as exc:
                    return AdminConfigResult(
                        False,
                        {
                            "error": "invalid_model_capabilities",
                            "message": str(exc),
                        },
                        400,
                    )
                data["model_capabilities"] = model_capabilities
            if isinstance(raw_diagnostics, dict):
                previous_diagnostics = (
                    previous_data.get("diagnostics", {})
                    if isinstance(previous_data.get("diagnostics"), dict)
                    else {}
                )
                merged_diagnostics = ConfigLoader()._merge_dicts(
                    previous_diagnostics,
                    raw_diagnostics,
                )
                diagnostics = DiagnosticsConfig.from_dict(merged_diagnostics)
                data["diagnostics"] = diagnostics.to_dict()
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            if isinstance(raw_model_capabilities, dict):
                settings = self.model_capabilities_settings()
                self.model_capability_catalog.stop_periodic()
                self.model_capability_catalog.start_periodic(
                    enabled=bool(settings["enabled"]),
                    interval_sec=int(settings["interval_sec"]),
                )
            if agent_settings_changed:
                get_interactive_run_limiter().configure(
                    max_running_agents=run_limits.max_running_agents,
                    max_shells_per_agent=run_limits.max_shells_per_agent,
                )
            return AdminConfigResult(
                True,
                {"ok": True, **self.read_server_settings()},
            )

    def _validate_agent_settings(
        self,
        agent_registry: AgentRegistryConfig,
        runtime_profiles: RuntimeProfilesConfig,
        run_limits: RunLimitsConfig,
        *,
        capability_packages: dict[str, CapabilityPackageConfig],
    ) -> AdminConfigResult | None:
        if run_limits.max_running_agents < 1 or run_limits.max_shells_per_agent < 1:
            return AdminConfigResult(
                False,
                {
                    "error": "invalid_run_limits",
                    "message": "run_limits values must be positive integers",
                },
                400,
            )
        missing_profiles = [
            agent_id
            for agent_id, agent in agent_registry.agents.items()
            if agent.runtime_profile
            and agent.runtime_profile not in runtime_profiles.profiles
        ]
        if missing_profiles:
            return AdminConfigResult(
                False,
                {
                    "error": "invalid_agent_registry",
                    "message": (
                        "Agent runtime_profile references must exist: "
                        + ", ".join(sorted(missing_profiles))
                    ),
                },
                400,
            )
        missing_packages = [
            f"{agent_id}:{package_ref}"
            for agent_id, agent in agent_registry.agents.items()
            for package_ref in agent.capability_refs
            if package_ref not in capability_packages
        ]
        if missing_packages:
            return AdminConfigResult(
                False,
                {
                    "error": "invalid_agent_registry",
                    "message": (
                        "agent capability package references must exist: "
                        + ", ".join(sorted(missing_packages))
                    ),
                },
                400,
            )
        return None

    def accept_capability_package_draft(self, payload: dict[str, Any]) -> AdminConfigResult:
        raw_draft = payload.get("draft")
        if not isinstance(raw_draft, dict):
            raw_draft = payload
        package_id = str(
            raw_draft.get("id") or payload.get("package_id") or payload.get("id") or ""
        ).strip()
        if not package_id:
            return AdminConfigResult(False, {"error": "capability_package_id_required"}, 400)

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            try:
                install_result = CapabilityPackageInstaller().install_draft(
                    data,
                    raw_draft,
                    package_id=package_id,
                )
            except CapabilityPackageIngestError as exc:
                return AdminConfigResult(
                    False,
                    {"error": exc.error, "message": exc.message},
                    int(exc.status),
                )
            except Exception as exc:
                return AdminConfigResult(
                    False,
                    {"error": "invalid_capability_package_draft", "message": str(exc)},
                    400,
                )
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "package_id": install_result.package_id,
                    "capability_package": install_result.package.to_dict(),
                    "components": [
                        self._capability_component_view(data, component_id)
                        for component_id in install_result.component_ids
                    ],
                    **self.read_server_settings(),
                },
            )

    def delete_capability_package(self, payload: dict[str, Any]) -> AdminConfigResult:
        package_id = str(payload.get("package_id") or payload.get("id") or "").strip()
        if not package_id:
            return AdminConfigResult(False, {"error": "capability_package_id_required"}, 400)
        if package_id == "environment":
            return AdminConfigResult(False, {"error": "builtin_capability_package"}, 400)
        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            packages = data.get("capability_packages", {})
            if not isinstance(packages, dict) or package_id not in packages:
                return AdminConfigResult(False, {"error": "capability_package_not_found"}, 404)
            package = CapabilityPackageConfig.from_dict(
                package_id,
                packages.get(package_id) if isinstance(packages.get(package_id), dict) else {},
            )
            del packages[package_id]
            components = data.get("capability_components", {})
            if not isinstance(components, dict):
                components = {}
                data["capability_components"] = components
            removed_components: list[str] = []
            installer = CapabilityPackageInstaller()
            for component_id in package.components:
                raw_component = components.get(component_id)
                if not isinstance(raw_component, dict):
                    continue
                component = CapabilityComponentConfig.from_dict(component_id, raw_component)
                component.package_ids = [
                    item for item in component.package_ids if item != package_id
                ]
                if component.package_ids:
                    components[component_id] = component.to_dict()
                    installer.materialize_component(data, component)
                    continue
                del components[component_id]
                removed_components.append(component_id)
                installer.remove_materialized_component(data, component)
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "package_id": package_id,
                    "deleted": True,
                    "removed_components": removed_components,
                    **self.read_server_settings(),
                },
            )

    def enable_capability_package(self, payload: dict[str, Any]) -> AdminConfigResult:
        package_id = str(payload.get("package_id") or payload.get("id") or "").strip()
        if not package_id:
            return AdminConfigResult(False, {"error": "capability_package_id_required"}, 400)
        enabled = _bool_field(payload, "enabled", True)
        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            packages = ensure_default_capability_packages(
                data.get("capability_packages", {})
                if isinstance(data.get("capability_packages"), dict)
                else {}
            )
            raw_package = packages.get(package_id)
            if not isinstance(raw_package, dict):
                return AdminConfigResult(False, {"error": "capability_package_not_found"}, 404)
            package = CapabilityPackageConfig.from_dict(package_id, raw_package)
            package.enabled = enabled
            packages[package_id] = package.to_dict()
            data["capability_packages"] = packages
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "package_id": package_id,
                    "enabled": enabled,
                    "capability_package": package.to_dict(),
                    **self.read_server_settings(),
                },
            )

    def _capability_component_view(
        self,
        data: dict[str, Any],
        component_id: str,
    ) -> dict[str, Any]:
        components = data.get("capability_components", {})
        raw = components.get(component_id) if isinstance(components, dict) else None
        if not isinstance(raw, dict):
            return {"id": component_id}
        view = CapabilityComponentConfig.from_dict(component_id, raw).to_dict()
        view["id"] = component_id
        return view

    def list_environment_requirements(self) -> dict[str, Any]:
        data = self._load_data()
        return {
            "environment_requirements": self._admin_resource_views(
                data, "environment_requirement"
            ),
        }

    def list_mcp_servers(self) -> dict[str, Any]:
        data = self._load_data()
        return {
            "mcp_servers": self._admin_resource_views(data, "mcp"),
        }

    def environment_requirements_dashboard(self) -> dict[str, Any]:
        data = self._load_data()
        items = [
            self._admin_resource_dashboard_item("environment_requirement", item)
            for item in self._admin_resource_views(data, "environment_requirement")
        ]
        return {
            "items": items,
            "summary": _admin_resource_dashboard_summary(items),
        }

    def mcp_servers_dashboard(self) -> dict[str, Any]:
        data = self._load_data()
        items = [
            self._admin_resource_dashboard_item("mcp", item)
            for item in self._admin_resource_views(data, "mcp")
        ]
        return {
            "items": items,
            "summary": _admin_resource_dashboard_summary(items),
        }

    def environment_requirements_behavior_catalog(self) -> dict[str, Any]:
        data = self._load_data()
        chat_commands = self._chat_command_catalog()
        mention_providers = self._mention_provider_catalog(data)
        return {
            "user_actions": self._user_action_catalog(chat_commands, mention_providers),
            "chat_commands": chat_commands,
            "mention_providers": mention_providers,
            "ui_actions": self._ui_action_catalog(),
            "agent_tools": self._agent_tool_catalog(data),
        }

    def record_environment_requirement(self, payload: dict[str, Any]) -> AdminConfigResult:
        item_payload = _environment_requirement_payload(payload)
        candidate = EnvironmentRequirementConfig.from_dict(
            str(item_payload.get("id") or payload.get("id") or ""),
            item_payload,
        )
        if not candidate.id or not candidate.name:
            return AdminConfigResult(
                False, {"error": "environment_requirement_id_required"}, 400
            )
        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._admin_resource_items(data, "environment_requirement")
            previous = (
                items.get(candidate.id, {})
                if isinstance(items.get(candidate.id), dict)
                else {}
            )
            merged = {**previous, **item_payload}
            normalized = EnvironmentRequirementConfig.from_dict(candidate.id, merged)
            items[normalized.id] = normalized.to_dict()
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "kind": "environment_requirement",
                    "id": normalized.id,
                    "name": normalized.name,
                    "created": not previous,
                    "environment_requirement": self._admin_resource_view(
                        "environment_requirement",
                        normalized.id,
                        items[normalized.id],
                    ),
                },
            )

    def record_mcp_server(self, payload: dict[str, Any]) -> AdminConfigResult:
        item_payload = _mcp_server_payload(payload)
        name = str(item_payload.get("name") or payload.get("name") or "").strip()
        if not name:
            return AdminConfigResult(False, {"error": "mcp_server_name_required"}, 400)

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._admin_resource_items(data, "mcp")
            previous = items.get(name, {}) if isinstance(items.get(name), dict) else {}
            merged = {**previous, **item_payload}
            merged.pop("name", None)
            normalized = MCPServerConfig.from_dict(name, merged)
            items[normalized.name] = normalized.to_dict()
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "kind": "mcp_server",
                    "name": normalized.name,
                    "created": not previous,
                    "mcp_server": self._admin_resource_view(
                        "mcp",
                        normalized.name,
                        items[normalized.name],
                    ),
                },
            )

    def delete_environment_requirement(self, payload: dict[str, Any]) -> AdminConfigResult:
        item_payload = _environment_requirement_payload(payload)
        candidate = EnvironmentRequirementConfig.from_dict(
            str(item_payload.get("id") or payload.get("id") or ""),
            item_payload,
        )
        item_id = candidate.id
        if not item_id:
            return AdminConfigResult(
                False, {"error": "environment_requirement_id_required"}, 400
            )

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._admin_resource_items(data, "environment_requirement")
            if item_id not in items:
                return AdminConfigResult(
                    False, {"error": "environment_requirement_not_found"}, 404
                )
            del items[item_id]
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "kind": "environment_requirement",
                    "name": item_id,
                },
            )

    def delete_mcp_server(self, payload: dict[str, Any]) -> AdminConfigResult:
        item_payload = _mcp_server_payload(payload)
        name = str(item_payload.get("name") or payload.get("name") or "").strip()
        if not name:
            return AdminConfigResult(False, {"error": "mcp_server_name_required"}, 400)

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._admin_resource_items(data, "mcp")
            if name not in items:
                return AdminConfigResult(False, {"error": "mcp_server_not_found"}, 404)
            del items[name]
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True, {"ok": True, "kind": "mcp_server", "name": name}
            )

    def enable_environment_requirement(self, payload: dict[str, Any]) -> AdminConfigResult:
        item_payload = _environment_requirement_payload(payload)
        candidate = EnvironmentRequirementConfig.from_dict(
            str(item_payload.get("id") or payload.get("id") or ""),
            item_payload,
        )
        item_id = candidate.id
        if not item_id:
            return AdminConfigResult(
                False, {"error": "environment_requirement_id_required"}, 400
            )
        enabled = _bool_field(item_payload, "enabled", payload.get("enabled", True))

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._admin_resource_items(data, "environment_requirement")
            item = items.get(item_id)
            if not isinstance(item, dict):
                return AdminConfigResult(
                    False, {"error": "environment_requirement_not_found"}, 404
                )
            item["enabled"] = enabled
            normalized = EnvironmentRequirementConfig.from_dict(item_id, item)
            items[item_id] = normalized.to_dict()
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "kind": "environment_requirement",
                    "name": item_id,
                    "environment_requirement": self._admin_resource_view(
                        "environment_requirement", item_id, items[item_id]
                    ),
                },
            )

    def enable_mcp_server(self, payload: dict[str, Any]) -> AdminConfigResult:
        item_payload = _mcp_server_payload(payload)
        name = str(item_payload.get("name") or payload.get("name") or "").strip()
        if not name:
            return AdminConfigResult(False, {"error": "mcp_server_name_required"}, 400)
        enabled = _bool_field(item_payload, "enabled", payload.get("enabled", True))

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._admin_resource_items(data, "mcp")
            item = items.get(name)
            if not isinstance(item, dict):
                return AdminConfigResult(False, {"error": "mcp_server_not_found"}, 404)
            item["enabled"] = enabled
            normalized = MCPServerConfig.from_dict(name, item)
            items[name] = normalized.to_dict()
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "kind": "mcp_server",
                    "name": name,
                    "mcp_server": self._admin_resource_view("mcp", name, items[name]),
                },
            )

    def list_providers(self) -> dict[str, Any]:
        data = self._load_data()
        raw_items = (((data.get("providers") or {}).get("items")) or {})
        providers = []
        if isinstance(raw_items, dict):
            for provider_id in sorted(raw_items):
                item = raw_items.get(provider_id)
                if not isinstance(item, dict):
                    continue
                providers.append(self._provider_view(str(provider_id), item))
        return {"providers": providers}

    def record_provider(self, payload: dict[str, Any]) -> AdminConfigResult:
        provider_id = str(payload.get("provider_id") or payload.get("id") or "").strip()
        if not provider_id:
            return AdminConfigResult(False, {"error": "provider_id_required"}, 400)
        if payload.get("api_key") and payload.get("api_key_env"):
            return AdminConfigResult(False, {"error": "api_key_conflict"}, 400)
        if payload.get("base_url") and payload.get("base_url_env"):
            return AdminConfigResult(False, {"error": "base_url_conflict"}, 400)

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._provider_items(data)
            previous = items.get(provider_id, {}) if isinstance(items.get(provider_id), dict) else {}
            provider_type = str(payload.get("type") or previous.get("type") or "openai_chat")
            base_url = _field_or_env(payload, "base_url", "base_url_env")
            if base_url is None:
                base_url = previous.get("base_url")
            api_key = _field_or_env(payload, "api_key", "api_key_env")
            if api_key is None:
                api_key = previous.get("api_key", "")
            provider_data = {
                "type": provider_type,
                "compat": payload.get("compat")
                or previous.get("compat")
                or infer_provider_compat(str(base_url or "")),
                "enabled": _bool_field(payload, "enabled", previous.get("enabled", True)),
                "api_key": str(api_key or ""),
                "base_url": base_url,
                "headers": _dict_field(payload, "headers", previous),
                "timeout_sec": int(payload.get("timeout_sec") or previous.get("timeout_sec") or 120),
                "max_retries": int(payload.get("max_retries") or previous.get("max_retries") or 3),
                "api_features": ProviderApiFeatures.from_dict(
                    _dict_field(payload, "api_features", previous),
                    provider_type=provider_type,
                ).to_dict(),
                "stream_recovery": StreamRecoveryConfig.from_dict(
                    _dict_field(payload, "stream_recovery", previous)
                ).to_dict(),
                "extra": _dict_field(payload, "extra", previous),
            }
            provider = ProviderConfig.from_dict(provider_id, provider_data)
            normalized_provider = provider.to_dict()
            previous_models = previous.get("models")
            if isinstance(previous_models, list):
                normalized_provider["models"] = _normalize_provider_models(previous_models)
            items[provider_id] = normalized_provider
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "provider": self._provider_view(provider_id, items[provider_id]),
                    "created": not previous,
                },
            )

    def test_provider(self, payload: dict[str, Any]) -> AdminConfigResult:
        provider_id = str(payload.get("provider_id") or payload.get("id") or "").strip()
        model = str(payload.get("model") or "").strip()
        prompt = str(payload.get("prompt") or "ping")
        if not provider_id:
            return AdminConfigResult(False, {"error": "provider_id_required"}, 400)
        if not model:
            return AdminConfigResult(False, {"error": "model_required"}, 400)
        try:
            provider = self._expanded_provider(provider_id)
            if provider is None:
                return AdminConfigResult(False, {"error": "provider_not_found"}, 404)
            if self.provider_test_handler is not None:
                result = self.provider_test_handler(provider, model, prompt)
            else:
                response = ProviderManager().create(
                    provider, allow_disabled=True
                ).test(model=model, prompt=prompt)
                preview = response.content.strip().replace("\n", " ")
                if len(preview) > 200:
                    preview = preview[:197] + "..."
                result = {
                    "ok": True,
                    "provider_id": provider.id,
                    "model": model,
                    "tokens": response.prompt_tokens + response.completion_tokens,
                    "response": preview,
                }
        except Exception as exc:
            return AdminConfigResult(False, {"error": "provider_test_failed", "message": str(exc)}, 500)
        return AdminConfigResult(True, result)

    def delete_provider(self, payload: dict[str, Any]) -> AdminConfigResult:
        provider_id = str(payload.get("provider_id") or payload.get("id") or "").strip()
        if not provider_id:
            return AdminConfigResult(False, {"error": "provider_id_required"}, 400)

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._provider_items(data)
            if provider_id not in items:
                return AdminConfigResult(False, {"error": "provider_not_found"}, 404)
            blockers = self._provider_profile_blockers(data, provider_id)
            blockers.extend(self._provider_agent_blockers(data, provider_id))
            if blockers:
                return AdminConfigResult(
                    False,
                    {
                        "error": "provider_in_use",
                        "provider_id": provider_id,
                        "blockers": blockers,
                    },
                    409,
                )
            del items[provider_id]
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(True, {"ok": True, "provider_id": provider_id})

    def copy_provider(self, payload: dict[str, Any]) -> AdminConfigResult:
        provider_id = str(payload.get("provider_id") or payload.get("id") or "").strip()
        target_id = str(payload.get("target_id") or "").strip()
        if not provider_id:
            return AdminConfigResult(False, {"error": "provider_id_required"}, 400)

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._provider_items(data)
            source = items.get(provider_id)
            if not isinstance(source, dict):
                return AdminConfigResult(False, {"error": "provider_not_found"}, 404)
            if target_id and target_id in items:
                return AdminConfigResult(False, {"error": "provider_exists"}, 409)
            new_id = target_id or self._unique_provider_copy_id(items, provider_id)
            copied = deepcopy(source)
            copied["enabled"] = True
            provider = ProviderConfig.from_dict(new_id, copied)
            items[new_id] = provider.to_dict()
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "provider": self._provider_view(new_id, items[new_id]),
                    "copied_from": provider_id,
                },
            )

    def enable_provider(self, payload: dict[str, Any]) -> AdminConfigResult:
        provider_id = str(payload.get("provider_id") or payload.get("id") or "").strip()
        if not provider_id:
            return AdminConfigResult(False, {"error": "provider_id_required"}, 400)
        enabled = _bool_field(payload, "enabled", True)

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._provider_items(data)
            item = items.get(provider_id)
            if not isinstance(item, dict):
                return AdminConfigResult(False, {"error": "provider_not_found"}, 404)
            item["enabled"] = enabled
            provider = ProviderConfig.from_dict(provider_id, item)
            items[provider_id] = provider.to_dict()
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "provider": self._provider_view(provider_id, items[provider_id]),
                },
            )

    def list_provider_models(self, payload: dict[str, Any]) -> AdminConfigResult:
        provider_id = str(payload.get("provider_id") or payload.get("id") or "").strip()
        if not provider_id:
            return AdminConfigResult(False, {"error": "provider_id_required"}, 400)
        try:
            provider = self._expanded_provider(provider_id)
            if provider is None:
                return AdminConfigResult(False, {"error": "provider_not_found"}, 404)
            if self.provider_models_handler is not None:
                result = self.provider_models_handler(provider)
            else:
                result = ProviderManager().list_models(provider)
        except Exception as exc:
            return AdminConfigResult(
                False, {"error": "provider_models_failed", "message": str(exc)}, 500
            )
        models = result.get("models") if isinstance(result, dict) else None
        if isinstance(models, list):
            result["models"] = [
                self.model_capability_catalog.enrich_model(provider, item)
                if isinstance(item, dict)
                else item
                for item in models
            ]
            models = result["models"]
            with self._lock:
                previous_data = self._load_data()
                data = deepcopy(previous_data)
                provider_item = self._provider_items(data).get(provider_id)
                if isinstance(provider_item, dict):
                    provider_item["models"] = _normalize_provider_models(models)
                    reload_error = self._commit_config(data, previous_data)
                    if reload_error:
                        return reload_error
        return AdminConfigResult(True, result)

    def list_model_profiles(self) -> dict[str, Any]:
        return self._model_profile_state(self._load_data())

    def _model_profile_state(self, data: dict[str, Any]) -> dict[str, Any]:
        models = data.get("models", {})
        models = models if isinstance(models, dict) else {}
        raw_profiles = models.get("profiles", {})
        profiles = []
        if isinstance(raw_profiles, dict):
            for profile_id in sorted(raw_profiles):
                item = raw_profiles.get(profile_id)
                if not isinstance(item, dict):
                    continue
                profiles.append(self._profile_view(str(profile_id), item, data=data))
        return {
            "model_profiles": profiles,
            "active_main": models.get("active_main"),
            "active_sub": models.get("active_sub"),
        }

    def record_model_profile(self, payload: dict[str, Any]) -> AdminConfigResult:
        profile_id = str(payload.get("profile_id") or payload.get("id") or "").strip()
        if not profile_id:
            return AdminConfigResult(False, {"error": "profile_id_required"}, 400)
        allowed_fields = set(MODEL_PROFILE_ADMIN_INPUT_FIELDS)
        unknown_fields = sorted(str(field) for field in payload if field not in allowed_fields)
        if unknown_fields:
            return AdminConfigResult(
                False,
                {
                    "error": "unknown_model_profile_field",
                    "fields": unknown_fields,
                    "message": "Unknown model profile field: "
                    + ", ".join(unknown_fields),
                },
                400,
            )

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            profiles = self._model_profiles(data)
            previous = profiles.get(profile_id, {}) if isinstance(profiles.get(profile_id), dict) else {}
            model_name = str(payload.get("model") or previous.get("model") or "").strip()
            if not model_name:
                return AdminConfigResult(False, {"error": "model_required"}, 400)
            provider_id = str(payload.get("provider", previous.get("provider") or "") or "").strip()
            if not provider_id:
                return AdminConfigResult(False, {"error": "provider_required"}, 400)
            provider_config = None
            provider_item = self._provider_items(data).get(provider_id)
            if isinstance(provider_item, dict):
                provider_config = ProviderConfig.from_dict(provider_id, provider_item)
            else:
                return AdminConfigResult(
                    False,
                    {"error": "provider_not_found", "provider_id": provider_id},
                    404,
                )
            catalog_capability = self.model_capability_catalog.lookup(
                provider_config or provider_id,
                model_name,
            )
            capability_defaults = ProviderManager.known_model_capabilities(
                provider_config or provider_id,
                model_name,
            )
            if catalog_capability is not None:
                if catalog_capability.max_output_tokens:
                    capability_defaults["max_tokens"] = catalog_capability.max_output_tokens
                if catalog_capability.max_context_tokens:
                    capability_defaults["max_context_tokens"] = (
                        catalog_capability.max_context_tokens
                    )
            explicit_max_tokens = payload.get("max_tokens") is not None
            explicit_max_context_tokens = payload.get("max_context_tokens") is not None
            user_configured = bool(
                payload.get("capability_user_configured")
                if "capability_user_configured" in payload
                else (
                    explicit_max_tokens
                    or explicit_max_context_tokens
                    or previous.get("capability_user_configured", False)
                )
            )
            recommended_max_tokens = capability_defaults.get("max_tokens")
            recommended_max_context_tokens = capability_defaults.get(
                "max_context_tokens"
            )
            max_tokens_value = (
                recommended_max_tokens
                if not user_configured and recommended_max_tokens
                else (
                    payload.get("max_tokens")
                    if explicit_max_tokens
                    else previous.get("max_tokens")
                )
            )
            max_context_tokens_value = (
                recommended_max_context_tokens
                if not user_configured and recommended_max_context_tokens
                else (
                    payload.get("max_context_tokens")
                    if explicit_max_context_tokens
                    else previous.get("max_context_tokens")
                )
            )
            try:
                max_tokens_int = int(max_tokens_value or 0)
                max_context_tokens_int = int(max_context_tokens_value or 0)
            except (TypeError, ValueError) as exc:
                return AdminConfigResult(
                    False,
                    {"error": "invalid_model_profile", "message": str(exc)},
                    400,
                )
            if max_tokens_int < 1 or max_context_tokens_int < 1:
                return AdminConfigResult(
                    False,
                    {
                        "error": "model_capability_required",
                        "message": "max_tokens and max_context_tokens are required. Refresh model capabilities or enter explicit values.",
                        "provider_id": provider_id,
                        "model": model_name,
                    },
                    400,
                )
            raw_temperature = payload.get(
                "temperature",
                previous.get("temperature", 0.0),
            )
            if raw_temperature is None:
                raw_temperature = 0.0
            profile_data = {
                "model": model_name,
                "provider": provider_id,
                "max_tokens": max_tokens_int,
                "temperature": float(raw_temperature),
                "max_context_tokens": max_context_tokens_int,
                "preserve_reasoning_content": bool(payload.get("preserve_reasoning_content", previous.get("preserve_reasoning_content", True))),
                "backfill_reasoning_content_for_tool_calls": bool(payload.get("backfill_reasoning_content_for_tool_calls", previous.get("backfill_reasoning_content_for_tool_calls", False))),
                "reasoning_effort": payload.get("reasoning_effort", previous.get("reasoning_effort")),
                "thinking_enabled": payload.get("thinking_enabled", previous.get("thinking_enabled")),
                "reasoning_replay_mode": payload.get("reasoning_replay_mode", previous.get("reasoning_replay_mode")),
                "reasoning_replay_placeholder": payload.get("reasoning_replay_placeholder", previous.get("reasoning_replay_placeholder")),
            }
            profile = ModelProfileConfig.from_dict(profile_id, profile_data)
            profiles[profile_id] = {
                **profile.to_dict(),
                "capability_user_configured": user_configured,
                "capability_source": (
                    capability_source_label(catalog_capability)
                    if catalog_capability
                    else previous.get("capability_source")
                ),
            }
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "model_profile": self._profile_view(
                        profile_id, profiles[profile_id], data=data
                    ),
                    "created": not previous,
                },
            )

    def delete_model_profile(self, payload: dict[str, Any]) -> AdminConfigResult:
        profile_id = str(payload.get("profile_id") or payload.get("id") or "").strip()
        if not profile_id:
            return AdminConfigResult(False, {"error": "profile_id_required"}, 400)
        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            models = data.setdefault("models", {})
            if not isinstance(models, dict):
                return AdminConfigResult(
                    False,
                    {"error": "profile_not_found", "profile_id": profile_id},
                    404,
                )
            profiles = models.setdefault("profiles", {})
            if not isinstance(profiles, dict) or profile_id not in profiles:
                return AdminConfigResult(
                    False,
                    {"error": "profile_not_found", "profile_id": profile_id},
                    404,
                )

            del profiles[profile_id]
            next_profile_id = next(iter(sorted(profiles)), None)
            if models.get("active_main") == profile_id:
                if next_profile_id:
                    models["active_main"] = next_profile_id
                else:
                    models.pop("active_main", None)
            if models.get("active_sub") == profile_id:
                active_main = models.get("active_main")
                if isinstance(active_main, str) and active_main in profiles:
                    models["active_sub"] = active_main
                elif next_profile_id:
                    models["active_sub"] = next_profile_id
                else:
                    models.pop("active_sub", None)

            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "deleted": True,
                    "profile_id": profile_id,
                    **self.list_model_profiles(),
                },
            )

    def activate_model_profile(self, payload: dict[str, Any]) -> AdminConfigResult:
        profile_id = str(payload.get("profile_id") or payload.get("id") or "").strip()
        target = str(payload.get("target") or "main").strip().lower()
        if not profile_id:
            return AdminConfigResult(False, {"error": "profile_id_required"}, 400)
        if target not in {"main", "sub", "both"}:
            return AdminConfigResult(False, {"error": "invalid_target"}, 400)
        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            models = data.setdefault("models", {})
            if not isinstance(models, dict):
                models = {}
                data["models"] = models
            profiles = models.setdefault("profiles", {})
            if not isinstance(profiles, dict) or profile_id not in profiles:
                return AdminConfigResult(False, {"error": "profile_not_found"}, 404)
            if target in {"main", "both"}:
                models["active_main"] = profile_id
            if target in {"sub", "both"}:
                models["active_sub"] = profile_id
            profile_data = profiles.get(profile_id)
            if isinstance(profile_data, dict) and profile_data.get("provider"):
                provider_id = str(profile_data.get("provider"))
                provider_item = self._provider_items(data).get(provider_id)
                if isinstance(provider_item, dict):
                    provider = ProviderConfig.from_dict(provider_id, provider_item)
                    if not provider.enabled:
                        return AdminConfigResult(
                            False,
                            {
                                "error": "provider_disabled",
                                "provider_id": provider_id,
                            },
                            409,
                        )
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {"ok": True, "active_main": models.get("active_main"), "active_sub": models.get("active_sub")},
            )

    def _reload(self) -> AdminConfigResult | None:
        if self.reload_handler is None:
            return None
        try:
            self.reload_handler()
        except Exception as exc:
            logger.exception(
                "Admin config reload failed for %s: %s: %s",
                self.config_path,
                type(exc).__name__,
                exc,
            )
            return AdminConfigResult(False, {"error": "config_reload_failed", "message": str(exc)}, 500)
        return None

    def _commit_config(
        self, data: dict[str, Any], previous_data: dict[str, Any]
    ) -> AdminConfigResult | None:
        save_yaml_config(self.config_path, data)
        reload_error = self._reload()
        if reload_error is None:
            return None
        save_yaml_config(self.config_path, previous_data)
        self._reload()
        return reload_error

    def _load_data(self) -> dict[str, Any]:
        try:
            data = load_yaml_config(self.config_path)
        except FileNotFoundError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _expanded_provider(self, provider_id: str) -> ProviderConfig | None:
        data = self._load_data()
        expanded = ConfigLoader()._expand_env_refs(data)
        raw = (((expanded.get("providers") or {}).get("items")) or {}).get(provider_id)
        if not isinstance(raw, dict):
            return None
        return ProviderConfig.from_dict(provider_id, raw)

    def _provider_config_for_profile(
        self, data: dict[str, Any], profile: dict[str, Any]
    ) -> ProviderConfig | None:
        provider_id = str(profile.get("provider") or "").strip()
        if not provider_id:
            return None
        raw = (((data.get("providers") or {}).get("items")) or {}).get(provider_id)
        if not isinstance(raw, dict):
            return None
        return ProviderConfig.from_dict(provider_id, raw)

    def _provider_items(self, data: dict[str, Any]) -> dict[str, Any]:
        providers = data.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            data["providers"] = providers
        items = providers.setdefault("items", {})
        if not isinstance(items, dict):
            items = {}
            providers["items"] = items
        return items

    def list_provider_model_catalog(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        data = data or self._load_data()
        models: list[dict[str, Any]] = []
        for provider_id, item in sorted(self._provider_items(data).items()):
            if not isinstance(item, dict):
                continue
            if item.get("enabled") is False:
                continue
            for model in _normalize_provider_models(item.get("models", [])):
                model_id = str(model.get("id") or model.get("model") or "").strip()
                if not model_id:
                    continue
                models.append(
                    {
                        **model,
                        "id": model_id,
                        "model_id": model_id,
                        "provider_id": str(provider_id),
                    }
                )
        return {"models": models}

    def _agent_profile_views(
        self, data: dict[str, Any], active_mode: str | None
    ) -> dict[str, Any]:
        registry, _profiles = ensure_default_environment_agent_registry(
            data.get("agent_registry", {})
            if isinstance(data.get("agent_registry"), dict)
            else {},
            data.get("runtime_profiles", {})
            if isinstance(data.get("runtime_profiles"), dict)
            else {},
        )
        raw_agents = registry.get("agents", {})
        agents = deepcopy(raw_agents) if isinstance(raw_agents, dict) else {}
        mode_names = self._mode_names(data)
        for agent_id in mode_names:
            item = agents.get(agent_id)
            if not isinstance(item, dict):
                agents[agent_id] = {"name": agent_id}
        if active_mode and active_mode not in agents:
            agents[active_mode] = {"name": active_mode}
        return agents

    def _active_agent_model(
        self, agents: dict[str, Any], active_mode: str | None
    ) -> dict[str, Any]:
        if not active_mode:
            return {}
        agent = agents.get(active_mode)
        if not isinstance(agent, dict):
            return {}
        model = agent.get("model")
        if not isinstance(model, dict):
            return {}
        view = dict(model)
        parameters = view.get("parameters")
        view["parameters"] = parameters if isinstance(parameters, dict) else {}
        return view

    def _mode_names(self, data: dict[str, Any]) -> list[str]:
        raw_modes = data.get("modes", {})
        profiles = raw_modes.get("profiles", {}) if isinstance(raw_modes, dict) else {}
        names = set(BUILTIN_MODES.keys())
        if isinstance(profiles, dict):
            names.update(str(name) for name in profiles.keys())
        return sorted(names)

    def _provider_profile_blockers(
        self, data: dict[str, Any], provider_id: str
    ) -> list[dict[str, Any]]:
        models = data.get("models", {})
        if not isinstance(models, dict):
            return []
        profiles = models.get("profiles", {})
        if not isinstance(profiles, dict):
            return []
        blockers: list[dict[str, Any]] = []
        active_main = models.get("active_main")
        active_sub = models.get("active_sub")
        for profile_id, profile_data in sorted(profiles.items()):
            if not isinstance(profile_data, dict):
                continue
            if str(profile_data.get("provider") or "") != provider_id:
                continue
            blockers.append(
                {
                    "profile_id": str(profile_id),
                    "active_main": profile_id == active_main,
                    "active_sub": profile_id == active_sub,
                }
            )
        return blockers

    def _provider_agent_blockers(
        self, data: dict[str, Any], provider_id: str
    ) -> list[dict[str, Any]]:
        agents = self._agent_profile_views(data, None)
        blockers: list[dict[str, Any]] = []
        for agent_id, agent_data in sorted(agents.items()):
            if not isinstance(agent_data, dict):
                continue
            model = agent_data.get("model")
            if not isinstance(model, dict):
                continue
            if str(model.get("provider") or model.get("provider_id") or "") != provider_id:
                continue
            blockers.append(
                {
                    "agent_id": str(agent_id),
                    "model": str(model.get("model") or model.get("model_id") or ""),
                }
            )
        return blockers

    def _unique_provider_copy_id(
        self, items: dict[str, Any], provider_id: str
    ) -> str:
        base = f"{provider_id}-copy"
        if base not in items:
            return base
        index = 2
        while f"{base}-{index}" in items:
            index += 1
        return f"{base}-{index}"

    def _models_data(self) -> dict[str, Any]:
        data = self._load_data()
        models = data.get("models", {})
        return models if isinstance(models, dict) else {}

    def _model_profiles(self, data: dict[str, Any]) -> dict[str, Any]:
        models = data.setdefault("models", {})
        if not isinstance(models, dict):
            models = {}
            data["models"] = models
        profiles = models.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            models["profiles"] = profiles
        return profiles

    def _admin_resource_items(self, data: dict[str, Any], kind: str) -> dict[str, Any]:
        if kind == "environment_requirement":
            environment = data.setdefault("environment", {})
            if not isinstance(environment, dict):
                environment = {}
                data["environment"] = environment
            items = environment.setdefault("requirements", {})
            if not isinstance(items, dict):
                items = {}
                environment["requirements"] = items
            return items

        mcp = data.setdefault("mcp", {})
        if not isinstance(mcp, dict):
            mcp = {}
            data["mcp"] = mcp
        items = mcp.setdefault("servers", {})
        if not isinstance(items, dict):
            items = {}
            mcp["servers"] = items
        return items

    def _admin_resource_views(self, data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
        items = self._admin_resource_items(data, kind)
        views: list[dict[str, Any]] = []
        for name in sorted(items):
            item = items.get(name)
            if not isinstance(item, dict):
                continue
            views.append(self._admin_resource_view(kind, str(name), item))
        return views

    def _normalize_admin_resource_item(
        self, kind: str, name: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        if kind == "environment_requirement":
            return EnvironmentRequirementConfig.from_dict(name, item).to_dict()
        return MCPServerConfig.from_dict(name, item).to_dict()

    def _admin_resource_view(
        self, kind: str, name: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        view = self._normalize_admin_resource_item(kind, name, item)
        view["entry_type"] = kind
        view.setdefault("name", name)
        if kind == "mcp":
            view["kind"] = "mcp_server"
            view["id"] = f"mcp:{name}"
        else:
            view.setdefault("id", name)
        return view

    def _admin_resource_dashboard_item(
        self, kind: str, view: dict[str, Any]
    ) -> dict[str, Any]:
        name = str(view.get("name") or view.get("id") or "")
        docs = list(view.get("docs") or []) if isinstance(view.get("docs"), list) else []
        repo_url = str(view.get("repo_url") or "")
        if not repo_url and _looks_like_url(view.get("source")):
            repo_url = str(view.get("source"))
        placement = str(view.get("placement") or "")
        scope = str(view.get("scope") or "")
        if kind == "mcp":
            placement = placement or "server"
            scope = placement
        else:
            placement = placement or "peer"
            scope = scope or placement
        status = "unchecked" if _bool_field(view, "enabled", True) else "stopped"
        return {
            "id": str(view.get("id") or f"{kind}:{name}"),
            "kind": str(view.get("kind") or kind),
            "entry_type": kind,
            "name": name,
            "alias": str(view.get("alias") or view.get("command") or view.get("path_hint") or name),
            "source": str(view.get("source") or ""),
            "repo_url": repo_url,
            "docs": docs,
            "evidence": (
                list(view.get("evidence") or [])
                if isinstance(view.get("evidence"), list)
                else []
            ),
            "placement": placement,
            "scope": scope,
            "status": status,
            "status_detail": "清单已停用" if status == "stopped" else "等待环境检查",
            "check": str(view.get("check") or ""),
            "install": str(view.get("install") or ""),
            "command": str(view.get("command") or view.get("path_hint") or ""),
            "environment_requirement_refs": (
                [str(item) for item in view.get("environment_requirement_refs") or []]
                if isinstance(view.get("environment_requirement_refs"), list)
                else []
            ),
            "credentials": (
                [str(item) for item in view.get("credentials") or []]
                if isinstance(view.get("credentials"), list)
                else []
            ),
            "risk_level": str(view.get("risk_level") or ""),
            "enabled": _bool_field(view, "enabled", True),
            "last_action": str(view.get("last_action") or ""),
            "last_updated": str(view.get("last_updated") or ""),
            "component_id": str(view.get("component_id") or ""),
            "package_ids": (
                [str(item) for item in view.get("package_ids") or []]
                if isinstance(view.get("package_ids"), list)
                else []
            ),
            "managed_by": str(view.get("managed_by") or ""),
        }

    def _chat_command_catalog(self) -> list[dict[str, Any]]:
        registry = create_builtin_action_registry()
        commands: list[dict[str, Any]] = []
        for action in registry.iter_actions(VSCODE_CHAT_PROFILE):
            slash_triggers = action.matching_triggers(
                VSCODE_CHAT_PROFILE, kind=TriggerKind.SLASH
            )
            for trigger in slash_triggers:
                commands.append(
                    {
                        "id": action.action_id,
                        "name": str(trigger.value).lstrip("/") or action.action_id,
                        "display_name": str(trigger.value),
                        "feature_id": action.feature_id,
                        "source_type": "action_registry",
                        "registration_path": "reuleauxcoder.app.commands.registry.ActionRegistry",
                        "description": action.description,
                        "trigger_kind": "slash",
                        "trigger": str(trigger.value),
                        "ui_targets": sorted(action.ui_targets),
                        "required_capabilities": self._capability_values(
                            action.required_capabilities
                        ),
                        "interactive": bool(action.interactive),
                        "supports_args": bool(trigger.supports_args),
                        "args_hint": str(trigger.args_hint or ""),
                        "selection_behavior": trigger.selection_behavior.value,
                        "available_during_run": bool(trigger.available_during_run),
                        "visibility": trigger.visibility.value,
                    }
                )
        return sorted(commands, key=lambda item: str(item.get("trigger") or ""))

    def _mention_provider_catalog(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        packages = self._capability_package_catalog(data)
        agent_tools = self._agent_tool_catalog(data)
        return [
            {
                "id": "workspace_files",
                "name": "workspace_files",
                "display_name": "Workspace files",
                "source_type": "workspace",
                "registration_path": "dogcode.webview-ui.chat.PromptInput",
                "description": "Search workspace files and insert them as chat context references.",
                "trigger": "@",
                "enabled": True,
                "insert_format": "@path",
                "item_count": None,
            },
            {
                "id": "capability_packages",
                "name": "capability_packages",
                "display_name": "Capability packages",
                "source_type": "config",
                "registration_path": "capability_packages",
                "description": "Mention installed capability packages as context for the agent.",
                "trigger": "@",
                "enabled": True,
                "insert_format": "@capability:<id>",
                "item_count": len(packages),
            },
            {
                "id": "agent_tools",
                "name": "agent_tools",
                "display_name": "Agent tools",
                "source_type": "behavior_catalog",
                "registration_path": "agent_tools",
                "description": "Mention available agent tools without granting direct execution.",
                "trigger": "@",
                "enabled": True,
                "insert_format": "@tool:<name>",
                "item_count": len(agent_tools),
            },
        ]

    def _user_action_catalog(
        self,
        chat_commands: list[dict[str, Any]],
        mention_providers: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for command in chat_commands:
            actions.append(
                {
                    "id": f"slash:{command.get('id')}",
                    "name": command.get("name"),
                    "display_name": command.get("display_name"),
                    "feature_id": command.get("feature_id"),
                    "source_type": command.get("source_type"),
                    "registration_path": command.get("registration_path"),
                    "description": command.get("description"),
                    "trigger_kind": "slash",
                    "trigger": command.get("trigger"),
                    "ui_targets": command.get("ui_targets", ["chatview"]),
                    "interactive": True,
                    "execution_semantics": "registered slash command",
                    "reference_only": False,
                }
            )
        for provider in mention_providers:
            actions.append(
                {
                    "id": f"mention:{provider.get('id')}",
                    "name": provider.get("name"),
                    "display_name": provider.get("display_name"),
                    "feature_id": "reference_context",
                    "source_type": provider.get("source_type"),
                    "registration_path": provider.get("registration_path"),
                    "description": provider.get("description"),
                    "trigger_kind": "mention",
                    "trigger": provider.get("trigger", "@"),
                    "ui_targets": ["chatview"],
                    "interactive": True,
                    "execution_semantics": "reference_only mention; no tool permission grant",
                    "reference_only": True,
                }
            )
        return actions

    def _ui_action_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "id": str(item["id"]),
                "name": str(item["id"]),
                "feature_id": str(item["feature_id"]),
                "source_type": "settings_ui",
                "registration_path": "dogcode.webview-ui.settings.ToolchainsTab",
                "description": str(item["description"]),
                "ui_targets": ["webview"],
                "required_capabilities": ["buttons", "tabs"],
                "interactive": True,
                "triggers": [
                    {
                        "kind": str(trigger.get("kind") or "button"),
                        "value": str(trigger.get("value") or ""),
                        "ui_targets": ["webview"],
                        "required_capabilities": ["buttons"],
                    }
                    for trigger in (
                        item.get("triggers", [])
                        if isinstance(item.get("triggers"), list)
                        else []
                    )
                    if isinstance(trigger, dict)
                ],
            }
            for item in SETTINGS_UI_ACTIONS
        ]

    def _trigger_view(
        self,
        trigger: Any,
        *,
        fallback_ui_targets: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        targets = trigger.ui_targets or (fallback_ui_targets or frozenset())
        return {
            "kind": getattr(trigger.kind, "value", str(trigger.kind)),
            "value": str(trigger.value),
            "ui_targets": sorted(targets),
            "required_capabilities": self._capability_values(
                trigger.required_capabilities
            ),
        }

    def _capability_values(self, capabilities: Any) -> list[str]:
        return sorted(
            getattr(capability, "value", str(capability)) for capability in capabilities
        )

    def _agent_tool_permission_context(self, data: dict[str, Any]) -> dict[str, Any]:
        agent_registry, runtime_profiles, run_limits = self._agent_settings_from_data(data)
        packages = {
            str(package_id): CapabilityPackageConfig.from_dict(
                str(package_id), package_data
            )
            for package_id, package_data in ensure_default_capability_packages(
                data.get("capability_packages", {})
                if isinstance(data.get("capability_packages"), dict)
                else {}
            ).items()
            if isinstance(package_data, dict)
        }
        components = {
            str(component_id): CapabilityComponentConfig.from_dict(
                str(component_id), component_data
            )
            for component_id, component_data in ensure_default_capability_components(
                data.get("capability_components", {})
                if isinstance(data.get("capability_components"), dict)
                else {}
            ).items()
            if isinstance(component_data, dict)
        }
        snapshot = build_agent_run_snapshot(
            agent_registry=agent_registry,
            runtime_profiles=runtime_profiles,
            run_limits=run_limits,
            capability_packages=packages,
            capability_components=components,
        )
        agent_id = ""
        for candidate_id, agent in agent_registry.agents.items():
            if agent.chat_entrypoint:
                agent_id = candidate_id
                break
        if not agent_id and "main_chat" in agent_registry.agents:
            agent_id = "main_chat"
        if not agent_id and agent_registry.agents:
            agent_id = next(iter(agent_registry.agents))
        agent = agent_registry.agents.get(agent_id)
        raw_agent = (
            snapshot.get("agents", {}).get(agent_id, {})
            if isinstance(snapshot.get("agents"), dict)
            else {}
        )
        effective = (
            raw_agent.get("effective_capabilities", {})
            if isinstance(raw_agent, dict)
            else {}
        )
        approval_settings = self._approval_settings(data)
        approval = ApprovalConfig(
            default_mode=cast(
                ApprovalAction,
                str(
                    approval_settings.get("default_mode")
                    or DEFAULTS["approval_default_mode"]
                ),
            ),
            rules=[
                ApprovalRuleConfig(
                    tool_name=rule.get("tool_name"),
                    tool_source=rule.get("tool_source"),
                    mcp_server=rule.get("mcp_server"),
                    effect_class=rule.get("effect_class"),
                    profile=rule.get("profile"),
                    action=rule.get("action", "require_approval"),
                )
                for rule in approval_settings.get("rules", [])
                if isinstance(rule, dict)
            ],
        )
        profile_id = str(getattr(agent, "runtime_profile", "") or "")
        profile = runtime_profiles.profiles.get(profile_id)
        return {
            "agent_id": agent_id,
            "agent": agent,
            "subject": PermissionSubject(
                agent_id=agent_id,
                role=str(getattr(agent, "role", "") or ""),
                visibility=str(getattr(agent, "visibility", "user") or "user"),
                trigger_source="chat",
                interactive=True,
                runtime_profile_id=profile_id,
            ),
            "effective_capabilities": effective if isinstance(effective, dict) else {},
            "approval": approval,
            "runtime_profile": profile.to_dict() if profile is not None else {},
        }

    def _permission_view_for_agent_tool(
        self,
        permission_context: dict[str, Any],
        target: PermissionTarget,
    ) -> dict[str, Any]:
        decision = PermissionGateway().evaluate(
            PermissionRequest(
                subject=permission_context["subject"],
                target=target,
                tool_call=ToolCall(
                    id="behavior-catalog-preview",
                    name=target.name,
                    arguments={},
                ),
                effective_capabilities=permission_context["effective_capabilities"],
                approval=permission_context["approval"],
                runtime_profile=permission_context["runtime_profile"],
                agent_config=permission_context["agent"],
                enforce_effective_capabilities=True,
                metadata={"catalog_agent_id": permission_context.get("agent_id", "")},
            )
        )
        return decision.to_dict()

    def _agent_tool_catalog(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        packages = self._capability_package_catalog(data)
        component_package_ids = self._component_package_ids(packages)
        raw_components = data.get("capability_components", {})
        components = ensure_default_capability_components(
            raw_components if isinstance(raw_components, dict) else {}
        )
        mode_refs = self._mode_refs_by_tool(data)
        permission_context = self._agent_tool_permission_context(data)

        for tool in build_tools():
            tool_name = str(getattr(tool, "name", "") or "")
            if not tool_name:
                continue
            component_id = f"builtin_tool:{tool_name}"
            component = components.get(component_id, {})
            related_package_ids = self._string_list(
                component.get("package_ids") if isinstance(component, dict) else None
            ) or component_package_ids.get(component_id, [])
            approval_status = self._approval_action_for_tool(
                data, tool_name=tool_name, source_type="builtin"
            )
            component_execution_policy = str(
                (
                    component.get("execution_policy")
                    if isinstance(component, dict)
                    else None
                )
                or ""
            ).strip()
            execution_policy = self._execution_policy_for_approval(approval_status)
            if (
                approval_status == "allow"
                and component_execution_policy
                and component_execution_policy != "inherit"
            ):
                execution_policy = component_execution_policy
            permission = self._permission_view_for_agent_tool(
                permission_context,
                PermissionTarget(
                    kind="builtin_tool",
                    name=tool_name,
                    tool_source="builtin",
                    registry_path=f"builtin:{tool_name}",
                    component_id=component_id,
                ),
            )
            items[f"builtin:{tool_name}"] = {
                "id": f"builtin:{tool_name}",
                "name": tool_name,
                "display_name": tool_name,
                "source_type": "builtin",
                "source_label": "Builtin tool",
                "description": str(
                    (
                        component.get("description")
                        if isinstance(component, dict)
                        else None
                    )
                    or getattr(tool, "description", "")
                    or ""
                ),
                "registration_path": "reuleauxcoder.extensions.tools.registry",
                "enabled": True,
                "related_package_ids": sorted(dict.fromkeys(related_package_ids)),
                "related_components": (
                    [component_id]
                    if component_id in components
                    or component_id in DEFAULT_BUILTIN_TOOL_COMPONENTS
                    else []
                ),
                "mode_refs": self._tool_mode_refs(mode_refs, tool_name),
                "approval_status": approval_status,
                "execution_policy": execution_policy,
                "permission": permission,
            }

        for kind in ("mcp",):
            for view in self._admin_resource_views(data, kind):
                name = str(view.get("name") or view.get("id") or "")
                if not name:
                    continue
                component_id = str(view.get("component_id") or f"{kind}:{name}")
                related_package_ids = self._string_list(
                    view.get("package_ids")
                ) or component_package_ids.get(component_id, [])
                approval_status = self._approval_action_for_tool(
                    data,
                    tool_name=name,
                    source_type="mcp" if kind == "mcp" else "unknown",
                    mcp_server=name if kind == "mcp" else None,
                )
                target = PermissionTarget(
                    kind="mcp_tool",
                    name=name,
                    tool_source="mcp",
                    component_id=component_id,
                    registry_path=f"{kind}:{name}",
                    mcp_server=name,
                    mcp_tool=name,
                )
                items[f"{kind}:{name}"] = {
                    "id": f"{kind}:{name}",
                    "name": name,
                    "display_name": name,
                    "source_type": kind,
                    "source_label": self._tool_source_label(kind),
                    "description": str(view.get("description") or ""),
                    "registration_path": self._admin_resource_registration_path(kind, name),
                    "enabled": _bool_field(view, "enabled", True),
                    "related_package_ids": sorted(dict.fromkeys(related_package_ids)),
                    "related_components": [component_id] if component_id else [],
                    "mode_refs": self._tool_mode_refs(mode_refs, name),
                    "approval_status": approval_status,
                    "execution_policy": self._execution_policy_for_approval(
                        approval_status
                    ),
                    "permission": self._permission_view_for_agent_tool(
                        permission_context,
                        target,
                    ),
                }

        for package_id, package in packages.items():
            components = self._string_list(package.get("components"))
            items[f"capability_package:{package_id}"] = {
                "id": f"capability_package:{package_id}",
                "name": package_id,
                "display_name": str(package.get("name") or package_id),
                "source_type": "capability_package",
                "source_label": "Capability package",
                "description": str(package.get("description") or ""),
                "registration_path": "agent.capability_refs[]",
                "enabled": _bool_field(package, "enabled", True),
                "related_package_ids": [package_id],
                "related_components": components,
                "mode_refs": [],
                "approval_status": "inherits_component_policy",
                "execution_policy": str(package.get("execution_policy") or "inherit"),
                "generated_by": str(package.get("generated_by") or ""),
            }

        return sorted(items.values(), key=lambda item: str(item.get("id") or ""))

    def _capability_package_catalog(
        self, data: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        raw_packages = data.get("capability_packages", {})
        packages = ensure_default_capability_packages(
            raw_packages if isinstance(raw_packages, dict) else {}
        )
        catalog: dict[str, dict[str, Any]] = {}
        for package_id, package_data in packages.items():
            if not isinstance(package_data, dict):
                continue
            package = CapabilityPackageConfig.from_dict(str(package_id), package_data)
            package_view = package.to_dict()
            package_view["id"] = str(package_id)
            catalog[str(package_id)] = package_view
        return catalog

    def _component_package_ids(
        self, packages: dict[str, dict[str, Any]]
    ) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for package_id, package in packages.items():
            for component_id in self._string_list(package.get("components")):
                result.setdefault(component_id, []).append(package_id)
        return {key: sorted(dict.fromkeys(value)) for key, value in result.items()}

    def _mode_refs_by_tool(self, data: dict[str, Any]) -> dict[str, list[str]]:
        profiles = deepcopy(BUILTIN_MODES)
        raw_modes = data.get("modes", {})
        mode_data = raw_modes if isinstance(raw_modes, dict) else {}
        custom_profiles = mode_data.get("profiles", {})
        if isinstance(custom_profiles, dict):
            for name, value in custom_profiles.items():
                if not isinstance(value, dict):
                    continue
                base = profiles.get(name)
                if isinstance(base, dict):
                    merged = deepcopy(base)
                    merged.update(value)
                    profiles[str(name)] = merged
                else:
                    profiles[str(name)] = value
        refs: dict[str, list[str]] = {}
        for mode_name, profile in profiles.items():
            if not isinstance(profile, dict):
                continue
            for tool_name in self._string_list(profile.get("tools")):
                refs.setdefault(tool_name, []).append(str(mode_name))
        return {key: sorted(value) for key, value in refs.items()}

    def _tool_mode_refs(
        self, mode_refs: dict[str, list[str]], tool_name: str
    ) -> list[str]:
        return sorted(dict.fromkeys(mode_refs.get(tool_name, []) + mode_refs.get("*", [])))

    def _approval_action_for_tool(
        self,
        data: dict[str, Any],
        *,
        tool_name: str,
        source_type: str,
        mcp_server: str | None = None,
    ) -> str:
        approval = self._approval_settings(data)
        config = ApprovalConfig(
            default_mode=cast(
                ApprovalAction,
                str(approval.get("default_mode") or "require_approval"),
            ),
            rules=[
                ApprovalRuleConfig(
                    tool_name=rule.get("tool_name"),
                    tool_source=rule.get("tool_source"),
                    mcp_server=rule.get("mcp_server"),
                    effect_class=rule.get("effect_class"),
                    profile=rule.get("profile"),
                    action=rule.get("action", "require_approval"),
                )
                for rule in approval.get("rules", [])
                if isinstance(rule, dict)
            ],
        )
        tool_source = (
            source_type
            if source_type in {"builtin", "mcp", "unknown"}
            else "unknown"
        )
        match = ApprovalPolicyEngine(config).evaluate(
            ToolApprovalContext(
                tool_call=ToolCall(id="catalog", name=tool_name, arguments={}),
                tool_name=tool_name,
                tool_source=cast(ToolSource, tool_source),
                mcp_server=mcp_server,
            )
        )
        return str(match.action)

    def _execution_policy_for_approval(self, approval_status: str) -> str:
        status = str(approval_status or "").strip()
        if status == "allow":
            return "allow"
        if status == "deny":
            return "deny"
        if status == "warn":
            return "escalate"
        if status == "require_approval":
            return "require_user"
        return "inherit"

    def _tool_source_label(self, kind: str) -> str:
        if kind == "environment_requirement":
            return "Environment requirement"
        if kind == "mcp":
            return "MCP"
        return kind

    def _admin_resource_registration_path(self, kind: str, name: str) -> str:
        if kind == "environment_requirement":
            return f"environment.requirements.{name}"
        if kind == "mcp":
            return f"mcp.servers.{name}"
        return name

    def _string_list(self, value: Any) -> list[str]:
        return [
            str(item)
            for item in value
            if str(item).strip()
        ] if isinstance(value, list) else []

    def _provider_view(self, provider_id: str, item: dict[str, Any]) -> dict[str, Any]:
        provider = ProviderConfig.from_dict(provider_id, item)
        view = provider.to_dict()
        view.pop("api_key", None)
        view["api_key_hint"] = _mask(str(item.get("api_key", "") or ""))
        view["id"] = provider_id
        view["models"] = _normalize_provider_models(item.get("models", []))
        return view

    def _profile_view(
        self,
        profile_id: str,
        item: dict[str, Any],
        *,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        profile = ModelProfileConfig.from_dict(profile_id, item)
        view = profile.to_dict()
        view["id"] = profile_id
        if "capability_user_configured" in item:
            view["capability_user_configured"] = bool(
                item.get("capability_user_configured")
            )
        if item.get("capability_source"):
            view["capability_source"] = item.get("capability_source")
        if item.get("capability_applied_at"):
            view["capability_applied_at"] = item.get("capability_applied_at")
        capability = None
        if data is not None:
            capability = self.model_capability_catalog.lookup(
                self._provider_config_for_profile(data, item)
                or str(item.get("provider") or ""),
                profile.model,
            )
        recommendation = capability_recommendation(
            capability,
            current_max_tokens=profile.max_tokens,
            current_max_context_tokens=profile.max_context_tokens,
        )
        if recommendation is not None:
            view["capability_recommendation"] = recommendation
        return view


def _field_or_env(payload: dict[str, Any], field_name: str, env_field_name: str) -> str | None:
    if env_field_name in payload and payload.get(env_field_name):
        return "${" + str(payload[env_field_name]).strip() + "}"
    if field_name in payload:
        value = payload.get(field_name)
        return str(value) if value is not None else ""
    return None


def _dict_field(payload: dict[str, Any], field_name: str, previous: dict[str, Any]) -> dict[str, Any]:
    value = payload.get(field_name, previous.get(field_name, {}))
    return dict(value) if isinstance(value, dict) else {}


def _bool_field(payload: dict[str, Any], field_name: str, default: Any) -> bool:
    if field_name not in payload:
        return bool(default)
    value = payload.get(field_name)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _environment_requirement_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_payload = payload.get("environment_requirement")
    if not isinstance(raw_payload, dict):
        raw_payload = payload.get("payload")
    return dict(raw_payload) if isinstance(raw_payload, dict) else dict(payload)


def _mcp_server_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_payload = payload.get("mcp_server")
    if not isinstance(raw_payload, dict):
        raw_payload = payload.get("payload")
    return dict(raw_payload) if isinstance(raw_payload, dict) else dict(payload)


def _normalize_provider_models(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            model_id = item.strip()
            model = {"id": model_id}
        elif isinstance(item, dict):
            model_id = str(
                item.get("id") or item.get("model_id") or item.get("model") or ""
            ).strip()
            model = {
                "id": model_id,
                **{
                    str(key): val
                    for key, val in item.items()
                    if key not in {"api_key", "secret", "token"}
                },
            }
        else:
            continue
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append(model)
    models.sort(key=lambda item: str(item.get("id") or ""))
    return models


def _looks_like_url(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("https://") or text.startswith("http://")


def _admin_resource_dashboard_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "total": len(items),
        "ready": 0,
        "missing": 0,
        "stopped": 0,
        "awaiting": 0,
    }
    for item in items:
        status = str(item.get("status") or "")
        if status in {"ready", "configured"}:
            summary["ready"] += 1
        elif status == "missing":
            summary["missing"] += 1
        elif status == "stopped":
            summary["stopped"] += 1
        elif status in {"awaiting_approval", "needs_review", "parse_failed"}:
            summary["awaiting"] += 1
    return summary


def _mask(value: str) -> str:
    if not value:
        return "(empty)"
    if value.startswith("${") and value.endswith("}"):
        return value
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"

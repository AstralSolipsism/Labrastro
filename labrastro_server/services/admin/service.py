"""Admin helpers for the remote relay HTTP service."""

from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

from reuleauxcoder.app.runtime.agent_runtime import get_interactive_run_limiter
from reuleauxcoder.domain.config.models import (
    AgentRegistryConfig,
    CapabilityPackageConfig,
    DiagnosticsConfig,
    EnvironmentCLIToolConfig,
    EnvironmentSkillConfig,
    GitHubConfig,
    MCPServerConfig,
    ModelProfileConfig,
    ProviderApiFeatures,
    ProviderConfig,
    RunLimitsConfig,
    RuntimeProfilesConfig,
    SandboxProviderConfig,
    ensure_default_capability_packages,
    ensure_default_environment_agent_registry,
    infer_provider_compat,
)
from reuleauxcoder.domain.config.schema import BUILTIN_MODES, DEFAULT_ACTIVE_MODE
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.services.config.loader import ConfigLoader
from reuleauxcoder.services.llm.diagnostics import (
    summarize_tool_argument_validation_events,
)
from reuleauxcoder.services.providers.model_capabilities import (
    ModelCapabilityCatalogService,
    capability_recommendation,
    capability_source_label,
    utc_now_iso,
)
from reuleauxcoder.services.providers.manager import ProviderManager


ProviderTestHandler = Callable[[ProviderConfig, str, str], dict[str, Any]]
ProviderModelsHandler = Callable[[ProviderConfig], dict[str, Any]]
ConfigReloadHandler = Callable[[], None]


@dataclass(slots=True)
class AdminConfigResult:
    ok: bool
    payload: dict[str, Any]
    status: int = 200


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

    def read_server_settings(self) -> dict[str, Any]:
        data = self._load_data()
        raw_capability_packages = data.get("capability_packages", {})
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

    def tool_argument_validation_stats(self) -> dict[str, Any]:
        return summarize_tool_argument_validation_events()

    def update_server_settings(self, payload: dict[str, Any]) -> AdminConfigResult:
        raw_settings = payload.get("settings")
        raw_agent_registry = payload.get("agent_registry")
        raw_runtime_profiles = payload.get("runtime_profiles")
        raw_run_limits = payload.get("run_limits")
        raw_capability_packages = payload.get("capability_packages")
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
            and not isinstance(raw_github, dict)
            and not isinstance(raw_sandbox, dict)
            and not isinstance(raw_model_capabilities, dict)
            and not isinstance(raw_diagnostics, dict)
        ):
            return AdminConfigResult(False, {"error": "server_settings_required"}, 400)
        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            previous_packages = (
                previous_data.get("capability_packages", {})
                if isinstance(previous_data.get("capability_packages"), dict)
                else {}
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

    def list_toolchains(self) -> dict[str, Any]:
        data = self._load_data()
        return {
            "cli_tools": self._toolchain_views(data, "cli"),
            "mcp_servers": self._toolchain_views(data, "mcp"),
            "skills": self._toolchain_views(data, "skill"),
        }

    def toolchain_dashboard(self) -> dict[str, Any]:
        data = self._load_data()
        items: list[dict[str, Any]] = []
        for kind in ("cli", "mcp", "skill"):
            items.extend(
                self._toolchain_dashboard_item(kind, item)
                for item in self._toolchain_views(data, kind)
            )
        return {
            "items": items,
            "summary": _toolchain_dashboard_summary(items),
        }

    def record_toolchain(self, payload: dict[str, Any]) -> AdminConfigResult:
        kind, item_payload = _toolchain_payload(payload)
        if kind is None:
            return AdminConfigResult(False, {"error": "toolchain_kind_required"}, 400)
        name = str(item_payload.get("name") or payload.get("name") or "").strip()
        if not name:
            return AdminConfigResult(False, {"error": "toolchain_name_required"}, 400)

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._toolchain_items(data, kind)
            previous = items.get(name, {}) if isinstance(items.get(name), dict) else {}
            merged = {**previous, **item_payload}
            merged.pop("name", None)
            merged.pop("kind", None)
            normalized = self._normalize_toolchain_item(kind, name, merged)
            items[name] = normalized
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "kind": kind,
                    "name": name,
                    "created": not previous,
                    "toolchain": self._toolchain_view(kind, name, items[name]),
                },
            )

    def delete_toolchain(self, payload: dict[str, Any]) -> AdminConfigResult:
        kind, item_payload = _toolchain_payload(payload)
        if kind is None:
            return AdminConfigResult(False, {"error": "toolchain_kind_required"}, 400)
        name = str(item_payload.get("name") or payload.get("name") or "").strip()
        if not name:
            return AdminConfigResult(False, {"error": "toolchain_name_required"}, 400)

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._toolchain_items(data, kind)
            if name not in items:
                return AdminConfigResult(False, {"error": "toolchain_not_found"}, 404)
            del items[name]
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(True, {"ok": True, "kind": kind, "name": name})

    def enable_toolchain(self, payload: dict[str, Any]) -> AdminConfigResult:
        kind, item_payload = _toolchain_payload(payload)
        if kind is None:
            return AdminConfigResult(False, {"error": "toolchain_kind_required"}, 400)
        name = str(item_payload.get("name") or payload.get("name") or "").strip()
        if not name:
            return AdminConfigResult(False, {"error": "toolchain_name_required"}, 400)
        enabled = _bool_field(item_payload, "enabled", payload.get("enabled", True))

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            items = self._toolchain_items(data, kind)
            item = items.get(name)
            if not isinstance(item, dict):
                return AdminConfigResult(False, {"error": "toolchain_not_found"}, 404)
            item["enabled"] = enabled
            items[name] = self._normalize_toolchain_item(kind, name, item)
            reload_error = self._commit_config(data, previous_data)
            if reload_error:
                return reload_error
            return AdminConfigResult(
                True,
                {
                    "ok": True,
                    "kind": kind,
                    "name": name,
                    "toolchain": self._toolchain_view(kind, name, items[name]),
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
        data = self._load_data()
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
        if payload.get("api_key") and payload.get("api_key_env"):
            return AdminConfigResult(False, {"error": "api_key_conflict"}, 400)

        with self._lock:
            previous_data = self._load_data()
            data = deepcopy(previous_data)
            profiles = self._model_profiles(data)
            previous = profiles.get(profile_id, {}) if isinstance(profiles.get(profile_id), dict) else {}
            api_key = _field_or_env(payload, "api_key", "api_key_env")
            if api_key is None:
                api_key = previous.get("api_key", "")
            model_name = str(payload.get("model") or previous.get("model") or "gpt-4o")
            provider_id = payload.get("provider", previous.get("provider"))
            provider_config = None
            provider_item = self._provider_items(data).get(str(provider_id or ""))
            if isinstance(provider_item, dict):
                provider_config = ProviderConfig.from_dict(str(provider_id or ""), provider_item)
            catalog_capability = self.model_capability_catalog.lookup(
                provider_config or str(provider_id or ""),
                model_name,
            )
            capability_defaults = ProviderManager.known_model_capabilities(
                provider_config or str(provider_id or ""),
                model_name,
            )
            if catalog_capability is not None:
                if catalog_capability.max_output_tokens:
                    capability_defaults["max_tokens"] = catalog_capability.max_output_tokens
                if catalog_capability.max_context_tokens:
                    capability_defaults["max_context_tokens"] = (
                        catalog_capability.max_context_tokens
                    )
            user_configured = bool(
                payload.get("capability_user_configured")
                if "capability_user_configured" in payload
                else (
                    "max_tokens" in payload
                    or "max_context_tokens" in payload
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
                    if payload.get("max_tokens") is not None
                    else previous.get("max_tokens")
                )
            )
            max_context_tokens_value = (
                recommended_max_context_tokens
                if not user_configured and recommended_max_context_tokens
                else (
                    payload.get("max_context_tokens")
                    if payload.get("max_context_tokens") is not None
                    else previous.get("max_context_tokens")
                )
            )
            profile_data = {
                "model": model_name,
                "api_key": str(api_key or ""),
                "provider": provider_id,
                "base_url": payload.get("base_url", previous.get("base_url")),
                "max_tokens": int(max_tokens_value or recommended_max_tokens or 4096),
                "temperature": float(payload.get("temperature") if payload.get("temperature") is not None else previous.get("temperature", 0.0)),
                "max_context_tokens": int(
                    max_context_tokens_value or recommended_max_context_tokens or 128000
                ),
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

    def _toolchain_items(self, data: dict[str, Any], kind: str) -> dict[str, Any]:
        if kind in {"cli", "skill"}:
            environment = data.setdefault("environment", {})
            if not isinstance(environment, dict):
                environment = {}
                data["environment"] = environment
            key = "cli_tools" if kind == "cli" else "skills"
            items = environment.setdefault(key, {})
            if not isinstance(items, dict):
                items = {}
                environment[key] = items
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

    def _toolchain_views(self, data: dict[str, Any], kind: str) -> list[dict[str, Any]]:
        items = self._toolchain_items(data, kind)
        views: list[dict[str, Any]] = []
        for name in sorted(items):
            item = items.get(name)
            if not isinstance(item, dict):
                continue
            views.append(self._toolchain_view(kind, str(name), item))
        return views

    def _normalize_toolchain_item(
        self, kind: str, name: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        if kind == "cli":
            return EnvironmentCLIToolConfig.from_dict(name, item).to_dict()
        if kind == "skill":
            return EnvironmentSkillConfig.from_dict(name, item).to_dict()
        return MCPServerConfig.from_dict(name, item).to_dict()

    def _toolchain_view(
        self, kind: str, name: str, item: dict[str, Any]
    ) -> dict[str, Any]:
        view = self._normalize_toolchain_item(kind, name, item)
        view["kind"] = kind
        view["name"] = name
        view["id"] = name
        return view

    def _toolchain_dashboard_item(
        self, kind: str, view: dict[str, Any]
    ) -> dict[str, Any]:
        name = str(view.get("name") or view.get("id") or "")
        docs = list(view.get("docs") or []) if isinstance(view.get("docs"), list) else []
        repo_url = str(view.get("repo_url") or "")
        if not repo_url and _looks_like_url(view.get("source")):
            repo_url = str(view.get("source"))
        placement = str(view.get("placement") or "")
        scope = str(view.get("scope") or "")
        if kind == "cli":
            placement = placement or "local"
            scope = placement
        elif kind == "mcp":
            placement = placement or "server"
            scope = placement
        else:
            placement = scope or "project"
            scope = placement
        status = "unchecked" if _bool_field(view, "enabled", True) else "stopped"
        return {
            "id": f"{kind}:{name}",
            "kind": kind,
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
            "requirements": (
                dict(view.get("requirements") or {})
                if isinstance(view.get("requirements"), dict)
                else {}
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
        }

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
        view.pop("api_key", None)
        view["api_key_hint"] = _mask(str(item.get("api_key", "") or ""))
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


def _toolchain_payload(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    raw_kind = str(payload.get("kind") or "").strip().lower()
    kind_map = {
        "cli": "cli",
        "cli_tool": "cli",
        "cli_tools": "cli",
        "mcp": "mcp",
        "mcp_server": "mcp",
        "mcp_servers": "mcp",
        "skill": "skill",
        "skills": "skill",
    }
    kind = kind_map.get(raw_kind)
    raw_payload = payload.get("payload")
    item_payload = dict(raw_payload) if isinstance(raw_payload, dict) else dict(payload)
    return kind, item_payload


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


def _toolchain_dashboard_summary(items: list[dict[str, Any]]) -> dict[str, int]:
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

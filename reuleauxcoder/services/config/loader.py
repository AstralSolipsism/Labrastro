"""Configuration loader for host-authoritative and local bootstrap configs."""

import os
from pathlib import Path
import re
from typing import Optional
import yaml

from reuleauxcoder.domain.config.models import (
    AgentRegistryConfig,
    AuthConfig,
    ApprovalConfig,
    ApprovalRuleConfig,
    Config,
    ContextConfig,
    DIAGNOSTICS_CONFIG_FIELDS,
    DiagnosticsConfig,
    EnvironmentCLIToolConfig,
    EnvironmentConfig,
    EnvironmentSkillConfig,
    GitHubConfig,
    DEFAULT_BUILTIN_TOOL_COMPONENTS,
    DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE,
    DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID,
    DEFAULT_MAIN_CHAT_AGENT,
    DEFAULT_MAIN_CHAT_AGENT_ID,
    CapabilityComponentConfig,
    CapabilityPackageConfig,
    LLM_TRACE_DIAGNOSTICS_CONFIG_FIELDS,
    MemoryConfig,
    MCPServerConfig,
    ModeConfig,
    MODEL_PROFILE_CONFIG_FIELDS,
    ModelProfileConfig,
    PersistenceConfig,
    PromptConfig,
    PROVIDER_CONFIG_FIELDS,
    ProviderConfig,
    ProvidersConfig,
    RemoteExecConfig,
    RunLimitsConfig,
    RuntimeProfilesConfig,
    SandboxProviderConfig,
    SkillsConfig,
    TOOL_DIAGNOSTICS_CONFIG_FIELDS,
    ensure_default_capability_components,
    ensure_default_capability_packages,
    ensure_default_environment_agent_registry,
)
from reuleauxcoder.domain.config.schema import (
    BUILTIN_MODES,
    DEFAULTS,
    DEFAULT_ACTIVE_MODE,
)
from reuleauxcoder.infrastructure.yaml.loader import save_yaml_config, load_yaml_config


class ExampleConfigError(Exception):
    """Raised when the config is still the example template and needs user editing."""


class ConfigEnvironmentError(Exception):
    """Raised when config references a missing environment variable."""


class ConfigSchemaError(Exception):
    """Raised when config contains fields outside the current schema."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        message = "Invalid configuration schema:\n" + "\n".join(
            f"  - {error}" for error in self.errors
        )
        super().__init__(message)


class ConfigValidationError(Exception):
    """Raised when loaded config is structurally complete but invalid."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        message = "Invalid configuration:\n" + "\n".join(
            f"  - {error}" for error in self.errors
        )
        super().__init__(message)


class ConfigLoader:
    """Loads configuration from config.yaml.

    Two loading modes are intentionally separate:
    1. Explicit config: only the given file plus builtin defaults.
    2. Layered user config: global + workspace, only for no --config bootstrap.
    """

    GLOBAL_CONFIG_PATH = Path.home() / ".rcoder" / "config.yaml"
    WORKSPACE_CONFIG_PATH = Path.cwd() / ".rcoder" / "config.yaml"

    _ROOT_FIELDS = {
        "agent_registry",
        "approval",
        "auth",
        "capability_packages",
        "capability_components",
        "cli",
        "context",
        "diagnostics",
        "environment",
        "github",
        "lsp",
        "mcp",
        "memory",
        "meta",
        "model_capabilities",
        "models",
        "modes",
        "persistence",
        "prompt",
        "providers",
        "remote_exec",
        "run_limits",
        "runtime_profiles",
        "sandbox_provider",
        "session",
        "skills",
        "tool_output",
    }
    _PROVIDERS_FIELDS = {"items"}
    _PROVIDER_ITEM_FIELDS = set(PROVIDER_CONFIG_FIELDS) | {"models"}
    _MODELS_FIELDS = {"active_main", "active_sub", "profiles"}
    _MODEL_PROFILE_FIELDS = set(MODEL_PROFILE_CONFIG_FIELDS)
    _DIAGNOSTICS_FIELDS = set(DIAGNOSTICS_CONFIG_FIELDS)
    _LLM_TRACE_FIELDS = set(LLM_TRACE_DIAGNOSTICS_CONFIG_FIELDS)
    _TOOL_DIAGNOSTICS_FIELDS = set(TOOL_DIAGNOSTICS_CONFIG_FIELDS)
    _UNKNOWN_FIELD_RULES = (
        ((), _ROOT_FIELDS),
        (("providers",), _PROVIDERS_FIELDS),
        (("providers", "items", "*"), _PROVIDER_ITEM_FIELDS),
        (("models",), _MODELS_FIELDS),
        (("models", "profiles", "*"), _MODEL_PROFILE_FIELDS),
        (("diagnostics",), _DIAGNOSTICS_FIELDS),
        (("diagnostics", "llm_trace"), _LLM_TRACE_FIELDS),
        (("diagnostics", "tool_diagnostics"), _TOOL_DIAGNOSTICS_FIELDS),
    )
    _SECTION_MAP_KEYS = {
        "mcp": {"servers"},
        "models": {"profiles"},
        "modes": {"profiles"},
        "providers": {"items"},
    }

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = Path(config_path) if config_path is not None else None

    _ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

    def _expand_env_value(self, value, field_path: str):
        if not isinstance(value, str):
            return value
        match = self._ENV_REF_RE.match(value.strip())
        if match is None:
            return value
        env_name = match.group(1)
        if env_name not in os.environ:
            raise ConfigEnvironmentError(
                f"Config field '{field_path}' references missing environment variable '{env_name}'"
            )
        return os.environ[env_name]

    def _expand_env_refs(self, data: dict) -> dict:
        """Expand supported ${ENV_NAME} references in provider/model runtime fields."""
        expanded = dict(data)
        providers = expanded.get("providers", {})
        if isinstance(providers, dict):
            items = providers.get("items", {})
            if isinstance(items, dict):
                for provider_id, provider_data in items.items():
                    if not isinstance(provider_data, dict):
                        continue
                    for field in ("api_key", "base_url"):
                        if field in provider_data:
                            provider_data[field] = self._expand_env_value(
                                provider_data[field],
                                f"providers.items.{provider_id}.{field}",
                            )

        persistence = expanded.get("persistence", {})
        if isinstance(persistence, dict) and "database_url" in persistence:
            value = str(persistence.get("database_url") or "").strip()
            if value:
                match = self._ENV_REF_RE.match(value)
                if match is not None and match.group(1) not in os.environ:
                    persistence["database_url"] = ""
                else:
                    persistence["database_url"] = self._expand_env_value(
                        persistence["database_url"], "persistence.database_url"
                    )
        auth = expanded.get("auth", {})
        if isinstance(auth, dict):
            for field in ("token_secret", "store_path"):
                if field in auth:
                    value = str(auth.get(field) or "").strip()
                    if value:
                        auth[field] = self._expand_env_value(
                            auth[field], f"auth.{field}"
                        )
            raw_superadmins = auth.get("superadmins", [])
            if isinstance(raw_superadmins, list):
                for index, item in enumerate(raw_superadmins):
                    if not isinstance(item, dict):
                        continue
                    for field in ("username", "password"):
                        if field in item:
                            value = str(item.get(field) or "").strip()
                            if value:
                                item[field] = self._expand_env_value(
                                    item[field],
                                    f"auth.superadmins.{index}.{field}",
                                )
        github = expanded.get("github", {})
        if isinstance(github, dict):
            for field in (
                "app_id",
                "installation_id",
                "private_key_path",
                "webhook_secret",
                "api_base_url",
                "web_base_url",
            ):
                if field in github:
                    value = str(github.get(field) or "").strip()
                    if value:
                        github[field] = self._expand_env_value(
                            github[field], f"github.{field}"
                        )
        return expanded

    @staticmethod
    def _collect_unknown_fields(
        data: dict,
        *,
        allowed: set[str],
        path: str,
        errors: list[str],
    ) -> None:
        prefix = f"{path}." if path else ""
        for key in sorted(data):
            if str(key) not in allowed:
                errors.append(f"Unknown config field: {prefix}{key}")

    @staticmethod
    def _schema_rule_targets(data: dict, path_parts: tuple[str, ...]):
        targets: list[tuple[str, dict]] = [("", data)]
        for part in path_parts:
            next_targets: list[tuple[str, dict]] = []
            for current_path, current_data in targets:
                if not isinstance(current_data, dict):
                    continue
                if part == "*":
                    for child_key, child_data in current_data.items():
                        if not isinstance(child_data, dict):
                            continue
                        child_path = (
                            f"{current_path}.{child_key}"
                            if current_path
                            else str(child_key)
                        )
                        next_targets.append((child_path, child_data))
                    continue
                child_data = current_data.get(part)
                if not isinstance(child_data, dict):
                    continue
                child_path = f"{current_path}.{part}" if current_path else part
                next_targets.append((child_path, child_data))
            targets = next_targets
        return targets

    def _reject_unknown_config_fields(self, data: dict) -> None:
        """Reject fields outside the current config schema."""
        if not isinstance(data, dict):
            return
        errors: list[str] = []
        for path_parts, allowed in self._UNKNOWN_FIELD_RULES:
            for path, target in self._schema_rule_targets(data, path_parts):
                self._collect_unknown_fields(
                    target, allowed=allowed, path=path, errors=errors
                )
        if errors:
            raise ConfigSchemaError(errors)

    @staticmethod
    def _named_config_items(section: object, key: str) -> tuple[tuple[str, dict], ...]:
        if not isinstance(section, dict):
            return ()
        items = section.get(key, {})
        if not isinstance(items, dict):
            return ()
        return tuple(
            (str(name), item) for name, item in items.items() if isinstance(item, dict)
        )

    @staticmethod
    def _config_section(data: dict, key: str) -> dict:
        section = data.get(key, {})
        return section if isinstance(section, dict) else {}

    def _merge_named_map(self, base: object, override: dict) -> dict:
        merged = dict(base) if isinstance(base, dict) else {}
        for item_name, item_value in override.items():
            if isinstance(item_value, dict) and isinstance(merged.get(item_name), dict):
                merged[item_name] = self._merge_dicts(merged[item_name], item_value)
            else:
                merged[item_name] = item_value
        return merged

    def _merge_config_section(
        self, base: object, override: dict, *, map_keys: set[str]
    ) -> dict:
        merged = dict(base) if isinstance(base, dict) else {}
        for section_key, section_value in override.items():
            if section_key in map_keys and isinstance(section_value, dict):
                merged[section_key] = self._merge_named_map(
                    merged.get(section_key, {}), section_value
                )
            else:
                merged[section_key] = section_value
        return merged

    def _load_yaml(self, path: Path) -> dict:
        """Load YAML file, return empty dict if not exists or invalid."""
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
                return data if data else {}
        except (yaml.YAMLError, IOError):
            return {}

    def _merge_dicts(self, base: dict, override: dict) -> dict:
        """Merge two dicts, override takes priority.

        For nested dicts, merge recursively.
        For configured map sections, merge by key (override wins for same key).
        """
        result = dict(base)

        for key, value in override.items():
            map_keys = self._SECTION_MAP_KEYS.get(key)
            if map_keys is not None and isinstance(value, dict):
                result[key] = self._merge_config_section(
                    result.get(key, {}), value, map_keys=map_keys
                )
            elif (
                isinstance(value, dict)
                and key in result
                and isinstance(result[key], dict)
            ):
                # Recursively merge nested dicts
                result[key] = self._merge_dicts(result[key], value)
            else:
                # Override wins
                result[key] = value

        return result

    def _base_config_data(self) -> dict:
        return {
            "modes": {
                "active": DEFAULT_ACTIVE_MODE,
                "profiles": dict(BUILTIN_MODES),
            }
        }

    def load(self) -> Config:
        """Load configuration using explicit-only or local layered semantics."""
        if self.config_path is not None:
            return self.load_explicit_config(self.config_path)
        return self.load_layered_user_config()

    def load_explicit_config(self, path: Path) -> Config:
        """Load a host/admin config without reading HOME or workspace configs."""
        config_data = self._base_config_data()

        explicit_data = self._load_yaml(path)
        if explicit_data:
            config_data = self._merge_dicts(config_data, explicit_data)

        self._reject_unknown_config_fields(config_data)

        has_runtime_config = bool(config_data.get("models"))
        if not has_runtime_config and not self._is_remote_host_mode_config(config_data):
            raise ExampleConfigError(
                f"\n  The explicit config at {path} is missing model/runtime configuration.\n"
                "  Configure providers.items and models.profiles, or enable remote_exec.host_mode.\n"
            )

        config_data = self._expand_env_refs(config_data)
        return self._parse_config(config_data)

    def load_layered_user_config(self) -> Config:
        """Load global + workspace config for local bootstrap/dev usage."""
        config_data = self._base_config_data()

        # Load global config
        global_data = self._load_yaml(self.GLOBAL_CONFIG_PATH)

        # Detect example config — user still needs to configure providers and model profiles.
        if global_data and self._is_example_config(global_data):
            raise ExampleConfigError(
                f"\n  The config at {self.GLOBAL_CONFIG_PATH} is still the example template.\n"
                "  Please configure providers.items and models.profiles, then restart.\n"
            )

        if global_data:
            config_data = self._merge_dicts(config_data, global_data)

        # Load workspace config (overrides global)
        workspace_data = self._load_yaml(self.WORKSPACE_CONFIG_PATH)
        if workspace_data:
            config_data = self._merge_dicts(config_data, workspace_data)

        self._reject_unknown_config_fields(config_data)

        # Require explicit model/runtime config for local CLI usage. Remote host mode
        # can bootstrap without a model so admins can configure providers via UI.
        has_runtime_config = bool(config_data.get("models"))
        if not has_runtime_config and not self._is_remote_host_mode_config(config_data):
            self._generate_example_global_config()
            raise ExampleConfigError(
                f"\n  Welcome to ReuleauxCoder! \U0001F389\n\n"
                f"  No config.yaml found. I've created an example at:\n"
                f"    {self.GLOBAL_CONFIG_PATH}\n\n"
                "  Please configure providers.items and models.profiles, then restart.\n"
            )

        config_data = self._expand_env_refs(config_data)
        self._bootstrap_workspace_snapshot(config_data, workspace_data)

        config = self._parse_config(config_data)
        self._backfill_workspace_modes(config)
        return config

    def _parse_config(self, data: dict) -> Config:
        """Parse YAML data into Config model."""
        self._reject_unknown_config_fields(data)
        approval_config = data.get("approval", {})
        tool_output_config = data.get("tool_output", {})
        session_config = data.get("session", {})
        cli_config = data.get("cli", {})
        mcp_config = self._config_section(data, "mcp")
        models_config = self._config_section(data, "models")
        providers_config = self._config_section(data, "providers")
        modes_config = self._config_section(data, "modes")
        skills_config = data.get("skills", {})
        prompt_config = data.get("prompt", {})
        context_config = data.get("context", {})
        memory_config = data.get("memory", {})
        remote_exec_config = data.get("remote_exec", {})
        lsp_config = data.get("lsp", {})
        auth_config = data.get("auth", {})
        agent_registry_config = data.get("agent_registry", {})
        runtime_profiles_config = data.get("runtime_profiles", {})
        run_limits_config = data.get("run_limits", {})
        capability_packages_config = data.get("capability_packages", {})
        capability_components_config = data.get("capability_components", {})
        persistence_config = data.get("persistence", {})
        diagnostics_config = data.get("diagnostics", {})
        sandbox_provider_config = data.get("sandbox_provider", {})
        github_config = data.get("github", {})
        environment_config = data.get("environment", {})
        if not isinstance(environment_config, dict):
            environment_config = {}

        providers = ProvidersConfig()
        for provider_id, provider_data in self._named_config_items(
            providers_config, "items"
        ):
            providers.items[str(provider_id)] = ProviderConfig.from_dict(
                provider_id, provider_data
            )

        # Parse MCP servers
        mcp_servers = [
            MCPServerConfig.from_dict(name, server_data)
            for name, server_data in self._named_config_items(mcp_config, "servers")
        ]

        # Parse model profiles
        model_profiles: dict[str, ModelProfileConfig] = {
            name: ModelProfileConfig.from_dict(name, profile_data)
            for name, profile_data in self._named_config_items(
                models_config, "profiles"
            )
        }

        active_main_model_profile = models_config.get("active_main")
        if (
            not isinstance(active_main_model_profile, str)
            or active_main_model_profile not in model_profiles
        ):
            active_main_model_profile = next(iter(model_profiles.keys()), None)

        active_sub_model_profile = models_config.get("active_sub")
        if (
            not isinstance(active_sub_model_profile, str)
            or active_sub_model_profile not in model_profiles
        ):
            active_sub_model_profile = active_main_model_profile

        # Parse modes (builtin modes already merged during load())
        modes: dict[str, ModeConfig] = {
            name: ModeConfig.from_dict(name, mode_data)
            for name, mode_data in self._named_config_items(modes_config, "profiles")
        }

        active_mode = modes_config.get("active")
        if not isinstance(active_mode, str) or active_mode not in modes:
            active_mode = (
                DEFAULT_ACTIVE_MODE
                if DEFAULT_ACTIVE_MODE in modes
                else next(iter(modes.keys()), None)
            )

        approval_rules = [
            ApprovalRuleConfig(
                tool_name=rule.get("tool_name"),
                tool_source=rule.get("tool_source"),
                mcp_server=rule.get("mcp_server"),
                effect_class=rule.get("effect_class"),
                profile=rule.get("profile"),
                action=rule.get("action", "require_approval"),
            )
            for rule in approval_config.get("rules", DEFAULTS["approval_rules"])
        ]

        cli_tools: dict[str, EnvironmentCLIToolConfig] = {}
        cli_tools_data = environment_config.get("cli_tools", {})
        if isinstance(cli_tools_data, dict):
            for name, tool_data in cli_tools_data.items():
                if not isinstance(tool_data, dict):
                    continue
                cli_tools[str(name)] = EnvironmentCLIToolConfig.from_dict(
                    str(name), tool_data
                )

        skills: dict[str, EnvironmentSkillConfig] = {}
        skills_data = environment_config.get("skills", {})
        if isinstance(skills_data, dict):
            for name, skill_data in skills_data.items():
                if not isinstance(skill_data, dict):
                    continue
                skills[str(name)] = EnvironmentSkillConfig.from_dict(
                    str(name), skill_data
                )

        capability_packages: dict[str, CapabilityPackageConfig] = {}
        raw_capability_packages = ensure_default_capability_packages(
            capability_packages_config
            if isinstance(capability_packages_config, dict)
            else {}
        )
        for package_id, package_data in raw_capability_packages.items():
            if not isinstance(package_data, dict):
                continue
            capability_packages[str(package_id)] = CapabilityPackageConfig.from_dict(
                str(package_id), package_data
            )

        capability_components: dict[str, CapabilityComponentConfig] = {}
        raw_capability_components = ensure_default_capability_components(
            capability_components_config
            if isinstance(capability_components_config, dict)
            else {}
        )
        if isinstance(raw_capability_components, dict):
            for component_id, component_data in raw_capability_components.items():
                if not isinstance(component_data, dict):
                    continue
                capability_components[str(component_id)] = (
                    CapabilityComponentConfig.from_dict(
                        str(component_id), component_data
                    )
                )

        agent_registry_data, runtime_profiles_data = ensure_default_environment_agent_registry(
            agent_registry_config if isinstance(agent_registry_config, dict) else {},
            runtime_profiles_config if isinstance(runtime_profiles_config, dict) else {},
        )
        agent_registry = AgentRegistryConfig.from_dict(agent_registry_data)
        runtime_profiles = RuntimeProfilesConfig.from_dict(runtime_profiles_data)
        run_limits = RunLimitsConfig.from_dict(
            run_limits_config if isinstance(run_limits_config, dict) else {}
        )
        diagnostics = DiagnosticsConfig.from_dict(
            diagnostics_config if isinstance(diagnostics_config, dict) else {}
        )
        diagnostics_has_llm_trace = (
            isinstance(diagnostics_config, dict)
            and isinstance(diagnostics_config.get("llm_trace"), dict)
        )

        return Config(
            mcp_servers=mcp_servers,
            mcp_artifact_root=str(
                mcp_config.get("artifact_root", ".rcoder/mcp-artifacts")
            ),
            model_profiles=model_profiles,
            providers=providers,
            active_main_model_profile=active_main_model_profile,
            active_sub_model_profile=active_sub_model_profile,
            modes=modes,
            active_mode=active_mode,
            tool_output_max_chars=tool_output_config.get(
                "max_chars", DEFAULTS["tool_output_max_chars"]
            ),
            tool_output_max_lines=tool_output_config.get(
                "max_lines", DEFAULTS["tool_output_max_lines"]
            ),
            tool_output_store_full=tool_output_config.get(
                "store_full_output", DEFAULTS["tool_output_store_full"]
            ),
            tool_output_store_dir=tool_output_config.get(
                "store_dir", DEFAULTS["tool_output_store_dir"]
            ),
            approval=ApprovalConfig(
                default_mode=approval_config.get(
                    "default_mode", DEFAULTS["approval_default_mode"]
                ),
                rules=approval_rules,
            ),
            skills=SkillsConfig(
                enabled=skills_config.get("enabled", True),
                scan_project=skills_config.get("scan_project", True),
                scan_user=skills_config.get("scan_user", True),
                disabled=[
                    str(name)
                    for name in skills_config.get("disabled", [])
                    if str(name).strip()
                ],
            ),
            prompt=PromptConfig(
                system_append=str(prompt_config.get("system_append", "") or ""),
            ),
            context=ContextConfig(
                snip_keep_recent_tools=context_config.get(
                    "snip_keep_recent_tools", DEFAULTS["snip_keep_recent_tools"]
                ),
                snip_threshold_chars=context_config.get(
                    "snip_threshold_chars", DEFAULTS["snip_threshold_chars"]
                ),
                snip_min_lines=context_config.get(
                    "snip_min_lines", DEFAULTS["snip_min_lines"]
                ),
                summarize_keep_recent_turns=context_config.get(
                    "summarize_keep_recent_turns",
                    DEFAULTS["summarize_keep_recent_turns"],
                ),
                token_fudge_factor=context_config.get(
                    "token_fudge_factor", DEFAULTS["token_fudge_factor"]
                ),
            ),
            memory=MemoryConfig.from_dict(
                memory_config if isinstance(memory_config, dict) else {}
            ),
            remote_exec=RemoteExecConfig(
                enabled=bool(remote_exec_config.get("enabled", False)),
                host_mode=bool(remote_exec_config.get("host_mode", False)),
                relay_bind=str(remote_exec_config.get("relay_bind", "127.0.0.1:8765")),
                bootstrap_token_ttl_sec=int(
                    remote_exec_config.get("bootstrap_token_ttl_sec", 300)
                ),
                peer_token_ttl_sec=int(
                    remote_exec_config.get("peer_token_ttl_sec", 3600)
                ),
                heartbeat_interval_sec=int(
                    remote_exec_config.get("heartbeat_interval_sec", 10)
                ),
                heartbeat_timeout_sec=int(
                    remote_exec_config.get("heartbeat_timeout_sec", 30)
                ),
                default_tool_timeout_sec=int(
                    remote_exec_config.get("default_tool_timeout_sec", 30)
                ),
                shell_timeout_sec=int(remote_exec_config.get("shell_timeout_sec", 120)),
            ),
            lsp=lsp_config if isinstance(lsp_config, dict) else {},
            auth=AuthConfig.from_dict(
                auth_config if isinstance(auth_config, dict) else {}
            ),
            agent_registry=agent_registry,
            runtime_profiles=runtime_profiles,
            run_limits=run_limits,
            capability_packages=capability_packages,
            capability_components=capability_components,
            persistence=PersistenceConfig.from_dict(
                persistence_config if isinstance(persistence_config, dict) else {}
            ),
            diagnostics=diagnostics,
            sandbox_provider=SandboxProviderConfig.from_dict(
                sandbox_provider_config
                if isinstance(sandbox_provider_config, dict)
                else {}
            ),
            github=GitHubConfig.from_dict(
                github_config if isinstance(github_config, dict) else {}
            ),
            environment=EnvironmentConfig(cli_tools=cli_tools, skills=skills),
            session_auto_save=session_config.get(
                "auto_save", DEFAULTS["session_auto_save"]
            ),
            session_dir=session_config.get("dir"),
            history_file=cli_config.get("history_file"),
            llm_trace_authoritative=diagnostics_has_llm_trace,
        )

    def _is_workspace_bootstrapped(self, workspace_data: dict) -> bool:
        """Check whether workspace has been bootstrapped."""
        if not isinstance(workspace_data, dict):
            return False
        meta = workspace_data.get("meta")
        return isinstance(meta, dict) and bool(meta.get("workspace_bootstrapped"))

    @staticmethod
    def _is_example_config(global_data: dict) -> bool:
        """Check whether the global config is the unedited example template."""
        if not isinstance(global_data, dict):
            return False
        meta = global_data.get("meta")
        return isinstance(meta, dict) and bool(meta.get("example"))

    @staticmethod
    def _is_remote_host_mode_config(data: dict) -> bool:
        """Return true when config explicitly starts a remote host control plane."""
        if not isinstance(data, dict):
            return False
        remote_exec = data.get("remote_exec")
        if not isinstance(remote_exec, dict):
            return False
        return bool(remote_exec.get("enabled")) and bool(remote_exec.get("host_mode"))

    def _generate_example_global_config(self) -> None:
        """Generate an example config at the global config path."""
        example = {
            "meta": {"example": True},
            "providers": {"items": {}},
            "models": {
                "profiles": {}
            },
            "modes": {
                "active": "coder",
                "profiles": {
                    name: {
                        "description": m["description"],
                        "tools": list(m["tools"]),
                        "prompt_append": m["prompt_append"],
                    }
                    for name, m in sorted(BUILTIN_MODES.items())
                },
            },
            "approval": {
                "default_mode": "require_approval",
                "rules": [
                    {"tool_name": "read_file", "action": "allow"},
                    {"tool_name": "glob", "action": "allow"},
                    {"tool_name": "grep", "action": "allow"},
                    {"tool_name": "list_file", "action": "allow"},
                    {"tool_name": "write_file", "action": "require_approval"},
                    {"tool_name": "edit_file", "action": "require_approval"},
                    {"tool_name": "shell", "action": "require_approval"},
                    {"tool_name": "delegate_agent", "action": "require_approval"},
                    {"tool_source": "mcp", "action": "require_approval"},
                ],
            },
            "run_limits": {
                "max_running_agents": 4,
                "max_shells_per_agent": 1,
            },
            "runtime_profiles": {
                "environment_local": {
                    "executor": "reuleauxcoder",
                    "execution_location": "local_workspace",
                    "runtime_home_policy": "per_task",
                    "approval_mode": "full",
                },
                "capability_packager_local": {
                    "executor": "reuleauxcoder",
                    "execution_location": "local_workspace",
                    "runtime_home_policy": "per_task",
                    "approval_mode": "full",
                }
            },
            "agent_registry": {
                "agents": {
                    DEFAULT_MAIN_CHAT_AGENT_ID: DEFAULT_MAIN_CHAT_AGENT,
                    "environment_configurator": {
                        "name": "Environment Configurator",
                        "description": "Checks and configures the local workspace environment from the server manifest.",
                        "role": "environment",
                        "visibility": "system",
                        "delegable": False,
                        "taskflow_eligible": False,
                        "system_flow_only": ["environment_config"],
                        "runtime_profile": "environment_local",
                        "dispatch": {
                            "profile": "Best for checking and configuring local workspace environment components from the server manifest.",
                            "examples": [
                                "Check whether required CLI, MCP, and Skill components are available.",
                                "Configure missing local tools declared by the environment manifest.",
                            ],
                            "avoid": ["General implementation and code review tasks."],
                        },
                        "capability_refs": ["environment"],
                    },
                    "capability_packager": {
                        "name": "Capability Packager",
                        "description": "Generates capability package drafts from repositories, docs, and project notes.",
                        "role": "capability_packager",
                        "visibility": "internal",
                        "delegable": False,
                        "taskflow_eligible": False,
                        "system_flow_only": ["capability_ingest"],
                        "runtime_profile": "capability_packager_local",
                        "dispatch": {
                            "profile": "Best for reading repository and documentation bundles and producing capability package drafts.",
                            "examples": [
                                "Analyze a GitHub repository README and docs to infer CLI, MCP, and Skill installation details.",
                                "Extract credentials, risks, install steps, and usage instructions for a capability package.",
                            ],
                            "avoid": ["Executing install commands."],
                        },
                        "capability_refs": ["environment"],
                    }
                },
            },
            "capability_packages": {
                DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE_ID: DEFAULT_CORE_BUILTIN_CAPABILITY_PACKAGE,
                "environment": {
                    "name": "Environment Tools",
                    "description": "Read and configure the local workspace environment manifest.",
                    "source": {"type": "builtin"},
                    "components": [],
                }
            },
            "capability_components": DEFAULT_BUILTIN_TOOL_COMPONENTS,
            "sandbox_provider": {
                "type": "docker",
                "host_base_url": "http://labrastro-host:8765",
                "worker_image": "labrastro-host:test",
                "workspace_volume_root": "ezcode-workspaces",
                "network": "",
                "idle_ttl_seconds": 3600,
            },
            "skills": {"enabled": True},
        }
        save_yaml_config(self.GLOBAL_CONFIG_PATH, example)

    def _ensure_workspace_config(
        self, workspace_data: dict, builtin_modes: dict
    ) -> None:
        """Ensure workspace config has the minimum required structure.

        Only fills in missing sections (modes). Never overwrites existing
        user-defined settings. Marks workspace_bootstrapped when done.
        """
        path = self.WORKSPACE_CONFIG_PATH
        changed = False

        # Ensure modes section exists with profiles and active
        modes = workspace_data.setdefault("modes", {})
        if not isinstance(modes, dict):
            modes = {}
            workspace_data["modes"] = modes
            changed = True

        profiles = modes.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            modes["profiles"] = {
                name: {
                    "description": m["description"],
                    "tools": list(m["tools"]),
                    "prompt_append": m["prompt_append"],
                }
                for name, m in sorted(builtin_modes.items())
            }
            changed = True

        if not isinstance(modes.get("active"), str):
            modes["active"] = DEFAULT_ACTIVE_MODE
            changed = True

        if changed:
            workspace_data.setdefault("meta", {})["workspace_bootstrapped"] = True
            save_yaml_config(path, workspace_data)

    def _backfill_workspace_modes(self, config: Config) -> None:
        """Backfill builtin mode defaults into workspace config for discoverability.

        Only runs if the workspace has not yet been bootstrapped, to avoid
        overwriting user customizations on version upgrades.
        """
        path = self.WORKSPACE_CONFIG_PATH

        try:
            workspace_data = load_yaml_config(path)
        except FileNotFoundError:
            workspace_data = {}

        if self._is_workspace_bootstrapped(workspace_data):
            return

        modes_data = workspace_data.get("modes")
        profiles_data = (
            modes_data.get("profiles") if isinstance(modes_data, dict) else None
        )
        has_active = isinstance(modes_data, dict) and isinstance(
            modes_data.get("active"), str
        )

        if isinstance(profiles_data, dict) and profiles_data and has_active:
            return

        self._ensure_workspace_config(workspace_data, BUILTIN_MODES)

    def _bootstrap_workspace_snapshot(
        self, merged_data: dict, workspace_data: dict
    ) -> None:
        """Ensure workspace has minimum structure on first run.

        Only adds missing sections; never replaces existing user configuration.
        Once bootstrapped (meta.workspace_bootstrapped is true), this is a no-op.
        """
        if self._is_workspace_bootstrapped(workspace_data):
            return

        modes_data = (
            workspace_data.get("modes") if isinstance(workspace_data, dict) else None
        )
        profiles_data = (
            modes_data.get("profiles") if isinstance(modes_data, dict) else None
        )
        has_active_mode = isinstance(modes_data, dict) and isinstance(
            modes_data.get("active"), str
        )

        needs_bootstrap = (
            not workspace_data
            or not isinstance(profiles_data, dict)
            or not profiles_data
            or not has_active_mode
        )
        if not needs_bootstrap:
            return

        self._ensure_workspace_config(workspace_data, BUILTIN_MODES)

    @classmethod
    def from_path(cls, path: Optional[Path] = None) -> Config:
        """Load an explicit config path, or layered user config when path is absent."""
        loader = cls(path)
        return loader.load()

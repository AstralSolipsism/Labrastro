"""Application runner - shared initialization logic for all interfaces.

This module provides a unified entry point that handles:
- Configuration loading
- LLM client initialization
- Agent setup with hooks and tools
- MCP server management
- Session management

Different interfaces (CLI, TUI, VSCode extension) can reuse this logic
and only need to implement their own UI-specific rendering.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from reuleauxcoder.app.runtime.session_state import (
    get_session_fingerprint,
    restore_config_runtime_defaults,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import (
    Config,
    DEFAULT_MAIN_CHAT_AGENT_ID,
    resolve_agent_effective_capability_scope,
)
from reuleauxcoder.domain.hooks import (
    HookPoint,
    RunnerShutdownContext,
    RunnerStartupContext,
    SessionSaveContext,
    SessionStartContext,
    discover_hook_specs,
    instantiate_hooks,
)
from reuleauxcoder.domain.memory.runtime import bind_memory_scope_to_agent
from reuleauxcoder.extensions.mcp.manager import MCPManager
from labrastro_server.adapters.reuleauxcoder.remote_backend import RemoteRelayToolBackend
from labrastro_server.interfaces.http.remote.service import RemoteRelayHTTPService
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.interfaces.entrypoint.dependencies import (
    AppContext,
    AppDependencies,
    AppOptions,
    _default_create_remote_artifact_provider,
)
from reuleauxcoder.interfaces.entrypoint.remote_relay import (
    bind_remote_chat_handler,
    init_remote_relay,
)
from reuleauxcoder.interfaces.entrypoint.session_lifecycle import restore_session
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.services.config.loader import ConfigValidationError
from reuleauxcoder.services.llm.client import LLM


def build_capability_catalog(config: Config, agent_id: str = "") -> str:
    if agent_id:
        scope = resolve_agent_effective_capability_scope(config, agent_id)
        if scope.found:
            return scope.capability_catalog
    lines: list[str] = []
    components = config.capability_components
    for package_id, package in sorted(config.capability_packages.items()):
        if not package.enabled or package_id == "environment":
            continue
        title = package.name or package_id
        description = f" - {package.description}" if package.description else ""
        lines.append(f"- `{package_id}`: {title}{description}")
        for component_id in package.components:
            component = components.get(component_id)
            if component is None or not component.enabled:
                continue
            details = _capability_component_details(component.config)
            suffix = f" ({details})" if details else ""
            lines.append(
                f"  - `{component.id}` [{component.kind}] {component.name}{suffix}"
            )
        if package.usage:
            lines.append("  - usage: " + " | ".join(package.usage[:3]))
    return "\n".join(lines)


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


class AppRunner:
    """Application runner that handles initialization and cleanup."""

    def __init__(
        self,
        options: AppOptions | None = None,
        dependencies: AppDependencies | None = None,
    ):
        self.options = options or AppOptions()
        self.dependencies = dependencies or AppDependencies()
        self._mcp_manager: MCPManager | None = None
        self._relay_server: RelayServer | None = None
        self._relay_http_service: RemoteRelayHTTPService | None = None
        self._lsp_manager: LspManager | None = None

    def initialize(self) -> AppContext:
        """Initialize all application components and return context."""
        config = self.dependencies.load_config(self.options.config_path)
        setattr(config, "_source_path", self.options.config_path)
        validation_errors = config.validate()
        if validation_errors:
            raise ConfigValidationError(validation_errors)
        if self.options.server_mode:
            config.remote_exec.enabled = True
            config.remote_exec.host_mode = True
        ui_bus = self.dependencies.create_ui_bus()
        action_registry = self.dependencies.create_action_registry()
        self._init_remote_relay(config, ui_bus)
        config, ui_bus, llm, agent = self._build_core(config, ui_bus)
        self._bind_remote_chat_handler(agent)
        skills_service = self._init_skills(config, agent, ui_bus)
        mcp_manager = self._attach_mcp_if_configured(config, agent, ui_bus)
        sessions_dir = Path(config.session_dir) if config.session_dir else None
        if self.options.server_mode:
            restore_config_runtime_defaults(config, agent)
            current_session_id, session_exit_time = None, None
        else:
            current_session_id, session_exit_time, sessions_dir = self._restore_session(
                config, agent, ui_bus
            )

        app_ctx = AppContext(
            config=config,
            llm=llm,
            agent=agent,
            ui_bus=ui_bus,
            ui_interactor=None,
            mcp_manager=mcp_manager,
            skills_service=skills_service,
            action_registry=action_registry,
            current_session_id=current_session_id,
            session_exit_time=session_exit_time,
            sessions_dir=sessions_dir,
        )
        self._run_lifecycle_hooks(
            agent,
            HookPoint.RUNNER_STARTUP,
            RunnerStartupContext(
                hook_point=HookPoint.RUNNER_STARTUP,
                metadata={"ui_bus": ui_bus},
            ),
        )
        self._run_lifecycle_hooks(
            agent,
            HookPoint.SESSION_START,
            SessionStartContext(
                hook_point=HookPoint.SESSION_START,
                session_id=current_session_id,
                metadata={"ui_bus": ui_bus},
            ),
        )
        return app_ctx

    def _build_core(
        self,
        config: Config,
        ui_bus: UIEventBus,
    ) -> tuple[Config, UIEventBus, LLM, Agent]:
        """Build config + ui bus + llm + agent, with runtime hooks initialized."""
        if self.options.model:
            profiles = getattr(config, "model_profiles", {}) or {}
            if self.options.model not in profiles:
                raise ConfigValidationError(
                    [f"model profile '{self.options.model}' does not exist"]
                )
            config.active_main_model_profile = self.options.model

        llm = self.dependencies.create_llm(config)
        llm.ui_bus = ui_bus
        tool_backend = self.dependencies.create_tool_backend(config, ui_bus)
        if self._relay_server is not None:
            tool_backend = RemoteRelayToolBackend(
                relay_server=self._relay_server, ui_bus=ui_bus
            )
        tools = self.dependencies.load_tools(tool_backend)
        agent = self.dependencies.create_agent(llm, tools, config)
        main_agent_id = _agent_config_id(config, agent)
        setattr(agent, "runtime_config", config)
        if main_agent_id:
            setattr(agent, "agent_config_id", main_agent_id)
            setattr(agent, "main_agent_id", main_agent_id)
            scope = resolve_agent_effective_capability_scope(config, main_agent_id)
            if scope.found:
                setattr(agent, "effective_capabilities", scope.effective_capabilities)
                setattr(agent, "enforce_effective_capabilities", True)
        setattr(agent, "capability_catalog", build_capability_catalog(config, main_agent_id))
        setattr(agent, "current_session_id", None)
        setattr(agent, "session_fingerprint", get_session_fingerprint(config, agent))
        bind_memory_scope_to_agent(
            agent,
            owner_agent_id=getattr(config.memory, "default_agent_id", "core"),
            memory_namespace=getattr(config.memory, "default_namespace", "") or None,
            workspace_id=str(Path.cwd()),
        )
        agent.context._ui_bus = ui_bus

        self._register_hooks(agent, config)
        self._init_lsp(config, agent, ui_bus)
        self._wire_agent_tool_parent(agent)
        return config, ui_bus, llm, agent

    def _init_remote_relay(self, config: Config, ui_bus: UIEventBus) -> None:
        init_remote_relay(self, config, ui_bus)

    def _bind_remote_chat_handler(self, agent: Agent) -> None:
        bind_remote_chat_handler(self, agent)

    @staticmethod
    def build_capability_catalog(config: Config, agent_id: str = "") -> str:
        return build_capability_catalog(config, agent_id)

    def _register_hooks(self, agent: Agent, config: Config) -> None:
        """Register hooks discovered via decorator mechanism."""
        specs = discover_hook_specs()
        hooks = instantiate_hooks(specs, config)
        for hook_point, hook in hooks:
            agent.register_hook(hook_point, hook)

    def _init_lsp(self, config: Config, agent: Agent, ui_bus: UIEventBus) -> None:
        """Initialize host-side LSP for local execution."""
        lsp_config = LspConfig.from_config(config)
        if not lsp_config.enabled:
            self._set_lsp_tool_manager(None)
            return
        if self._relay_server is not None:
            self._set_lsp_tool_manager(None)
            ui_bus.info(
                "LSP: remote execution will use peer-side LSP when the peer supports it.",
                kind=UIEventKind.SYSTEM,
            )
            return

        manager = LspManager(lsp_config, workspace_cwd=Path.cwd())
        report = manager.health_check()
        if report.available == 0:
            ui_bus.info(
                "LSP: no language servers found on PATH. Install pyright, rust-analyzer, gopls, etc. for diagnostics.",
                kind=UIEventKind.SYSTEM,
            )
        else:
            ready = ", ".join(
                status.language for status in report.statuses if status.available
            )
            ui_bus.info(
                f"LSP: {report.available}/{report.total} language servers ready: {ready}",
                kind=UIEventKind.SYSTEM,
            )

        manager.start_worker()
        self._lsp_manager = manager
        setattr(agent, "lsp_manager", manager)
        for hooks in agent.hook_registry._hooks.values():
            for hook in hooks:
                setter = getattr(hook, "set_lsp_manager", None)
                if callable(setter):
                    setter(manager)
        self._set_lsp_tool_manager(manager)

    @staticmethod
    def _wire_agent_tool_parent(agent: Agent) -> None:
        """Inject parent agent into the delegation tool if present."""
        for tool in agent.tools:
            if tool.name == "delegate_agent":
                tool._parent_agent = agent

    def _attach_mcp_if_configured(
        self,
        config: Config,
        agent: Agent,
        ui_bus: UIEventBus,
    ) -> MCPManager | None:
        """Initialize and attach MCP runtime if servers are configured."""
        mcp_manager = None
        scope = _agent_capability_scope(config, agent)
        mcp_servers = scope.mcp_servers if scope is not None else config.mcp_servers
        server_mcp_servers = [
            server
            for server in mcp_servers
            if getattr(server, "placement", "server") in {"server", "both"}
        ]
        if server_mcp_servers:
            mcp_manager = self._init_mcp(server_mcp_servers, agent, ui_bus)
        setattr(agent, "mcp_manager", mcp_manager)
        return mcp_manager

    def _init_skills(
        self, config: Config, agent: Agent, ui_bus: UIEventBus
    ) -> SkillsService:
        """Initialize skills service and attach stable catalog to the agent."""
        scope = _agent_capability_scope(config, agent)
        skills_config = scope.skills if scope is not None else config.skills
        environment_requirements = (
            scope.environment.requirements
            if scope is not None
            else config.environment.requirements
        )
        environment_skill_paths = [
            Path(str(requirement.path)).expanduser()
            for requirement in environment_requirements.values()
            if getattr(requirement, "enabled", True)
            and getattr(requirement, "kind", "") == "path"
            and "skill_root" in set(getattr(requirement, "tags", []) or [])
            and str(requirement.path or "").strip()
        ]
        registered_skill_paths = [
            Path(path).expanduser()
            for skill in skills_config.items.values()
            if getattr(skill, "enabled", True)
            for path in [
                str(getattr(skill, "path_hint", "") or getattr(skill, "source_path", ""))
            ]
            if path.strip()
        ]
        skills_service = SkillsService(
            workspace_dir=Path.cwd(),
            home_dir=Path.home(),
            enabled=skills_config.enabled,
            scan_project=skills_config.scan_project,
            scan_user=skills_config.scan_user,
            disabled_names=list(skills_config.disabled),
            extra_paths=[*environment_skill_paths, *registered_skill_paths],
        )
        reload_result = skills_service.reload()
        setattr(agent, "skills_service", skills_service)
        setattr(agent, "skills_catalog", reload_result.catalog)

        if not skills_config.enabled:
            ui_bus.info("Skills disabled by config.", kind=UIEventKind.SYSTEM)
            return skills_service

        ui_bus.info(
            f"Skills loaded: {len(reload_result.all_skills)} discovered, {len(reload_result.active_skills)} active.",
            kind=UIEventKind.SYSTEM,
        )
        if reload_result.added:
            ui_bus.info(
                "Skills added: " + ", ".join(reload_result.added),
                kind=UIEventKind.SYSTEM,
            )
        for name in reload_result.removed:
            ui_bus.warning(f"Skill removed: {name}", kind=UIEventKind.SYSTEM)
        for name in reload_result.missing:
            ui_bus.warning(
                f"Skill not found and skipped: {name}", kind=UIEventKind.SYSTEM
            )
        for diagnostic in reload_result.diagnostics:
            emit = ui_bus.warning if diagnostic.level == "warning" else ui_bus.error
            emit(diagnostic.message, kind=UIEventKind.SYSTEM)
        return skills_service

    def _restore_session(
        self,
        config: Config,
        agent: Agent,
        ui_bus: UIEventBus,
    ) -> tuple[str | None, str | None, Path | None]:
        return restore_session(self.options, self.dependencies, config, agent, ui_bus)

    def cleanup(self, agent: Agent | None = None) -> None:
        """Clean up resources (MCP connections, remote relay, etc.)."""
        if agent is not None:
            self._run_lifecycle_hooks(
                agent,
                HookPoint.RUNNER_SHUTDOWN,
                RunnerShutdownContext(hook_point=HookPoint.RUNNER_SHUTDOWN),
            )
        if self._relay_http_service is not None:
            artifact_provider = getattr(
                self._relay_http_service, "artifact_provider", None
            )
            build_dir = (
                getattr(artifact_provider, "_build_dir", None)
                if artifact_provider is not None
                else None
            )
            self._relay_http_service.stop()
            self._relay_http_service = None
            if isinstance(build_dir, Path):
                shutil.rmtree(build_dir, ignore_errors=True)
        if self._relay_server is not None:
            for peer in self._relay_server.registry.list_online():
                try:
                    self._relay_server.request_cleanup(peer.peer_id, timeout_sec=5)
                except Exception:
                    pass
            self._relay_server.stop()
            self._relay_server = None
        if self._mcp_manager:
            self._mcp_manager.disconnect_all()
            self._mcp_manager.stop()
            self._mcp_manager = None
        if self._lsp_manager:
            self._lsp_manager.shutdown_all()
            self._lsp_manager = None
            self._set_lsp_tool_manager(None)

    @staticmethod
    def _set_lsp_tool_manager(manager: LspManager | None) -> None:
        try:
            from reuleauxcoder.extensions.tools.builtin.lsp import (
                set_lsp_manager as set_lsp_tool_manager,
            )

            set_lsp_tool_manager(manager)
        except Exception:
            pass

    @staticmethod
    def _run_lifecycle_hooks(
        agent: Agent,
        hook_point: HookPoint,
        context: RunnerStartupContext
        | RunnerShutdownContext
        | SessionStartContext
        | SessionSaveContext,
    ) -> None:
        """Run hooks for a lifecycle event without mutating control flow."""
        for decision in agent.hook_registry.run_guards(hook_point, context):
            if not decision.allowed:
                break
        agent.hook_registry.run_transforms(hook_point, context)
        agent.hook_registry.run_observers(hook_point, context)

    def _init_mcp(
        self, mcp_servers: list[Any], agent: Agent, ui_bus: UIEventBus
    ) -> MCPManager:
        """Initialize MCP manager and connect to servers."""
        manager = self.dependencies.create_mcp_manager(ui_bus)
        manager.start()

        enabled_servers = [s for s in mcp_servers if getattr(s, "enabled", True)]
        for server_config in enabled_servers:
            success = manager.connect_server(server_config)
            if not success:
                ui_bus.warning(
                    f"Warning: Failed to connect to MCP server '{server_config.name}'",
                    kind=UIEventKind.MCP,
                )

        if manager.tools:
            agent.add_tools(manager.tools)
            ui_bus.success(
                f"Loaded {len(manager.tools)} MCP tools from {len(enabled_servers)} enabled server(s)",
                kind=UIEventKind.MCP,
            )

        self._mcp_manager = manager
        return manager


def _agent_config_id(config: Config, agent: Any) -> str:
    for attr_name in ("agent_config_id", "main_agent_id"):
        value = str(getattr(agent, attr_name, "") or "").strip()
        if value and value in config.agent_registry.agents:
            return value
    if DEFAULT_MAIN_CHAT_AGENT_ID in config.agent_registry.agents:
        return DEFAULT_MAIN_CHAT_AGENT_ID
    return ""


def _agent_capability_scope(config: Config, agent: Any):
    agent_id = _agent_config_id(config, agent)
    if not agent_id:
        return None
    scope = resolve_agent_effective_capability_scope(config, agent_id)
    return scope if scope.found else None

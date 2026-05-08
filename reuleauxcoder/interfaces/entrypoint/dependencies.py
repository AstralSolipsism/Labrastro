"""Dependency providers and context models for the shared app runner."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Any, Callable

from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.mcp.manager import MCPManager
from labrastro_server.interfaces.http.remote.service import RemoteRelayHTTPService
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.registry import build_tools
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.interfaces.interactions import UIInteractor
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from labrastro_server.infrastructure.persistence.factory import (
    create_auth_store,
    create_github_pull_request_service,
    create_issue_assignment_service,
    create_persistence_maintenance_service,
    create_runtime_control_plane,
    create_session_store as create_configured_session_store,
    create_taskflow_service,
)
from reuleauxcoder.services.config.loader import ConfigLoader
from labrastro_server.services.agent_runtime.control_plane import AgentRuntimeControlPlane
from labrastro_server.services.auth.service import AuthService, validate_auth_config
from reuleauxcoder.services.llm.client import LLM
from reuleauxcoder.services.llm.factory import build_llm_from_settings


def _default_load_config(path: Path | None) -> Config:
    return ConfigLoader.from_path(path)


def _default_create_llm(config: Config) -> LLM:
    return build_llm_from_settings(
        config,
        debug_trace=getattr(config, "llm_debug_trace", False),
    )


def _default_create_tool_backend(config: Config, ui_bus: UIEventBus) -> ToolBackend:
    return LocalToolBackend()


def _default_load_tools(tool_backend: ToolBackend) -> list[Any]:
    return build_tools(tool_backend)


def _default_create_agent(llm: LLM, tools: list[Any], config: Config) -> Agent:
    return Agent(
        llm=llm,
        tools=tools,
        max_context_tokens=config.max_context_tokens,
        available_modes=getattr(config, "modes", {}) or {},
        active_mode=getattr(config, "active_mode", None),
    )


def _default_create_session_store(sessions_dir: Path | None) -> SessionStore:
    return SessionStore(sessions_dir)


def _default_create_configured_session_store(
    config: Config, sessions_dir: Path | None
):
    return create_configured_session_store(config, sessions_dir)


def _default_create_mcp_manager(ui_bus: UIEventBus) -> MCPManager:
    return MCPManager(ui_bus=ui_bus)


def _default_create_action_registry() -> ActionRegistry:
    return create_builtin_action_registry()


def _default_create_remote_relay_server(config: Config) -> RelayServer | None:
    if not getattr(config, "remote_exec", None) or not config.remote_exec.enabled:
        return None
    if not config.remote_exec.host_mode:
        return None
    return RelayServer(
        heartbeat_interval_sec=config.remote_exec.heartbeat_interval_sec,
        heartbeat_timeout_sec=config.remote_exec.heartbeat_timeout_sec,
        default_tool_timeout_sec=config.remote_exec.default_tool_timeout_sec,
        shell_timeout_sec=config.remote_exec.shell_timeout_sec,
        peer_token_ttl_sec=config.remote_exec.peer_token_ttl_sec,
    )


def _default_create_remote_artifact_provider(
    ui_bus: UIEventBus,
) -> Callable[[str, str, str], tuple[bytes, str] | None]:
    repo_root = Path(__file__).resolve().parents[3]
    agent_dir = repo_root / "reuleauxcoder-agent"
    artifact_root = repo_root / "artifacts" / "remote"
    build_dir = Path(tempfile.mkdtemp(prefix="rcoder-peer-build-"))
    build_lock = threading.Lock()
    cache: dict[tuple[str, str], Path] = {}

    def _find_prebuilt_binary(
        os_name: str, arch: str, artifact_name: str
    ) -> Path | None:
        output_name = artifact_name + (".exe" if os_name == "windows" else "")
        candidates = [
            artifact_root / os_name / arch / output_name,
            artifact_root / f"{os_name}-{arch}" / output_name,
            repo_root / output_name,
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        return None

    def provide(
        os_name: str, arch: str, artifact_name: str
    ) -> tuple[bytes, str] | None:
        if artifact_name != "rcoder-peer":
            return None
        if os_name not in {"linux", "darwin", "windows"}:
            return None
        if arch not in {"amd64", "arm64"}:
            return None

        prebuilt_binary = _find_prebuilt_binary(os_name, arch, artifact_name)
        if prebuilt_binary is not None:
            ui_bus.info(
                f"Using prebuilt peer artifact for {os_name}/{arch}: {prebuilt_binary.name}",
                kind=UIEventKind.REMOTE,
                os=os_name,
                arch=arch,
                source="prebuilt",
            )
            return prebuilt_binary.read_bytes(), "application/octet-stream"

        if shutil.which("go") is None:
            raise RuntimeError(
                "peer artifact unavailable: no prebuilt binary found and local 'go' toolchain is not installed"
            )

        target_key = (os_name, arch)
        with build_lock:
            binary_path = cache.get(target_key)
            if binary_path is None or not binary_path.exists():
                output_name = artifact_name + (".exe" if os_name == "windows" else "")
                binary_path = build_dir / f"{os_name}-{arch}-{output_name}"
                env = dict(os.environ)
                env["GOOS"] = os_name
                env["GOARCH"] = arch
                subprocess.run(
                    [
                        "go",
                        "build",
                        "-o",
                        str(binary_path),
                        "./cmd/reuleauxcoder-agent",
                    ],
                    cwd=agent_dir,
                    env=env,
                    check=True,
                    timeout=180,
                )
                cache[target_key] = binary_path
                ui_bus.info(
                    f"Built peer artifact for {os_name}/{arch}: {binary_path.name}",
                    kind=UIEventKind.REMOTE,
                    os=os_name,
                    arch=arch,
                    source="built",
                )
        return binary_path.read_bytes(), "application/octet-stream"

    setattr(provide, "_build_dir", build_dir)
    setattr(provide, "_artifact_root", artifact_root)
    return provide


def _default_create_remote_http_service(
    config: Config,
    relay_server: RelayServer,
    ui_bus: UIEventBus,
) -> RemoteRelayHTTPService | None:
    if not getattr(config, "remote_exec", None) or not config.remote_exec.enabled:
        return None
    if not config.remote_exec.host_mode:
        return None
    runtime_control_plane = create_runtime_control_plane(config)
    taskflow_service = create_taskflow_service(
        config, runtime_control_plane=runtime_control_plane
    )
    issue_assignment_service = create_issue_assignment_service(
        config, taskflow_service=taskflow_service
    )
    github_pr_service = create_github_pull_request_service(
        config,
        runtime_control_plane=runtime_control_plane,
        issue_assignment_service=issue_assignment_service,
    )
    persistence_maintenance_service = create_persistence_maintenance_service(config)
    auth_errors = []
    if not config.auth.enabled:
        auth_errors.append("auth.enabled is required for remote host mode")
    auth_errors.extend(validate_auth_config(config.auth))
    if auth_errors:
        raise ValueError("; ".join(auth_errors))
    auth_service = AuthService(
        config.auth,
        create_auth_store(config),
        issue_bootstrap_token=lambda ttl: relay_server.issue_bootstrap_token(ttl_sec=ttl),
    )
    return RemoteRelayHTTPService(
        relay_server=relay_server,
        bind=config.remote_exec.relay_bind,
        ui_bus=ui_bus,
        artifact_provider=_default_create_remote_artifact_provider(ui_bus),
        auth_service=auth_service,
        bootstrap_token_ttl_sec=config.remote_exec.bootstrap_token_ttl_sec,
        mcp_servers=config.mcp_servers,
        mcp_artifact_root=config.mcp_artifact_root,
        environment_cli_tools=config.environment.cli_tools,
        environment_skills=config.environment.skills,
        admin_config_path=getattr(config, "_source_path", None),
        runtime_control_plane=runtime_control_plane,
        taskflow_service=taskflow_service,
        issue_assignment_service=issue_assignment_service,
        github_pr_service=github_pr_service,
        persistence_maintenance_service=persistence_maintenance_service,
    )


@dataclass
class AppDependencies:
    """Lightweight dependency providers for AppRunner."""

    load_config: Callable[[Path | None], Config] = _default_load_config
    create_ui_bus: Callable[[], UIEventBus] = UIEventBus
    create_llm: Callable[[Config], LLM] = _default_create_llm
    create_tool_backend: Callable[[Config, UIEventBus], ToolBackend] = (
        _default_create_tool_backend
    )
    load_tools: Callable[[ToolBackend], list[Any]] = _default_load_tools
    create_agent: Callable[[LLM, list[Any], Config], Agent] = _default_create_agent
    create_session_store: Callable[[Path | None], SessionStore] = (
        _default_create_session_store
    )
    create_configured_session_store: Callable[[Config, Path | None], Any] = (
        _default_create_configured_session_store
    )
    create_mcp_manager: Callable[[UIEventBus], MCPManager] = _default_create_mcp_manager
    create_action_registry: Callable[[], ActionRegistry] = (
        _default_create_action_registry
    )
    create_remote_relay_server: Callable[[Config], RelayServer | None] = (
        _default_create_remote_relay_server
    )
    create_remote_http_service: Callable[
        [Config, RelayServer, UIEventBus], RemoteRelayHTTPService | None
    ] = _default_create_remote_http_service


@dataclass
class AppContext:
    """Context object containing all initialized application components."""

    config: Config
    llm: LLM
    agent: Agent
    ui_bus: UIEventBus
    ui_interactor: UIInteractor | None = None
    mcp_manager: MCPManager | None = None
    skills_service: SkillsService | None = None
    action_registry: ActionRegistry | None = None
    current_session_id: str | None = None
    session_exit_time: str | None = None
    sessions_dir: Path | None = None


@dataclass
class AppOptions:
    """Options for application initialization."""

    config_path: Path | None = None
    model: str | None = None
    resume_session_id: str | None = None
    auto_resume_latest: bool = True
    server_mode: bool = False

"""Remote relay bootstrap and peer chat binding helpers."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import inspect
from pathlib import Path
import time
import uuid
from typing import Any, Callable

from rich.console import Console

from reuleauxcoder.app.runtime.session_state import (
    apply_agent_default_model,
    apply_agent_config_model,
    apply_session_runtime_state,
    apply_session_model_override,
    build_session_runtime_state,
    get_session_fingerprint,
    restore_config_runtime_defaults,
)
from reuleauxcoder.app.runtime.agent_runtime import (
    AgentRunCancelled,
    get_interactive_run_limiter,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.memory.runtime import (
    bind_main_chat_memory_scope_to_agent,
    bind_memory_scope_to_agent,
)
from reuleauxcoder.domain.session.locale import session_locale_prompt_append
from reuleauxcoder.domain.agent.events import (
    AgentEvent,
    AgentEventType,
    ToolFailureKind,
)
from reuleauxcoder.domain.agent.runtime_boundary import runtime_agent_run_id
from reuleauxcoder.domain.agent.tool_diagnostics import (
    ToolDiagnostic,
    ToolDiagnosticKind,
    ToolDiagnosticStage,
    diagnostic_to_dict,
    tool_diagnostic_from_failure,
)
from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
    PendingApproval,
)
from reuleauxcoder.domain.config.models import (
    Config,
    DEFAULT_MAIN_CHAT_AGENT_ID,
    PromptConfig,
    build_agent_run_snapshot,
    resolve_agent_environment_requirement_scope_ids,
    resolve_agent_effective_capability_scope,
)
from reuleauxcoder.domain.hooks.lifecycle import (
    LifecycleHookDispatcher,
    bind_lifecycle_dispatcher_to_hook_registry,
    bind_lifecycle_runtime_adapters_to_agent,
    build_lifecycle_event_context,
    default_lifecycle_hook_runtime_adapters,
    lifecycle_registry_from_config,
    system_builtin_lifecycle_declarations_from_hook_registry,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.session.models import Session, SessionMetadata, SessionRuntimeState
from reuleauxcoder.domain.session.document import settle_orphaned_running_session_run
from labrastro_server.adapters.reuleauxcoder.remote_backend import RemoteRelayToolBackend
from labrastro_server.adapters.reuleauxcoder.mcp_tools import RemotePeerMCPTool
from labrastro_server.interfaces.http.remote.protocol import (
    ChatCommandDispatchRequest,
    ChatCommandDispatchResponse,
    ToolPreviewResult,
)
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.extensions.tools.backend import ExecutionContext
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.entrypoint.dependencies import AppDependencies
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.interfaces.vscode.registration import VSCODE_CHAT_PROFILE
from reuleauxcoder.services.llm.factory import llm_trace_enabled, resolve_model_runtime
from reuleauxcoder.services.llm.diagnostics import persist_tool_diagnostic_event
from reuleauxcoder.services.providers.diagnostics import provider_error_envelope
from labrastro_server.taskflow.application.taskflow_service import (
    TASKFLOW_SYSTEM_PROMPT,
    TASKFLOW_WORKFLOW_MODE,
)

REMOTE_PREVIEW_TOOLS = {"apply_patch", "draft_document_commit"}


class RemoteToolProtocolError(RuntimeError):
    """Protocol boundary error for remote peer tool lifecycle events."""

    def __init__(
        self,
        *,
        tool_name: str,
        tool_call_id: str | None,
        code: str,
        message: str,
    ):
        super().__init__(message)
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.code = code
        self.message = message

    def payload(self) -> dict[str, Any]:
        diagnostic = tool_diagnostic_from_failure(
            stage=ToolDiagnosticStage.PROTOCOL,
            kind=ToolDiagnosticKind.TOOL_PROTOCOL_ERROR,
            code=self.code,
            message=self.message,
            tool_name=self.tool_name,
            tool_call_id=self.tool_call_id,
        )
        return {
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "code": self.code,
            "message": self.message,
            "failure_kind": ToolFailureKind.TOOL_PROTOCOL_ERROR.value,
            "tool_diagnostics": [diagnostic.to_dict()],
        }


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256-{hashlib.sha256(encoded).hexdigest()}"


def _rebuild_agent_lifecycle_dispatcher(agent: Agent, config: Any) -> LifecycleHookDispatcher:
    hook_registry = getattr(agent, "hook_registry", None)
    if hook_registry is None:
        hook_registry = HookRegistry()
        setattr(agent, "hook_registry", hook_registry)
    dispatcher = LifecycleHookDispatcher(
        lifecycle_registry_from_config(config),
        runtime_adapters=default_lifecycle_hook_runtime_adapters(
            hook_registry=hook_registry,
            prompt_llm=getattr(agent, "llm", None),
        ),
    )
    existing_ids = {
        str(getattr(item, "id", "") or "")
        for item in getattr(dispatcher.registry, "query", lambda: [])()
    }
    for declaration in system_builtin_lifecycle_declarations_from_hook_registry(
        hook_registry
    ):
        if declaration.id in existing_ids:
            continue
        dispatcher.registry.register(declaration)
        existing_ids.add(declaration.id)
    setattr(agent, "lifecycle_dispatcher", dispatcher)
    bind_lifecycle_runtime_adapters_to_agent(agent)
    bind_lifecycle_dispatcher_to_hook_registry(hook_registry, dispatcher)
    return dispatcher


def _tool_arguments_preview(value: Any, limit: int = 240) -> str:
    text = str(value or "").replace("\x00", "\uFFFD")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def init_remote_relay(runner, config: Config, ui_bus: UIEventBus) -> None:
    """Initialize remote relay server if enabled and host_mode."""
    try:
        relay = runner.dependencies.create_remote_relay_server(config)
    except Exception as exc:
        ui_bus.warning(
            f"Remote relay initialization failed: {exc}", kind=UIEventKind.REMOTE
        )
        return
    if relay is None:
        return
    try:
        relay.start()
        runner._relay_server = relay
    except Exception as exc:
        ui_bus.warning(
            f"Remote relay server failed to start: {exc}", kind=UIEventKind.REMOTE
        )
        return

    try:
        http_service = runner.dependencies.create_remote_http_service(
            config, relay, ui_bus
        )
    except Exception as exc:
        relay.stop()
        runner._relay_server = None
        if config.remote_exec.enabled and config.remote_exec.host_mode:
            raise
        ui_bus.warning(
            f"Remote relay HTTP service initialization failed: {exc}",
            kind=UIEventKind.REMOTE,
        )
        return

    if http_service is not None:
        try:
            http_service.start()
            runner._relay_http_service = http_service
        except Exception as exc:
            relay.stop()
            runner._relay_server = None
            runner._relay_http_service = None
            ui_bus.warning(
                f"Remote relay HTTP service failed to start: {exc}",
                kind=UIEventKind.REMOTE,
            )
            return

    ui_bus.success(
        "Remote relay server started.",
        kind=UIEventKind.REMOTE,
        bind=getattr(config.remote_exec, "relay_bind", None),
        base_url=runner._relay_http_service.base_url
        if runner._relay_http_service
        else None,
    )


def remote_session_metadata_payload(
    session: Session | SessionMetadata,
) -> dict[str, Any]:
    preview = (
        session.preview
        if isinstance(session, SessionMetadata)
        else session.get_preview()
    )
    return {
        "id": session.id,
        "model": session.model,
        "saved_at": session.saved_at,
        "preview": preview,
        "fingerprint": session.fingerprint,
    }


def active_model_payload(
    provider_id: str,
    model_id: str,
    parameters: dict[str, Any] | None = None,
    display_name: str = "",
) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "provider": provider_id,
        "model_id": model_id,
        "model": model_id,
        "display_name": display_name or model_id,
        **dict(parameters or {}),
    }


def _chat_locale_prompt_append(locale: str | None) -> str:
    return session_locale_prompt_append(locale)


def _runtime_config_with_chat_locale(
    config: Config | None,
    locale: str | None,
) -> Config | None:
    append = _chat_locale_prompt_append(locale)
    if config is None or not append:
        return config

    current_prompt = getattr(config, "prompt", None) or PromptConfig()
    current_append = str(getattr(current_prompt, "system_append", "") or "")
    merged_append = f"{current_append.rstrip()}\n\n{append}" if current_append.strip() else append
    return replace(
        config,
        prompt=replace(current_prompt, system_append=merged_append),
    )


def _chat_entrypoint_agent_config(config: Config | None) -> Any | None:
    if config is None:
        return None
    registry = getattr(config, "agent_registry", None)
    agents = getattr(registry, "agents", {}) if registry is not None else {}
    if not isinstance(agents, dict):
        return None
    default_agent = agents.get(DEFAULT_MAIN_CHAT_AGENT_ID)
    if getattr(default_agent, "chat_entrypoint", False):
        return default_agent
    candidates = [
        agent
        for agent in agents.values()
        if getattr(agent, "chat_entrypoint", False)
        and getattr(agent, "visibility", "user") == "user"
    ]
    candidates.sort(key=lambda item: str(getattr(item, "id", "")))
    return candidates[0] if candidates else None


def _runtime_config_with_chat_agent_prompt(
    config: Config | None,
    agent_config: Any | None,
) -> Config | None:
    if config is None or agent_config is None:
        return config
    prompt_config = getattr(agent_config, "prompt", None)
    if prompt_config is None:
        return config
    additions: list[str] = []
    agent_md = str(getattr(prompt_config, "agent_md", "") or "").strip()
    system_append = str(getattr(prompt_config, "system_append", "") or "").strip()
    if agent_md:
        additions.append(f"Agent profile for `{agent_config.id}`:\n{agent_md}")
    if system_append:
        additions.append(system_append)
    if not additions:
        return config

    current_prompt = getattr(config, "prompt", None) or PromptConfig()
    current_append = str(getattr(current_prompt, "system_append", "") or "")
    merged_append = "\n\n".join(
        part for part in [current_append.strip(), *additions] if part
    )
    return replace(
        config,
        prompt=replace(current_prompt, system_append=merged_append),
    )


def _capability_projection_for_agent(config: Config, agent_id: str) -> dict[str, Any]:
    snapshot = build_agent_run_snapshot(
        agent_registry=config.agent_registry,
        runtime_profiles=config.runtime_profiles,
        run_limits=config.run_limits,
        capability_packages=config.capability_packages,
        capability_components=config.capability_components,
    )
    agent = snapshot.get("agents", {}).get(agent_id)
    if not isinstance(agent, dict):
        return {}
    return agent


def switch_session_model(
    config: Config | None,
    session_store: Any,
    fingerprint: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if config is None:
        return {"ok": False, "error": "config_unavailable", "_status": 503}
    provider_id = str(payload.get("provider_id") or payload.get("provider") or "").strip()
    model_id = str(payload.get("model_id") or payload.get("model") or "").strip()
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}
    display_name = str(payload.get("display_name") or "").strip()
    if not provider_id or not model_id:
        return {"ok": False, "error": "provider_model_required", "_status": 400}
    provider = getattr(config, "providers", None)
    provider_item = (
        (getattr(provider, "items", {}) or {}).get(provider_id)
        if provider is not None
        else None
    )
    if provider_item is None:
        return {
            "ok": False,
            "error": "provider_not_found",
            "provider_id": provider_id,
            "_status": 404,
        }
    if getattr(provider_item, "enabled", True) is False:
        return {
            "ok": False,
            "error": "provider_disabled",
            "provider_id": provider_id,
            "_status": 409,
        }

    session_id = str(payload.get("session_id") or "").strip()
    loaded: Session | None = None
    if session_id:
        loaded = session_store.load(session_id)
        if loaded is not None and loaded.fingerprint != fingerprint:
            return {
                "ok": False,
                "error": "session_fingerprint_mismatch",
                "fingerprint": loaded.fingerprint,
                "current_fingerprint": fingerprint,
                "_status": 403,
            }
    else:
        latest = session_store.get_latest(fingerprint=fingerprint)
        if latest is not None:
            loaded = session_store.load(latest.id)
            session_id = latest.id
        else:
            session_id = session_store.generate_session_id()

    runtime_state = (
        SessionRuntimeState.from_dict(loaded.runtime_state.to_dict())
        if loaded is not None
        else SessionRuntimeState(
            model=resolve_model_runtime(config).model or None,
            active_mode=getattr(config, "active_mode", None),
            llm_debug_trace=llm_trace_enabled(config),
            active_main_model_profile=getattr(
                config, "active_main_model_profile", None
            ),
            active_sub_model_profile=getattr(config, "active_sub_model_profile", None),
        )
    )
    runtime_state.active_model_provider = provider_id
    runtime_state.active_model = model_id
    runtime_state.active_model_display_name = display_name or model_id
    runtime_state.active_model_parameters = dict(
        {key: val for key, val in parameters.items() if val is not None}
    )
    runtime_state.active_main_model_profile = None
    runtime_state.model = model_id
    if runtime_state.active_sub_model_profile is None:
        runtime_state.active_sub_model_profile = getattr(
            config, "active_sub_model_profile", None
        )
    if runtime_state.active_mode is None:
        runtime_state.active_mode = getattr(config, "active_mode", None)
    if runtime_state.llm_debug_trace is None:
        runtime_state.llm_debug_trace = llm_trace_enabled(config)

    save_runtime_state = getattr(session_store, "save_runtime_state", None)
    messages = list(loaded.messages) if loaded is not None else []
    total_prompt_tokens = loaded.total_prompt_tokens if loaded is not None else 0
    total_completion_tokens = (
        loaded.total_completion_tokens if loaded is not None else 0
    )
    if callable(save_runtime_state):
        save_runtime_state(
            session_id,
            runtime_state.model or resolve_model_runtime(config).model,
            runtime_state,
            messages=messages,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            active_mode=runtime_state.active_mode,
            fingerprint=fingerprint,
        )
    else:
        session_store.save(
            messages,
            runtime_state.model or resolve_model_runtime(config).model,
            session_id,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            active_mode=runtime_state.active_mode,
            runtime_state=runtime_state,
            fingerprint=fingerprint,
        )

    saved = session_store.load(session_id)
    return {
        "ok": True,
        "session_id": session_id,
        "fingerprint": fingerprint,
        "active_model": active_model_payload(
            provider_id,
            model_id,
            runtime_state.active_model_parameters,
            runtime_state.active_model_display_name or "",
        ),
        "runtime_state": runtime_state.to_dict(),
        "metadata": remote_session_metadata_payload(saved)
        if saved is not None
        else {
            "id": session_id,
            "model": runtime_state.model or "",
            "saved_at": "",
            "preview": "",
            "fingerprint": fingerprint,
        },
    }


def bind_remote_session_run_handler(runner, agent: Agent) -> None:
    """Bind remote chat handlers for interactive peers."""
    if runner._relay_http_service is None or runner._relay_server is None:
        return
    setattr(agent, "agent_run_control_plane", runner._relay_http_service.runtime_control_plane)

    relay_server: RelayServer = runner._relay_server
    config = getattr(agent, "runtime_config", None)
    runtime_config: dict[str, Config | None] = {"value": config}
    ui_bus = getattr(agent.context, "_ui_bus", None)
    sessions_dir = (
        Path(config.session_dir)
        if config and getattr(config, "session_dir", None)
        else None
    )
    skills_service: SkillsService | None = getattr(agent, "skills_service", None)
    session_store = runner.dependencies.create_configured_session_store(
        config, sessions_dir
    )
    startup_announced: set[tuple[str, str, str]] = set()
    interactive_run_limiter = get_interactive_run_limiter()
    if config is not None:
        interactive_run_limiter.configure(
            max_running_agents=config.run_limits.max_running_agents,
            max_shells_per_agent=config.run_limits.max_shells_per_agent,
        )

    def _current_config() -> Config | None:
        return runtime_config["value"]

    def _reload_config() -> None:
        nonlocal skills_service
        next_config = runner.dependencies.load_config(runner.options.config_path)
        setattr(next_config, "_source_path", runner.options.config_path)
        if runner.options.server_mode:
            next_config.remote_exec.enabled = True
            next_config.remote_exec.host_mode = True
        errors = next_config.validate()
        if errors:
            raise ValueError("; ".join(errors))
        runtime_config["value"] = next_config
        interactive_run_limiter.configure(
            max_running_agents=next_config.run_limits.max_running_agents,
            max_shells_per_agent=next_config.run_limits.max_shells_per_agent,
        )
        if runner._relay_http_service.runtime_control_plane is not None:
            runner._relay_http_service.runtime_control_plane.configure(
                max_running_tasks=next_config.run_limits.max_running_agents,
                runtime_snapshot=build_agent_run_snapshot(
                    agent_registry=next_config.agent_registry,
                    runtime_profiles=next_config.runtime_profiles,
                    run_limits=next_config.run_limits,
                    capability_packages=next_config.capability_packages,
                    capability_components=next_config.capability_components,
                ),
            )
        setattr(agent, "runtime_config", next_config)
        _rebuild_agent_lifecycle_dispatcher(agent, next_config)
        chat_agent = _chat_entrypoint_agent_config(next_config)
        catalog_agent_id = str(getattr(chat_agent, "id", "") or DEFAULT_MAIN_CHAT_AGENT_ID)
        setattr(
            agent,
            "capability_catalog",
            runner.build_capability_catalog(next_config, catalog_agent_id),
        )
        setattr(agent, "session_fingerprint", get_session_fingerprint(next_config, agent))
        if chat_agent is not None:
            scope = resolve_agent_effective_capability_scope(next_config, catalog_agent_id)
            runner._relay_http_service.mcp_servers = (
                list(scope.mcp_servers) if scope.found else list(next_config.mcp_servers)
            )
        else:
            runner._relay_http_service.mcp_servers = list(next_config.mcp_servers)
        runner._relay_http_service.mcp_artifact_root = Path(
            next_config.mcp_artifact_root
        )
        runner._relay_http_service.environment_requirements = dict(
            next_config.environment.requirements
        )
        runner._relay_http_service.capability_packages = dict(
            next_config.capability_packages
        )
        runner._relay_http_service.environment_requirement_scope_ids = (
            resolve_agent_environment_requirement_scope_ids(next_config)
        )
        if ui_bus is not None:
            ui_bus.set_lifecycle_dispatcher(getattr(agent, "lifecycle_dispatcher", None))
            old_mcp_manager = getattr(agent, "mcp_manager", None)
            if old_mcp_manager is not None:
                try:
                    old_mcp_manager.stop()
                except Exception:
                    pass
            agent.tools = [
                tool for tool in agent.tools if getattr(tool, "tool_source", "") != "mcp"
            ]
            skills_service = runner._init_skills(next_config, agent, ui_bus)
            runner._attach_mcp_if_configured(next_config, agent, ui_bus)
        if ui_bus is not None:
            ui_bus.info("Remote admin config reloaded.", kind=UIEventKind.REMOTE)

    runner._relay_http_service.admin_manager.reload_handler = _reload_config

    def _dispatch_config_change_lifecycle(event: dict[str, Any]) -> None:
        dispatcher = getattr(agent, "lifecycle_dispatcher", None)
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            return
        metadata = {
            key: event[key]
            for key in (
                "status",
                "config_path",
                "previous_etag",
                "current_etag",
                "changed_sections",
                "execution_target",
                "path_space",
                "runtime_working_directory",
                "runtime_workspace_root",
            )
            if key in event
        }
        context = build_lifecycle_event_context(
            "ConfigChange",
            placement="server",
            trigger_source=str(event.get("trigger_source") or "admin"),
            session_run_id=str(getattr(agent, "current_session_id", "") or ""),
            agent_run_id=runtime_agent_run_id(agent),
            turn_id=str(getattr(agent, "runtime_turn_id", "") or ""),
            origin="admin",
            locale=str(getattr(agent, "locale", "") or ""),
            metadata=metadata,
            payload=dict(event),
        )
        try:
            dispatch(context)
        except Exception:
            return

    runner._relay_http_service.admin_manager.config_change_handler = (
        _dispatch_config_change_lifecycle
    )

    def _peer_fingerprint(peer_id: str) -> str:
        peer = relay_server.registry.get(peer_id)
        workspace_root = peer.workspace_root if peer is not None else "."
        machine_key = peer_id
        if peer is not None:
            host_info = (
                peer.meta.get("host_info_min") if isinstance(peer.meta, dict) else None
            )
            if isinstance(host_info, dict):
                machine_key = str(
                    host_info.get("hostname") or host_info.get("machine_id") or peer_id
                )
        return f"remote:{machine_key}:{workspace_root or '.'}"

    def _peer_runtime_context(peer_id: str) -> dict[str, Any]:
        peer = relay_server.registry.get(peer_id)
        if peer is None:
            raise ValueError(f"remote peer '{peer_id}' is not online")
        host_info = peer.meta.get("host_info_min") if isinstance(peer.meta, dict) else None
        if not isinstance(host_info, dict):
            raise ValueError("remote peer registration missing host_info_min")
        for key in ("os", "shell"):
            value = host_info.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"remote peer registration missing host_info_min.{key}")
        if not isinstance(peer.cwd, str) or not peer.cwd.strip():
            raise ValueError("remote peer registration missing cwd")
        if not isinstance(peer.workspace_root, str) or not peer.workspace_root.strip():
            raise ValueError("remote peer registration missing workspace_root")
        if not isinstance(peer.features, list):
            raise ValueError("remote peer registration missing features")
        return {
            "cwd": peer.cwd,
            "workspace_root": peer.workspace_root,
            "features": list(peer.features),
            "host_info_min": dict(host_info),
        }

    def _session_history_status() -> dict[str, bool]:
        current_config = _current_config()
        session_auto_save = bool(
            getattr(current_config, "session_auto_save", True)
            if current_config is not None
            else False
        )
        persistence = getattr(current_config, "persistence", None)
        sessions_enabled = bool(getattr(persistence, "sessions_enabled", True))
        return {
            "session_auto_save": session_auto_save,
            "session_history_writable": bool(
                runner._relay_http_service
                and runner._relay_http_service.session_handler is not None
                and session_auto_save
                and sessions_enabled
            ),
        }

    def _session_metadata_payload(
        session: Session | SessionMetadata,
    ) -> dict[str, Any]:
        preview = (
            session.preview
            if isinstance(session, SessionMetadata)
            else session.get_preview()
        )
        return {
            "id": session.id,
            "model": session.model,
            "saved_at": session.saved_at,
            "preview": preview,
            "fingerprint": session.fingerprint,
        }

    def _load_session_document(session_id: str) -> dict[str, Any] | None:
        loader = getattr(session_store, "load_document", None)
        if not callable(loader):
            return None
        try:
            document = loader(session_id)
        except Exception:
            return None
        return document if isinstance(document, dict) else None

    def _record_transcript(record: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(record, dict):
            return None
        transcript = record.get("transcript")
        return transcript if isinstance(transcript, dict) else None

    def _load_session_record(
        session_id: str,
        loaded: Session | None = None,
    ) -> dict[str, Any] | None:
        loader = getattr(session_store, "load_record", None)
        if callable(loader):
            try:
                record = loader(session_id)
            except Exception:
                record = None
            if isinstance(record, dict):
                return record
        session = loaded or session_store.load(session_id)
        if session is None:
            return None
        list_events = getattr(session_store, "list_trace_events", None)
        events: list[dict[str, Any]] = []
        if callable(list_events):
            try:
                raw_events = list_events(session_id, replayable_only=False)
            except Exception:
                raw_events = []
            if isinstance(raw_events, list):
                events = [dict(event) for event in raw_events if isinstance(event, dict)]
        return session.to_record(
            transcript=_load_session_document(session_id),
            events=events,
        )

    def _save_session_document(session_id: str, document: dict[str, Any]) -> None:
        saver = getattr(session_store, "save_document", None)
        if callable(saver):
            saver(session_id, document)

    def _is_recoverable_live_chat_session(chat_session: Any) -> bool:
        if chat_session is None:
            return False
        if bool(getattr(chat_session, "done", False)):
            return False
        if not bool(getattr(chat_session, "running", False)):
            return False
        status = str(getattr(chat_session, "status", "") or "").lower()
        if status in {"done", "error", "cancelled", "interrupted", "failed"}:
            return False
        return True

    def _repair_orphaned_running_session_document(
        session_id: str,
        document: dict[str, Any] | None,
        record: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(document, dict):
            return document
        run_state = document.get("run_state") if isinstance(document.get("run_state"), dict) else {}
        stats = document.get("stats") if isinstance(document.get("stats"), dict) else {}
        session_run_id = str(run_state.get("session_run_id") or "")
        if not session_run_id:
            return document
        if str(run_state.get("status") or "") != "running" and str(stats.get("runStatus") or "") != "running":
            return document
        service = runner._relay_http_service
        live_session = service._get_session_run(session_run_id) if service is not None else None
        if _is_recoverable_live_chat_session(live_session):
            return document
        repaired = settle_orphaned_running_session_run(
            document,
            session_id=session_id,
            reason="审批已失效：远端运行已结束，请重新发起任务。",
        )
        _save_session_document(session_id, repaired)
        if isinstance(record, dict):
            record["transcript"] = repaired
        return repaired

    def _append_session_trace_event(
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int | None:
        append_event = getattr(session_store, "append_trace_event", None)
        if not callable(append_event):
            return None
        return append_event(
            session_id,
            event_type,
            payload,
            session_run_id=session_run_id,
            session_run_seq=session_run_seq,
            source=source,
            replayable=replayable,
        )

    def _persist_session_placeholder(peer_agent: Agent, peer_id: str) -> None:
        current_config = _current_config()
        session_id = str(getattr(peer_agent, "current_session_id", "") or "")
        if not session_id or current_config is None:
            return
        if not getattr(current_config, "session_auto_save", True):
            return
        if session_store.load(session_id) is not None:
            return
        save_runtime_state = getattr(session_store, "save_runtime_state", None)
        if not callable(save_runtime_state):
            return
        save_runtime_state(
            session_id,
            getattr(getattr(peer_agent, "llm", None), "model", "")
            or resolve_model_runtime(current_config).model,
            build_session_runtime_state(current_config, peer_agent),
            messages=[],
            active_mode=getattr(peer_agent, "active_mode", None),
            fingerprint=_peer_fingerprint(peer_id),
        )

    def _handle_session_request(
        action: str, peer_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        fingerprint = _peer_fingerprint(peer_id)
        current_config = _current_config()
        if action == "list":
            limit = max(1, min(100, int(payload.get("limit", 20) or 20)))
            sessions = session_store.list(limit=limit, fingerprint=fingerprint)
            session_payloads = [
                _session_metadata_payload(session) for session in sessions
            ]
            list_etag = _stable_digest(session_payloads)
            if payload.get("if_list_etag") == list_etag:
                return {
                    "ok": True,
                    "fingerprint": fingerprint,
                    "list_etag": list_etag,
                    "sessions_unchanged": True,
                }
            return {
                "ok": True,
                "fingerprint": fingerprint,
                "list_etag": list_etag,
                "sessions_unchanged": False,
                "sessions": session_payloads,
            }

        if action == "load":
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                return {"ok": False, "error": "missing_session_id", "_status": 400}
            loaded = session_store.load(session_id)
            if loaded is None:
                return {"ok": False, "error": "session_not_found", "_status": 404}
            if loaded.fingerprint != fingerprint:
                return {
                    "ok": False,
                    "error": "session_fingerprint_mismatch",
                    "fingerprint": loaded.fingerprint,
                    "current_fingerprint": fingerprint,
                    "_status": 403,
                }
            record = _load_session_record(session_id, loaded)
            document = _record_transcript(record) or _load_session_document(session_id)
            document = _repair_orphaned_running_session_document(session_id, document, record)
            return {
                "ok": True,
                "fingerprint": fingerprint,
                "record": record,
                "metadata": _session_metadata_payload(loaded),
                "document": document,
                "runtime_state": loaded.runtime_state.to_dict(),
                "last_event_seq": int((document or {}).get("last_event_seq") or 0),
            }

        if action == "new":
            if current_config is None:
                return {"ok": False, "error": "config_unavailable", "_status": 503}
            session_id = session_store.generate_session_id()
            runtime_state = SessionRuntimeState(
                model=getattr(current_config, "model", None),
                active_mode=getattr(current_config, "active_mode", None),
                active_main_model_profile=getattr(
                    current_config, "active_main_model_profile", None
                ),
                active_sub_model_profile=getattr(
                    current_config, "active_sub_model_profile", None
                ),
            )
            active_mode = getattr(current_config, "active_mode", None)
            agent_config = getattr(current_config.agent_registry, "agents", {}).get(
                active_mode
            )
            agent_model = getattr(agent_config, "model", None)
            if getattr(agent_model, "configured", False):
                runtime_state.active_model_provider = agent_model.provider
                runtime_state.active_model = agent_model.model
                runtime_state.active_model_display_name = agent_model.display_name
                runtime_state.active_model_parameters = dict(agent_model.parameters)
                runtime_state.model = agent_model.model
            save_runtime_state = getattr(session_store, "save_runtime_state", None)
            if callable(save_runtime_state):
                save_runtime_state(
                    session_id,
                    runtime_state.model or "",
                    runtime_state,
                    messages=[],
                    active_mode=runtime_state.active_mode,
                    fingerprint=fingerprint,
                )
            loaded = session_store.load(session_id)
            record = _load_session_record(session_id, loaded)
            document = _record_transcript(record) or _load_session_document(session_id)
            return {
                "ok": True,
                "fingerprint": fingerprint,
                "record": record,
                "metadata": _session_metadata_payload(loaded)
                if loaded is not None
                else {
                    "id": session_id,
                    "model": runtime_state.model or "",
                    "saved_at": "",
                    "preview": "",
                    "fingerprint": fingerprint,
                },
                "runtime_state": runtime_state.to_dict(),
                "document": document,
                "last_event_seq": int((document or {}).get("last_event_seq") or 0),
            }

        if action == "model":
            result = switch_session_model(
                current_config,
                session_store,
                fingerprint,
                payload,
            )
            session_id = str(result.get("session_id") or payload.get("session_id") or "")
            if result.get("ok") and session_id:
                record = _load_session_record(session_id)
                document = _record_transcript(record) or _load_session_document(session_id)
                result["record"] = record
                result["document"] = document
                result["last_event_seq"] = int((document or {}).get("last_event_seq") or 0)
            return result

        if action == "delete":
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                return {"ok": False, "error": "missing_session_id", "_status": 400}
            loaded = session_store.load(session_id)
            if loaded is None:
                return {"ok": False, "error": "session_not_found", "_status": 404}
            if loaded.fingerprint != fingerprint:
                return {
                    "ok": False,
                    "error": "session_fingerprint_mismatch",
                    "fingerprint": loaded.fingerprint,
                    "current_fingerprint": fingerprint,
                    "_status": 403,
                }
            deleted = session_store.delete(session_id)
            delete_events = getattr(session_store, "delete_trace_events", None)
            if callable(delete_events):
                delete_events(session_id)
            return {
                "ok": deleted,
                "session_id": session_id,
                "fingerprint": fingerprint,
            }

        return {"ok": False, "error": "unknown_session_action", "_status": 404}

    runner._relay_http_service.session_history_status_provider = _session_history_status
    runner._relay_http_service.set_session_handler(_handle_session_request)
    set_trace_sink = getattr(runner._relay_http_service, "set_session_trace_event_sink", None)
    if callable(set_trace_sink):
        set_trace_sink(_append_session_trace_event)

    def _apply_chat_entrypoint_runtime(
        peer_agent: Agent,
        current_config: Config,
    ) -> Any | None:
        chat_agent = _chat_entrypoint_agent_config(current_config)
        if chat_agent is None:
            return None
        agent_id = str(getattr(chat_agent, "id", "") or "").strip()
        if not agent_id:
            return None
        setattr(peer_agent, "agent_config_id", agent_id)
        setattr(peer_agent, "main_agent_id", agent_id)
        capability_projection = _capability_projection_for_agent(
            current_config,
            agent_id,
        )
        resolved_capabilities = capability_projection.get("resolved_capabilities")
        effective_capabilities = capability_projection.get("effective_capabilities")
        setattr(
            peer_agent,
            "resolved_capabilities",
            resolved_capabilities if isinstance(resolved_capabilities, dict) else {},
        )
        setattr(
            peer_agent,
            "effective_capabilities",
            effective_capabilities if isinstance(effective_capabilities, dict) else {},
        )
        setattr(peer_agent, "enforce_effective_capabilities", True)
        setattr(peer_agent, "capability_catalog", runner.build_capability_catalog(current_config, agent_id))
        if not getattr(peer_agent, "session_model_overridden", False):
            apply_agent_config_model(current_config, peer_agent, agent_id)
        runtime_config = _runtime_config_with_chat_agent_prompt(
            getattr(peer_agent, "runtime_config", current_config),
            chat_agent,
        )
        if runtime_config is not None:
            setattr(peer_agent, "runtime_config", runtime_config)
        return chat_agent

    def _create_peer_agent(
        peer_id: str,
        remote_stream_handler: Callable[..., None] | None = None,
        session_hint: str | None = None,
        resume_latest: bool = True,
    ) -> Agent:
        current_config = _current_config()
        if current_config is None:
            return agent

        peer_llm = runner.dependencies.create_llm(current_config)
        peer_llm.ui_bus = ui_bus
        peer_backend = RemoteRelayToolBackend(relay_server=relay_server, ui_bus=ui_bus)
        peer_tools = runner.dependencies.load_tools(peer_backend)
        peer_agent = runner.dependencies.create_agent(
            peer_llm, peer_tools, current_config
        )
        server_mcp_manager = getattr(agent, "mcp_manager", None)
        server_mcp_tools = list(getattr(server_mcp_manager, "tools", []) or [])
        if server_mcp_tools:
            peer_agent.add_tools(server_mcp_tools)
        setattr(peer_agent, "runtime_config", current_config)
        setattr(peer_agent, "capability_catalog", runner.build_capability_catalog(current_config))
        bind_memory_scope_to_agent(
            peer_agent,
            owner_agent_id=f"peer:{peer_id}",
            memory_namespace=f"peer:{peer_id}",
        )
        if runner._relay_http_service is not None:
            setattr(
                peer_agent,
                "agent_run_control_plane",
                runner._relay_http_service.runtime_control_plane,
            )
        setattr(peer_agent, "skills_service", skills_service)
        setattr(peer_agent, "skills_catalog", getattr(agent, "skills_catalog", ""))
        runner._register_hooks(peer_agent, current_config)
        runner._wire_agent_tool_parent(peer_agent)

        peer_context = _peer_runtime_context(peer_id)
        workspace_root = peer_context["workspace_root"]
        runtime_cwd = peer_context["cwd"]
        setattr(peer_agent, "runtime_execution_target", "remote_peer")
        setattr(peer_agent, "runtime_peer_context", peer_context)
        setattr(peer_agent, "runtime_working_directory", runtime_cwd)
        setattr(peer_agent, "workspace_mutation_backend", peer_backend)
        peer_backend.workspace_id = str(workspace_root)
        peer_backend.execution_target = "remote_peer"
        peer_backend.path_space = "remote_peer_workspace"
        for tool_info in relay_server.get_peer_mcp_tools(peer_id):
            peer_agent.add_tools([RemotePeerMCPTool(peer_backend, tool_info)])
        for tool in peer_agent.tools:
            backend = getattr(tool, "backend", None)
            if getattr(backend, "backend_id", None) != "remote_relay":
                continue
            context = getattr(backend, "context", None)
            if not isinstance(context, ExecutionContext):
                continue
            context.peer_id = peer_id
            context.remote_stream_handler = remote_stream_handler
            context.execution_target = "remote_peer"
            context.cwd = runtime_cwd
            context.workspace_root = workspace_root

        fingerprint = _peer_fingerprint(peer_id)
        setattr(peer_agent, "session_fingerprint", fingerprint)

        if session_hint:
            loaded = session_store.load(session_hint)
            if loaded is not None:
                if loaded.fingerprint != fingerprint:
                    raise ValueError(
                        f"Session '{session_hint}' belongs to fingerprint "
                        f"'{loaded.fingerprint}', current fingerprint is '{fingerprint}'."
                    )
                apply_session_runtime_state(loaded, current_config, peer_agent)
            else:
                restore_config_runtime_defaults(current_config, peer_agent)
            _apply_chat_entrypoint_runtime(peer_agent, current_config)
            setattr(peer_agent, "current_session_id", session_hint)
            return peer_agent

        if resume_latest:
            latest = session_store.get_latest(fingerprint=fingerprint)
            if latest:
                loaded = session_store.load(latest.id)
                if loaded is not None:
                    apply_session_runtime_state(loaded, current_config, peer_agent)
                    _apply_chat_entrypoint_runtime(peer_agent, current_config)
                    setattr(peer_agent, "current_session_id", latest.id)
                    return peer_agent

        restore_config_runtime_defaults(current_config, peer_agent)
        _apply_chat_entrypoint_runtime(peer_agent, current_config)
        setattr(peer_agent, "current_session_id", session_store.generate_session_id())
        return peer_agent

    def _bind_main_chat_account_memory_scope(peer_agent: Agent, peer_id: str) -> None:
        peer = relay_server.registry.get(peer_id)
        if peer is not None:
            bind_main_chat_memory_scope_to_agent(peer_agent, peer_info=peer)

    def _save_peer_session(peer_agent: Agent, peer_id: str) -> None:
        current_config = _current_config()
        if (
            current_config is None
            or not getattr(current_config, "session_auto_save", True)
            or not getattr(peer_agent, "messages", None)
        ):
            return
        sid = session_store.save(
            peer_agent.messages,
            getattr(peer_agent.llm, "model", "")
            or resolve_model_runtime(current_config).model,
            getattr(peer_agent, "current_session_id", None),
            total_prompt_tokens=peer_agent.state.total_prompt_tokens,
            total_completion_tokens=peer_agent.state.total_completion_tokens,
            active_mode=getattr(peer_agent, "active_mode", None),
            runtime_state=build_session_runtime_state(current_config, peer_agent),
            fingerprint=_peer_fingerprint(peer_id),
        )
        setattr(peer_agent, "current_session_id", sid)

    def _enable_remote_session_trace_persistence(
        peer_agent: Agent,
        peer_id: str,
        remote_session: Any,
    ) -> None:
        current_config = _current_config()
        if current_config is None or not getattr(current_config, "session_auto_save", True):
            return
        _persist_session_placeholder(peer_agent, peer_id)
        session_id = str(getattr(peer_agent, "current_session_id", "") or "")
        enable_trace_persistence = getattr(
            remote_session, "enable_trace_persistence", None
        )
        if session_id and callable(enable_trace_persistence):
            enable_trace_persistence(session_id)

    def _apply_remote_session_run_model_override(
        peer_agent: Agent,
        peer_id: str,
        remote_session: Any,
        *,
        ensure_session_run_start: Callable[[], None] | None = None,
    ) -> bool:
        def _ensure_start() -> None:
            if callable(ensure_session_run_start):
                ensure_session_run_start()

        provider_id = str(getattr(remote_session, "provider_id", "") or "").strip()
        model_id = str(getattr(remote_session, "model_id", "") or "").strip()
        parameters = getattr(remote_session, "model_parameters", None)
        if not isinstance(parameters, dict):
            parameters = {}
        if not provider_id and not model_id:
            return True
        if not provider_id or not model_id:
            _ensure_start()
            remote_session.append_event(
                "error",
                {
                    "message": "provider_model_required",
                    "provider_id": provider_id,
                    "model_id": model_id,
                },
            )
            remote_session.append_event("session_run_end", {"response": ""})
            return False
        current_config = _current_config()
        if current_config is None:
            _ensure_start()
            remote_session.append_event("error", {"message": "config_unavailable"})
            remote_session.append_event("session_run_end", {"response": ""})
            return False
        session_id = str(
            getattr(peer_agent, "current_session_id", None)
            or getattr(remote_session, "session_hint", None)
            or ""
        ).strip()
        result = switch_session_model(
            current_config,
            session_store,
            _peer_fingerprint(peer_id),
            {
                "session_id": session_id,
                "provider_id": provider_id,
                "model_id": model_id,
                "parameters": dict(parameters),
            },
        )
        if not result.get("ok"):
            _ensure_start()
            remote_session.append_event(
                "error",
                {
                    "message": str(result.get("error") or "model_override_failed"),
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "status": result.get("_status"),
                },
            )
            remote_session.append_event("session_run_end", {"response": ""})
            return False
        resolved_session_id = str(result.get("session_id") or session_id).strip()
        if resolved_session_id:
            setattr(peer_agent, "current_session_id", resolved_session_id)
            remote_session.session_id = resolved_session_id
        try:
            apply_session_model_override(
                current_config,
                peer_agent,
                provider=provider_id,
                model=model_id,
                display_name=model_id,
                parameters=parameters,
            )
        except Exception as exc:
            _ensure_start()
            remote_session.append_event(
                "error",
                {
                    "message": str(exc),
                    "provider_id": provider_id,
                    "model_id": model_id,
                },
            )
            remote_session.append_event("session_run_end", {"response": ""})
            return False
        return True

    def _agent_chat(agent_obj: Any, prompt: str, *, clear_stop_request: bool) -> str:
        signature = inspect.signature(agent_obj.chat)
        if "clear_stop_request" in signature.parameters:
            return agent_obj.chat(prompt, clear_stop_request=clear_stop_request)
        return agent_obj.chat(prompt)

    def _normalized_chat_mentions(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        mentions: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "").strip()
            name = str(item.get("name") or item.get("path") or item.get("id") or "").strip()
            if kind not in {"file", "capability", "agent_tool", "mcp", "plugin"} or not name:
                continue
            mention = {
                "kind": kind,
                "name": name,
            }
            mention["reference_only"] = True
            for key in ("id", "path", "source", "insertText", "insert_text"):
                raw = item.get(key)
                if raw is not None and str(raw).strip():
                    mention[key] = str(raw).strip()
            mentions.append(mention)
        return mentions

    def _prompt_with_mention_context(prompt: str, mentions: list[dict[str, Any]]) -> str:
        if not mentions:
            return prompt
        lines = [
            "",
            "Referenced context objects from the user's @ mentions:",
        ]
        for mention in mentions:
            target = mention.get("path") or mention.get("id") or mention.get("name")
            source = mention.get("source") or "unknown"
            lines.append(
                f"- {mention['kind']}: {mention['name']} ({target}; source={source}; reference_only=true)"
            )
        lines.append(
            "These @ mentions are context references only and do not grant new tool permissions."
        )
        return f"{prompt}\n" + "\n".join(lines)

    def _dispatch_vscode_command(
        peer_agent: Agent,
        peer_id: str,
        command_text: str,
        *,
        invalid_as_chat: bool = False,
    ) -> ChatCommandDispatchResponse:
        current_config = _current_config()
        if current_config is None:
            return ChatCommandDispatchResponse(
                ok=False,
                action="continue",
                session_id=getattr(peer_agent, "current_session_id", None),
                events=[
                    {
                        "type": "error",
                        "payload": {
                            "message": "config_unavailable",
                            "code": "config_unavailable",
                        },
                    }
                ],
                error="config_unavailable",
            )

        command_bus = UIEventBus()
        peer_context_manager = getattr(peer_agent, "context", None)
        previous_context_bus = getattr(peer_context_manager, "_ui_bus", None)
        if peer_context_manager is not None:
            peer_context_manager._ui_bus = command_bus
        try:
            command_result = handle_command(
                command_text,
                peer_agent,
                current_config,
                getattr(peer_agent, "current_session_id", None),
                command_bus,
                VSCODE_CHAT_PROFILE,
                runner.dependencies.create_action_registry(),
                sessions_dir,
                skills_service,
            )
        finally:
            if peer_context_manager is not None:
                peer_context_manager._ui_bus = previous_context_bus

        session_id = command_result["session_id"]
        events = [
            {
                "type": _structured_ui_event_type(event),
                "payload": _structured_ui_event_payload(event),
            }
            for event in getattr(command_bus, "_history", [])
        ]
        if command_result["action"] == "chat":
            if invalid_as_chat:
                return ChatCommandDispatchResponse(
                    ok=True,
                    action="chat",
                    session_id=session_id,
                    events=events,
                )
            events.append(
                {
                    "type": "error",
                    "payload": {
                        "message": f"Unknown or invalid command: {command_text}",
                        "code": "invalid_chat_command",
                    },
                }
            )
            return ChatCommandDispatchResponse(
                ok=False,
                action="chat",
                session_id=session_id,
                events=events,
                error="invalid_chat_command",
            )

        setattr(peer_agent, "current_session_id", session_id)
        if command_result["action"] == "exit":
            events.append(
                {
                    "type": "output",
                    "payload": {
                        "format": "plain",
                        "content": "Exit command received. Use Ctrl+C to terminate remote peer.\n",
                    },
                }
            )
        _save_peer_session(peer_agent, peer_id)
        return ChatCommandDispatchResponse(
            ok=True,
            action=str(command_result["action"]),
            session_id=session_id,
            events=events,
        )

    def _dispatch_chat_command(
        peer_id: str, req: ChatCommandDispatchRequest
    ) -> ChatCommandDispatchResponse:
        command_text = req.command_text
        peer_agent = _create_peer_agent(
            peer_id,
            session_hint=req.session_hint,
            resume_latest=False,
        )
        _bind_main_chat_account_memory_scope(peer_agent, peer_id)
        response = _dispatch_vscode_command(peer_agent, peer_id, command_text)
        response.events.insert(
            0,
            {
                "type": "session_run_start",
                "payload": {
                    "prompt": command_text,
                    "command_id": req.command_id,
                    "trigger": req.trigger,
                    "mentions": req.mentions,
                },
            },
        )
        response.events.append({"type": "session_run_end", "payload": {"response": ""}})
        return response

    def _stream_session_run(peer_id: str, prompt: str, remote_session) -> None:
        def _ensure_session_run_start() -> None:
            ensure_start = getattr(remote_session, "ensure_session_run_start", None)
            if callable(ensure_start):
                ensure_start(prompt)
                return
            events = getattr(remote_session, "events", [])
            if any(event.get("type") == "session_run_start" for event in events):
                return
            remote_session.append_event(
                "session_run_start",
                {
                    "prompt": prompt,
                    "mode": getattr(remote_session, "mode", None),
                    "workflow_mode": getattr(remote_session, "workflow_mode", None),
                    "taskflow_id": getattr(remote_session, "taskflow_id", None),
                    "provider_id": getattr(remote_session, "provider_id", None),
                    "model_id": getattr(remote_session, "model_id", None),
                    "locale": getattr(remote_session, "locale", None),
                    "mentions": getattr(remote_session, "mentions", []),
                },
            )

        peer_agent = _create_peer_agent(
            peer_id,
            session_hint=getattr(remote_session, "session_hint", None),
            resume_latest=False,
        )
        _enable_remote_session_trace_persistence(peer_agent, peer_id, remote_session)
        requested_mode = str(getattr(remote_session, "mode", "") or "").strip()
        if requested_mode:
            try:
                peer_agent.set_mode(requested_mode)
                if not getattr(peer_agent, "session_model_overridden", False):
                    current_config = _current_config()
                    if current_config is not None:
                        apply_agent_default_model(current_config, peer_agent)
            except ValueError as exc:
                _ensure_session_run_start()
                remote_session.append_event(
                    "error",
                    {
                        "message": str(exc),
                        "mode": requested_mode,
                    },
                )
                remote_session.append_event("session_run_end", {"response": ""})
                return
        if getattr(remote_session, "workflow_mode", None) == TASKFLOW_WORKFLOW_MODE:
            taskflow_service = runner._relay_http_service.taskflow_service
            taskflow_id = getattr(remote_session, "taskflow_id", None)
            if not taskflow_id:
                state = taskflow_service.start_taskflow(
                    project_id=f"peer-{peer_id}",
                    raw_goal=prompt,
                    session_id=getattr(peer_agent, "current_session_id", None),
                    peer_id=peer_id,
                    metadata={"source": "session_run_start"},
                )
                taskflow_id = state.meta.taskflow_id
                remote_session.taskflow_id = taskflow_id
                remote_session.append_event(
                    "taskflow_started", {"taskflow": state.to_dict()}
                )
            setattr(peer_agent, "workflow_mode", TASKFLOW_WORKFLOW_MODE)
            setattr(peer_agent, "memory_project_id", f"peer-{peer_id}")
            setattr(peer_agent, "memory_taskflow_id", taskflow_id)
            setattr(
                peer_agent,
                "workflow_prompt_append",
                f"{TASKFLOW_SYSTEM_PROMPT}\nCurrent Taskflow taskflow_id: `{taskflow_id}`.",
            )
        else:
            _bind_main_chat_account_memory_scope(peer_agent, peer_id)
        if not _apply_remote_session_run_model_override(
            peer_agent,
            peer_id,
            remote_session,
            ensure_session_run_start=_ensure_session_run_start,
        ):
            return
        remote_session.set_cancel_callback(
            lambda reason: (
                peer_agent.request_stop(),
                relay_server.cancel_pending_requests(peer_id, reason),
            )
        )
        if hasattr(remote_session, "set_follow_up_callback"):
            remote_session.set_follow_up_callback(
                lambda ticket: peer_agent.queue_follow_up(
                    str(ticket.get("followup_id") or ""),
                    str(ticket.get("text") or ""),
                )
            )
        if hasattr(remote_session, "set_follow_up_cancel_callback") and hasattr(
            peer_agent, "cancel_follow_up"
        ):
            remote_session.set_follow_up_cancel_callback(peer_agent.cancel_follow_up)
        if hasattr(peer_agent, "set_follow_up_consumed_handler"):
            peer_agent.set_follow_up_consumed_handler(
                lambda item: remote_session.mark_follow_up_consumed(
                    getattr(item, "followup_id", "")
                )
            )
        if getattr(remote_session, "cancel_requested", False):
            peer_agent.request_stop()

        runtime_agent_id = f"session_run:{getattr(remote_session, 'session_run_id', uuid.uuid4().hex)}"
        setattr(peer_agent, "runtime_agent_id", runtime_agent_id)

        mention_refs = _normalized_chat_mentions(getattr(remote_session, "mentions", []))
        if mention_refs:
            remote_session.append_event(
                "mention_context",
                {
                    "mentions": mention_refs,
                    "reference_only": True,
                },
            )
            prompt = _prompt_with_mention_context(prompt, mention_refs)

        def _emit_runtime_status(payload: dict[str, Any]) -> None:
            remote_session.append_event("runtime_status", payload)

        try:
            interactive_run_limiter.acquire_agent_slot(
                runtime_agent_id,
                agent_type="chat",
                label=peer_id,
                is_cancelled=lambda: bool(getattr(remote_session, "cancel_requested", False)),
                on_wait=_emit_runtime_status,
            )
        except AgentRunCancelled:
            _ensure_session_run_start()
            remote_session.append_event(
                "session_run_cancelled",
                {"reason": getattr(remote_session, "cancel_reason", "user_cancelled")},
            )
            remote_session.append_event("session_run_end", {"response": ""})
            return

        session_id = getattr(peer_agent, "current_session_id", "-") or "-"
        peer_info = relay_server.registry.get(peer_id)
        connection_marker = (
            f"{getattr(peer_info, 'connected_at', 0):.6f}"
            if peer_info is not None
            else "0"
        )
        startup_key = (peer_id, str(session_id), connection_marker)
        if startup_key not in startup_announced:
            remote_session.append_event(
                "remote_peer_ready",
                {
                    "peer_id": peer_id,
                    "session_id": session_id,
                    "fingerprint": _peer_fingerprint(peer_id),
                    "mode": getattr(peer_agent, "active_mode", "-") or "-",
                    "model": getattr(getattr(peer_agent, "llm", None), "model", "-")
                    or "-",
                    "main_agent_id": getattr(peer_agent, "main_agent_id", None),
                    "agent_config_id": getattr(peer_agent, "agent_config_id", None),
                    "effective_capabilities": getattr(
                        peer_agent, "effective_capabilities", {}
                    ),
                    "workspace_root": getattr(peer_info, "workspace_root", None)
                    if peer_info is not None
                    else None,
                },
            )
            startup_announced.add(startup_key)

        if prompt.startswith("/"):
            command_response = _dispatch_vscode_command(
                peer_agent,
                peer_id,
                prompt,
                invalid_as_chat=True,
            )
            if command_response.events:
                _ensure_session_run_start()
                for event in command_response.events:
                    remote_session.append_event(
                        str(event.get("type") or "output"),
                        event.get("payload")
                        if isinstance(event.get("payload"), dict)
                        else {},
                    )
            if command_response.action != "chat":
                if not command_response.events:
                    _ensure_session_run_start()
                remote_session.append_event("session_run_end", {"response": ""})
                interactive_run_limiter.release_agent_slot(runtime_agent_id)
                return

        ansi_console = Console(
            record=True, force_terminal=True, color_system="truecolor"
        )
        renderer = CLIRenderer(console_override=ansi_console)
        assistant_content_emitted = {"value": False}
        draft_content_emitted = {"value": False}
        reasoning_content_emitted = {"value": False}
        session_run_interrupted_emitted = {"value": False}
        active_tool_calls_by_name: dict[str, list[str]] = {}
        assistant_stream_parts: list[str] = []
        reasoning_stream_parts: list[str] = []
        context_event_bus = UIEventBus()
        stream_observability: dict[str, Any] = {
            "schema": "stream_observability.v1",
            "provider_output_count": 0,
            "provider_reasoning_count": 0,
            "provider_tool_delta_count": 0,
            "draft_preview_chunk_count": 0,
            "patch_syntax_error_count": 0,
            "patch_syntax_error_codes": {},
            "patch_semantic_error_count": 0,
            "patch_semantic_error_codes": {},
        }
        last_stream_observability_emit_at = {"value": 0.0}

        def _agent_event_payload(event: AgentEvent, payload: dict[str, Any]) -> dict[str, Any]:
            return {**dict(payload), "emitted_at": event.timestamp}

        def _tool_identity_event_payload(event: AgentEvent) -> dict[str, Any]:
            return {
                key: str(value).strip()
                for key, value in {
                    "tool_id": event.data.get("tool_id") or event.data.get("toolId"),
                    "risk": event.data.get("risk"),
                    "exposure": event.data.get("exposure"),
                    "capability_name": event.data.get("capability_name")
                    or event.data.get("capabilityName"),
                }.items()
                if value is not None and str(value).strip()
            }

        def _append_agent_event(
            event: AgentEvent,
            event_type: str,
            payload: dict[str, Any],
            *,
            live_only: bool = False,
        ) -> None:
            data = _agent_event_payload(event, payload)
            if live_only:
                remote_session.append_live_event(event_type, data)
                return
            remote_session.append_event(event_type, data)

        def _emit_stream_observability(reason: str, *, force: bool = False) -> None:
            now = time.time()
            if not force and now - last_stream_observability_emit_at["value"] < 1.0:
                return
            last_stream_observability_emit_at["value"] = now
            remote_session.append_event(
                "stream_observability",
                {
                    **stream_observability,
                    "reason": reason,
                    "emitted_at": now,
                },
            )

        def _record_patch_syntax_error(payload: dict[str, Any]) -> None:
            if str(payload.get("tool_name") or "") != "apply_patch":
                return
            code = str(payload.get("code") or "patch_syntax_error").strip() or "patch_syntax_error"
            codes = stream_observability.setdefault("patch_syntax_error_codes", {})
            if not isinstance(codes, dict):
                codes = {}
                stream_observability["patch_syntax_error_codes"] = codes
            codes[code] = int(codes.get(code, 0) or 0) + 1
            stream_observability["patch_syntax_error_count"] = (
                int(stream_observability.get("patch_syntax_error_count", 0) or 0) + 1
            )

        def _record_patch_semantic_error(payload: dict[str, Any]) -> None:
            if str(payload.get("tool_name") or "") != "apply_patch":
                return
            code = (
                str(payload.get("failure_code") or payload.get("code") or "semantic_preview_failed").strip()
                or "semantic_preview_failed"
            )
            codes = stream_observability.setdefault("patch_semantic_error_codes", {})
            if not isinstance(codes, dict):
                codes = {}
                stream_observability["patch_semantic_error_codes"] = codes
            codes[code] = int(codes.get(code, 0) or 0) + 1
            stream_observability["patch_semantic_error_count"] = (
                int(stream_observability.get("patch_semantic_error_count", 0) or 0) + 1
            )

        def _append_context_event(event) -> None:
            if event.kind != UIEventKind.CONTEXT:
                return
            remote_session.append_event(
                _structured_ui_event_type(event),
                _structured_ui_event_payload(event),
            )

        context_event_bus.subscribe(_append_context_event, replay_history=False)

        def _remote_diagnostic_context(
            *,
            tool_name: str | None = None,
            tool_call_id: str | None = None,
        ) -> dict[str, Any]:
            llm = getattr(peer_agent, "llm", None)
            provider_config = getattr(llm, "provider_config", None)
            return {
                "session_id": getattr(peer_agent, "current_session_id", None),
                "session_run_id": getattr(remote_session, "session_run_id", None),
                "peer_id": peer_id,
                "round_index": getattr(peer_agent.state, "current_round", None),
                "tool": tool_name,
                "tool_call_id": tool_call_id,
                "provider_id": getattr(llm, "provider_id", None),
                "provider_type": getattr(llm, "provider_type", None),
                "compat": getattr(provider_config, "compat", None),
                "model": getattr(llm, "model", None),
            }

        def _record_remote_diagnostics(
            diagnostics_payload: list[ToolDiagnostic | dict[str, Any]],
            *,
            tool_name: str | None = None,
            tool_call_id: str | None = None,
        ) -> None:
            diagnostics_config = getattr(
                getattr(peer_agent, "config", None),
                "diagnostics",
                None,
            )
            tool_diagnostics = getattr(diagnostics_config, "tool_diagnostics", None)
            if getattr(tool_diagnostics, "enabled", True) is False:
                return
            try:
                persist_tool_diagnostic_event(
                    diagnostics=diagnostics_payload,
                    metadata=_remote_diagnostic_context(
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                    ),
                )
            except Exception:
                pass

        def _flush_output() -> None:
            rendered = ansi_console.export_text(clear=True, styles=True)
            if rendered.strip():
                remote_session.append_event(
                    "output", {"format": "terminal", "content": rendered}
                )

        def _append_final_reasoning() -> None:
            if reasoning_content_emitted["value"]:
                return
            content = "".join(reasoning_stream_parts)
            if not content:
                return
            remote_session.append_event(
                "reasoning_message",
                {"format": "markdown", "content": content},
            )
            reasoning_content_emitted["value"] = True

        def _append_final_assistant(response: str | None = None) -> None:
            if assistant_content_emitted["value"]:
                return
            content = "".join(assistant_stream_parts)
            if not content and draft_content_emitted["value"]:
                return
            content = content or (response if response else "")
            if not content:
                return
            remote_session.append_event(
                "assistant_message",
                {"format": "markdown", "content": content},
            )
            assistant_content_emitted["value"] = True

        def _append_final_stream_content(response: str | None = None) -> None:
            _append_final_reasoning()
            _append_final_assistant(response)

        def _remote_backend() -> RemoteRelayToolBackend | None:
            for tool in getattr(peer_agent, "tools", []):
                backend = getattr(tool, "backend", None)
                if isinstance(backend, RemoteRelayToolBackend):
                    return backend
            return None

        def _peer_supports_tool_preview() -> bool:
            peer = relay_server.registry.get(peer_id)
            return bool(peer and "tool_preview" in peer.features)

        def _args_section(request: ApprovalRequest) -> dict[str, Any] | None:
            tool_args = _approval_tool_args(request)
            if not tool_args:
                return None
            return {
                "id": "args",
                "title": "Arguments",
                "kind": "json",
                "content": tool_args,
            }

        def _approval_tool_args(request: ApprovalRequest) -> dict[str, Any]:
            hidden_keys = {"intent", "content"}
            if request.tool_name == "draft_document_commit":
                hidden_keys.add("diff")
            return {
                key: value
                for key, value in dict(request.tool_args or {}).items()
                if key not in hidden_keys
            }

        def _owner_tool_args(request: ApprovalRequest) -> dict[str, Any]:
            return _approval_tool_args(request)

        def _approval_intent(request: ApprovalRequest) -> str | None:
            if isinstance(request.intent, str) and request.intent.strip():
                return request.intent.strip()
            value = dict(request.tool_args or {}).get("intent")
            return value.strip() if isinstance(value, str) and value.strip() else None

        def _approval_lifecycle_payload(
            request: ApprovalRequest,
        ) -> dict[str, Any]:
            metadata = (
                dict(request.metadata)
                if isinstance(request.metadata, dict)
                else {}
            )
            payload: dict[str, Any] = {}
            lifecycle_event = str(metadata.get("lifecycle_event") or "").strip()
            if lifecycle_event:
                payload["lifecycle_event"] = lifecycle_event
            lifecycle_hooks = _approval_lifecycle_hooks(
                metadata.get("lifecycle_hooks")
            )
            if lifecycle_hooks:
                payload["lifecycle_hooks"] = lifecycle_hooks
            return payload

        def _approval_lifecycle_hooks(value: Any) -> list[dict[str, Any]]:
            if not isinstance(value, list):
                return []
            public_fields = (
                "hook_id",
                "display_name",
                "source",
                "handler_type",
                "decision",
                "reason",
            )
            hooks: list[dict[str, Any]] = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                public_item = {
                    field: item[field]
                    for field in public_fields
                    if item.get(field) is not None
                }
                if public_item:
                    hooks.append(public_item)
            return hooks

        def _section_markdown(section: dict[str, Any]) -> str:
            title = str(section.get("title") or "Details")
            kind = str(section.get("kind") or "text")
            content = section.get("content", "")
            if kind == "diff":
                return f"### {title}\n\n```diff\n{content}\n```"
            if kind == "json":
                return (
                    f"### {title}\n\n```json\n"
                    f"{json.dumps(content, ensure_ascii=False, indent=2)}\n```"
                )
            return f"### {title}\n\n{content}"

        def _preview_save_candidate(preview: ToolPreviewResult) -> dict[str, Any]:
            candidate = preview.meta.get("approved_save_candidate")
            if not isinstance(candidate, dict) or not candidate:
                candidate = preview.meta.get("save_candidate")
            return dict(candidate) if isinstance(candidate, dict) else {}

        def _missing_preview_save_candidate_fields(
            tool_name: str,
            preview: ToolPreviewResult,
        ) -> list[str]:
            candidate = _preview_save_candidate(preview)
            if not candidate:
                return ["approved_save_candidate"]
            missing: list[str] = []
            identity = candidate.get("preview_identity")
            if not isinstance(identity, dict) or not identity:
                missing.append("preview_identity")
            else:
                for key in (
                    "plan_id",
                    "candidate_hash",
                    "tool_name",
                    "workspace_id",
                    "execution_target",
                    "path_space",
                    "args_hash",
                ):
                    if not str(identity.get(key) or "").strip():
                        missing.append(f"preview_identity.{key}")
            if candidate.get("tool_name") != tool_name:
                missing.append("approved_save_candidate.tool_name")
            operations = candidate.get("operations")
            if not isinstance(operations, list) or not operations:
                missing.append("approved_save_candidate.operations")
            return missing

        def _preview_failure_reason(preview: ToolPreviewResult) -> str:
            code = str(preview.error_code or "REMOTE_PREVIEW_FAILED")
            message = str(
                preview.error_message
                or preview.error_code
                or "remote peer could not build a tool preview"
            )
            return f"Error [{code}]: {message}" if code else f"Error: {message}"

        def _preview_failure_meta(
            preview: ToolPreviewResult,
            *,
            tool_name: str,
            tool_call_id: str | None,
        ) -> dict[str, Any]:
            code = str(preview.error_code or "REMOTE_PREVIEW_FAILED")
            message = str(
                preview.error_message
                or preview.error_code
                or "remote peer could not build a tool preview"
            )
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.PREVIEW,
                kind=ToolDiagnosticKind.TOOL_RESULT_ERROR,
                code=code,
                message=message,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                repairable=True,
            )
            return {
                "failure_kind": ToolFailureKind.TOOL_RESULT_ERROR.value,
                "code": code,
                "message": message,
                "tool_diagnostics": [diagnostic.to_dict()],
            }

        def _build_remote_preview(
            request: ApprovalRequest,
            tool_call_id: str | None,
        ) -> tuple[list[dict[str, Any]], ToolPreviewResult | None, str | None]:
            if request.tool_name not in REMOTE_PREVIEW_TOOLS:
                section = _args_section(request)
                return ([section] if section else []), None, "preview_unavailable"

            if request.tool_name == "draft_document_commit":
                draft_args = dict(request.tool_args or {})
                diff = str(draft_args.get("diff") or "")
                if diff.strip():
                    return (
                        [
                            {
                                "id": "diff",
                                "title": "Proposed file diff",
                                "kind": "diff",
                                "content": diff,
                                "path": draft_args.get("target_path"),
                                "resolved_path": draft_args.get("target_path"),
                            }
                        ],
                        None,
                        None,
                    )
                raise RemoteToolProtocolError(
                    tool_name=request.tool_name,
                    tool_call_id=tool_call_id,
                    code="DRAFT_DOCUMENT_DIFF_REQUIRED",
                    message="draft document approval requires a preview diff",
                )

            backend = _remote_backend()
            if backend is None:
                raise RemoteToolProtocolError(
                    tool_name=request.tool_name,
                    tool_call_id=tool_call_id,
                    code="REMOTE_BACKEND_MISSING",
                    message="remote peer tool backend is not available",
                )
            if not _peer_supports_tool_preview():
                raise RemoteToolProtocolError(
                    tool_name=request.tool_name,
                    tool_call_id=tool_call_id,
                    code="REMOTE_PREVIEW_REQUIRED",
                    message=(
                        f"remote peer must support tool_preview before approving "
                        f"{request.tool_name}"
                    ),
                )

            preview = backend.preview_tool(request.tool_name, _owner_tool_args(request))
            if not preview.ok:
                return [], preview, _preview_failure_reason(preview)
            missing_candidate_fields = _missing_preview_save_candidate_fields(
                request.tool_name,
                preview,
            )
            if missing_candidate_fields and (preview.sections or preview.diff.strip()):
                raise RemoteToolProtocolError(
                    tool_name=request.tool_name,
                    tool_call_id=tool_call_id,
                    code="REMOTE_PREVIEW_SAVE_CANDIDATE_REQUIRED",
                    message=(
                        "remote peer preview must include approved_save_candidate "
                        "before approval: "
                        + ", ".join(missing_candidate_fields)
                    ),
                )
            if preview.sections:
                return preview.sections, preview, None
            if preview.diff.strip():
                return (
                    [
                        {
                            "id": "diff",
                            "title": "Proposed file diff",
                            "kind": "diff",
                            "content": preview.diff,
                            "path": preview.resolved_path,
                            "resolved_path": preview.resolved_path,
                            "original_text": preview.original_text,
                            "modified_text": preview.modified_text,
                        }
                    ],
                    preview,
                    None,
                )
            raise RemoteToolProtocolError(
                tool_name=request.tool_name,
                tool_call_id=tool_call_id,
                code="REMOTE_PREVIEW_EMPTY",
                message="remote peer preview did not include diff or sections",
            )

        def _emit_protocol_error(
            *,
            tool_name: str,
            tool_call_id: str | None,
            code: str,
            message: str,
        ) -> None:
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.PROTOCOL,
                kind=ToolDiagnosticKind.TOOL_PROTOCOL_ERROR,
                code=code,
                message=message,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            _record_remote_diagnostics(
                [diagnostic],
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            remote_session.append_event(
                "tool_call_protocol_error",
                {
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "code": code,
                    "message": message,
                    "failure_kind": ToolFailureKind.TOOL_PROTOCOL_ERROR.value,
                    "tool_diagnostics": [diagnostic.to_dict()],
                },
            )

        class _RemoteApprovalProvider(ApprovalProvider):
            def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
                return self._request_remote_decision(request)

            @property
            def handler(self) -> Callable[[PendingApproval], None]:
                return self._handle_pending_approval

            def _handle_pending_approval(self, pending: PendingApproval) -> None:
                pending.resolve(self._request_remote_decision(pending.request))

            def _request_remote_decision(
                self, request: ApprovalRequest
            ) -> ApprovalDecision:
                approval_id = str(uuid.uuid4())
                tool_call_id = str(request.metadata.get("tool_call_id") or "")
                try:
                    sections, preview, preview_error = _build_remote_preview(
                        request, tool_call_id or None
                    )
                except RemoteToolProtocolError as exc:
                    _emit_protocol_error(
                        tool_name=exc.tool_name,
                        tool_call_id=exc.tool_call_id,
                        code=exc.code,
                        message=exc.message,
                    )
                    raise
                if preview is not None and not preview.ok:
                    preview_meta = _preview_failure_meta(
                        preview,
                        tool_name=request.tool_name,
                        tool_call_id=tool_call_id or None,
                    )
                    _record_remote_diagnostics(
                        preview_meta.get("tool_diagnostics", []),
                        tool_name=request.tool_name,
                        tool_call_id=tool_call_id or None,
                    )
                    return ApprovalDecision.deny_once(
                        preview_error,
                        meta=preview_meta,
                    )
                payload = {
                    "approval_id": approval_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": request.tool_name,
                    "tool_source": request.tool_source,
                    "reason": request.reason,
                    "intent": _approval_intent(request),
                    "tool_args": _approval_tool_args(request),
                    "sections": sections,
                    "preview_unavailable": bool(preview_error) or not sections,
                    "preview_error": preview_error,
                    **(
                        {"approved_save_candidate": _preview_save_candidate(preview)}
                        if preview is not None and preview.ok and _preview_save_candidate(preview)
                        else {}
                    ),
                    **_approval_lifecycle_payload(request),
                    "format": "markdown",
                    "content": "\n\n".join(
                        part
                        for part in [
                            f"## Approval required: {request.tool_name}",
                            _approval_intent(request) or "",
                            f"Tool `{request.tool_name}` from source `{request.tool_source}` requires approval.",
                            request.reason or "",
                            *[_section_markdown(section) for section in sections],
                        ]
                        if part
                    ),
                }
                remote_session.register_approval(approval_id, payload)
                remote_session.append_event("approval_request", payload)
                decision, reason, decision_meta = remote_session.wait_approval(approval_id)
                decision_meta = dict(decision_meta or {})
                remote_session.append_event(
                    "approval_resolved",
                    {
                        "approval_id": approval_id,
                        "tool_call_id": tool_call_id,
                        "decision": decision,
                        "reason": reason,
                        **(
                            {"approved_save_candidate": decision_meta["approved_save_candidate"]}
                            if decision == "allow_once"
                            and isinstance(decision_meta.get("approved_save_candidate"), dict)
                            else {}
                        ),
                    },
                )
                if decision == "allow_once":
                    backend = _remote_backend()
                    if backend is not None and preview is not None and preview.ok:
                        approved_candidate = decision_meta.get("approved_save_candidate")
                        backend.remember_approved_candidate(
                            request.tool_name,
                            _owner_tool_args(request),
                            dict(approved_candidate)
                            if isinstance(approved_candidate, dict) and approved_candidate
                            else _preview_save_candidate(preview),
                        )
                    return ApprovalDecision.allow_once(reason, meta=decision_meta)
                denial_diagnostic = tool_diagnostic_from_failure(
                    stage=ToolDiagnosticStage.APPROVAL,
                    kind=ToolDiagnosticKind.APPROVAL_DENIED,
                    code="approval_denied",
                    message=reason
                    or f"Tool '{request.tool_name}' denied by approval provider",
                    tool_name=request.tool_name,
                    tool_call_id=tool_call_id or None,
                )
                return ApprovalDecision.deny_once(
                    reason,
                    meta={
                        "failure_kind": ToolFailureKind.APPROVAL_DENIED.value,
                        "tool_diagnostics": [denial_diagnostic.to_dict()],
                    },
                )

        def _on_remote_stream(
            tool_name: str, chunk: Any, tool_call_id: str | None = None
        ) -> None:
            resolved_tool_call_id = tool_call_id or getattr(chunk, "tool_call_id", None)
            if not resolved_tool_call_id:
                _emit_protocol_error(
                    tool_name=tool_name,
                    tool_call_id=None,
                    code="REMOTE_TOOL_CALL_ID_REQUIRED",
                    message="remote peer tool stream is missing tool_call_id",
                )
                return
            remote_session.append_event(
                "tool_call_stream",
                {
                    "tool_name": tool_name,
                    "tool_call_id": resolved_tool_call_id,
                    "format": "plain",
                    "stream": getattr(chunk, "chunk_type", "stdout"),
                    "content": getattr(chunk, "data", ""),
                    "meta": getattr(chunk, "meta", {}),
                },
            )

        backend = _remote_backend()
        if backend is not None and isinstance(backend.context, ExecutionContext):
            backend.context.remote_stream_handler = _on_remote_stream

        def _on_agent_event(event: AgentEvent) -> None:
            if event.event_type == AgentEventType.SESSION_RUN_START:
                _append_agent_event(
                    event,
                    "session_run_start",
                    {
                        "prompt": event.data.get("user_input") or prompt,
                        "mode": getattr(remote_session, "mode", None),
                        "workflow_mode": getattr(remote_session, "workflow_mode", None),
                        "taskflow_id": getattr(remote_session, "taskflow_id", None),
                        "provider_id": getattr(remote_session, "provider_id", None),
                        "model_id": getattr(remote_session, "model_id", None),
                        "locale": getattr(remote_session, "locale", None),
                        "mentions": getattr(remote_session, "mentions", []),
                    },
                )
                return
            if event.event_type == AgentEventType.STREAM_TOKEN:
                content = event.data.get("token", "")
                if content:
                    assistant_stream_parts.append(str(content))
                    stream_observability["provider_output_count"] = (
                        int(stream_observability.get("provider_output_count", 0) or 0) + 1
                    )
                    stream_observability["last_body_chunk_at"] = event.timestamp
                    _append_agent_event(
                        event,
                        "assistant_delta",
                        {"format": "markdown", "content": content},
                    )
                    _emit_stream_observability("assistant_delta")
                return
            if event.event_type == AgentEventType.REASONING_TOKEN:
                content = event.data.get("token", "")
                if content:
                    reasoning_stream_parts.append(str(content))
                    stream_observability["provider_reasoning_count"] = (
                        int(stream_observability.get("provider_reasoning_count", 0) or 0) + 1
                    )
                    stream_observability["last_reasoning_chunk_at"] = event.timestamp
                    _append_agent_event(
                        event,
                        "reasoning_delta",
                        {"format": "markdown", "content": content},
                    )
                    _emit_stream_observability("reasoning_delta")
                return
            if event.event_type == AgentEventType.USAGE_UPDATE:
                _append_agent_event(event, "usage_update", event.data)
                return
            if event.event_type == AgentEventType.RUNTIME_STATUS:
                _append_agent_event(event, "runtime_status", event.data)
                return
            if event.event_type == AgentEventType.LIFECYCLE_HOOK:
                _append_agent_event(event, "lifecycle_hook", event.data)
                return
            if event.event_type == AgentEventType.SESSION_RUN_END:
                response = event.data.get("response", "")
                if event.data.get("render_response", True) and response:
                    _append_final_stream_content(str(response))
                _emit_stream_observability("session_run_end", force=True)
                return
            if event.event_type == AgentEventType.PROVIDER_STREAM_INTERRUPTED:
                _append_agent_event(event, "provider_stream_interrupted", event.data)
                _emit_stream_observability("provider_stream_interrupted", force=True)
                return
            if event.event_type == AgentEventType.PROVIDER_STREAM_RECOVERING:
                _append_agent_event(event, "provider_stream_recovering", event.data)
                return
            if event.event_type == AgentEventType.PROVIDER_STREAM_RECOVERED:
                _append_agent_event(event, "provider_stream_recovered", event.data)
                return
            if event.event_type == AgentEventType.SESSION_RUN_INTERRUPTED:
                session_run_interrupted_emitted["value"] = True
                remote_session.register_recovery(dict(event.data))
                _append_final_stream_content()
                _append_agent_event(event, "session_run_interrupted", event.data)
                _emit_stream_observability("session_run_interrupted", force=True)
                return
            if event.event_type == AgentEventType.TOOL_CALL_DELTA:
                stream_observability["provider_tool_delta_count"] = (
                    int(stream_observability.get("provider_tool_delta_count", 0) or 0) + 1
                )
                stream_observability["last_tool_delta_at"] = event.timestamp
                _append_agent_event(
                    event,
                    "tool_call_delta",
                    {
                        "index": event.data.get("index", 0),
                        "tool_name": event.data.get("tool_name") or event.tool_name,
                        "tool_call_id": event.data.get("tool_call_id") or event.tool_call_id,
                        "tool_source": event.data.get("tool_source"),
                        "arguments_preview": _tool_arguments_preview(
                            event.data.get("arguments_preview")
                        ),
                        "status": "preparing",
                        "started_at": event.timestamp,
                    },
                )
                _emit_stream_observability("tool_call_delta")
                return
            if event.event_type in {
                AgentEventType.TOOL_ARGUMENTS_COMPLETE,
                AgentEventType.TOOL_ARGUMENTS_VALID,
                AgentEventType.TOOL_ARGUMENTS_INVALID,
                AgentEventType.MUTATION_PREVIEWING,
                AgentEventType.MUTATION_PREVIEW_READY,
                AgentEventType.MUTATION_PREVIEW_FAILED,
            }:
                if event.event_type == AgentEventType.TOOL_ARGUMENTS_INVALID:
                    _record_patch_syntax_error(event.data)
                    _emit_stream_observability("patch_syntax_error", force=True)
                if event.event_type == AgentEventType.MUTATION_PREVIEW_FAILED:
                    _record_patch_semantic_error(event.data)
                    _emit_stream_observability("patch_semantic_error", force=True)
                _append_agent_event(event, event.event_type.value, event.data)
                return
            if event.event_type == AgentEventType.FILE_CHANGE_STARTED:
                _append_agent_event(event, "file_change_started", event.data)
                return
            if event.event_type == AgentEventType.FILE_CHANGE_PATCH_UPDATED:
                _append_agent_event(event, "file_change_patch_updated", event.data)
                return
            if event.event_type == AgentEventType.FILE_CHANGE_APPROVAL_REQUESTED:
                _append_agent_event(event, "file_change_approval_requested", event.data)
                return
            if event.event_type == AgentEventType.FILE_CHANGE_APPROVAL_RESOLVED:
                _append_agent_event(event, "file_change_approval_resolved", event.data)
                return
            if event.event_type == AgentEventType.FILE_CHANGE_COMPLETED:
                _append_agent_event(event, "file_change_completed", event.data)
                return
            if event.event_type == AgentEventType.DOCUMENT_DRAFT_STARTED:
                _append_agent_event(event, "document_draft_started", event.data)
                return
            if event.event_type == AgentEventType.DOCUMENT_DRAFT_PREVIEW_CHUNK:
                if event.data.get("content"):
                    draft_content_emitted["value"] = True
                stream_observability["draft_preview_chunk_count"] = (
                    int(stream_observability.get("draft_preview_chunk_count", 0) or 0) + 1
                )
                if event.data.get("flush_latency_ms") is not None:
                    stream_observability["last_draft_preview_flush_latency_ms"] = event.data.get(
                        "flush_latency_ms"
                    )
                _append_agent_event(
                    event,
                    "document_draft_preview_chunk",
                    event.data,
                    live_only=True,
                )
                _emit_stream_observability("document_draft_preview_chunk")
                return
            if event.event_type == AgentEventType.DOCUMENT_DRAFT_PROGRESS:
                _append_agent_event(event, "document_draft_progress", event.data)
                return
            if event.event_type == AgentEventType.DOCUMENT_DRAFT_SNAPSHOT:
                if event.data.get("content"):
                    draft_content_emitted["value"] = True
                _append_agent_event(event, "document_draft_snapshot", event.data)
                return
            if event.event_type == AgentEventType.DOCUMENT_DRAFT_COMMIT_REQUESTED:
                _append_agent_event(event, "document_draft_commit_requested", event.data)
                return
            if event.event_type == AgentEventType.DOCUMENT_DRAFT_COMMITTED:
                _append_agent_event(event, "document_draft_committed", event.data)
                return
            if event.event_type == AgentEventType.DOCUMENT_DRAFT_FAILED:
                _append_agent_event(event, "document_draft_failed", event.data)
                return
            if event.event_type == AgentEventType.DOCUMENT_DRAFT_CANCELLED:
                _append_agent_event(event, "document_draft_cancelled", event.data)
                return
            if event.event_type == AgentEventType.DRAFT_BODY_STALLED:
                _append_agent_event(event, "draft_body_stalled", event.data)
                return
            if event.event_type == AgentEventType.DRAFT_INTERRUPTED_RECOVERABLE:
                _append_agent_event(event, "draft_interrupted_recoverable", event.data)
                return
            if event.event_type == AgentEventType.TOOL_CALL_START:
                if event.tool_name and event.tool_call_id:
                    active_tool_calls_by_name.setdefault(event.tool_name, []).append(
                        event.tool_call_id
                    )
                _append_agent_event(
                    event,
                    "tool_call_start",
                    {
                        "tool_name": event.tool_name,
                        "tool_call_id": event.tool_call_id,
                        "tool_args": event.tool_args or {},
                        "tool_source": event.data.get("tool_source"),
                        "index": event.data.get("index"),
                        "started_at": event.timestamp,
                        **_tool_identity_event_payload(event),
                    },
                )
                return
            elif event.event_type == AgentEventType.TOOL_CALL_END:
                if not event.tool_call_id:
                    _emit_protocol_error(
                        tool_name=event.tool_name or "tool",
                        tool_call_id=None,
                        code="REMOTE_TOOL_CALL_ID_REQUIRED",
                        message="remote peer tool end event is missing tool_call_id",
                    )
                    return
                if event.tool_name and event.tool_call_id:
                    candidates = active_tool_calls_by_name.get(event.tool_name)
                    if candidates and event.tool_call_id in candidates:
                        candidates.remove(event.tool_call_id)
                _append_agent_event(
                    event,
                    "tool_call_end",
                    {
                        "tool_name": event.tool_name,
                        "tool_call_id": event.tool_call_id,
                        "tool_result": event.tool_result or "",
                        "tool_source": event.data.get("tool_source"),
                        "index": event.data.get("index"),
                        "meta": event.data.get("meta") or {},
                        "ended_at": event.timestamp,
                        **_tool_identity_event_payload(event),
                    },
                )
                return
            elif event.event_type == AgentEventType.TOOL_CALL_PROTOCOL_ERROR:
                payload = dict(event.data)
                _emit_protocol_error(
                    tool_name=str(payload.get("tool_name") or event.tool_name or "tool"),
                    tool_call_id=payload.get("tool_call_id") or event.tool_call_id,
                    code=str(payload.get("code") or "REMOTE_PROTOCOL_ERROR"),
                    message=str(payload.get("message") or event.error_message or "remote tool protocol error"),
                )
                return
            elif event.event_type == AgentEventType.ERROR:
                _append_agent_event(
                    event,
                    "error",
                    {"message": event.error_message or "unknown error"},
                )
                return
            elif event.event_type == AgentEventType.AGENT_RELATION_COMPLETED:
                _append_agent_event(event, "agent_relation_completed", event.data)
                return

        previous_approval = peer_agent.approval_provider
        peer_context_manager = getattr(peer_agent, "context", None)
        previous_context_bus = getattr(peer_context_manager, "_ui_bus", None)
        if peer_context_manager is not None:
            peer_context_manager._ui_bus = context_event_bus
        peer_agent.add_event_handler(_on_agent_event)
        peer_agent.approval_provider = _RemoteApprovalProvider()
        previous_runtime_config = getattr(peer_agent, "runtime_config", None)
        localized_runtime_config = _runtime_config_with_chat_locale(
            previous_runtime_config,
            getattr(remote_session, "locale", None),
        )
        if localized_runtime_config is not previous_runtime_config:
            setattr(peer_agent, "runtime_config", localized_runtime_config)
        try:
            result = _agent_chat(
                peer_agent,
                prompt,
                clear_stop_request=not getattr(
                    remote_session, "cancel_requested", False
                ),
            )
            _flush_output()
            _save_peer_session(peer_agent, peer_id)
            _append_final_stream_content(str(result) if result else None)
            if getattr(remote_session, "cancel_requested", False):
                remote_session.append_event(
                    "session_run_cancelled",
                    {"reason": getattr(remote_session, "cancel_reason", None)},
                )
            if not session_run_interrupted_emitted["value"]:
                remote_session.append_event(
                    "session_run_end",
                    {
                        "response": result,
                        "response_rendered": assistant_content_emitted["value"]
                        or draft_content_emitted["value"],
                    },
                )
        except RemoteToolProtocolError as exc:
            _flush_output()
            _save_peer_session(peer_agent, peer_id)
            _append_final_stream_content()
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.CHAT,
                kind=ToolDiagnosticKind.CHAT_TERMINAL_ERROR,
                code=exc.code,
                message=exc.message,
                tool_name=exc.tool_name,
                tool_call_id=exc.tool_call_id,
            )
            _record_remote_diagnostics(
                [diagnostic],
                tool_name=exc.tool_name,
                tool_call_id=exc.tool_call_id,
            )
            failure_payload = {
                "message": exc.message,
                "code": exc.code,
                "recoverable": False,
                "failure_kind": ToolFailureKind.CHAT_TERMINAL_ERROR.value,
                "tool_diagnostics": [diagnostic.to_dict()],
            }
            remote_session.append_event("error", failure_payload)
            remote_session.append_event("session_run_failed", failure_payload)
        except Exception as exc:
            _flush_output()
            _save_peer_session(peer_agent, peer_id)
            _append_final_stream_content()
            llm = getattr(peer_agent, "llm", None)
            provider_config = getattr(llm, "provider_config", None)
            provider_diagnostic = provider_error_envelope(
                provider_config,
                str(getattr(llm, "model", "") or ""),
                exc,
                code="REMOTE_CHAT_ERROR",
            )
            has_actionable_provider_diagnostic = any(
                provider_diagnostic.get(key)
                for key in (
                    "suspected_reason",
                    "recommended_action",
                    "upstream_status",
                    "request_param_keys",
                )
            )
            failure_message = str(
                provider_diagnostic.get("message") if has_actionable_provider_diagnostic else exc
            )
            diagnostic = tool_diagnostic_from_failure(
                stage=ToolDiagnosticStage.CHAT,
                kind=ToolDiagnosticKind.CHAT_TERMINAL_ERROR,
                code="REMOTE_CHAT_ERROR",
                message=failure_message,
            )
            _record_remote_diagnostics([diagnostic])
            failure_payload = {
                **(provider_diagnostic if has_actionable_provider_diagnostic else {}),
                "message": failure_message,
                "code": "REMOTE_CHAT_ERROR",
                "recoverable": False,
                "failure_kind": ToolFailureKind.CHAT_TERMINAL_ERROR.value,
                "tool_diagnostics": [diagnostic.to_dict()],
            }
            if has_actionable_provider_diagnostic:
                failure_payload["provider_diagnostic"] = provider_diagnostic
            diagnostic_path = getattr(exc, "llm_diagnostic_path", None)
            if diagnostic_path:
                failure_payload["diagnostic_path"] = str(diagnostic_path)
            failure_payload["error_type"] = type(exc).__name__
            provider_phase = getattr(exc, "provider_error_phase", None)
            if provider_phase:
                failure_payload["provider_error_phase"] = str(provider_phase)
            remote_session.append_event("error", failure_payload)
            remote_session.append_event("session_run_failed", failure_payload)
        finally:
            if localized_runtime_config is not previous_runtime_config:
                setattr(peer_agent, "runtime_config", previous_runtime_config)
            if peer_context_manager is not None:
                peer_context_manager._ui_bus = previous_context_bus
            peer_agent.approval_provider = previous_approval
            if hasattr(peer_agent, "set_follow_up_consumed_handler"):
                peer_agent.set_follow_up_consumed_handler(None)
            if hasattr(remote_session, "set_follow_up_callback"):
                remote_session.set_follow_up_callback(None)
            if hasattr(remote_session, "set_follow_up_cancel_callback"):
                remote_session.set_follow_up_cancel_callback(None)
            try:
                peer_agent._event_handlers.remove(_on_agent_event)
            except ValueError:
                pass
            interactive_run_limiter.release_agent_slot(runtime_agent_id)
            renderer.close()

    runner._relay_http_service.set_chat_command_handler(_dispatch_chat_command)
    runner._relay_http_service.set_session_run_events_handler(_stream_session_run)


def _structured_ui_event_type(event) -> str:
    data = getattr(event, "data", {}) or {}
    if data.get("event_type") == "lifecycle_hook":
        return "lifecycle_hook"
    if event.kind == UIEventKind.CONTEXT and (
        data.get("context_kind") == "memory_injection"
        or data.get("schema") == "memory_context.v1"
    ):
        return "memory_context"
    return {
        UIEventKind.VIEW: "view",
        UIEventKind.CONTEXT: "context_event",
        UIEventKind.REMOTE: "remote_event",
        UIEventKind.MCP: "mcp_event",
        UIEventKind.MODEL: "model_event",
        UIEventKind.SESSION: "session_event",
        UIEventKind.COMMAND: "command_event",
        UIEventKind.APPROVAL: "approval_event",
        UIEventKind.SYSTEM: "system_event",
        UIEventKind.AGENT: "agent_event",
    }.get(event.kind, "ui_event")


def _structured_ui_event_payload(event) -> dict[str, Any]:
    return {
        "message": event.message,
        "level": event.level.value,
        "kind": event.kind.value,
        "timestamp": event.timestamp,
        **dict(event.data),
    }

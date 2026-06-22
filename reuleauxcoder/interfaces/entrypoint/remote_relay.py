"""Remote relay bootstrap and peer chat binding helpers."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import inspect
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Callable

from rich.console import Console

from reuleauxcoder.app.runtime.session_state import (
    apply_agent_default_model,
    apply_agent_config_model,
    apply_session_model_override,
    apply_session_runtime_state,
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
from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.agent.runtime_boundary import runtime_agent_run_id
from reuleauxcoder.domain.agent.tool_diagnostics import (
    ToolDiagnostic,
    diagnostic_to_dict,
)
from reuleauxcoder.domain.approval import PendingApproval
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
from labrastro_server.interfaces.http.remote.protocol import (
    ChatCommandDispatchRequest,
    ChatCommandDispatchResponse,
)
from labrastro_server.relay.server import RelayServer
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
    ReuleauxCoderExecutorBackend,
)
from reuleauxcoder.domain.agent_runtime.models import ExecutorType, WorkerKind
from reuleauxcoder.extensions.skills.service import SkillsService
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

    service = runner._relay_http_service
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
        peer_agent = runner.dependencies.create_agent(
            peer_llm, [], current_config
        )
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
        setattr(peer_agent, "runtime_execution_target", "local_peer")
        setattr(peer_agent, "runtime_peer_context", peer_context)
        setattr(peer_agent, "runtime_working_directory", runtime_cwd)
        setattr(peer_agent, "runtime_workspace_root", workspace_root)

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

    def _agent_run_binding_for_request(request: Any) -> Any | None:
        service = runner._relay_http_service
        runtime = getattr(service, "runtime_control_plane", None)
        if runtime is None:
            return None
        metadata = dict(getattr(request, "metadata", {}) or {})
        expected_session_run_id = str(metadata.get("session_run_id") or "").strip()
        expected_branch_id = str(metadata.get("branch_binding_id") or "").strip()
        bindings = runtime.list_session_run_bindings(agent_run_id=request.task_id)
        for binding in bindings:
            if expected_session_run_id and binding.session_run_id != expected_session_run_id:
                continue
            if expected_branch_id and binding.branch_binding_id != expected_branch_id:
                continue
            return binding
        return None

    def _wait_agent_run_binding_for_request(
        request: Any,
        *,
        timeout_sec: float = 1.0,
    ) -> Any | None:
        deadline = time.time() + max(0.0, timeout_sec)
        while True:
            binding = _agent_run_binding_for_request(request)
            if binding is not None:
                return binding
            if time.time() >= deadline:
                return None
            time.sleep(0.01)

    def _session_run_writer_for_binding(binding: Any) -> Any:
        service = runner._relay_http_service
        if service is None:
            raise RuntimeError("remote relay service unavailable")
        session = service._get_session_run(binding.session_run_id)
        if session is None:
            raise RuntimeError("session_run_not_found")
        return session.scoped_writer(
            branch_binding_id=binding.branch_binding_id,
            agent_run_id=binding.agent_run_id,
        )

    def _emit_agent_session_run_event(
        peer_agent: Any,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        event = AgentEvent.session_run_event(event_type, payload)
        emit_event = getattr(peer_agent, "_emit_event", None)
        if callable(emit_event):
            emit_event(event)
            return
        for handler in list(getattr(peer_agent, "_event_handlers", []) or []):
            handler(event)

    def _install_session_run_ui_bus(peer_agent: Any) -> None:
        session_bus = UIEventBus(
            lifecycle_dispatcher=getattr(peer_agent, "lifecycle_dispatcher", None)
        )

        def _forward_ui_event(event: Any) -> None:
            _emit_agent_session_run_event(
                peer_agent,
                _structured_ui_event_type(event),
                _structured_ui_event_payload(event),
            )

        session_bus.subscribe(_forward_ui_event, replay_history=False)
        context = getattr(peer_agent, "context", None)
        if context is not None:
            context._ui_bus = session_bus

    def _install_session_run_command_dispatch(
        peer_agent: Any,
        peer_id: str,
    ) -> None:
        original_chat = getattr(peer_agent, "chat", None)
        if not callable(original_chat):
            return

        def _call_original_chat(prompt: str, *args: Any, **kwargs: Any) -> Any:
            signature = inspect.signature(original_chat)
            if "clear_stop_request" in signature.parameters:
                return original_chat(
                    prompt,
                    clear_stop_request=bool(kwargs.get("clear_stop_request", True)),
                )
            return original_chat(prompt)

        def _session_run_chat(prompt: str, *args: Any, **kwargs: Any) -> Any:
            command_text = str(prompt or "")
            if command_text.startswith("/"):
                response = _dispatch_vscode_command(
                    peer_agent,
                    peer_id,
                    command_text,
                    invalid_as_chat=True,
                )
                for event in list(response.events or []):
                    if not isinstance(event, dict):
                        continue
                    event_type = str(event.get("type") or "").strip()
                    payload = (
                        dict(event.get("payload"))
                        if isinstance(event.get("payload"), dict)
                        else {}
                    )
                    if event_type:
                        _emit_agent_session_run_event(peer_agent, event_type, payload)
                if response.action != "chat":
                    return ""
            return _call_original_chat(command_text, *args, **kwargs)

        peer_agent.chat = _session_run_chat

    def _remote_stream_handler_for_binding(binding: Any) -> Callable[..., None]:
        writer = _session_run_writer_for_binding(binding)

        def _handle(
            tool_name: str,
            chunk: Any,
            tool_call_id: str | None = None,
        ) -> None:
            resolved_tool_call_id = str(
                getattr(chunk, "tool_call_id", None)
                or tool_call_id
                or ""
            ).strip()
            if not resolved_tool_call_id:
                writer.append_event(
                    "tool_call_protocol_error",
                    {
                        "tool_name": str(tool_name or ""),
                        "tool_call_id": None,
                        "code": "LOCAL_ACTION_TOOL_CALL_ID_REQUIRED",
                        "message": "Local action tool stream chunks must include tool_call_id.",
                        "recoverable": False,
                    },
                )
                return
            writer.append_event(
                "tool_call_stream",
                {
                    "tool_name": str(tool_name or ""),
                    "tool_call_id": resolved_tool_call_id,
                    "stream": str(getattr(chunk, "chunk_type", "") or ""),
                    "content": str(getattr(chunk, "data", "") or ""),
                    "format": "plain",
                    "meta": dict(getattr(chunk, "meta", {}) or {}),
                },
            )

        return _handle

    def _remote_peer_ready_payload(peer_agent: Agent, peer_id: str) -> dict[str, Any]:
        current_config = _current_config()
        model = str(getattr(getattr(peer_agent, "llm", None), "model", "") or "")
        if not model and current_config is not None:
            model = str(resolve_model_runtime(current_config).model or "")
        return {
            "peer_id": peer_id,
            "session_id": str(getattr(peer_agent, "current_session_id", "") or ""),
            "fingerprint": str(getattr(peer_agent, "session_fingerprint", "") or ""),
            "mode": str(getattr(peer_agent, "active_mode", "") or ""),
            "model": model,
            "main_agent_id": str(getattr(peer_agent, "main_agent_id", "") or ""),
            "agent_config_id": str(getattr(peer_agent, "agent_config_id", "") or ""),
            "effective_capabilities": dict(
                getattr(peer_agent, "effective_capabilities", {}) or {}
            ),
        }

    def _configure_peer_agent_for_session_run(
        peer_agent: Agent,
        request: Any,
    ) -> None:
        metadata = dict(getattr(request, "metadata", {}) or {})
        mode = str(metadata.get("mode") or "").strip()
        if mode:
            set_mode = getattr(peer_agent, "set_mode", None)
            if callable(set_mode):
                set_mode(mode)
        locale = str(metadata.get("locale") or "").strip()
        if locale:
            next_config = _runtime_config_with_chat_locale(
                getattr(peer_agent, "runtime_config", _current_config()),
                locale,
            )
            if next_config is not None:
                setattr(peer_agent, "runtime_config", next_config)
            setattr(peer_agent, "locale", locale)

    def _configure_server_agent_for_session_run(
        server_agent: Agent,
        request: Any,
    ) -> None:
        _configure_peer_agent_for_session_run(server_agent, request)
        current_config = _current_config()
        if current_config is None:
            return
        binding = _agent_run_binding_for_request(request)
        remote_session = (
            service._get_session_run(binding.session_run_id)
            if binding is not None
            else None
        )
        if remote_session is None:
            return
        provider_id = str(getattr(remote_session, "provider_id", "") or "").strip()
        model_id = str(getattr(remote_session, "model_id", "") or "").strip()
        if not provider_id or not model_id:
            return
        apply_session_model_override(
            current_config,
            server_agent,
            provider=provider_id,
            model=model_id,
            parameters=dict(getattr(remote_session, "model_parameters", {}) or {}),
        )

    def _create_server_agent_for_request(request: Any) -> Agent:
        current_config = _current_config()
        if current_config is None:
            return agent
        server_llm = runner.dependencies.create_llm(current_config)
        server_llm.ui_bus = ui_bus
        tool_backend = runner.dependencies.create_tool_backend(current_config, ui_bus)
        server_tools = runner.dependencies.load_tools(tool_backend)
        server_agent = runner.dependencies.create_agent(
            server_llm,
            server_tools,
            current_config,
        )
        setattr(server_agent, "runtime_config", current_config)
        setattr(
            server_agent,
            "capability_catalog",
            runner.build_capability_catalog(current_config),
        )
        setattr(server_agent, "agent_run_control_plane", service.runtime_control_plane)
        setattr(server_agent, "skills_service", skills_service)
        setattr(server_agent, "skills_catalog", getattr(agent, "skills_catalog", ""))
        runner._register_hooks(server_agent, current_config)
        runner._wire_agent_tool_parent(server_agent)
        restore_config_runtime_defaults(current_config, server_agent)
        _apply_chat_entrypoint_runtime(server_agent, current_config)
        binding = _agent_run_binding_for_request(request)
        if binding is not None and binding.session_id:
            setattr(server_agent, "current_session_id", binding.session_id)
        _configure_server_agent_for_session_run(server_agent, request)
        return server_agent

    def _executor_failure_payload(exc: Exception) -> dict[str, Any]:
        payload = ReuleauxCoderExecutorBackend._exception_payload(exc)
        payload.setdefault("error_type", type(exc).__name__)
        return payload

    def _start_server_session_run_worker() -> None:
        service = runner._relay_http_service
        runtime = getattr(service, "runtime_control_plane", None)
        if service is None or runtime is None:
            return
        existing = getattr(service, "_server_session_run_worker_thread", None)
        if isinstance(existing, threading.Thread) and existing.is_alive():
            return
        stop_event = threading.Event()
        setattr(service, "_server_session_run_worker_stop", stop_event)
        backend = ReuleauxCoderExecutorBackend(
            create_agent=_create_server_agent_for_request
        )

        def _run_claim(claim: Any) -> None:
            result: ExecutorRunResult
            try:
                result = backend.start(claim.executor_request)
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                result = ExecutorRunResult(
                    task_id=claim.task.id,
                    status="failed",
                    output="",
                    events=[
                        ExecutorEvent.error(
                            message,
                            **_executor_failure_payload(exc),
                        ),
                    ],
                    error=message,
                )
            runtime.complete_claimed_agent_run_activation(
                claim.task.id,
                result,
                request_id=claim.request_id,
                activation_id=claim.activation_id,
                worker_id=claim.worker_id,
                peer_id="server",
                artifacts=list(result.artifacts),
            )

        def _loop() -> None:
            while not stop_event.is_set():
                if runner._relay_http_service is not service:
                    break
                if getattr(service, "_server", None) is None:
                    break
                claim = runtime.claim_agent_run_activation(
                    worker_id="server-session-run-worker",
                    worker_kind=WorkerKind.SERVER_WORKER,
                    executors=[ExecutorType.REULEAUXCODER],
                    peer_features=[
                        "worker_kind:server_worker",
                        "agent_runs.remote_server",
                    ],
                    wait_sec=0,
                )
                if claim is None:
                    stop_event.wait(0.05)
                    continue
                _run_claim(claim)

        thread = threading.Thread(target=_loop, daemon=True)
        setattr(service, "_server_session_run_worker_thread", thread)
        thread.start()

    def _start_local_peer_session_run_worker() -> None:
        service = runner._relay_http_service
        runtime = getattr(service, "runtime_control_plane", None)
        if service is None or runtime is None:
            return
        existing = getattr(service, "_local_peer_session_run_worker_thread", None)
        if isinstance(existing, threading.Thread) and existing.is_alive():
            return
        stop_event = threading.Event()
        setattr(service, "_local_peer_session_run_worker_stop", stop_event)
        active_claims: dict[str, Any] = {}
        active_agents: dict[str, tuple[Agent, str]] = {}

        def _create_agent_for_request(request: Any) -> Agent:
            claim = active_claims.get(request.task_id)
            binding = _wait_agent_run_binding_for_request(request)
            if binding is None:
                raise RuntimeError("session_run_binding_required")
            metadata = dict(getattr(request, "metadata", {}) or {})
            peer_id = str(
                metadata.get("remote_peer_id")
                or getattr(binding, "peer_id", "")
                or ""
            ).strip()
            if not peer_id:
                raise RuntimeError("remote_peer_id_required")
            session_hint = str(metadata.get("session_hint") or "").strip() or None
            remote_session = service._get_session_run(binding.session_run_id)
            peer_agent = _create_peer_agent(
                peer_id,
                remote_stream_handler=_remote_stream_handler_for_binding(binding),
                session_hint=session_hint,
                resume_latest=False,
            )
            _install_session_run_ui_bus(peer_agent)
            _configure_peer_agent_for_session_run(peer_agent, request)
            _bind_main_chat_account_memory_scope(peer_agent, peer_id)
            _install_session_run_command_dispatch(peer_agent, peer_id)
            if remote_session is not None:
                _enable_remote_session_trace_persistence(
                    peer_agent,
                    peer_id,
                    remote_session,
                )
            if claim is not None:
                runtime.pin_claimed_activation_session(
                    request_id=claim.request_id,
                    task_id=claim.task.id,
                    activation_id=claim.activation_id,
                    worker_id=claim.worker_id,
                    peer_id=peer_id,
                    workdir=str(getattr(peer_agent, "runtime_working_directory", "") or ""),
                    executor_session_id=str(
                        getattr(peer_agent, "current_session_id", "") or ""
                    ),
                    metadata={
                        "session_id": str(
                            getattr(peer_agent, "current_session_id", "") or ""
                        ),
                        "peer_id": peer_id,
                        "mode": str(getattr(peer_agent, "active_mode", "") or ""),
                    },
                )
            writer = _session_run_writer_for_binding(binding)
            writer.append_event(
                "remote_peer_ready",
                _remote_peer_ready_payload(peer_agent, peer_id),
            )
            mentions = _normalized_chat_mentions(metadata.get("mentions"))
            if mentions:
                metadata["mention_context"] = {
                    "mentions": [dict(item) for item in mentions],
                    "reference_only": True,
                }
                request.metadata = metadata
                request.prompt = _prompt_with_mention_context(request.prompt, mentions)
            active_agents[request.task_id] = (peer_agent, peer_id)
            return peer_agent

        backend = ReuleauxCoderExecutorBackend(
            create_agent=_create_agent_for_request
        )

        def _run_claim(claim: Any, peer_id: str) -> None:
            active_claims[claim.task.id] = claim
            try:
                try:
                    result = backend.start(claim.executor_request)
                except Exception as exc:
                    message = str(exc) or type(exc).__name__
                    result = ExecutorRunResult(
                        task_id=claim.task.id,
                        status="failed",
                        output="",
                        events=[
                            ExecutorEvent.error(
                                message,
                                **_executor_failure_payload(exc),
                            ),
                        ],
                        error=message,
                    )
                agent_pair = active_agents.get(claim.task.id)
                if agent_pair is not None:
                    peer_agent, agent_peer_id = agent_pair
                    _save_peer_session(peer_agent, agent_peer_id)
                runtime.complete_claimed_agent_run_activation(
                    claim.task.id,
                    result,
                    request_id=claim.request_id,
                    activation_id=claim.activation_id,
                    worker_id=claim.worker_id,
                    peer_id=peer_id,
                    artifacts=list(result.artifacts),
                )
            finally:
                active_claims.pop(claim.task.id, None)
                active_agents.pop(claim.task.id, None)

        def _loop() -> None:
            while not stop_event.is_set():
                if runner._relay_http_service is not service:
                    break
                if getattr(service, "_server", None) is None:
                    break
                claimed_any = False
                for peer in list(relay_server.registry.list_online()):
                    features = [str(item) for item in getattr(peer, "features", []) or []]
                    if "agent_runs.local_workspace" not in set(features):
                        continue
                    claim = runtime.claim_agent_run_activation(
                        worker_id=f"local-peer-session-run:{peer.peer_id}",
                        worker_kind=WorkerKind.LOCAL_PEER,
                        executors=[ExecutorType.REULEAUXCODER],
                        peer_id=peer.peer_id,
                        peer_features=features,
                        workspace_root=getattr(peer, "workspace_root", None),
                        wait_sec=0,
                    )
                    if claim is None:
                        continue
                    claimed_any = True
                    _run_claim(claim, peer.peer_id)
                if not claimed_any:
                    stop_event.wait(0.05)

        thread = threading.Thread(target=_loop, daemon=True)
        setattr(service, "_local_peer_session_run_worker_thread", thread)
        thread.start()

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

    runner._relay_http_service.set_chat_command_handler(_dispatch_chat_command)
    _start_server_session_run_worker()
    _start_local_peer_session_run_worker()


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

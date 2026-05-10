"""Remote relay bootstrap and peer chat binding helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import inspect
from pathlib import Path
import uuid
from typing import Any, Callable

from rich.console import Console

from reuleauxcoder.app.runtime.session_state import (
    apply_agent_default_model,
    apply_session_runtime_state,
    build_session_runtime_state,
    restore_config_runtime_defaults,
)
from reuleauxcoder.app.runtime.agent_runtime import (
    AgentRuntimeCancelled,
    get_agent_runtime_limiter,
)
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.approval import (
    ApprovalDecision,
    ApprovalProvider,
    ApprovalRequest,
    PendingApproval,
)
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.session.models import Session, SessionMetadata, SessionRuntimeState
from labrastro_server.adapters.reuleauxcoder.remote_backend import RemoteRelayToolBackend
from labrastro_server.adapters.reuleauxcoder.mcp_tools import RemotePeerMCPTool
from labrastro_server.interfaces.http.remote.protocol import ChatResponse, ToolPreviewResult
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.extensions.skills.service import SkillsService
from reuleauxcoder.extensions.tools.backend import ExecutionContext
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.cli.registration import CLI_PROFILE
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.entrypoint.dependencies import AppDependencies
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from labrastro_server.services.taskflow.service import (
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


def _snapshot_digest(snapshot: dict[str, Any]) -> str:
    normalized = dict(snapshot)
    normalized.pop("updatedAt", None)
    normalized.pop("updated_at", None)
    return _stable_digest(normalized)


def _message_field(message: Any, field: str) -> Any:
    if isinstance(message, dict):
        return message.get(field)
    return getattr(message, field, None)


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return ""


def _assistant_contents_by_turn(messages: list[Any]) -> list[list[str]]:
    groups: list[list[str]] = []
    current_turn_index = -1
    for message in messages:
        role = _message_field(message, "role")
        if role == "user":
            groups.append([])
            current_turn_index = len(groups) - 1
            continue
        if role != "assistant":
            continue
        content = _message_content_text(_message_field(message, "content"))
        if not content:
            continue
        if current_turn_index < 0:
            groups.append([])
            current_turn_index = 0
        groups[current_turn_index].append(content)
    return groups


def _patch_snapshot_turn_final_text(
    turn: dict[str, Any], authoritative_final: str, turn_index: int
) -> None:
    assistant_messages = turn.get("assistantMessages")
    if not isinstance(assistant_messages, list):
        assistant_messages = []
        turn["assistantMessages"] = assistant_messages
    if not assistant_messages:
        assistant_messages.append(
            {
                "id": f"assistant-final-{turn_index}",
                "role": "assistant",
                "parts": [],
            }
        )
    message = assistant_messages[-1]
    if not isinstance(message, dict):
        message = {"role": "assistant", "parts": []}
        assistant_messages[-1] = message
    parts = message.get("parts")
    if not isinstance(parts, list):
        parts = []
        message["parts"] = parts
    text_part_index = -1
    for index in range(len(parts) - 1, -1, -1):
        part = parts[index]
        if isinstance(part, dict) and part.get("type") == "text":
            text_part_index = index
            break
    current_final = ""
    if text_part_index >= 0 and isinstance(parts[text_part_index], dict):
        text = parts[text_part_index].get("text")
        current_final = text if isinstance(text, str) else ""
    elif isinstance(message.get("text"), str):
        current_final = str(message.get("text"))
    if len(authoritative_final) <= len(current_final):
        return
    if text_part_index >= 0 and isinstance(parts[text_part_index], dict):
        parts[text_part_index] = {
            **parts[text_part_index],
            "type": "text",
            "text": authoritative_final,
            "textFormat": parts[text_part_index].get("textFormat") or "markdown",
        }
    else:
        parts.append(
            {
                "id": f"assistant-final-part-{turn_index}",
                "type": "text",
                "text": authoritative_final,
                "textFormat": "markdown",
            }
        )
    message["text"] = authoritative_final


def _merge_snapshot_with_session_messages(
    snapshot: dict[str, Any], messages: list[Any]
) -> dict[str, Any]:
    turns = snapshot.get("turns")
    if not isinstance(turns, list):
        return snapshot
    assistant_groups = _assistant_contents_by_turn(messages)
    if not assistant_groups:
        return snapshot
    next_snapshot = copy.deepcopy(snapshot)
    next_turns = next_snapshot.get("turns")
    if not isinstance(next_turns, list):
        return snapshot
    for turn_index, turn in enumerate(next_turns):
        if turn_index >= len(assistant_groups) or not isinstance(turn, dict):
            continue
        final_content = assistant_groups[turn_index][-1] if assistant_groups[turn_index] else ""
        if final_content:
            _patch_snapshot_turn_final_text(turn, final_content, turn_index)
    return next_snapshot


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


def switch_session_model(
    config: Config | None,
    session_store: Any,
    fingerprint: str,
    payload: dict[str, Any],
    *,
    snapshot_loader: Callable[[str], tuple[dict[str, Any] | None, str | None]]
    | None = None,
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
            model=getattr(config, "model", None),
            active_mode=getattr(config, "active_mode", None),
            llm_debug_trace=getattr(config, "llm_debug_trace", None),
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
        runtime_state.llm_debug_trace = getattr(config, "llm_debug_trace", None)

    save_runtime_state = getattr(session_store, "save_runtime_state", None)
    messages = list(loaded.messages) if loaded is not None else []
    total_prompt_tokens = loaded.total_prompt_tokens if loaded is not None else 0
    total_completion_tokens = (
        loaded.total_completion_tokens if loaded is not None else 0
    )
    if callable(save_runtime_state):
        save_runtime_state(
            session_id,
            runtime_state.model or getattr(config, "model", ""),
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
            runtime_state.model or getattr(config, "model", ""),
            session_id,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            active_mode=runtime_state.active_mode,
            runtime_state=runtime_state,
            fingerprint=fingerprint,
        )

    saved = session_store.load(session_id)
    snapshot: dict[str, Any] | None = None
    snapshot_error: str | None = None
    if snapshot_loader is not None:
        snapshot, snapshot_error = snapshot_loader(session_id)
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
        "messages": list(saved.messages) if saved is not None else [],
        "snapshot": snapshot,
        "snapshot_error": snapshot_error,
    }


def bind_remote_chat_handler(runner, agent: Agent) -> None:
    """Bind remote chat handlers for interactive peers."""
    if runner._relay_http_service is None or runner._relay_server is None:
        return

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
    agent_runtime_limiter = get_agent_runtime_limiter()
    if config is not None:
        agent_runtime_limiter.configure(
            max_running_agents=config.agent_runtime.max_running_agents,
            max_shells_per_agent=config.agent_runtime.max_shells_per_agent,
        )

    def _current_config() -> Config | None:
        return runtime_config["value"]

    def _reload_config() -> None:
        next_config = runner.dependencies.load_config(runner.options.config_path)
        setattr(next_config, "_source_path", runner.options.config_path)
        if runner.options.server_mode:
            next_config.remote_exec.enabled = True
            next_config.remote_exec.host_mode = True
        errors = next_config.validate()
        if errors:
            raise ValueError("; ".join(errors))
        runtime_config["value"] = next_config
        agent_runtime_limiter.configure(
            max_running_agents=next_config.agent_runtime.max_running_agents,
            max_shells_per_agent=next_config.agent_runtime.max_shells_per_agent,
        )
        if runner._relay_http_service.runtime_control_plane is not None:
            runner._relay_http_service.runtime_control_plane.configure(
                max_running_tasks=next_config.agent_runtime.max_running_agents,
                runtime_snapshot=next_config.agent_runtime.to_runtime_snapshot(
                    next_config.capability_packages
                ),
            )
        runner._relay_http_service.mcp_servers = list(next_config.mcp_servers)
        runner._relay_http_service.mcp_artifact_root = Path(
            next_config.mcp_artifact_root
        )
        runner._relay_http_service.environment_cli_tools = dict(
            next_config.environment.cli_tools
        )
        runner._relay_http_service.environment_skills = dict(
            next_config.environment.skills
        )
        if ui_bus is not None:
            ui_bus.info("Remote admin config reloaded.", kind=UIEventKind.REMOTE)

    runner._relay_http_service.admin_manager.reload_handler = _reload_config

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

    def _load_session_snapshot(session_id: str) -> tuple[dict[str, Any] | None, str | None]:
        return session_store.load_snapshot(session_id)

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
            getattr(getattr(peer_agent, "llm", None), "model", current_config.model),
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
            snapshot, snapshot_error = _load_session_snapshot(session_id)
            return {
                "ok": True,
                "fingerprint": fingerprint,
                "metadata": _session_metadata_payload(loaded),
                "messages": list(loaded.messages),
                "runtime_state": loaded.runtime_state.to_dict(),
                "snapshot": snapshot,
                "snapshot_error": snapshot_error,
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
            agent_config = getattr(current_config.agent_runtime, "agents", {}).get(
                active_mode
            )
            agent_model = getattr(agent_config, "model", None)
            if getattr(agent_model, "configured", False):
                runtime_state.active_model_provider = agent_model.provider
                runtime_state.active_model = agent_model.model
                runtime_state.active_model_display_name = agent_model.display_name
                runtime_state.active_model_parameters = dict(agent_model.parameters)
                runtime_state.model = agent_model.model
            return {
                "ok": True,
                "fingerprint": fingerprint,
                "metadata": {
                    "id": session_id,
                    "model": getattr(current_config, "model", ""),
                    "saved_at": "",
                    "preview": "",
                    "fingerprint": fingerprint,
                },
                "messages": [],
                "runtime_state": runtime_state.to_dict(),
                "snapshot": None,
            }

        if action == "model":
            return switch_session_model(
                current_config,
                session_store,
                fingerprint,
                payload,
                snapshot_loader=_load_session_snapshot,
            )

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
            session_store.delete_snapshot(session_id)
            return {
                "ok": deleted,
                "session_id": session_id,
                "fingerprint": fingerprint,
            }

        if action == "fork":
            source_session_id = str(payload.get("source_session_id") or "").strip()
            if not source_session_id:
                return {
                    "ok": False,
                    "error": "missing_source_session_id",
                    "_status": 400,
                }
            keep_raw = payload.get("keep_through_message_index", -1)
            try:
                keep_through_message_index = int(keep_raw)
            except (TypeError, ValueError):
                return {
                    "ok": False,
                    "error": "invalid_keep_through_message_index",
                    "_status": 400,
                }
            if keep_through_message_index < -1:
                return {
                    "ok": False,
                    "error": "invalid_keep_through_message_index",
                    "_status": 400,
                }
            source = session_store.load(source_session_id)
            if source is None:
                return {"ok": False, "error": "session_not_found", "_status": 404}
            if source.fingerprint != fingerprint:
                return {
                    "ok": False,
                    "error": "session_fingerprint_mismatch",
                    "fingerprint": source.fingerprint,
                    "current_fingerprint": fingerprint,
                    "_status": 403,
                }
            if keep_through_message_index >= len(source.messages):
                return {
                    "ok": False,
                    "error": "keep_through_message_index_out_of_range",
                    "message_count": len(source.messages),
                    "_status": 400,
                }

            session_id = session_store.generate_session_id()
            cloned_messages = (
                []
                if keep_through_message_index < 0
                else [
                    json.loads(json.dumps(message))
                    for message in source.messages[: keep_through_message_index + 1]
                ]
            )
            runtime_state = SessionRuntimeState.from_dict(source.runtime_state.to_dict())
            session_store.save_runtime_state(
                session_id,
                runtime_state.model or source.model,
                runtime_state,
                messages=cloned_messages,
                total_prompt_tokens=0,
                total_completion_tokens=0,
                active_mode=source.active_mode or runtime_state.active_mode,
                fingerprint=fingerprint,
            )

            snapshot_payload = payload.get("snapshot")
            if isinstance(snapshot_payload, dict) and snapshot_payload:
                session_store.save_snapshot(session_id, snapshot_payload)

            saved = session_store.load(session_id)
            snapshot, snapshot_error = _load_session_snapshot(session_id)
            return {
                "ok": True,
                "session_id": session_id,
                "source_session_id": source_session_id,
                "keep_through_message_index": keep_through_message_index,
                "fingerprint": fingerprint,
                "metadata": _session_metadata_payload(saved)
                if saved is not None
                else {
                    "id": session_id,
                    "model": runtime_state.model or source.model,
                    "saved_at": "",
                    "preview": "",
                    "fingerprint": fingerprint,
                },
                "messages": list(saved.messages) if saved is not None else cloned_messages,
                "runtime_state": runtime_state.to_dict(),
                "snapshot": snapshot,
                "snapshot_error": snapshot_error,
            }

        if action == "snapshot":
            session_id = str(payload.get("session_id") or "")
            snapshot = payload.get("snapshot")
            if not session_id:
                return {"ok": False, "error": "missing_session_id", "_status": 400}
            if not isinstance(snapshot, dict):
                return {"ok": False, "error": "invalid_snapshot", "_status": 400}
            loaded = session_store.load(session_id)
            if loaded is None:
                return {"ok": False, "error": "session_not_found", "_status": 404}
            if loaded.fingerprint != fingerprint:
                return {
                    "ok": False,
                    "error": "session_fingerprint_mismatch",
                    "_status": 403,
                }
            snapshot = _merge_snapshot_with_session_messages(snapshot, loaded.messages)
            digest = _snapshot_digest(snapshot)
            latest_snapshot, _snapshot_error = _load_session_snapshot(session_id)
            if (
                isinstance(latest_snapshot, dict)
                and _snapshot_digest(latest_snapshot) == digest
            ):
                return {
                    "ok": True,
                    "session_id": session_id,
                    "snapshot_digest": digest,
                    "unchanged": True,
                }
            session_store.save_snapshot(session_id, snapshot)
            return {
                "ok": True,
                "session_id": session_id,
                "snapshot_digest": digest,
                "unchanged": False,
            }

        return {"ok": False, "error": "unknown_session_action", "_status": 404}

    runner._relay_http_service.session_history_status_provider = _session_history_status
    runner._relay_http_service.set_session_handler(_handle_session_request)

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
        setattr(peer_agent, "skills_service", skills_service)
        setattr(peer_agent, "skills_catalog", getattr(agent, "skills_catalog", ""))
        runner._register_hooks(peer_agent, current_config)
        runner._wire_agent_tool_parent(peer_agent)

        peer = relay_server.registry.get(peer_id)
        workspace_root = peer.workspace_root if peer is not None else None
        runtime_cwd = workspace_root or (peer.cwd if peer is not None else None)
        if runtime_cwd:
            setattr(peer_agent, "runtime_working_directory", runtime_cwd)
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
            if workspace_root:
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
            setattr(peer_agent, "current_session_id", session_hint)
            return peer_agent

        if resume_latest:
            latest = session_store.get_latest(fingerprint=fingerprint)
            if latest:
                loaded = session_store.load(latest.id)
                if loaded is not None:
                    apply_session_runtime_state(loaded, current_config, peer_agent)
                    setattr(peer_agent, "current_session_id", latest.id)
                    return peer_agent

        restore_config_runtime_defaults(current_config, peer_agent)
        setattr(peer_agent, "current_session_id", session_store.generate_session_id())
        return peer_agent

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
            getattr(peer_agent.llm, "model", current_config.model),
            getattr(peer_agent, "current_session_id", None),
            total_prompt_tokens=peer_agent.state.total_prompt_tokens,
            total_completion_tokens=peer_agent.state.total_completion_tokens,
            active_mode=getattr(peer_agent, "active_mode", None),
            runtime_state=build_session_runtime_state(current_config, peer_agent),
            fingerprint=_peer_fingerprint(peer_id),
        )
        setattr(peer_agent, "current_session_id", sid)

    def _chat(peer_id: str, prompt: str) -> ChatResponse:
        peer_agent = _create_peer_agent(peer_id)
        runtime_agent_id = f"chat:{uuid.uuid4().hex[:12]}"
        setattr(peer_agent, "runtime_agent_id", runtime_agent_id)
        try:
            with agent_runtime_limiter.agent_slot(
                runtime_agent_id,
                agent_type="chat",
                label=peer_id,
                is_cancelled=peer_agent.stop_requested,
            ):
                response = peer_agent.chat(prompt)
            _save_peer_session(peer_agent, peer_id)
            return ChatResponse(response=response)
        except Exception as exc:
            _save_peer_session(peer_agent, peer_id)
            return ChatResponse(response="", error=str(exc))

    def _agent_chat(agent_obj: Any, prompt: str, *, clear_stop_request: bool) -> str:
        signature = inspect.signature(agent_obj.chat)
        if "clear_stop_request" in signature.parameters:
            return agent_obj.chat(prompt, clear_stop_request=clear_stop_request)
        return agent_obj.chat(prompt)

    def _stream_chat(peer_id: str, prompt: str, remote_session) -> None:
        peer_agent = _create_peer_agent(
            peer_id,
            session_hint=getattr(remote_session, "session_hint", None),
            resume_latest=False,
        )
        requested_mode = str(getattr(remote_session, "mode", "") or "").strip()
        if requested_mode:
            try:
                peer_agent.set_mode(requested_mode)
                if not getattr(peer_agent, "session_model_overridden", False):
                    current_config = _current_config()
                    if current_config is not None:
                        apply_agent_default_model(current_config, peer_agent)
            except ValueError as exc:
                remote_session.append_event(
                    "error",
                    {
                        "message": str(exc),
                        "mode": requested_mode,
                    },
                )
                remote_session.append_event("chat_end", {"response": ""})
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
                    metadata={"source": "chat_start"},
                )
                taskflow_id = state.meta.taskflow_id
                remote_session.taskflow_id = taskflow_id
                remote_session.append_event(
                    "taskflow_started", {"taskflow": state.to_dict()}
                )
            setattr(peer_agent, "workflow_mode", TASKFLOW_WORKFLOW_MODE)
            setattr(
                peer_agent,
                "workflow_prompt_append",
                f"{TASKFLOW_SYSTEM_PROMPT}\nCurrent Taskflow taskflow_id: `{taskflow_id}`.",
            )
        remote_session.set_cancel_callback(
            lambda reason: (
                peer_agent.request_stop(),
                relay_server.cancel_pending_requests(peer_id, reason),
            )
        )
        if getattr(remote_session, "cancel_requested", False):
            peer_agent.request_stop()

        runtime_agent_id = f"chat:{getattr(remote_session, 'chat_id', uuid.uuid4().hex)}"
        setattr(peer_agent, "runtime_agent_id", runtime_agent_id)

        def _emit_runtime_status(payload: dict[str, Any]) -> None:
            remote_session.append_event("runtime_status", payload)

        try:
            agent_runtime_limiter.acquire_agent_slot(
                runtime_agent_id,
                agent_type="chat",
                label=peer_id,
                is_cancelled=lambda: bool(getattr(remote_session, "cancel_requested", False)),
                on_wait=_emit_runtime_status,
            )
        except AgentRuntimeCancelled:
            remote_session.append_event(
                "chat_cancelled",
                {"reason": getattr(remote_session, "cancel_reason", "user_cancelled")},
            )
            remote_session.append_event("chat_end", {"response": ""})
            return

        session_id = getattr(peer_agent, "current_session_id", "-") or "-"
        _persist_session_placeholder(peer_agent, peer_id)
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
                    "workspace_root": getattr(peer_info, "workspace_root", None)
                    if peer_info is not None
                    else None,
                },
            )
            startup_announced.add(startup_key)

        current_config = _current_config()
        if prompt.strip().startswith("/") and current_config is not None:
            command_bus = UIEventBus()
            command_result = handle_command(
                prompt.strip(),
                peer_agent,
                current_config,
                getattr(peer_agent, "current_session_id", None),
                command_bus,
                CLI_PROFILE,
                runner.dependencies.create_action_registry(),
                sessions_dir,
                skills_service,
            )
            if command_result["action"] != "chat":
                setattr(peer_agent, "current_session_id", command_result["session_id"])

                for event in getattr(command_bus, "_history", []):
                    remote_session.append_event(
                        _structured_ui_event_type(event),
                        _structured_ui_event_payload(event),
                    )

                if command_result["action"] == "exit":
                    remote_session.append_event(
                        "output",
                        {
                            "format": "plain",
                            "content": "Exit command received. Use Ctrl+C to terminate remote peer.\n",
                        },
                    )
                _save_peer_session(peer_agent, peer_id)
                remote_session.append_event("chat_end", {"response": ""})
                agent_runtime_limiter.release_agent_slot(runtime_agent_id)
                return

        ansi_console = Console(
            record=True, force_terminal=True, color_system="truecolor"
        )
        renderer = CLIRenderer(console_override=ansi_console)
        assistant_content_emitted = {"value": False}
        active_tool_calls_by_name: dict[str, list[str]] = {}

        def _flush_output() -> None:
            rendered = ansi_console.export_text(clear=True, styles=True)
            if rendered.strip():
                remote_session.append_event(
                    "output", {"format": "terminal", "content": rendered}
                )

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
            if not request.tool_args:
                return None
            return {
                "id": "args",
                "title": "Arguments",
                "kind": "json",
                "content": request.tool_args,
            }

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

        def _preview_state(preview: ToolPreviewResult) -> dict[str, Any]:
            state: dict[str, Any] = {}
            if preview.resolved_path is not None:
                state["resolved_path"] = preview.resolved_path
            if preview.old_sha256 is not None:
                state["old_sha256"] = preview.old_sha256
            if preview.old_exists is not None:
                state["old_exists"] = preview.old_exists
            if preview.old_size is not None:
                state["old_size"] = preview.old_size
            if preview.old_mtime_ns is not None:
                state["old_mtime_ns"] = preview.old_mtime_ns
            return state

        def _build_remote_preview(
            request: ApprovalRequest,
        ) -> tuple[list[dict[str, Any]], ToolPreviewResult | None, str | None]:
            backend = _remote_backend()
            if (
                backend is None
                or request.tool_name not in {"write_file", "edit_file"}
                or not _peer_supports_tool_preview()
            ):
                section = _args_section(request)
                return ([section] if section else []), None, "preview_unavailable"

            preview = backend.preview_tool(request.tool_name, dict(request.tool_args))
            if preview.ok:
                if preview.sections:
                    return preview.sections, preview, None
                if preview.diff:
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

            sections: list[dict[str, Any]] = []
            section = _args_section(request)
            if section is not None:
                sections.append(section)
            sections.append(
                {
                    "id": "preview",
                    "title": "Preview unavailable",
                    "kind": "text",
                    "content": preview.error_message
                    or preview.error_code
                    or "Peer could not build a preview.",
                }
            )
            return sections, preview, preview.error_message or "preview_unavailable"

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
                remote_session.register_approval(approval_id)
                sections, preview, preview_error = _build_remote_preview(request)
                payload = {
                    "approval_id": approval_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": request.tool_name,
                    "tool_source": request.tool_source,
                    "reason": request.reason,
                    "tool_args": request.tool_args,
                    "sections": sections,
                    "preview_unavailable": preview is None or not preview.ok,
                    "preview_error": preview_error,
                    "format": "markdown",
                    "content": "\n\n".join(
                        part
                        for part in [
                            f"## Approval required: {request.tool_name}",
                            f"Tool `{request.tool_name}` from source `{request.tool_source}` requires approval.",
                            request.reason or "",
                            *[_section_markdown(section) for section in sections],
                        ]
                        if part
                    ),
                }
                remote_session.append_event("approval_request", payload)
                decision, reason = remote_session.wait_approval(approval_id)
                remote_session.append_event(
                    "approval_resolved",
                    {
                        "approval_id": approval_id,
                        "tool_call_id": tool_call_id,
                        "decision": decision,
                        "reason": reason,
                    },
                )
                if decision == "allow_once":
                    backend = _remote_backend()
                    if backend is not None and preview is not None and preview.ok:
                        backend.remember_approved_preview(
                            request.tool_name,
                            dict(request.tool_args),
                            _preview_state(preview),
                        )
                    return ApprovalDecision.allow_once(reason)
                return ApprovalDecision.deny_once(reason)

        def _on_remote_stream(
            tool_name: str, chunk: Any, tool_call_id: str | None = None
        ) -> None:
            resolved_tool_call_id = tool_call_id
            if not resolved_tool_call_id:
                candidates = active_tool_calls_by_name.get(tool_name) or []
                if candidates:
                    resolved_tool_call_id = candidates[-1]
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

        def _on_agent_event(event: AgentEvent) -> None:
            if event.event_type == AgentEventType.STREAM_TOKEN:
                content = event.data.get("token", "")
                if content:
                    assistant_content_emitted["value"] = True
                    remote_session.append_event(
                        "assistant_delta",
                        {"format": "markdown", "content": content},
                    )
                return
            if event.event_type == AgentEventType.USAGE_UPDATE:
                remote_session.append_event("usage_update", event.data)
                return
            if event.event_type == AgentEventType.RUNTIME_STATUS:
                remote_session.append_event("runtime_status", event.data)
                return
            if event.event_type == AgentEventType.CHAT_END:
                response = event.data.get("response", "")
                if event.data.get("render_response", True) and response:
                    assistant_content_emitted["value"] = True
                    remote_session.append_event(
                        "assistant_message",
                        {"format": "markdown", "content": response},
                    )
                return
            if event.event_type == AgentEventType.TOOL_CALL_START:
                if event.tool_name and event.tool_call_id:
                    active_tool_calls_by_name.setdefault(event.tool_name, []).append(
                        event.tool_call_id
                    )
                remote_session.append_event(
                    "tool_call_start",
                    {
                        "tool_name": event.tool_name,
                        "tool_call_id": event.tool_call_id,
                        "tool_args": event.tool_args or {},
                        "tool_source": event.data.get("tool_source"),
                        "started_at": event.timestamp,
                    },
                )
                return
            elif event.event_type == AgentEventType.TOOL_CALL_END:
                if event.tool_name and event.tool_call_id:
                    candidates = active_tool_calls_by_name.get(event.tool_name)
                    if candidates and event.tool_call_id in candidates:
                        candidates.remove(event.tool_call_id)
                remote_session.append_event(
                    "tool_call_end",
                    {
                        "tool_name": event.tool_name,
                        "tool_call_id": event.tool_call_id,
                        "tool_success": event.tool_success,
                        "tool_result": event.tool_result or "",
                        "tool_source": event.data.get("tool_source"),
                        "meta": event.data.get("meta") or {},
                        "ended_at": event.timestamp,
                    },
                )
                return
            elif event.event_type == AgentEventType.ERROR:
                remote_session.append_event(
                    "error", {"message": event.error_message or "unknown error"}
                )
                return
            elif event.event_type == AgentEventType.SUBAGENT_COMPLETED:
                remote_session.append_event("subagent_completed", event.data)
                return

        previous_approval = peer_agent.approval_provider
        peer_agent.add_event_handler(_on_agent_event)
        peer_agent.approval_provider = _RemoteApprovalProvider()
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
            if getattr(remote_session, "cancel_requested", False):
                remote_session.append_event(
                    "chat_cancelled",
                    {"reason": getattr(remote_session, "cancel_reason", None)},
                )
            remote_session.append_event(
                "chat_end",
                {
                    "response": result,
                    "response_rendered": assistant_content_emitted["value"],
                },
            )
        except Exception as exc:
            _flush_output()
            _save_peer_session(peer_agent, peer_id)
            remote_session.append_event("error", {"message": str(exc)})
        finally:
            peer_agent.approval_provider = previous_approval
            try:
                peer_agent._event_handlers.remove(_on_agent_event)
            except ValueError:
                pass
            agent_runtime_limiter.release_agent_slot(runtime_agent_id)
            renderer.close()

    runner._relay_http_service.set_chat_handler(_chat)
    runner._relay_http_service.set_stream_chat_handler(_stream_chat)


def _structured_ui_event_type(event) -> str:
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

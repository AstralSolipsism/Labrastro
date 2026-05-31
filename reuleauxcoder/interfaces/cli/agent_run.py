"""Headless AgentRun CLI entrypoint."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from reuleauxcoder.app.runtime.session_state import (
    apply_session_runtime_state,
    build_session_runtime_state,
    get_session_fingerprint,
)
from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.extensions.config_target import resolve_cli_config_path
from reuleauxcoder.interfaces.entrypoint import AppOptions, AppRunner
from reuleauxcoder.interfaces.events import UIEvent, UIEventKind, UIEventLevel
from reuleauxcoder.services.llm.client import llm_is_configured, llm_unavailable_reason


class AgentRunJSONLEmitter:
    """Emit executor events for AgentRun workers without terminal UI formatting."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream

    def emit(self, payload: dict[str, Any]) -> None:
        self._stream.write(json.dumps(_jsonable(payload), ensure_ascii=False) + "\n")
        self._stream.flush()

    def status(self, status: str, **data: Any) -> None:
        self.emit({"type": "status", "status": status, **data})

    def error(self, message: str, **data: Any) -> None:
        self.emit({"type": "error", "message": message, "text": message, **data})

    def result(self, *, status: str, output: str = "", error: str = "", **data: Any) -> None:
        self.emit(
            {
                "type": "result",
                "status": status,
                "output": output,
                **({"error": error} if error else {}),
                **data,
            }
        )

    def on_agent_event(self, event: AgentEvent) -> None:
        event_type = event.event_type
        if event_type == AgentEventType.STREAM_TOKEN:
            token = str(event.data.get("token") or "")
            if token:
                self.emit({"type": "text", "text": token})
            return
        if event_type == AgentEventType.REASONING_TOKEN:
            token = str(event.data.get("token") or "")
            if token:
                self.emit({"type": "thinking", "text": token})
            return
        if event_type == AgentEventType.TOOL_CALL_START:
            self.emit(
                {
                    "type": "tool_use",
                    "data": {
                        "tool_name": event.tool_name,
                        "tool_call_id": event.tool_call_id,
                        "input": event.tool_args or {},
                        **dict(event.data or {}),
                    },
                }
            )
            return
        if event_type == AgentEventType.TOOL_CALL_END:
            self.emit(
                {
                    "type": "tool_result",
                    "text": event.tool_result or "",
                    "data": {
                        "tool_name": event.tool_name,
                        "tool_call_id": event.tool_call_id,
                        "output": event.tool_result or "",
                        **dict(event.data or {}),
                    },
                }
            )
            return
        if event_type == AgentEventType.TOOL_CALL_PROTOCOL_ERROR:
            self.error(
                event.error_message or str(event.data.get("message") or "tool_call_protocol_error"),
                data=dict(event.data or {}),
            )
            return
        if event_type == AgentEventType.RUNTIME_STATUS:
            data = dict(event.data or {})
            status = str(data.pop("status", "") or "runtime_status")
            self.status(status, **data)
            return
        if event_type in {
            AgentEventType.PROVIDER_STREAM_INTERRUPTED,
            AgentEventType.PROVIDER_STREAM_RECOVERING,
            AgentEventType.PROVIDER_STREAM_RECOVERED,
            AgentEventType.SESSION_RUN_INTERRUPTED,
        }:
            self.status(event_type.value, **dict(event.data or {}))
            return
        if event_type == AgentEventType.ERROR:
            self.error(
                event.error_message or str(event.data.get("message") or "agent_error"),
                data=dict(event.data or {}),
            )
            return

    def on_ui_event(self, event: UIEvent) -> None:
        if event.kind == UIEventKind.AGENT:
            return
        if event.level == UIEventLevel.ERROR:
            self.error(event.message, data=dict(event.data or {}))
            return
        if event.kind in {UIEventKind.CONTEXT, UIEventKind.REMOTE, UIEventKind.SESSION}:
            self.emit(
                {
                    "type": "log",
                    "text": event.message,
                    "data": {
                        "level": event.level.value,
                        "kind": event.kind.value,
                        **dict(event.data or {}),
                    },
                }
            )


def run_agent_run_cli(args: Any) -> int:
    session_id = str(getattr(args, "agent_run_session", "") or "").strip()
    if not session_id:
        print("Error: agent-run requires --session <executor_session_id>", file=sys.stderr)
        return 2
    prompt = str(getattr(args, "prompt", "") or "")
    emitter = AgentRunJSONLEmitter(sys.stdout)
    runner: AppRunner | None = None
    ctx = None
    try:
        config_path = resolve_cli_config_path(args, require=False, purpose="agent-run mode")
        runner = AppRunner(
            AppOptions(
                config_path=config_path,
                model=getattr(args, "model", None),
                auto_resume_latest=False,
            )
        )
        ctx = runner.initialize()
        store = runner.dependencies.create_configured_session_store(ctx.config, ctx.sessions_dir)
        loaded = store.load(session_id)
        if loaded is not None:
            apply_session_runtime_state(loaded, ctx.config, ctx.agent)
            setattr(ctx.agent, "session_fingerprint", loaded.fingerprint)
        setattr(ctx.agent, "current_session_id", session_id)
        ctx.current_session_id = session_id
        ctx.ui_bus.subscribe(emitter.on_ui_event)
        ctx.agent.add_event_handler(emitter.on_agent_event)
        emitter.status("session_pinned", executor_session_id=session_id)
        if not llm_is_configured(ctx.agent.llm):
            message = llm_unavailable_reason(ctx.agent.llm)
            emitter.error(message)
            _save_agent_run_session(runner, ctx, session_id)
            emitter.result(status="failed", error=message, executor_session_id=session_id)
            return 1
        output = ctx.agent.chat(prompt)
        _save_agent_run_session(runner, ctx, session_id)
        emitter.result(status="completed", output=output, executor_session_id=session_id)
        return 0
    except BaseException as exc:
        message = str(exc) or type(exc).__name__
        if runner is not None and ctx is not None:
            try:
                _save_agent_run_session(runner, ctx, session_id)
            except Exception:
                pass
        emitter.error(message)
        emitter.result(status="failed", error=message, executor_session_id=session_id)
        return 1
    finally:
        if runner is not None:
            runner.cleanup()


def _save_agent_run_session(runner: AppRunner, ctx: Any, session_id: str) -> None:
    store = runner.dependencies.create_configured_session_store(ctx.config, ctx.sessions_dir)
    messages = list(getattr(ctx.agent, "messages", []) or [])
    model = getattr(ctx.agent.llm, "model", "")
    active_mode = getattr(ctx.agent, "active_mode", None)
    runtime_state = build_session_runtime_state(ctx.config, ctx.agent)
    fingerprint = get_session_fingerprint(ctx.config, ctx.agent)
    save_kwargs = dict(
        total_prompt_tokens=ctx.agent.state.total_prompt_tokens,
        total_completion_tokens=ctx.agent.state.total_completion_tokens,
        active_mode=active_mode,
        fingerprint=fingerprint,
    )
    if store.has_history_content(messages):
        store.save(
            messages,
            model,
            session_id,
            runtime_state=runtime_state,
            **save_kwargs,
        )
        return
    store.save_runtime_state(
        session_id,
        model,
        runtime_state,
        messages=messages,
        **save_kwargs,
    )


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    return str(value)

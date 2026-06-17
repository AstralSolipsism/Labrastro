from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from labrastro_server.interfaces.http.remote.service import _SessionRunProjection
from reuleauxcoder.interfaces.entrypoint.runner import AppRunner


def _wait_for(predicate, timeout: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("condition was not satisfied")


def _same_bound_method(left, right) -> bool:
    return (
        getattr(left, "__self__", None) is getattr(right, "__self__", None)
        and getattr(left, "__func__", None) is getattr(right, "__func__", None)
    )


def test_runner_mcp_elicitation_handler_waits_for_session_run_user_input() -> None:
    session = _SessionRunProjection(session_run_id="run-1", peer_id="peer-1")
    runner = AppRunner()
    runner._relay_http_service = SimpleNamespace(
        _get_session_run=lambda session_run_id: (
            session if session_run_id == "run-1" else None
        )
    )
    handler = runner._mcp_elicitation_handler
    result: dict[str, Any] = {}

    def run_handler() -> None:
        result["value"] = handler(
            {
                "session_run_id": "run-1",
                "agent_run_id": "agent-run-1",
                "turn_id": "turn-1",
                "tool_call_id": "tool-call-1",
                "mcp_server": "docs",
                "tool_name": "lookup",
                "request_id": "7",
                "message": "Pick a format",
                "input_schema": {
                    "type": "object",
                    "properties": {"format": {"type": "string"}},
                },
            }
        )

    thread = threading.Thread(target=run_handler)
    thread.start()
    try:
        request_event = _wait_for(
            lambda: next(
                (
                    event
                    for event in session.events
                    if event["type"] == "user_input_request"
                ),
                None,
            )
        )
        payload = request_event["payload"]
        assert payload["kind"] == "mcp_elicitation"
        assert payload["event_name"] == "Elicitation"
        assert payload["session_run_id"] == "run-1"
        assert payload["agent_run_id"] == "agent-run-1"
        assert payload["turn_id"] == "turn-1"
        assert payload["tool_call_id"] == "tool-call-1"
        assert payload["mcp_server"] == "docs"
        assert payload["tool_name"] == "lookup"
        assert payload["message"] == "Pick a format"
        assert payload["input_schema"]["properties"]["format"]["type"] == "string"

        session.resolve_user_input(
            payload["input_id"],
            "accept",
            {"format": "markdown"},
            "chosen",
        )
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert result["value"] == {
            "action": "accept",
            "content": {"format": "markdown"},
            "reason": "chosen",
        }
    finally:
        if thread.is_alive():
            session.cancel_pending_user_inputs("test cleanup")
            thread.join(timeout=2)


def test_runner_mcp_elicitation_handler_timeout_declines_and_audits_reason() -> None:
    session = _SessionRunProjection(session_run_id="run-1", peer_id="peer-1")
    runner = AppRunner()
    runner._relay_http_service = SimpleNamespace(
        _get_session_run=lambda session_run_id: (
            session if session_run_id == "run-1" else None
        )
    )

    result = runner._mcp_elicitation_handler(
        {
            "session_run_id": "run-1",
            "tool_call_id": "tool-call-1",
            "request_id": "timeout",
            "message": "Pick a format",
            "timeout_sec": 0.01,
        }
    )

    assert result == {
        "action": "decline",
        "content": {},
        "reason": "user_input_timeout",
    }
    resolved_events = [
        event for event in session.events if event["type"] == "user_input_resolved"
    ]
    assert resolved_events
    assert resolved_events[-1]["payload"]["action"] == "decline"
    assert resolved_events[-1]["payload"]["reason"] == "user_input_timeout"


def test_runner_init_mcp_installs_elicitation_handler_before_connect() -> None:
    class FakeManager:
        def __init__(self) -> None:
            self.handler = None
            self.handler_at_connect = None
            self.tools = []

        def set_elicitation_handler(self, handler) -> None:
            self.handler = handler

        def start(self) -> None:
            return None

        def connect_server(self, _server) -> bool:
            self.handler_at_connect = self.handler
            return True

    manager = FakeManager()
    runner = AppRunner()
    runner.dependencies.create_mcp_manager = lambda _ui_bus: manager
    ui_bus = SimpleNamespace(warning=lambda *args, **kwargs: None, success=lambda *args, **kwargs: None)
    agent = SimpleNamespace(add_tools=lambda _tools: None)

    runner._init_mcp([SimpleNamespace(name="docs", enabled=True)], agent, ui_bus)

    assert _same_bound_method(manager.handler, runner._mcp_elicitation_handler)
    assert _same_bound_method(manager.handler_at_connect, runner._mcp_elicitation_handler)

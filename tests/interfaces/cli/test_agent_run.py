import io
import json
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.interfaces.cli.agent_run import (
    AgentRunJSONLEmitter,
    _save_agent_run_session,
)


def _events(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


def test_agent_run_emitter_outputs_jsonl_for_agent_events() -> None:
    buffer = io.StringIO()
    emitter = AgentRunJSONLEmitter(buffer)

    emitter.status("session_pinned", executor_session_id="labrastro-agent-run-task-1")
    emitter.on_agent_event(AgentEvent.reasoning_token("plan"))
    emitter.on_agent_event(AgentEvent.stream_token("answer"))
    emitter.on_agent_event(
        AgentEvent.tool_call_start(
            "read_file",
            {"path": "README.md"},
            tool_call_id="tool-1",
        )
    )
    emitter.on_agent_event(
        AgentEvent.tool_call_end("read_file", "ok", tool_call_id="tool-1")
    )
    emitter.result(
        status="completed",
        output="answer",
        executor_session_id="labrastro-agent-run-task-1",
    )

    events = _events(buffer)
    assert [event["type"] for event in events] == [
        "status",
        "thinking",
        "text",
        "tool_use",
        "tool_result",
        "result",
    ]
    assert events[0]["executor_session_id"] == "labrastro-agent-run-task-1"
    assert events[1]["text"] == "plan"
    assert events[2]["text"] == "answer"
    assert events[3]["data"]["tool_call_id"] == "tool-1"
    assert events[4]["data"]["output"] == "ok"


def test_agent_run_main_bypasses_terminal_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reuleauxcoder.interfaces.cli import main as cli_main

    class BombRenderer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("agent-run must not instantiate CLIRenderer")

    monkeypatch.setattr(cli_main, "CLIRenderer", BombRenderer)
    monkeypatch.setattr(
        cli_main,
        "parse_args",
        lambda: SimpleNamespace(command="agent-run"),
    )
    monkeypatch.setattr(cli_main, "run_agent_run_cli", lambda args: 0)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main()

    assert exc_info.value.code == 0


def test_agent_run_save_persists_runtime_state_without_history_content(
    tmp_path,
) -> None:
    class FakeStore:
        def __init__(self) -> None:
            self.runtime_saved: dict[str, object] | None = None

        def has_history_content(self, messages: list[dict]) -> bool:
            return False

        def save(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("empty AgentRun sessions must use save_runtime_state")

        def save_runtime_state(self, *args: object, **kwargs: object) -> None:
            self.runtime_saved = {"args": args, "kwargs": kwargs}

    store = FakeStore()
    runner = SimpleNamespace(
        dependencies=SimpleNamespace(
            create_configured_session_store=lambda config, sessions_dir: store
        )
    )
    agent = SimpleNamespace(
        messages=[],
        llm=SimpleNamespace(model="agent-run-model", debug_trace=False),
        state=SimpleNamespace(total_prompt_tokens=0, total_completion_tokens=0),
        active_mode=None,
    )
    ctx = SimpleNamespace(
        config=SimpleNamespace(active_sub_model_profile=None),
        sessions_dir=tmp_path,
        agent=agent,
    )

    _save_agent_run_session(runner, ctx, "labrastro-agent-run-task-empty")

    assert store.runtime_saved is not None
    assert store.runtime_saved["args"][0] == "labrastro-agent-run-task-empty"

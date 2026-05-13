from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType


def test_agent_event_chat_start_contains_user_input() -> None:
    event = AgentEvent.chat_start("hello")
    assert event.event_type is AgentEventType.CHAT_START
    assert event.data == {"user_input": "hello"}


def test_agent_event_tool_call_start_contains_name_and_args() -> None:
    event = AgentEvent.tool_call_start("shell", {"command": "ls"})
    assert event.event_type is AgentEventType.TOOL_CALL_START
    assert event.tool_name == "shell"
    assert event.tool_args == {"command": "ls"}


def test_agent_event_tool_call_end_keeps_full_long_result_with_preview() -> None:
    result = "x" * 600
    event = AgentEvent.tool_call_end("read_file", result)
    assert event.event_type is AgentEventType.TOOL_CALL_END
    assert event.tool_name == "read_file"
    removed_field = "tool_" + "success"
    assert not hasattr(event, removed_field)
    assert event.tool_result == result
    assert event.data["tool_result_preview"] == "x" * 500


def test_agent_event_tool_call_protocol_error_contains_payload() -> None:
    event = AgentEvent.tool_call_protocol_error(
        "write_file",
        tool_call_id="call-1",
        code="REMOTE_PREVIEW_REQUIRED",
        message="remote peer must provide a tool preview",
    )

    assert event.event_type is AgentEventType.TOOL_CALL_PROTOCOL_ERROR
    assert event.tool_name == "write_file"
    assert event.tool_call_id == "call-1"
    assert event.data == {
        "tool_name": "write_file",
        "tool_call_id": "call-1",
        "code": "REMOTE_PREVIEW_REQUIRED",
        "message": "remote peer must provide a tool preview",
    }


def test_agent_event_delegated_run_completed_contains_payload() -> None:
    event = AgentEvent.delegated_run_completed(
        run_id="run-1",
        agent_id="researcher",
        task="scan repo",
        status="ok",
        result="done",
        error=None,
    )
    assert event.event_type is AgentEventType.DELEGATED_RUN_COMPLETED
    assert event.data["run_id"] == "run-1"
    assert event.data["agent_id"] == "researcher"
    assert event.data["status"] == "ok"
    assert event.data["result"] == "done"


def test_agent_event_usage_update_contains_context_cache_and_cost() -> None:
    event = AgentEvent.usage_update(
        prompt_tokens=1200,
        completion_tokens=300,
        context_tokens=2200,
        context_window=128000,
        max_output_tokens=4096,
        model="deepseek-v4",
        mode="coder",
        cache_read_tokens=800,
        cache_write_tokens=200,
        cost_usd=0.0123,
        usage_extra={"prompt_tokens_details": {"cached_tokens": 800}},
        run_status="running",
    )

    assert event.event_type is AgentEventType.USAGE_UPDATE
    assert event.data["prompt_tokens"] == 1200
    assert event.data["completion_tokens"] == 300
    assert event.data["context_tokens"] == 2200
    assert event.data["context_window"] == 128000
    assert event.data["max_output_tokens"] == 4096
    assert event.data["cache_reads"] == 800
    assert event.data["cache_writes"] == 200
    assert event.data["cost_usd"] == 0.0123
    assert event.data["cost_status"] == "available"
    assert event.data["usage_extra"]["prompt_tokens_details"]["cached_tokens"] == 800
    assert event.data["run_status"] == "running"


def test_agent_event_error_contains_message() -> None:
    event = AgentEvent.error("boom")
    assert event.event_type is AgentEventType.ERROR
    assert event.error_message == "boom"

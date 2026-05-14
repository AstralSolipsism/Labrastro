from pathlib import Path

from reuleauxcoder.services.llm.diagnostics import (
    aggregate_tool_argument_validation_events,
    persist_llm_error_diagnostic,
    persist_tool_argument_validation_event,
    snapshot_messages,
    summarize_tool_argument_validation_events,
)
from reuleauxcoder.domain.agent.tool_arguments import validate_and_repair_tool_arguments


def test_snapshot_messages_keeps_last_10_and_truncates_content() -> None:
    messages = [{"role": "user", "content": f"msg-{i}"} for i in range(12)]
    messages[-1]["content"] = "x" * 600
    messages[-1]["reasoning_content"] = "r" * 600

    snapshot = snapshot_messages(messages)

    assert len(snapshot) == 10
    assert snapshot[0]["index"] == 2
    assert snapshot[-1]["role"] == "user"
    assert snapshot[-1]["content"].endswith("...")
    assert snapshot[-1]["reasoning_content"].endswith("...")


def test_persist_llm_error_diagnostic_writes_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    error = RuntimeError("boom")
    path = persist_llm_error_diagnostic(
        model="demo-model",
        base_url="https://example.com/v1",
        session_id="session_test",
        request_params={
            "stream": True,
            "temperature": 0,
            "max_tokens": 128,
            "tools": [{"type": "function", "function": {"name": "shell"}}],
        },
        raw_messages=[{"role": "user", "content": "hello"}],
        sanitized_messages=[{"role": "user", "content": "hello"}],
        error=error,
        metadata={"round_index": 2, "active_mode": "coder", "pending_tool_calls": 1},
    )

    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert '"session_id": "session_test"' in content
    assert '"tool_names": [' in content
    assert '"round_index": 2' in content


def test_tool_argument_validation_telemetry_aggregates_by_model_tool_and_issue(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    validation = validate_and_repair_tool_arguments(
        tool_name="write_file",
        arguments={},
        schema={
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    )

    path = persist_tool_argument_validation_event(
        validation=validation,
        metadata={
            "model": "deepseek-v4-pro",
            "tool": "write_file",
            "provider_id": "deepseek",
        },
    )

    counts = aggregate_tool_argument_validation_events(path)

    assert counts["model=deepseek-v4-pro|tool=write_file|final_valid=false"] == 1
    assert (
        counts[
            "issue|model=deepseek-v4-pro|tool=write_file|code=missing_required|path=$.content"
        ]
        == 1
    )

    summary = summarize_tool_argument_validation_events(path)
    assert summary["totals"] == {"events": 1, "invalid": 1, "repaired": 0}
    assert summary["by_model"][0]["name"] == "deepseek-v4-pro"
    assert summary["issues"][0]["code"] == "missing_required"

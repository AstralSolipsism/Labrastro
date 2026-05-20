from pathlib import Path

import json

from reuleauxcoder.services.llm.diagnostics import (
    aggregate_tool_diagnostic_events,
    persist_llm_error_diagnostic,
    persist_tool_diagnostic_event,
    snapshot_messages,
    summarize_tool_diagnostic_events,
)
from reuleauxcoder.domain.agent.tool_arguments import validate_and_repair_tool_arguments
from reuleauxcoder.domain.agent.tool_diagnostics import (
    diagnostics_from_argument_validation,
)


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


def test_persist_llm_error_diagnostic_records_cause_chain_and_provider_details(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    try:
        try:
            raise OSError("socket closed")
        except OSError as cause:
            raise RuntimeError("Connection error.") from cause
    except RuntimeError as error:
        setattr(error, "provider_error_phase", "request_start")
        setattr(
            error,
            "provider_retry_attempts",
            [
                {
                    "attempt": 1,
                    "phase": "request_start",
                    "error": {
                        "type": "APIConnectionError",
                        "message": "Connection error.",
                    },
                    "headers": {"Authorization": "Bearer secret-token"},
                    "action": "raise",
                }
            ],
        )
        path = persist_llm_error_diagnostic(
            model="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            session_id="session_test",
            request_params={
                "stream": True,
                "max_tokens": 384000,
                "stream_options": {"include_usage": True},
                "api_key": "sk-secret",
                "tools": [{"type": "function", "function": {"name": "write_file"}}],
            },
            raw_messages=[{"role": "user", "content": "hello"}],
            sanitized_messages=[{"role": "user", "content": "hello"}],
            error=error,
            metadata={"Authorization": "Bearer secret-token"},
            provider_id="deepseek",
            provider_type="openai_chat",
            timeout_sec=120,
            max_retries=3,
            duration_ms=42,
        )

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["provider"] == {
        "id": "deepseek",
        "type": "openai_chat",
        "base_url": "https://api.deepseek.com",
        "timeout_sec": 120,
        "max_retries": 3,
    }
    assert payload["duration_ms"] == 42
    assert payload["provider_error"]["phase"] == "request_start"
    assert payload["error"]["cause_chain"][0]["type"] == "RuntimeError"
    assert payload["error"]["cause_chain"][1]["type"] == "OSError"
    assert "RuntimeError: Connection error." in payload["error"]["traceback"]
    assert payload["request"]["tool_names"] == ["write_file"]
    assert payload["metadata"]["Authorization"] == "[REDACTED]"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "sk-secret" not in serialized
    assert "secret-token" not in serialized


def test_tool_diagnostic_telemetry_aggregates_by_model_tool_stage_and_issue(
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

    path = persist_tool_diagnostic_event(
        diagnostics=diagnostics_from_argument_validation(
            validation,
            tool_call_id="call_write_1",
        ),
        validation=validation,
        metadata={
            "model": "deepseek-v4-pro",
            "tool": "write_file",
            "provider_id": "deepseek",
        },
    )

    counts = aggregate_tool_diagnostic_events(path)

    assert counts["model=deepseek-v4-pro|tool=write_file|final_valid=false"] == 1
    assert (
        counts[
            "issue|model=deepseek-v4-pro|tool=write_file|stage=argument_validation|kind=schema_issue|code=missing_required|path=$.content"
        ]
        == 1
    )

    summary = summarize_tool_diagnostic_events(path)
    assert summary["totals"] == {
        "events": 1,
        "diagnostics": 1,
        "errors": 1,
        "warnings": 0,
        "repaired": 0,
    }
    assert summary["by_model"][0]["name"] == "deepseek-v4-pro"
    assert summary["by_stage"][0]["name"] == "argument_validation"
    assert summary["issues"][0]["code"] == "missing_required"

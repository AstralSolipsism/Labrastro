from reuleauxcoder.domain.agent.events import (
    AgentEvent,
    AgentEventType,
    ToolFailureKind,
)


def test_agent_event_session_run_start_contains_user_input() -> None:
    event = AgentEvent.session_run_start("hello")
    assert event.event_type is AgentEventType.SESSION_RUN_START
    assert event.data == {"user_input": "hello"}


def test_agent_event_reasoning_token_contains_token() -> None:
    event = AgentEvent.reasoning_token("thinking")
    assert event.event_type is AgentEventType.REASONING_TOKEN
    assert event.data == {"token": "thinking"}


def test_agent_event_tool_call_start_contains_name_and_args() -> None:
    event = AgentEvent.tool_call_start("shell", {"command": "ls"}, index=2)
    assert event.event_type is AgentEventType.TOOL_CALL_START
    assert event.tool_name == "shell"
    assert event.tool_args == {"command": "ls"}
    assert event.data["index"] == 2


def test_agent_event_tool_call_delta_contains_preview() -> None:
    event = AgentEvent.tool_call_delta(
        index=0,
        tool_call_id="call-1",
        tool_name="grep",
        arguments_delta='{"pattern"',
        arguments_preview='{"pattern": "remotePeerState"}',
        tool_source="builtin",
    )

    assert event.event_type is AgentEventType.TOOL_CALL_DELTA
    assert event.tool_name == "grep"
    assert event.tool_call_id == "call-1"
    assert event.data == {
        "index": 0,
        "tool_call_id": "call-1",
        "tool_name": "grep",
        "arguments_delta": '{"pattern"',
        "arguments_preview": '{"pattern": "remotePeerState"}',
        "status": "preparing",
        "tool_source": "builtin",
    }


def test_agent_event_tool_arguments_state_events_are_stable() -> None:
    complete = AgentEvent.tool_arguments_complete(
        "apply_patch",
        tool_call_id="call-1",
        index=0,
        tool_source="builtin",
    )
    valid = AgentEvent.tool_arguments_valid(
        "apply_patch",
        tool_call_id="call-1",
        index=0,
        tool_source="builtin",
    )
    invalid = AgentEvent.tool_arguments_invalid(
        "apply_patch",
        tool_call_id="call-1",
        index=0,
        message="bad patch",
        code="preflight_failed",
        retry_hint="Use *** Update File: <path>.",
    )

    assert complete.event_type is AgentEventType.TOOL_ARGUMENTS_COMPLETE
    assert complete.data == {
        "tool_name": "apply_patch",
        "tool_call_id": "call-1",
        "index": 0,
        "status": "complete",
        "tool_source": "builtin",
    }
    assert valid.event_type is AgentEventType.TOOL_ARGUMENTS_VALID
    assert valid.data["status"] == "valid"
    assert invalid.event_type is AgentEventType.TOOL_ARGUMENTS_INVALID
    assert invalid.data == {
        "tool_name": "apply_patch",
        "tool_call_id": "call-1",
        "index": 0,
        "status": "invalid",
        "message": "bad patch",
        "code": "preflight_failed",
        "retry_hint": "Use *** Update File: <path>.",
    }


def test_agent_event_mutation_preview_candidate_events_are_stable() -> None:
    previewing = AgentEvent.mutation_previewing(
        "apply_patch",
        item_id="file-change:call-1",
        tool_call_id="call-1",
        index=0,
        tool_metadata={
            "tool_id": "builtin:apply_patch",
            "risk": "workspace_write",
            "exposure": "direct",
            "capability_name": "workspace",
        },
    )
    ready = AgentEvent.mutation_preview_ready(
        "apply_patch",
        item_id="file-change:call-1",
        tool_call_id="call-1",
        changes=[{"path": "src/app.py", "kind": "update"}],
        index=0,
        tool_metadata={
            "tool_id": "builtin:apply_patch",
            "risk": "workspace_write",
            "exposure": "direct",
            "capability_name": "workspace",
        },
    )
    failed = AgentEvent.mutation_preview_failed(
        "apply_patch",
        item_id="file-change:call-1",
        tool_call_id="call-1",
        error="patch context does not match file",
        index=0,
        tool_metadata={
            "tool_id": "builtin:apply_patch",
            "risk": "workspace_write",
            "exposure": "direct",
            "capability_name": "workspace",
        },
    )

    assert previewing.event_type is AgentEventType.MUTATION_PREVIEWING
    assert previewing.data == {
        "tool_name": "apply_patch",
        "tool_call_id": "call-1",
        "item_id": "file-change:call-1",
        "index": 0,
        "status": "previewing",
        "tool_id": "builtin:apply_patch",
        "risk": "workspace_write",
        "exposure": "direct",
        "capability_name": "workspace",
    }
    assert ready.event_type is AgentEventType.MUTATION_PREVIEW_READY
    assert ready.data["changes"] == [{"path": "src/app.py", "kind": "update"}]
    assert ready.data["status"] == "ready"
    assert ready.data["tool_id"] == "builtin:apply_patch"
    assert ready.data["risk"] == "workspace_write"
    assert ready.data["exposure"] == "direct"
    assert ready.data["capability_name"] == "workspace"
    assert failed.event_type is AgentEventType.MUTATION_PREVIEW_FAILED
    assert failed.data["status"] == "failed"
    assert failed.data["tool_id"] == "builtin:apply_patch"
    assert failed.data["error"] == "patch context does not match file"


def test_agent_event_draft_recovery_state_events_are_stable() -> None:
    stalled = AgentEvent.draft_body_stalled(
        draft_id="draft-1",
        target_path="docs/a.md",
        content_length=42,
        content_sha256="abc",
        last_chunk_seq=3,
        reason="stream interrupted",
    )
    recoverable = AgentEvent.draft_interrupted_recoverable(
        draft_id="draft-1",
        target_path="docs/a.md",
        content_length=42,
        content_sha256="abc",
        last_chunk_seq=3,
        reason="stream interrupted",
    )

    assert stalled.event_type is AgentEventType.DRAFT_BODY_STALLED
    assert stalled.data == {
        "draft_id": "draft-1",
        "target_path": "docs/a.md",
        "status": "stalled",
        "content_length": 42,
        "content_sha256": "abc",
        "last_chunk_seq": 3,
        "reason": "stream interrupted",
    }
    assert "content" not in stalled.data
    assert recoverable.event_type is AgentEventType.DRAFT_INTERRUPTED_RECOVERABLE
    assert recoverable.data["status"] == "recoverable"
    assert recoverable.data["recovery_action"] == "continue"
    assert "content" not in recoverable.data


def test_agent_event_tool_call_end_keeps_full_long_result_with_preview() -> None:
    result = "x" * 600
    event = AgentEvent.tool_call_end("read_file", result, index=3)
    assert event.event_type is AgentEventType.TOOL_CALL_END
    assert event.tool_name == "read_file"
    removed_field = "tool_" + "success"
    assert not hasattr(event, removed_field)
    assert event.tool_result == result
    assert event.data["index"] == 3
    assert event.data["tool_result_preview"] == "x" * 500


def test_agent_event_tool_call_protocol_error_contains_payload() -> None:
    event = AgentEvent.tool_call_protocol_error(
        "apply_patch",
        tool_call_id="call-1",
        code="REMOTE_PREVIEW_REQUIRED",
        message="remote peer must provide a tool preview",
    )

    assert event.event_type is AgentEventType.TOOL_CALL_PROTOCOL_ERROR
    assert event.tool_name == "apply_patch"
    assert event.tool_call_id == "call-1"
    assert event.data == {
        "tool_name": "apply_patch",
        "tool_call_id": "call-1",
        "code": "REMOTE_PREVIEW_REQUIRED",
        "message": "remote peer must provide a tool preview",
        "failure_kind": ToolFailureKind.TOOL_PROTOCOL_ERROR.value,
        "tool_diagnostics": [
            {
                "stage": "protocol",
                "kind": "tool_protocol_error",
                "severity": "error",
                "code": "REMOTE_PREVIEW_REQUIRED",
                "message": "remote peer must provide a tool preview",
                "repairable": False,
                "tool_name": "apply_patch",
                "tool_call_id": "call-1",
            }
        ],
    }


def test_agent_event_file_change_started_contains_stable_payload() -> None:
    event = AgentEvent.file_change_started(
        item_id="file-change:call-1",
        tool_call_id="call-1",
        changes=[{"path": "main.py", "kind": "update", "diff": "---\n+ok"}],
    )

    assert event.event_type is AgentEventType.FILE_CHANGE_STARTED
    assert event.tool_call_id == "call-1"
    assert event.data == {
        "item_id": "file-change:call-1",
        "tool_call_id": "call-1",
        "changes": [{"path": "main.py", "kind": "update", "diff": "---\n+ok"}],
        "status": "in_progress",
    }


def test_agent_event_file_change_completed_contains_status_and_error() -> None:
    event = AgentEvent.file_change_completed(
        item_id="file-change:call-1",
        tool_call_id="call-1",
        status="failed",
        error="patch context does not match file",
    )

    assert event.event_type is AgentEventType.FILE_CHANGE_COMPLETED
    assert event.data["status"] == "failed"
    assert event.data["error"] == "patch context does not match file"


def test_agent_event_agent_relation_completed_contains_payload() -> None:
    event = AgentEvent.agent_relation_completed(
        run_id="run-1",
        agent_id="researcher",
        task="scan repo",
        status="ok",
        result="done",
        error=None,
    )
    assert event.event_type is AgentEventType.AGENT_RELATION_COMPLETED
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


def test_agent_event_document_draft_preview_chunk_is_live_only_payload() -> None:
    import hashlib

    event = AgentEvent.document_draft_preview_chunk(
        draft_id="draft-1",
        target_path="docs/architecture.md",
        chunk_seq=3,
        start_offset=12,
        content="正文片段",
        flush_latency_ms=125,
    )

    assert event.event_type is AgentEventType.DOCUMENT_DRAFT_PREVIEW_CHUNK
    assert event.data == {
        "draft_id": "draft-1",
        "target_path": "docs/architecture.md",
        "chunk_seq": 3,
        "start_offset": 12,
        "end_offset": 16,
        "content": "正文片段",
        "content_sha256": hashlib.sha256("正文片段".encode("utf-8")).hexdigest(),
        "flush_latency_ms": 125,
        "status": "streaming",
    }


def test_agent_event_document_draft_preview_chunk_uses_utf16_offsets() -> None:
    import hashlib

    event = AgentEvent.document_draft_preview_chunk(
        draft_id="draft-1",
        target_path="docs/architecture.md",
        chunk_seq=1,
        start_offset=1,
        content="😀B",
    )

    assert event.event_type is AgentEventType.DOCUMENT_DRAFT_PREVIEW_CHUNK
    assert event.data["start_offset"] == 1
    assert event.data["end_offset"] == 4
    assert event.data["content_sha256"] == hashlib.sha256(
        "😀B".encode("utf-8")
    ).hexdigest()


def test_agent_event_document_draft_progress_has_no_body_content() -> None:
    event = AgentEvent.document_draft_progress(
        draft_id="draft-1",
        target_path="docs/architecture.md",
        content_length=7998,
        content_sha256="sha",
        last_chunk_seq=42,
    )

    assert event.event_type is AgentEventType.DOCUMENT_DRAFT_PROGRESS
    assert event.data == {
        "draft_id": "draft-1",
        "target_path": "docs/architecture.md",
        "content_length": 7998,
        "content_sha256": "sha",
        "last_chunk_seq": 42,
        "status": "streaming",
    }
    assert "content" not in event.data


def test_agent_event_document_draft_snapshot_carries_consistent_body_hash() -> None:
    import hashlib

    content = "# Architecture\n\n正文\n"
    event = AgentEvent.document_draft_snapshot(
        draft_id="draft-1",
        target_path="docs/architecture.md",
        content=content,
        snapshot_kind="interrupted",
        final=True,
        last_chunk_seq=7,
    )

    assert event.event_type is AgentEventType.DOCUMENT_DRAFT_SNAPSHOT
    assert event.data == {
        "draft_id": "draft-1",
        "target_path": "docs/architecture.md",
        "content_length": len(content),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content": content,
        "snapshot_kind": "interrupted",
        "final": True,
        "last_chunk_seq": 7,
        "status": "streaming",
    }


def test_agent_event_document_draft_snapshot_uses_utf16_content_length() -> None:
    content = "A😀B"
    event = AgentEvent.document_draft_snapshot(
        draft_id="draft-1",
        target_path="docs/architecture.md",
        content=content,
        snapshot_kind="final",
        final=True,
        last_chunk_seq=1,
    )

    assert event.data["content_length"] == 4

from reuleauxcoder.domain.session.document import (
    apply_session_event,
    empty_session_document,
    settle_orphaned_running_session_run,
    session_metadata_from_document,
)


def _session_run_start(document: dict | None, prompt: str, seq: int) -> dict:
    return apply_session_event(
        document,
        session_id="session-1",
        event_type="session_run_start",
        payload={"prompt": prompt},
        session_event_seq=seq,
        created_at=f"2026-05-21T00:00:0{seq}+00:00",
    )


def _session_run_event(
    document: dict,
    event_type: str,
    payload: dict,
    seq: int,
    *,
    run_id: str = "run-1",
) -> dict:
    return apply_session_event(
        document,
        session_id="session-1",
        event_type=event_type,
        payload=payload,
        session_event_seq=seq,
        session_run_id=run_id,
        session_run_seq=seq,
    )


def _assistant_parts(document: dict) -> list[dict]:
    return document["turns"][-1]["assistantMessages"][-1]["parts"]


def test_session_run_start_initializes_session_title_summary_and_preview() -> None:
    document = _session_run_start(None, "分析项目会话历史问题", 1)

    assert document["session"]["title"] == "分析项目会话历史问题"
    assert document["session"]["summary"] == "分析项目会话历史问题"
    assert document["metadata"]["preview"] == "分析项目会话历史问题"
    assert document["stats"]["taskText"] == "分析项目会话历史问题"
    assert document["turns"][0]["userMessage"]["text"] == "分析项目会话历史问题"


def test_later_session_run_start_keeps_existing_title_summary_and_preview() -> None:
    document = _session_run_start(None, "分析项目会话历史问题", 1)
    document = _session_run_start(document, "请继续", 2)

    assert document["session"]["title"] == "分析项目会话历史问题"
    assert document["session"]["summary"] == "分析项目会话历史问题"
    assert document["metadata"]["preview"] == "分析项目会话历史问题"
    assert document["stats"]["taskText"] == "请继续"
    assert [turn["userMessage"]["text"] for turn in document["turns"]] == [
        "分析项目会话历史问题",
        "请继续",
    ]


def test_tool_parts_insert_by_model_index_when_start_events_arrive_out_of_order() -> None:
    document = _session_run_start(None, "并发读取文件", 1)
    document = _session_run_event(
        document,
        "tool_call_start",
        {
            "tool_call_id": "call-fast",
            "tool_name": "grep",
            "tool_args": {"pattern": "TODO"},
            "index": 1,
        },
        2,
    )
    document = _session_run_event(
        document,
        "tool_call_start",
        {
            "tool_call_id": "call-slow",
            "tool_name": "read_file",
            "tool_args": {"path": "src/a.ts"},
            "index": 0,
        },
        3,
    )

    parts = _assistant_parts(document)
    assert [part["toolCallId"] for part in parts] == ["call-slow", "call-fast"]
    assert [part["preparingIndex"] for part in parts] == [0, 1]


def test_tool_call_end_without_start_keeps_unexecuted_failure_as_error_part() -> None:
    document = _session_run_start(None, "运行受限工具", 1)
    document = _session_run_event(
        document,
        "tool_call_end",
        {
            "tool_call_id": "call-denied",
            "tool_name": "shell",
            "tool_result": "Error: tool 'shell' denied by permission gateway: blocked",
            "index": 0,
            "meta": {
                "tool_diagnostics": [
                    {
                        "stage": "preflight",
                        "kind": "tool_result_error",
                        "severity": "error",
                        "code": "permission_deny",
                        "message": "blocked",
                    }
                ]
            },
        },
        2,
    )

    tool = _assistant_parts(document)[0]
    assert tool["toolCallId"] == "call-denied"
    assert tool["status"] == "error"
    assert tool["preparingIndex"] == 0
    assert tool["resultMeta"]["tool_diagnostics"][0]["code"] == "permission_deny"


def test_workflow_artifact_event_persists_primary_artifact() -> None:
    document = _session_run_start(None, "生成能力包", 1)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="workflow_artifact",
        payload={
            "workflow": "capability_package_ingest",
            "artifact_type": "capability_package_draft",
            "title": "能力包草案 review 已生成",
            "artifact": {
                "package_id": "review",
                "description": "Review package",
                "validation": {"ok": True},
            },
            "raw_event_refs": [{"agent_run_id": "agent-run-1", "seq": 20, "type": "result"}],
        },
        session_event_seq=2,
        created_at="2026-05-21T00:00:02+00:00",
    )

    part = document["turns"][0]["assistantMessages"][0]["parts"][0]
    assert part["type"] == "workflow_artifact"
    assert part["lane"] == "primary"
    assert part["workflow"] == "capability_package_ingest"
    assert part["artifactType"] == "capability_package_draft"
    assert part["artifact"]["package_id"] == "review"
    assert part["artifact"]["validation"]["ok"] is True
    assert part["rawEventRefs"] == [{"agent_run_id": "agent-run-1", "seq": 20, "type": "result"}]


def test_session_run_start_can_initialize_blank_or_default_title() -> None:
    blank_title_document = empty_session_document("session-1")
    blank_title_document["session"]["title"] = " "
    blank_title_document["turns"].append(
        {"userMessage": {"text": ""}, "assistantMessages": []}
    )

    blank_title_document = _session_run_start(blank_title_document, "补全空标题", 2)

    assert blank_title_document["session"]["title"] == "补全空标题"
    assert blank_title_document["session"]["summary"] == "补全空标题"
    assert blank_title_document["metadata"]["preview"] == "补全空标题"

    default_title_document = _session_run_start(None, "", 1)
    assert default_title_document["session"]["title"] == "新会话"

    default_title_document = _session_run_start(default_title_document, "补全默认标题", 2)

    assert default_title_document["session"]["title"] == "补全默认标题"
    assert default_title_document["session"]["summary"] == "补全默认标题"
    assert default_title_document["metadata"]["preview"] == "补全默认标题"


def test_session_metadata_uses_initial_summary_after_multiple_session_run_starts() -> None:
    document = _session_run_start(None, "分析项目会话历史问题", 1)
    document = _session_run_start(document, "请继续", 2)

    metadata = session_metadata_from_document(
        document,
        {
            "id": "session-1",
            "model": "test-model",
            "saved_at": "fallback",
            "preview": "fallback preview",
            "fingerprint": "local",
        },
    )

    assert metadata["preview"] == "分析项目会话历史问题"
    assert metadata["summary"] == "分析项目会话历史问题"
    assert metadata["saved_at"] == "2026-05-21T00:00:02+00:00"


def test_remote_peer_ready_updates_runtime_metadata_without_transcript_card() -> None:
    document = _session_run_start(None, "检查远端状态", 1)

    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="remote_peer_ready",
        payload={
            "peer_id": "peer-1",
            "session_id": "remote-session-1",
            "fingerprint": "fp-1",
            "mode": "agent",
            "model": "model-a",
            "workspace_root": "/workspace",
        },
        session_event_seq=2,
    )

    assert document["stats"]["model"] == "model-a"
    assert document["stats"]["mode"] == "agent"
    assert document["run_state"]["remote_peer"]["peer_id"] == "peer-1"
    assert document["turns"][0]["assistantMessages"] == []
    assert document["parts"] == []


def test_agent_queue_runtime_status_updates_runtime_metadata_without_transcript_card() -> None:
    document = _session_run_start(None, "等待 agent 队列", 1)

    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="runtime_status",
        payload={
            "phase": "agent_queue",
            "status": "queued",
            "agent_type": "chat",
        },
        session_event_seq=2,
    )

    assert document["run_state"]["runtime_status"]["phase"] == "agent_queue"
    assert document["run_state"]["runtime_status"]["status"] == "queued"
    assert document["turns"][0]["assistantMessages"] == []
    assert document["parts"] == []


def test_lifecycle_hook_event_is_projected_into_transcript_process_items() -> None:
    document = _session_run_start(None, "运行 hook", 1)

    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="lifecycle_hook",
        payload={
            "phase": "result",
            "event_name": "PreToolUse",
            "hook_id": "hook:admin:PreToolUse:0",
            "display_name": "Tool guard",
            "decision": "allow",
            "level": "info",
        },
        session_event_seq=2,
        session_run_id="run-1",
        session_run_seq=2,
    )

    part = document["turns"][0]["assistantMessages"][0]["parts"][0]
    assert part["type"] == "ui_event"
    assert part["kind"] == "lifecycle_hook"
    assert part["title"] == "Tool guard"
    assert part["payload"]["hook_id"] == "hook:admin:PreToolUse:0"
    assert part["payload"]["decision"] == "allow"


def test_shell_queue_runtime_status_updates_existing_shell_tool_without_view_card() -> None:
    document = _session_run_start(None, "运行命令", 1)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="tool_call_start",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "shell",
            "tool_source": "builtin",
            "tool_args": {"command": "npm test"},
        },
        session_event_seq=2,
    )

    queued = apply_session_event(
        document,
        session_id="session-1",
        event_type="runtime_status",
        payload={
            "phase": "shell_queue",
            "status": "queued",
            "tool_call_id": "call-1",
        },
        session_event_seq=3,
    )
    queued_tool = queued["turns"][0]["assistantMessages"][0]["parts"][0]
    assert queued_tool["status"] == "pending"
    assert all(part["type"] != "view" for part in queued["turns"][0]["assistantMessages"][0]["parts"])

    running = apply_session_event(
        queued,
        session_id="session-1",
        event_type="runtime_status",
        payload={
            "phase": "shell_queue",
            "status": "running",
            "tool_call_id": "call-1",
        },
        session_event_seq=4,
    )
    running_tool = running["turns"][0]["assistantMessages"][0]["parts"][0]
    assert running_tool["status"] == "running"
    assert all(part["type"] != "view" for part in running["turns"][0]["assistantMessages"][0]["parts"])


def test_shell_queue_runtime_status_without_tool_does_not_create_fallback_card() -> None:
    document = _session_run_start(None, "运行命令", 1)

    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="runtime_status",
        payload={
            "phase": "shell_queue",
            "status": "queued",
            "tool_call_id": "missing-call",
        },
        session_event_seq=2,
    )

    assert document["turns"][0]["assistantMessages"] == []
    assert document["parts"] == []


def test_runtime_projection_contract_keeps_transcript_business_visible() -> None:
    document = _session_run_start(None, "运行测试", 1)
    events = [
        (
            "remote_peer_ready",
            {
                "peer_id": "peer-1",
                "session_id": "remote-session-1",
                "mode": "agent",
                "model": "model-a",
            },
        ),
        (
            "tool_call_start",
            {
                "tool_call_id": "call-1",
                "tool_name": "shell",
                "tool_source": "builtin",
                "tool_args": {"command": "npm test"},
            },
        ),
        (
            "runtime_status",
            {
                "phase": "shell_queue",
                "status": "queued",
                "tool_call_id": "call-1",
            },
        ),
        (
            "runtime_status",
            {
                "phase": "agent_queue",
                "status": "queued",
                "agent_type": "chat",
            },
        ),
        (
            "tool_call_end",
            {
                "tool_call_id": "call-1",
                "tool_name": "shell",
                "tool_source": "builtin",
                "tool_result": "ok",
            },
        ),
        ("session_run_end", {"response": "完成", "response_rendered": False}),
    ]

    for index, (event_type, payload) in enumerate(events, start=2):
        document = apply_session_event(
            document,
            session_id="session-1",
            event_type=event_type,
            payload=payload,
            session_event_seq=index,
        )

    parts = document["turns"][0]["assistantMessages"][0]["parts"]
    assert [part["type"] for part in parts] == ["tool", "assistant_text"]
    assert parts[0]["tool"] == "shell"
    assert parts[0]["status"] == "returned"
    assert parts[0]["output"] == "ok"
    assert parts[1]["markdown"] == "完成"
    assert document["stats"]["runStatus"] == "done"


def test_session_run_end_with_response_finalizes_active_transcript_items() -> None:
    document = _session_run_start(None, "生成能力包", 1)
    document = _session_run_event(
        document,
        "reasoning_delta",
        {"content": "正在分析能力包。"},
        2,
    )
    document = _session_run_event(
        document,
        "assistant_delta",
        {"content": "安装中"},
        3,
    )

    document = _session_run_event(
        document,
        "session_run_end",
        {"response": "能力包已安装。", "response_rendered": False},
        4,
    )

    parts = _assistant_parts(document)
    assert parts[0]["type"] == "thinking"
    assert parts[0]["active"] is False
    assert parts[0]["traceNodeStatus"] == "success"
    assert parts[1]["type"] == "assistant_text"
    assert parts[1]["markdown"] == "能力包已安装。"
    assert parts[1]["streaming"] is False
    assert parts[1]["streamKey"] == "assistant-message"
    assert document["stats"]["runStatus"] == "done"
    assert document["run_state"]["status"] == "done"
    assert document["session"]["state"] == "success"


def test_session_run_end_with_only_final_response_marks_text_success() -> None:
    document = _session_run_start(None, "生成能力包", 1)

    document = _session_run_event(
        document,
        "session_run_end",
        {"response": "能力包已安装。", "response_rendered": False},
        2,
    )

    parts = _assistant_parts(document)
    assert len(parts) == 1
    assert parts[0]["type"] == "assistant_text"
    assert parts[0]["markdown"] == "能力包已安装。"
    assert parts[0]["streaming"] is False
    assert parts[0]["streamKey"] == "assistant-message"
    assert parts[0].get("traceNodeStatus") == "success"
    assert document["stats"]["runStatus"] == "done"
    assert document["run_state"]["status"] == "done"
    assert document["session"]["state"] == "success"


def test_session_run_end_with_rendered_response_finalizes_active_thinking() -> None:
    document = _session_run_start(None, "生成能力包", 1)
    document = _session_run_event(
        document,
        "reasoning_delta",
        {"content": "正在分析能力包。"},
        2,
    )
    document = _session_run_event(
        document,
        "assistant_delta",
        {"content": "能力包已安装。"},
        3,
    )

    document = _session_run_event(
        document,
        "session_run_end",
        {"response": "能力包已安装。", "response_rendered": True},
        4,
    )

    parts = _assistant_parts(document)
    assert parts[0]["type"] == "thinking"
    assert parts[0]["active"] is False
    assert parts[1]["type"] == "assistant_text"
    assert parts[1]["markdown"] == "能力包已安装。"
    assert parts[1]["streaming"] is False
    assert parts[1]["streamKey"] == "assistant-message"
    assert document["stats"]["runStatus"] == "done"
    assert document["session"]["state"] == "success"


def test_output_event_preserves_warning_notice_level() -> None:
    document = _session_run_start(None, "生成能力包", 1)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="output",
        payload={
            "content": "资料抓取问题：The read operation timed out",
            "format": "plain",
            "level": "warning",
        },
        session_event_seq=2,
    )

    part = document["turns"][0]["assistantMessages"][0]["parts"][0]
    assert part["id"] == "output-2"
    assert part["type"] == "notice"
    assert part["level"] == "warning"
    assert part["text"] == "资料抓取问题：The read operation timed out"
    assert part["format"] == "plain"
    assert part["sessionEventSeq"] == 2


def test_tool_events_preserve_raw_agent_run_event_refs_in_history_document() -> None:
    document = _session_run_start(None, "生成能力包", 1)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="tool_call_start",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "fetch_capabilities",
            "tool_args": {"repo": "repo"},
            "raw_event_refs": [{"agent_run_id": "agent-run-1", "seq": 10, "type": "tool_use"}],
        },
        session_event_seq=2,
    )
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="tool_call_end",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "fetch_capabilities",
            "tool_result": "ok",
            "raw_event_refs": [{"agent_run_id": "agent-run-1", "seq": 11, "type": "tool_result"}],
        },
        session_event_seq=3,
    )

    part = document["turns"][0]["assistantMessages"][0]["parts"][0]
    assert part["type"] == "tool"
    assert part["toolCallId"] == "call-1"
    assert part["rawEventRefs"] == [
        {"agent_run_id": "agent-run-1", "seq": 10, "type": "tool_use"},
        {"agent_run_id": "agent-run-1", "seq": 11, "type": "tool_result"},
    ]


def test_live_session_run_events_reduce_into_canonical_transcript_blocks() -> None:
    document = _session_run_start(None, "运行测试", 1)
    events = [
        ("reasoning_delta", {"content": "plan "}),
        ("reasoning_delta", {"content": "more"}),
        ("reasoning_message", {"content": "plan more", "format": "markdown"}),
        ("assistant_delta", {"content": "hel"}),
        ("assistant_delta", {"content": "lo"}),
        ("assistant_message", {"content": "hello", "format": "markdown"}),
        (
            "tool_call_delta",
            {
                "index": 0,
                "tool_name": "shell",
                "arguments_preview": '{"command":"npm test"}',
            },
        ),
        (
            "tool_call_start",
            {
                "index": 0,
                "tool_call_id": "call-1",
                "tool_name": "shell",
                "tool_args": {"command": "npm test"},
            },
        ),
        (
            "tool_call_stream",
            {
                "tool_call_id": "call-1",
                "tool_name": "shell",
                "stream": "stdout",
                "content": "ok",
            },
        ),
        (
            "tool_call_end",
            {
                "tool_call_id": "call-1",
                "tool_name": "shell",
                "tool_result": "ok\n",
            },
        ),
        ("session_run_end", {"response": "hello", "response_rendered": True}),
    ]

    for index, (event_type, payload) in enumerate(events, start=2):
        document = apply_session_event(
            document,
            session_id="session-1",
            event_type=event_type,
            payload=payload,
            session_event_seq=index,
            session_run_id="run-1",
            session_run_seq=index,
        )

    parts = document["turns"][0]["assistantMessages"][0]["parts"]
    assert [part["type"] for part in parts] == [
        "reasoning",
        "assistant_text",
        "tool",
    ]
    assert parts[0]["raw"] == "plan more"
    assert parts[0]["id"] == "thinking-run-1"
    assert parts[1]["markdown"] == "hello"
    assert parts[1]["id"] == "assistant-stream-run-1"
    assert parts[1]["streaming"] is False
    assert parts[1]["streamKey"] == "assistant-message"
    assert parts[2]["toolCallId"] == "call-1"
    assert parts[2]["id"] == "tool-preparing:run-1:0"
    assert parts[2]["status"] == "returned"
    assert parts[2]["output"] == "ok"
    assert parts[2]["outputChunks"] == [{"stream": "stdout", "content": "ok"}]
    assert parts[2]["finalOutput"] == "ok\n"
    assert document["stats"]["runStatus"] == "done"


def test_file_change_events_reduce_into_single_replayable_part() -> None:
    document = _session_run_start(None, "修改文件", 1)
    changes = [
        {
            "path": "main.py",
            "kind": "update",
            "diff": "--- a/main.py\n+++ b/main.py\n@@\n-old\n+new\n",
        }
    ]
    events = [
        (
            "file_change_started",
            {
                "item_id": "file-change:call-1",
                "tool_call_id": "call-1",
                "changes": [],
                "status": "in_progress",
            },
        ),
        (
            "file_change_patch_updated",
            {
                "item_id": "file-change:call-1",
                "tool_call_id": "call-1",
                "changes": changes,
                "patch_preview": "*** Begin Patch",
            },
        ),
        (
            "file_change_approval_requested",
            {
                "item_id": "file-change:call-1",
                "tool_call_id": "call-1",
                "approval_id": "approval:call-1",
                "reason": "requires approval",
            },
        ),
        (
            "file_change_approval_resolved",
            {
                "item_id": "file-change:call-1",
                "tool_call_id": "call-1",
                "approval_id": "approval:call-1",
                "decision": "allow_once",
            },
        ),
        (
            "file_change_completed",
            {
                "item_id": "file-change:call-1",
                "tool_call_id": "call-1",
                "changes": changes,
                "status": "completed",
            },
        ),
    ]

    for index, (event_type, payload) in enumerate(events, start=2):
        document = _session_run_event(document, event_type, payload, index)

    parts = _assistant_parts(document)
    assert len(parts) == 1
    part = parts[0]
    assert part["type"] == "file_change"
    assert part["itemId"] == "file-change:call-1"
    assert part["toolCallId"] == "call-1"
    assert part["status"] == "completed"
    assert part["path"] == "main.py"
    assert part["addedLines"] == 1
    assert part["removedLines"] == 1
    assert part["approvalId"] == "approval:call-1"
    assert part["approvalDecision"] == "allow_once"
    assert part["diff"].endswith("-old\n+new")


def test_document_draft_events_reduce_into_single_status_part() -> None:
    document = _session_run_start(None, "生成文档", 1)
    events = [
        (
            "document_draft_started",
            {
                "draft_id": "draft-1",
                "target_path": "docs/architecture.md",
                "title": "Architecture",
                "format": "markdown",
            },
        ),
        (
            "document_draft_commit_requested",
            {
                "draft_id": "draft-1",
                "target_path": "docs/architecture.md",
                "item_id": "file-change:draft:draft-1",
                "approval_id": "approval:draft:draft-1",
            },
        ),
        (
            "document_draft_committed",
            {
                "draft_id": "draft-1",
                "target_path": "docs/architecture.md",
                "item_id": "file-change:draft:draft-1",
            },
        ),
    ]

    for index, (event_type, payload) in enumerate(events, start=2):
        document = _session_run_event(document, event_type, payload, index)

    parts = _assistant_parts(document)
    assert len(parts) == 1
    part = parts[0]
    assert part["type"] == "document_draft"
    assert part["draftId"] == "draft-1"
    assert part["targetPath"] == "docs/architecture.md"
    assert part["itemId"] == "file-change:draft:draft-1"
    assert part["approvalId"] == "approval:draft:draft-1"
    assert part["status"] == "committed"


def test_document_draft_delta_updates_count_without_assistant_text() -> None:
    document = _session_run_start(None, "生成文档", 1)
    document = _session_run_event(
        document,
        "document_draft_started",
        {
            "draft_id": "draft-1",
            "target_path": "docs/architecture.md",
            "title": "Architecture",
            "format": "markdown",
        },
        2,
    )
    document = _session_run_event(
        document,
        "document_draft_delta",
        {
            "draft_id": "draft-1",
            "target_path": "docs/architecture.md",
            "content": "# Architecture\n",
        },
        3,
    )
    document = _session_run_event(
        document,
        "document_draft_delta",
        {
            "draft_id": "draft-1",
            "target_path": "docs/architecture.md",
            "content": "\nBody\n",
        },
        4,
    )

    parts = _assistant_parts(document)
    assert len(parts) == 1
    assert parts[0]["type"] == "document_draft"
    assert parts[0]["draftId"] == "draft-1"
    assert parts[0]["contentLength"] == len("# Architecture\n\nBody\n")
    rendered = str(document["turns"][0]["assistantMessages"])
    assert "# Architecture" not in rendered
    assert "Body" not in rendered


def test_session_run_end_finalizes_existing_stream_without_duplicate_when_response_not_rendered() -> None:
    document = _session_run_start(None, "运行测试", 1)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="assistant_delta",
        payload={"content": "hel"},
        session_event_seq=2,
        session_run_id="run-1",
        session_run_seq=2,
    )
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="session_run_end",
        payload={"response": "hello", "response_rendered": False},
        session_event_seq=3,
        session_run_id="run-1",
        session_run_seq=3,
    )

    parts = document["turns"][0]["assistantMessages"][0]["parts"]
    assert parts == [
        {
            "id": "assistant-stream-run-1",
            "type": "assistant_text",
            "markdown": "hello",
            "format": "markdown",
            "streaming": False,
            "streamKey": "assistant-message",
            "eventKey": "session:session-1:3",
            "sessionEventSeq": 3,
            "traceNodeStatus": "success",
        }
    ]


def test_provider_stream_interrupted_reduces_to_replayable_warning_notice() -> None:
    document = _session_run_start(None, "运行测试", 1)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="provider_stream_interrupted",
        payload={"message": "stream lost"},
        session_event_seq=2,
        session_run_id="run-1",
        session_run_seq=2,
    )

    parts = document["turns"][0]["assistantMessages"][0]["parts"]
    assert parts == [
        {
            "id": "stream-recovery-2",
            "type": "notice",
            "level": "warning",
            "text": "stream lost",
            "format": "plain",
            "eventKey": "session:session-1:2",
            "sessionEventSeq": 2,
        }
    ]


def test_provider_stream_interrupted_falls_back_to_session_locale_message_key() -> None:
    document = apply_session_event(
        None,
        session_id="session-1",
        event_type="session_run_start",
        payload={"prompt": "运行测试", "locale": "en"},
        session_event_seq=1,
        session_run_id="run-1",
        session_run_seq=1,
    )
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="provider_stream_interrupted",
        payload={"message_key": "provider_stream_interrupted.recovering"},
        session_event_seq=2,
        session_run_id="run-1",
        session_run_seq=2,
    )

    parts = document["turns"][0]["assistantMessages"][0]["parts"]
    assert parts[0]["text"] == "The model output stream was interrupted. Trying to recover."


def test_session_run_failed_falls_back_to_localized_capability_message() -> None:
    document = apply_session_event(
        None,
        session_id="session-1",
        event_type="session_run_start",
        payload={"prompt": "生成能力包", "locale": "zh-CN"},
        session_event_seq=1,
        session_run_id="run-1",
        session_run_seq=1,
    )
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="session_run_failed",
        payload={
            "code": "capability_package_session_failed",
            "message_key": "capability_package.session_failed",
            "diagnostic_message": "boom",
        },
        session_event_seq=2,
        session_run_id="run-1",
        session_run_seq=2,
    )

    parts = document["turns"][0]["assistantMessages"][0]["parts"]
    assert parts[0]["text"] == "错误：能力包流程执行失败。"
    assert document["run_state"]["error"] == "能力包流程执行失败。"


def test_session_run_failed_finalizes_active_streams_and_running_tools() -> None:
    document = _session_run_start(None, "运行测试", 1)
    document = _session_run_event(document, "reasoning_delta", {"content": "plan"}, 2)
    document = _session_run_event(document, "assistant_delta", {"content": "partial"}, 3)
    document = _session_run_event(
        document,
        "tool_call_start",
        {
            "tool_call_id": "call-running",
            "tool_name": "shell",
            "tool_args": {"command": "npm test"},
        },
        4,
    )
    document = _session_run_event(
        document,
        "approval_request",
        {
            "approval_id": "approval-1",
            "tool_call_id": "call-approval",
            "tool_name": "shell",
            "reason": "need approval",
            "tool_args": {"command": "npm publish"},
        },
        5,
    )

    document = _session_run_event(document, "session_run_failed", {"message": "boom"}, 6)

    parts = _assistant_parts(document)
    thinking = next(part for part in parts if part["type"] == "thinking")
    assistant_text = next(part for part in parts if part["type"] == "assistant_text")
    running_tool = next(part for part in parts if part.get("toolCallId") == "call-running")
    approval_tool = next(part for part in parts if part.get("toolCallId") == "call-approval")
    assert thinking["active"] is False
    assert thinking["traceNodeStatus"] == "error"
    assert assistant_text["streaming"] is False
    assert assistant_text["streamKey"] == "assistant-message"
    assert assistant_text["traceNodeStatus"] == "error"
    assert running_tool["status"] == "error"
    assert running_tool["traceNodeStatus"] == "error"
    assert approval_tool["status"] == "denied"
    assert approval_tool["approvalDecision"] == "deny_once"
    assert document["stats"]["runStatus"] == "error"
    assert document["run_state"]["status"] == "error"
    assert document["session"]["state"] == "error"


def test_shell_tool_stream_truncates_like_frontend_and_preserves_visible_output_on_end() -> None:
    document = _session_run_start(None, "运行测试", 1)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="tool_call_start",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "shell",
            "tool_args": {"command": "yes"},
        },
        session_event_seq=2,
        session_run_id="run-1",
        session_run_seq=2,
    )
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="tool_call_stream",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "shell",
            "stream": "stdout",
            "content": f"{'x' * 21000}tail",
        },
        session_event_seq=3,
        session_run_id="run-1",
        session_run_seq=3,
    )
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="tool_call_end",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "shell",
            "tool_result": "final-only",
        },
        session_event_seq=4,
        session_run_id="run-1",
        session_run_seq=4,
    )

    tool = document["turns"][0]["assistantMessages"][0]["parts"][0]
    assert tool["outputChunks"][0] == {
        "stream": "system",
        "content": "\n... 输出过长，已截断早期内容，保留最近输出 ...\n",
        "truncated": True,
    }
    assert tool["outputTruncated"] is True
    assert len(tool["output"]) <= 20000
    assert tool["output"].endswith("tail")
    assert tool["finalOutput"] == "final-only"


def test_session_run_cancelled_settles_running_tool_and_appends_notice() -> None:
    document = _session_run_start(None, "运行测试", 1)
    document = _session_run_event(document, "reasoning_delta", {"content": "plan"}, 2)
    document = _session_run_event(document, "assistant_delta", {"content": "partial"}, 3)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="tool_call_start",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "shell",
            "tool_args": {"command": "npm test"},
        },
        session_event_seq=4,
        session_run_id="run-1",
        session_run_seq=4,
    )
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="session_run_cancelled",
        payload={"reason": "user_cancelled"},
        session_event_seq=5,
        session_run_id="run-1",
        session_run_seq=5,
    )

    parts = document["turns"][0]["assistantMessages"][0]["parts"]
    assert parts[0]["type"] == "thinking"
    assert parts[0]["active"] is False
    assert parts[1]["type"] == "assistant_text"
    assert parts[1]["streaming"] is False
    assert parts[1]["streamKey"] == "assistant-message"
    assert parts[2]["type"] == "tool"
    assert parts[2]["status"] == "cancelled"
    assert parts[2]["traceNodeStatus"] == "cancelled"
    assert parts[3] == {
        "id": "cancelled-5",
        "type": "notice",
        "level": "info",
        "text": "已取消当前请求。",
        "format": "plain",
        "eventKey": "session:session-1:5",
        "sessionEventSeq": 5,
        "traceNodeStatus": "cancelled",
    }
    assert document["stats"]["runStatus"] == "cancelled"
    assert document["run_state"]["status"] == "cancelled"
    assert document["session"]["state"] == "cancelled"


def test_session_run_interrupted_finalizes_active_items_without_failing_session() -> None:
    document = _session_run_start(None, "继续生成", 1)
    document = _session_run_event(document, "reasoning_delta", {"content": "plan"}, 2)
    document = _session_run_event(document, "assistant_delta", {"content": "partial"}, 3)
    document = _session_run_event(
        document,
        "tool_call_start",
        {
            "tool_call_id": "call-1",
            "tool_name": "shell",
            "tool_args": {"command": "npm test"},
        },
        4,
    )

    document = _session_run_event(
        document,
        "session_run_interrupted",
        {"message_key": "provider_stream.interrupted_can_continue", "locale": "zh-CN"},
        5,
    )

    parts = _assistant_parts(document)
    thinking = next(part for part in parts if part["type"] == "thinking")
    assistant_text = next(part for part in parts if part["type"] == "assistant_text")
    tool = next(part for part in parts if part["type"] == "tool")
    notice = next(part for part in parts if part["type"] == "notice")
    assert thinking["active"] is False
    assert assistant_text["streaming"] is False
    assert assistant_text["streamKey"] == "assistant-message"
    assert tool["status"] == "cancelled"
    assert notice["level"] == "warning"
    assert "模型输出流中断" in notice["text"]
    assert document["stats"]["runStatus"] == "interrupted"
    assert document["run_state"]["status"] == "interrupted"
    assert document["session"]["state"] == "active"


def test_session_run_interrupted_uses_english_prefix_for_english_locale() -> None:
    document = apply_session_event(
        None,
        session_id="session-1",
        event_type="session_run_start",
        payload={"prompt": "Continue generation", "locale": "en"},
        session_event_seq=1,
        session_run_id="run-1",
        session_run_seq=1,
    )

    document = _session_run_event(
        document,
        "session_run_interrupted",
        {"message_key": "provider_stream.interrupted_can_continue"},
        2,
    )

    notice = next(part for part in _assistant_parts(document) if part["type"] == "notice")
    assert notice["text"].startswith("Output interrupted: ")
    assert "The model output stream was interrupted." in notice["text"]
    assert "输出中断" not in notice["text"]


def test_error_then_session_run_failed_keeps_single_error_notice() -> None:
    document = _session_run_start(None, "运行测试", 1)
    for seq, event_type in enumerate(["error", "session_run_failed"], start=2):
        document = apply_session_event(
            document,
            session_id="session-1",
            event_type=event_type,
            payload={"message": "boom"},
            session_event_seq=seq,
            session_run_id="run-1",
            session_run_seq=seq,
        )

    parts = document["turns"][0]["assistantMessages"][0]["parts"]
    notices = [part for part in parts if part["type"] == "notice"]
    assert notices == [
        {
            "id": "error-2",
            "type": "notice",
            "level": "error",
            "text": "错误：boom",
            "format": "plain",
            "eventKey": "session:session-1:2",
            "sessionEventSeq": 2,
            "traceNodeStatus": "error",
        }
    ]
    assert document["stats"]["runStatus"] == "error"

    document = _session_run_start(document, "第二轮", 4)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="session_run_failed",
        payload={"message": "again failed"},
        session_event_seq=5,
        session_run_id="run-2",
        session_run_seq=5,
    )
    notices = [
        part
        for turn in document["turns"]
        for message in turn["assistantMessages"]
        for part in message["parts"]
        if part["type"] == "notice"
    ]
    assert len(notices) == 2
    assert notices[1]["text"] == "错误：again failed"


def test_terminal_error_settles_stale_pending_approval() -> None:
    document = _session_run_start(None, "需要审批", 1)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="approval_request",
        payload={
            "approval_id": "approval-1",
            "tool_call_id": "call-1",
            "tool_name": "shell",
            "tool_source": "builtin",
            "reason": "need approval",
            "tool_args": {"command": "echo hi"},
        },
        session_event_seq=2,
    )
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="error",
        payload={"message": "peer_disconnected: peer_shutdown"},
        session_event_seq=3,
    )

    tool = document["turns"][0]["assistantMessages"][0]["parts"][0]
    assert tool["status"] == "denied"
    assert tool["approvalDecision"] == "deny_once"
    assert tool["approvalResultReason"] == "peer_disconnected: peer_shutdown"


def test_approval_request_preserves_lifecycle_hook_identity_metadata() -> None:
    document = _session_run_start(None, "需要审批", 1)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="approval_request",
        payload={
            "approval_id": "approval-1",
            "tool_call_id": "call-1",
            "tool_name": "lifecycle:UserPromptSubmit",
            "tool_source": "lifecycle_hook",
            "reason": "Review prompt before continuing.",
            "tool_args": {"user_input": "install linked skill"},
            "lifecycle_event": "UserPromptSubmit",
            "lifecycle_hooks": [
                {
                    "hook_id": "hook:admin:prompt-review:UserPromptSubmit:0",
                    "display_name": "Prompt review",
                    "handler_type": "prompt",
                    "reason": "Review prompt before continuing.",
                }
            ],
        },
        session_event_seq=2,
    )

    tool = document["turns"][0]["assistantMessages"][0]["parts"][0]
    assert tool["resultMeta"]["lifecycle_event"] == "UserPromptSubmit"
    assert tool["resultMeta"]["lifecycle_hooks"] == [
        {
            "hook_id": "hook:admin:prompt-review:UserPromptSubmit:0",
            "display_name": "Prompt review",
            "handler_type": "prompt",
            "reason": "Review prompt before continuing.",
        }
    ]


def test_orphaned_running_session_run_settles_stale_pending_approval() -> None:
    document = _session_run_start(None, "需要审批", 1)
    document["run_state"]["session_run_id"] = "run-missing"
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="approval_request",
        payload={
            "approval_id": "approval-1",
            "tool_call_id": "call-1",
            "tool_name": "shell",
            "tool_source": "builtin",
            "reason": "need approval",
            "tool_args": {
                "command": "npm view @jshookmcp/jshook@0.1.8 version description dependencies --json 2>&1"
            },
        },
        session_event_seq=2,
    )

    repaired = settle_orphaned_running_session_run(
        document,
        session_id="session-1",
        reason="审批已失效：远端运行已结束，请重新发起任务。",
    )

    tool = repaired["turns"][0]["assistantMessages"][0]["parts"][0]
    assert repaired["stats"]["runStatus"] == "error"
    assert repaired["run_state"]["status"] == "error"
    assert repaired["run_state"]["error"] == "审批已失效：远端运行已结束，请重新发起任务。"
    assert tool["status"] == "denied"
    assert tool["approvalDecision"] == "deny_once"
    assert tool["approvalResultReason"] == "审批已失效：远端运行已结束，请重新发起任务。"

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


def test_capability_package_draft_event_persists_structured_card() -> None:
    document = _session_run_start(None, "生成能力包", 1)
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="capability_package_draft",
        payload={
            "title": "能力包草案 review 已生成",
            "package_id": "review",
            "draft": {"id": "review", "description": "Review package"},
            "validation": {"ok": True},
            "raw_event_refs": [{"agent_run_id": "agent-run-1", "seq": 20, "type": "result"}],
        },
        session_event_seq=2,
        created_at="2026-05-21T00:00:02+00:00",
    )

    part = document["turns"][0]["assistantMessages"][0]["parts"][0]
    assert part["type"] == "capability_package_draft"
    assert part["packageId"] == "review"
    assert part["draft"]["id"] == "review"
    assert part["validation"]["ok"] is True
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
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="tool_call_start",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "shell",
            "tool_args": {"command": "npm test"},
        },
        session_event_seq=2,
        session_run_id="run-1",
        session_run_seq=2,
    )
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type="session_run_cancelled",
        payload={"reason": "user_cancelled"},
        session_event_seq=3,
        session_run_id="run-1",
        session_run_seq=3,
    )

    parts = document["turns"][0]["assistantMessages"][0]["parts"]
    assert parts[0]["type"] == "tool"
    assert parts[0]["status"] == "cancelled"
    assert parts[0]["traceNodeStatus"] == "cancelled"
    assert parts[1] == {
        "id": "cancelled-3",
        "type": "notice",
        "level": "info",
        "text": "已取消当前请求。",
        "format": "plain",
        "eventKey": "session:session-1:3",
        "sessionEventSeq": 3,
        "traceNodeStatus": "cancelled",
    }
    assert document["stats"]["runStatus"] == "cancelled"


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

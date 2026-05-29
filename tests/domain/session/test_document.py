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
    assert [part["type"] for part in parts] == ["tool", "text"]
    assert parts[0]["tool"] == "shell"
    assert parts[0]["status"] == "returned"
    assert parts[0]["toolOutput"] == "ok"
    assert parts[1]["text"] == "完成"
    assert document["stats"]["runStatus"] == "done"


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


def test_orphaned_running_chat_settles_stale_pending_approval() -> None:
    document = _session_run_start(None, "需要审批", 1)
    document["run_state"]["session_run_id"] = "chat-missing"
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

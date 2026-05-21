from reuleauxcoder.domain.session.document import (
    apply_session_event,
    empty_session_document,
    session_metadata_from_document,
)


def _chat_start(document: dict | None, prompt: str, seq: int) -> dict:
    return apply_session_event(
        document,
        session_id="session-1",
        event_type="chat_start",
        payload={"prompt": prompt},
        session_event_seq=seq,
        created_at=f"2026-05-21T00:00:0{seq}+00:00",
    )


def test_chat_start_initializes_session_title_summary_and_preview() -> None:
    document = _chat_start(None, "分析项目会话历史问题", 1)

    assert document["session"]["title"] == "分析项目会话历史问题"
    assert document["session"]["summary"] == "分析项目会话历史问题"
    assert document["metadata"]["preview"] == "分析项目会话历史问题"
    assert document["stats"]["taskText"] == "分析项目会话历史问题"
    assert document["turns"][0]["userMessage"]["text"] == "分析项目会话历史问题"


def test_later_chat_start_keeps_existing_title_summary_and_preview() -> None:
    document = _chat_start(None, "分析项目会话历史问题", 1)
    document = _chat_start(document, "请继续", 2)

    assert document["session"]["title"] == "分析项目会话历史问题"
    assert document["session"]["summary"] == "分析项目会话历史问题"
    assert document["metadata"]["preview"] == "分析项目会话历史问题"
    assert document["stats"]["taskText"] == "请继续"
    assert [turn["userMessage"]["text"] for turn in document["turns"]] == [
        "分析项目会话历史问题",
        "请继续",
    ]


def test_chat_start_can_initialize_blank_or_default_title() -> None:
    blank_title_document = empty_session_document("session-1")
    blank_title_document["session"]["title"] = " "
    blank_title_document["turns"].append(
        {"userMessage": {"text": ""}, "assistantMessages": []}
    )

    blank_title_document = _chat_start(blank_title_document, "补全空标题", 2)

    assert blank_title_document["session"]["title"] == "补全空标题"
    assert blank_title_document["session"]["summary"] == "补全空标题"
    assert blank_title_document["metadata"]["preview"] == "补全空标题"

    default_title_document = _chat_start(None, "", 1)
    assert default_title_document["session"]["title"] == "新会话"

    default_title_document = _chat_start(default_title_document, "补全默认标题", 2)

    assert default_title_document["session"]["title"] == "补全默认标题"
    assert default_title_document["session"]["summary"] == "补全默认标题"
    assert default_title_document["metadata"]["preview"] == "补全默认标题"


def test_session_metadata_uses_initial_summary_after_multiple_chat_starts() -> None:
    document = _chat_start(None, "分析项目会话历史问题", 1)
    document = _chat_start(document, "请继续", 2)

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

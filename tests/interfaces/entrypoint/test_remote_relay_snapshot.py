from reuleauxcoder.interfaces.entrypoint.remote_relay import (
    _merge_snapshot_with_session_messages,
)


def test_snapshot_save_repairs_stale_final_assistant_text() -> None:
    snapshot = {
        "turns": [
            {
                "userMessage": {"text": "question", "parts": []},
                "assistantMessages": [
                    {
                        "id": "assistant-1",
                        "role": "assistant",
                        "parts": [
                            {
                                "id": "part-1",
                                "type": "text",
                                "text": "partial answer",
                                "textFormat": "markdown",
                            }
                        ],
                    }
                ],
            }
        ]
    }
    full_answer = "partial answer with the complete final section"

    merged = _merge_snapshot_with_session_messages(
        snapshot,
        [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": full_answer},
        ],
    )

    assistant = merged["turns"][0]["assistantMessages"][0]
    assert assistant["text"] == full_answer
    assert assistant["parts"][-1]["text"] == full_answer
    assert snapshot["turns"][0]["assistantMessages"][0]["parts"][-1]["text"] == "partial answer"


def test_snapshot_save_appends_missing_turns_from_history_messages() -> None:
    snapshot = {
        "turns": [
            {
                "userMessage": {"text": "first question", "parts": []},
                "assistantMessages": [
                    {
                        "id": "assistant-1",
                        "role": "assistant",
                        "parts": [
                            {
                                "id": "part-1",
                                "type": "text",
                                "text": "first answer",
                                "textFormat": "markdown",
                            }
                        ],
                    }
                ],
            }
        ]
    }

    merged = _merge_snapshot_with_session_messages(
        snapshot,
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "write the file"},
            {
                "role": "assistant",
                "content": "Now writing the comprehensive synthesis document.",
            },
            {
                "role": "tool",
                "tool_call_id": "call-write",
                "content": "Error: bad arguments for write_file",
            },
        ],
    )

    assert len(merged["turns"]) == 2
    assert merged["turns"][1]["userMessage"]["text"] == "write the file"
    assert (
        merged["turns"][1]["assistantMessages"][0]["parts"][0]["text"]
        == "Now writing the comprehensive synthesis document."
    )
    assert merged["turns"][1]["assistantMessages"][1]["parts"][0]["toolOutput"] == (
        "Error: bad arguments for write_file"
    )

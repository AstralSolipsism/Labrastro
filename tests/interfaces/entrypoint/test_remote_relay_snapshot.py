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

from reuleauxcoder.domain.session.models import Session


def test_session_preview_uses_first_user_and_latest_assistant_message() -> None:
    session = Session(
        id="s1",
        model="test",
        saved_at="now",
        messages=[
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "first topic\nwith newline"},
            {"role": "assistant", "content": "intermediate answer"},
            {"role": "user", "content": "follow up"},
            {"role": "assistant", "content": "latest visible state"},
        ],
    )

    assert session.get_preview() == "first topic with newline ... latest visible state"


def test_session_preview_uses_first_user_as_latest_when_no_assistant_reply() -> None:
    session = Session(
        id="s1",
        model="test",
        saved_at="now",
        messages=[
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "x" * 100},
            {"role": "tool", "content": "ignored"},
        ],
    )

    assert session.get_preview() == f"{'x' * 60} ... {'x' * 60}"


def test_session_preview_ignores_non_text_content() -> None:
    session = Session(
        id="s1",
        model="test",
        saved_at="now",
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "structured"}]},
            {"role": "assistant", "content": None},
        ],
    )

    assert session.get_preview() == ""

from reuleauxcoder.domain.session.document import apply_session_event


def test_session_run_start_sets_run_state_without_legacy_chat_fields() -> None:
    document = apply_session_event(
        None,
        session_id="session-1",
        event_type="session_run_start",
        payload={"prompt": "继续推进"},
        session_event_seq=1,
        session_run_id="run-1",
        session_run_seq=1,
    )

    assert document["run_state"]["session_run_id"] == "run-1"
    assert document["run_state"]["last_session_run_seq"] == 1
    assert "chat_id" not in document["run_state"]
    assert "last_chat_seq" not in document["run_state"]
    assert document["stats"]["runStatus"] == "running"
    assert document["turns"][0]["userMessage"]["text"] == "继续推进"

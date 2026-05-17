from types import SimpleNamespace

from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.cli.repl import _auto_save_session
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


def _agent_with_messages() -> SimpleNamespace:
    messages = [{"role": "user", "content": "hello"}]
    return SimpleNamespace(
        llm=SimpleNamespace(model="runtime-model", debug_trace=False),
        state=SimpleNamespace(
            total_prompt_tokens=3,
            total_completion_tokens=5,
        ),
        messages=messages,
        active_mode="coder",
        session_fingerprint="local",
    )


def test_auto_save_session_saves_and_updates_current_id(tmp_path) -> None:
    config = Config(session_dir=str(tmp_path))
    agent = _agent_with_messages()
    ui_bus = UIEventBus()

    sid = _auto_save_session(agent, config, tmp_path, None, ui_bus)

    assert sid
    assert agent.current_session_id == sid
    loaded = SessionStore(tmp_path).load(sid)
    assert loaded is not None
    assert loaded.model == "runtime-model"
    assert loaded.messages[0]["content"] == "hello"
    assert any(event.kind == UIEventKind.SESSION for event in ui_bus._history)


def test_auto_save_session_noops_when_disabled(tmp_path) -> None:
    config = Config(session_dir=str(tmp_path))
    config.session_auto_save = False
    agent = _agent_with_messages()

    sid = _auto_save_session(agent, config, tmp_path, None, UIEventBus())

    assert sid is None
    assert not hasattr(agent, "current_session_id")
    assert SessionStore(tmp_path).list(limit=10, fingerprint=None) == []

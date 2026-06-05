from pathlib import Path
from types import SimpleNamespace

from reuleauxcoder.domain.config.models import ApprovalConfig, Config
from reuleauxcoder.domain.hooks.base import ObserverHook
from reuleauxcoder.domain.hooks.lifecycle import (
    LifecycleHookDeclaration,
    LifecycleHookDispatcher,
    LifecycleHookOutput,
    LifecycleHookRuntimeAdapter,
    LifecycleHookRuntimeAdapterRegistry,
    LifecycleHookRegistry,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import HookPoint, SessionSaveContext
from reuleauxcoder.domain.session.models import SessionRuntimeState
from reuleauxcoder.extensions.command.builtin.sessions import (
    ListSessionsCommand,
    NewSessionCommand,
    ResumeSessionCommand,
    SaveSessionCommand,
    _handle_list_sessions,
    _handle_new_session,
    _handle_resume_session,
    _handle_save_session,
)
from reuleauxcoder.extensions.command.builtin.system import ExitCommand, _handle_exit
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind, UIEventLevel


class FakeLLM:
    def __init__(self) -> None:
        self.model = "base-model"
        self.debug_trace = False

    def reconfigure(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeContext:
    def __init__(self) -> None:
        self.max_tokens = 64000

    def reconfigure(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens


class FakeAgent:
    def __init__(self) -> None:
        self.llm = FakeLLM()
        self.context = FakeContext()
        self.state = SimpleNamespace(
            messages=[],
            total_prompt_tokens=0,
            total_completion_tokens=0,
            current_round=0,
        )
        self.messages = self.state.messages
        self.available_modes = {"coder": SimpleNamespace(name="coder", description="")}
        self.active_mode = None
        self.hook_registry = HookRegistry()

    def set_mode(self, mode_name: str) -> None:
        self.active_mode = mode_name

    def reset(self) -> None:
        self.state.messages.clear()
        self.messages = self.state.messages
        self.state.total_prompt_tokens = 0
        self.state.total_completion_tokens = 0
        self.state.current_round = 0


class CaptureInternalLifecycleAdapter(LifecycleHookRuntimeAdapter):
    handler_type = "internal"
    supported_events = {"SessionEnd"}
    supported_placements = {"server"}

    def unavailable_reason(self, declaration, *, placement=None):
        del declaration, placement
        return ""

    def dispatch(self, declaration, context):
        del declaration, context
        return LifecycleHookOutput.from_dict(
            {"diagnostics": [{"code": "session_end_observed"}]}
        )


def _build_ctx(tmp_path: Path, *, fingerprint: str = "local") -> SimpleNamespace:
    config = Config(approval=ApprovalConfig(), session_dir=str(tmp_path))
    agent = FakeAgent()
    setattr(agent, "session_fingerprint", fingerprint)
    ui_bus = UIEventBus()
    return SimpleNamespace(
        config=config, agent=agent, ui_bus=ui_bus, sessions_dir=tmp_path
    )


def test_list_sessions_defaults_to_current_fingerprint(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "local msg"}],
        model="m1",
        fingerprint="local",
    )
    store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="m2",
        fingerprint="remote:abc",
    )
    ctx = _build_ctx(tmp_path, fingerprint="local")

    result = _handle_list_sessions(ListSessionsCommand(), ctx)

    assert [item["id"] for item in result.payload["sessions"]] == [local_id]
    assert result.payload["show_all"] is False
    assert result.payload["fingerprint"] == "local"


def test_list_sessions_all_shows_all_fingerprints(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "local msg"}],
        model="m1",
        fingerprint="local",
    )
    remote_id = store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="m2",
        fingerprint="remote:abc",
    )
    ctx = _build_ctx(tmp_path, fingerprint="local")

    result = _handle_list_sessions(ListSessionsCommand(show_all=True), ctx)

    assert {item["id"] for item in result.payload["sessions"]} == {local_id, remote_id}
    assert result.payload["show_all"] is True
    assert {item["fingerprint"] for item in result.payload["sessions"]} == {
        "local",
        "remote:abc",
    }


def test_resume_latest_uses_current_fingerprint_only(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "local msg"}],
        model="m1",
        fingerprint="local",
        runtime_state=SessionRuntimeState(model="m1", active_mode="coder"),
    )
    store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="m2",
        fingerprint="remote:abc",
        runtime_state=SessionRuntimeState(model="m2", active_mode="coder"),
    )
    ctx = _build_ctx(tmp_path, fingerprint="local")

    result = _handle_resume_session(ResumeSessionCommand(target="latest"), ctx)

    assert result.session_id == local_id
    assert ctx.agent.session_fingerprint == "local"
    assert any(
        event.level == UIEventLevel.SUCCESS
        and event.kind == UIEventKind.SESSION
        and local_id in event.message
        for event in ctx.ui_bus._history
    )


def test_resume_cross_fingerprint_by_id_warns_but_allows(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    remote_id = store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="m2",
        fingerprint="remote:abc",
        runtime_state=SessionRuntimeState(model="m2", active_mode="coder"),
    )
    ctx = _build_ctx(tmp_path, fingerprint="local")

    result = _handle_resume_session(ResumeSessionCommand(target=remote_id), ctx)

    assert result.session_id == remote_id
    assert ctx.agent.session_fingerprint == "remote:abc"
    assert any(
        event.level == UIEventLevel.WARNING
        and event.kind == UIEventKind.SESSION
        and "belongs to fingerprint 'remote:abc'" in event.message
        for event in ctx.ui_bus._history
    )


def test_new_session_skips_auto_save_when_disabled(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    ctx.config.session_auto_save = False
    ctx.agent.messages.append({"role": "user", "content": "unsaved"})

    result = _handle_new_session(NewSessionCommand(), ctx)

    assert result.session_id
    assert ctx.agent.messages == []
    assert SessionStore(tmp_path).list(limit=10, fingerprint=None) == []


def test_new_session_auto_saves_when_enabled(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    ctx.agent.messages.append({"role": "user", "content": "saved"})

    result = _handle_new_session(NewSessionCommand(), ctx)

    saved = SessionStore(tmp_path).list(limit=10, fingerprint="local")
    assert result.session_id
    assert len(saved) == 1
    assert "saved" in saved[0].preview


def test_exit_skips_auto_save_when_disabled(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    ctx.config.session_auto_save = False
    ctx.agent.messages.append({"role": "user", "content": "unsaved"})

    result = _handle_exit(ExitCommand(), ctx)

    assert result.action == "exit"
    assert SessionStore(tmp_path).list(limit=10, fingerprint=None) == []


def test_manual_save_ignores_auto_save_disabled(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    ctx.config.session_auto_save = False
    ctx.agent.messages.append({"role": "user", "content": "manual"})

    result = _handle_save_session(SaveSessionCommand(), ctx)

    assert result.session_id
    loaded = SessionStore(tmp_path).load(result.session_id)
    assert loaded is not None
    assert loaded.messages[0]["content"] == "manual"


def test_session_save_hooks_receive_full_session_data(tmp_path: Path) -> None:
    captured: list[dict] = []

    class CaptureSessionSave(ObserverHook[SessionSaveContext]):
        def run(self, context: SessionSaveContext) -> None:
            captured.append(dict(context.session_data))

    ctx = _build_ctx(tmp_path)
    ctx.agent.messages.append({"role": "user", "content": "capture me"})
    ctx.agent.state.total_prompt_tokens = 3
    ctx.agent.state.total_completion_tokens = 5
    ctx.agent.active_mode = "coder"
    ctx.agent.hook_registry.register(
        HookPoint.SESSION_SAVE, CaptureSessionSave(name="capture_session_save")
    )

    result = _handle_save_session(SaveSessionCommand(), ctx)

    assert result.session_id
    assert captured
    assert captured[0]["session_id"] == result.session_id
    assert captured[0]["messages"][0]["content"] == "capture me"
    assert captured[0]["model"] == "base-model"
    assert captured[0]["active_mode"] == "coder"
    assert captured[0]["total_prompt_tokens"] == 3
    assert captured[0]["total_completion_tokens"] == 5
    assert captured[0]["fingerprint"] == "local"


def test_session_save_lifecycle_enters_unified_ui_audit(tmp_path: Path) -> None:
    ctx = _build_ctx(tmp_path)
    ctx.agent.messages.append({"role": "user", "content": "capture me"})
    ctx.agent.lifecycle_dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry(
            [
                LifecycleHookDeclaration.from_dict(
                    "hook:system_builtin:session_save:observer",
                    {
                        "event": "SessionEnd",
                        "source": "system_builtin",
                        "placement": "server",
                        "handler_type": "internal",
                        "handler_ref": "session_save_observer",
                        "display_name": "Session save observer",
                        "summary": "Observes saved sessions.",
                        "permissions": [],
                        "trust": "trusted",
                    },
                )
            ]
        ),
        runtime_adapters=LifecycleHookRuntimeAdapterRegistry(
            [CaptureInternalLifecycleAdapter()]
        ),
    )

    result = _handle_save_session(SaveSessionCommand(), ctx)

    audit_events = [
        event
        for event in ctx.ui_bus._history
        if event.data.get("event_type") == "lifecycle_hook"
    ]
    assert result.session_id
    assert audit_events
    audit = audit_events[0].data
    assert audit["event_name"] == "SessionEnd"
    assert audit["session_run_id"] == result.session_id
    assert audit["trigger_source"] == "session"
    assert audit["source"] == "system_builtin"
    assert audit["hook_id"] == "hook:system_builtin:session_save:observer"
    assert audit["payload"]["technical"]["old_hook_point"] == "session_save"
    assert audit["diagnostics"] == [{"code": "session_end_observed"}]

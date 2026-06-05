from pathlib import Path
from types import SimpleNamespace

from reuleauxcoder.domain.config.models import ApprovalConfig, Config, ModeConfig
from reuleauxcoder.domain.hooks.lifecycle import (
    LifecycleHookDeclaration,
    LifecycleHookDispatcher,
    LifecycleHookOutput,
    LifecycleHookRuntimeAdapter,
    LifecycleHookRuntimeAdapterRegistry,
    LifecycleHookRegistry,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import HookPoint, SessionStartContext
from reuleauxcoder.domain.session.models import SessionRuntimeState
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.entrypoint.runner import (
    AppDependencies,
    AppOptions,
    AppRunner,
)
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind, UIEventLevel


class FakeLLM:
    def __init__(self) -> None:
        self.model = "base-model"
        self.debug_trace = False
        self.api_key = "key"
        self.base_url = None
        self.temperature = 0.0
        self.max_tokens = 2048

    def reconfigure(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeContext:
    def __init__(self) -> None:
        self.max_tokens = 64000

    def reconfigure(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens


class FakeAgent:
    def __init__(self, fingerprint: str = "local") -> None:
        self.llm = FakeLLM()
        self.context = FakeContext()
        self.state = SimpleNamespace(
            messages=[],
            total_prompt_tokens=0,
            total_completion_tokens=0,
            current_round=0,
        )
        self.messages = self.state.messages
        self.available_modes = {
            "coder": ModeConfig(name="coder", description="Default coding mode"),
            "debugger": ModeConfig(name="debugger", description="Debug mode"),
        }
        self.active_mode = None
        self.session_fingerprint = fingerprint
        self.active_main_model_profile = None
        self.active_sub_model_profile = None
        self.hook_registry = HookRegistry()

    def set_mode(self, mode_name: str) -> None:
        self.active_mode = mode_name


class CaptureSessionStartLifecycleAdapter(LifecycleHookRuntimeAdapter):
    handler_type = "internal"
    supported_events = {"SessionStart"}
    supported_placements = {"server"}

    def unavailable_reason(self, declaration, *, placement=None):
        del declaration, placement
        return ""

    def dispatch(self, declaration, context):
        del declaration, context
        return LifecycleHookOutput.from_dict(
            {"diagnostics": [{"code": "session_start_observed"}]}
        )


def _build_config(tmp_path: Path) -> Config:
    return Config(
        approval=ApprovalConfig(default_mode="require_approval"),
        session_dir=str(tmp_path),
        modes={
            "coder": ModeConfig(name="coder", description="Default coding mode"),
            "debugger": ModeConfig(name="debugger", description="Debug mode"),
        },
        active_mode="coder",
    )


def _build_runner(**options) -> AppRunner:
    return AppRunner(
        options=AppOptions(**options),
        dependencies=AppDependencies(
            create_session_store=lambda sessions_dir: SessionStore(sessions_dir)
        ),
    )


def test_restore_session_auto_resume_latest_is_fingerprint_scoped(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    local_id = store.save(
        messages=[{"role": "user", "content": "local msg"}],
        model="local-model",
        fingerprint="local",
        runtime_state=SessionRuntimeState(
            model="local-model", active_mode="debugger", llm_debug_trace=True
        ),
    )
    store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="remote-model",
        fingerprint="remote:abc",
        runtime_state=SessionRuntimeState(
            model="remote-model", active_mode="coder", llm_debug_trace=False
        ),
    )
    runner = _build_runner(auto_resume_latest=True)
    config = _build_config(tmp_path)
    agent = FakeAgent(fingerprint="local")
    ui_bus = UIEventBus()

    current_session_id, session_exit_time, sessions_dir = runner._restore_session(
        config, agent, ui_bus
    )

    assert current_session_id == local_id
    assert session_exit_time is None
    assert sessions_dir == tmp_path
    assert agent.session_fingerprint == "local"
    assert agent.active_mode == "debugger"
    assert agent.llm.model == "local-model"
    assert agent.llm.debug_trace is True
    assert any(
        event.level == UIEventLevel.INFO
        and event.kind == UIEventKind.SESSION
        and f"Auto-resumed latest session: {local_id}" in event.message
        for event in ui_bus._history
    )


def test_runner_session_start_lifecycle_enters_unified_ui_audit() -> None:
    agent = FakeAgent()
    ui_bus = UIEventBus()
    agent.lifecycle_dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry(
            [
                LifecycleHookDeclaration.from_dict(
                    "hook:system_builtin:session_start:observer",
                    {
                        "event": "SessionStart",
                        "source": "system_builtin",
                        "placement": "server",
                        "handler_type": "internal",
                        "handler_ref": "session_start_observer",
                        "display_name": "Session start observer",
                        "summary": "Observes restored sessions.",
                        "permissions": [],
                        "trust": "trusted",
                    },
                )
            ]
        ),
        runtime_adapters=LifecycleHookRuntimeAdapterRegistry(
            [CaptureSessionStartLifecycleAdapter()]
        ),
    )

    AppRunner._run_lifecycle_hooks(
        agent,
        HookPoint.SESSION_START,
        SessionStartContext(
            hook_point=HookPoint.SESSION_START,
            session_id="session-1",
            metadata={"ui_bus": ui_bus, "source": "restore"},
        ),
    )

    audit_events = [
        event
        for event in ui_bus._history
        if event.data.get("event_type") == "lifecycle_hook"
    ]
    assert audit_events
    audit = audit_events[0].data
    assert audit["event_name"] == "SessionStart"
    assert audit["session_run_id"] == "session-1"
    assert audit["trigger_source"] == "runner"
    assert audit["source"] == "system_builtin"
    assert audit["hook_id"] == "hook:system_builtin:session_start:observer"
    assert audit["payload"]["technical"]["old_hook_point"] == "session_start"
    assert audit["diagnostics"] == [{"code": "session_start_observed"}]


def test_restore_session_manual_resume_warns_on_cross_fingerprint_and_restores_runtime(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    remote_id = store.save(
        messages=[{"role": "user", "content": "remote msg"}],
        model="remote-model",
        fingerprint="remote:abc",
        runtime_state=SessionRuntimeState(
            model="remote-model",
            active_mode="debugger",
            llm_debug_trace=True,
            approval_rules=[{"tool_name": "shell", "action": "deny"}],
        ),
    )
    runner = _build_runner(resume_session_id=remote_id, auto_resume_latest=False)
    config = _build_config(tmp_path)
    agent = FakeAgent(fingerprint="local")
    ui_bus = UIEventBus()

    current_session_id, _, _ = runner._restore_session(config, agent, ui_bus)

    assert current_session_id == remote_id
    assert agent.session_fingerprint == "remote:abc"
    assert agent.active_mode == "debugger"
    assert agent.llm.model == "remote-model"
    assert agent.llm.debug_trace is True
    assert [
        (rule.tool_name, rule.action)
        for rule in getattr(agent, "session_approval_rules")
    ] == [("shell", "deny")]
    assert any(
        event.level == UIEventLevel.WARNING
        and event.kind == UIEventKind.SESSION
        and "belongs to fingerprint 'remote:abc'" in event.message
        for event in ui_bus._history
    )
    assert any(
        event.level == UIEventLevel.SUCCESS
        and event.kind == UIEventKind.SESSION
        and f"Resumed session: {remote_id}" in event.message
        for event in ui_bus._history
    )

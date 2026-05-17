from types import SimpleNamespace

from reuleauxcoder.domain.context.manager import ContextManager
from reuleauxcoder.extensions.command.builtin.system import (
    _handle_compact,
    _handle_debug,
    _parse_debug,
)
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


def test_parse_debug_on_off() -> None:
    assert _parse_debug("/debug on", None).enabled is True
    assert _parse_debug("/debug off", None).enabled is False
    assert _parse_debug("/debug maybe", None) is None


def test_handle_debug_toggles_runtime_flag() -> None:
    ui_bus = UIEventBus()
    llm = SimpleNamespace(debug_trace=False)
    config = SimpleNamespace()
    ctx = SimpleNamespace(
        config=config,
        agent=SimpleNamespace(llm=llm),
        ui_bus=ui_bus,
    )

    result = _handle_debug(SimpleNamespace(enabled=True), ctx)
    assert ctx.agent.llm.debug_trace is True
    assert result.payload == {"llm_debug_trace": True}

    result = _handle_debug(SimpleNamespace(enabled=False), ctx)
    assert ctx.agent.llm.debug_trace is False
    assert result.payload == {"llm_debug_trace": False}


def test_handle_compact_force_emits_detailed_context_events() -> None:
    ui_bus = UIEventBus()
    agent = SimpleNamespace(
        messages=[
            {"role": "user", "content": f"message {index}"} for index in range(8)
        ],
        context=ContextManager(ui_bus=ui_bus),
        llm=None,
    )
    ctx = SimpleNamespace(agent=agent, ui_bus=ui_bus)

    result = _handle_compact(SimpleNamespace(force_strategy="collapse"), ctx)

    assert result.action == "continue"
    context_events = [
        event for event in ui_bus._history if event.kind == UIEventKind.CONTEXT
    ]
    assert [event.data["phase"] for event in context_events] == ["before", "after"]
    assert context_events[0].data["applied_layers"] == ["hard_collapse"]
    assert any("Forced collapse" in event.message for event in ui_bus._history)

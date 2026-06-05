from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.hooks.lifecycle import (
    FunctionLifecycleHookRuntimeAdapter,
    LifecycleHookDeclaration,
    LifecycleHookDispatcher,
    LifecycleHookOutput,
    LifecycleHookRegistry,
    LifecycleHookRuntimeAdapterRegistry,
)
from reuleauxcoder.interfaces.events import (
    AgentEventBridge,
    UIEvent,
    UIEventBus,
    UIEventKind,
    UIEventLevel,
)


def _notification_dispatcher(handler):
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:admin:notification",
        {
            "event": "Notification",
            "source": "admin_managed",
            "placement": "server",
            "handler_type": "internal",
            "handler_ref": "notification-audit",
            "display_name": "Notification audit",
            "summary": "Audits UI notifications.",
            "permissions": [],
            "trust": "trusted",
        },
    )
    return LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=LifecycleHookRuntimeAdapterRegistry([
            FunctionLifecycleHookRuntimeAdapter("internal", handler),
        ]),
    )


def test_ui_event_factory_methods_set_level_and_kind() -> None:
    assert UIEvent.info("x", kind=UIEventKind.COMMAND).level is UIEventLevel.INFO
    assert UIEvent.success("x").level is UIEventLevel.SUCCESS
    assert UIEvent.warning("x").level is UIEventLevel.WARNING
    assert UIEvent.error("x").level is UIEventLevel.ERROR
    assert UIEvent.debug("x").level is UIEventLevel.DEBUG


def test_ui_event_bus_replays_history_to_new_subscriber() -> None:
    bus = UIEventBus()
    seen = []

    bus.info("first")
    bus.subscribe(lambda event: seen.append(event.message), replay_history=True)

    assert seen == ["first"]


def test_ui_event_bus_emit_ignores_handler_exceptions() -> None:
    bus = UIEventBus()
    seen = []

    def broken_handler(event):
        raise RuntimeError("boom")

    def good_handler(event):
        seen.append(event.message)

    bus.subscribe(broken_handler, replay_history=False)
    bus.subscribe(good_handler, replay_history=False)
    bus.info("hello")

    assert seen == ["hello"]


def test_ui_event_bus_dispatches_notification_lifecycle_without_mutating_event() -> None:
    contexts = []

    def handler(_declaration, context):
        contexts.append(context)
        return LifecycleHookOutput.from_dict({
            "decision": "deny",
            "continue_flow": False,
            "user_message": "Notification audited.",
            "updated_input": {"message": "rewritten"},
            "additional_context": [{"role": "system", "content": "ignored"}],
            "diagnostics": [{"code": "notification_seen"}],
        })

    bus = UIEventBus(lifecycle_dispatcher=_notification_dispatcher(handler))
    seen = []
    bus.subscribe(seen.append, replay_history=False)

    bus.warning(
        "MCP server needs attention.",
        kind=UIEventKind.MCP,
        session_run_id="session-1",
        agent_run_id="run-1",
        turn_id="turn-1",
        trigger_source="mcp",
        notification_code="mcp_attention",
    )

    assert len(contexts) == 1
    lifecycle_context = contexts[0]
    assert lifecycle_context.event_name == "Notification"
    assert lifecycle_context.session_run_id == "session-1"
    assert lifecycle_context.agent_run_id == "run-1"
    assert lifecycle_context.turn_id == "turn-1"
    assert lifecycle_context.source == "mcp"
    assert lifecycle_context.payload["message"] == "MCP server needs attention."
    assert lifecycle_context.payload["level"] == "warning"
    assert lifecycle_context.payload["kind"] == "mcp"
    assert lifecycle_context.payload["notification_code"] == "mcp_attention"

    assert len(seen) == 2
    assert seen[0].message == "MCP server needs attention."
    assert seen[0].level is UIEventLevel.WARNING
    assert seen[0].kind is UIEventKind.MCP
    assert seen[0].data["notification_code"] == "mcp_attention"

    audit = seen[1]
    assert audit.kind is UIEventKind.AGENT
    assert audit.data["event_type"] == "lifecycle_hook"
    assert audit.data["event_name"] == "Notification"
    assert audit.data["decision"] == "deny"
    assert audit.data["continue_flow"] is False
    assert audit.data["message"] == "Notification audited."
    assert audit.data["payload"]["message"] == "MCP server needs attention."
    diagnostic_codes = {item["code"] for item in audit.data["diagnostics"]}
    assert diagnostic_codes >= {
        "notification_seen",
        "lifecycle_output_field_ignored",
    }


def test_ui_event_bus_open_view_emits_structured_view_event() -> None:
    bus = UIEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event), replay_history=False)

    bus.open_view(
        "skills",
        title="Skills",
        payload={"markdown": "# Skills"},
        focus=False,
        reuse_key="skills",
    )

    event = seen[0]
    assert event.kind is UIEventKind.VIEW
    assert event.data["action"] == "open"
    assert event.data["view_type"] == "skills"
    assert event.data["title"] == "Skills"
    assert event.data["payload"] == {"markdown": "# Skills"}
    assert event.data["focus"] is False
    assert event.data["reuse_key"] == "skills"


def test_agent_event_bridge_maps_error_to_error_level() -> None:
    bus = UIEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event), replay_history=False)

    AgentEventBridge(bus).on_agent_event(AgentEvent.error("boom"))

    event = seen[0]
    assert event.kind is UIEventKind.AGENT
    assert event.level is UIEventLevel.ERROR
    assert event.data["error_message"] == "boom"


def test_agent_event_bridge_maps_tool_events_to_debug_level() -> None:
    bus = UIEventBus()
    seen = []
    bus.subscribe(lambda event: seen.append(event), replay_history=False)

    AgentEventBridge(bus).on_agent_event(
        AgentEvent.tool_call_start("shell", {"command": "ls"})
    )

    event = seen[0]
    assert event.kind is UIEventKind.AGENT
    assert event.level is UIEventLevel.DEBUG
    assert event.data["tool_name"] == "shell"

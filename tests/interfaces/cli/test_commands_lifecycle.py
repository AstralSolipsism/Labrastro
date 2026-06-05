from types import SimpleNamespace

from reuleauxcoder.app.commands.models import CommandResult
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import slash_trigger
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.domain.hooks.lifecycle import (
    FunctionLifecycleHookRuntimeAdapter,
    LifecycleHookDeclaration,
    LifecycleHookDispatcher,
    LifecycleHookOutput,
    LifecycleHookRegistry,
    LifecycleHookRuntimeAdapterRegistry,
)
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.events import UIEventBus
from reuleauxcoder.interfaces.ui_registry import UICapability, UIProfile


CLI_PROFILE = UIProfile(
    ui_id="cli",
    display_name="CLI",
    capabilities=frozenset({UICapability.TEXT_INPUT}),
)


def _slash_action(command: str, *, handler) -> ActionSpec:
    def parser(user_input, _parse_ctx):
        return {"command": user_input} if user_input == command else None

    return ActionSpec(
        action_id=f"test.{command}",
        feature_id="feature.test",
        description="test command",
        ui_targets=frozenset({"cli"}),
        required_capabilities=frozenset({UICapability.TEXT_INPUT}),
        triggers=(slash_trigger(command),),
        parser=parser,
        handler=handler,
    )


def _dispatcher(handler):
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:admin:command-expansion:UserPromptExpansion:0",
        {
            "event": "UserPromptExpansion",
            "source": "admin_managed",
            "placement": "server",
            "handler_type": "internal",
            "handler_ref": "command-expansion",
            "display_name": "Command expansion guard",
            "summary": "Validates slash commands before dispatch.",
            "permissions": [],
            "trust": "trusted",
        },
    )
    return LifecycleHookDispatcher(
        registry=LifecycleHookRegistry([declaration]),
        runtime_adapters=LifecycleHookRuntimeAdapterRegistry([
            FunctionLifecycleHookRuntimeAdapter("internal", handler),
        ]),
    )


def _agent(dispatcher):
    return SimpleNamespace(
        lifecycle_dispatcher=dispatcher,
        current_session_id="session-1",
        runtime_agent_run_id="run-1",
        runtime_turn_id="turn-1",
        locale="zh-CN",
        ui_interactor=None,
        approval_provider=None,
    )


def test_handle_command_blocks_user_prompt_expansion_before_dispatch() -> None:
    dispatched: list[dict] = []
    lifecycle_contexts = []

    def command_handler(command, _ctx):
        dispatched.append(command)
        return CommandResult(action="continue")

    def lifecycle_handler(_declaration, context):
        lifecycle_contexts.append(context)
        return LifecycleHookOutput(
            decision="deny",
            user_message="This command is blocked.",
        )

    ui_bus = UIEventBus()
    result = handle_command(
        "/danger",
        _agent(_dispatcher(lifecycle_handler)),
        SimpleNamespace(),
        "session-1",
        ui_bus,
        CLI_PROFILE,
        ActionRegistry([_slash_action("/danger", handler=command_handler)]),
    )

    assert result["action"] == "continue"
    assert dispatched == []
    assert lifecycle_contexts[0].event_name == "UserPromptExpansion"
    assert lifecycle_contexts[0].session_run_id == "session-1"
    assert lifecycle_contexts[0].agent_run_id == "run-1"
    assert lifecycle_contexts[0].turn_id == "turn-1"
    assert lifecycle_contexts[0].payload["command_text"] == "/danger"
    lifecycle_events = [
        event for event in ui_bus._history if event.data.get("event_type") == "lifecycle_hook"
    ]
    assert lifecycle_events
    assert lifecycle_events[-1].data["event_name"] == "UserPromptExpansion"
    assert lifecycle_events[-1].data["decision"] == "deny"
    assert lifecycle_events[-1].data["phase"] == "result"
    assert lifecycle_events[-1].data["message"] == "This command is blocked."


def test_handle_command_applies_user_prompt_expansion_updated_command_before_parse() -> None:
    dispatched: list[dict] = []

    def command_handler(command, _ctx):
        dispatched.append(command)
        return CommandResult(action="continue")

    def lifecycle_handler(_declaration, _context):
        return LifecycleHookOutput(updated_input={"command_text": "/safe"})

    result = handle_command(
        "/unsafe",
        _agent(_dispatcher(lifecycle_handler)),
        SimpleNamespace(),
        "session-1",
        UIEventBus(),
        CLI_PROFILE,
        ActionRegistry([_slash_action("/safe", handler=command_handler)]),
    )

    assert result["action"] == "continue"
    assert dispatched == [{"command": "/safe"}]


def test_handle_command_blocks_user_prompt_expansion_ask_without_promptless_dispatch() -> None:
    dispatched: list[dict] = []

    def command_handler(command, _ctx):
        dispatched.append(command)
        return CommandResult(action="continue")

    def lifecycle_handler(_declaration, _context):
        return LifecycleHookOutput(
            decision="ask",
            reason="Reviewer must approve this command.",
        )

    ui_bus = UIEventBus()
    result = handle_command(
        "/review",
        _agent(_dispatcher(lifecycle_handler)),
        SimpleNamespace(),
        "session-1",
        ui_bus,
        CLI_PROFILE,
        ActionRegistry([_slash_action("/review", handler=command_handler)]),
    )

    assert result["action"] == "continue"
    assert dispatched == []
    lifecycle_events = [
        event for event in ui_bus._history if event.data.get("event_type") == "lifecycle_hook"
    ]
    assert lifecycle_events[-1].data["decision"] == "ask"
    assert lifecycle_events[-1].data["level"] == "warning"
    assert lifecycle_events[-1].data["message"] == "Reviewer must approve this command."

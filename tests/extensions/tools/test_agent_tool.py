from types import SimpleNamespace

from labrastro_server.services.agent_runtime.control_plane import AgentRunRequest
from reuleauxcoder.domain.agent.events import AgentEventType
from reuleauxcoder.domain.agent_runtime.models import AgentConfig
from reuleauxcoder.domain.config.models import AgentRegistryConfig
from reuleauxcoder.domain.hooks.lifecycle import (
    FunctionLifecycleHookRuntimeAdapter,
    LifecycleHookDeclaration,
    LifecycleHookDispatcher,
    LifecycleHookOutput,
    LifecycleHookRegistry,
    LifecycleHookRuntimeAdapterRegistry,
)
from reuleauxcoder.extensions.tools.builtin.agent import DelegateAgentTool


class _ControlPlaneStub:
    def __init__(self) -> None:
        self.requests: list[AgentRunRequest] = []

    def submit_agent_run(self, request: AgentRunRequest):
        self.requests.append(request)
        return SimpleNamespace(
            id="run-1",
            agent_id=request.agent_id,
            source=request.source,
            status=SimpleNamespace(value="queued"),
        )


def _tool() -> tuple[DelegateAgentTool, _ControlPlaneStub]:
    control = _ControlPlaneStub()
    config = SimpleNamespace(
        agent_registry=AgentRegistryConfig(
            agents={
                "reviewer": AgentConfig(
                    id="reviewer",
                    name="Reviewer",
                    description="Review code changes.",
                ),
                "capability_packager": AgentConfig(
                    id="capability_packager",
                    name="Capability Packager",
                    visibility="internal",
                    delegable=False,
                    taskflow_eligible=False,
                    system_flow_only=["capability_ingest"],
                ),
            }
        )
    )
    parent = SimpleNamespace(
        runtime_config=config,
        agent_run_control_plane=control,
        runtime_agent_id="chat:parent",
        current_session_id="session-1",
        runtime_working_directory="/workspace",
        runtime_turn_id="turn-1",
        _events=[],
    )
    parent._emit_event = parent._events.append
    tool = DelegateAgentTool()
    tool._parent_agent = parent
    return tool, control


def _tool_with_executor_bound_parent() -> tuple[DelegateAgentTool, _ControlPlaneStub]:
    tool, control = _tool()
    tool._parent_agent = SimpleNamespace(
        runtime_config=tool._parent_agent.runtime_config,
        agent_run_control_plane=control,
        runtime_task_id="agent-run:parent",
        current_session_id="session-1",
        runtime_workspace_root="/workspace",
    )
    return tool, control


def test_delegate_agent_schema_requires_agent_and_task() -> None:
    assert DelegateAgentTool.name == "delegate_agent"
    assert DelegateAgentTool.parameters["required"] == ["agent_id", "task"]
    assert "tasks" not in DelegateAgentTool.parameters["properties"]


def test_delegate_agent_rejects_missing_agent_config() -> None:
    tool, _control = _tool()

    assert (
        tool.preflight_validate(agent_id="missing", task="review")
        == "Error: AgentConfig not found: missing"
    )


def test_delegate_agent_rejects_internal_agent() -> None:
    tool, _control = _tool()

    result = tool.preflight_validate(agent_id="capability_packager", task="review")

    assert result is not None
    assert "permission gateway" in result
    assert "system flow" in result


def test_delegate_agent_submits_agent_run() -> None:
    tool, control = _tool()

    result = tool.execute(agent_id="reviewer", task="review this diff")

    assert "Delegated AgentRun submitted" in result
    assert len(control.requests) == 1
    request = control.requests[0]
    assert request.agent_id == "reviewer"
    assert request.prompt == "review this diff"
    assert request.source.value == "delegation"
    assert request.delegated_by_run_id == "chat:parent"
    assert request.parent_run_id == "chat:parent"
    assert request.workdir == "/workspace"
    assert request.metadata["parent_session_id"] == "session-1"


def test_delegate_agent_submits_agent_run_from_executor_bound_parent() -> None:
    tool, control = _tool_with_executor_bound_parent()

    result = tool.execute(agent_id="reviewer", task="review this diff")

    assert "Delegated AgentRun submitted" in result
    request = control.requests[0]
    assert request.delegated_by_run_id == "agent-run:parent"
    assert request.parent_run_id == "agent-run:parent"
    assert request.workdir == "/workspace"
    assert request.metadata["workspace_root"] == "/workspace"


def _delegation_lifecycle_dispatcher(handler) -> LifecycleHookDispatcher:
    declarations = [
        LifecycleHookDeclaration.from_dict(
            f"hook:test:{event_name}",
            {
                "event": event_name,
                "source": "admin_managed",
                "placement": "server",
                "handler_type": "internal",
                "handler_ref": event_name,
                "display_name": event_name,
                "summary": f"Test {event_name} lifecycle.",
                "permissions": [],
                "trust": "trusted",
            },
        )
        for event_name in ("TaskCreated", "SubagentStart")
    ]
    return LifecycleHookDispatcher(
        LifecycleHookRegistry(declarations),
        runtime_adapters=LifecycleHookRuntimeAdapterRegistry([
            FunctionLifecycleHookRuntimeAdapter("internal", handler),
        ]),
    )


def test_delegate_agent_task_created_lifecycle_denial_blocks_submit() -> None:
    tool, control = _tool()
    contexts = []

    def handler(_declaration, context):
        contexts.append(context)
        return LifecycleHookOutput(
            decision="deny",
            user_message="Delegation is blocked by policy.",
        )

    tool._parent_agent.lifecycle_dispatcher = _delegation_lifecycle_dispatcher(handler)

    result = tool.execute(agent_id="reviewer", task="review this diff")

    assert result == "Error: Delegation is blocked by policy."
    assert control.requests == []
    assert [context.event_name for context in contexts] == ["TaskCreated"]
    assert contexts[0].agent_run_id == "chat:parent"
    assert contexts[0].turn_id == "turn-1"
    assert contexts[0].payload["agent_id"] == "reviewer"
    assert contexts[0].payload["task"] == "review this diff"
    lifecycle_events = [
        event
        for event in tool._parent_agent._events
        if event.event_type == AgentEventType.LIFECYCLE_HOOK
    ]
    assert lifecycle_events[-1].data["event_name"] == "TaskCreated"
    assert lifecycle_events[-1].data["decision"] == "deny"


def test_delegate_agent_emits_task_created_and_subagent_start_lifecycle_audit() -> None:
    tool, control = _tool()
    contexts = []

    def handler(_declaration, context):
        contexts.append(context)
        return LifecycleHookOutput(diagnostics=[{"code": f"seen:{context.event_name}"}])

    tool._parent_agent.lifecycle_dispatcher = _delegation_lifecycle_dispatcher(handler)

    result = tool.execute(agent_id="reviewer", task="review this diff")

    assert "Delegated AgentRun submitted" in result
    assert len(control.requests) == 1
    assert [context.event_name for context in contexts] == [
        "TaskCreated",
        "SubagentStart",
    ]
    assert contexts[1].payload["child_agent_run_id"] == "run-1"
    assert contexts[1].payload["agent_id"] == "reviewer"
    lifecycle_events = [
        event.data
        for event in tool._parent_agent._events
        if event.event_type == AgentEventType.LIFECYCLE_HOOK
    ]
    assert [event["event_name"] for event in lifecycle_events] == [
        "TaskCreated",
        "SubagentStart",
    ]
    assert lifecycle_events[0]["payload"]["task"] == "review this diff"
    assert lifecycle_events[1]["payload"]["child_agent_run_id"] == "run-1"

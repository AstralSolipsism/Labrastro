from types import SimpleNamespace

from labrastro_server.services.agent_runtime.control_plane import AgentRunRequest
from reuleauxcoder.domain.agent_runtime.models import AgentConfig
from reuleauxcoder.domain.config.models import AgentRegistryConfig, Config
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
    config = Config(
        api_key="test",
        agent_registry=AgentRegistryConfig(
            agents={
                "reviewer": AgentConfig(
                    id="reviewer",
                    name="Reviewer",
                    description="Review code changes.",
                )
            }
        ),
    )
    parent = SimpleNamespace(
        runtime_config=config,
        agent_run_control_plane=control,
        runtime_agent_id="chat:parent",
        current_session_id="session-1",
        runtime_working_directory="/workspace",
    )
    tool = DelegateAgentTool()
    tool._parent_agent = parent
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

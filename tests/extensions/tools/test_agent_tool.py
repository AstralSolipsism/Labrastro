from __future__ import annotations

from types import SimpleNamespace
import json

from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunControlPlane,
    AgentRunRequest,
)
from labrastro_server.services.agent_runtime.executor_backend import ExecutorRunResult
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.agent_runtime.models import (
    AgentConfig,
    AgentRunFeedbackKind,
    AgentRunStatus,
    AgentRunWaitingReason,
    AgentRunRelation,
    AgentRunRelationType,
)
from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.config.models import (
    AgentRegistryConfig,
    ApprovalConfig,
    Config,
)
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.tools.builtin.agent import (
    AgentSearchTool,
    CallAgentTool,
)


class _ControlPlaneStub:
    def __init__(self) -> None:
        self.requests: list[AgentRunRequest] = []
        self.persistent_calls: list[dict] = []
        self.agent_call_waiting: list[dict] = []
        self.feedback: list[dict] = []

    def submit_agent_run(self, request: AgentRunRequest):
        self.requests.append(request)
        return SimpleNamespace(
            id="run-1",
            agent_id=request.agent_id,
            source=request.source,
            status=SimpleNamespace(value="queued"),
        )

    def mark_agent_call_waiting(
        self,
        owner_agent_run_id: str,
        *,
        target_agent_run_id: str,
        conversation_scope: str,
        thread_key: str = "",
        wait: bool = True,
    ) -> None:
        self.agent_call_waiting.append(
            {
                "owner_agent_run_id": owner_agent_run_id,
                "target_agent_run_id": target_agent_run_id,
                "conversation_scope": conversation_scope,
                "thread_key": thread_key,
                "wait": wait,
            }
        )

    def append_agent_run_feedback(self, task_id: str, **kwargs):
        payload = {"task_id": task_id, **kwargs}
        self.feedback.append(payload)
        return SimpleNamespace(
            id="feedback-1",
            agent_run_id=task_id,
            kind=kwargs.get("kind"),
        )

    def call_persistent_agent(
        self,
        *,
        owner_agent_run_id: str,
        owner_session_run_id: str,
        agent_id: str,
        prompt: str,
        thread_key: str = "",
        thread_summary: str = "",
        wait: bool = True,
        workdir: str | None = None,
        metadata: dict | None = None,
    ):
        self.persistent_calls.append(
            {
                "owner_agent_run_id": owner_agent_run_id,
                "owner_session_run_id": owner_session_run_id,
                "agent_id": agent_id,
                "prompt": prompt,
                "thread_key": thread_key,
                "thread_summary": thread_summary,
                "wait": wait,
                "workdir": workdir,
                "metadata": dict(metadata or {}),
            }
        )
        request = AgentRunRequest(
            agent_id=agent_id,
            prompt=prompt,
            owner_session_run_id=owner_session_run_id,
            source="delegation",
            workdir=workdir,
            metadata=dict(metadata or {}),
            relation=AgentRunRelation(
                id="",
                owner_agent_run_id=owner_agent_run_id,
                related_agent_run_id="",
                relation_type=AgentRunRelationType.AGENT_CALL_PERSISTENT,
                payload={
                    "conversation_scope": "persistent",
                    "wait": wait,
                    "thread_key": thread_key,
                    "thread_summary": thread_summary,
                },
            ),
        )
        self.requests.append(request)
        return SimpleNamespace(
            id="run-1",
            agent_id=request.agent_id,
            source=request.source,
            status=SimpleNamespace(value="queued"),
        )


def _parent_agent() -> tuple[SimpleNamespace, _ControlPlaneStub]:
    control = _ControlPlaneStub()
    config = SimpleNamespace(
        agent_registry=AgentRegistryConfig(
            agents={
                "reviewer": AgentConfig(
                    id="reviewer",
                    name="Reviewer",
                    description="Review code changes.",
                    callable_scopes=["ephemeral"],
                ),
                "researcher": AgentConfig(
                    id="researcher",
                    name="Researcher",
                    description="Research project context.",
                    callable_scopes=["ephemeral", "persistent"],
                ),
                "capability_packager": AgentConfig(
                    id="capability_packager",
                    name="Capability Packager",
                    visibility="internal",
                    callable_scopes=[],
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
    return parent, control


def _agent_call_tools() -> tuple[AgentSearchTool, CallAgentTool, _ControlPlaneStub]:
    parent, control = _parent_agent()
    search = AgentSearchTool()
    call = CallAgentTool()
    search._parent_agent = parent
    call._parent_agent = parent
    return search, call, control


def _current_activation_id(control: AgentRunControlPlane, task_id: str) -> str:
    return str(control.get_agent_run(task_id).current_activation_id or "")


def test_agent_search_returns_agent_ids_not_tool_ids() -> None:
    search, _call, _control = _agent_call_tools()

    payload = json.loads(
        search.execute(query="research", conversation_scope="persistent")
    )

    assert payload["conversation_scope"] == "persistent"
    result = payload["results"][0]
    assert result["agent_id"] == "researcher"
    assert result["callable_scopes"] == ["ephemeral", "persistent"]
    assert result["authorization_status"] == "requires_approval"
    assert result["existing_threads"] == []
    assert result["source"] == "agent_registry"
    assert result["summary"] == "Research project context."
    assert isinstance(result["capability_scope"], dict)
    assert "tool_id" not in result


def test_call_agent_requires_exact_agent_id_and_conversation_scope() -> None:
    _search, call, _control = _agent_call_tools()

    assert (
        call.preflight_validate(
            agent_id="Reviewer",
            conversation_scope="ephemeral",
            request="review",
        )
        == "Error: AgentConfig not found: Reviewer"
    )
    assert (
        call.preflight_validate(
            agent_id="reviewer",
            conversation_scope="persistent",
            request="review",
        )
        == "Error: AgentConfig does not expose conversation_scope persistent: reviewer"
    )


def test_call_agent_records_failed_invocation_as_internal_feedback() -> None:
    _search, call, control = _agent_call_tools()

    result = call.execute(
        agent_id="Reviewer",
        conversation_scope="ephemeral",
        request="review",
    )

    assert result == "Error: AgentConfig not found: Reviewer"
    assert control.requests == []
    assert len(control.feedback) == 1
    feedback = control.feedback[0]
    assert feedback["task_id"] == "chat:parent"
    assert feedback["kind"].value == "agent_call_failed"
    assert feedback["visibility"].value == "internal"
    assert feedback["requires_activation"] is False
    assert feedback["payload"]["agent_id"] == "Reviewer"
    assert feedback["payload"]["conversation_scope"] == "ephemeral"
    assert feedback["payload"]["status"] == "failed"


def test_call_agent_executor_preflight_failure_records_feedback() -> None:
    parent, control = _parent_agent()
    call = CallAgentTool()
    call._parent_agent = parent
    parent.state = SimpleNamespace(current_round=0)
    parent.get_tool = lambda name: call if name == "call_agent" else None

    result = ToolExecutor(parent).execute(
        ToolCall(
            id="call-agent",
            name="call_agent",
            arguments={
                "agent_id": "Reviewer",
                "conversation_scope": "ephemeral",
                "request": "review",
            },
        )
    )

    assert result == "Error: AgentConfig not found: Reviewer"
    assert control.requests == []
    assert len(control.feedback) == 1
    assert control.feedback[0]["task_id"] == "chat:parent"
    assert control.feedback[0]["kind"].value == "agent_call_failed"


def test_call_agent_submits_ephemeral_run_without_parent_field_relation() -> None:
    _search, call, control = _agent_call_tools()

    result = call.execute(
        agent_id="reviewer",
        conversation_scope="ephemeral",
        request="review this",
    )

    assert "Agent call submitted" in result
    request = control.requests[0]
    assert request.agent_id == "reviewer"
    assert request.prompt == "review this"
    assert request.owner_session_run_id == "session-1"
    assert request.source.value == "delegation"
    assert not hasattr(request, "parent_run_id")
    assert not hasattr(request, "delegated_by_run_id")
    assert request.relation is not None
    assert request.relation.owner_agent_run_id == "chat:parent"
    assert request.relation.relation_type == AgentRunRelationType.AGENT_CALL_EPHEMERAL
    assert request.relation.payload["conversation_scope"] == "ephemeral"
    assert "relation_type" not in request.metadata
    assert "call_agent_mode" not in request.metadata
    assert "called_by_agent_run_id" not in request.metadata


def test_call_agent_persistent_uses_thread_binding_inputs() -> None:
    _search, call, control = _agent_call_tools()

    result = call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="collect project context",
        thread_key="project-context",
        thread_summary="Project context research",
    )

    assert "Agent call submitted" in result
    request = control.requests[0]
    assert request.agent_id == "researcher"
    assert request.owner_session_run_id == "session-1"
    assert request.source.value == "delegation"
    call_args = control.persistent_calls[0]
    assert call_args["owner_agent_run_id"] == "chat:parent"
    assert call_args["agent_id"] == "researcher"
    assert call_args["prompt"] == "collect project context"
    assert call_args["thread_key"] == "project-context"
    assert call_args["thread_summary"] == "Project context research"
    assert call_args["wait"] is True
    assert call_args["metadata"] == {}
    assert control.agent_call_waiting == [
        {
            "owner_agent_run_id": "chat:parent",
            "target_agent_run_id": "run-1",
            "conversation_scope": "persistent",
            "thread_key": "project-context",
            "wait": True,
        }
    ]


def test_call_agent_persistent_empty_thread_key_uses_default_thread() -> None:
    control = AgentRunControlPlane()
    parent_run = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="plan", owner_session_run_id="session-1"),
        task_id="parent-run",
    )
    parent, _stub = _parent_agent()
    parent.agent_run_control_plane = control
    parent.runtime_agent_run_id = parent_run.id
    parent.current_session_id = "session-1"
    call = CallAgentTool()
    call._parent_agent = parent

    result = call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="collect default context",
        thread_summary="Default research thread",
        wait=False,
    )

    assert "Agent call submitted" in result
    payload = json.loads(result.removeprefix("Agent call submitted: "))
    assert payload["thread_key"] == ""
    detail = control.load_agent_run_detail(parent_run.id)
    binding = detail["agent_thread_bindings"][0]
    assert binding["thread_key"] == ""
    assert binding["thread_summary"] == "Default research thread"


def test_call_agent_rejects_thread_arguments_for_ephemeral_scope() -> None:
    _search, call, control = _agent_call_tools()

    result = call.execute(
        agent_id="reviewer",
        conversation_scope="ephemeral",
        request="review",
        thread_key="review-thread",
        thread_summary="Review thread",
    )

    assert "invalid_agent_call_arguments" in result
    assert control.requests == []
    feedback = control.feedback[0]
    assert feedback["payload"]["error_code"] == "invalid_agent_call_arguments"
    assert feedback["requires_activation"] is False


def test_call_agent_stores_relation_outside_agent_run_metadata() -> None:
    control = AgentRunControlPlane()
    parent_run = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="plan", owner_session_run_id="session-1"),
        task_id="parent-run",
    )
    parent, _stub = _parent_agent()
    parent.agent_run_control_plane = control
    parent.runtime_agent_run_id = parent_run.id
    call = CallAgentTool()
    call._parent_agent = parent

    result = call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="collect project context",
        thread_key="project-context",
        thread_summary="Project context research",
    )

    assert "Agent call submitted" in result
    child = control.list_agent_runs(agent_id="researcher")[0]
    assert child["owner_session_run_id"] == "session-1"
    assert "called_by_agent_run_id" not in child["metadata"]
    assert "relation_type" not in child["metadata"]
    parent_detail = control.load_agent_run_detail(parent_run.id)
    child_detail = control.load_agent_run_detail(child["id"])
    assert parent_detail["relations"] == child_detail["relations"]
    relation = parent_detail["relations"][0]
    assert relation["owner_agent_run_id"] == parent_run.id
    assert relation["related_agent_run_id"] == child["id"]
    assert relation["relation_type"] == "agent_call_persistent"
    assert relation["payload"]["thread_key"] == "project-context"
    assert "call_agent_mode" not in child["metadata"]
    assert "purpose_key" not in child["metadata"]


def test_call_agent_persistent_reuses_binding_and_activation() -> None:
    control = AgentRunControlPlane()
    parent_run = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="plan", owner_session_run_id="session-1"),
        task_id="parent-run",
    )
    parent, _stub = _parent_agent()
    parent.agent_run_control_plane = control
    parent.runtime_agent_run_id = parent_run.id
    parent.current_session_id = "session-1"
    call = CallAgentTool()
    call._parent_agent = parent

    first = call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="collect project context",
        thread_key="project-context",
        thread_summary="Project context research",
    )
    child_id = control.list_agent_runs(agent_id="researcher")[0]["id"]
    control.complete_agent_run_activation(
        child_id,
        ExecutorRunResult(task_id=child_id, status="completed", output="context ready"),
        activation_id=_current_activation_id(control, child_id),
    )
    second = call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="refresh project context",
        thread_key="project-context",
        thread_summary="Project context research",
    )

    assert "Agent call submitted" in first
    assert "Agent call submitted" in second
    children = control.list_agent_runs(agent_id="researcher")
    assert len(children) == 1
    assert children[0]["id"] == child_id
    parent_detail = control.load_agent_run_detail(parent_run.id)
    child_detail = control.load_agent_run_detail(child_id)
    assert len(parent_detail["agent_thread_bindings"]) == 1
    binding = parent_detail["agent_thread_bindings"][0]
    assert binding["main_agent_run_id"] == parent_run.id
    assert binding["target_agent_run_id"] == child_id
    assert binding["thread_key"] == "project-context"
    assert binding["thread_summary"] == "Project context research"
    assert child_detail["agent_thread_bindings"] == parent_detail["agent_thread_bindings"]
    assert [activation["prompt"] for activation in child_detail["activations"]] == [
        "collect project context",
        "refresh project context",
    ]


def test_call_agent_persistent_summary_mismatch_returns_structured_failure() -> None:
    control = AgentRunControlPlane()
    parent_run = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="plan", owner_session_run_id="session-1"),
        task_id="parent-run",
    )
    parent, _stub = _parent_agent()
    parent.agent_run_control_plane = control
    parent.runtime_agent_run_id = parent_run.id
    parent.current_session_id = "session-1"
    call = CallAgentTool()
    call._parent_agent = parent

    call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="collect project context",
        thread_key="project-context",
        thread_summary="Project context research",
        wait=False,
    )
    result = call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="reuse project context",
        thread_key="project-context",
        thread_summary="Different summary",
        wait=False,
    )

    assert result.startswith("Error: agent_thread_summary_mismatch")
    assert len(control.list_agent_runs(agent_id="researcher")) == 1
    feedback = control.load_agent_run_detail(parent_run.id)["feedback"][-1]
    assert feedback["kind"] == AgentRunFeedbackKind.AGENT_CALL_FAILED.value
    assert feedback["requires_activation"] is False
    assert feedback["payload"]["error_code"] == "agent_thread_summary_mismatch"


def test_call_agent_persistent_busy_returns_structured_failure() -> None:
    control = AgentRunControlPlane()
    parent_run = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="plan", owner_session_run_id="session-1"),
        task_id="parent-run",
    )
    parent, _stub = _parent_agent()
    parent.agent_run_control_plane = control
    parent.runtime_agent_run_id = parent_run.id
    parent.current_session_id = "session-1"
    call = CallAgentTool()
    call._parent_agent = parent

    call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="collect project context",
        thread_key="project-context",
        thread_summary="Project context research",
        wait=False,
    )
    child_id = control.list_agent_runs(agent_id="researcher")[0]["id"]
    result = call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="reuse project context",
        thread_key="project-context",
        thread_summary="Project context research",
        wait=True,
    )

    assert result.startswith("Error: agent_thread_busy")
    assert len(control.list_agent_runs(agent_id="researcher")) == 1
    assert len(control.load_agent_run_detail(child_id)["activations"]) == 1
    assert control.get_agent_run(parent_run.id).status == AgentRunStatus.QUEUED
    feedback = control.load_agent_run_detail(parent_run.id)["feedback"][-1]
    assert feedback["payload"]["error_code"] == "agent_thread_busy"
    assert feedback["requires_activation"] is False


def test_call_agent_wait_true_resumes_parent_from_agent_feedback() -> None:
    control = AgentRunControlPlane()
    parent_run = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="plan", owner_session_run_id="session-1"),
        task_id="parent-run",
    )
    parent, _stub = _parent_agent()
    parent.agent_run_control_plane = control
    parent.runtime_agent_run_id = parent_run.id
    parent.current_session_id = "session-1"
    call = CallAgentTool()
    call._parent_agent = parent

    call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="collect project context",
        thread_key="project-context",
        thread_summary="Project context research",
        wait=True,
    )
    child_id = control.list_agent_runs(agent_id="researcher")[0]["id"]
    waiting = control.get_agent_run(parent_run.id)
    assert waiting.status == AgentRunStatus.WAITING
    assert waiting.waiting_reason == AgentRunWaitingReason.AGENT_CALL

    control.complete_agent_run_activation(
        parent_run.id,
        ExecutorRunResult(task_id=parent_run.id, status="completed", output="waiting"),
        activation_id=_current_activation_id(control, parent_run.id),
    )
    control.complete_agent_run_activation(
        child_id,
        ExecutorRunResult(task_id=child_id, status="completed", output="context ready"),
        activation_id=_current_activation_id(control, child_id),
    )

    detail = control.load_agent_run_detail(parent_run.id)
    feedback = [
        item
        for item in detail["feedback"]
        if item["kind"] == AgentRunFeedbackKind.AGENT_CALL_RESULT.value
    ][0]
    assert feedback["requires_activation"] is True
    assert feedback["consumed_by_activation_id"] == "parent-run:activation:2"
    assert detail["agent_run"]["status"] == AgentRunStatus.QUEUED.value
    assert detail["activations"][-1]["input_kind"] == "agent_feedback"
    assert detail["activations"][-1]["input_payload"]["target_agent_run_id"] == child_id


def test_call_agent_wait_true_target_first_resumes_after_parent_completion() -> None:
    control = AgentRunControlPlane()
    parent_run = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="plan", owner_session_run_id="session-1"),
        task_id="parent-run",
    )
    parent, _stub = _parent_agent()
    parent.agent_run_control_plane = control
    parent.runtime_agent_run_id = parent_run.id
    parent.current_session_id = "session-1"
    call = CallAgentTool()
    call._parent_agent = parent

    call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="collect project context",
        thread_key="project-context",
        thread_summary="Project context research",
        wait=True,
    )
    child_id = control.list_agent_runs(agent_id="researcher")[0]["id"]
    control.complete_agent_run_activation(
        child_id,
        ExecutorRunResult(task_id=child_id, status="completed", output="context ready"),
        activation_id=_current_activation_id(control, child_id),
    )
    pending_detail = control.load_agent_run_detail(parent_run.id)
    pending_feedback = pending_detail["feedback"][0]
    assert pending_feedback["requires_activation"] is True
    assert pending_feedback["consumed_by_activation_id"] is None
    assert pending_detail["agent_run"]["status"] == AgentRunStatus.WAITING.value

    resumed = control.complete_agent_run_activation(
        parent_run.id,
        ExecutorRunResult(task_id=parent_run.id, status="completed", output="waiting"),
        activation_id=_current_activation_id(control, parent_run.id),
    )

    detail = control.load_agent_run_detail(parent_run.id)
    feedback = detail["feedback"][0]
    assert resumed.status == AgentRunStatus.QUEUED
    assert feedback["consumed_by_activation_id"] == "parent-run:activation:2"
    assert detail["activations"][-1]["input_kind"] == "agent_feedback"
    assert detail["activations"][-1]["input_payload"]["target_agent_run_id"] == child_id


def test_call_agent_wait_false_feedback_does_not_block_parent_completion() -> None:
    control = AgentRunControlPlane()
    parent_run = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="plan", owner_session_run_id="session-1"),
        task_id="parent-run",
    )
    parent, _stub = _parent_agent()
    parent.agent_run_control_plane = control
    parent.runtime_agent_run_id = parent_run.id
    parent.current_session_id = "session-1"
    call = CallAgentTool()
    call._parent_agent = parent

    call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="collect project context",
        thread_key="project-context",
        thread_summary="Project context research",
        wait=False,
    )
    child_id = control.list_agent_runs(agent_id="researcher")[0]["id"]
    assert control.get_agent_run(parent_run.id).status == AgentRunStatus.QUEUED

    control.complete_agent_run_activation(
        child_id,
        ExecutorRunResult(task_id=child_id, status="completed", output="context ready"),
        activation_id=_current_activation_id(control, child_id),
    )
    completed = control.complete_agent_run_activation(
        parent_run.id,
        ExecutorRunResult(task_id=parent_run.id, status="completed", output="done"),
        activation_id=_current_activation_id(control, parent_run.id),
    )

    detail = control.load_agent_run_detail(parent_run.id)
    feedback = [
        item
        for item in detail["feedback"]
        if item["kind"] == AgentRunFeedbackKind.AGENT_CALL_RESULT.value
    ][0]
    assert feedback["requires_activation"] is False
    assert feedback["consumed_by_activation_id"] is None
    assert completed.status == AgentRunStatus.COMPLETED


def test_agent_search_projects_persistent_existing_threads() -> None:
    control = AgentRunControlPlane()
    parent_run = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="plan", owner_session_run_id="session-1"),
        task_id="parent-run",
    )
    parent, _stub = _parent_agent()
    parent.agent_run_control_plane = control
    parent.runtime_agent_run_id = parent_run.id
    parent.current_session_id = "session-1"
    search = AgentSearchTool()
    search._parent_agent = parent
    call = CallAgentTool()
    call._parent_agent = parent

    call.execute(
        agent_id="researcher",
        conversation_scope="persistent",
        request="collect project context",
        thread_key="project-context",
        thread_summary="Project context research",
    )

    payload = json.loads(
        search.execute(query="research", conversation_scope="persistent")
    )

    assert payload["results"][0]["agent_id"] == "researcher"
    assert payload["results"][0]["existing_threads"][0]["thread_key"] == "project-context"
    assert payload["results"][0]["authorization_status"] == "requires_approval"


def test_call_agent_grant_reuses_approved_invocation_without_reapproval() -> None:
    control = AgentRunControlPlane()
    parent_run = control.submit_agent_run(
        AgentRunRequest(agent_id="planner", prompt="plan", owner_session_run_id="session-1"),
        task_id="parent-run",
    )
    config = Config(
        approval=ApprovalConfig(default_mode="require_approval"),
        agent_registry=AgentRegistryConfig(
            agents={
                "planner": AgentConfig(id="planner", name="Planner"),
                "researcher": AgentConfig(
                    id="researcher",
                    name="Researcher",
                    callable_scopes=["persistent"],
                    capability_refs=["capability:research"],
                    runtime_profile="analysis",
                ),
            }
        ),
    )

    class ApprovalProvider:
        def __init__(self) -> None:
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision.allow_once("ok")

    approval = ApprovalProvider()
    call = CallAgentTool()
    agent = Agent(
        llm=SimpleNamespace(),
        tools=[call],
        config=config,
        approval_provider=approval,
    )
    setattr(agent, "runtime_config", config)
    setattr(agent, "agent_config_id", "planner")
    setattr(agent, "runtime_agent_run_id", parent_run.id)
    setattr(agent, "current_session_id", "session-1")
    setattr(agent, "runtime_working_directory", "/workspace")
    setattr(agent, "permission_trigger_source", "chat")
    setattr(agent, "permission_interactive", True)
    setattr(agent, "agent_run_control_plane", control)
    call._parent_agent = agent

    arguments = {
        "agent_id": "researcher",
        "conversation_scope": "persistent",
        "request": "collect project context",
        "thread_key": "project-context",
        "thread_summary": "Project context research",
    }
    first = ToolExecutor(agent).execute(
        ToolCall(id="call-agent-1", name="call_agent", arguments=arguments)
    )
    grant_decision = agent.evaluate_tool_permission(
        call,
        tool_call=ToolCall(
            id="call-agent-grant-check",
            name="call_agent",
            arguments=arguments,
        ),
    )
    child_id = control.list_agent_runs(agent_id="researcher")[0]["id"]
    control.complete_agent_run_activation(
        child_id,
        ExecutorRunResult(task_id=child_id, status="completed", output="context ready"),
        activation_id=_current_activation_id(control, child_id),
    )
    second = ToolExecutor(agent).execute(
        ToolCall(
            id="call-agent-2",
            name="call_agent",
            arguments={
                **arguments,
                "request": "refresh project context",
            },
        )
    )

    assert "Agent call submitted" in first
    assert "Agent call submitted" in second
    assert len(approval.requests) == 1
    assert grant_decision.policy_matched == "agent_call_grant"

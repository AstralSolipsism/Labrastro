from __future__ import annotations

from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorRunRequest,
    ReuleauxCoderExecutorBackend,
)
from labrastro_server.services.agent_runtime.prompt_renderer import (
    CanonicalAgentContext,
    ExecutorPromptRenderer,
)
from reuleauxcoder.domain.agent_runtime.models import ExecutorType


def test_reuleaux_backend_binds_memory_scope_from_executor_request() -> None:
    seen: dict[str, str | None] = {}

    class FakeAgent:
        current_session_id = "session-1"

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:
            seen["prompt"] = prompt
            seen["owner_agent_id"] = getattr(self, "memory_owner_agent_id", None)
            seen["namespace"] = getattr(self, "memory_namespace", None)
            seen["task_id"] = getattr(self, "memory_task_id", None)
            seen["project_id"] = getattr(self, "memory_project_id", None)
            seen["workspace_id"] = getattr(self, "memory_workspace_id", None)
            seen["goal_id"] = getattr(self, "memory_goal_id", None)
            seen["taskflow_id"] = getattr(self, "memory_taskflow_id", None)
            return "ok"

    backend = ReuleauxCoderExecutorBackend(create_agent=lambda request: FakeAgent())
    request = ExecutorRunRequest(
        task_id="task-1",
        agent_id="agent-a",
        executor=ExecutorType.REULEAUXCODER,
        prompt="run task",
        workdir="/workspace/project",
        metadata={
            "project_id": "project-1",
            "workspace_id": "workspace-1",
            "goal_id": "goal-1",
            "taskflow_id": "taskflow-1",
        },
    )

    result = backend.start(request)

    assert result.output == "ok"
    assert seen == {
        "prompt": "run task",
        "owner_agent_id": "agent-a",
        "namespace": "agent-a",
        "task_id": "task-1",
        "project_id": "project-1",
        "workspace_id": "workspace-1",
        "goal_id": "goal-1",
        "taskflow_id": "taskflow-1",
    }


def test_external_executor_prompt_renderer_does_not_receive_memory_context() -> None:
    context = CanonicalAgentContext(
        agent_id="agent-a",
        agent_name="Agent A",
        agent_md="Follow configured agent instructions.",
        system_append="Use project state, not private memory.",
    )

    rendered = ExecutorPromptRenderer().render("codex", context)

    assert set(rendered.files) == {"AGENTS.md"}
    prompt = rendered.files["AGENTS.md"]
    assert "Private Agent Memory" not in prompt
    assert "memory_namespace" not in prompt
    assert "memory_items" not in prompt

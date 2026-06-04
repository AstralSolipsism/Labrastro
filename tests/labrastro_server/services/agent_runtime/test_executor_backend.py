from __future__ import annotations

import importlib

from reuleauxcoder.domain.agent.events import AgentEvent


def _executor_backend():
    return importlib.import_module(
        "labrastro_server.services.agent_runtime.executor_backend"
    )


def test_backend_registry_routes_start_resume_and_cancel_by_executor() -> None:
    backend_module = _executor_backend()

    class FakeBackend:
        executor = backend_module.ExecutorType.CODEX

        def __init__(self) -> None:
            self.started_task_id: str | None = None
            self.resumed_session_id: str | None = None
            self.cancelled_task_id: str | None = None

        def start(self, request):
            self.started_task_id = request.task_id
            return backend_module.ExecutorRunResult(
                task_id=request.task_id,
                status="completed",
                output="started",
                executor_session_id="codex-session-1",
            )

        def resume(self, session, prompt: str):
            self.resumed_session_id = session.executor_session_id
            return backend_module.ExecutorRunResult(
                task_id=session.task_id,
                status="completed",
                output=f"resumed: {prompt}",
                executor_session_id=session.executor_session_id,
            )

        def cancel(self, task_id: str, reason: str = "user_cancelled") -> bool:
            self.cancelled_task_id = task_id
            return reason == "user_cancelled"

    fake = FakeBackend()
    registry = backend_module.ExecutorBackendRegistry()
    registry.register(fake)

    started = registry.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="codex",
            prompt="review this",
        )
    )
    resumed = registry.resume(
        backend_module.TaskSessionRef(
            agent_id="reviewer",
            executor="codex",
            execution_location="remote_server",
            issue_id="issue-1",
            task_id="task-1",
            executor_session_id="codex-session-1",
        ),
        prompt="continue",
    )

    assert started.output == "started"
    assert resumed.output == "resumed: continue"
    assert registry.cancel("codex", "task-1") is True
    assert fake.started_task_id == "task-1"
    assert fake.resumed_session_id == "codex-session-1"
    assert fake.cancelled_task_id == "task-1"


def test_registry_rejects_missing_executor_backend() -> None:
    backend_module = _executor_backend()
    registry = backend_module.ExecutorBackendRegistry()

    try:
        registry.start(
            backend_module.ExecutorRunRequest(
                task_id="task-1",
                agent_id="reviewer",
                executor="claude",
                prompt="review this",
            )
        )
    except KeyError as exc:
        assert "claude" in str(exc)
    else:
        raise AssertionError("missing executor backend should be rejected")


def test_reuleauxcoder_backend_wraps_chat_output_as_executor_events() -> None:
    backend_module = _executor_backend()

    class FakeAgent:
        current_session_id = "session-1"

        def __init__(self) -> None:
            self.prompt: str | None = None
            self.clear_stop_request: bool | None = None

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:
            self.prompt = prompt
            self.clear_stop_request = clear_stop_request
            return "done"

    agents: list[FakeAgent] = []

    def create_agent(_request):
        agent = FakeAgent()
        agents.append(agent)
        return agent

    backend = backend_module.ReuleauxCoderExecutorBackend(create_agent=create_agent)
    result = backend.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="run",
        )
    )

    assert result.status == "completed"
    assert result.output == "done"
    assert result.executor_session_id == "session-1"
    assert [event.type.value for event in result.events] == [
        "status",
        "text",
        "status",
    ]
    assert result.events[1].text == "done"
    assert agents[0].prompt == "run"
    assert agents[0].clear_stop_request is True


def test_reuleauxcoder_backend_captures_lifecycle_hook_events() -> None:
    backend_module = _executor_backend()

    class FakeAgent:
        current_session_id = "session-1"

        def __init__(self) -> None:
            self._event_handlers = []

        def add_event_handler(self, handler) -> None:
            self._event_handlers.append(handler)

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:  # noqa: ARG002
            for handler in list(self._event_handlers):
                handler(
                    AgentEvent.lifecycle_hook(
                        {
                            "phase": "result",
                            "event_name": "Stop",
                            "hook_id": "hook:stop",
                            "display_name": "Stop review",
                            "message": "review passed",
                        }
                    )
                )
            return "done"

    agent = FakeAgent()
    backend = backend_module.ReuleauxCoderExecutorBackend(
        create_agent=lambda _request: agent
    )
    result = backend.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="run",
        )
    )

    assert [event.type.value for event in result.events] == [
        "status",
        "lifecycle_hook",
        "text",
        "status",
    ]
    assert result.events[1].data["phase"] == "result"
    assert result.events[1].data["event_name"] == "Stop"
    assert result.events[1].data["hook_id"] == "hook:stop"
    assert agent._event_handlers == []


def test_reuleauxcoder_backend_detaches_lifecycle_hook_handler_after_failure() -> None:
    backend_module = _executor_backend()

    class FakeAgent:
        current_session_id = "session-1"

        def __init__(self) -> None:
            self._event_handlers = []

        def add_event_handler(self, handler) -> None:
            self._event_handlers.append(handler)

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:  # noqa: ARG002
            for handler in list(self._event_handlers):
                handler(
                    AgentEvent.lifecycle_hook(
                        {
                            "phase": "dispatch_failed",
                            "event_name": "UserPromptSubmit",
                            "hook_id": "hook:prompt",
                            "error": "failed closed",
                        }
                    )
                )
            raise RuntimeError("boom")

    agent = FakeAgent()
    backend = backend_module.ReuleauxCoderExecutorBackend(
        create_agent=lambda _request: agent
    )
    result = backend.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="run",
        )
    )

    assert result.status == "failed"
    assert [event.type.value for event in result.events] == [
        "status",
        "lifecycle_hook",
        "error",
        "status",
    ]
    assert result.events[1].data["phase"] == "dispatch_failed"
    assert agent._event_handlers == []


def test_reuleauxcoder_backend_binds_permission_context_to_agent() -> None:
    backend_module = _executor_backend()

    class FakeAgent:
        current_session_id = "session-1"

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:  # noqa: ARG002
            return "done"

    agents: list[FakeAgent] = []

    def create_agent(_request):
        agent = FakeAgent()
        agents.append(agent)
        return agent

    backend = backend_module.ReuleauxCoderExecutorBackend(create_agent=create_agent)
    backend.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="run",
            workdir="/workspace/repo",
            runtime_profile_id="codex",
            metadata={
                "permission_context": {
                    "agent_id": "reviewer",
                    "source": "taskflow",
                    "interactive": False,
                    "runtime_profile_id": "codex",
                    "effective_capabilities": {
                        "tools": ["builtin:read_file"],
                        "execution_policies": [],
                    },
                }
            },
        )
    )

    agent = agents[0]
    assert getattr(agent, "agent_config_id") == "reviewer"
    assert getattr(agent, "runtime_task_id") == "task-1"
    assert getattr(agent, "runtime_workspace_root") == "/workspace/repo"
    assert getattr(agent, "permission_trigger_source") == "taskflow"
    assert getattr(agent, "permission_interactive") is False
    assert getattr(agent, "runtime_profile_id") == "codex"
    assert getattr(agent, "effective_capabilities") == {
        "tools": ["builtin:read_file"],
        "execution_policies": [],
    }
    assert getattr(agent, "enforce_effective_capabilities") is True


def test_reuleauxcoder_backend_resume_restores_executor_session_id() -> None:
    backend_module = _executor_backend()

    class FakeAgent:
        current_session_id = None

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:
            return f"{self.current_session_id}: {prompt}"

    backend = backend_module.ReuleauxCoderExecutorBackend(
        create_agent=lambda _request: FakeAgent()
    )

    result = backend.resume(
        backend_module.TaskSessionRef(
            agent_id="reviewer",
            executor="reuleauxcoder",
            execution_location="local_workspace",
            issue_id="issue-1",
            task_id="task-1",
            executor_session_id="session-1",
        ),
        prompt="continue",
    )

    assert result.output == "session-1: continue"
    assert result.executor_session_id == "session-1"


def test_reuleauxcoder_backend_resume_restores_permission_context() -> None:
    backend_module = _executor_backend()

    class FakeAgent:
        current_session_id = "session-1"

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:  # noqa: ARG002
            return "resumed"

    agents: list[FakeAgent] = []

    def create_agent(_request):
        agent = FakeAgent()
        agents.append(agent)
        return agent

    backend = backend_module.ReuleauxCoderExecutorBackend(create_agent=create_agent)
    backend.resume(
        backend_module.TaskSessionRef(
            agent_id="reviewer",
            executor="reuleauxcoder",
            execution_location="local_workspace",
            issue_id="issue-1",
            task_id="task-1",
            workdir="/workspace/repo",
            executor_session_id="session-1",
            metadata={
                "permission_context": {
                    "agent_id": "reviewer",
                    "source": "taskflow",
                    "interactive": False,
                    "runtime_profile_id": "codex",
                    "effective_capabilities": {
                        "tools": ["builtin:read_file"],
                        "execution_policies": [],
                    },
                }
            },
        ),
        prompt="continue",
    )

    agent = agents[0]
    assert getattr(agent, "agent_config_id") == "reviewer"
    assert getattr(agent, "runtime_task_id") == "task-1"
    assert getattr(agent, "runtime_workspace_root") == "/workspace/repo"
    assert getattr(agent, "permission_trigger_source") == "taskflow"
    assert getattr(agent, "permission_interactive") is False
    assert getattr(agent, "runtime_profile_id") == "codex"
    assert getattr(agent, "effective_capabilities") == {
        "tools": ["builtin:read_file"],
        "execution_policies": [],
    }
    assert getattr(agent, "enforce_effective_capabilities") is True


def test_reuleauxcoder_backend_cancel_delegates_to_active_agent() -> None:
    backend_module = _executor_backend()

    class FakeAgent:
        current_session_id = "session-1"

        def __init__(self) -> None:
            self.cancel_reason: str | None = None

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:
            return "done"

        def request_stop(self, reason: str) -> None:
            self.cancel_reason = reason

    agent = FakeAgent()
    backend = backend_module.ReuleauxCoderExecutorBackend(
        create_agent=lambda _request: agent
    )
    backend.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="run",
        )
    )

    assert backend.cancel("task-1", reason="user_cancelled") is True
    assert agent.cancel_reason == "user_cancelled"

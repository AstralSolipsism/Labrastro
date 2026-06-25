from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
import threading
import time

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.llm.models import ToolCall


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


def test_reuleauxcoder_backend_event_sink_receives_events_during_chat() -> None:
    backend_module = _executor_backend()
    live_events = []
    live_checkpoints: list[str] = []

    class FakeAgent:
        current_session_id = "session-1"

        def __init__(self) -> None:
            self._event_handlers = []

        def add_event_handler(self, handler) -> None:
            self._event_handlers.append(handler)

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:  # noqa: ARG002
            live_checkpoints.append(live_events[-1].data["status"])
            for handler in list(self._event_handlers):
                handler(AgentEvent.reasoning_token("thinking"))
            live_checkpoints.append(live_events[-1].data["event_type"])
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
            event_sink=live_events.append,
        )
    )

    assert live_checkpoints == ["running", "reasoning_delta"]
    assert [event.type.value for event in live_events] == [
        event.type.value for event in result.events
    ]
    assert result.events[1].data == {
        "event_type": "reasoning_delta",
        "payload": {"content": "thinking"},
    }


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


def test_reuleauxcoder_backend_captures_lifecycle_runtime_artifacts_without_event_content() -> None:
    backend_module = _executor_backend()
    huge = "OVERSIZED_LIFECYCLE_OUTPUT_SECRET" * 500
    artifact_id = "lifecycle-output-overflow:hook-stop:1"

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
                            "output": {
                                "artifacts": [
                                    {
                                        "kind": "lifecycle_output_overflow",
                                        "id": artifact_id,
                                        "field": "reason",
                                        "original_chars": len(huge),
                                    }
                                ]
                            },
                        },
                        runtime_artifacts=[
                            {
                                "artifact_id": artifact_id,
                                "type": "log",
                                "status": "generated",
                                "content": huge,
                                "metadata": {
                                    "kind": "lifecycle_output_overflow",
                                    "hook_id": "hook:stop",
                                    "event_name": "Stop",
                                    "field": "reason",
                                },
                            }
                        ],
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
    events_json = json.dumps(
        [event.to_dict() for event in result.events],
        sort_keys=True,
    )

    assert result.artifacts == [
        {
            "artifact_id": artifact_id,
            "type": "log",
            "status": "generated",
            "content": huge,
            "metadata": {
                "kind": "lifecycle_output_overflow",
                "hook_id": "hook:stop",
                "event_name": "Stop",
                "field": "reason",
            },
        }
    ]
    assert artifact_id in events_json
    assert "OVERSIZED_LIFECYCLE_OUTPUT_SECRET" not in events_json


def test_reuleauxcoder_backend_captures_mcp_elicitation_lifecycle_from_tool_context() -> None:
    backend_module = _executor_backend()

    class FakeMCPTool:
        name = "search"
        description = "Search docs"
        tool_source = "mcp"
        server_name = "docs"
        parameters = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }

        def __init__(self) -> None:
            self.context: dict = {}

        def bind_lifecycle_context(self, context: dict):
            self.context = dict(context)

            def _restore() -> None:
                self.context = {}

            return _restore

        def preflight_validate(self, **_kwargs) -> None:
            return None

        def execute(self, query: str) -> str:
            emit = self.context.get("_agent_lifecycle_event_emitter")
            assert callable(emit)
            emit(
                {
                    "phase": "request",
                    "event_name": "Elicitation",
                    "session_run_id": self.context["session_run_id"],
                    "agent_run_id": self.context["agent_run_id"],
                    "branch_binding_id": self.context["branch_binding_id"],
                    "turn_id": self.context["turn_id"],
                    "tool_call_id": self.context["tool_call_id"],
                    "tool_name": self.context["tool_name"],
                    "mcp_server": self.context["mcp_server"],
                    "message": "Choose repository",
                    "payload": {"query": query},
                }
            )
            emit(
                {
                    "phase": "result",
                    "event_name": "ElicitationResult",
                    "session_run_id": self.context["session_run_id"],
                    "agent_run_id": self.context["agent_run_id"],
                    "branch_binding_id": self.context["branch_binding_id"],
                    "turn_id": self.context["turn_id"],
                    "tool_call_id": self.context["tool_call_id"],
                    "tool_name": self.context["tool_name"],
                    "mcp_server": self.context["mcp_server"],
                    "message": "MCP elicitation accepted.",
                    "payload": {"result_action": "accept"},
                }
            )
            return "ok"

    class FakeAgent:
        current_session_id = "session-1"
        runtime_turn_id = "turn-1"
        active_mode = "coder"

        def __init__(self) -> None:
            self._event_handlers = []
            self.state = SimpleNamespace(current_round=0)
            self.approval_provider = None
            self.lifecycle_dispatcher = None
            self.tool = FakeMCPTool()

        def add_event_handler(self, handler) -> None:
            self._event_handlers.append(handler)

        def _emit_event(self, event) -> None:
            for handler in list(self._event_handlers):
                handler(event)

        def get_tool(self, name: str):  # noqa: ARG002
            return self.tool

        def is_tool_allowed_in_mode(self, name: str) -> bool:  # noqa: ARG002
            return True

        def suggest_modes_for_tool(self, name: str) -> list[str]:  # noqa: ARG002
            return []

        def get_active_mode_config(self):
            return SimpleNamespace(prompt_append="")

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:  # noqa: ARG002
            ToolExecutor(self).execute(
                ToolCall(id="call-mcp-1", name="search", arguments={"query": "hooks"})
            )
            return "done"

    agent = FakeAgent()
    backend = backend_module.ReuleauxCoderExecutorBackend(
        create_agent=lambda _request: agent
    )

    result = backend.start(
        backend_module.ExecutorRunRequest(
            task_id="agent-run-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="run",
            metadata={"branch_binding_id": "branch-a"},
        )
    )

    lifecycle_events = [
        event.data for event in result.events if event.type.value == "lifecycle_hook"
    ]
    assert [event["event_name"] for event in lifecycle_events] == [
        "Elicitation",
        "ElicitationResult",
    ]
    assert lifecycle_events[0]["agent_run_id"] == "agent-run-1"
    assert lifecycle_events[0]["branch_binding_id"] == "branch-a"
    assert lifecycle_events[0]["session_run_id"] == "session-1"
    assert lifecycle_events[0]["tool_call_id"] == "call-mcp-1"
    assert lifecycle_events[0]["tool_name"] == "search"
    assert lifecycle_events[0]["mcp_server"] == "docs"
    assert lifecycle_events[1]["payload"]["result_action"] == "accept"
    assert agent._event_handlers == []


def test_reuleauxcoder_backend_maps_budget_session_end_to_blocked_result() -> None:
    backend_module = _executor_backend()

    class FakeAgent:
        current_session_id = "session-1"

        def __init__(self) -> None:
            self._event_handlers = []

        def add_event_handler(self, handler) -> None:
            self._event_handlers.append(handler)

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:  # noqa: ARG002
            message = "AgentRun budget exceeded: max_turns=1"
            for handler in list(self._event_handlers):
                handler(
                    AgentEvent.session_run_end(
                        f"({message})",
                        status="budget_exceeded",
                        error=message,
                        session_state="budget_exceeded",
                    )
                )
            return f"({message})"

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

    assert result.status == "blocked"
    assert result.error == "AgentRun budget exceeded: max_turns=1"
    assert [event.type.value for event in result.events] == [
        "status",
        "session_run_end",
        "text",
        "status",
    ]
    assert result.events[1].data == {
        "response": "(AgentRun budget exceeded: max_turns=1)",
        "response_rendered": True,
        "status": "budget_exceeded",
        "error": "AgentRun budget exceeded: max_turns=1",
        "session_state": "budget_exceeded",
    }
    assert result.events[-1].data["status"] == "blocked"
    assert agent._event_handlers == []


def test_reuleauxcoder_backend_marks_streamed_session_end_as_rendered_before_text_event() -> None:
    backend_module = _executor_backend()

    class FakeAgent:
        current_session_id = "session-1"

        def __init__(self) -> None:
            self._event_handlers = []

        def add_event_handler(self, handler) -> None:
            self._event_handlers.append(handler)

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:  # noqa: ARG002
            for handler in list(self._event_handlers):
                handler(AgentEvent.session_run_end("streamed answer", render_response=False))
            return "streamed answer"

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

    session_end = [
        event for event in result.events if event.type.value == "session_run_end"
    ][0]
    text_events = [event for event in result.events if event.type.value == "text"]
    assert session_end.data == {
        "response": "streamed answer",
        "response_rendered": True,
    }
    assert [event.text for event in text_events] == ["streamed answer"]


def test_reuleauxcoder_backend_captures_tool_start_and_result_events() -> None:
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
                    AgentEvent.tool_call_start(
                        "apply_patch",
                        {"path": "a.txt", "content": "updated"},
                        tool_call_id="call-1",
                        tool_source="builtin",
                        index=0,
                    )
                )
                handler(
                    AgentEvent.tool_call_end(
                        "apply_patch",
                        "wrote a.txt",
                        tool_call_id="call-1",
                        tool_source="builtin",
                        index=0,
                        meta={"lifecycle": "post_tool_observed"},
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

    tool_events = [
        event for event in result.events if event.type.value in {"tool_use", "tool_result"}
    ]
    assert [event.type.value for event in tool_events] == ["tool_use", "tool_result"]
    assert tool_events[0].data == {
        "tool_name": "apply_patch",
        "tool_call_id": "call-1",
        "input": {"path": "a.txt", "content": "updated"},
        "tool_source": "builtin",
        "index": 0,
    }
    assert tool_events[1].data == {
        "tool_name": "apply_patch",
        "tool_call_id": "call-1",
        "output": "wrote a.txt",
        "tool_source": "builtin",
        "index": 0,
        "meta": {"lifecycle": "post_tool_observed"},
    }
    assert agent._event_handlers == []


def test_reuleauxcoder_backend_captures_usage_update_events() -> None:
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
                    AgentEvent.usage_update(
                        prompt_tokens=7,
                        completion_tokens=10,
                        model="gpt-4.1",
                        run_status="running",
                        usage_extra={
                            "lifecycle_prompt": {
                                "hook_id": "hook:prompt-review",
                                "provider": "test",
                            }
                        },
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
        "usage",
        "text",
        "status",
    ]
    assert result.events[1].data["prompt_tokens"] == 7
    assert result.events[1].data["completion_tokens"] == 10
    assert result.events[1].data["usage_extra"]["lifecycle_prompt"]["hook_id"] == (
        "hook:prompt-review"
    )
    assert agent._event_handlers == []


def test_reuleauxcoder_backend_captures_lifecycle_prompt_usage_from_real_agent() -> None:
    from reuleauxcoder.domain.config.models import (
        Config,
        ModelProfileConfig,
        ProviderConfig,
        ProvidersConfig,
        SkillRegistrationConfig,
        SkillsConfig,
    )
    from reuleauxcoder.domain.llm.models import LLMResponse
    from reuleauxcoder.interfaces.entrypoint.dependencies import _default_create_agent

    backend_module = _executor_backend()

    class FakeLLM:
        model = "fake-model"
        debug_trace = False
        max_tokens = None

        def __init__(self) -> None:
            self.lifecycle_calls = 0

        def chat(self, messages, **kwargs):  # noqa: ARG002
            if (
                messages
                and "LifecycleHookOutput" in str(messages[0].get("content") or "")
            ):
                self.lifecycle_calls += 1
                return LLMResponse(
                    content='{"diagnostics":[{"code":"prompt_ok"}]}',
                    prompt_tokens=2,
                    completion_tokens=3,
                    usage_extra={
                        "lifecycle_prompt": {
                            "hook_id": "hook:skill:prompt-router:UserPromptSubmit:0",
                            "provider": "test",
                        }
                    },
                )
            return LLMResponse(
                content="done",
                prompt_tokens=5,
                completion_tokens=7,
            )

    llm = FakeLLM()
    config = Config(
        providers=ProvidersConfig(
            items={"openai": ProviderConfig(id="openai", api_key="key")}
        ),
        model_profiles={
            "main": ModelProfileConfig(
                name="main",
                provider="openai",
                model="gpt-4.1",
                max_tokens=8192,
                max_context_tokens=12345,
            )
        },
        active_main_model_profile="main",
        skills=SkillsConfig(
            items={
                "prompt-router": SkillRegistrationConfig(
                    name="prompt-router",
                    hooks=[
                        {
                            "event": "UserPromptSubmit",
                            "placement": "server",
                            "handler_type": "prompt",
                            "display_name": "Prompt observer",
                            "summary": "Records prompt lifecycle usage.",
                            "permissions": [],
                            "trust": "trusted",
                            "technical": {"prompt": "Return lifecycle diagnostics."},
                        }
                    ],
                )
            }
        ),
    )

    backend = backend_module.ReuleauxCoderExecutorBackend(
        create_agent=lambda _request: _default_create_agent(llm, [], config)
    )

    result = backend.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="run",
        )
    )

    usage_events = [
        event for event in result.events if event.type.value == "usage"
    ]
    lifecycle_usage = [
        event
        for event in usage_events
        if event.data.get("usage_extra", {}).get("lifecycle_prompt", {}).get("hook_id")
        == "hook:skill:prompt-router:UserPromptSubmit:0"
    ]
    assert result.status == "completed"
    assert llm.lifecycle_calls == 1
    assert lifecycle_usage
    assert lifecycle_usage[-1].data["prompt_tokens"] >= 2
    assert lifecycle_usage[-1].data["completion_tokens"] >= 3


def test_reuleauxcoder_backend_persists_lifecycle_overflow_artifacts_from_real_agent() -> None:
    from reuleauxcoder.domain.config.models import (
        Config,
        ModelProfileConfig,
        ProviderConfig,
        ProvidersConfig,
        SkillRegistrationConfig,
        SkillsConfig,
    )
    from reuleauxcoder.domain.llm.models import LLMResponse
    from reuleauxcoder.interfaces.entrypoint.dependencies import _default_create_agent

    backend_module = _executor_backend()
    huge = "OVERSIZED_LIFECYCLE_OUTPUT_SECRET" * 500

    class FakeLLM:
        model = "fake-model"
        debug_trace = False
        max_tokens = None

        def chat(self, messages, **kwargs):  # noqa: ARG002
            if (
                messages
                and "LifecycleHookOutput" in str(messages[0].get("content") or "")
            ):
                return LLMResponse(
                    content=json.dumps(
                        {
                            "decision": "deny",
                            "continue_flow": False,
                            "reason": huge,
                        }
                    )
                )
            return LLMResponse(content="done")

    config = Config(
        providers=ProvidersConfig(
            items={"openai": ProviderConfig(id="openai", api_key="key")}
        ),
        model_profiles={
            "main": ModelProfileConfig(
                name="main",
                provider="openai",
                model="gpt-4.1",
                max_tokens=8192,
                max_context_tokens=12345,
            )
        },
        active_main_model_profile="main",
        skills=SkillsConfig(
            items={
                "prompt-router": SkillRegistrationConfig(
                    name="prompt-router",
                    hooks=[
                        {
                            "event": "UserPromptSubmit",
                            "placement": "server",
                            "handler_type": "prompt",
                            "display_name": "Prompt guard",
                            "summary": "Blocks unsafe prompt.",
                            "permissions": [],
                            "trust": "trusted",
                            "technical": {"prompt": "Return lifecycle decision."},
                        }
                    ],
                )
            }
        ),
    )
    backend = backend_module.ReuleauxCoderExecutorBackend(
        create_agent=lambda _request: _default_create_agent(FakeLLM(), [], config)
    )

    result = backend.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="run",
        )
    )
    events_json = json.dumps(
        [event.to_dict() for event in result.events],
        sort_keys=True,
    )

    assert result.status == "blocked"
    assert result.artifacts == [
        {
            "artifact_id": (
                "lifecycle-output-overflow:"
                "hook-skill-prompt-router-UserPromptSubmit-0:1"
            ),
            "type": "log",
            "status": "generated",
            "content": huge,
            "metadata": {
                "kind": "lifecycle_output_overflow",
                "hook_id": "hook:skill:prompt-router:UserPromptSubmit:0",
                "source": "skill",
                "handler_type": "prompt",
                "field": "reason",
                "original_chars": len(huge),
                "original_bytes": len(huge.encode("utf-8", errors="replace")),
                "preview": huge[:256],
                "event_name": "UserPromptSubmit",
                "session_run_id": "",
                "agent_run_id": "task-1",
                "turn_id": "",
                "placement": "server",
                "trigger_source": "taskflow",
            },
        }
    ]
    assert huge not in events_json
    assert result.artifacts[0]["artifact_id"] in events_json


def test_reuleauxcoder_backend_captures_lifecycle_updated_session_start_from_real_agent() -> None:
    from reuleauxcoder.domain.config.models import (
        Config,
        ModelProfileConfig,
        ProviderConfig,
        ProvidersConfig,
        SkillRegistrationConfig,
        SkillsConfig,
    )
    from reuleauxcoder.domain.llm.models import LLMResponse
    from reuleauxcoder.interfaces.entrypoint.dependencies import _default_create_agent

    backend_module = _executor_backend()

    class FakeLLM:
        model = "fake-model"
        debug_trace = False
        max_tokens = None

        def __init__(self) -> None:
            self.messages = []

        def chat(self, messages, **kwargs):  # noqa: ARG002
            if (
                messages
                and "LifecycleHookOutput" in str(messages[0].get("content") or "")
            ):
                return LLMResponse(
                    content='{"updated_input":{"user_input":"rewritten prompt"}}'
                )
            self.messages = messages
            return LLMResponse(content="done")

    llm = FakeLLM()
    config = Config(
        providers=ProvidersConfig(
            items={"openai": ProviderConfig(id="openai", api_key="key")}
        ),
        model_profiles={
            "main": ModelProfileConfig(
                name="main",
                provider="openai",
                model="gpt-4.1",
                max_tokens=8192,
                max_context_tokens=12345,
            )
        },
        active_main_model_profile="main",
        skills=SkillsConfig(
            items={
                "prompt-router": SkillRegistrationConfig(
                    name="prompt-router",
                    hooks=[
                        {
                            "event": "UserPromptSubmit",
                            "placement": "server",
                            "handler_type": "prompt",
                            "display_name": "Rewrite prompt",
                            "summary": "Rewrite prompt before the model sees it.",
                            "permissions": [],
                            "trust": "trusted",
                            "technical": {"prompt": "Rewrite the prompt."},
                        }
                    ],
                )
            }
        ),
    )
    backend = backend_module.ReuleauxCoderExecutorBackend(
        create_agent=lambda _request: _default_create_agent(llm, [], config)
    )

    result = backend.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="original prompt",
        )
    )

    session_start_events = [
        event for event in result.events if event.type.value == "session_run_start"
    ]
    assert result.status == "completed"
    assert session_start_events
    assert session_start_events[0].data["prompt"] == "rewritten prompt"
    assert llm.messages[-2]["content"] == "rewritten prompt"


def test_reuleauxcoder_backend_captures_skill_catalog_expansion_lifecycle_from_real_agent() -> None:
    from reuleauxcoder.domain.config.models import (
        Config,
        ModelProfileConfig,
        ProviderConfig,
        ProvidersConfig,
        SkillRegistrationConfig,
        SkillsConfig,
    )
    from reuleauxcoder.domain.llm.models import LLMResponse
    from reuleauxcoder.interfaces.entrypoint.dependencies import _default_create_agent
    from labrastro_server.services.agent_runtime.session_projection import (
        agent_run_event_to_session_events,
    )

    backend_module = _executor_backend()

    class FakeLLM:
        model = "fake-model"
        debug_trace = False
        max_tokens = None

        def __init__(self) -> None:
            self.main_messages = []

        def chat(self, messages, **kwargs):  # noqa: ARG002
            if (
                messages
                and "LifecycleHookOutput" in str(messages[0].get("content") or "")
            ):
                return LLMResponse(
                    content=(
                        '{"additional_context":['
                        '"Use code-review skill only after loading SKILL.md."]}'
                    )
                )
            self.main_messages = messages
            return LLMResponse(content="done")

    llm = FakeLLM()
    config = Config(
        providers=ProvidersConfig(
            items={"openai": ProviderConfig(id="openai", api_key="key")}
        ),
        model_profiles={
            "main": ModelProfileConfig(
                name="main",
                provider="openai",
                model="gpt-4.1",
                max_tokens=8192,
                max_context_tokens=12345,
            )
        },
        active_main_model_profile="main",
        skills=SkillsConfig(
            items={
                "code-review": SkillRegistrationConfig(
                    name="code-review",
                    hooks=[
                        {
                            "event": "UserPromptExpansion",
                            "placement": "server",
                            "handler_type": "prompt",
                            "display_name": "Skill catalog guard",
                            "summary": "Reviews active Skill catalog expansion.",
                            "permissions": [],
                            "trust": "trusted",
                            "technical": {"prompt": "Review Skill catalog expansion."},
                        }
                    ],
                )
            }
        ),
    )

    def create_agent(_request):
        agent = _default_create_agent(llm, [], config)
        agent.skills_catalog = "# Skills\n- code-review"
        return agent

    backend = backend_module.ReuleauxCoderExecutorBackend(create_agent=create_agent)

    result = backend.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="review this",
        )
    )

    lifecycle_events = [
        event
        for event in result.events
        if event.type.value == "lifecycle_hook"
        and event.data.get("event_name") == "UserPromptExpansion"
        and event.data.get("payload", {}).get("trigger_kind") == "skill_catalog"
    ]
    assert result.status == "completed"
    assert [event.data["phase"] for event in lifecycle_events] == [
        "dispatch_start",
        "result",
    ]
    assert lifecycle_events[-1].data["event_name"] == "UserPromptExpansion"
    assert lifecycle_events[-1].data["trigger_source"] == "skill"
    assert lifecycle_events[-1].data["payload"]["trigger_kind"] == "skill_catalog"
    assert "Lifecycle skill context:" in llm.main_messages[0]["content"]
    assert "Use code-review skill only after loading SKILL.md." in (
        llm.main_messages[0]["content"]
    )

    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "task-1",
            "seq": 2,
            "type": "lifecycle_hook",
            "payload": {
                "type": "lifecycle_hook",
                "data": lifecycle_events[-1].data,
            },
        }
    )
    assert session_events[0][0] == "lifecycle_hook"
    assert session_events[0][1]["event_name"] == "UserPromptExpansion"
    assert session_events[0][1]["trigger_source"] == "skill"


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


def test_reuleauxcoder_backend_preserves_chat_exception_diagnostics() -> None:
    backend_module = _executor_backend()

    class FakeAgent:
        current_session_id = "session-1"

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:  # noqa: ARG002
            error = RuntimeError("Connection error.")
            setattr(error, "llm_diagnostic_path", "/tmp/llm_error_session.json")
            setattr(error, "provider_error_phase", "request_start")
            raise error

    backend = backend_module.ReuleauxCoderExecutorBackend(
        create_agent=lambda _request: FakeAgent()
    )
    result = backend.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="run",
        )
    )

    error_events = [event for event in result.events if event.type.value == "error"]
    assert result.status == "failed"
    assert error_events
    assert error_events[0].text == "Connection error."
    assert error_events[0].data["error_type"] == "RuntimeError"
    assert error_events[0].data["provider_error_phase"] == "request_start"
    assert error_events[0].data["diagnostic_path"].endswith(
        "llm_error_session.json"
    )


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
                "resolved_capabilities": {
                    "builtin_tool_grants": ["read_file"],
                    "tool_specs": [],
                },
                "permission_context": {
                    "agent_id": "reviewer",
                    "source": "taskflow",
                    "interactive": False,
                    "runtime_profile_id": "codex",
                    "effective_capabilities": {
                        "builtin_tool_grants": ["read_file"],
                        "tool_specs": [],
                        "execution_policies": [],
                    },
                }
            },
        )
    )

    agent = agents[0]
    assert getattr(agent, "agent_config_id") == "reviewer"
    assert getattr(agent, "runtime_agent_run_id") == "task-1"
    assert getattr(agent, "runtime_task_id") == "task-1"
    assert getattr(agent, "runtime_workspace_root") == "/workspace/repo"
    assert getattr(agent, "runtime_working_directory") == "/workspace/repo"
    assert getattr(agent, "permission_trigger_source") == "taskflow"
    assert getattr(agent, "permission_interactive") is False
    assert getattr(agent, "runtime_profile_id") == "codex"
    assert getattr(agent, "effective_capabilities") == {
        "builtin_tool_grants": ["read_file"],
        "tool_specs": [],
        "execution_policies": [],
    }
    assert getattr(agent, "resolved_capabilities")["builtin_tool_grants"] == [
        "read_file"
    ]
    assert getattr(agent, "resolved_capabilities")["tool_specs"] == []
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
                        "builtin_tool_grants": ["read_file"],
                        "tool_specs": [],
                        "execution_policies": [],
                    },
                }
            },
        ),
        prompt="continue",
    )

    agent = agents[0]
    assert getattr(agent, "agent_config_id") == "reviewer"
    assert getattr(agent, "runtime_agent_run_id") == "task-1"
    assert getattr(agent, "runtime_task_id") == "task-1"
    assert getattr(agent, "runtime_workspace_root") == "/workspace/repo"
    assert getattr(agent, "runtime_working_directory") == "/workspace/repo"
    assert getattr(agent, "permission_trigger_source") == "taskflow"
    assert getattr(agent, "permission_interactive") is False
    assert getattr(agent, "runtime_profile_id") == "codex"
    assert getattr(agent, "effective_capabilities") == {
        "builtin_tool_grants": ["read_file"],
        "tool_specs": [],
        "execution_policies": [],
    }
    assert getattr(agent, "enforce_effective_capabilities") is True


def test_reuleauxcoder_backend_applies_budget_as_runtime_bounds() -> None:
    backend_module = _executor_backend()

    class FakeAgent:
        current_session_id = "session-1"
        max_rounds = 50

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:  # noqa: ARG002
            return "done"

    agent = FakeAgent()
    backend = backend_module.ReuleauxCoderExecutorBackend(
        create_agent=lambda _request: agent
    )
    before = time.monotonic()

    backend.start(
        backend_module.ExecutorRunRequest(
            task_id="task-1",
            agent_id="reviewer",
            executor="reuleauxcoder",
            prompt="run",
            budget={
                "max_turns": "2",
                "timeout_sec": "30",
                "token_budget": "1200",
                "max_tool_calls": "3",
            },
        )
    )

    assert agent.max_rounds == 2
    assert agent.runtime_budget == {
        "max_turns": 2,
        "timeout_sec": 30,
        "token_budget": 1200,
        "max_tool_calls": 3,
    }
    assert agent.runtime_timeout_sec == 30
    assert agent.runtime_deadline >= before + 29
    assert agent.runtime_token_budget == 1200
    assert agent.runtime_budget_enforcement == {
        "max_tool_calls": "tool_executor",
        "max_turns": "agent_loop",
        "timeout_sec": "agent_loop_and_tool_executor",
        "token_budget": "agent_loop_and_tool_executor",
    }


def test_reuleauxcoder_backend_does_not_cancel_completed_agent() -> None:
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

    assert backend.cancel("task-1", reason="user_cancelled") is False
    assert agent.cancel_reason is None


def test_reuleauxcoder_backend_cancel_delegates_to_running_agent() -> None:
    backend_module = _executor_backend()
    started = threading.Event()
    release = threading.Event()

    class FakeAgent:
        current_session_id = "session-1"

        def __init__(self) -> None:
            self.cancel_reason: str | None = None

        def chat(self, prompt: str, *, clear_stop_request: bool = True) -> str:  # noqa: ARG002
            started.set()
            release.wait(timeout=5)
            return "done"

        def request_stop(self, reason: str) -> None:
            self.cancel_reason = reason

    agent = FakeAgent()
    backend = backend_module.ReuleauxCoderExecutorBackend(
        create_agent=lambda _request: agent
    )
    result_holder = []
    thread = threading.Thread(
        target=lambda: result_holder.append(
            backend.start(
                backend_module.ExecutorRunRequest(
                    task_id="task-1",
                    agent_id="reviewer",
                    executor="reuleauxcoder",
                    prompt="run",
                )
            )
        )
    )
    thread.start()
    assert started.wait(timeout=5)

    try:
        assert backend.cancel("task-1", reason="user_cancelled") is True
        assert agent.cancel_reason == "user_cancelled"
    finally:
        release.set()
        thread.join(timeout=5)

    assert result_holder[0].status == "completed"

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.session.document import apply_session_event
from reuleauxcoder.domain.hooks.lifecycle import (
    FunctionLifecycleHookRuntimeAdapter,
    LifecycleHookDeclaration,
    LifecycleHookDispatchResult,
    LifecycleHookDispatcher,
    LifecycleHookEventContext,
    LifecycleHookOutput,
    LifecycleHookRegistry,
    LifecycleHookRuntimeAdapterRegistry,
)
from reuleauxcoder.domain.llm.models import LLMResponse, ToolCall


class _LLM:
    model = "test-model"
    max_tokens = None

    def __init__(self) -> None:
        self.messages = []

    def chat(self, messages, **kwargs):  # noqa: ARG002
        self.messages = messages
        return LLMResponse(content="done")


class _LifecycleDispatcher:
    def __init__(
        self,
        outputs: list[dict] | None = None,
        *,
        event_outputs: dict[str, list[dict]] | None = None,
    ) -> None:
        self.contexts: list[LifecycleHookEventContext] = []
        self.event_outputs = {
            "UserPromptSubmit": list(outputs or []),
            **{key: list(value) for key, value in dict(event_outputs or {}).items()},
        }

    def dispatch(
        self,
        context: LifecycleHookEventContext,
    ) -> list[LifecycleHookDispatchResult]:
        self.contexts.append(context)
        outputs = self.event_outputs.get(context.event_name, [])
        if not outputs:
            return []
        return [
            LifecycleHookDispatchResult(
                LifecycleHookDeclaration.from_dict(
                    f"hook:admin_managed:test:{index}",
                    {
                        "event": context.event_name,
                        "source": "admin_managed",
                        "placement": "server",
                        "handler_type": "prompt",
                        "display_name": "Test prompt hook",
                        "summary": "Test prompt hook.",
                        "permissions": [],
                        "trust": "trusted",
                    },
                ),
                LifecycleHookOutput.from_dict(output),
            )
            for index, output in enumerate(outputs)
        ]


class _VisibleTool:
    name = "read_file"
    description = "Read a file"
    parameters = {}
    tool_source = "builtin"

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": {}}

    def execute(self, **kwargs) -> str:  # noqa: ARG002
        return "read ok"

    def preflight_validate(self, **kwargs) -> str | None:  # noqa: ARG002
        return None


class _ApprovalProvider:
    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.requests = []

    def request_approval(self, request):
        self.requests.append(request)
        if self.approved:
            return ApprovalDecision.allow_once("approved")
        return ApprovalDecision.deny_once("denied")


def _project_agent_events_to_session_document(events: list) -> dict | None:
    document = None
    for seq, event in enumerate(events, start=1):
        event_type = event.event_type.value
        if event_type == "session_run_start":
            payload = {"prompt": event.data.get("user_input") or ""}
        elif event_type == "error":
            payload = {"message": event.error_message or event.data.get("message") or ""}
        elif event_type == "session_run_end":
            payload = dict(event.data)
        elif event_type in {
            "lifecycle_hook",
            "tool_call_start",
            "tool_call_end",
            "usage_update",
        }:
            payload = dict(event.data)
        else:
            continue
        document = apply_session_event(
            document,
            session_id="session-1",
            event_type=event_type,
            payload=payload,
            session_event_seq=seq,
            session_run_id="run-1",
            session_run_seq=seq,
        )
    return document


def test_agent_chat_dispatches_user_prompt_submit_and_stop_lifecycle_events() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher()
    agent = Agent(
        llm=llm,
        tools=[],
        lifecycle_dispatcher=dispatcher,
    )
    setattr(agent, "current_session_id", "session-1")
    setattr(agent, "permission_trigger_source", "chat")

    result = agent.chat("install this skill")

    assert result == "done"
    assert [context.event_name for context in dispatcher.contexts] == [
        "UserPromptSubmit",
        "Stop",
    ]
    submit_context = dispatcher.contexts[0]
    assert submit_context.session_run_id == "session-1"
    assert submit_context.payload["user_input"] == "install this skill"
    assert llm.messages[1]["content"] == "install this skill"

    stop_context = dispatcher.contexts[1]
    assert stop_context.payload["result"] == "done"
    assert stop_context.payload["interrupted"] is False


def test_agent_chat_lifecycle_overflow_runtime_artifacts_reach_event_side_channel() -> None:
    huge = "OVERSIZED_LIFECYCLE_OUTPUT_SECRET" * 500
    declaration = LifecycleHookDeclaration.from_dict(
        "hook:oversized",
        {
            "event": "UserPromptSubmit",
            "source": "admin_managed",
            "placement": "server",
            "handler_type": "prompt",
            "display_name": "Oversized guard",
            "summary": "Blocks oversized prompt output.",
            "permissions": [],
            "trust": "trusted",
        },
    )
    dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([declaration]),
        runtime_adapters=LifecycleHookRuntimeAdapterRegistry(
            [
                FunctionLifecycleHookRuntimeAdapter(
                    "prompt",
                    lambda _declaration, _context: LifecycleHookOutput.from_dict(
                        {
                            "decision": "deny",
                            "continue_flow": False,
                            "reason": huge,
                        }
                    ),
                )
            ]
        ),
    )
    agent = Agent(
        llm=_LLM(),
        tools=[],
        lifecycle_dispatcher=dispatcher,
    )
    events = []
    agent.add_event_handler(events.append)

    agent.chat("install this skill")

    lifecycle_event = next(
        event
        for event in events
        if event.event_type.value == "lifecycle_hook"
        and event.data.get("phase") == "result"
    )
    event_json = json.dumps(lifecycle_event.data, ensure_ascii=False, sort_keys=True)
    assert huge not in event_json
    assert lifecycle_event.runtime_artifacts == [
        {
            "artifact_id": "lifecycle-output-overflow:hook-oversized:1",
            "type": "log",
            "status": "generated",
            "content": huge,
            "metadata": {
                "kind": "lifecycle_output_overflow",
                "hook_id": "hook:oversized",
                "source": "admin_managed",
                "handler_type": "prompt",
                "field": "reason",
                "original_chars": len(huge),
                "original_bytes": len(huge.encode("utf-8", errors="replace")),
                "preview": huge[:256],
                "event_name": "UserPromptSubmit",
                "session_run_id": "",
                "agent_run_id": "",
                "turn_id": "",
                "placement": "server",
                "trigger_source": "chat",
            },
        }
    ]


def test_agent_chat_lifecycle_context_populates_authoritative_fields() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher()
    agent = Agent(
        llm=llm,
        tools=[],
        lifecycle_dispatcher=dispatcher,
    )
    setattr(agent, "current_session_id", "session-1")
    setattr(agent, "runtime_agent_run_id", "agent-run-1")
    setattr(agent, "runtime_turn_id", "turn-1")
    setattr(agent, "permission_trigger_source", "settings")
    setattr(agent, "locale", "zh-CN")

    result = agent.chat("install this skill")

    assert result == "done"
    for context in dispatcher.contexts:
        assert context.session_run_id == "session-1"
        assert context.agent_run_id == "agent-run-1"
        assert context.turn_id == "turn-1"
        assert context.source == "settings"
        assert context.origin == "agent"
        assert context.locale == "zh-CN"
        assert context.placement == "server"
        assert context.timestamp
        assert context.metadata["round_index"] == 0
        assert context.payload["session_run_id"] == "session-1"
        assert context.payload["agent_run_id"] == "agent-run-1"
        assert context.payload["turn_id"] == "turn-1"
        assert context.payload["trigger_source"] == "settings"
        assert context.payload["placement"] == "server"
        assert context.payload["timestamp"] == context.timestamp


def test_agent_chat_without_tool_call_does_not_dispatch_permission_request_lifecycle() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher()
    agent = Agent(
        llm=llm,
        tools=[_VisibleTool()],
        lifecycle_dispatcher=dispatcher,
    )

    result = agent.chat("hello")

    assert result == "done"
    assert [context.event_name for context in dispatcher.contexts] == [
        "UserPromptSubmit",
        "Stop",
    ]


def test_agent_chat_max_turns_budget_projects_budget_exceeded_terminal_state() -> None:
    class _ToolCallingLLM:
        model = "test-model"
        max_tokens = None

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, **kwargs):  # noqa: ARG002
            self.calls += 1
            return LLMResponse(
                tool_calls=[
                    ToolCall(id="call-1", name="read_file", arguments={}),
                ],
            )

    llm = _ToolCallingLLM()
    dispatcher = _LifecycleDispatcher()
    agent = Agent(
        llm=llm,
        tools=[_VisibleTool()],
        max_rounds=1,
        lifecycle_dispatcher=dispatcher,
    )
    agent.runtime_budget = {"max_turns": 1}
    events = []
    agent.add_event_handler(events.append)

    result = agent.chat("inspect the repo")

    document = _project_agent_events_to_session_document(events)

    assert result == "(AgentRun budget exceeded: max_turns=1)"
    assert llm.calls == 1
    assert dispatcher.contexts[0].event_name == "UserPromptSubmit"
    assert dispatcher.contexts[-1].event_name == "Stop"
    stop_context = dispatcher.contexts[-1]
    assert stop_context.payload["termination_reason"] == "budget_exceeded"
    assert stop_context.payload["budget"] == {
        "field": "max_turns",
        "limit": 1,
        "message": "AgentRun budget exceeded: max_turns=1",
    }
    assert events[-1].event_type.value == "session_run_end"
    assert events[-1].data["status"] == "budget_exceeded"
    assert events[-1].data["session_state"] == "budget_exceeded"
    assert events[-1].data["error"] == "AgentRun budget exceeded: max_turns=1"
    assert document["run_state"]["status"] == "budget_exceeded"
    assert document["stats"]["runStatus"] == "budget_exceeded"
    assert document["session"]["state"] == "budget_exceeded"
    assert document["run_state"]["error"] == "AgentRun budget exceeded: max_turns=1"


def test_agent_chat_token_budget_projects_budget_exceeded_terminal_state() -> None:
    class _TokenBudgetLLM:
        model = "test-model"
        max_tokens = None

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, **kwargs):  # noqa: ARG002
            self.calls += 1
            return LLMResponse(
                content="model output should not become a completed answer",
                prompt_tokens=6,
                completion_tokens=5,
            )

    llm = _TokenBudgetLLM()
    dispatcher = _LifecycleDispatcher()
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=dispatcher)
    agent.runtime_budget = {"token_budget": 10}
    events = []
    agent.add_event_handler(events.append)

    result = agent.chat("inspect the repo")
    document = _project_agent_events_to_session_document(events)

    assert result == "(AgentRun budget exceeded: token_budget=10)"
    assert llm.calls == 1
    assert dispatcher.contexts[-1].event_name == "Stop"
    stop_context = dispatcher.contexts[-1]
    assert stop_context.payload["termination_reason"] == "budget_exceeded"
    assert stop_context.payload["budget"] == {
        "field": "token_budget",
        "limit": 10,
        "message": "AgentRun budget exceeded: token_budget=10",
    }
    assert events[-1].data["status"] == "budget_exceeded"
    assert document["run_state"]["status"] == "budget_exceeded"
    assert document["run_state"]["error"] == "AgentRun budget exceeded: token_budget=10"


def test_agent_chat_timeout_budget_projects_budget_exceeded_without_model_call() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher()
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=dispatcher)
    agent.runtime_budget = {"timeout_sec": 1}
    agent.runtime_deadline = 0
    events = []
    agent.add_event_handler(events.append)

    result = agent.chat("inspect the repo")
    document = _project_agent_events_to_session_document(events)

    assert result == "(AgentRun budget exceeded: timeout_sec=1)"
    assert llm.messages == []
    assert dispatcher.contexts[-1].event_name == "Stop"
    stop_context = dispatcher.contexts[-1]
    assert stop_context.payload["termination_reason"] == "budget_exceeded"
    assert stop_context.payload["budget"] == {
        "field": "timeout_sec",
        "limit": 1,
        "message": "AgentRun budget exceeded: timeout_sec=1",
    }
    assert events[-1].data["status"] == "budget_exceeded"
    assert document["run_state"]["status"] == "budget_exceeded"
    assert document["run_state"]["error"] == "AgentRun budget exceeded: timeout_sec=1"


def test_agent_chat_projects_stop_lifecycle_terminal_message_and_artifacts() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher(
        event_outputs={
            "Stop": [
                {
                    "user_message": "Final answer passed lifecycle review.",
                    "artifacts": [{"kind": "review", "id": "artifact-1"}],
                }
            ]
        }
    )
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=dispatcher)
    events = []
    agent.add_event_handler(events.append)

    result = agent.chat("hello")

    assert result == "done"
    stop_result = [
        event
        for event in events
        if event.event_type.value == "lifecycle_hook"
        and event.data["event_name"] == "Stop"
        and event.data["phase"] == "result"
    ][0]
    assert stop_result.data["message"] == "Final answer passed lifecycle review."
    assert stop_result.data["artifacts"] == [{"kind": "review", "id": "artifact-1"}]
    assert not [event for event in events if event.event_type.value == "error"]


def test_agent_chat_diagnoses_ignored_stop_lifecycle_control_fields() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher(
        event_outputs={
            "Stop": [
                {
                    "continue_flow": False,
                    "decision": "deny",
                    "updated_input": {"user_input": "must not replace result"},
                    "additional_context": [
                        {"role": "system", "content": "must not re-enter model"}
                    ],
                    "user_message": "Final review recorded.",
                    "artifacts": [{"kind": "review", "id": "artifact-1"}],
                }
            ]
        }
    )
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=dispatcher)
    events = []
    agent.add_event_handler(events.append)

    result = agent.chat("hello")

    assert result == "done"
    assert agent.state.messages[-1]["content"] == "done"
    stop_result = [
        event
        for event in events
        if event.event_type.value == "lifecycle_hook"
        and event.data["event_name"] == "Stop"
        and event.data["phase"] == "result"
    ][0]
    ignored_fields = {
        item["field"]
        for item in stop_result.data["diagnostics"]
        if item.get("code") == "lifecycle_output_field_ignored"
    }
    assert ignored_fields == {
        "continue_flow",
        "decision",
        "additional_context",
        "updated_input",
    }
    assert stop_result.data["message"] == "Final review recorded."
    assert stop_result.data["artifacts"] == [{"kind": "review", "id": "artifact-1"}]
    assert not [event for event in events if event.event_type.value == "error"]


def test_agent_chat_uses_updated_prompt_for_session_start_and_llm() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher([
        {"updated_input": {"user_input": "rewritten prompt"}}
    ])
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=dispatcher)
    events = []
    agent.add_event_handler(events.append)

    result = agent.chat("original prompt")

    assert result == "done"
    assert events[0].data["user_input"] == "rewritten prompt"
    assert agent.state.messages[0]["content"] == "rewritten prompt"
    assert llm.messages[1]["content"] == "rewritten prompt"


def test_agent_chat_injects_allowed_additional_context_before_user_prompt() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher([
        {
            "additional_context": [
                {"role": "system", "content": "Use staging only."},
                {"role": "user", "content": "Treat this as context, not user input."},
                "Prefer read-only commands.",
            ]
        }
    ])
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=dispatcher)

    result = agent.chat("deploy")

    assert result == "done"
    assert [
        {key: message.get(key) for key in ("role", "content")}
        for message in agent.state.messages
    ] == [
        {"role": "system", "content": "Use staging only."},
        {
            "role": "system",
            "content": (
                "Lifecycle additional context:\n"
                "Treat this as context, not user input.\n"
                "Prefer read-only commands."
            ),
        },
        {"role": "user", "content": "deploy"},
        {"role": "assistant", "content": "done"},
    ]
    assert llm.messages[1]["content"] == "Use staging only."
    assert llm.messages[3]["content"] == "deploy"


def test_agent_chat_blocks_user_prompt_submit_deny_without_calling_llm() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher([
        {
            "decision": "deny",
            "continue_flow": False,
            "user_message": "Blocked by prompt policy.",
        }
    ])
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=dispatcher)
    events = []
    agent.add_event_handler(events.append)

    result = agent.chat("install risky package")

    assert result == "Blocked by prompt policy."
    assert llm.messages == []
    assert agent.state.messages == []
    event_types = [event.event_type.value for event in events]
    assert event_types[0] == "session_run_start"
    assert event_types[1:3] == ["lifecycle_hook", "lifecycle_hook"]
    assert event_types[-3:] == ["error", "usage_update", "session_run_end"]
    assert events[1].data["phase"] == "dispatch_start"
    assert events[2].data["phase"] == "result"
    assert events[2].data["decision"] == "deny"


def test_agent_chat_user_prompt_submit_deny_remains_blocked_in_session_document() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher([
        {
            "decision": "deny",
            "continue_flow": False,
            "user_message": "Blocked by prompt policy.",
        }
    ])
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=dispatcher)
    events = []
    agent.add_event_handler(events.append)

    result = agent.chat("install risky package")

    document = None
    for seq, event in enumerate(events, start=1):
        event_type = event.event_type.value
        if event_type == "session_run_start":
            payload = {"prompt": event.data.get("user_input") or ""}
        elif event_type == "error":
            payload = {"message": event.error_message or event.data.get("message") or ""}
        elif event_type == "session_run_end":
            payload = dict(event.data)
        elif event_type in {"lifecycle_hook", "usage_update"}:
            payload = dict(event.data)
        else:
            continue
        document = apply_session_event(
            document,
            session_id="session-1",
            event_type=event_type,
            payload=payload,
            session_event_seq=seq,
            session_run_id="run-1",
            session_run_seq=seq,
        )

    assert result == "Blocked by prompt policy."
    assert document["run_state"]["status"] == "blocked"
    assert document["run_state"]["error"] == "Blocked by prompt policy."
    assert document["session"]["state"] == "blocked"


def test_agent_chat_routes_user_prompt_submit_ask_through_approval_provider() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher([
        {
            "decision": "ask",
            "user_message": "Review prompt before continuing.",
        }
    ])
    approval = _ApprovalProvider(approved=False)
    agent = Agent(
        llm=llm,
        tools=[],
        lifecycle_dispatcher=dispatcher,
        approval_provider=approval,
    )

    result = agent.chat("install linked skill")

    assert result == "denied"
    assert llm.messages == []
    assert agent.state.messages == []
    assert approval.requests[0].tool_source == "lifecycle_hook"
    assert approval.requests[0].metadata["lifecycle_event"] == "UserPromptSubmit"


def test_agent_chat_continues_user_prompt_submit_after_approval() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher([
        {
            "decision": "ask",
            "updated_input": {"user_input": "approved prompt"},
            "user_message": "Review prompt before continuing.",
        }
    ])
    approval = _ApprovalProvider(approved=True)
    agent = Agent(
        llm=llm,
        tools=[],
        lifecycle_dispatcher=dispatcher,
        approval_provider=approval,
    )

    result = agent.chat("install linked skill")

    assert result == "done"
    assert approval.requests[0].tool_args == {"user_input": "approved prompt"}
    assert llm.messages[1]["content"] == "approved prompt"


def test_agent_chat_blocks_user_prompt_submit_continue_flow_false() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher([
        {
            "continue_flow": False,
            "user_message": "Prompt hook stopped the turn.",
        }
    ])
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=dispatcher)

    result = agent.chat("stop before model")

    assert result == "Prompt hook stopped the turn."
    assert llm.messages == []
    assert agent.state.messages == []


def test_agent_chat_blocks_user_prompt_submit_defer_without_persisting_prompt() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher([
        {
            "decision": "defer",
            "user_message": "Prompt hook deferred the turn.",
        }
    ])
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=dispatcher)

    result = agent.chat("defer before model")

    assert result == "Prompt hook deferred the turn."
    assert llm.messages == []
    assert agent.state.messages == []


def test_agent_chat_does_not_persist_additional_context_when_prompt_blocked() -> None:
    llm = _LLM()
    dispatcher = _LifecycleDispatcher([
        {
            "decision": "deny",
            "user_message": "Blocked with context.",
            "additional_context": [
                {"role": "system", "content": "This must stay out of model history."}
            ],
        }
    ])
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=dispatcher)

    result = agent.chat("blocked context")

    assert result == "Blocked with context."
    assert llm.messages == []
    assert agent.state.messages == []


def test_agent_chat_fails_closed_when_user_prompt_submit_dispatch_raises() -> None:
    class _FailingDispatcher:
        def dispatch(self, _context):
            raise RuntimeError("dispatch unavailable")

    llm = _LLM()
    agent = Agent(llm=llm, tools=[], lifecycle_dispatcher=_FailingDispatcher())

    result = agent.chat("do not continue")

    assert result == "UserPromptSubmit lifecycle dispatch failed"
    assert llm.messages == []
    assert agent.state.messages == []


def test_agent_chat_dispatches_stop_failure_lifecycle_when_run_raises() -> None:
    class _FailingLoop:
        def run(self) -> str:
            raise RuntimeError("boom")

    dispatcher = _LifecycleDispatcher()
    agent = Agent(
        llm=_LLM(),
        tools=[],
        loop=_FailingLoop(),
        lifecycle_dispatcher=dispatcher,
    )
    setattr(agent, "current_session_id", "session-1")
    setattr(agent, "runtime_agent_run_id", "agent-run-1")
    setattr(agent, "runtime_turn_id", "turn-1")
    setattr(agent, "permission_trigger_source", "taskflow")
    setattr(agent, "locale", "zh-CN")

    with pytest.raises(RuntimeError, match="boom"):
        agent.chat("hello")

    assert [context.event_name for context in dispatcher.contexts] == [
        "UserPromptSubmit",
        "StopFailure",
    ]
    failure_context = dispatcher.contexts[1]
    assert failure_context.session_run_id == "session-1"
    assert failure_context.agent_run_id == "agent-run-1"
    assert failure_context.turn_id == "turn-1"
    assert failure_context.source == "taskflow"
    assert failure_context.origin == "agent"
    assert failure_context.locale == "zh-CN"
    assert failure_context.placement == "server"
    assert failure_context.timestamp
    assert failure_context.metadata["round_index"] == 0
    assert failure_context.payload["session_run_id"] == "session-1"
    assert failure_context.payload["agent_run_id"] == "agent-run-1"
    assert failure_context.payload["turn_id"] == "turn-1"
    assert failure_context.payload["trigger_source"] == "taskflow"
    assert failure_context.payload["timestamp"] == failure_context.timestamp
    assert failure_context.payload["error"]["type"] == "RuntimeError"
    assert failure_context.payload["error"]["message"] == "boom"


def test_agent_chat_emits_stop_failure_lifecycle_recovery_message() -> None:
    class _FailingLoop:
        def run(self) -> str:
            raise RuntimeError("boom")

    dispatcher = _LifecycleDispatcher(
        event_outputs={
            "StopFailure": [
                {
                    "user_message": "Lifecycle recovery: retry after reconnecting.",
                    "artifacts": [{"kind": "failure_report", "id": "failure-1"}],
                }
            ]
        }
    )
    agent = Agent(
        llm=_LLM(),
        tools=[],
        loop=_FailingLoop(),
        lifecycle_dispatcher=dispatcher,
    )
    events = []
    agent.add_event_handler(events.append)

    with pytest.raises(RuntimeError, match="boom"):
        agent.chat("hello")

    failure_result = [
        event
        for event in events
        if event.event_type.value == "lifecycle_hook"
        and event.data["event_name"] == "StopFailure"
        and event.data["phase"] == "result"
    ][0]
    assert failure_result.data["message"] == "Lifecycle recovery: retry after reconnecting."
    assert failure_result.data["artifacts"] == [
        {"kind": "failure_report", "id": "failure-1"}
    ]
    assert [
        event.error_message
        for event in events
        if event.event_type.value == "error"
    ] == ["Lifecycle recovery: retry after reconnecting."]


def test_agent_context_manager_dispatches_pre_compact_through_bound_lifecycle() -> None:
    dispatcher = _LifecycleDispatcher(
        event_outputs={
            "PreCompact": [
                {
                    "decision": "deny",
                    "continue_flow": False,
                    "user_message": "Keep the full context.",
                }
            ]
        }
    )
    agent = Agent(
        llm=_LLM(),
        tools=[],
        lifecycle_dispatcher=dispatcher,
        max_context_tokens=1000,
    )
    agent.context._collapse_at = 1
    messages = [{"role": "user", "content": "x" * 100} for _ in range(8)]

    assert agent.context.maybe_compress(messages, llm=None) is False

    pre_compact_contexts = [
        context for context in dispatcher.contexts if context.event_name == "PreCompact"
    ]
    assert pre_compact_contexts
    assert pre_compact_contexts[0].payload["before_message_count"] == 8

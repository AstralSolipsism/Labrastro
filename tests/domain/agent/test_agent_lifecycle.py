from __future__ import annotations

from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.approval import ApprovalDecision
from reuleauxcoder.domain.hooks.lifecycle import (
    LifecycleHookDeclaration,
    LifecycleHookDispatchResult,
    LifecycleHookEventContext,
    LifecycleHookOutput,
)
from reuleauxcoder.domain.llm.models import LLMResponse


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


class _ApprovalProvider:
    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.requests = []

    def request_approval(self, request):
        self.requests.append(request)
        if self.approved:
            return ApprovalDecision.allow_once("approved")
        return ApprovalDecision.deny_once("denied")


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

    with pytest.raises(RuntimeError, match="boom"):
        agent.chat("hello")

    assert [context.event_name for context in dispatcher.contexts] == [
        "UserPromptSubmit",
        "StopFailure",
    ]
    failure_context = dispatcher.contexts[1]
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

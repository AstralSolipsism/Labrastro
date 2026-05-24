import json

import pytest

from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext, HookPoint
from reuleauxcoder.domain.hooks.builtin.project_context import ProjectContextHook
from reuleauxcoder.domain.llm.models import (
    EMPTY_ASSISTANT_CONTENT_PLACEHOLDER,
    LLMResponse,
    ToolCall,
)
from reuleauxcoder.interfaces.events import UIEventBus, UIEventLevel
from reuleauxcoder.services.llm.client import LLM, _merge_hook_request_overrides
from reuleauxcoder.services.llm.sanitizer import sanitize_messages_for_llm


def test_llm_response_message_uses_placeholder_for_reasoning_only_assistant() -> None:
    response = LLMResponse(reasoning_content="thinking")

    assert response.message["content"] == EMPTY_ASSISTANT_CONTENT_PLACEHOLDER
    assert response.message["reasoning_content"] == "thinking"


def test_llm_response_message_keeps_null_content_for_tool_call_assistant() -> None:
    response = LLMResponse(
        tool_calls=[ToolCall(id="tool_1", name="glob", arguments={"pattern": "*.py"})]
    )

    assert response.message["content"] is None
    assert response.message["tool_calls"][0]["function"]["name"] == "glob"


def test_sanitize_messages_repairs_empty_non_tool_assistant_content() -> None:
    sanitized = sanitize_messages_for_llm(
        [{"role": "assistant", "content": "", "reasoning_content": "thinking"}],
        preserve_reasoning_content=True,
    )

    assert sanitized[0]["content"] == EMPTY_ASSISTANT_CONTENT_PLACEHOLDER


def test_sanitize_messages_backfills_reasoning_content_for_assistant_tool_calls() -> (
    None
):
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=True,
    )

    assert sanitized[0]["reasoning_content"] == "[PLACE_HOLDER]"


def test_sanitize_messages_does_not_backfill_when_disabled() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=False,
    )

    assert "reasoning_content" not in sanitized[0]


def test_sanitize_messages_drops_reasoning_for_non_tool_assistant_by_default() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "done",
            "reasoning_content": "private thoughts",
        }
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
    )

    assert "reasoning_content" not in sanitized[0]


def test_sanitize_messages_keeps_reasoning_for_tool_assistant_when_present() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "need to inspect files first",
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        require_reasoning_content_for_tool_calls=True,
    )

    assert sanitized[0]["reasoning_content"] == "need to inspect files first"


def test_sanitize_messages_does_not_require_tool_reasoning_without_replay_mode() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        reasoning_replay_mode="none",
    )

    assert "reasoning_content" not in sanitized[0]


def test_sanitize_messages_replays_tool_reasoning_by_mode() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        reasoning_replay_mode="tool_calls",
    )

    assert sanitized[0]["reasoning_content"] == "[PLACE_HOLDER]"


def test_sanitize_messages_replays_all_assistant_reasoning_within_tool_turn() -> None:
    messages = [
        {"role": "user", "content": "question 1"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "need tool first",
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
        {
            "role": "assistant",
            "content": "final answer",
            "reasoning_content": "now I can answer",
        },
        {"role": "user", "content": "question 2"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        reasoning_replay_mode="tool_calls",
    )

    assert sanitized[1]["reasoning_content"] == "need tool first"
    assert sanitized[3]["reasoning_content"] == "now I can answer"


def test_sanitize_messages_does_not_replay_non_tool_turn_reasoning_by_mode() -> None:
    messages = [
        {"role": "user", "content": "question 1"},
        {
            "role": "assistant",
            "content": "plain answer",
            "reasoning_content": "private thoughts",
        },
        {"role": "user", "content": "question 2"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        reasoning_replay_mode="tool_calls",
    )

    assert "reasoning_content" not in sanitized[1]


def test_sanitize_messages_backfills_missing_reasoning_for_non_tool_assistant_in_tool_turn() -> (
    None
):
    """Non-tool-call assistant lacking reasoning_content in a tool-call turn
    (e.g. injected delegated-run status notes) must receive a placeholder when
    reasoning_replay_mode='tool_calls'."""
    messages = [
        {"role": "user", "content": "delegate work"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "I need background jobs",
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "delegate_agent", "arguments": '{"agent_id":"researcher","task":"..."}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "started jobs"},
        {
            "role": "assistant",
            "content": "[Delegated AgentRun submitted]...",
            # No reasoning_content — injected by system
        },
        {"role": "user", "content": "are they done?"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        reasoning_replay_mode="tool_calls",
    )

    # Tool-call assistant keeps its original reasoning
    assert sanitized[1]["reasoning_content"] == "I need background jobs"
    # Injected non-tool-call assistant gets backfilled
    assert sanitized[3]["reasoning_content"] == "[PLACE_HOLDER]"


def test_sanitize_messages_fallbacks_empty_reasoning_for_tool_assistant_when_required() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        require_reasoning_content_for_tool_calls=True,
    )

    assert sanitized[0]["reasoning_content"] == "[PLACE_HOLDER]"


def test_sanitize_messages_strips_reasoning_content_when_preserve_disabled() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "hidden",
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=False,
        backfill_reasoning_content_for_tool_calls=True,
    )

    assert "reasoning_content" not in sanitized[0]


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeDelta:
    def __init__(
        self, content: str = "", reasoning_content: str | None = None, tool_calls=None
    ):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, delta):
        self.delta = delta


class _FakeChunk:
    def __init__(
        self,
        *,
        content: str = "",
        reasoning_content: str | None = None,
        tool_calls=None,
        usage=None,
    ):
        self.usage = usage
        self.choices = [
            _FakeChoice(
                _FakeDelta(
                    content=content,
                    reasoning_content=reasoning_content,
                    tool_calls=tool_calls,
                )
            )
        ]


def _set_fake_call_with_retry(llm: LLM, func):
    assert llm._provider is not None

    def _wrapped(params, **_kwargs):
        return func(params)

    llm._provider.call_with_retry = _wrapped


def test_llm_chat_applies_project_context_hook_to_provider_request(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENT.md").write_text(
        "Use the Labrastro project context marker: hook-injected-context.",
        encoding="utf-8",
    )
    captured = {}
    registry = HookRegistry()
    registry.register(HookPoint.BEFORE_LLM_REQUEST, ProjectContextHook())

    llm = LLM(model="demo-model", api_key="sk-test-12345678")

    def _fake_call_with_retry(params):
        captured.update(params)
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=1, completion_tokens=1)),
            ]
        )

    _set_fake_call_with_retry(llm, _fake_call_with_retry)

    response = llm.chat(
        [{"role": "user", "content": "Hi"}],
        hook_registry=registry,
    )

    assert response.content == "Hello"
    assert any(
        "hook-injected-context" in str(message.get("content") or "")
        for message in captured["messages"]
    )


def test_llm_can_initialize_without_api_key_and_fails_on_chat() -> None:
    llm = LLM(model="demo-model", api_key="")

    assert llm.client is None
    with pytest.raises(RuntimeError, match="No model provider API key is configured"):
        llm.chat([{"role": "user", "content": "Hi"}])


def test_hook_request_override_merge_protects_provider_payload_keys() -> None:
    openai_merged = _merge_hook_request_overrides(
        "openai_chat",
        {
            "messages": [{"role": "system", "content": "rebuilt"}],
            "tools": [{"function": {"name": "rebuilt_tool"}}],
            "temperature": 0.0,
        },
        {
            "messages": [{"role": "user", "content": "stale"}],
            "tools": [{"function": {"name": "stale_tool"}}],
            "temperature": 0.5,
        },
        {
            "messages": [{"role": "user", "content": "stale"}],
            "tools": [{"function": {"name": "stale_tool"}}],
            "temperature": 0.0,
        },
    )
    assert openai_merged["messages"][0]["content"] == "rebuilt"
    assert openai_merged["tools"][0]["function"]["name"] == "rebuilt_tool"
    assert openai_merged["temperature"] == 0.5

    anthropic_merged = _merge_hook_request_overrides(
        "anthropic_messages",
        {"system": "rebuilt", "messages": ["rebuilt"], "tools": ["rebuilt"]},
        {"system": "stale", "messages": ["stale"], "tools": ["stale"]},
        {"system": "stale", "messages": ["stale"], "tools": ["stale"]},
    )
    assert anthropic_merged == {
        "system": "rebuilt",
        "messages": ["rebuilt"],
        "tools": ["rebuilt"],
    }

    responses_merged = _merge_hook_request_overrides(
        "openai_responses",
        {"input": ["rebuilt"], "tools": ["rebuilt"], "store": False},
        {"input": ["stale"], "tools": ["stale"], "store": True},
        {"input": ["stale"], "tools": ["stale"], "store": False},
    )
    assert responses_merged == {
        "input": ["rebuilt"],
        "tools": ["rebuilt"],
        "store": True,
    }


def test_llm_chat_sends_explicit_thinking_enabled_state() -> None:
    captured = {}

    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        thinking_enabled=True,
    )

    def _fake_call_with_retry(params):
        captured.update(params)
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=1, completion_tokens=1)),
            ]
        )

    _set_fake_call_with_retry(llm, _fake_call_with_retry)
    response = llm.chat([{"role": "user", "content": "Hi"}])

    assert response.content == "Hello"
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}


def test_llm_chat_sends_explicit_thinking_disabled_state() -> None:
    captured = {}

    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        thinking_enabled=False,
    )

    def _fake_call_with_retry(params):
        captured.update(params)
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=1, completion_tokens=1)),
            ]
        )

    _set_fake_call_with_retry(llm, _fake_call_with_retry)
    response = llm.chat([{"role": "user", "content": "Hi"}])

    assert response.content == "Hello"
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_llm_chat_omits_thinking_state_when_unset() -> None:
    captured = {}

    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        thinking_enabled=None,
    )

    def _fake_call_with_retry(params):
        captured.update(params)
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=1, completion_tokens=1)),
            ]
        )

    _set_fake_call_with_retry(llm, _fake_call_with_retry)
    response = llm.chat([{"role": "user", "content": "Hi"}])

    assert response.content == "Hello"
    assert "extra_body" not in captured


def test_llm_debug_trace_persists_trace_and_emits_ui_event(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ui_bus = UIEventBus()
    seen = []
    ui_bus.subscribe(seen.append, replay_history=False)

    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        base_url="https://example.com/v1",
        debug_trace=True,
        ui_bus=ui_bus,
    )

    def _fake_call_with_retry(params):
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(reasoning_content="thinking"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=12, completion_tokens=3)),
            ]
        )

    _set_fake_call_with_retry(llm, _fake_call_with_retry)
    reasoning_tokens: list[str] = []
    response = llm.chat(
        [{"role": "user", "content": "Hi"}],
        on_reasoning_token=reasoning_tokens.append,
        session_id="session_test",
        trace_id="trace_1",
    )

    assert response.content == "Hello"
    assert reasoning_tokens == ["thinking"]
    debug_events = [event for event in seen if event.level == UIEventLevel.DEBUG]
    assert debug_events
    trace_path = debug_events[-1].data.get("trace_path")
    assert trace_path

    payload = json.loads(open(trace_path, encoding="utf-8").read())
    assert payload["model"] == "demo-model"
    assert payload["request"]["stream"] is True
    assert payload["request"]["reasoning_effort"] is None
    assert payload["request"]["reasoning_replay_mode"] == "none"
    assert payload["request"]["thinking_enabled"] is None
    assert payload["request"]["thinking_type"] is None
    assert payload["request"]["preserve_reasoning_content"] is True
    assert payload["stream"]["event_count"] >= 2
    assert payload["stream"]["reasoning_chunks"] == 1
    assert payload["stream"]["events"][0]["type"] == "content"
    assert payload["stream"]["events"][0]["text"] == "Hello"
    assert payload["stream"]["raw_chunk_count"] == 0
    assert payload["stream"]["raw_chunks"] == []
    assert payload["response"]["reasoning_received"] is True
    assert payload["response"]["reasoning_chars"] == len("thinking")
    assert payload["response"]["usage"]["prompt_tokens"] == 12
    assert payload["response"]["usage"]["completion_tokens"] == 3


def test_llm_debug_trace_can_record_raw_stream_chunks(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ui_bus = UIEventBus()
    seen = []
    ui_bus.subscribe(seen.append, replay_history=False)

    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        base_url="https://example.com/v1",
        debug_trace=True,
        debug_raw_chunks=True,
        ui_bus=ui_bus,
    )

    def _fake_call_with_retry(params):
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=12, completion_tokens=3)),
            ]
        )

    _set_fake_call_with_retry(llm, _fake_call_with_retry)
    response = llm.chat(
        [{"role": "user", "content": "Hi"}],
        session_id="session_test",
        trace_id="trace_raw",
    )

    assert response.content == "Hello"
    debug_events = [event for event in seen if event.level == UIEventLevel.DEBUG]
    trace_path = debug_events[-1].data.get("trace_path")
    payload = json.loads(open(trace_path, encoding="utf-8").read())
    assert payload["stream"]["raw_chunk_count"] == 2
    assert payload["stream"]["raw_chunks"][0]["_chunk_index"] == 0
    assert payload["stream"]["raw_chunks"][0]["choices"][0]["delta"]["content"] == "Hello"


def test_llm_chat_persists_provider_error_diagnostic_path(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        base_url="https://example.com/v1",
    )

    def _fake_call_with_retry(params):
        error = RuntimeError("Connection error.")
        setattr(error, "provider_error_phase", "request_start")
        setattr(error, "provider_retry_attempts", [{"attempt": 1, "action": "raise"}])
        setattr(error, "provider_request_params", dict(params))
        raise error

    _set_fake_call_with_retry(llm, _fake_call_with_retry)

    with pytest.raises(RuntimeError) as exc_info:
        llm.chat([{"role": "user", "content": "Hi"}], session_id="session_test")

    diagnostic_path = getattr(exc_info.value, "llm_diagnostic_path", "")
    assert diagnostic_path
    payload = json.loads(open(diagnostic_path, encoding="utf-8").read())
    assert payload["provider"]["id"] == "direct-openai-chat"
    assert payload["provider_error"]["phase"] == "request_start"
    assert payload["provider_error"]["retry_attempts"] == [
        {"attempt": 1, "action": "raise"}
    ]
    assert payload["request"]["stream"] is True


def test_llm_chat_continues_after_partial_stream_interruption(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    llm = LLM(model="demo-model", api_key="sk-test-12345678")
    calls: list[dict] = []
    tokens: list[str] = []

    class BrokenAfterContent:
        def __iter__(self):
            yield _FakeChunk(content="Hello ")
            raise RuntimeError("incomplete chunked read")

    def _fake_call_with_retry(params):
        calls.append(dict(params))
        if len(calls) == 1:
            return BrokenAfterContent()
        return iter(
            [
                _FakeChunk(content="world"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=2, completion_tokens=1)),
            ]
        )

    _set_fake_call_with_retry(llm, _fake_call_with_retry)

    response = llm.chat(
        [{"role": "user", "content": "Hi"}],
        on_token=tokens.append,
        session_id="session_recovery",
    )

    assert response.content == "Hello world"
    assert response.stream_status == "completed"
    assert response.interruption["classification"] == "text_interrupted"
    assert response.recovery["attempted"] is True
    assert tokens == ["Hello ", "world"]
    assert calls[1]["messages"][-1]["content"].startswith("<stream_recovery>")


def test_llm_flat_fallback_provider_preserves_stream_recovery_without_models() -> None:
    llm = LLM(model="demo-model", api_key="sk-test-12345678")

    provider = llm._fallback_provider_config(
        {
            "provider": "backup",
            "type": "openai_chat",
            "compat": "generic",
            "api_key": "sk-backup",
            "base_url": "https://backup.example/v1",
            "models": [{"id": "cached-model"}],
            "stream_recovery": {
                "enabled": False,
                "max_continue_attempts": 0,
                "retry_empty_once": False,
                "retry_tool_delta_once": False,
            },
        }
    )

    assert provider is not None
    assert provider.id == "backup"
    assert provider.stream_recovery.enabled is False
    assert provider.stream_recovery.max_continue_attempts == 0
    assert provider.stream_recovery.retry_empty_once is False
    assert provider.stream_recovery.retry_tool_delta_once is False
    assert not hasattr(provider, "models")


def test_llm_chat_passes_ui_bus_to_hook_context_without_metadata_leak(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ui_bus = UIEventBus()
    seen = []
    ui_bus.subscribe(seen.append, replay_history=False)

    class CaptureUiBusHook(TransformHook[BeforeLLMRequestContext]):
        def __init__(self) -> None:
            super().__init__(name="capture_ui_bus")
            self.ui_bus = None
            self.metadata = {}

        def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
            self.ui_bus = context.ui_bus
            self.metadata = dict(context.metadata)
            context.metadata["hook_seen"] = "yes"
            return context

    hook = CaptureUiBusHook()
    registry = HookRegistry()
    registry.register(HookPoint.BEFORE_LLM_REQUEST, hook)
    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        base_url="https://example.com/v1",
        debug_trace=True,
    )

    def _fake_call_with_retry(params):
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=1, completion_tokens=1)),
            ]
        )

    _set_fake_call_with_retry(llm, _fake_call_with_retry)
    response = llm.chat(
        [{"role": "user", "content": "Hi"}],
        hook_registry=registry,
        session_id="session_test",
        metadata={"request_id": "request-1"},
        ui_bus=ui_bus,
    )

    assert response.content == "Hello"
    assert hook.ui_bus is ui_bus
    assert hook.metadata == {"request_id": "request-1"}
    debug_events = [event for event in seen if event.level == UIEventLevel.DEBUG]
    assert debug_events
    trace_path = debug_events[-1].data.get("trace_path")
    payload = json.loads(open(trace_path, encoding="utf-8").read())
    assert payload["metadata"]["request_id"] == "request-1"
    assert payload["metadata"]["hook_seen"] == "yes"
    assert "ui_bus" not in payload["metadata"]


def test_llm_chat_emits_reasoning_diagnostics_for_placeholder_and_missing_stream() -> None:
    ui_bus = UIEventBus()
    seen = []
    ui_bus.subscribe(seen.append, replay_history=False)
    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        thinking_enabled=True,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=True,
        ui_bus=ui_bus,
    )

    def _fake_call_with_retry(params):
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=1, completion_tokens=1)),
            ]
        )

    _set_fake_call_with_retry(llm, _fake_call_with_retry)
    response = llm.chat(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tool_1",
                        "type": "function",
                        "function": {"name": "glob", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
            {"role": "user", "content": "continue"},
        ]
    )

    assert response.content == "Hello"
    debug_messages = [
        event.message for event in seen if event.level == UIEventLevel.DEBUG
    ]
    assert any("sanitizer backfilled placeholder" in msg for msg in debug_messages)
    assert any("thinking enabled but no reasoning_content" in msg for msg in debug_messages)

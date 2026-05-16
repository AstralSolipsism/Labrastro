from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.config.models import ProviderConfig
from reuleauxcoder.domain.providers.models import ProviderRequest
from reuleauxcoder.services.providers.adapters.anthropic_messages import (
    AnthropicMessagesProvider,
    convert_chat_tools_to_anthropic_tools,
    convert_messages_to_anthropic,
)
from reuleauxcoder.services.providers.adapters.openai_chat import OpenAIChatProvider
from reuleauxcoder.services.providers.adapters.openai_responses import (
    OpenAIResponsesProvider,
    convert_chat_tools_to_responses_tools,
    convert_messages_to_responses_input,
)
from reuleauxcoder.extensions.provider.manifest import (
    ProviderManifestManager,
    run_provider_list_cli,
    run_provider_record_cli,
)
from reuleauxcoder.services.providers.manager import ProviderManager


def test_provider_manager_enriches_deepseek_v4_model_capabilities(monkeypatch) -> None:
    class FakeModels:
        def list(self):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id="deepseek-v4-pro",
                        owned_by="deepseek",
                        created=1,
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.models = FakeModels()

    monkeypatch.setattr("reuleauxcoder.services.providers.manager.OpenAI", FakeOpenAI)

    result = ProviderManager().list_models(
        ProviderConfig(
            id="deepseek",
            type="openai_chat",
            compat="deepseek",
            api_key="sk-test",
            base_url="https://api.deepseek.com",
        )
    )

    model = result["models"][0]
    assert model["id"] == "deepseek-v4-pro"
    assert model["max_tokens"] == 384000
    assert model["max_context_tokens"] == 1000000
    assert model["capability_source"] == "DeepSeek API Docs / Models & Pricing"


def test_anthropic_message_conversion_maps_tools_and_thinking() -> None:
    system, messages = convert_messages_to_anthropic(
        [
            {"role": "system", "content": "You are careful."},
            {"role": "user", "content": "List files"},
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "Need shell",
                "reasoning_signature": "sig-1",
                "tool_calls": [
                    {
                        "id": "tool_1",
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "arguments": '{"command":"ls"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tool_1", "content": "README.md"},
        ]
    )

    assert system == "You are careful."
    assert messages[1]["content"][0]["type"] == "thinking"
    assert messages[1]["content"][0]["signature"] == "sig-1"
    assert messages[1]["content"][1]["type"] == "tool_use"
    assert messages[2]["content"][0]["type"] == "tool_result"


def test_anthropic_tool_conversion_maps_openai_function_schema() -> None:
    tools = convert_chat_tools_to_anthropic_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "shell",
                    "description": "Run shell",
                    "parameters": {"type": "object"},
                },
            }
        ]
    )

    assert tools == [
        {
            "name": "shell",
            "description": "Run shell",
            "input_schema": {"type": "object"},
        }
    ]


def test_responses_message_conversion_maps_function_history() -> None:
    converted = convert_messages_to_responses_input(
        [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "arguments": '{"command":"pwd"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "/tmp"},
        ]
    )

    assert converted[0] == {"role": "developer", "content": "System"}
    assert converted[2]["type"] == "function_call"
    assert converted[3]["type"] == "function_call_output"


def test_responses_tool_conversion_maps_openai_function_schema() -> None:
    tools = convert_chat_tools_to_responses_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "shell",
                    "description": "Run shell",
                    "parameters": {"type": "object"},
                },
            }
        ]
    )

    assert tools[0]["type"] == "function"
    assert tools[0]["name"] == "shell"
    assert tools[0]["parameters"] == {"type": "object"}


def test_responses_provider_parses_streaming_text_and_function_call() -> None:
    provider = OpenAIResponsesProvider(
        ProviderConfig(id="responses", type="openai_responses", api_key="sk-test")
    )
    events = [
        SimpleNamespace(
            type="response.output_text.delta",
            delta="hello",
        ),
        SimpleNamespace(
            type="response.output_item.added",
            item=SimpleNamespace(
                type="function_call",
                id="item_1",
                call_id="call_1",
                name="shell",
                arguments="",
            ),
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            item_id="item_1",
            delta='{"command":"pwd"}',
        ),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                id="resp_1",
                usage=SimpleNamespace(
                    input_tokens=3,
                    output_tokens=4,
                    prompt_cache_hit_tokens=2,
                    prompt_cache_miss_tokens=1,
                ),
            ),
        ),
    ]
    captured = {}

    def _fake_create(**params):
        captured["params"] = params
        return iter(events)

    provider.client = SimpleNamespace(
        responses=SimpleNamespace(create=_fake_create)
    )

    response = provider.chat(
        ProviderRequest(
            model="gpt-demo",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "shell", "parameters": {"type": "object"}},
                }
            ],
        )
    )

    assert captured["params"]["stream"] is True
    assert response.content == "hello"
    assert response.provider_response_id == "resp_1"
    assert response.prompt_tokens == 3
    assert response.completion_tokens == 4
    assert response.cache_read_tokens == 2
    assert response.cache_write_tokens == 1
    assert response.usage_extra["prompt_cache"] == {
        "hit_tokens": 2,
        "miss_tokens": 1,
    }
    assert response.tool_calls[0].id == "call_1"
    assert response.tool_calls[0].arguments == {"command": "pwd"}


def test_chat_provider_parses_reasoning_delta_field() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(id="chat", type="openai_chat", api_key="sk-test")
    )
    events = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning="think",
                        reasoning_content=None,
                        reasoning_details=None,
                        tool_calls=None,
                    )
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content="done",
                        reasoning=None,
                        reasoning_content=None,
                        reasoning_details=None,
                        tool_calls=None,
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        ),
    ]
    provider.call_with_retry = lambda _params: iter(events)
    reasoning_tokens: list[str] = []

    response = provider.chat(
        ProviderRequest(
            model="qwen-demo",
            messages=[{"role": "user", "content": "hi"}],
            on_reasoning_token=reasoning_tokens.append,
        )
    )

    assert response.reasoning_content == "think"
    assert response.content == "done"
    assert reasoning_tokens == ["think"]


def test_responses_provider_emits_reasoning_delta_callback() -> None:
    provider = OpenAIResponsesProvider(
        ProviderConfig(id="responses", type="openai_responses", api_key="sk-test")
    )
    events = [
        SimpleNamespace(type="response.reasoning_text.delta", delta="plan"),
        SimpleNamespace(type="response.output_text.delta", delta="done"),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(id="resp_1", usage=None),
        ),
    ]
    provider.client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **_params: iter(events))
    )
    reasoning_tokens: list[str] = []

    response = provider.chat(
        ProviderRequest(
            model="gpt-demo",
            messages=[{"role": "user", "content": "hi"}],
            on_reasoning_token=reasoning_tokens.append,
        )
    )

    assert response.reasoning_content == "plan"
    assert response.content == "done"
    assert reasoning_tokens == ["plan"]


def test_chat_provider_parses_streaming_tool_arguments_across_chunks() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(id="chat", type="openai_chat", api_key="sk-test")
    )
    events = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning=None,
                        reasoning_content=None,
                        reasoning_details=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_1",
                                function=SimpleNamespace(
                                    name="write_file",
                                    arguments='{"file_path":"demo.md",',
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning=None,
                        reasoning_content=None,
                        reasoning_details=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id=None,
                                function=SimpleNamespace(
                                    name=None,
                                    arguments='"content":"hello"}',
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        ),
    ]
    provider.call_with_retry = lambda _params: iter(events)

    response = provider.chat(
        ProviderRequest(
            model="deepseek-demo",
            messages=[{"role": "user", "content": "hi"}],
            metadata={"llm_debug_raw_chunks": True},
        )
    )

    assert response.tool_calls[0].name == "write_file"
    assert response.tool_calls[0].arguments == {
        "file_path": "demo.md",
        "content": "hello",
    }
    assert response.tool_calls[0].argument_error is None
    assert response.provider_extra["tool_argument_diagnostics"] == []
    assert len(response.provider_extra["debug_raw_stream_chunks"]) == 2
    assert response.provider_extra["debug_raw_stream_chunks"][0]["_chunk_index"] == 0
    assert (
        response.provider_extra["debug_raw_stream_chunks"][0]["choices"][0]["delta"][
            "tool_calls"
        ][0]["function"]["arguments"]
        == '{"file_path":"demo.md",'
    )


def test_chat_provider_marks_empty_tool_arguments_as_diagnostic() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(id="chat", type="openai_chat", api_key="sk-test")
    )
    events = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning=None,
                        reasoning_content=None,
                        reasoning_details=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_1",
                                function=SimpleNamespace(
                                    name="write_file",
                                    arguments="",
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        )
    ]
    provider.call_with_retry = lambda _params: iter(events)

    response = provider.chat(
        ProviderRequest(model="deepseek-demo", messages=[{"role": "user", "content": "hi"}])
    )

    assert response.tool_calls[0].arguments == {}
    assert response.tool_calls[0].argument_error == "missing tool arguments"
    assert response.diagnostics[0].code == "invalid_tool_arguments"
    assert response.provider_extra["tool_argument_diagnostics"][0]["tool_name"] == "write_file"


def test_chat_provider_marks_invalid_tool_arguments_as_diagnostic() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(id="chat", type="openai_chat", api_key="sk-test")
    )
    events = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning=None,
                        reasoning_content=None,
                        reasoning_details=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_1",
                                function=SimpleNamespace(
                                    name="write_file",
                                    arguments='{"file_path":',
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        )
    ]
    provider.call_with_retry = lambda _params: iter(events)

    response = provider.chat(
        ProviderRequest(model="deepseek-demo", messages=[{"role": "user", "content": "hi"}])
    )

    assert response.tool_calls[0].arguments == {}
    assert response.tool_calls[0].argument_error == "invalid JSON arguments: Expecting value"
    diagnostic = response.provider_extra["tool_argument_diagnostics"][0]
    assert diagnostic["code"] == "invalid_tool_arguments"
    assert diagnostic["raw_arguments"] == '{"file_path":'


def test_responses_provider_marks_invalid_tool_arguments_as_diagnostic() -> None:
    provider = OpenAIResponsesProvider(
        ProviderConfig(id="responses", type="openai_responses", api_key="sk-test")
    )
    events = [
        SimpleNamespace(
            type="response.output_item.added",
            item=SimpleNamespace(
                type="function_call",
                id="item_1",
                call_id="call_1",
                name="write_file",
                arguments='{"file_path":',
            ),
        ),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(id="resp_1", usage=None),
        ),
    ]
    provider.client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **_params: iter(events))
    )

    response = provider.chat(
        ProviderRequest(model="gpt-demo", messages=[{"role": "user", "content": "hi"}])
    )

    assert response.tool_calls[0].argument_error == "invalid JSON arguments: Expecting value"
    assert response.diagnostics[0].code == "invalid_tool_arguments"
    assert response.provider_extra["tool_argument_diagnostics"][0]["tool_call_id"] == "call_1"


def test_anthropic_provider_marks_empty_tool_arguments_as_diagnostic() -> None:
    provider = AnthropicMessagesProvider(
        ProviderConfig(id="anthropic", type="anthropic_messages", api_key="sk-test")
    )
    events = [
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="tool_use",
                id="call_1",
                name="write_file",
            ),
        )
    ]
    provider.client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_params: iter(events))
    )

    response = provider.chat(
        ProviderRequest(model="claude-demo", messages=[{"role": "user", "content": "hi"}])
    )

    assert response.tool_calls[0].argument_error == "missing tool arguments"
    assert response.diagnostics[0].code == "invalid_tool_arguments"
    assert response.provider_extra["tool_argument_diagnostics"][0]["tool_name"] == "write_file"


def test_anthropic_provider_emits_thinking_delta_callback() -> None:
    provider = AnthropicMessagesProvider(
        ProviderConfig(id="anthropic", type="anthropic_messages", api_key="sk-test")
    )
    events = [
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking="inspect"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="done"),
        ),
    ]
    provider.client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_params: iter(events))
    )
    reasoning_tokens: list[str] = []

    response = provider.chat(
        ProviderRequest(
            model="claude-demo",
            messages=[{"role": "user", "content": "hi"}],
            on_reasoning_token=reasoning_tokens.append,
        )
    )

    assert response.reasoning_content == "inspect"
    assert response.content == "done"
    assert reasoning_tokens == ["inspect"]


def test_chat_provider_parses_deepseek_cache_usage_fields() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(id="chat", type="openai_chat", api_key="sk-test")
    )
    events = [
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=2,
                prompt_cache_hit_tokens=7,
                prompt_cache_miss_tokens=3,
            ),
        )
    ]
    provider.call_with_retry = lambda _params: iter(events)

    response = provider.chat(
        ProviderRequest(
            model="deepseek-demo",
            messages=[{"role": "user", "content": "hi"}],
        )
    )

    assert response.prompt_tokens == 10
    assert response.completion_tokens == 2
    assert response.cache_read_tokens == 7
    assert response.cache_write_tokens == 3
    assert response.usage_extra["prompt_cache"] == {
        "hit_tokens": 7,
        "miss_tokens": 3,
    }


def test_chat_provider_preserves_reasoning_details_signature() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(id="chat", type="openai_chat", api_key="sk-test")
    )
    events = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None,
                        reasoning=None,
                        reasoning_content=None,
                        reasoning_details=[
                            {
                                "type": "reasoning.text",
                                "text": "think",
                                "signature": "sig-1",
                            }
                        ],
                        tool_calls=None,
                    )
                )
            ],
            usage=None,
        )
    ]
    provider.call_with_retry = lambda _params: iter(events)
    reasoning_tokens: list[str] = []

    response = provider.chat(
        ProviderRequest(
            model="qwen-demo",
            messages=[{"role": "user", "content": "hi"}],
            on_reasoning_token=reasoning_tokens.append,
        )
    )

    assert response.reasoning_content == "think"
    assert response.reasoning_signature == "sig-1"
    assert response.reasoning_details[0]["signature"] == "sig-1"
    assert reasoning_tokens == ["think"]


def test_chat_provider_downgrades_deepseek_forced_tool_choice_during_thinking() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(
            id="deepseek",
            type="openai_chat",
            compat="deepseek",
            api_key="sk-test",
            base_url="https://api.deepseek.com",
        )
    )
    request = ProviderRequest(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "shell", "parameters": {"type": "object"}},
            }
        ],
        thinking_enabled=True,
        tool_choice={"type": "function", "function": {"name": "shell"}},
    )

    params = provider.build_request_params(request)

    assert params["tool_choice"] == "auto"
    diagnostics = request.metadata["provider_diagnostics"]
    assert diagnostics[0].code == "tool_choice_thinking_downgraded"


def test_chat_provider_keeps_deepseek_forced_tool_choice_when_thinking_disabled() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(
            id="deepseek",
            type="openai_chat",
            compat="deepseek",
            api_key="sk-test",
            base_url="https://api.deepseek.com",
        )
    )
    provider.config.api_features.tool_choice_required = True
    request = ProviderRequest(
        model="deepseek-v4-pro",
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "shell", "parameters": {"type": "object"}},
            }
        ],
        thinking_enabled=False,
        tool_choice="required",
    )

    params = provider.build_request_params(request)

    assert params["tool_choice"] == "required"


def test_responses_provider_omits_temperature_by_default() -> None:
    provider = OpenAIResponsesProvider(
        ProviderConfig(id="responses", type="openai_responses", api_key="sk-test")
    )

    params = provider.build_request_params(
        ProviderRequest(model="gpt-demo", messages=[{"role": "user", "content": "hi"}])
    )

    assert "temperature" not in params


def test_responses_provider_requests_reasoning_summary_by_default() -> None:
    provider = OpenAIResponsesProvider(
        ProviderConfig(id="responses", type="openai_responses", api_key="sk-test")
    )

    params = provider.build_request_params(
        ProviderRequest(
            model="gpt-demo",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="low",
        )
    )

    assert params["reasoning"] == {"effort": "low", "summary": "auto"}


def test_chat_provider_applies_kimi_compat_rules() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(
            id="kimi",
            type="openai_chat",
            compat="kimi",
            api_key="sk-test",
        )
    )
    request = ProviderRequest(
        model="kimi-k2.6",
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "shell", "parameters": {"type": "object"}},
            }
        ],
        thinking_enabled=True,
        tool_choice="required",
        max_tokens=4096,
    )

    params = provider.build_request_params(request)

    assert "temperature" not in params
    assert params["extra_body"] == {"thinking": {"type": "enabled"}}
    assert params["tool_choice"] == "auto"
    diagnostics = request.metadata["provider_diagnostics"]
    assert {item.code for item in diagnostics} == {
        "tool_choice_thinking_downgraded",
        "kimi_thinking_tool_max_tokens_low",
    }


def test_chat_provider_applies_glm_compat_clear_thinking() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(
            id="glm",
            type="openai_chat",
            compat="glm",
            api_key="sk-test",
            extra={"clear_thinking": False},
        )
    )

    params = provider.build_request_params(
        ProviderRequest(
            model="glm-5",
            messages=[{"role": "user", "content": "hi"}],
            thinking_enabled=True,
            tool_choice="required",
            tools=[
                {
                    "type": "function",
                    "function": {"name": "shell", "parameters": {"type": "object"}},
                }
            ],
        )
    )

    assert params["extra_body"] == {
        "thinking": {"type": "enabled"},
        "clear_thinking": False,
    }
    assert params["tool_choice"] == "auto"


def test_chat_provider_applies_qwen_compat_thinking_body() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(
            id="qwen",
            type="openai_chat",
            compat="qwen",
            api_key="sk-test",
            extra={"thinking_budget": "2048", "preserve_thinking": "true"},
        )
    )
    request = ProviderRequest(
        model="qwen3",
        messages=[{"role": "user", "content": "hi"}],
        thinking_enabled=True,
        reasoning_effort="high",
        tool_choice="required",
        tools=[
            {
                "type": "function",
                "function": {"name": "shell", "parameters": {"type": "object"}},
            }
        ],
    )

    params = provider.build_request_params(request)

    assert params["extra_body"] == {
        "enable_thinking": True,
        "thinking_budget": 2048,
        "preserve_thinking": True,
    }
    assert "reasoning_effort" not in params
    assert params["tool_choice"] == "auto"
    diagnostics = request.metadata["provider_diagnostics"]
    assert {item.code for item in diagnostics} == {
        "reasoning_effort_ignored_for_compat",
        "tool_choice_thinking_downgraded",
    }


def test_anthropic_provider_omits_temperature_when_thinking_enabled() -> None:
    provider = AnthropicMessagesProvider(
        ProviderConfig(id="anthropic", type="anthropic_messages", api_key="sk-test")
    )

    params = provider.build_request_params(
        ProviderRequest(
            model="claude-demo",
            messages=[{"role": "user", "content": "hi"}],
            thinking_enabled=True,
            max_tokens=1400,
        )
    )

    assert "temperature" not in params
    assert params["thinking"]["budget_tokens"] == 1024


def test_anthropic_provider_maps_deepseek_reasoning_effort_to_output_config() -> None:
    provider = AnthropicMessagesProvider(
        ProviderConfig(
            id="deepseek-anthropic",
            type="anthropic_messages",
            compat="deepseek",
            api_key="sk-test",
            base_url="https://api.deepseek.com/anthropic",
        )
    )

    params = provider.build_request_params(
        ProviderRequest(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "hi"}],
            thinking_enabled=True,
            reasoning_effort="xhigh",
            max_tokens=512,
        )
    )

    assert params["max_tokens"] == 512
    assert params["thinking"]["budget_tokens"] == 1024
    assert params["extra_body"]["output_config"] == {"effort": "max"}
    assert "provider_diagnostics" not in params


def test_responses_provider_applies_qwen_compat_enable_thinking() -> None:
    provider = OpenAIResponsesProvider(
        ProviderConfig(
            id="qwen-responses",
            type="openai_responses",
            compat="qwen",
            api_key="sk-test",
            extra={"thinking_budget": 4096},
        )
    )
    request = ProviderRequest(
        model="qwen3",
        messages=[{"role": "user", "content": "hi"}],
        thinking_enabled=True,
        reasoning_effort="high",
    )

    params = provider.build_request_params(request)

    assert params["extra_body"] == {
        "enable_thinking": True,
        "thinking_budget": 4096,
    }
    assert "reasoning" not in params
    diagnostics = request.metadata["provider_diagnostics"]
    assert diagnostics[0].code == "reasoning_effort_ignored_for_compat"


def test_provider_capability_downgrade_records_diagnostic() -> None:
    provider = OpenAIResponsesProvider(
        ProviderConfig(id="responses", type="openai_responses", api_key="sk-test")
    )
    provider.config.api_features.tool_choice_required = False
    request = ProviderRequest(
        model="gpt-demo",
        messages=[{"role": "user", "content": "hi"}],
        tool_choice="required",
    )

    params = provider.build_request_params(request)

    assert params["tool_choice"] == "auto"
    diagnostics = request.metadata["provider_diagnostics"]
    assert diagnostics[0].code == "tool_choice_required_downgraded"


def test_provider_manifest_record_updates_config(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    manager = ProviderManifestManager(path)

    result = manager.record_provider(
        ProviderConfig(
            id="openai-main",
            type="openai_chat",
            compat="zenmux",
            api_key="${OPENAI_API_KEY}",
            base_url="https://api.openai.com/v1",
        )
    )

    assert result.created is True
    raw = path.read_text(encoding="utf-8")
    assert "openai-main" in raw
    assert "zenmux" in raw
    assert "${OPENAI_API_KEY}" in raw


def test_provider_manager_rejects_disabled_provider() -> None:
    provider = ProviderConfig(
        id="disabled",
        type="openai_chat",
        enabled=False,
        api_key="sk-test",
        base_url="https://example.invalid/v1",
    )

    with pytest.raises(RuntimeError, match="disabled"):
        ProviderManager().create(provider)


def test_provider_record_cli_writes_compat(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-test")

    result = run_provider_record_cli(
        SimpleNamespace(
            config=str(path),
            provider_id="kimi",
            provider_type="openai_chat",
            compat="kimi",
            api_key=None,
            api_key_env="MOONSHOT_API_KEY",
            base_url="https://api.moonshot.ai/v1",
            base_url_env=None,
            header=[],
            timeout_sec=120,
            max_retries=3,
            api_feature=[],
            extra=[],
        )
    )

    assert result == 0
    capsys.readouterr()
    provider = ProviderManifestManager(path).raw_provider("kimi")
    assert provider is not None
    assert provider.compat == "kimi"


def test_provider_list_cli_displays_compat(tmp_path, capsys) -> None:
    path = tmp_path / "config.yaml"
    ProviderManifestManager(path).record_provider(
        ProviderConfig(
            id="deepseek",
            type="openai_chat",
            compat="deepseek",
            api_key="sk-test",
            base_url="https://api.deepseek.com",
        )
    )

    result = run_provider_list_cli(SimpleNamespace(config=str(path)))

    assert result == 0
    output = capsys.readouterr().out
    assert "deepseek\topenai_chat\tcompat=deepseek" in output

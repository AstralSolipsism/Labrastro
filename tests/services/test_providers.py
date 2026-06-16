from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, BadRequestError

from reuleauxcoder.domain.config.models import ProviderConfig
from reuleauxcoder.domain.providers.models import ProviderRequest
from reuleauxcoder.services.providers.adapters.anthropic_messages import (
    AnthropicMessagesProvider,
    convert_chat_tools_to_anthropic_tools,
    convert_messages_to_anthropic,
)
from reuleauxcoder.services.providers.adapters.labrastro_server import (
    LabrastroServerProvider,
)
from reuleauxcoder.services.providers.adapters.openai_chat import (
    OpenAIChatProvider,
    _DEBUG_HTTP_CHUNK_SINK,
    _DebugHTTPTransport,
)
from reuleauxcoder.services.providers.adapters.openai_responses import (
    OpenAIResponsesProvider,
    convert_chat_tools_to_responses_tools,
    convert_messages_to_responses_input,
)
from reuleauxcoder.services.llm.client import LLM, llm_is_configured
from reuleauxcoder.services.llm.factory import ResolvedModelRuntime
from reuleauxcoder.extensions.provider.manifest import (
    ProviderManifestManager,
    run_provider_list_cli,
    run_provider_record_cli,
)
from reuleauxcoder.services.providers.manager import ProviderManager
from reuleauxcoder.services.providers.stream_supervisor import ProviderStreamInterruptedError


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


def test_chat_provider_uses_configured_openai_client(monkeypatch) -> None:
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "reuleauxcoder.services.providers.adapters.openai_chat.OpenAI",
        FakeOpenAI,
    )

    provider = OpenAIChatProvider(
        ProviderConfig(
            id="chat",
            type="openai_chat",
            api_key="sk-test",
            base_url="https://api.example.test/v1",
            headers={"X-Test": "yes"},
            timeout_sec=9,
            max_retries=2,
        )
    )

    assert provider.config.timeout_sec == 9
    assert provider.config.max_retries == 2
    assert captured["api_key"] == "sk-test"
    assert captured["base_url"] == "https://api.example.test/v1"
    assert captured["timeout"] == 9
    assert captured["default_headers"] == {"X-Test": "yes"}
    assert isinstance(captured["http_client"], httpx.Client)


def test_labrastro_server_provider_streams_through_agent_run_bridge(monkeypatch) -> None:
    monkeypatch.setenv("LABRASTRO_REMOTE_BASE_URL", "http://127.0.0.1:8765/")
    monkeypatch.setenv("LABRASTRO_PEER_TOKEN", "peer-token")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_ID", "run-1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_REQUEST_ID", "claim-1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_ACTIVATION_ID", "run-1:activation:1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_WORKER_ID", "worker-1")

    class FakeStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_lines(self):
            yield "event: token"
            yield 'data: {"text": "hel"}'
            yield ""
            yield "event: reasoning_token"
            yield 'data: {"text": "plan"}'
            yield ""
            yield "event: tool_call_delta"
            yield 'data: {"id": "call-1", "name": "read_file"}'
            yield ""
            yield "event: done"
            yield (
                'data: {"content": "hello", "reasoning_content": "plan", '
                '"tool_calls": [{"id": "call-1", "name": "read_file", '
                '"arguments": {"path": "README.md"}}], "prompt_tokens": 1, '
                '"completion_tokens": 2, "cost_usd": 0.01, '
                '"diagnostics": [{"code": "notice", "message": "ok"}]}'
            )
            yield ""

    class FakeClient:
        def __init__(self):
            self.calls = []

        def stream(self, method, url, json):
            self.calls.append({"method": method, "url": url, "json": json})
            return FakeStream()

    provider = LabrastroServerProvider(
        ProviderConfig(id="labrastro-server", type="labrastro_server")
    )
    fake_client = FakeClient()
    provider.client = fake_client
    tokens: list[str] = []
    reasoning: list[str] = []
    tool_deltas: list[dict] = []

    response = provider.chat(
        ProviderRequest(
            model="agent-run",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=384000,
            on_token=tokens.append,
            on_reasoning_token=reasoning.append,
            on_tool_call_delta=tool_deltas.append,
        )
    )

    assert tokens == ["hel"]
    assert reasoning == ["plan"]
    assert tool_deltas == [{"id": "call-1", "name": "read_file"}]
    assert response.content == "hello"
    assert response.reasoning_content == "plan"
    assert response.prompt_tokens == 1
    assert response.completion_tokens == 2
    assert response.cost_usd == 0.01
    assert response.diagnostics[0].code == "notice"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    call = fake_client.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://127.0.0.1:8765/remote/agent-run-activations/model-request"
    assert call["json"]["peer_token"] == "peer-token"
    assert call["json"]["agent_run_id"] == "run-1"
    assert call["json"]["request_id"] == "claim-1"
    assert call["json"]["activation_id"] == "run-1:activation:1"
    assert call["json"]["worker_id"] == "worker-1"
    assert call["json"]["parameters"]["max_tokens"] == 384000


def test_labrastro_server_provider_converts_transport_drop_to_stream_interruption(monkeypatch) -> None:
    monkeypatch.setenv("LABRASTRO_REMOTE_BASE_URL", "http://127.0.0.1:8765/")
    monkeypatch.setenv("LABRASTRO_PEER_TOKEN", "peer-token")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_ID", "run-1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_REQUEST_ID", "claim-1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_ACTIVATION_ID", "run-1:activation:1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_WORKER_ID", "worker-1")

    class BrokenStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_lines(self):
            yield "event: token"
            yield 'data: {"text": "hel"}'
            yield ""
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body"
            )

    class FakeClient:
        def stream(self, method, url, json):
            return BrokenStream()

    provider = LabrastroServerProvider(
        ProviderConfig(id="labrastro-server", type="labrastro_server")
    )
    provider.client = FakeClient()
    tokens: list[str] = []

    with pytest.raises(ProviderStreamInterruptedError) as exc_info:
        provider.chat(
            ProviderRequest(
                model="agent-run",
                messages=[{"role": "user", "content": "hi"}],
                on_token=tokens.append,
            )
        )

    assert tokens == ["hel"]
    interrupted = exc_info.value
    assert interrupted.partial_response.content == "hel"
    assert interrupted.partial_response.stream_status == "interrupted"
    assert interrupted.interruption["recoverable"] is True
    assert interrupted.interruption["error_type"] == "RemoteProtocolError"


def test_labrastro_server_provider_uses_interrupted_sse_terminal_as_stream_interruption(monkeypatch) -> None:
    monkeypatch.setenv("LABRASTRO_REMOTE_BASE_URL", "http://127.0.0.1:8765/")
    monkeypatch.setenv("LABRASTRO_PEER_TOKEN", "peer-token")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_ID", "run-1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_REQUEST_ID", "claim-1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_ACTIVATION_ID", "run-1:activation:1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_WORKER_ID", "worker-1")

    class InterruptedStream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_lines(self):
            yield "event: token"
            yield 'data: {"text": "hel"}'
            yield ""
            yield "event: interrupted"
            yield (
                'data: {"content": "hel", "message": "Provider stream interrupted.", '
                '"interruption": {"recoverable": true, "retry_action": "continue", '
                '"partial_kind": "text", "classification": "text_interrupted"}}'
            )
            yield ""

    class FakeClient:
        def stream(self, method, url, json):
            return InterruptedStream()

    provider = LabrastroServerProvider(
        ProviderConfig(id="labrastro-server", type="labrastro_server")
    )
    provider.client = FakeClient()

    with pytest.raises(ProviderStreamInterruptedError) as exc_info:
        provider.chat(
            ProviderRequest(
                model="agent-run",
                messages=[{"role": "user", "content": "hi"}],
            )
        )

    assert exc_info.value.partial_response.content == "hel"
    assert exc_info.value.interruption["classification"] == "text_interrupted"


def test_labrastro_server_provider_does_not_apply_idle_read_timeout(monkeypatch) -> None:
    monkeypatch.setenv("LABRASTRO_REMOTE_BASE_URL", "http://127.0.0.1:8765/")
    monkeypatch.setenv("LABRASTRO_PEER_TOKEN", "peer-token")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_ID", "run-1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_REQUEST_ID", "claim-1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_ACTIVATION_ID", "run-1:activation:1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_WORKER_ID", "worker-1")

    provider = LabrastroServerProvider(
        ProviderConfig(
            id="labrastro-server",
            type="labrastro_server",
            timeout_sec=9,
        )
    )
    try:
        assert provider.client.timeout.connect == 9
        assert provider.client.timeout.write == 9
        assert provider.client.timeout.pool == 9
        assert provider.client.timeout.read is None
    finally:
        provider.client.close()


def test_labrastro_server_llm_is_configured_without_local_api_key(monkeypatch) -> None:
    monkeypatch.setenv("LABRASTRO_REMOTE_BASE_URL", "http://127.0.0.1:8765/")
    monkeypatch.setenv("LABRASTRO_PEER_TOKEN", "peer-token")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_ID", "run-1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_REQUEST_ID", "claim-1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_ACTIVATION_ID", "run-1:activation:1")
    monkeypatch.setenv("LABRASTRO_AGENT_RUN_WORKER_ID", "worker-1")

    llm = LLM(
        model="agent-run",
        provider_config=ProviderConfig(id="labrastro-server", type="labrastro_server"),
    )

    assert llm.api_key == ""
    assert llm.configured is True
    assert llm_is_configured(llm) is True


def test_labrastro_server_llm_reports_missing_worker_environment(monkeypatch) -> None:
    for key in (
        "LABRASTRO_REMOTE_BASE_URL",
        "LABRASTRO_PEER_TOKEN",
        "LABRASTRO_AGENT_RUN_ID",
        "LABRASTRO_AGENT_RUN_REQUEST_ID",
        "LABRASTRO_AGENT_RUN_ACTIVATION_ID",
        "LABRASTRO_AGENT_RUN_WORKER_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    llm = LLM(
        model="agent-run",
        provider_config=ProviderConfig(id="labrastro-server", type="labrastro_server"),
    )

    assert llm.configured is False
    assert llm_is_configured(llm) is False
    assert "LABRASTRO_REMOTE_BASE_URL" in llm.unavailable_reason
    assert "LABRASTRO_AGENT_RUN_WORKER_ID" in llm.unavailable_reason


def test_regular_llm_provider_still_requires_api_key() -> None:
    llm = LLM(
        model="gpt-test",
        provider_config=ProviderConfig(id="chat", type="openai_chat"),
    )

    assert llm.configured is False
    assert llm.unavailable_reason.startswith("No model provider API key is configured")


def test_resolved_model_runtime_treats_labrastro_server_as_server_origin() -> None:
    runtime = ResolvedModelRuntime(
        model="agent-run",
        provider_config=ProviderConfig(id="labrastro-server", type="labrastro_server"),
    )

    assert runtime.configured is True


def test_debug_http_transport_uses_incoming_request_when_wrapping_response(
    monkeypatch,
) -> None:
    transport = _DebugHTTPTransport()
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")

    def fake_handle_request(self, incoming_request):
        assert incoming_request is request
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(b"data: ok\n\n"),
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake_handle_request)
    sink: list[dict] = []
    token = _DEBUG_HTTP_CHUNK_SINK.set(sink)
    try:
        response = transport.handle_request(request)
        assert response.request is request
        assert b"".join(response.iter_bytes()) == b"data: ok\n\n"
    finally:
        _DEBUG_HTTP_CHUNK_SINK.reset(token)
        transport.close()

    assert [item["type"] for item in sink] == [
        "request_start",
        "response_start",
        "response_body_chunk",
    ]


def test_chat_provider_retries_connection_errors_without_stream_options_downgrade() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(
            id="chat",
            type="openai_chat",
            api_key="sk-test",
            max_retries=1,
        )
    )
    calls = []
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")

    def create(**params):
        calls.append(dict(params))
        raise APIConnectionError(request=request)

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    with pytest.raises(APIConnectionError) as exc_info:
        provider.chat(
            ProviderRequest(model="demo", messages=[{"role": "user", "content": "hi"}])
        )

    assert len(calls) == 2
    assert all("stream_options" in call for call in calls)
    attempts = getattr(exc_info.value, "provider_retry_attempts")
    assert [item["action"] for item in attempts] == ["retry", "raise"]
    assert not any(item.get("action") == "retry_without_stream_options" for item in attempts)
    assert getattr(exc_info.value, "provider_error_phase") == "request_start"


def test_chat_provider_retries_unsupported_stream_options_once() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(id="chat", type="openai_chat", api_key="sk-test")
    )
    calls = []

    def create(**params):
        calls.append(dict(params))
        if len(calls) == 1:
            request = httpx.Request(
                "POST", "https://api.example.test/v1/chat/completions"
            )
            response = httpx.Response(400, request=request)
            raise BadRequestError(
                "Unrecognized request argument supplied: stream_options",
                response=response,
                body=None,
            )
        return iter(
            [
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                content="ok",
                                reasoning=None,
                                reasoning_content=None,
                                reasoning_details=None,
                                tool_calls=None,
                            )
                        )
                    ],
                    usage=None,
                )
            ]
        )

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    response = provider.chat(
        ProviderRequest(model="demo", messages=[{"role": "user", "content": "hi"}])
    )

    assert response.content == "ok"
    assert "stream_options" in calls[0]
    assert "stream_options" not in calls[1]
    assert response.provider_extra["stream_options_enabled"] is False
    assert response.provider_extra["retry_attempts"][0]["action"] == (
        "retry_without_stream_options"
    )


def test_chat_provider_marks_stream_iteration_errors() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(id="chat", type="openai_chat", api_key="sk-test")
    )

    class BrokenStream:
        def __iter__(self):
            raise RuntimeError("stream broke")

    provider.call_with_retry = lambda _params, **_kwargs: BrokenStream()

    with pytest.raises(ProviderStreamInterruptedError) as exc_info:
        provider.chat(
            ProviderRequest(model="demo", messages=[{"role": "user", "content": "hi"}])
        )

    assert str(exc_info.value) == "stream broke"
    assert exc_info.value.partial_response.stream_status == "interrupted"
    assert exc_info.value.interruption["phase"] == "stream_iterate"
    assert exc_info.value.interruption["classification"] == "empty_interrupted"


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
    deltas: list[dict] = []

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
            on_tool_call_delta=deltas.append,
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
    assert deltas == [
        {
            "index": 0,
            "tool_call_id": "call_1",
            "tool_name": "shell",
            "arguments_delta": "",
            "arguments_preview": "",
            "status": "preparing",
        },
        {
            "index": 0,
            "tool_call_id": "call_1",
            "tool_name": "shell",
            "arguments_delta": '{"command":"pwd"}',
            "arguments_preview": '{"command":"pwd"}',
            "status": "preparing",
        },
    ]


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
    provider.call_with_retry = lambda _params, **_kwargs: iter(events)
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
                                    name="apply_patch",
                                    arguments='{"patch":"*** Begin Patch',
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
                                    arguments='\\n*** End Patch"}',
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        ),
    ]
    provider.call_with_retry = lambda _params, **_kwargs: iter(events)

    response = provider.chat(
        ProviderRequest(
            model="deepseek-demo",
            messages=[{"role": "user", "content": "hi"}],
            metadata={"llm_debug_raw_chunks": True},
        )
    )

    assert response.tool_calls[0].name == "apply_patch"
    assert response.tool_calls[0].arguments == {
        "patch": "*** Begin Patch\n*** End Patch",
    }
    assert response.tool_calls[0].argument_error is None
    assert response.provider_extra["tool_argument_diagnostics"] == []
    assert len(response.provider_extra["debug_raw_stream_chunks"]) == 2
    assert response.provider_extra["debug_raw_stream_chunks"][0]["_chunk_index"] == 0
    assert (
        response.provider_extra["debug_raw_stream_chunks"][0]["choices"][0]["delta"][
            "tool_calls"
        ][0]["function"]["arguments"]
        == '{"patch":"*** Begin Patch'
    )


def test_chat_provider_emits_tool_call_delta_callback() -> None:
    provider = OpenAIChatProvider(
        ProviderConfig(id="chat", type="openai_chat", api_key="sk-test")
    )
    events = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        role="assistant",
                        content=None,
                        reasoning=None,
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
                        content=None,
                        reasoning=None,
                        reasoning_content=None,
                        reasoning_details=None,
                        tool_calls=[
                            SimpleNamespace(
                                index=0,
                                id="call_1",
                                function=SimpleNamespace(
                                    name="grep",
                                    arguments='{"pattern":"remote',
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
                                    arguments='PeerState"}',
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        ),
        SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=1)),
    ]
    provider.call_with_retry = lambda _params, **_kwargs: iter(events)
    deltas: list[dict] = []

    response = provider.chat(
        ProviderRequest(
            model="gpt-demo",
            messages=[{"role": "user", "content": "hi"}],
            on_tool_call_delta=deltas.append,
        )
    )

    assert response.tool_calls[0].name == "grep"
    assert deltas == [
        {
            "index": 0,
            "tool_call_id": "call_1",
            "tool_name": "grep",
            "arguments_delta": '{"pattern":"remote',
            "arguments_preview": '{"pattern":"remote',
            "status": "preparing",
        },
        {
            "index": 0,
            "tool_call_id": "call_1",
            "tool_name": "grep",
            "arguments_delta": 'PeerState"}',
            "arguments_preview": '{"pattern":"remotePeerState"}',
            "status": "preparing",
        },
    ]


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
                                    name="apply_patch",
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
    provider.call_with_retry = lambda _params, **_kwargs: iter(events)

    response = provider.chat(
        ProviderRequest(model="deepseek-demo", messages=[{"role": "user", "content": "hi"}])
    )

    assert response.tool_calls[0].arguments == {}
    assert response.tool_calls[0].argument_error == "missing tool arguments"
    assert response.diagnostics[0].code == "invalid_tool_arguments"
    assert response.provider_extra["tool_argument_diagnostics"][0]["tool_name"] == "apply_patch"


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
                                    name="apply_patch",
                                    arguments='{"patch":',
                                ),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        )
    ]
    provider.call_with_retry = lambda _params, **_kwargs: iter(events)

    response = provider.chat(
        ProviderRequest(model="deepseek-demo", messages=[{"role": "user", "content": "hi"}])
    )

    assert response.tool_calls[0].arguments == {}
    assert response.tool_calls[0].argument_error == "invalid JSON arguments: Expecting value"
    diagnostic = response.provider_extra["tool_argument_diagnostics"][0]
    assert diagnostic["code"] == "invalid_tool_arguments"
    assert diagnostic["raw_arguments"] == '{"patch":'


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
                name="apply_patch",
                arguments='{"patch":',
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
                name="apply_patch",
            ),
        )
    ]
    provider.client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_params: iter(events))
    )
    deltas: list[dict] = []

    response = provider.chat(
        ProviderRequest(
            model="claude-demo",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=4096,
            on_tool_call_delta=deltas.append,
        )
    )

    assert response.tool_calls[0].argument_error == "missing tool arguments"
    assert response.diagnostics[0].code == "invalid_tool_arguments"
    assert response.provider_extra["tool_argument_diagnostics"][0]["tool_name"] == "apply_patch"
    assert deltas == [
        {
            "index": 0,
            "tool_call_id": "call_1",
            "tool_name": "apply_patch",
            "arguments_delta": "",
            "arguments_preview": "",
            "status": "preparing",
        }
    ]


def test_anthropic_provider_emits_tool_argument_delta_callback() -> None:
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
                name="grep",
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(
                type="input_json_delta",
                partial_json='{"pattern":"RunStatusBar"}',
            ),
        ),
    ]
    provider.client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **_params: iter(events))
    )
    deltas: list[dict] = []

    response = provider.chat(
        ProviderRequest(
            model="claude-demo",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=4096,
            on_tool_call_delta=deltas.append,
        )
    )

    assert response.tool_calls[0].name == "grep"
    assert response.tool_calls[0].arguments == {"pattern": "RunStatusBar"}
    assert deltas[-1] == {
        "index": 0,
        "tool_call_id": "call_1",
        "tool_name": "grep",
        "arguments_delta": '{"pattern":"RunStatusBar"}',
        "arguments_preview": '{"pattern":"RunStatusBar"}',
        "status": "preparing",
    }


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
            max_tokens=4096,
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
    provider.call_with_retry = lambda _params, **_kwargs: iter(events)

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
    provider.call_with_retry = lambda _params, **_kwargs: iter(events)
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


def test_provider_record_cli_requires_authoritative_config(
    capsys, monkeypatch
) -> None:
    monkeypatch.delenv("RCODER_CONFIG_PATH", raising=False)

    result = run_provider_record_cli(
        SimpleNamespace(
            config=None,
            provider_id="kimi",
            provider_type="openai_chat",
            compat="kimi",
            api_key="sk-test",
            api_key_env=None,
            base_url="https://api.moonshot.ai/v1",
            base_url_env=None,
            header=[],
            timeout_sec=120,
            max_retries=3,
            api_feature=[],
            extra=[],
        )
    )

    assert result == 1
    assert "requires --config or RCODER_CONFIG_PATH" in capsys.readouterr().out


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

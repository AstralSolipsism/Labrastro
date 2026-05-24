"""OpenAI Responses provider adapter."""

from __future__ import annotations

from typing import Any

from openai import OpenAI

from reuleauxcoder.domain.config.models import ProviderConfig
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.providers.models import (
    ProviderDiagnostic,
    ProviderRequest,
    ProviderResponse,
)
from reuleauxcoder.services.providers.compat import apply_openai_responses_qwen
from reuleauxcoder.services.providers.stream_supervisor import StreamSupervisor
from reuleauxcoder.services.providers.tool_call_delta import (
    emit_tool_call_delta,
    tool_arguments_preview,
)
from reuleauxcoder.services.providers.tool_arguments import (
    parse_provider_tool_arguments,
)


def convert_chat_tools_to_responses_tools(tools: list[dict[str, Any]]) -> list[dict]:
    converted: list[dict] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            continue
        converted.append(
            {
                "type": "function",
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "parameters": function.get("parameters", {"type": "object"}),
            }
        )
    return converted


def convert_messages_to_responses_input(messages: list[dict[str, Any]]) -> list[dict]:
    converted: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            converted.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": str(message.get("content") or ""),
                }
            )
            continue
        if role == "assistant" and message.get("tool_calls"):
            content = message.get("content")
            if content:
                converted.append({"role": "assistant", "content": str(content)})
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                converted.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call.get("id", ""),
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", "{}"),
                    }
                )
            continue
        if role in {"system", "user", "assistant"}:
            converted.append(
                {
                    "role": "developer" if role == "system" else role,
                    "content": str(message.get("content") or ""),
                }
            )
    return converted


def _usage_attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage_int(obj: Any, name: str) -> int | None:
    value = _usage_attr(obj, name)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usage_float(obj: Any, name: str) -> float | None:
    value = _usage_attr(obj, name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _usage_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    if hasattr(obj, "model_dump"):
        dumped = obj.model_dump()
        return dict(dumped) if isinstance(dumped, dict) else {}
    return {
        key: value
        for key in (
            "cached_tokens",
            "cache_creation_tokens",
            "input_tokens",
            "output_tokens",
        )
        if (value := getattr(obj, key, None)) is not None
    }


def _first_int(*values: int | None) -> int | None:
    for value in values:
        if value is not None:
            return value
    return None


def _extract_cache_usage(usage: Any) -> tuple[int | None, int | None, dict[str, Any]]:
    details = _usage_attr(usage, "input_tokens_details") or _usage_attr(
        usage, "prompt_tokens_details"
    )
    prompt_cache_hit = _usage_int(usage, "prompt_cache_hit_tokens")
    prompt_cache_miss = _usage_int(usage, "prompt_cache_miss_tokens")
    extra = {"input_tokens_details": _usage_dict(details)} if details is not None else {}
    if prompt_cache_hit is not None or prompt_cache_miss is not None:
        extra["prompt_cache"] = {
            "hit_tokens": prompt_cache_hit,
            "miss_tokens": prompt_cache_miss,
        }
    return (
        _first_int(_usage_int(details, "cached_tokens"), prompt_cache_hit),
        _first_int(_usage_int(details, "cache_creation_tokens"), prompt_cache_miss),
        extra,
    )


class OpenAIResponsesProvider:
    """Provider adapter for OpenAI Responses API."""

    provider_type = "openai_responses"

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.provider_id = config.id
        client_kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "base_url": config.base_url,
            "timeout": config.timeout_sec,
        }
        if config.headers:
            client_kwargs["default_headers"] = config.headers
        self.client = OpenAI(**client_kwargs)

    def build_request_params(self, request: ProviderRequest) -> dict:
        diagnostics: list[ProviderDiagnostic] = []
        params: dict[str, Any] = {
            "model": request.model,
            "input": convert_messages_to_responses_input(request.messages),
            "stream": True,
        }
        if request.max_tokens > 0:
            params["max_output_tokens"] = request.max_tokens
        if self.config.extra.get("send_temperature"):
            params["temperature"] = request.temperature
        if request.tools:
            if not self.config.api_features.tools:
                raise RuntimeError(
                    f"Provider '{self.provider_id}' does not support tools"
                )
            params["tools"] = convert_chat_tools_to_responses_tools(request.tools)
        if request.tool_choice:
            if (
                request.tool_choice == "required"
                and not self.config.api_features.tool_choice_required
            ):
                params["tool_choice"] = "auto"
                diagnostics.append(
                    ProviderDiagnostic(
                        code="tool_choice_required_downgraded",
                        message=(
                            f"Provider '{self.provider_id}' does not declare required tool_choice support; "
                            "tool_choice was downgraded to auto."
                        ),
                    )
                )
            else:
                params["tool_choice"] = request.tool_choice
        qwen_compat = apply_openai_responses_qwen(
            self.config, request, params, diagnostics
        )
        if request.reasoning_effort and not qwen_compat:
            if self.config.api_features.reasoning_effort:
                reasoning: dict[str, Any] = {"effort": request.reasoning_effort}
                summary = self.config.extra.get("reasoning_summary", "auto")
                if summary:
                    reasoning["summary"] = str(summary)
                params["reasoning"] = reasoning
            else:
                diagnostics.append(
                    ProviderDiagnostic(
                        code="reasoning_effort_unsupported",
                        message=(
                            f"Provider '{self.provider_id}' does not declare reasoning_effort support; "
                            "the option was ignored."
                        ),
                    )
                )
        if (
            request.thinking_enabled is not None
            and not qwen_compat
            and not self.config.api_features.thinking
        ):
            diagnostics.append(
                ProviderDiagnostic(
                    code="thinking_unsupported",
                    message=(
                        f"Provider '{self.provider_id}' does not declare thinking support; "
                        "the option was ignored."
                    ),
                )
            )
        if diagnostics:
            request.metadata.setdefault("provider_diagnostics", []).extend(
                diagnostics
            )
        return params

    def chat(self, request: ProviderRequest) -> ProviderResponse:
        params = request.request_params or self.build_request_params(request)
        diagnostics = [
            item
            for item in request.metadata.get("provider_diagnostics", [])
            if isinstance(item, ProviderDiagnostic)
        ]
        stream = self.client.responses.create(**params)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tokens: list[str] = []
        debug_events: list[dict[str, Any]] = []
        tool_calls: dict[str, dict[str, str]] = {}
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens: int | None = None
        cache_write_tokens: int | None = None
        cost_usd: float | None = None
        usage_extra: dict[str, Any] = {}
        provider_response_id: str | None = None

        def _build_response(
            *,
            stream_status: str = "completed",
            interruption: dict[str, Any] | None = None,
            recovery: dict[str, Any] | None = None,
        ) -> ProviderResponse:
            parsed: list[ToolCall] = []
            tool_argument_diagnostics: list[dict[str, Any]] = []
            response_diagnostics = list(diagnostics)
            if stream_status == "completed":
                for index, raw in enumerate(tool_calls.values()):
                    tool_call_id = raw.get("id") or f"tool_call_{len(parsed)}"
                    tool_call, diagnostic, provider_diagnostic = parse_provider_tool_arguments(
                        index=index,
                        tool_call_id=tool_call_id,
                        tool_name=raw.get("name") or "",
                        raw_arguments=raw.get("args") or "",
                    )
                    if diagnostic:
                        tool_argument_diagnostics.append(diagnostic)
                    if provider_diagnostic:
                        response_diagnostics.append(provider_diagnostic)
                    parsed.append(tool_call)
            return ProviderResponse(
                content="".join(content_parts),
                reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
                tool_calls=parsed,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cost_usd=cost_usd,
                usage_extra=usage_extra,
                tokens=tokens,
                provider_response_id=provider_response_id,
                diagnostics=response_diagnostics,
                stream_status=stream_status,
                interruption=interruption,
                recovery=recovery,
                provider_extra={
                    "request_params": dict(params),
                    "debug_stream_events": debug_events,
                    "tool_argument_diagnostics": tool_argument_diagnostics,
                    "stream_partial": {"has_tool_delta": bool(tool_calls)},
                },
            )

        def _decode_event(_event_index: int, event: Any) -> None:
            nonlocal prompt_tokens
            nonlocal completion_tokens
            nonlocal cache_read_tokens
            nonlocal cache_write_tokens
            nonlocal cost_usd
            nonlocal usage_extra
            nonlocal provider_response_id
            event_type = getattr(event, "type", "")
            debug_events.append({"type": event_type})
            if event_type == "response.created":
                response = getattr(event, "response", None)
                provider_response_id = getattr(response, "id", None)
                return
            if event_type == "response.output_text.delta":
                delta = str(getattr(event, "delta", "") or "")
                if delta:
                    content_parts.append(delta)
                    tokens.append(delta)
                    if request.on_token is not None:
                        request.on_token(delta)
                return
            if event_type in {
                "response.reasoning_text.delta",
                "response.reasoning_summary_text.delta",
            }:
                delta = str(getattr(event, "delta", "") or "")
                if delta:
                    reasoning_parts.append(delta)
                    if request.on_reasoning_token is not None:
                        request.on_reasoning_token(delta)
                return
            if event_type == "response.output_item.added":
                item = getattr(event, "item", None)
                if getattr(item, "type", None) == "function_call":
                    item_id = str(getattr(item, "id", None) or getattr(item, "call_id", ""))
                    arguments = str(getattr(item, "arguments", "") or "")
                    name = str(getattr(item, "name", "") or "")
                    tool_calls[item_id] = {
                        "id": str(getattr(item, "call_id", item_id) or item_id),
                        "name": name,
                        "args": arguments,
                    }
                    emit_tool_call_delta(
                        request,
                        index=len(tool_calls) - 1,
                        tool_call_id=tool_calls[item_id]["id"],
                        tool_name=name,
                        arguments_delta=arguments,
                        arguments_preview=tool_arguments_preview(arguments),
                    )
                return
            if event_type == "response.function_call_arguments.delta":
                item_id = str(getattr(event, "item_id", "") or getattr(event, "call_id", ""))
                if item_id:
                    raw = tool_calls.setdefault(
                        item_id,
                        {
                            "id": str(getattr(event, "call_id", item_id) or item_id),
                            "name": "",
                            "args": "",
                        },
                    )
                    delta = str(getattr(event, "delta", "") or "")
                    raw["args"] += delta
                    emit_tool_call_delta(
                        request,
                        index=list(tool_calls).index(item_id),
                        tool_call_id=raw.get("id") or "",
                        tool_name=raw.get("name") or "",
                        arguments_delta=delta,
                        arguments_preview=tool_arguments_preview(raw.get("args", "")),
                    )
                return
            if event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                if getattr(item, "type", None) == "function_call":
                    item_id = str(getattr(item, "id", None) or getattr(item, "call_id", ""))
                    raw = tool_calls.setdefault(
                        item_id,
                        {
                            "id": str(getattr(item, "call_id", item_id) or item_id),
                            "name": "",
                            "args": "",
                        },
                    )
                    raw["id"] = str(getattr(item, "call_id", raw["id"]) or raw["id"])
                    raw["name"] = str(getattr(item, "name", raw["name"]) or raw["name"])
                    raw["args"] = str(
                        getattr(item, "arguments", raw["args"]) or raw["args"]
                    )
                return
            if event_type == "response.completed":
                response = getattr(event, "response", None)
                provider_response_id = getattr(response, "id", provider_response_id)
                usage = getattr(response, "usage", None)
                if usage is not None:
                    prompt_tokens = getattr(usage, "input_tokens", 0) or getattr(
                        usage, "prompt_tokens", 0
                    ) or 0
                    completion_tokens = getattr(
                        usage, "output_tokens", 0
                    ) or getattr(usage, "completion_tokens", 0) or 0
                    cache_read_tokens, cache_write_tokens, usage_extra = (
                        _extract_cache_usage(usage)
                    )
                    cost_usd = _usage_float(usage, "cost_usd")
                return
            if event_type == "error":
                error = getattr(event, "error", None)
                raise RuntimeError(str(getattr(error, "message", error)))

        StreamSupervisor(
            provider_id=self.config.id,
            provider_type=self.config.type,
            params=params,
            partial_response_factory=lambda: _build_response(
                stream_status="interrupted"
            ),
        ).consume(stream, _decode_event)

        return _build_response()

    def test(self, *, model: str, prompt: str = "ping") -> ProviderResponse:
        return self.chat(
            ProviderRequest(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=32,
            )
        )

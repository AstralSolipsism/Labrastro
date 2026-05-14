"""OpenAI-compatible Chat Completions provider adapter."""

from __future__ import annotations

import time
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

from reuleauxcoder.domain.config.models import ProviderConfig
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.providers.models import (
    ProviderDiagnostic,
    ProviderRequest,
    ProviderResponse,
)
from reuleauxcoder.services.providers.compat import (
    apply_openai_chat_reasoning,
    apply_openai_chat_thinking,
    apply_openai_chat_tool_choice,
    should_omit_openai_chat_temperature,
)
from reuleauxcoder.services.providers.tool_arguments import (
    parse_provider_tool_arguments,
)


MAX_DEBUG_STREAM_EVENTS = 200


def _reasoning_detail_to_dict(detail: Any) -> dict[str, Any]:
    if isinstance(detail, dict):
        return dict(detail)
    if hasattr(detail, "model_dump"):
        dumped = detail.model_dump()
        return dict(dumped) if isinstance(dumped, dict) else {}
    result: dict[str, Any] = {}
    for key in ("type", "text", "signature", "format", "index"):
        value = getattr(detail, key, None)
        if value is not None:
            result[key] = value
    return result


def _extract_stream_event(chunk: Any) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    choices = getattr(chunk, "choices", None) or []
    delta = choices[0].delta if choices else None
    if delta is not None:
        content = getattr(delta, "content", None)
        if content:
            events.append({"type": "content", "text": str(content)})
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            events.append({"type": "reasoning", "text": str(reasoning)})
        reasoning = getattr(delta, "reasoning", None)
        if reasoning:
            events.append({"type": "reasoning", "text": str(reasoning)})
        reasoning_details = getattr(delta, "reasoning_details", None) or []
        for detail in reasoning_details:
            detail_dict = _reasoning_detail_to_dict(detail)
            detail_type = detail_dict.get("type")
            text = detail_dict.get("text")
            if text:
                events.append(
                    {
                        "type": "reasoning_detail",
                        "detail_type": str(detail_type or ""),
                        "text": str(text),
                    }
                )
            signature = detail_dict.get("signature")
            if signature:
                events.append({"type": "reasoning_signature"})
        tool_calls = getattr(delta, "tool_calls", None) or []
        for tool_call in tool_calls:
            function = getattr(tool_call, "function", None)
            name = getattr(function, "name", None) if function is not None else None
            arguments = (
                getattr(function, "arguments", None) if function is not None else None
            )
            if name:
                events.append(
                    {
                        "type": "tool_name",
                        "text": str(name),
                        "index": getattr(tool_call, "index", None),
                    }
                )
            if arguments:
                events.append(
                    {
                        "type": "tool_arguments",
                        "text": str(arguments),
                        "index": getattr(tool_call, "index", None),
                    }
                )
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        events.append(
            {
                "type": "usage",
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            }
        )
    return events


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


def _first_int(*values: int | None) -> int | None:
    for value in values:
        if value is not None:
            return value
    return None


def _extract_cache_usage(usage: Any) -> tuple[int | None, int | None, dict[str, Any]]:
    details = _usage_attr(usage, "prompt_tokens_details") or _usage_attr(
        usage, "input_tokens_details"
    )
    prompt_cache_hit = _usage_int(usage, "prompt_cache_hit_tokens")
    prompt_cache_miss = _usage_int(usage, "prompt_cache_miss_tokens")
    cached = _first_int(_usage_int(details, "cached_tokens"), prompt_cache_hit)
    cache_creation = _first_int(
        _usage_int(details, "cache_creation_tokens"),
        prompt_cache_miss,
    )
    extra: dict[str, Any] = {}
    if details is not None:
        extra["prompt_tokens_details"] = (
            dict(details) if isinstance(details, dict) else _reasoning_detail_to_dict(details)
        )
    if prompt_cache_hit is not None or prompt_cache_miss is not None:
        extra["prompt_cache"] = {
            "hit_tokens": prompt_cache_hit,
            "miss_tokens": prompt_cache_miss,
        }
    return cached, cache_creation, extra


class OpenAIChatProvider:
    """Provider adapter for OpenAI-compatible Chat Completions APIs."""

    provider_type = "openai_chat"

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
        self.call_with_retry = self._call_with_retry

    def build_request_params(self, request: ProviderRequest) -> dict:
        diagnostics: list[ProviderDiagnostic] = []
        params: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "stream": True,
            "max_tokens": request.max_tokens,
        }
        if not should_omit_openai_chat_temperature(self.config):
            params["temperature"] = request.temperature
        apply_openai_chat_reasoning(self.config, request, params, diagnostics)
        apply_openai_chat_thinking(self.config, request, params, diagnostics)
        if request.tools:
            if not self.config.api_features.tools:
                raise RuntimeError(
                    f"Provider '{self.provider_id}' does not support tools"
                )
            params["tools"] = request.tools
        apply_openai_chat_tool_choice(self.config, request, params, diagnostics)
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
        debug_stream_events: list[dict[str, Any]] = []
        debug_stream_options_enabled = False
        try:
            try:
                params["stream_options"] = {"include_usage": True}
                stream = self.call_with_retry(params)
                debug_stream_options_enabled = True
            except Exception:
                params.pop("stream_options", None)
                stream = self.call_with_retry(params)

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tokens: list[str] = []
            tc_map: dict[int, dict] = {}
            prompt_tok = 0
            completion_tok = 0
            cache_read_tokens: int | None = None
            cache_write_tokens: int | None = None
            cost_usd: float | None = None
            usage_extra: dict[str, Any] = {}
            reasoning_signature: str | None = None
            reasoning_details_out: list[dict[str, Any]] = []

            for chunk in stream:
                if len(debug_stream_events) < MAX_DEBUG_STREAM_EVENTS:
                    debug_stream_events.extend(_extract_stream_event(chunk))
                    if len(debug_stream_events) > MAX_DEBUG_STREAM_EVENTS:
                        debug_stream_events = debug_stream_events[
                            :MAX_DEBUG_STREAM_EVENTS
                        ]
                usage = getattr(chunk, "usage", None)
                if usage:
                    prompt_tok = getattr(usage, "prompt_tokens", 0) or 0
                    completion_tok = getattr(usage, "completion_tokens", 0) or 0
                    cache_read_tokens, cache_write_tokens, usage_extra = (
                        _extract_cache_usage(usage)
                    )
                    cost_usd = _usage_float(usage, "cost_usd")
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = choices[0].delta
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                    tokens.append(delta.content)
                    if request.on_token is not None:
                        request.on_token(delta.content)
                if getattr(delta, "reasoning_content", None):
                    reasoning_parts.append(delta.reasoning_content)
                if getattr(delta, "reasoning", None):
                    reasoning_parts.append(delta.reasoning)
                reasoning_details = getattr(delta, "reasoning_details", None) or []
                for detail in reasoning_details:
                    detail_dict = _reasoning_detail_to_dict(detail)
                    if detail_dict:
                        reasoning_details_out.append(detail_dict)
                    text = detail_dict.get("text")
                    if text:
                        reasoning_parts.append(str(text))
                    signature = detail_dict.get("signature")
                    if signature and reasoning_signature is None:
                        reasoning_signature = str(signature)
                if getattr(delta, "tool_calls", None):
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tc_map:
                            tc_map[idx] = {"id": "", "name": "", "args": ""}
                        if tc_delta.id:
                            tc_map[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tc_map[idx]["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                tc_map[idx]["args"] += tc_delta.function.arguments

            parsed: list[ToolCall] = []
            tool_argument_diagnostics: list[dict[str, Any]] = []
            for idx in sorted(tc_map):
                raw = tc_map[idx]
                tool_call_id = raw.get("id") or f"tool_call_{idx}"
                raw_args = raw.get("args", "")
                tool_name = str(raw.get("name") or "")
                tool_call, diagnostic, provider_diagnostic = parse_provider_tool_arguments(
                    index=idx,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    raw_arguments=str(raw_args),
                )
                if diagnostic:
                    tool_argument_diagnostics.append(diagnostic)
                if provider_diagnostic:
                    diagnostics.append(provider_diagnostic)
                parsed.append(tool_call)

            return ProviderResponse(
                content="".join(content_parts),
                reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
                reasoning_signature=reasoning_signature,
                reasoning_details=reasoning_details_out,
                tool_calls=parsed,
                prompt_tokens=prompt_tok,
                completion_tokens=completion_tok,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cost_usd=cost_usd,
                usage_extra=usage_extra,
                tokens=tokens,
                diagnostics=diagnostics,
                provider_extra={
                    "request_params": dict(params),
                    "debug_stream_events": debug_stream_events,
                    "stream_options_enabled": debug_stream_options_enabled,
                    "tool_argument_diagnostics": tool_argument_diagnostics,
                },
            )
        finally:
            request.metadata.pop("provider_diagnostics", None)

    def test(self, *, model: str, prompt: str = "ping") -> ProviderResponse:
        return self.chat(
            ProviderRequest(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=32,
            )
        )

    def _call_with_retry(self, params: dict):
        max_retries = self.config.max_retries
        retried_without_temperature = False
        attempt = 0
        while True:
            try:
                return self.client.chat.completions.create(**params)
            except BadRequestError as exc:
                message = str(exc).lower()
                if (
                    not retried_without_temperature
                    and "temperature" in message
                    and "temperature" in params
                ):
                    params.pop("temperature", None)
                    retried_without_temperature = True
                    continue
                raise
            except (RateLimitError, APITimeoutError, APIConnectionError):
                if attempt >= max_retries:
                    raise
                time.sleep(2**attempt)
                attempt += 1

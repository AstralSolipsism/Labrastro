"""OpenAI-compatible Chat Completions provider adapter."""

from __future__ import annotations

import time
import base64
import contextvars
from typing import Any

import httpx
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
from reuleauxcoder.services.providers.stream_supervisor import (
    ProviderStreamInterruptedError,
    StreamLivenessLimits,
    StreamSupervisor,
)
from reuleauxcoder.services.providers.tool_call_delta import (
    emit_tool_call_delta,
    tool_arguments_preview,
)
from reuleauxcoder.services.providers.tool_arguments import (
    parse_provider_tool_arguments,
)


MAX_DEBUG_STREAM_EVENTS = 200
_DEBUG_HTTP_CHUNK_SINK: contextvars.ContextVar[list[dict[str, Any]] | None] = (
    contextvars.ContextVar("openai_chat_debug_http_chunk_sink", default=None)
)


def _safe_response_headers(headers: httpx.Headers) -> dict[str, str]:
    allowed = {
        "content-type",
        "date",
        "server",
        "x-request-id",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "cf-ray",
    }
    return {key: value for key, value in headers.items() if key.lower() in allowed}


def _safe_request_headers(headers: httpx.Headers) -> dict[str, str]:
    allowed = {
        "accept",
        "content-type",
        "user-agent",
    }
    return {key: value for key, value in headers.items() if key.lower() in allowed}


def _exception_summary(exc: Exception) -> dict[str, Any]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
    }


def _safe_setattr(obj: Any, name: str, value: Any) -> None:
    try:
        setattr(obj, name, value)
    except Exception:
        pass


def _attach_provider_exception_diagnostics(
    exc: Exception,
    *,
    config: ProviderConfig,
    params: dict[str, Any],
    phase: str,
    attempts: list[dict[str, Any]],
    stream_options_enabled: bool,
    debug_http_chunks: list[dict[str, Any]],
) -> None:
    effective_attempts = list(attempts)
    if not effective_attempts:
        existing_attempts = getattr(exc, "provider_retry_attempts", None)
        if isinstance(existing_attempts, list):
            effective_attempts = list(existing_attempts)
    _safe_setattr(exc, "provider_id", config.id)
    _safe_setattr(exc, "provider_type", config.type)
    _safe_setattr(exc, "provider_base_url", config.base_url)
    _safe_setattr(exc, "provider_timeout_sec", config.timeout_sec)
    _safe_setattr(exc, "provider_max_retries", config.max_retries)
    _safe_setattr(exc, "provider_error_phase", phase)
    _safe_setattr(exc, "provider_retry_attempts", effective_attempts)
    _safe_setattr(exc, "provider_request_params", dict(params))
    _safe_setattr(exc, "provider_stream_options_enabled", stream_options_enabled)
    if debug_http_chunks:
        _safe_setattr(exc, "provider_debug_http_chunks", list(debug_http_chunks))


def _stream_options_unsupported(exc: BadRequestError) -> bool:
    message = str(exc).lower()
    if "stream_options" not in message and "include_usage" not in message:
        return False
    unsupported_markers = (
        "unsupported",
        "not support",
        "unrecognized",
        "unknown",
        "extra inputs",
        "invalid",
        "not permitted",
    )
    return any(marker in message for marker in unsupported_markers)


class _DebugByteStream(httpx.SyncByteStream):
    def __init__(
        self,
        stream: httpx.SyncByteStream,
        sink: list[dict[str, Any]],
    ) -> None:
        self._stream = stream
        self._sink = sink

    def __iter__(self):
        for chunk in self._stream:
            self._sink.append(
                {
                    "type": "response_body_chunk",
                    "index": len(self._sink),
                    "byte_length": len(chunk),
                    "text": chunk.decode("utf-8", errors="replace"),
                    "base64": base64.b64encode(chunk).decode("ascii"),
                }
            )
            yield chunk

    def close(self) -> None:
        self._stream.close()


class _DebugHTTPTransport(httpx.HTTPTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        sink = _DEBUG_HTTP_CHUNK_SINK.get()
        if sink is not None:
            sink.append(
                {
                    "type": "request_start",
                    "method": request.method,
                    "url_path": request.url.path,
                    "headers": _safe_request_headers(request.headers),
                }
            )
        try:
            response = super().handle_request(request)
        except Exception as exc:
            if sink is not None:
                sink.append(
                    {
                        "type": "request_error",
                        "method": request.method,
                        "url_path": request.url.path,
                        "error": _exception_summary(exc),
                    }
                )
            raise
        if sink is None:
            return response
        sink.append(
            {
                "type": "response_start",
                "method": request.method,
                "url_path": request.url.path,
                "status_code": response.status_code,
                "headers": _safe_response_headers(response.headers),
            }
        )
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_DebugByteStream(response.stream, sink),
            extensions=response.extensions,
            request=request,
        )


def _debug_value_to_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 10:
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _debug_value_to_json(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_debug_value_to_json(item, depth=depth + 1) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _debug_value_to_json(
                value.model_dump(mode="json"),
                depth=depth + 1,
            )
        except TypeError:
            try:
                return _debug_value_to_json(value.model_dump(), depth=depth + 1)
            except Exception:
                pass
        except Exception:
            pass
    for method_name in ("to_dict_recursive", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _debug_value_to_json(method(), depth=depth + 1)
            except Exception:
                pass
    if hasattr(value, "__dict__"):
        return {
            key: _debug_value_to_json(item, depth=depth + 1)
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item)
        }
    return repr(value)


def _chunk_to_debug_dict(chunk: Any, chunk_index: int) -> dict[str, Any]:
    value = _debug_value_to_json(chunk)
    if isinstance(value, dict):
        result = dict(value)
    else:
        result = {"value": value}
    result["_chunk_index"] = chunk_index
    return result


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
        client_kwargs["http_client"] = httpx.Client(
            transport=_DebugHTTPTransport(),
            timeout=config.timeout_sec,
        )
        self.client = OpenAI(**client_kwargs)
        self.call_with_retry = self._call_with_retry

    def build_request_params(self, request: ProviderRequest) -> dict:
        diagnostics: list[ProviderDiagnostic] = []
        params: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages,
            "stream": True,
        }
        if request.max_tokens > 0:
            params["max_tokens"] = request.max_tokens
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
        debug_raw_stream_chunks: list[dict[str, Any]] = []
        debug_http_chunks: list[dict[str, Any]] = []
        retry_attempts: list[dict[str, Any]] = []
        capture_debug_chunks = bool(request.metadata.get("llm_debug_raw_chunks"))
        debug_stream_options_enabled = False
        debug_http_token = (
            _DEBUG_HTTP_CHUNK_SINK.set(debug_http_chunks)
            if capture_debug_chunks
            else None
        )
        try:
            try:
                params["stream_options"] = {"include_usage": True}
                stream = self.call_with_retry(
                    params,
                    attempts=retry_attempts,
                    phase="request_start",
                )
                debug_stream_options_enabled = True
            except BadRequestError as exc:
                if not _stream_options_unsupported(exc):
                    _attach_provider_exception_diagnostics(
                        exc,
                        config=self.config,
                        params=params,
                        phase="request_start",
                        attempts=retry_attempts,
                        stream_options_enabled=False,
                        debug_http_chunks=debug_http_chunks,
                    )
                    raise
                retry_attempts.append(
                    {
                        "attempt": len(retry_attempts) + 1,
                        "phase": "request_start",
                        "stream_options": True,
                        "error": _exception_summary(exc),
                        "action": "retry_without_stream_options",
                    }
                )
                params.pop("stream_options", None)
                stream = self.call_with_retry(
                    params,
                    attempts=retry_attempts,
                    phase="request_start",
                )
                debug_stream_options_enabled = False

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
                            response_diagnostics.append(provider_diagnostic)
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
                    diagnostics=response_diagnostics,
                    stream_status=stream_status,
                    interruption=interruption,
                    recovery=recovery,
                    provider_extra={
                        "request_params": dict(params),
                        "debug_stream_events": debug_stream_events,
                        "debug_raw_stream_chunks": debug_raw_stream_chunks,
                        "debug_http_chunks": debug_http_chunks,
                        "stream_options_enabled": debug_stream_options_enabled,
                        "retry_attempts": retry_attempts,
                        "tool_argument_diagnostics": tool_argument_diagnostics,
                        "stream_partial": {"has_tool_delta": bool(tc_map)},
                    },
                )

            def _decode_chunk(chunk_index: int, chunk: Any) -> None:
                nonlocal prompt_tok
                nonlocal completion_tok
                nonlocal cache_read_tokens
                nonlocal cache_write_tokens
                nonlocal cost_usd
                nonlocal usage_extra
                nonlocal reasoning_signature
                nonlocal debug_stream_events
                if capture_debug_chunks:
                    debug_raw_stream_chunks.append(
                        _chunk_to_debug_dict(chunk, chunk_index)
                    )
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
                    return
                delta = choices[0].delta
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                    tokens.append(delta.content)
                    if request.on_token is not None:
                        request.on_token(delta.content)
                if getattr(delta, "reasoning_content", None):
                    reasoning_parts.append(delta.reasoning_content)
                    if request.on_reasoning_token is not None:
                        request.on_reasoning_token(delta.reasoning_content)
                if getattr(delta, "reasoning", None):
                    reasoning_parts.append(delta.reasoning)
                    if request.on_reasoning_token is not None:
                        request.on_reasoning_token(delta.reasoning)
                reasoning_details = getattr(delta, "reasoning_details", None) or []
                for detail in reasoning_details:
                    detail_dict = _reasoning_detail_to_dict(detail)
                    if detail_dict:
                        reasoning_details_out.append(detail_dict)
                    text = detail_dict.get("text")
                    if text:
                        reasoning_text = str(text)
                        reasoning_parts.append(reasoning_text)
                        if request.on_reasoning_token is not None:
                            request.on_reasoning_token(reasoning_text)
                    signature = detail_dict.get("signature")
                    if signature and reasoning_signature is None:
                        reasoning_signature = str(signature)
                if getattr(delta, "tool_calls", None):
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tc_map:
                            tc_map[idx] = {"id": "", "name": "", "args": ""}
                        name_changed = False
                        args_delta = ""
                        if tc_delta.id:
                            tc_map[idx]["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                tc_map[idx]["name"] = tc_delta.function.name
                                name_changed = True
                            if tc_delta.function.arguments:
                                args_delta = str(tc_delta.function.arguments)
                                tc_map[idx]["args"] += args_delta
                        if name_changed or args_delta:
                            emit_tool_call_delta(
                                request,
                                index=idx,
                                tool_call_id=tc_map[idx].get("id") or "",
                                tool_name=tc_map[idx].get("name") or "",
                                arguments_delta=args_delta,
                                arguments_preview=tool_arguments_preview(
                                    tc_map[idx].get("args", "")
                                ),
                            )

            StreamSupervisor(
                provider_id=self.config.id,
                provider_type=self.config.type,
                params=params,
                attempts=retry_attempts,
                stream_options_enabled=debug_stream_options_enabled,
                debug_http_chunks=debug_http_chunks,
                liveness_limits=StreamLivenessLimits.from_config(self.config),
                partial_response_factory=lambda: _build_response(
                    stream_status="interrupted"
                ),
            ).consume(stream, _decode_chunk)

            return _build_response()
        except ProviderStreamInterruptedError:
            raise
        except Exception as exc:
            _attach_provider_exception_diagnostics(
                exc,
                config=self.config,
                params=params,
                phase=getattr(exc, "provider_error_phase", None) or "request_start",
                attempts=retry_attempts,
                stream_options_enabled=debug_stream_options_enabled,
                debug_http_chunks=debug_http_chunks,
            )
            raise
        finally:
            if debug_http_token is not None:
                _DEBUG_HTTP_CHUNK_SINK.reset(debug_http_token)
            request.metadata.pop("provider_diagnostics", None)

    def test(self, *, model: str, prompt: str = "ping") -> ProviderResponse:
        return self.chat(
            ProviderRequest(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=32,
            )
        )

    def _call_with_retry(
        self,
        params: dict,
        *,
        attempts: list[dict[str, Any]] | None = None,
        phase: str = "request_start",
    ):
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
                    if attempts is not None:
                        attempts.append(
                            {
                                "attempt": len(attempts) + 1,
                                "phase": phase,
                                "stream_options": bool(params.get("stream_options")),
                                "error": _exception_summary(exc),
                                "action": "retry_without_temperature",
                            }
                        )
                    params.pop("temperature", None)
                    retried_without_temperature = True
                    continue
                raise
            except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
                record: dict[str, Any] = {
                    "attempt": len(attempts or []) + 1,
                    "phase": phase,
                    "stream_options": bool(params.get("stream_options")),
                    "error": _exception_summary(exc),
                }
                if attempt >= max_retries:
                    record["action"] = "raise"
                    if attempts is not None:
                        attempts.append(record)
                    _attach_provider_exception_diagnostics(
                        exc,
                        config=self.config,
                        params=params,
                        phase=phase,
                        attempts=attempts or [record],
                        stream_options_enabled=bool(params.get("stream_options")),
                        debug_http_chunks=[],
                    )
                    raise
                delay = 2**attempt
                record["action"] = "retry"
                record["sleep_sec"] = delay
                if attempts is not None:
                    attempts.append(record)
                time.sleep(delay)
                attempt += 1

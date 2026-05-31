"""Labrastro server-origin provider adapter."""

from __future__ import annotations

import json
import os
from typing import Any, Iterable

import httpx

from reuleauxcoder.domain.config.models import ProviderConfig
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.providers.models import (
    ProviderDiagnostic,
    ProviderRequest,
    ProviderResponse,
)
from reuleauxcoder.services.providers.stream_supervisor import (
    ProviderStreamInterruptedError,
    StreamSupervisor,
)


class LabrastroServerProvider:
    """Provider adapter that delegates LLM requests back to Labrastro server."""

    provider_type = "labrastro_server"

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.provider_id = config.id
        self.base_url = _required_env("LABRASTRO_REMOTE_BASE_URL").rstrip("/")
        self.peer_token = _required_env("LABRASTRO_PEER_TOKEN")
        self.agent_run_id = _required_env("LABRASTRO_AGENT_RUN_ID")
        self.request_id = _required_env("LABRASTRO_AGENT_RUN_REQUEST_ID")
        self.worker_id = _required_env("LABRASTRO_AGENT_RUN_WORKER_ID")
        self.client = httpx.Client(timeout=_server_origin_timeout(config.timeout_sec))

    def build_request_params(self, request: ProviderRequest) -> dict[str, Any]:
        return {
            "endpoint": "/remote/agent-runs/model-request",
            "agent_run_id": self.agent_run_id,
            "request_id": self.request_id,
            "worker_id": self.worker_id,
            "model": request.model,
            "stream": True,
        }

    def chat(self, request: ProviderRequest) -> ProviderResponse:
        request_params = self.build_request_params(request)
        payload = {
            "peer_token": self.peer_token,
            "agent_run_id": self.agent_run_id,
            "request_id": self.request_id,
            "worker_id": self.worker_id,
            "model": request.model,
            "messages": list(request.messages),
            "tools": list(request.tools),
            "parameters": {
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "reasoning_effort": request.reasoning_effort,
                "thinking_enabled": request.thinking_enabled,
                "tool_choice": request.tool_choice,
            },
            "metadata": dict(request.metadata),
            "stream": True,
        }
        url = f"{self.base_url}/remote/agent-runs/model-request"
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_delta_seen = False

        def partial_response() -> ProviderResponse:
            extra: dict[str, Any] = {}
            if tool_delta_seen:
                extra["stream_partial"] = {"has_tool_delta": True}
            return ProviderResponse(
                content="".join(content_parts),
                reasoning_content="".join(reasoning_parts) or None,
                tokens=list(content_parts),
                provider_extra=extra,
            )

        with self.client.stream("POST", url, json=payload) as response:
            if response.status_code >= 400:
                raise RuntimeError(_response_error(response))
            final: ProviderResponse | None = None
            server_error: RuntimeError | None = None
            supervisor = StreamSupervisor(
                provider_id=self.config.id,
                provider_type=self.config.type,
                params=request_params,
                partial_response_factory=partial_response,
            )

            def decode_event(_index: int, item: tuple[str, str]) -> None:
                nonlocal final, server_error, tool_delta_seen
                event, data = item
                if event == "heartbeat":
                    return
                if event == "token":
                    text = str(_json_data(data).get("text") or "")
                    if text:
                        content_parts.append(text)
                    if text and request.on_token is not None:
                        request.on_token(text)
                    return
                if event == "reasoning_token":
                    text = str(_json_data(data).get("text") or "")
                    if text:
                        reasoning_parts.append(text)
                    if text and request.on_reasoning_token is not None:
                        request.on_reasoning_token(text)
                    return
                if event == "tool_call_delta":
                    delta = _json_data(data)
                    tool_delta_seen = True
                    if request.on_tool_call_delta is not None:
                        request.on_tool_call_delta(delta)
                    return
                if event == "done":
                    final = _provider_response_from_dict(_json_data(data))
                    return
                if event == "interrupted":
                    interrupted = _json_data(data)
                    raise _provider_stream_interrupted(interrupted, partial_response())
                if event == "error":
                    error = _json_data(data)
                    server_error = RuntimeError(
                        str(error.get("message") or error.get("error") or "provider_request_failed")
                    )
                    return
                server_error = RuntimeError(f"unexpected labrastro_server model event: {event}")

            supervisor.consume(_iter_sse_events(response), decode_event)
            if server_error is not None:
                raise server_error
            if final is None:
                message = "labrastro_server provider stream ended without final response"
                partial = partial_response()
                interruption = {
                    "phase": "stream_complete",
                    "classification": "empty_interrupted" if not partial.content and not partial.reasoning_content else "text_interrupted",
                    "recoverable": True,
                    "partial_kind": "text" if partial.content else ("reasoning" if partial.reasoning_content else "empty"),
                    "retry_action": "continue" if partial.content or partial.reasoning_content else "retry",
                    "error_type": "StreamEndedWithoutFinalResponse",
                    "message": message,
                }
                partial.stream_status = "interrupted"
                partial.interruption = interruption
                partial.recovery = {
                    "attempted": False,
                    "action": interruption["retry_action"],
                    "attempt": 0,
                    "max_attempts": 1,
                }
                raise ProviderStreamInterruptedError(
                    message,
                    original_error=RuntimeError(message),
                    partial_response=partial,
                    interruption=interruption,
                )
            return final

    def test(self, *, model: str, prompt: str = "ping") -> ProviderResponse:
        return self.chat(
            ProviderRequest(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
        )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for labrastro_server provider")
    return value


def _server_origin_timeout(timeout_sec: int | float | None) -> httpx.Timeout:
    timeout = float(timeout_sec or 120)
    return httpx.Timeout(connect=timeout, read=None, write=timeout, pool=timeout)


def _iter_sse_events(response: httpx.Response) -> Iterable[tuple[str, str]]:
    event = "message"
    data: list[str] = []
    for line in response.iter_lines():
        if not line:
            if data:
                yield event, "\n".join(data)
            event = "message"
            data = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:") :].strip() or "message"
            continue
        if line.startswith("data:"):
            data.append(line[len("data:") :].lstrip())
    if data:
        yield event, "\n".join(data)


def _json_data(data: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except json.JSONDecodeError:
        return {"message": data}
    return value if isinstance(value, dict) else {"value": value}


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.read().decode("utf-8", errors="replace").strip()
    except Exception:
        payload = ""
    return payload or f"HTTP {response.status_code}"


def _provider_response_from_dict(data: dict[str, Any]) -> ProviderResponse:
    return ProviderResponse(
        content=str(data.get("content") or ""),
        reasoning_content=(
            str(data["reasoning_content"])
            if data.get("reasoning_content") is not None
            else None
        ),
        reasoning_signature=(
            str(data["reasoning_signature"])
            if data.get("reasoning_signature") is not None
            else None
        ),
        reasoning_details=[
            dict(item) for item in data.get("reasoning_details", []) if isinstance(item, dict)
        ]
        if isinstance(data.get("reasoning_details"), list)
        else [],
        tool_calls=[
            _tool_call_from_dict(item)
            for item in data.get("tool_calls", [])
            if isinstance(item, dict)
        ]
        if isinstance(data.get("tool_calls"), list)
        else [],
        prompt_tokens=int(data.get("prompt_tokens") or 0),
        completion_tokens=int(data.get("completion_tokens") or 0),
        cache_read_tokens=(
            int(data["cache_read_tokens"])
            if data.get("cache_read_tokens") is not None
            else None
        ),
        cache_write_tokens=(
            int(data["cache_write_tokens"])
            if data.get("cache_write_tokens") is not None
            else None
        ),
        cost_usd=(
            float(data["cost_usd"])
            if data.get("cost_usd") is not None
            else None
        ),
        usage_extra=dict(data.get("usage_extra") or {})
        if isinstance(data.get("usage_extra"), dict)
        else {},
        tokens=[str(item) for item in data.get("tokens", [])]
        if isinstance(data.get("tokens"), list)
        else [],
        provider_response_id=(
            str(data["provider_response_id"])
            if data.get("provider_response_id") is not None
            else None
        ),
        provider_extra=dict(data.get("provider_extra") or {})
        if isinstance(data.get("provider_extra"), dict)
        else {},
        diagnostics=[
            ProviderDiagnostic(
                code=str(item.get("code") or ""),
                message=str(item.get("message") or ""),
                level=str(item.get("level") or "warning"),
            )
            for item in data.get("diagnostics", [])
            if isinstance(item, dict)
        ]
        if isinstance(data.get("diagnostics"), list)
        else [],
        stream_status=str(data.get("stream_status") or "completed"),
        interruption=dict(data.get("interruption"))
        if isinstance(data.get("interruption"), dict)
        else None,
        recovery=dict(data.get("recovery"))
        if isinstance(data.get("recovery"), dict)
        else None,
    )


def _provider_stream_interrupted(
    data: dict[str, Any],
    fallback_partial: ProviderResponse,
) -> ProviderStreamInterruptedError:
    partial_data = data.get("partial_response")
    partial = (
        _provider_response_from_dict(partial_data)
        if isinstance(partial_data, dict)
        else _provider_response_from_dict(data)
    )
    if not (
        partial.content
        or partial.reasoning_content
        or partial.tool_calls
        or partial.prompt_tokens
        or partial.completion_tokens
        or partial.provider_extra
    ):
        partial = fallback_partial
    interruption = (
        dict(data.get("interruption"))
        if isinstance(data.get("interruption"), dict)
        else dict(partial.interruption or {})
    )
    message = str(
        data.get("message")
        or data.get("error")
        or interruption.get("message")
        or "Provider stream interrupted."
    )
    if not interruption:
        interruption = {
            "phase": "stream_iterate",
            "classification": "text_interrupted" if partial.content else "empty_interrupted",
            "recoverable": True,
            "partial_kind": "text" if partial.content else "empty",
            "retry_action": "continue" if partial.content else "retry",
            "error_type": "RemoteStreamInterrupted",
            "message": message,
        }
    partial.stream_status = "interrupted"
    partial.interruption = interruption
    partial.recovery = (
        dict(data.get("recovery"))
        if isinstance(data.get("recovery"), dict)
        else {
            "attempted": False,
            "action": interruption.get("retry_action") or "retry",
            "attempt": 0,
            "max_attempts": 1,
        }
    )
    return ProviderStreamInterruptedError(
        message,
        original_error=RuntimeError(message),
        partial_response=partial,
        interruption=interruption,
    )


def _tool_call_from_dict(data: dict[str, Any]) -> ToolCall:
    arguments = data.get("arguments")
    function = data.get("function")
    function_data = function if isinstance(function, dict) else {}
    if arguments is None:
        arguments = function_data.get("arguments")
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            arguments = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            arguments = {}
    return ToolCall(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or function_data.get("name") or ""),
        arguments=dict(arguments) if isinstance(arguments, dict) else {},
        argument_error=(
            str(data["argument_error"])
            if data.get("argument_error") is not None
            else None
        ),
        argument_diagnostics=[
            dict(item)
            for item in data.get("argument_diagnostics", [])
            if isinstance(item, dict)
        ]
        if isinstance(data.get("argument_diagnostics"), list)
        else [],
    )

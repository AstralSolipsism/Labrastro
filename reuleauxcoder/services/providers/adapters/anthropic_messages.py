"""Anthropic Messages provider adapter."""

from __future__ import annotations

import json
from typing import Any

from reuleauxcoder.domain.config.models import ProviderConfig
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.providers.models import (
    ProviderDiagnostic,
    ProviderRequest,
    ProviderResponse,
)
from reuleauxcoder.services.providers.compat import (
    apply_anthropic_reasoning_effort,
    deepseek_anthropic_budget_is_provider_managed,
)
from reuleauxcoder.services.providers.stream_supervisor import StreamSupervisor
from reuleauxcoder.services.providers.tool_call_delta import (
    emit_tool_call_delta,
    tool_arguments_preview,
)
from reuleauxcoder.services.providers.tool_arguments import (
    parse_provider_tool_arguments,
)


def convert_chat_tools_to_anthropic_tools(tools: list[dict[str, Any]]) -> list[dict]:
    converted: list[dict] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            continue
        converted.append(
            {
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters", {"type": "object"}),
            }
        )
    return converted


def convert_messages_to_anthropic(
    messages: list[dict[str, Any]]
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            content = str(message.get("content") or "")
            if content:
                system_parts.append(content)
            continue
        if role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id", ""),
                            "content": str(message.get("content") or ""),
                        }
                    ],
                }
            )
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            reasoning = message.get("reasoning_content")
            if reasoning:
                block: dict[str, Any] = {"type": "thinking", "thinking": str(reasoning)}
                signature = message.get("reasoning_signature")
                if signature:
                    block["signature"] = str(signature)
                blocks.append(block)
            content = message.get("content")
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for tool_call in message.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_call.get("id", ""),
                        "name": function.get("name", ""),
                        "input": arguments,
                    }
                )
            if blocks:
                converted.append({"role": "assistant", "content": blocks})
            continue
        if role == "user":
            converted.append(
                {"role": "user", "content": str(message.get("content") or "")}
            )
    return ("\n\n".join(system_parts) if system_parts else None), converted


def _usage_int(obj: Any, name: str) -> int | None:
    if obj is None:
        return None
    value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usage_float(obj: Any, name: str) -> float | None:
    if obj is None:
        return None
    value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
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
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
        if (value := getattr(obj, key, None)) is not None
    }


class AnthropicMessagesProvider:
    """Provider adapter for Anthropic Messages API."""

    provider_type = "anthropic_messages"

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.provider_id = config.id
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover - dependency smoke
            raise RuntimeError(
                "anthropic provider requires the 'anthropic' package"
            ) from exc
        client_kwargs: dict[str, Any] = {
            "api_key": config.api_key,
            "timeout": config.timeout_sec,
        }
        if config.base_url:
            client_kwargs["base_url"] = config.base_url
        if config.headers:
            client_kwargs["default_headers"] = config.headers
        self.client = Anthropic(**client_kwargs)

    def build_request_params(self, request: ProviderRequest) -> dict:
        diagnostics: list[ProviderDiagnostic] = []
        system, messages = convert_messages_to_anthropic(request.messages)
        provider_manages_budget = deepseek_anthropic_budget_is_provider_managed(
            self.config
        )
        params: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "stream": True,
        }
        if request.max_tokens < 1:
            raise RuntimeError("max_tokens is required for Anthropic messages providers")
        params["max_tokens"] = request.max_tokens
        if system:
            params["system"] = system
        if request.tools:
            if not self.config.api_features.tools:
                raise RuntimeError(
                    f"Provider '{self.provider_id}' does not support tools"
                )
            params["tools"] = convert_chat_tools_to_anthropic_tools(request.tools)
        if request.tool_choice:
            if request.tool_choice == "required":
                if self.config.api_features.tool_choice_required:
                    params["tool_choice"] = {"type": "any"}
                else:
                    params["tool_choice"] = {"type": "auto"}
                    diagnostics.append(
                        ProviderDiagnostic(
                            code="tool_choice_required_downgraded",
                            message=(
                                f"Provider '{self.provider_id}' does not declare required tool_choice support; "
                                "tool_choice was downgraded to auto."
                            ),
                        )
                    )
            elif request.tool_choice == "auto":
                params["tool_choice"] = {"type": "auto"}
        apply_anthropic_reasoning_effort(self.config, request, params, diagnostics)
        if request.thinking_enabled is not None:
            if self.config.api_features.thinking:
                if request.thinking_enabled:
                    budget = int(self.config.extra.get("thinking_budget_tokens", 1024))
                    if not provider_manages_budget and request.max_tokens <= 1024:
                        params["max_tokens"] = 1025
                        budget = 1024
                        diagnostics.append(
                            ProviderDiagnostic(
                                code="thinking_budget_adjusted",
                                message=(
                                    f"Provider '{self.provider_id}' requires thinking budget "
                                    "to be at least 1024 and lower than max_tokens; "
                                    "max_tokens was raised to 1025."
                                ),
                            )
                        )
                    params["thinking"] = {
                        "type": "enabled",
                        "budget_tokens": (
                            budget
                            if provider_manages_budget
                            else min(max(1024, budget), int(params["max_tokens"]) - 1)
                        ),
                    }
                    if not provider_manages_budget and request.temperature != 1.0:
                        diagnostics.append(
                            ProviderDiagnostic(
                                code="temperature_omitted_for_thinking",
                                message=(
                                    f"Provider '{self.provider_id}' does not allow custom "
                                    "temperature when Anthropic thinking is enabled; "
                                    "temperature was omitted."
                                ),
                            )
                        )
                else:
                    params["thinking"] = {"type": "disabled"}
            else:
                diagnostics.append(
                    ProviderDiagnostic(
                        code="thinking_unsupported",
                        message=(
                            f"Provider '{self.provider_id}' does not declare thinking support; "
                            "the option was ignored."
                        ),
                    )
                )
        if params.get("thinking", {}).get("type") != "enabled":
            params["temperature"] = request.temperature
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
        stream = self.client.messages.create(**params)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tokens: list[str] = []
        debug_events: list[dict[str, Any]] = []
        tool_blocks: dict[int, dict[str, Any]] = {}
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens: int | None = None
        cache_write_tokens: int | None = None
        cost_usd: float | None = None
        usage_extra: dict[str, Any] = {}
        reasoning_signature: str | None = None

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
                for index, raw in enumerate(tool_blocks.values()):
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
                reasoning_signature=reasoning_signature,
                tool_calls=parsed,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
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
                    "debug_stream_events": debug_events,
                    "tool_argument_diagnostics": tool_argument_diagnostics,
                    "stream_partial": {"has_tool_delta": bool(tool_blocks)},
                },
            )

        def _decode_event(_event_index: int, event: Any) -> None:
            nonlocal prompt_tokens
            nonlocal completion_tokens
            nonlocal cache_read_tokens
            nonlocal cache_write_tokens
            nonlocal cost_usd
            nonlocal usage_extra
            nonlocal reasoning_signature
            event_type = str(getattr(event, "type", "") or "")
            debug_events.append({"type": event_type})
            if event_type == "content_block_start":
                index = int(getattr(event, "index", len(tool_blocks)) or 0)
                block = getattr(event, "content_block", None)
                if getattr(block, "type", None) == "tool_use":
                    name = str(getattr(block, "name", "") or "")
                    tool_blocks[index] = {
                        "id": str(getattr(block, "id", "") or ""),
                        "name": name,
                        "args": "",
                    }
                    emit_tool_call_delta(
                        request,
                        index=index,
                        tool_call_id=tool_blocks[index]["id"],
                        tool_name=name,
                        arguments_delta="",
                        arguments_preview="",
                    )
                return
            if event_type == "content_block_delta":
                index = int(getattr(event, "index", 0) or 0)
                delta = getattr(event, "delta", None)
                delta_type = str(getattr(delta, "type", "") or "")
                if delta_type == "text_delta":
                    text = str(getattr(delta, "text", "") or "")
                    if text:
                        content_parts.append(text)
                        tokens.append(text)
                        if request.on_token is not None:
                            request.on_token(text)
                elif delta_type == "thinking_delta":
                    thinking = str(getattr(delta, "thinking", "") or "")
                    if thinking:
                        reasoning_parts.append(thinking)
                        if request.on_reasoning_token is not None:
                            request.on_reasoning_token(thinking)
                elif delta_type == "signature_delta":
                    reasoning_signature = str(getattr(delta, "signature", "") or "")
                elif delta_type == "input_json_delta":
                    raw = tool_blocks.setdefault(
                        index,
                        {"id": f"tool_call_{index}", "name": "", "args": ""},
                    )
                    partial_json = str(getattr(delta, "partial_json", "") or "")
                    raw["args"] += partial_json
                    emit_tool_call_delta(
                        request,
                        index=index,
                        tool_call_id=raw.get("id") or "",
                        tool_name=raw.get("name") or "",
                        arguments_delta=partial_json,
                        arguments_preview=tool_arguments_preview(raw.get("args", "")),
                    )
                return
            if event_type == "message_delta":
                usage = getattr(event, "usage", None)
                if usage is not None:
                    completion_tokens = getattr(usage, "output_tokens", 0) or 0
                    cache_read_tokens = _usage_int(
                        usage, "cache_read_input_tokens"
                    )
                    cache_write_tokens = _usage_int(
                        usage, "cache_creation_input_tokens"
                    )
                    cost_usd = _usage_float(usage, "cost_usd")
                    usage_extra = {"usage": _usage_dict(usage)}
                return
            if event_type == "message_start":
                message = getattr(event, "message", None)
                usage = getattr(message, "usage", None)
                if usage is not None:
                    prompt_tokens = getattr(usage, "input_tokens", 0) or 0
                    cache_read_tokens = _usage_int(
                        usage, "cache_read_input_tokens"
                    )
                    cache_write_tokens = _usage_int(
                        usage, "cache_creation_input_tokens"
                    )
                    cost_usd = _usage_float(usage, "cost_usd")
                    usage_extra = {"usage": _usage_dict(usage)}
                return

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

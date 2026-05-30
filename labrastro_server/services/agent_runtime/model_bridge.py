"""Server-origin model request bridge for AgentRuns."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
import logging
from typing import Any, Callable

from reuleauxcoder.domain.agent_runtime.models import ModelRequestOrigin, TaskStatus
from reuleauxcoder.domain.config.models import ProviderConfig
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.providers.models import ProviderRequest, ProviderResponse
from reuleauxcoder.services.providers.manager import ProviderManager


TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.BLOCKED,
}

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentRunModelBridgeError(Exception):
    code: str
    message: str
    status: HTTPStatus = HTTPStatus.BAD_REQUEST

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class PreparedAgentRunModelRequest:
    provider_config: ProviderConfig
    provider_model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    parameters: dict[str, Any]
    metadata: dict[str, Any]


class AgentRunModelBridge:
    """Validate a claimed AgentRun and execute its server-origin model request."""

    def __init__(self, *, runtime_control_plane: Any, admin_manager: Any):
        self.runtime_control_plane = runtime_control_plane
        self.admin_manager = admin_manager

    def prepare(
        self,
        payload: dict[str, Any],
        *,
        peer_id: str | None,
    ) -> PreparedAgentRunModelRequest:
        task_id = _required_str(payload, "agent_run_id")
        request_id = _required_str(payload, "request_id")
        worker_id = _required_str(payload, "worker_id")
        try:
            task = self.runtime_control_plane.get_agent_run(task_id)
        except KeyError as exc:
            raise AgentRunModelBridgeError(
                "agent_run_not_found",
                "AgentRun not found.",
                HTTPStatus.NOT_FOUND,
            ) from exc
        ok, reason = self.runtime_control_plane.validate_claim_owner(
            request_id=request_id,
            task_id=task_id,
            worker_id=worker_id,
            peer_id=peer_id,
        )
        if not ok:
            raise AgentRunModelBridgeError(
                reason or "claim_owner_mismatch",
                reason or "AgentRun claim owner mismatch.",
                HTTPStatus.FORBIDDEN,
            )
        if task.status in TERMINAL_STATUSES:
            raise AgentRunModelBridgeError(
                "agent_run_not_active",
                f"AgentRun is not active: {task.status.value}",
                HTTPStatus.CONFLICT,
            )
        metadata = dict(task.metadata or {})
        if str(metadata.get("model_request_origin") or "") != ModelRequestOrigin.SERVER.value:
            raise AgentRunModelBridgeError(
                "server_origin_model_not_allowed",
                "AgentRun is not authorized for server-origin model requests.",
                HTTPStatus.FORBIDDEN,
            )
        binding = _dict_value(metadata.get("model_binding"))
        provider_id = str(binding.get("provider") or "").strip()
        model = str(binding.get("model") or "").strip()
        if not provider_id or not model:
            raise AgentRunModelBridgeError(
                "model_binding_missing",
                "AgentRun has no provider/model binding snapshot.",
                HTTPStatus.BAD_REQUEST,
            )
        provider = self._provider_config(provider_id)
        if provider is None:
            raise AgentRunModelBridgeError(
                "model_provider_not_found",
                f"Model provider not found: {provider_id}",
                HTTPStatus.BAD_REQUEST,
            )
        if provider.type == "labrastro_server":
            raise AgentRunModelBridgeError(
                "invalid_model_provider",
                "Agent model binding must reference a real server-side provider.",
                HTTPStatus.BAD_REQUEST,
            )
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise AgentRunModelBridgeError(
                "model_messages_required",
                "messages must be a list.",
                HTTPStatus.BAD_REQUEST,
            )
        tools = payload.get("tools", [])
        parameters = {
            **_dict_value(payload.get("parameters")),
            **_dict_value(binding.get("parameters")),
        }
        bridge_metadata = {
            **_dict_value(payload.get("metadata")),
            "agent_run_id": task_id,
            "agent_id": task.agent_id,
            "request_id": request_id,
            "worker_id": worker_id,
            "model_request_origin": ModelRequestOrigin.SERVER.value,
        }
        return PreparedAgentRunModelRequest(
            provider_config=provider,
            provider_model=model,
            messages=[dict(item) for item in messages if isinstance(item, dict)],
            tools=[dict(item) for item in tools if isinstance(item, dict)]
            if isinstance(tools, list)
            else [],
            parameters=parameters,
            metadata=bridge_metadata,
        )

    def execute(
        self,
        prepared: PreparedAgentRunModelRequest,
        *,
        on_token: Callable[[str], None] | None = None,
        on_reasoning_token: Callable[[str], None] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], None] | None = None,
    ) -> ProviderResponse:
        provider = ProviderManager().create(prepared.provider_config)
        LOGGER.info(
            "agent_run_model_request server_origin_bridge agent_run_id=%s request_id=%s worker_id=%s agent_id=%s provider=%s model=%s",
            prepared.metadata.get("agent_run_id"),
            prepared.metadata.get("request_id"),
            prepared.metadata.get("worker_id"),
            prepared.metadata.get("agent_id"),
            prepared.provider_config.id,
            prepared.provider_model,
        )
        try:
            return provider.chat(
                ProviderRequest(
                    model=prepared.provider_model,
                    messages=prepared.messages,
                    tools=prepared.tools,
                    temperature=float(prepared.parameters.get("temperature") or 0.0),
                    max_tokens=int(prepared.parameters.get("max_tokens") or 0),
                    reasoning_effort=_optional_str(prepared.parameters.get("reasoning_effort")),
                    thinking_enabled=_optional_bool(prepared.parameters.get("thinking_enabled")),
                    tool_choice=prepared.parameters.get("tool_choice"),
                    on_token=on_token,
                    on_reasoning_token=on_reasoning_token,
                    on_tool_call_delta=on_tool_call_delta,
                    metadata=prepared.metadata,
                )
            )
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:
            raise AgentRunModelBridgeError(
                "provider_request_failed",
                str(exc) or "Provider request failed.",
                HTTPStatus.BAD_GATEWAY,
            ) from exc

    def _provider_config(self, provider_id: str) -> ProviderConfig | None:
        getter = getattr(self.admin_manager, "_expanded_provider", None)
        if callable(getter):
            return getter(provider_id)
        return None


def provider_response_to_dict(response: ProviderResponse) -> dict[str, Any]:
    return {
        "content": response.content,
        "reasoning_content": response.reasoning_content,
        "reasoning_signature": response.reasoning_signature,
        "reasoning_details": [dict(item) for item in response.reasoning_details],
        "tool_calls": [_tool_call_to_dict(item) for item in response.tool_calls],
        "prompt_tokens": response.prompt_tokens,
        "completion_tokens": response.completion_tokens,
        "cache_read_tokens": response.cache_read_tokens,
        "cache_write_tokens": response.cache_write_tokens,
        "cost_usd": response.cost_usd,
        "usage_extra": dict(response.usage_extra),
        "tokens": list(response.tokens),
        "provider_response_id": response.provider_response_id,
        "provider_extra": dict(response.provider_extra),
        "diagnostics": [item.to_dict() for item in response.diagnostics],
        "stream_status": response.stream_status,
        "interruption": dict(response.interruption) if response.interruption else None,
        "recovery": dict(response.recovery) if response.recovery else None,
    }


def _tool_call_to_dict(tool_call: ToolCall) -> dict[str, Any]:
    return {
        "id": tool_call.id,
        "name": tool_call.name,
        "arguments": dict(tool_call.arguments),
        "argument_error": tool_call.argument_error,
        "argument_diagnostics": [dict(item) for item in tool_call.argument_diagnostics],
    }


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise AgentRunModelBridgeError(
            f"{key}_required",
            f"{key} is required.",
            HTTPStatus.BAD_REQUEST,
        )
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)

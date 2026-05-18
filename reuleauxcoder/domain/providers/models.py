"""Provider request/response domain models."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from reuleauxcoder.domain.llm.models import LLMResponse, ToolCall


@dataclass(slots=True)
class ProviderDiagnostic:
    """Provider-level request shaping or execution diagnostic."""

    code: str
    message: str
    level: str = "warning"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "level": self.level}


@dataclass(slots=True)
class ProviderRequest:
    """Provider-neutral LLM request."""

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int = 0
    reasoning_effort: str | None = None
    thinking_enabled: bool | None = None
    tool_choice: str | dict[str, Any] | None = None
    on_token: Callable[[str], None] | None = None
    on_reasoning_token: Callable[[str], None] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    request_params: dict[str, Any] | None = None


@dataclass(slots=True)
class ProviderResponse:
    """Provider-neutral LLM response."""

    content: str = ""
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    usage_extra: dict[str, Any] = field(default_factory=dict)
    tokens: list[str] = field(default_factory=list)
    provider_response_id: str | None = None
    provider_extra: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[ProviderDiagnostic] = field(default_factory=list)
    stream_status: str = "completed"
    interruption: dict[str, Any] | None = None
    recovery: dict[str, Any] | None = None

    def to_llm_response(self) -> LLMResponse:
        extra = dict(self.provider_extra)
        if self.diagnostics:
            extra["diagnostics"] = [item.to_dict() for item in self.diagnostics]
        return LLMResponse(
            content=self.content,
            reasoning_content=self.reasoning_content,
            reasoning_signature=self.reasoning_signature,
            reasoning_details=list(self.reasoning_details),
            tool_calls=list(self.tool_calls),
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            cost_usd=self.cost_usd,
            usage_extra=dict(self.usage_extra),
            provider_response_id=self.provider_response_id,
            provider_extra=extra,
            tokens=list(self.tokens),
            stream_status=self.stream_status,
            interruption=dict(self.interruption) if self.interruption else None,
            recovery=dict(self.recovery) if self.recovery else None,
        )

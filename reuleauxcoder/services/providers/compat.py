"""Provider compatibility request-shaping rules."""

from __future__ import annotations

from typing import Any

from reuleauxcoder.domain.config.models import ProviderConfig, ProviderCompat
from reuleauxcoder.domain.providers.models import ProviderDiagnostic, ProviderRequest


def compat_of(config: ProviderConfig) -> ProviderCompat:
    return config.compat


def is_forced_tool_choice(tool_choice: Any) -> bool:
    if tool_choice == "required":
        return True
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        return choice_type in {"function", "tool"}
    return False


def is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def coerce_bool(value: Any) -> bool:
    return is_truthy(value)


def coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def merge_extra_body(params: dict[str, Any], values: dict[str, Any]) -> None:
    extra_body = dict(params.get("extra_body") or {})
    extra_body.update(values)
    params["extra_body"] = extra_body


def normalize_high_max_effort(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"max", "xhigh"}:
        return "max"
    return "high"


def should_omit_openai_chat_temperature(config: ProviderConfig) -> bool:
    if compat_of(config) == "kimi":
        return not is_truthy(config.extra.get("send_temperature"))
    return False


def apply_openai_chat_reasoning(
    config: ProviderConfig,
    request: ProviderRequest,
    params: dict[str, Any],
    diagnostics: list[ProviderDiagnostic],
) -> None:
    if not request.reasoning_effort:
        return
    compat = compat_of(config)
    if compat == "deepseek":
        params["reasoning_effort"] = normalize_high_max_effort(request.reasoning_effort)
        return
    if compat in {"kimi", "glm", "qwen"}:
        diagnostics.append(
            ProviderDiagnostic(
                code="reasoning_effort_ignored_for_compat",
                message=(
                    f"Provider '{config.id}' uses compat={compat}; standard "
                    "OpenAI Chat reasoning_effort was ignored."
                ),
            )
        )
        return
    if config.api_features.reasoning_effort:
        params["reasoning_effort"] = request.reasoning_effort
    else:
        diagnostics.append(
            ProviderDiagnostic(
                code="reasoning_effort_unsupported",
                message=(
                    f"Provider '{config.id}' does not declare reasoning_effort support; "
                    "the option was ignored."
                ),
            )
        )


def apply_openai_chat_thinking(
    config: ProviderConfig,
    request: ProviderRequest,
    params: dict[str, Any],
    diagnostics: list[ProviderDiagnostic],
) -> None:
    if request.thinking_enabled is None:
        return
    if not config.api_features.thinking:
        diagnostics.append(
            ProviderDiagnostic(
                code="thinking_unsupported",
                message=(
                    f"Provider '{config.id}' does not declare thinking support; "
                    "the option was ignored."
                ),
            )
        )
        return

    compat = compat_of(config)
    enabled = bool(request.thinking_enabled)
    if compat == "qwen":
        qwen_body: dict[str, Any] = {"enable_thinking": enabled}
        if "thinking_budget" in config.extra:
            budget = coerce_int(config.extra.get("thinking_budget"))
            qwen_body["thinking_budget"] = budget if budget is not None else config.extra.get("thinking_budget")
        if "preserve_thinking" in config.extra:
            qwen_body["preserve_thinking"] = coerce_bool(
                config.extra.get("preserve_thinking")
            )
        merge_extra_body(params, qwen_body)
        return

    body: dict[str, Any] = {"thinking": {"type": "enabled" if enabled else "disabled"}}
    if compat == "glm" and "clear_thinking" in config.extra:
        body["clear_thinking"] = coerce_bool(config.extra.get("clear_thinking"))
    merge_extra_body(params, body)


def apply_openai_chat_tool_choice(
    config: ProviderConfig,
    request: ProviderRequest,
    params: dict[str, Any],
    diagnostics: list[ProviderDiagnostic],
) -> None:
    if not request.tool_choice:
        if (
            compat_of(config) == "kimi"
            and request.thinking_enabled is not False
            and request.tools
            and request.max_tokens < 16_000
        ):
            diagnostics.append(
                ProviderDiagnostic(
                    code="kimi_thinking_tool_max_tokens_low",
                    message=(
                        f"Provider '{config.id}' uses compat=kimi; thinking plus "
                        "tools is recommended with max_tokens >= 16000."
                    ),
                )
            )
        return

    compat = compat_of(config)
    if (
        compat in {"deepseek", "kimi", "glm", "qwen"}
        and request.thinking_enabled is not False
        and is_forced_tool_choice(request.tool_choice)
    ):
        params["tool_choice"] = "auto"
        diagnostics.append(
            ProviderDiagnostic(
                code="tool_choice_thinking_downgraded",
                message=(
                    f"Provider '{config.id}' uses compat={compat}; forced tool_choice "
                    "was downgraded to auto while thinking is enabled."
                ),
            )
        )
    elif (
        request.tool_choice == "required"
        and not config.api_features.tool_choice_required
    ):
        params["tool_choice"] = "auto"
        diagnostics.append(
            ProviderDiagnostic(
                code="tool_choice_required_downgraded",
                message=(
                    f"Provider '{config.id}' does not declare required tool_choice support; "
                    "tool_choice was downgraded to auto."
                ),
            )
        )
    else:
        params["tool_choice"] = request.tool_choice

    if (
        compat == "kimi"
        and request.thinking_enabled is not False
        and request.tools
        and request.max_tokens < 16_000
    ):
        diagnostics.append(
            ProviderDiagnostic(
                code="kimi_thinking_tool_max_tokens_low",
                message=(
                    f"Provider '{config.id}' uses compat=kimi; thinking plus tools "
                    "is recommended with max_tokens >= 16000."
                ),
            )
        )


def apply_anthropic_reasoning_effort(
    config: ProviderConfig,
    request: ProviderRequest,
    params: dict[str, Any],
    diagnostics: list[ProviderDiagnostic],
) -> None:
    if not request.reasoning_effort:
        return
    if compat_of(config) == "deepseek" or config.extra.get("reasoning_effort_param") == "output_config":
        merge_extra_body(
            params,
            {
                "output_config": {
                    "effort": normalize_high_max_effort(request.reasoning_effort)
                }
            },
        )
        return
    diagnostics.append(
        ProviderDiagnostic(
            code="reasoning_effort_unsupported",
            message=(
                f"Provider '{config.id}' uses Anthropic thinking settings; "
                "reasoning_effort was ignored."
            ),
        )
    )


def deepseek_anthropic_budget_is_provider_managed(config: ProviderConfig) -> bool:
    return compat_of(config) == "deepseek"


def apply_openai_responses_qwen(
    config: ProviderConfig,
    request: ProviderRequest,
    params: dict[str, Any],
    diagnostics: list[ProviderDiagnostic],
) -> bool:
    if compat_of(config) != "qwen":
        return False
    if request.thinking_enabled is not None:
        body: dict[str, Any] = {"enable_thinking": bool(request.thinking_enabled)}
        if "thinking_budget" in config.extra:
            budget = coerce_int(config.extra.get("thinking_budget"))
            body["thinking_budget"] = budget if budget is not None else config.extra.get("thinking_budget")
        if "preserve_thinking" in config.extra:
            body["preserve_thinking"] = coerce_bool(config.extra.get("preserve_thinking"))
        merge_extra_body(params, body)
    if request.reasoning_effort:
        diagnostics.append(
            ProviderDiagnostic(
                code="reasoning_effort_ignored_for_compat",
                message=(
                    f"Provider '{config.id}' uses compat=qwen; OpenAI Responses "
                    "reasoning parameter was omitted."
                ),
            )
        )
    return True

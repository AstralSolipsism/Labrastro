"""CLI command handlers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from reuleauxcoder.app.commands import CommandContext, dispatch_command, parse_command
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.domain.agent.runtime_boundary import runtime_agent_run_id
from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.domain.hooks.lifecycle import build_lifecycle_event_context
from reuleauxcoder.domain.hooks.lifecycle_policy import (
    lifecycle_gate_output_is_terminal,
    lifecycle_output_message,
    lifecycle_output_requests_approval,
)
from reuleauxcoder.interfaces.events import (
    UIEvent,
    UIEventBus,
    UIEventKind,
    UIEventLevel,
)
from reuleauxcoder.interfaces.ui_registry import UIProfile

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.skills.service import SkillsService


@dataclass(slots=True)
class _CommandExpansionLifecycleResolution:
    command_text: str
    blocked: bool = False
    message: str = ""


def handle_command(
    user_input: str,
    agent: Agent,
    config: Config,
    current_session_id: str | None,
    ui_bus: UIEventBus,
    ui_profile: UIProfile,
    action_registry: ActionRegistry,
    sessions_dir: Path | None = None,
    skills_service: SkillsService | None = None,
):
    lifecycle_resolution = _apply_user_prompt_expansion_lifecycle(
        user_input,
        agent,
        current_session_id,
        ui_bus,
        ui_profile,
    )
    if lifecycle_resolution.blocked:
        return {
            "action": "continue",
            "session_id": current_session_id,
            "session_exit_time": None,
        }
    command_text = lifecycle_resolution.command_text
    parsed_action = parse_command(
        command_text,
        ui_profile=ui_profile,
        action_registry=action_registry,
        current_session_id=current_session_id,
    )
    if parsed_action is not None:
        result = dispatch_command(
            parsed_action,
            CommandContext(
                agent=agent,
                config=config,
                ui_bus=ui_bus,
                ui_profile=ui_profile,
                action_registry=parsed_action.registry,
                ui_interactor=getattr(agent, "ui_interactor", None),
                sessions_dir=sessions_dir,
                skills_service=skills_service,
            ),
        )
        return {
            "action": result.action,
            "session_id": result.session_id
            if result.session_id is not None
            else current_session_id,
            "session_exit_time": result.session_exit_time,
        }

    return {"action": "chat", "session_id": current_session_id}


def _apply_user_prompt_expansion_lifecycle(
    user_input: str,
    agent: Agent,
    current_session_id: str | None,
    ui_bus: UIEventBus,
    ui_profile: UIProfile,
) -> _CommandExpansionLifecycleResolution:
    if not user_input.startswith("/"):
        return _CommandExpansionLifecycleResolution(command_text=user_input)
    dispatcher = getattr(agent, "lifecycle_dispatcher", None)
    dispatch = getattr(dispatcher, "dispatch", None)
    if not callable(dispatch):
        return _CommandExpansionLifecycleResolution(command_text=user_input)

    context = build_lifecycle_event_context(
        "UserPromptExpansion",
        placement="server",
        trigger_source="command",
        session_run_id=current_session_id or str(getattr(agent, "current_session_id", "") or ""),
        agent_run_id=runtime_agent_run_id(agent),
        turn_id=str(getattr(agent, "runtime_turn_id", "") or ""),
        origin="user",
        locale=str(getattr(agent, "locale", "") or ""),
        metadata={
            "ui_profile": getattr(ui_profile, "ui_id", ""),
            "command_surface": "slash",
        },
        payload={
            "user_input": user_input,
            "command_text": user_input,
            "trigger_kind": "slash",
        },
    )
    try:
        results = list(dispatch(context))
    except Exception as exc:
        message = "UserPromptExpansion lifecycle dispatch failed."
        _emit_command_lifecycle_observation(
            ui_bus,
            "dispatch_failed",
            context,
            error=f"{type(exc).__name__}: {exc}",
            message=message,
        )
        return _CommandExpansionLifecycleResolution(
            command_text=user_input,
            blocked=True,
            message=message,
        )

    resolved_command = str(context.payload.get("command_text") or user_input)
    ask_reasons: list[str] = []
    ask_hooks: list[dict[str, str]] = []
    blocked_message = ""
    for result in results:
        _emit_command_lifecycle_observation(ui_bus, "result", context, result=result)
        output = getattr(result, "output", None)
        if output is None:
            continue
        updated_input = getattr(output, "updated_input", None)
        if isinstance(updated_input, dict):
            candidate = updated_input.get("command_text", updated_input.get("user_input"))
            if isinstance(candidate, str):
                resolved_command = candidate
        if lifecycle_gate_output_is_terminal(output):
            blocked_message = lifecycle_output_message(
                output,
                fallback="UserPromptExpansion lifecycle blocked this command.",
            )
            break
        if lifecycle_output_requests_approval(output):
            reason = lifecycle_output_message(
                output,
                fallback="UserPromptExpansion lifecycle requires approval.",
            )
            ask_reasons.append(reason)
            declaration = getattr(result, "declaration", None)
            ask_hooks.append({
                "hook_id": str(getattr(declaration, "id", "") or ""),
                "display_name": str(getattr(declaration, "display_name", "") or ""),
            })

    if not blocked_message and ask_reasons:
        blocked_message = _request_command_expansion_approval(
            agent,
            resolved_command,
            reasons=ask_reasons,
            hooks=ask_hooks,
        )
    return _CommandExpansionLifecycleResolution(
        command_text=resolved_command,
        blocked=bool(blocked_message),
        message=blocked_message,
    )


def _request_command_expansion_approval(
    agent: Agent,
    command_text: str,
    *,
    reasons: list[str],
    hooks: list[dict[str, str]],
) -> str:
    provider = getattr(agent, "approval_provider", None)
    request_approval = getattr(provider, "request_approval", None)
    if not callable(request_approval):
        return "UserPromptExpansion lifecycle requires approval, but no approval provider is configured."
    reason = "\n".join(item for item in reasons if item).strip()
    try:
        decision = request_approval(
            ApprovalRequest(
                tool_name="lifecycle:UserPromptExpansion",
                tool_args={"command_text": command_text},
                tool_source="lifecycle_hook",
                reason=reason or "UserPromptExpansion lifecycle requires approval.",
                intent="Review whether this user command lifecycle hook may continue command dispatch.",
                metadata={
                    "lifecycle_event": "UserPromptExpansion",
                    "lifecycle_hooks": hooks,
                },
            )
        )
    except (KeyboardInterrupt, EOFError):
        return "UserPromptExpansion lifecycle approval was interrupted."
    return "" if decision.approved else (
        decision.reason
        or "UserPromptExpansion lifecycle approval was denied."
    )


def _emit_command_lifecycle_observation(
    ui_bus: UIEventBus,
    phase: str,
    context: object,
    *,
    result: object | None = None,
    error: str = "",
    message: str = "",
) -> None:
    try:
        declaration = getattr(result, "declaration", None)
        output = getattr(result, "output", None)
        output_dict = output.to_dict() if hasattr(output, "to_dict") else {}
        diagnostics = (
            list(output_dict.get("diagnostics") or [])
            if isinstance(output_dict, dict)
            else []
        )
        decision = (
            str(output_dict.get("decision") or "none")
            if isinstance(output_dict, dict)
            else "none"
        )
        continue_flow = (
            bool(output_dict.get("continue_flow", True))
            if isinstance(output_dict, dict)
            else True
        )
        if error or decision in {"deny", "defer"} or continue_flow is False:
            level = UIEventLevel.ERROR
        elif decision == "ask" or diagnostics:
            level = UIEventLevel.WARNING
        else:
            level = UIEventLevel.INFO
        payload = {
            "event_type": "lifecycle_hook",
            "phase": phase,
            "event_name": str(getattr(context, "event_name", "") or ""),
            "placement": str(getattr(context, "placement", "") or "server"),
            "session_run_id": str(getattr(context, "session_run_id", "") or ""),
            "agent_run_id": str(getattr(context, "agent_run_id", "") or ""),
            "turn_id": str(getattr(context, "turn_id", "") or ""),
            "trigger_source": str(getattr(context, "source", "") or ""),
            "hook_id": str(getattr(declaration, "id", "") or ""),
            "display_name": str(getattr(declaration, "display_name", "") or ""),
            "source": str(getattr(declaration, "source", "") or ""),
            "decision": decision,
            "continue_flow": continue_flow,
            "diagnostics": diagnostics,
            "error": error,
            "level": level.value,
            "title": str(getattr(declaration, "display_name", "") or "UserPromptExpansion"),
            "payload": dict(getattr(context, "payload", {}) or {}),
        }
        if isinstance(output_dict, dict):
            payload["output"] = output_dict
            lifecycle_message = lifecycle_output_message(output, fallback="")
            if lifecycle_message:
                payload["message"] = lifecycle_message
        if message:
            payload["message"] = message
        ui_bus.emit(
            UIEvent(
                message=str(payload.get("message") or payload["event_name"]),
                level=level,
                kind=UIEventKind.AGENT,
                data=payload,
            )
        )
    except Exception:
        return

"""Agent loop - the main conversation loop."""

from __future__ import annotations

import os
import platform
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.llm.models import LLMResponse

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.agent.runtime_budget import (
    runtime_budget_limit,
    runtime_budget_int,
)
from reuleauxcoder.domain.agent.document_draft import DocumentDraftRuntime
from reuleauxcoder.domain.agent.document_draft_stream import DocumentDraftLiveStream
from reuleauxcoder.domain.agent.runtime_boundary import (
    runtime_agent_run_id,
    runtime_execution_target,
    runtime_workspace_root,
    runtime_working_directory,
)
from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.domain.files import (
    LocalWorkspaceMutationBackend,
    PatchArgumentStreamDecoder,
    PatchArgumentStreamError,
)
from reuleauxcoder.domain.hooks.lifecycle import (
    build_lifecycle_event_context,
    lifecycle_output_audit_fields,
    lifecycle_runtime_artifacts_for_event,
)
from reuleauxcoder.domain.hooks.lifecycle_policy import (
    lifecycle_gate_output_is_terminal,
    lifecycle_output_message,
    lifecycle_output_requests_approval,
)
from reuleauxcoder.domain.memory.runtime import memory_metadata_from_agent


class AgentLoop:
    """Manages the agent's conversation loop."""

    def __init__(self, agent: "Agent", *, prompt_fn: Callable[..., str], shell_name: str):
        self.agent = agent
        self._prompt_fn = prompt_fn
        self._shell = shell_name
        self.last_response_streamed = False
        self.last_run_interrupted = False
        self.last_interruption_payload: dict[str, Any] | None = None
        self.last_termination_reason: str | None = None
        self.last_budget_exceeded: dict[str, Any] | None = None
        self._skills_catalog_lifecycle_cache_key: tuple[Any, ...] | None = None
        self._skills_catalog_lifecycle_cache_value = ""

    def _ui_bus(self) -> Any:
        context = getattr(self.agent, "context", None)
        return getattr(context, "_ui_bus", None)

    def _runtime_tail_message(self) -> dict:
        """Build ephemeral runtime context appended only at send time."""
        runtime_context = self._runtime_context()
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        now_local = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        content = (
            "<system_context>\n"
            "This block is automatically injected by the system before each turn.\n"
            "It is NOT a user message — do not reply to it directly.\n"
            f"- UTC time: {now_utc}\n"
            f"- Local time: {now_local}\n"
            + "\n".join(runtime_context)
            + "\n"
            "Local time represents the user's current time at this turn.\n"
            "Always use Local time as the source of truth for all time-related reasoning.\n"
            "UTC time is provided only for reference.\n"
            "</system_context>"
        )
        return {"role": "user", "content": content}

    def _runtime_context(self) -> list[str]:
        execution_target = str(
            getattr(self.agent, "runtime_execution_target", "local") or "local"
        )
        if execution_target == "remote_peer":
            return self._remote_peer_runtime_context()
        return self._local_runtime_context(execution_target)

    def _local_runtime_context(self, execution_target: str) -> list[str]:
        uname = platform.uname()
        runtime_cwd = runtime_working_directory(self.agent) or os.getcwd()
        return [
            f"- Execution target: {execution_target}",
            f"- Working directory: {runtime_cwd}",
            f"- OS: {uname.system} {uname.release} ({uname.machine})",
            f"- Python: {platform.python_version()}",
            f"- Shell: {self._shell}",
        ]

    def _remote_peer_runtime_context(self) -> list[str]:
        context = getattr(self.agent, "runtime_peer_context", None)
        if not isinstance(context, dict):
            raise RuntimeError("remote peer runtime context is missing")

        cwd = self._required_runtime_string(context, "cwd")
        workspace_root = self._required_runtime_string(context, "workspace_root")
        host_info = context.get("host_info_min")
        if not isinstance(host_info, dict):
            raise RuntimeError("remote peer runtime context is missing host_info_min")

        os_name = self._required_runtime_string(host_info, "os")
        shell = self._required_runtime_string(host_info, "shell")
        arch = str(host_info.get("arch") or "").strip()
        features = context.get("features")
        if not isinstance(features, list):
            raise RuntimeError("remote peer runtime context is missing features")

        os_label = f"{os_name} ({arch})" if arch else os_name
        return [
            "- Execution target: remote_peer",
            f"- Working directory: {cwd}",
            f"- Workspace root: {workspace_root}",
            f"- OS: {os_label}",
            f"- Shell: {shell}",
            f"- Peer features: {', '.join(str(item) for item in features)}",
        ]

    @staticmethod
    def _required_runtime_string(source: dict[str, Any], key: str) -> str:
        value = source.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"remote peer runtime context is missing {key}")
        return value

    def _skills_catalog_for_prompt(self) -> str:
        catalog = str(getattr(self.agent, "skills_catalog", "") or "")
        if not catalog.strip():
            return ""
        dispatcher = getattr(self.agent, "lifecycle_dispatcher", None)
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            return catalog
        cache_key = (
            catalog,
            str(getattr(self.agent, "current_session_id", "") or ""),
            runtime_agent_run_id(self.agent),
            str(getattr(self.agent, "runtime_turn_id", "") or ""),
            id(dispatcher),
        )
        if self._skills_catalog_lifecycle_cache_key == cache_key:
            return self._skills_catalog_lifecycle_cache_value

        context = build_lifecycle_event_context(
            "UserPromptExpansion",
            placement="server",
            session_run_id=str(getattr(self.agent, "current_session_id", "") or ""),
            agent_run_id=runtime_agent_run_id(self.agent),
            turn_id=str(getattr(self.agent, "runtime_turn_id", "") or ""),
            trigger_source="skill",
            origin="agent",
            locale=str(getattr(self.agent, "locale", "") or ""),
            metadata={
                "round_index": getattr(self.agent.state, "current_round", None),
                "expansion_surface": "skills_catalog",
                "parent_trigger_source": str(
                    getattr(self.agent, "permission_trigger_source", "") or "chat"
                ),
            },
            payload={
                "user_input": "",
                "command_text": "",
                "trigger_kind": "skill_catalog",
                "skills_catalog": catalog,
            },
        )
        self._emit_lifecycle_observation("dispatch_start", context)
        try:
            results = list(dispatch(context) or [])
        except Exception as exc:
            self._emit_lifecycle_observation(
                "dispatch_failed",
                context,
                error=f"{type(exc).__name__}: {exc}",
                message="UserPromptExpansion lifecycle dispatch failed.",
            )
            return self._cache_skills_catalog_lifecycle(cache_key, "")

        additional_context: list[Any] = []
        ask_reasons: list[str] = []
        ask_hooks: list[dict[str, str]] = []
        for result in results:
            self._emit_lifecycle_observation("result", context, result=result)
            output = getattr(result, "output", None)
            if output is None:
                continue
            additional_context.extend(list(getattr(output, "additional_context", []) or []))
            if lifecycle_gate_output_is_terminal(output):
                return self._cache_skills_catalog_lifecycle(cache_key, "")
            if lifecycle_output_requests_approval(output):
                ask_reasons.append(
                    lifecycle_output_message(
                        output,
                        fallback="UserPromptExpansion lifecycle requires approval.",
                    )
                )
                declaration = getattr(result, "declaration", None)
                ask_hooks.append(
                    {
                        "hook_id": str(getattr(declaration, "id", "") or ""),
                        "display_name": str(
                            getattr(declaration, "display_name", "") or ""
                        ),
                    }
                )
        if ask_reasons and not self._approve_skill_catalog_expansion(
            catalog,
            reasons=ask_reasons,
            hooks=ask_hooks,
        ):
            return self._cache_skills_catalog_lifecycle(cache_key, "")
        extra = self._skill_expansion_additional_context(additional_context)
        resolved = f"{catalog}\n\n{extra}" if extra else catalog
        return self._cache_skills_catalog_lifecycle(cache_key, resolved)

    def _cache_skills_catalog_lifecycle(
        self,
        cache_key: tuple[Any, ...],
        value: str,
    ) -> str:
        self._skills_catalog_lifecycle_cache_key = cache_key
        self._skills_catalog_lifecycle_cache_value = value
        return value

    def _approve_skill_catalog_expansion(
        self,
        catalog: str,
        *,
        reasons: list[str],
        hooks: list[dict[str, str]],
    ) -> bool:
        provider = getattr(self.agent, "approval_provider", None)
        request_approval = getattr(provider, "request_approval", None)
        if not callable(request_approval):
            return False
        reason = "\n".join(item for item in reasons if item).strip()
        try:
            decision = request_approval(
                ApprovalRequest(
                    tool_name="lifecycle:UserPromptExpansion",
                    tool_args={"trigger_kind": "skill_catalog"},
                    tool_source="lifecycle_hook",
                    reason=reason or "UserPromptExpansion lifecycle requires approval.",
                    intent="Review whether Skill catalog expansion may enter the model prompt.",
                    metadata={
                        "lifecycle_event": "UserPromptExpansion",
                        "trigger_kind": "skill_catalog",
                        "catalog_chars": len(catalog),
                        "lifecycle_hooks": hooks,
                    },
                )
            )
        except (KeyboardInterrupt, EOFError):
            return False
        return bool(getattr(decision, "approved", False))

    def _emit_lifecycle_observation(
        self,
        phase: str,
        context: object,
        *,
        result: object | None = None,
        error: str = "",
        message: str = "",
    ) -> None:
        emit = getattr(self.agent, "_emit_event", None)
        if not callable(emit):
            return
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
        level = (
            "error"
            if error or decision == "deny" or continue_flow is False
            else "warning"
            if diagnostics
            else "info"
        )
        payload = {
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
            "level": level,
            "title": str(
                getattr(declaration, "display_name", "")
                or getattr(context, "event_name", "")
                or "Lifecycle hook"
            ),
            "payload": dict(getattr(context, "payload", {}) or {}),
        }
        if isinstance(output_dict, dict):
            payload.update(lifecycle_output_audit_fields(output))
            payload["output"] = output_dict
            output_message = lifecycle_output_message(output, fallback="")
            if output_message:
                payload["message"] = output_message
        if message:
            payload["message"] = message
        emit(
            AgentEvent.lifecycle_hook(
                payload,
                runtime_artifacts=lifecycle_runtime_artifacts_for_event(
                    output,
                    event_name=str(getattr(context, "event_name", "") or ""),
                    context=context,
                ),
            )
        )

    @staticmethod
    def _skill_expansion_additional_context(items: list[Any]) -> str:
        text_items: list[str] = []
        for item in items:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str) and content.strip():
                    text_items.append(content.strip())
                    continue
            text = str(item or "").strip()
            if text:
                text_items.append(text)
        if not text_items:
            return ""
        return "Lifecycle skill context:\n" + "\n".join(text_items)

    def _full_messages(self) -> list[dict]:
        """Get full messages including system prompt and ephemeral runtime tail."""
        mode = self.agent.get_active_mode_config()
        active_tools = self.agent.get_active_tools()
        blocked = self.agent.get_blocked_tools()
        blocked_tools = [tool.name for tool in blocked]

        suggested_modes: list[str] = []
        for tool in blocked:
            for mode_name in self.agent.suggest_modes_for_tool(tool.name):
                if (
                    mode_name != self.agent.active_mode
                    and mode_name not in suggested_modes
                ):
                    suggested_modes.append(mode_name)
        suggested_modes.sort()  # Ensure deterministic order for prompt caching

        available_modes = [
            (name, mode_cfg.description)
            for name, mode_cfg in sorted(self.agent.available_modes.items())
        ]

        system = self._prompt_fn(
            active_tools,
            mode_name=self.agent.active_mode,
            mode_prompt_append=mode.prompt_append if mode is not None else "",
            user_system_append=(
                getattr(
                    getattr(self.agent, "runtime_config", None), "prompt", None
                ).system_append
                if getattr(getattr(self.agent, "runtime_config", None), "prompt", None)
                is not None
                else ""
            ),
            blocked_tools=blocked_tools,
            mode_switch_hints=suggested_modes,
            available_modes=available_modes,
            skills_catalog=self._skills_catalog_for_prompt(),
            capability_catalog=getattr(self.agent, "capability_catalog", ""),
            workflow_mode=getattr(self.agent, "workflow_mode", None),
            workflow_prompt_append=getattr(
                self.agent, "workflow_prompt_append", ""
            ),
        )
        return [
            {"role": "system", "content": system},
            *self.agent.state.messages,
            self._runtime_tail_message(),
        ]

    def _tool_schemas(self) -> list[dict]:
        """Get tool schemas for LLM."""
        return [t.schema() for t in self.agent.get_active_tools()]

    def _inject_pending_follow_ups(self) -> None:
        consume = getattr(self.agent, "consume_follow_ups", None)
        if not callable(consume):
            return
        follow_ups = list(consume())
        if not follow_ups:
            return
        lines = [
            "<conversation_guidance>",
            "The user sent the following follow-up while this response was in progress.",
            "Treat it as updated guidance for the current task and acknowledge it in the next response when useful.",
        ]
        for index, item in enumerate(follow_ups, start=1):
            text = str(getattr(item, "text", "") or "").strip()
            if text:
                lines.append(f"{index}. {text}")
        lines.append("</conversation_guidance>")
        self.agent.state.messages.append({"role": "user", "content": "\n".join(lines)})

    def _tool_source(self, tool_name: str) -> str | None:
        tool = self.agent.get_tool(tool_name)
        return getattr(tool, "tool_source", None) if tool is not None else None

    @staticmethod
    def _sum_optional(current: int | None, increment: int | None) -> int | None:
        if increment is None:
            return current
        return (current or 0) + increment

    @staticmethod
    def _sum_optional_float(current: float | None, increment: float | None) -> float | None:
        if increment is None:
            return current
        return (current or 0.0) + increment

    def _record_response_usage(self, response: "LLMResponse") -> None:
        self.agent.state.total_prompt_tokens += response.prompt_tokens
        self.agent.state.total_completion_tokens += response.completion_tokens
        self.agent.state.total_cache_read_tokens = self._sum_optional(
            getattr(self.agent.state, "total_cache_read_tokens", None),
            response.cache_read_tokens,
        )
        self.agent.state.total_cache_write_tokens = self._sum_optional(
            getattr(self.agent.state, "total_cache_write_tokens", None),
            response.cache_write_tokens,
        )
        self.agent.state.total_cost_usd = self._sum_optional_float(
            getattr(self.agent.state, "total_cost_usd", None),
            response.cost_usd,
        )
        if response.usage_extra:
            usage_extra = getattr(self.agent.state, "usage_extra", None)
            if isinstance(usage_extra, dict):
                usage_extra.update(response.usage_extra)

    def _emit_usage_update(self, run_status: str = "running") -> None:
        context_tokens = self.agent.context.get_context_tokens(self.agent.state.messages)
        llm = getattr(self.agent, "llm", None)
        self.agent._emit_event(
            AgentEvent.usage_update(
                prompt_tokens=self.agent.state.total_prompt_tokens,
                completion_tokens=self.agent.state.total_completion_tokens,
                context_tokens=context_tokens,
                context_window=getattr(self.agent.context, "max_tokens", None),
                max_output_tokens=getattr(llm, "max_tokens", None),
                model=getattr(llm, "model", None),
                mode=getattr(self.agent, "active_mode", None),
                cache_read_tokens=getattr(
                    self.agent.state, "total_cache_read_tokens", None
                ),
                cache_write_tokens=getattr(
                    self.agent.state, "total_cache_write_tokens", None
                ),
                cost_usd=getattr(self.agent.state, "total_cost_usd", None),
                usage_extra=getattr(self.agent.state, "usage_extra", None),
                run_status=run_status,
            )
        )

    def _clear_terminal_metadata(self) -> None:
        self.last_termination_reason = None
        self.last_budget_exceeded = None

    def _record_budget_exceeded(self, limit: dict[str, Any]) -> str:
        budget = dict(limit)
        message = str(budget.get("message") or "").strip()
        self.last_termination_reason = "budget_exceeded"
        self.last_budget_exceeded = {
            "field": str(budget.get("field") or ""),
            "limit": budget.get("limit"),
            "message": message,
        }
        return f"({message})"

    def _budget_stop_result(self) -> str | None:
        limit = runtime_budget_limit(self.agent)
        return self._record_budget_exceeded(limit) if limit else None

    def _max_turns_stop_result(self) -> str | None:
        max_turns = runtime_budget_int(self.agent, "max_turns")
        if max_turns is None:
            return None
        return self._record_budget_exceeded(
            {
                "field": "max_turns",
                "limit": max_turns,
                "message": f"AgentRun budget exceeded: max_turns={max_turns}",
            }
        )

    @staticmethod
    def _stream_interruption_payload(response: "LLMResponse") -> dict[str, Any]:
        interruption = dict(response.interruption or {})
        recovery = dict(response.recovery or {})
        raw_message = str(interruption.get("message") or "").strip()
        recovered = (
            response.stream_status == "completed"
            and bool(recovery.get("attempted"))
            and not recovery.get("failed")
        )
        return {
            "stream_status": response.stream_status,
            "recoverable": bool(interruption.get("recoverable", True)),
            "phase": interruption.get("phase"),
            "classification": interruption.get("classification"),
            "partial_kind": interruption.get("partial_kind"),
            "retry_action": interruption.get("retry_action"),
            "notice_code": "provider_stream_interrupted",
            "message_key": (
                "provider_stream_interrupted.recovering"
                if recovered
                else "provider_stream.interrupted_can_continue"
            ),
            "diagnostic_message": raw_message,
            "diagnostic_path": interruption.get("diagnostic_path"),
            "recovery": recovery,
            "recovery_actions": ["continue", "retry"],
        }

    def _emit_stream_recovery_events(self, response: "LLMResponse") -> None:
        if not response.interruption:
            return
        payload = self._stream_interruption_payload(response)
        self.agent._emit_event(AgentEvent.provider_stream_interrupted(payload))
        recovery = payload.get("recovery")
        recovered = (
            response.stream_status == "completed"
            and isinstance(recovery, dict)
            and bool(recovery.get("attempted"))
            and not recovery.get("failed")
        )
        if recovered:
            self.agent._emit_event(AgentEvent.provider_stream_recovering(payload))
            self.agent._emit_event(AgentEvent.provider_stream_recovered(payload))

    def _emit_apply_patch_stream_delta(
        self,
        decoders: dict[str, PatchArgumentStreamDecoder],
        buffers: dict[str, str],
        started_items: set[str],
        *,
        index: int,
        tool_call_id: str | None,
        arguments_delta: str,
    ) -> None:
        item_id = f"file-change:{tool_call_id or f'index-{index}'}"
        if item_id not in started_items:
            started_items.add(item_id)
            self.agent._emit_event(
                AgentEvent.file_change_started(
                    item_id=item_id,
                    tool_call_id=tool_call_id,
                    changes=[],
                )
            )
        key = tool_call_id or f"index:{index}"
        decoder = decoders.setdefault(key, PatchArgumentStreamDecoder())
        try:
            patch_delta = decoder.push_delta(arguments_delta)
        except PatchArgumentStreamError as exc:
            self.agent._emit_event(
                AgentEvent.tool_call_protocol_error(
                    "apply_patch",
                    tool_call_id=tool_call_id,
                    code="PATCH_ARGUMENT_STREAM_INVALID",
                    message=str(exc),
                )
            )
            self.agent._emit_event(
                AgentEvent.file_change_completed(
                    item_id=item_id,
                    tool_call_id=tool_call_id,
                    changes=[],
                    status="failed",
                    error=str(exc),
                )
            )
            return
        if not patch_delta:
            return
        next_patch = f"{buffers.get(key, '')}{patch_delta}"
        buffers[key] = next_patch
        self.agent._emit_event(
            AgentEvent.file_change_patch_updated(
                item_id=item_id,
                tool_call_id=tool_call_id,
                changes=self._preview_stream_patch_changes(next_patch),
                patch_delta=patch_delta,
                patch_preview=next_patch[-2000:],
            )
        )

    def _preview_stream_patch_changes(self, patch: str) -> list[dict[str, Any]]:
        if runtime_execution_target(self.agent) == "remote_peer":
            return []
        workspace_root = runtime_workspace_root(self.agent) or runtime_working_directory(self.agent)
        result = LocalWorkspaceMutationBackend(workspace_root).preview_text_patch(patch)
        if result.changes:
            return [change.to_dict() for change in result.changes]
        return []

    def run(self) -> str:
        """Run the conversation loop."""
        self.last_run_interrupted = False
        self.last_interruption_payload = None
        self._clear_terminal_metadata()
        self._skills_catalog_lifecycle_cache_key = None
        self._skills_catalog_lifecycle_cache_value = ""
        # Compress if needed
        self.agent.context.maybe_compress(
            self.agent.state.messages,
            self.agent.llm,
        )
        self._emit_usage_update("running")
        draft_runtime = DocumentDraftRuntime(
            workspace_root=runtime_workspace_root(self.agent)
            or runtime_working_directory(self.agent)
            or os.getcwd(),
            mutation_backend=getattr(self.agent, "workspace_mutation_backend", None),
            approval_provider=getattr(self.agent, "approval_provider", None),
            emit=self.agent._emit_event,
        )
        draft_stream = DocumentDraftLiveStream()

        def _emit_draft_stream_events(events: list[AgentEvent]) -> None:
            for event in events:
                self.agent._emit_event(event)

        def _flush_active_draft(
            snapshot_kind: str,
            *,
            final: bool = True,
        ) -> None:
            _emit_draft_stream_events(
                draft_stream.flush(
                    draft_runtime.active,
                    snapshot_kind=snapshot_kind,
                    final=final,
                )
            )

        for round_num in range(self.agent.max_rounds):
            if self.agent.stop_requested():
                _flush_active_draft("cancelled")
                draft_runtime.cancel_active("stopped by cancellation request")
                return "(stopped by cancellation request)"
            if budget_result := self._budget_stop_result():
                _flush_active_draft("cancelled")
                draft_runtime.cancel_active(budget_result)
                return budget_result

            self.agent.state.current_round = round_num

            streamed_output = False
            streamed_reasoning = False
            self._inject_pending_follow_ups()
            patch_decoders: dict[str, PatchArgumentStreamDecoder] = {}
            patch_buffers: dict[str, str] = {}
            started_file_changes: set[str] = set()

            def _on_token(token: str) -> None:
                nonlocal streamed_output
                streamed_output = True
                draft = draft_runtime.active
                if draft is not None and draft.status == "streaming":
                    draft_runtime.append_stream_delta(token)
                    _emit_draft_stream_events(
                        draft_stream.append(draft, token)
                    )
                    return
                self.agent._emit_event(AgentEvent.stream_token(token))

            def _on_reasoning_token(token: str) -> None:
                nonlocal streamed_reasoning
                if not token:
                    return
                streamed_reasoning = True
                self.agent._emit_event(AgentEvent.reasoning_token(token))

            def _on_tool_call_delta(delta: dict[str, object]) -> None:
                raw_name = str(delta.get("tool_name") or "")
                try:
                    index = int(delta.get("index") or 0)
                except (TypeError, ValueError):
                    index = 0
                tool_call_id = str(delta.get("tool_call_id") or "") or None
                arguments_delta = str(delta.get("arguments_delta") or "")
                self.agent._emit_event(
                    AgentEvent.tool_call_delta(
                        index=index,
                        tool_call_id=tool_call_id,
                        tool_name=raw_name or None,
                        arguments_delta=arguments_delta,
                        arguments_preview=str(delta.get("arguments_preview") or ""),
                        tool_source=self._tool_source(raw_name) if raw_name else None,
                    )
                )
                if raw_name == "apply_patch":
                    self._emit_apply_patch_stream_delta(
                        patch_decoders,
                        patch_buffers,
                        started_file_changes,
                        index=index,
                        tool_call_id=tool_call_id,
                        arguments_delta=arguments_delta,
                    )

            resp = self.agent.llm.chat(
                messages=self._full_messages(),
                tools=self._tool_schemas(),
                on_token=_on_token,
                on_reasoning_token=_on_reasoning_token,
                on_tool_call_delta=_on_tool_call_delta,
                lifecycle_dispatcher=self.agent.lifecycle_dispatcher,
                session_id=getattr(self.agent, "current_session_id", None),
                ui_bus=self._ui_bus(),
                metadata={
                    **memory_metadata_from_agent(self.agent),
                    "round_index": round_num,
                    "active_mode": self.agent.active_mode,
                    "pending_tool_calls": len(self.agent._collect_pending_tool_calls()),
                },
            )

            self._record_response_usage(resp)
            self._emit_usage_update()
            self._emit_stream_recovery_events(resp)
            if resp.reasoning_content and not streamed_reasoning:
                self.agent._emit_event(AgentEvent.reasoning_token(resp.reasoning_content))
            if budget_result := self._budget_stop_result():
                self.last_response_streamed = streamed_output
                _flush_active_draft("cancelled")
                draft_runtime.cancel_active(budget_result)
                return budget_result

            if resp.stream_status == "interrupted":
                self.last_response_streamed = streamed_output
                self.last_run_interrupted = True
                self.last_interruption_payload = self._stream_interruption_payload(resp)
                self.agent.state.messages.append(resp.message)
                _flush_active_draft("interrupted")
                draft_runtime.cancel_active("provider stream interrupted")
                return resp.content

            # No tool calls -> done
            if not resp.tool_calls:
                self.last_response_streamed = streamed_output
                self.agent.state.messages.append(resp.message)
                _flush_active_draft("final")
                draft_runtime.commit_active()
                return resp.content

            # Tool calls -> execute
            assistant_message_index = len(self.agent.state.messages)
            self.agent.state.messages.append(resp.message)

            if self.agent.stop_requested():
                _flush_active_draft("cancelled")
                draft_runtime.cancel_active("stopped by cancellation request")
                return "(stopped by cancellation request)"

            def refresh_assistant_tool_call_message() -> None:
                # PreToolUse lifecycle hooks can rewrite ToolCall objects during
                # execution; keep the stored assistant message aligned with the
                # effective calls before tool results enter history.
                self.agent.state.messages[assistant_message_index] = resp.message

            if len(resp.tool_calls) == 1:
                tc = resp.tool_calls[0]
                result = self.agent._executor.execute(tc, index=0)
                if tc.name == "draft_document_begin":
                    draft_runtime.begin_from_tool_result(result)
                refresh_assistant_tool_call_message()
                self.agent.state.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )
                self._emit_usage_update()
            else:
                # If approval is interactive, run sequentially to keep terminal UX stable.
                if self.agent.approval_provider is not None:
                    for tool_index, tc in enumerate(resp.tool_calls):
                        if self.agent.stop_requested():
                            _flush_active_draft("cancelled")
                            draft_runtime.cancel_active("stopped by cancellation request")
                            return "(stopped by cancellation request)"
                        result = self.agent._executor.execute(tc, index=tool_index)
                        if tc.name == "draft_document_begin":
                            draft_runtime.begin_from_tool_result(result)
                        refresh_assistant_tool_call_message()
                        self.agent.state.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            }
                        )
                        self._emit_usage_update()
                else:
                    # No interactive approval needed: keep parallel execution.
                    if self.agent.stop_requested():
                        _flush_active_draft("cancelled")
                        draft_runtime.cancel_active("stopped by cancellation request")
                        return "(stopped by cancellation request)"
                    results = self.agent._executor.execute_parallel(resp.tool_calls)
                    refresh_assistant_tool_call_message()
                    for tc, result in zip(resp.tool_calls, results):
                        if tc.name == "draft_document_begin":
                            draft_runtime.begin_from_tool_result(result)
                        self.agent.state.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            }
                        )
                        self._emit_usage_update()

            # Compress if tool outputs are big
            self.agent.context.maybe_compress(
                self.agent.state.messages,
                self.agent.llm,
            )
            self._emit_usage_update()

        if budget_result := self._budget_stop_result():
            _flush_active_draft("cancelled")
            draft_runtime.cancel_active(budget_result)
            return budget_result
        if max_turns_result := self._max_turns_stop_result():
            _flush_active_draft("cancelled")
            draft_runtime.cancel_active(max_turns_result)
            return max_turns_result

        summary_prompt = (
            "Maximum tool-call rounds reached. Do not call any tools. "
            "Briefly summarize the current findings/status, list any blockers or incomplete work, "
            "and end the task."
        )
        self._inject_pending_follow_ups()
        self.agent.state.messages.append({"role": "user", "content": summary_prompt})
        summary_streamed = False
        summary_reasoning_streamed = False

        def _on_summary_token(token: str) -> None:
            nonlocal summary_streamed
            summary_streamed = True
            self.agent._emit_event(AgentEvent.stream_token(token))

        def _on_summary_reasoning_token(token: str) -> None:
            nonlocal summary_reasoning_streamed
            if not token:
                return
            summary_reasoning_streamed = True
            self.agent._emit_event(AgentEvent.reasoning_token(token))

        summary_resp = self.agent.llm.chat(
            messages=self._full_messages(),
            tools=None,
            on_token=_on_summary_token,
            on_reasoning_token=_on_summary_reasoning_token,
            lifecycle_dispatcher=self.agent.lifecycle_dispatcher,
            session_id=getattr(self.agent, "current_session_id", None),
            ui_bus=self._ui_bus(),
            metadata={
                **memory_metadata_from_agent(self.agent),
                "round_index": self.agent.state.current_round,
                "active_mode": self.agent.active_mode,
                "summary_phase": True,
                "pending_tool_calls": len(self.agent._collect_pending_tool_calls()),
            },
        )
        self.last_response_streamed = summary_streamed
        self._record_response_usage(summary_resp)
        self._emit_usage_update()
        self._emit_stream_recovery_events(summary_resp)
        if summary_resp.reasoning_content and not summary_reasoning_streamed:
            self.agent._emit_event(AgentEvent.reasoning_token(summary_resp.reasoning_content))
        if summary_resp.stream_status == "interrupted":
            self.last_run_interrupted = True
            self.last_interruption_payload = self._stream_interruption_payload(summary_resp)
            self.agent.state.messages.append(summary_resp.message)
            _flush_active_draft("interrupted")
            draft_runtime.cancel_active("provider stream interrupted")
            return summary_resp.content
        self.agent.state.messages.append(summary_resp.message)
        _flush_active_draft("final")
        draft_runtime.commit_active()
        return summary_resp.content or "(reached maximum tool-call rounds)"

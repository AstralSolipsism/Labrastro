"""Delegated AgentRun submission tool."""

from __future__ import annotations

import json

from labrastro_server.services.agent_runtime.control_plane import AgentRunRequest
from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.agent.runtime_boundary import (
    runtime_agent_run_id,
    runtime_working_directory,
)
from reuleauxcoder.domain.hooks.lifecycle import (
    build_lifecycle_event_context,
    lifecycle_output_audit_fields,
    lifecycle_runtime_artifacts_for_event,
)
from reuleauxcoder.domain.hooks.lifecycle_policy import (
    lifecycle_gate_output_is_terminal,
    lifecycle_output_message,
)
from reuleauxcoder.domain.permission_gateway import PermissionGateway
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.extensions.tools.spec import ToolRisk


@register_tool
class DelegateAgentTool(Tool):
    name = "delegate_agent"
    risk = ToolRisk.CAPABILITY
    permission_policy = "capability"
    description = (
        "Delegate work to a configured persistent Agent. "
        "The target must be an AgentConfig id from the server Agent Registry. "
        "Each delegation creates a durable AgentRun with source='delegation'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Persistent AgentConfig id to run.",
            },
            "task": {
                "type": "string",
                "description": "Concrete work prompt for the delegated AgentRun.",
            },
            "run_mode": {
                "type": "string",
                "enum": ["background"],
                "description": "Delegated AgentRuns are submitted as background work.",
            },
        },
        "required": ["agent_id", "task"],
    }

    _parent_agent = None

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def preflight_validate(self, **kwargs) -> str | None:
        agent_id = str(kwargs.get("agent_id") or "").strip()
        task = str(kwargs.get("task") or "").strip()
        if not agent_id:
            return "Error: 'agent_id' is required."
        if not task:
            return "Error: 'task' is required."
        if self._parent_agent is None:
            return "Error: delegate_agent is not initialized with a parent Agent."
        if self._runtime_control_plane() is None:
            return "Error: AgentRun control plane is unavailable."
        delegation_error = self._delegation_error(agent_id)
        if delegation_error:
            return delegation_error
        return None

    def execute(
        self,
        agent_id: str = "",
        task: str = "",
        run_mode: str = "background",
    ) -> str:
        if self._parent_agent is None:
            return "Error: delegate_agent is not initialized with a parent Agent."
        return self.run_backend(agent_id=agent_id, task=task, run_mode=run_mode)

    @backend_handler("local")
    def _execute_local(
        self,
        agent_id: str = "",
        task: str = "",
        run_mode: str = "background",
    ) -> str:
        validation_error = self.preflight_validate(agent_id=agent_id, task=task)
        if validation_error:
            return validation_error

        parent = self._parent_agent
        control = self._runtime_control_plane()
        parent_run_id = runtime_agent_run_id(parent)
        metadata = {
            "agent_run_source": "delegation",
            "delegated_by_run_id": parent_run_id,
            "run_mode": run_mode or "background",
        }
        current_session_id = getattr(parent, "current_session_id", None)
        if current_session_id:
            metadata["parent_session_id"] = str(current_session_id)
        workspace_root = runtime_working_directory(parent)
        if workspace_root:
            metadata["workspace_root"] = str(workspace_root)

        task_payload = self._delegation_lifecycle_payload(
            agent_id=agent_id,
            task=task,
            parent_run_id=parent_run_id,
            run_mode=run_mode,
            workspace_root=workspace_root,
        )
        blocked_message = self._dispatch_delegation_lifecycle(
            "TaskCreated",
            task_payload,
        )
        if blocked_message:
            return f"Error: {blocked_message}"

        run = control.submit_agent_run(
            AgentRunRequest(
                issue_id=f"delegation:{agent_id}",
                agent_id=str(agent_id).strip(),
                prompt=str(task).strip(),
                source="delegation",
                delegated_by_run_id=parent_run_id or None,
                parent_run_id=parent_run_id or None,
                workdir=str(workspace_root) if workspace_root else None,
                metadata=metadata,
            )
        )
        subagent_payload = dict(task_payload)
        subagent_payload.update({
            "child_agent_run_id": str(getattr(run, "id", "") or ""),
            "status": str(getattr(getattr(run, "status", None), "value", "") or ""),
        })
        self._dispatch_delegation_lifecycle("SubagentStart", subagent_payload)
        payload = {
            "agent_run_id": run.id,
            "agent_id": run.agent_id,
            "source": run.source.value,
            "status": run.status.value,
        }
        return "Delegated AgentRun submitted: " + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )

    def _runtime_control_plane(self):
        parent = self._parent_agent
        return getattr(parent, "agent_run_control_plane", None)

    def _delegation_lifecycle_payload(
        self,
        *,
        agent_id: str,
        task: str,
        parent_run_id: str,
        run_mode: str,
        workspace_root: str,
    ) -> dict:
        return {
            "tool_name": self.name,
            "agent_id": str(agent_id).strip(),
            "task": str(task).strip(),
            "parent_run_id": str(parent_run_id or ""),
            "run_mode": str(run_mode or "background"),
            "workspace_root": str(workspace_root or ""),
            "source": "delegation",
        }

    def _dispatch_delegation_lifecycle(
        self,
        event_name: str,
        payload: dict,
    ) -> str:
        parent = self._parent_agent
        dispatcher = getattr(parent, "lifecycle_dispatcher", None)
        dispatch = getattr(dispatcher, "dispatch", None)
        if not callable(dispatch):
            return ""
        context = build_lifecycle_event_context(
            event_name,
            placement="server",
            trigger_source="delegation",
            session_run_id=str(getattr(parent, "current_session_id", "") or ""),
            agent_run_id=str(payload.get("parent_run_id") or runtime_agent_run_id(parent)),
            turn_id=str(getattr(parent, "runtime_turn_id", "") or ""),
            origin="agent",
            locale=str(getattr(parent, "locale", "") or ""),
            metadata={
                "tool_name": self.name,
                "agent_id": str(payload.get("agent_id") or ""),
            },
            payload=payload,
        )
        try:
            results = list(dispatch(context))
        except Exception as exc:
            message = f"{event_name} lifecycle dispatch failed."
            self._emit_delegation_lifecycle_observation(
                "dispatch_failed",
                context,
                error=f"{type(exc).__name__}: {exc}",
                message=message,
            )
            return message if event_name == "TaskCreated" else ""
        blocked_message = ""
        for result in results:
            self._emit_delegation_lifecycle_observation(
                "result",
                context,
                result=result,
            )
            output = getattr(result, "output", None)
            if event_name == "TaskCreated" and lifecycle_gate_output_is_terminal(output):
                blocked_message = lifecycle_output_message(
                    output,
                    fallback="TaskCreated lifecycle blocked delegated AgentRun.",
                )
                break
        return blocked_message

    def _emit_delegation_lifecycle_observation(
        self,
        phase: str,
        context: object,
        *,
        result: object | None = None,
        error: str = "",
        message: str = "",
    ) -> None:
        parent = self._parent_agent
        emit = getattr(parent, "_emit_event", None)
        if not callable(emit):
            return
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
            level = (
                "error"
                if error or decision == "deny" or continue_flow is False
                else "warning"
                if diagnostics
                else "info"
            )
            event_payload = {
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
                event_payload.update(lifecycle_output_audit_fields(output))
                event_payload["output"] = output_dict
                output_message = lifecycle_output_message(output, fallback="")
                if output_message:
                    event_payload["message"] = output_message
            if message:
                event_payload["message"] = message
            emit(
                AgentEvent.lifecycle_hook(
                    event_payload,
                    runtime_artifacts=lifecycle_runtime_artifacts_for_event(
                        output,
                        event_name=str(getattr(context, "event_name", "") or ""),
                        context=context,
                    ),
                )
            )
        except Exception:
            return

    def _delegation_error(self, agent_id: str) -> str | None:
        config = getattr(self._parent_agent, "runtime_config", None)
        registry = getattr(config, "agent_registry", None)
        agents = getattr(registry, "agents", {}) if registry is not None else {}
        agent = agents.get(str(agent_id).strip())
        if agent is None:
            return f"Error: AgentConfig not found: {agent_id}"
        decision = PermissionGateway().evaluate_agent_invocation(
            agent,
            source="delegation",
            interactive=False,
        )
        if not decision.allowed:
            return (
                "Error: AgentConfig delegation denied by permission gateway: "
                f"{decision.reason}"
            )
        return None

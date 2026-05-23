"""Delegated AgentRun submission tool."""

from __future__ import annotations

import json

from labrastro_server.services.agent_runtime.control_plane import AgentRunRequest
from reuleauxcoder.domain.permission_gateway import PermissionGateway
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


@register_tool
class DelegateAgentTool(Tool):
    name = "delegate_agent"
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
        parent_run_id = str(getattr(parent, "runtime_agent_id", "") or "")
        metadata = {
            "agent_run_source": "delegation",
            "delegated_by_run_id": parent_run_id,
            "run_mode": run_mode or "background",
        }
        current_session_id = getattr(parent, "current_session_id", None)
        if current_session_id:
            metadata["parent_session_id"] = str(current_session_id)
        workspace_root = getattr(parent, "runtime_working_directory", None)
        if workspace_root:
            metadata["workspace_root"] = str(workspace_root)

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

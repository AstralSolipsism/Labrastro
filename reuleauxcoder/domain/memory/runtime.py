"""Runtime helpers for binding core agents to private memory scopes."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def memory_metadata_from_agent(agent: Any) -> dict[str, str]:
    """Extract memory scope metadata from a ReuleauxCoder core agent."""

    config = getattr(agent, "runtime_config", None) or getattr(agent, "config", None)
    memory_config = getattr(config, "memory", None)
    default_agent_id = getattr(memory_config, "default_agent_id", "core")
    owner = (
        getattr(agent, "memory_owner_agent_id", None)
        or getattr(agent, "agent_id", None)
        or default_agent_id
        or ""
    )
    namespace = (
        getattr(agent, "memory_namespace", None)
        or getattr(memory_config, "default_namespace", "")
        or owner
    )
    workspace_id = getattr(agent, "memory_workspace_id", None) or getattr(
        agent, "workspace_id", None
    )
    if not workspace_id:
        runtime_cwd = getattr(agent, "runtime_working_directory", None)
        workspace_id = str(Path(str(runtime_cwd)).resolve()) if runtime_cwd else ""
    values = {
        "owner_agent_id": owner,
        "memory_namespace": namespace,
        "project_id": getattr(agent, "memory_project_id", None)
        or getattr(agent, "project_id", None)
        or "",
        "workspace_id": workspace_id,
        "repo_id": getattr(agent, "memory_repo_id", None)
        or getattr(agent, "repo_id", None)
        or "",
        "goal_id": getattr(agent, "memory_goal_id", None)
        or getattr(agent, "goal_id", None)
        or "",
        "task_id": getattr(agent, "memory_task_id", None)
        or getattr(agent, "task_id", None)
        or "",
        "session_id": getattr(agent, "current_session_id", None) or "",
        "sensitivity": getattr(agent, "memory_sensitivity", None) or "",
    }
    return {key: str(value) for key, value in values.items() if str(value or "").strip()}


def bind_memory_scope_to_agent(
    agent: Any,
    *,
    owner_agent_id: str,
    memory_namespace: str | None = None,
    project_id: str | None = None,
    workspace_id: str | None = None,
    repo_id: str | None = None,
    goal_id: str | None = None,
    task_id: str | None = None,
    taskflow_id: str | None = None,
    issue_id: str | None = None,
) -> None:
    """Attach stable memory scope attributes to an in-process core agent."""

    setattr(agent, "memory_owner_agent_id", str(owner_agent_id or ""))
    setattr(agent, "memory_namespace", str(memory_namespace or owner_agent_id or ""))
    for attr, value in (
        ("memory_project_id", project_id),
        ("memory_workspace_id", workspace_id),
        ("memory_repo_id", repo_id),
        ("memory_goal_id", goal_id),
        ("memory_task_id", task_id),
        ("memory_taskflow_id", taskflow_id),
        ("memory_issue_id", issue_id),
    ):
        if value is not None:
            setattr(agent, attr, str(value))

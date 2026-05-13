"""Runtime helpers for binding core agents to private memory scopes."""

from __future__ import annotations

from pathlib import Path
from typing import Any


MAIN_CHAT_MEMORY_NAMESPACE = "main-chat"
GLOBAL_MEMORY_PROJECT_ID = "__global__"
ACCOUNT_MEMORY_OWNER_PREFIX = "account:"


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
    if not workspace_id and not getattr(agent, "memory_disable_workspace_fallback", False):
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


def bind_main_chat_memory_scope_to_agent(agent: Any, *, peer_info: Any) -> bool:
    """Bind the user-facing main chat agent to account-scoped memory.

    Returns True when an authenticated account identity was available. Other
    agent types should keep using bind_memory_scope_to_agent directly.
    """

    meta = getattr(peer_info, "meta", None)
    principal = meta.get("auth_principal") if isinstance(meta, dict) else None
    if not isinstance(principal, dict):
        return False
    user_id = str(principal.get("user_id") or "").strip()
    if not user_id:
        return False
    bind_memory_scope_to_agent(
        agent,
        owner_agent_id=f"{ACCOUNT_MEMORY_OWNER_PREFIX}{user_id}",
        memory_namespace=MAIN_CHAT_MEMORY_NAMESPACE,
        project_id=GLOBAL_MEMORY_PROJECT_ID,
        workspace_id="",
        repo_id="",
    )
    setattr(agent, "memory_disable_workspace_fallback", True)
    setattr(agent, "memory_scope_kind", "main_chat_account")
    setattr(agent, "memory_account_user_id", user_id)
    username = str(principal.get("username") or "").strip()
    if username:
        setattr(agent, "memory_account_username", username)
    device_id = str(principal.get("device_id") or "").strip()
    if device_id:
        setattr(agent, "memory_account_device_id", device_id)
    return True

"""Helpers for AgentRun-bound runtime identity and workspace state."""

from __future__ import annotations

from typing import Any


def _runtime_attr(agent: Any, *names: str) -> str:
    for name in names:
        value = getattr(agent, name, None)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def runtime_agent_run_id(agent: Any) -> str:
    """Return the canonical AgentRun id with legacy fallbacks."""

    return _runtime_attr(
        agent,
        "runtime_agent_run_id",
        "runtime_task_id",
        "runtime_agent_id",
    )


def runtime_branch_binding_id(agent: Any) -> str:
    """Return the branch binding id for the current AgentRun runtime scope."""

    return _runtime_attr(agent, "runtime_branch_binding_id")


def runtime_workspace_root(agent: Any) -> str:
    """Return the stable workspace root for the current runtime."""

    return _runtime_attr(
        agent,
        "runtime_workspace_root",
        "runtime_working_directory",
    )


def runtime_working_directory(agent: Any) -> str:
    """Return the current working directory with workspace-root fallback."""

    return _runtime_attr(
        agent,
        "runtime_working_directory",
        "runtime_workspace_root",
    )


def runtime_execution_target(agent: Any) -> str:
    """Return the runtime execution target label used in audits and approvals."""

    return _runtime_attr(agent, "runtime_execution_target") or "local"


def runtime_path_space(agent: Any) -> str:
    """Return the canonical path-space label for the current runtime boundary."""

    agent_run_id = runtime_agent_run_id(agent)
    workspace_root = runtime_workspace_root(agent)
    execution_target = runtime_execution_target(agent)
    if agent_run_id and workspace_root:
        return "agent_run_worktree"
    if execution_target in {"local", "local_workspace"}:
        return "local_workspace"
    return execution_target or "agent_runtime"


def runtime_boundary_fields(agent: Any) -> dict[str, str]:
    """Return canonical runtime identity/path fields for permission/audit payloads."""

    if agent is None:
        return {}
    return {
        "agent_run_id": runtime_agent_run_id(agent),
        "branch_binding_id": runtime_branch_binding_id(agent),
        "runtime_workspace_root": runtime_workspace_root(agent),
        "runtime_working_directory": runtime_working_directory(agent),
        "execution_target": runtime_execution_target(agent),
        "path_space": runtime_path_space(agent),
    }

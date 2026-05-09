"""Compatibility import path for the single-mainline TaskflowService."""

from labrastro_server.taskflow.application.taskflow_service import TaskflowService

TASKFLOW_WORKFLOW_MODE = "taskflow"
TASKFLOW_SYSTEM_PROMPT = (
    "You are running in Taskflow mode. Keep the conversation focused on "
    "clarifying the current goal, surfacing assumptions, and preparing the "
    "TaskflowState for compilation into WorkItems."
)

__all__ = ["TASKFLOW_SYSTEM_PROMPT", "TASKFLOW_WORKFLOW_MODE", "TaskflowService"]

"""State storage port for single-mainline Taskflow sessions."""

from __future__ import annotations

import threading
from typing import Protocol

from labrastro_server.taskflow.domain.taskflow_state import TaskflowState


class TaskflowStateStore(Protocol):
    """Persistence boundary for per-goal TaskflowState snapshots."""

    def get_taskflow_state(self, taskflow_id: str) -> TaskflowState:
        """Return a TaskflowState snapshot."""

    def save_taskflow_state(self, state: TaskflowState) -> None:
        """Persist a TaskflowState snapshot."""

    def list_taskflow_states(self, *, project_id: str | None = None) -> list[TaskflowState]:
        """Return TaskflowState snapshots, optionally filtered by project."""


class InMemoryTaskflowStateStore:
    """Thread-safe in-memory TaskflowState store for development and tests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, TaskflowState] = {}

    def get_taskflow_state(self, taskflow_id: str) -> TaskflowState:
        """Return a defensive copy of a TaskflowState snapshot."""

        with self._lock:
            try:
                state = self._states[taskflow_id]
            except KeyError:
                raise KeyError(f"taskflow state not found: {taskflow_id}") from None
            return TaskflowState.from_dict(state.to_dict())

    def save_taskflow_state(self, state: TaskflowState) -> None:
        """Persist a defensive copy of a TaskflowState snapshot."""

        with self._lock:
            self._states[state.meta.taskflow_id] = TaskflowState.from_dict(
                state.to_dict()
            )

    def list_taskflow_states(self, *, project_id: str | None = None) -> list[TaskflowState]:
        """Return defensive copies of known TaskflowState snapshots."""

        with self._lock:
            states = list(self._states.values())
            if project_id is not None:
                states = [
                    state for state in states if state.meta.project_id == project_id
                ]
            return [TaskflowState.from_dict(state.to_dict()) for state in states]


__all__ = ["InMemoryTaskflowStateStore", "TaskflowStateStore"]

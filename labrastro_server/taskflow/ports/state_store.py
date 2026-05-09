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


__all__ = ["InMemoryTaskflowStateStore", "TaskflowStateStore"]

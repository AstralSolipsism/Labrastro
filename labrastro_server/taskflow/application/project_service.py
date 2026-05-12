"""ProjectState persistence facade for the Taskflow compiler architecture.

Source: ``docs/文档.md`` Section 6. This service intentionally starts with an
in-memory implementation so the new architecture skeleton can be exercised
without forcing the pending Postgres Taskflow store wiring to land in the same
change.
"""

from __future__ import annotations

import threading
from typing import Protocol

from labrastro_server.taskflow.domain.project_state import ProjectState


class ProjectStateStore(Protocol):
    def get_project_state(self, project_id: str) -> ProjectState | None:
        """Return a project snapshot when present."""

    def save_project_state(self, state: ProjectState) -> None:
        """Persist a project snapshot."""


class ProjectService:
    """Store and retrieve ProjectState as the long-lived source of truth."""

    def __init__(self, *, store: ProjectStateStore | None = None) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, ProjectState] = {}
        self._store = store

    def get_project_state(self, project_id: str) -> ProjectState | None:
        """Return the ProjectState for ``project_id`` if one has been saved."""

        if self._store is not None:
            return self._store.get_project_state(project_id)
        with self._lock:
            state = self._states.get(project_id)
            return ProjectState.from_dict(state.to_dict()) if state is not None else None

    def save_project_state(self, state: ProjectState) -> None:
        """Persist a ProjectState snapshot."""

        if self._store is not None:
            state.touch()
            self._store.save_project_state(state)
            return
        with self._lock:
            state.touch()
            self._states[state.project_id] = ProjectState.from_dict(state.to_dict())

    def get_or_create_project_state(
        self, project_id: str, *, name: str = ""
    ) -> ProjectState:
        """Return a ProjectState, creating an empty one when absent."""

        if self._store is not None:
            state = self._store.get_project_state(project_id)
            if state is None:
                state = ProjectState.new(project_id=project_id, name=name)
                self.save_project_state(state)
            return state
        with self._lock:
            state = self._states.get(project_id)
            if state is None:
                state = ProjectState.new(project_id=project_id, name=name)
                self._states[project_id] = ProjectState.from_dict(state.to_dict())
                return state
            return ProjectState.from_dict(state.to_dict())


__all__ = ["ProjectService", "ProjectStateStore"]

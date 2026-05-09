"""ProjectState persistence facade for the Taskflow compiler architecture.

Source: ``docs/文档.md`` Section 6. This service intentionally starts with an
in-memory implementation so the new architecture skeleton can be exercised
without forcing the existing Postgres Taskflow store to migrate in the same
change.
"""

from __future__ import annotations

import threading

from labrastro_server.taskflow.domain.project_state import ProjectState


class ProjectService:
    """Store and retrieve ProjectState as the long-lived source of truth."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, ProjectState] = {}

    def get_project_state(self, project_id: str) -> ProjectState | None:
        """Return the ProjectState for ``project_id`` if one has been saved."""

        with self._lock:
            state = self._states.get(project_id)
            return ProjectState.from_dict(state.to_dict()) if state is not None else None

    def save_project_state(self, state: ProjectState) -> None:
        """Persist a ProjectState snapshot."""

        with self._lock:
            state.touch()
            self._states[state.project_id] = ProjectState.from_dict(state.to_dict())

    def get_or_create_project_state(
        self, project_id: str, *, name: str = ""
    ) -> ProjectState:
        """Return a ProjectState, creating an empty one when absent."""

        with self._lock:
            state = self._states.get(project_id)
            if state is None:
                state = ProjectState.new(project_id=project_id, name=name)
                self._states[project_id] = ProjectState.from_dict(state.to_dict())
                return state
            return ProjectState.from_dict(state.to_dict())


__all__ = ["ProjectService"]

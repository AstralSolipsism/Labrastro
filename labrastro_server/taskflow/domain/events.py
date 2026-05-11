"""Taskflow event helpers.

Events are stored in the TaskflowState snapshot so discovery and compilation
can be replayed without relying on chat transcript text.
"""

from __future__ import annotations

import uuid
from typing import Any

from labrastro_server.taskflow.domain.taskflow_state import (
    TaskflowEvent,
    TaskflowEventType,
    TaskflowState,
)


def new_event_id() -> str:
    return f"taskflow-event-{uuid.uuid4().hex}"


def append_taskflow_event(
    state: TaskflowState,
    event_type: TaskflowEventType | str,
    *,
    actor: str = "",
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> TaskflowEvent:
    """Append and return a typed event for ``state``."""

    event = TaskflowEvent(
        id=new_event_id(),
        type=event_type,
        taskflow_id=state.meta.taskflow_id,
        project_id=state.meta.project_id,
        goal_id=state.meta.goal_id,
        actor=actor,
        payload=dict(payload or {}),
        metadata=dict(metadata or {}),
    )
    state.events.append(event)
    return event


__all__ = ["append_taskflow_event", "new_event_id"]

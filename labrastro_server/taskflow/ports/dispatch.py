"""Executor-neutral Taskflow dispatch contracts.

Taskflow core compiles and authorizes work. Concrete execution belongs behind
this port so ReuleauxCoder can remain the built-in executor adapter rather than
the architecture boundary itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from labrastro_server.taskflow.domain.project_state import TaskRun


@dataclass(slots=True)
class ExecutorCandidate:
    """Generic executor candidate used by Taskflow scheduling."""

    executor_id: str
    capabilities: list[str] = field(default_factory=list)
    runtime_profile_id: str | None = None
    execution_location: str | None = None
    running_count: int = 0
    max_concurrent_tasks: int | None = None
    specialties: list[str] = field(default_factory=list)
    workflows: list[str] = field(default_factory=list)
    task_types: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def signals(self) -> set[str]:
        """Return all soft-matching signals exposed by this executor."""

        values: set[str] = set(self.capabilities)
        values.update(self.specialties)
        values.update(self.workflows)
        values.update(self.task_types)
        return values


@dataclass(slots=True)
class TaskflowDispatchResult:
    """Executor-neutral dispatch or scheduling result."""

    selected_executor_id: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    filtered: list[dict[str, Any]] = field(default_factory=list)
    score_summary: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    manual_override: bool = False
    runtime_task_id: str | None = None
    runtime_task: dict[str, Any] | None = None

    @property
    def selected(self) -> bool:
        return bool(self.selected_executor_id)


class TaskflowDispatcher(Protocol):
    """Port implemented by concrete executor adapters."""

    def dispatch_task_run(
        self,
        task_run: TaskRun,
        *,
        executor_hint: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskflowDispatchResult:
        """Dispatch a TaskRun through a concrete executor."""


__all__ = ["ExecutorCandidate", "TaskflowDispatcher", "TaskflowDispatchResult"]

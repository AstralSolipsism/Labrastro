"""Basic HR-style scheduling helpers for Agent runtime tasks."""

from __future__ import annotations

from dataclasses import dataclass, field

from reuleauxcoder.domain.agent_runtime.models import AgentConfig, TaskRecord, TaskStatus


@dataclass(frozen=True)
class AgentScheduleDecision:
    """Selected Agent and the reason for the assignment."""

    agent_id: str
    reason: str


@dataclass
class BasicAgentScheduler:
    """Select a configured Agent by current in-flight task count."""

    agents: dict[str, AgentConfig]
    default_agent_id: str | None = None
    running_tasks: list[TaskRecord] = field(default_factory=list)

    def choose_agent(self) -> AgentScheduleDecision:
        candidates = list(self.agents.values())
        if not candidates and self.default_agent_id in self.agents:
            return AgentScheduleDecision(
                agent_id=str(self.default_agent_id),
                reason="default_agent",
            )
        if not candidates:
            raise ValueError("no agent is configured")

        ranked = sorted(
            candidates,
            key=lambda agent: (
                self._running_count(agent.id),
                agent.max_concurrent_tasks or 999999,
                agent.id,
            ),
        )
        selected = ranked[0]
        limit = selected.max_concurrent_tasks
        if limit is not None and self._running_count(selected.id) >= limit:
            raise RuntimeError(f"agent concurrency limit reached: {selected.id}")
        return AgentScheduleDecision(
            agent_id=selected.id,
            reason="lowest_running_count",
        )

    def _running_count(self, agent_id: str) -> int:
        return sum(
            1
            for task in self.running_tasks
            if task.agent_id == agent_id
            and task.status
            in {TaskStatus.DISPATCHED, TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL}
        )


__all__ = ["AgentScheduleDecision", "BasicAgentScheduler"]

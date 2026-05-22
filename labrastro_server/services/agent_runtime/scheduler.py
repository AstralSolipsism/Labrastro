"""Basic HR-style scheduling helpers for AgentRuns."""

from __future__ import annotations

from dataclasses import dataclass, field

from reuleauxcoder.domain.agent_runtime.models import AgentConfig, AgentRunRecord, TaskStatus


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
    running_tasks: list[AgentRunRecord] = field(default_factory=list)

    def choose_agent(self) -> AgentScheduleDecision:
        candidates = [
            agent for agent in self.agents.values() if agent.can_run_taskflow
        ]
        default_agent = (
            self.agents.get(str(self.default_agent_id))
            if self.default_agent_id is not None
            else None
        )
        if not candidates and default_agent is not None and default_agent.can_run_taskflow:
            return AgentScheduleDecision(
                agent_id=str(self.default_agent_id),
                reason="default_agent",
            )
        if not candidates:
            raise ValueError("no taskflow-eligible agent is configured")

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

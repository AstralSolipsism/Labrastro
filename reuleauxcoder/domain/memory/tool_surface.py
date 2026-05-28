"""Policy for exposing memory as agent-visible tools."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class MemoryToolSurfacePolicy:
    """Opt-in policy for recall/remember/forget tools."""

    enabled: bool = False
    provider: str = ""
    allowed_agents: list[str] = field(default_factory=list)
    recall: bool = False
    remember: bool = False
    forget: bool = False
    list: bool = False

    def allows_agent(self, agent_id: str) -> bool:
        if not self.enabled:
            return False
        allowed = {str(item).strip() for item in self.allowed_agents if str(item).strip()}
        return not allowed or str(agent_id or "").strip() in allowed

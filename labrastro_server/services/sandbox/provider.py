"""SandboxProvider contract.

A sandbox is the durable execution room for a workspace. A sandbox session is
one temporary worker container/process running inside that room for an AgentRun.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SandboxProfile:
    """Minimum sandbox policy used by providers."""

    image: str = "labrastro-host:test"
    cpu_limit: str = ""
    memory_limit: str = ""
    network: str = ""
    workspace_volume_prefix: str = "ezcode-workspace"
    idle_ttl_seconds: int = 3600
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxRef:
    """Durable workspace room managed by a SandboxProvider."""

    id: str
    workspace_ref: str
    volume_name: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxSessionRef:
    """Temporary execution session inside a sandbox."""

    id: str
    sandbox_id: str
    agent_run_id: str
    container_id: str = ""
    status: str = "starting"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkspaceMountRef:
    """Prepared workspace mount for one sandbox session."""

    session_id: str
    path: str
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRunExecution:
    """Execution handle/result returned by a provider."""

    session_id: str
    status: str
    output: str = ""
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class SandboxProvider(Protocol):
    """Execution room provider for AgentRun workers."""

    def ensure_sandbox(
        self,
        workspace_ref: str,
        profile: SandboxProfile,
        metadata: dict[str, Any] | None = None,
    ) -> SandboxRef:
        ...

    def start_session(
        self,
        sandbox_id: str,
        runtime_profile: dict[str, Any],
        agent_run_id: str,
    ) -> SandboxSessionRef:
        ...

    def prepare_workspace(
        self,
        session_id: str,
        source: dict[str, Any] | None = None,
    ) -> WorkspaceMountRef:
        ...

    def exec_agent_run(
        self,
        session_id: str,
        executor_request: dict[str, Any],
    ) -> AgentRunExecution:
        ...

    def heartbeat(self, session_id: str) -> bool:
        ...

    def cancel(self, session_id: str) -> bool:
        ...

    def stop_session(self, session_id: str) -> bool:
        ...

    def gc(self) -> dict[str, Any]:
        ...

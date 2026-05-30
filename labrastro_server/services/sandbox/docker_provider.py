"""Docker-backed SandboxProvider.

The provider talks to the host Docker CLI/API boundary and keeps Docker-specific
lifecycle details out of Agent, Taskflow, and AgentRun scheduling.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from labrastro_server.services.sandbox.provider import (
    AgentRunExecution,
    SandboxProfile,
    SandboxRef,
    SandboxSessionRef,
    WorkspaceMountRef,
)


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-")
    return cleaned[:48] or "workspace"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass
class DockerSandboxProvider:
    """Single-host Docker implementation for on-demand worker sessions."""

    docker_bin: str = "docker"
    host_base_url: str = ""
    bootstrap_token: str = ""
    bootstrap_token_factory: Callable[[], str] | None = None
    worker_command: str = "/app/artifacts/remote/linux/amd64/rcoder-peer"
    workspace_path: str = "/workspace"
    dry_run: bool = False
    _sandboxes: dict[str, SandboxRef] = field(default_factory=dict)
    _sessions: dict[str, SandboxSessionRef] = field(default_factory=dict)

    def ensure_sandbox(
        self,
        workspace_ref: str,
        profile: SandboxProfile,
        metadata: dict[str, Any] | None = None,
    ) -> SandboxRef:
        sandbox_id = f"sbx-{_safe_id(workspace_ref)}-{_digest(workspace_ref)}"
        volume_name = f"{profile.workspace_volume_prefix}-{_digest(workspace_ref)}"
        if sandbox_id not in self._sandboxes:
            self._run([self.docker_bin, "volume", "create", volume_name])
            self._sandboxes[sandbox_id] = SandboxRef(
                id=sandbox_id,
                workspace_ref=workspace_ref,
                volume_name=volume_name,
                metadata=dict(metadata or {}),
            )
        return self._sandboxes[sandbox_id]

    def start_session(
        self,
        sandbox_id: str,
        runtime_profile: dict[str, Any],
        agent_run_id: str,
    ) -> SandboxSessionRef:
        sandbox = self._sandboxes.get(sandbox_id)
        if sandbox is None:
            raise KeyError(f"sandbox not found: {sandbox_id}")
        profile = _profile_from_runtime(runtime_profile)
        session_id = f"ssn-{_digest(agent_run_id + sandbox_id)}"
        name = f"labrastro-{session_id}"
        command = [
            self.worker_command,
            "--host",
            self.host_base_url,
            "--bootstrap-token",
            self._bootstrap_token(),
            "--agent-run-worker",
            "--worker-session-id",
            session_id,
            "--agent-run-worker-kind",
            "sandbox_worker",
            "--workspace-root",
            self.workspace_path,
            "--cwd",
            self.workspace_path,
        ]
        args = [
            self.docker_bin,
            "run",
            "-d",
            "--name",
            name,
            "-v",
            f"{sandbox.volume_name}:{self.workspace_path}",
        ]
        if profile.network:
            args.extend(["--network", profile.network])
        if profile.memory_limit:
            args.extend(["--memory", profile.memory_limit])
        if profile.cpu_limit:
            args.extend(["--cpus", profile.cpu_limit])
        for key, value in sorted(profile.env.items()):
            args.extend(["-e", f"{key}={value}"])
        args.append(profile.image)
        args.extend(command)
        container_id = self._run(args).strip()
        session = SandboxSessionRef(
            id=session_id,
            sandbox_id=sandbox_id,
            agent_run_id=agent_run_id,
            container_id=container_id,
            status="running",
            metadata={"container_name": name},
        )
        self._sessions[session_id] = session
        return session

    def _bootstrap_token(self) -> str:
        if self.bootstrap_token_factory is not None:
            return str(self.bootstrap_token_factory())
        return self.bootstrap_token

    def prepare_workspace(
        self,
        session_id: str,
        source: dict[str, Any] | None = None,
    ) -> WorkspaceMountRef:
        if session_id not in self._sessions:
            raise KeyError(f"sandbox session not found: {session_id}")
        return WorkspaceMountRef(
            session_id=session_id,
            path=self.workspace_path,
            source=str((source or {}).get("source") or ""),
            metadata=dict(source or {}),
        )

    def exec_agent_run(
        self,
        session_id: str,
        executor_request: dict[str, Any],
    ) -> AgentRunExecution:
        if session_id not in self._sessions:
            raise KeyError(f"sandbox session not found: {session_id}")
        return AgentRunExecution(
            session_id=session_id,
            status="dispatched",
            metadata={"executor_request": dict(executor_request)},
        )

    def heartbeat(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None:
            return False
        result = self._run(
            [self.docker_bin, "inspect", "-f", "{{.State.Running}}", session.container_id],
            check=False,
        ).strip()
        return result == "true"

    def cancel(self, session_id: str) -> bool:
        return self.stop_session(session_id)

    def stop_session(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        self._run([self.docker_bin, "rm", "-f", session.container_id], check=False)
        return True

    def gc(self) -> dict[str, Any]:
        stopped = 0
        for session_id in list(self._sessions):
            if not self.heartbeat(session_id):
                self._sessions.pop(session_id, None)
                stopped += 1
        return {"ok": True, "stale_sessions_removed": stopped}

    def _run(self, args: Sequence[str], *, check: bool = True) -> str:
        if self.dry_run:
            return "dry-run"
        completed = subprocess.run(
            list(args),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
        return completed.stdout


def _profile_from_runtime(runtime_profile: dict[str, Any]) -> SandboxProfile:
    sandbox = runtime_profile.get("sandbox") if isinstance(runtime_profile, dict) else {}
    if not isinstance(sandbox, dict):
        sandbox = {}
    return SandboxProfile(
        image=str(sandbox.get("image") or runtime_profile.get("worker_image") or "labrastro-host:test"),
        cpu_limit=str(sandbox.get("cpu_limit") or ""),
        memory_limit=str(sandbox.get("memory_limit") or ""),
        network=str(sandbox.get("network") or ""),
        workspace_volume_prefix=str(sandbox.get("workspace_volume_prefix") or "labrastro-workspace"),
        idle_ttl_seconds=int(sandbox.get("idle_ttl_seconds") or 3600),
        env={str(k): str(v) for k, v in dict(sandbox.get("env") or {}).items()},
    )

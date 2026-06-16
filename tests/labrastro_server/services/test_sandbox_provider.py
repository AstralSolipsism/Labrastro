from labrastro_server.services.agent_runtime.control_plane import (
    AgentRunRequest,
    AgentRunControlPlane,
)
from labrastro_server.services.agent_runtime.executor_backend import ExecutorRunResult
from labrastro_server.services.sandbox import DockerSandboxProvider, SandboxProfile
from labrastro_server.services.sandbox.provider import (
    AgentRunExecution,
    SandboxRef,
    SandboxSessionRef,
    WorkspaceMountRef,
)


def _current_activation_id(control: AgentRunControlPlane, task_id: str) -> str:
    return str(control.get_agent_run(task_id).current_activation_id or "")


def test_docker_sandbox_provider_builds_room_and_session_in_dry_run() -> None:
    provider = DockerSandboxProvider(
        host_base_url="http://host.docker.internal:8765",
        bootstrap_token="bt-test",
        dry_run=True,
    )
    profile = SandboxProfile(
        image="labrastro-worker:test",
        network="labrastro-net",
        memory_limit="1g",
        cpu_limit="1.5",
    )

    sandbox = provider.ensure_sandbox("repo:https://example.test/repo.git", profile)
    session = provider.start_session(
        sandbox.id,
        {
            "sandbox": {
                "image": "labrastro-worker:test",
                "network": "labrastro-net",
                "memory_limit": "1g",
                "cpu_limit": "1.5",
            }
        },
        "run-1",
    )
    mount = provider.prepare_workspace(session.id, {"source": "git"})
    execution = provider.exec_agent_run(session.id, {"agent_id": "coder"})

    assert sandbox.id.startswith("sbx-")
    assert sandbox.volume_name.startswith("labrastro-workspace-")
    assert session.sandbox_id == sandbox.id
    assert session.agent_run_id == "run-1"
    assert session.metadata["container_name"].startswith("labrastro-ssn-")
    assert mount.path == "/workspace"
    assert execution.status == "dispatched"
    assert provider.stop_session(session.id) is True


class _FakeSandboxProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.stopped: list[str] = []

    def ensure_sandbox(self, workspace_ref, profile, metadata=None):
        self.calls.append("ensure_sandbox")
        return SandboxRef(
            id="sbx-test",
            workspace_ref=workspace_ref,
            volume_name="vol-test",
            metadata=metadata or {},
        )

    def start_session(self, sandbox_id, runtime_profile, agent_run_id):
        self.calls.append("start_session")
        return SandboxSessionRef(
            id=f"ssn-{agent_run_id}",
            sandbox_id=sandbox_id,
            agent_run_id=agent_run_id,
            container_id="container-test",
            status="running",
        )

    def prepare_workspace(self, session_id, source=None):
        self.calls.append("prepare_workspace")
        return WorkspaceMountRef(
            session_id=session_id,
            path="/workspace",
            source=str((source or {}).get("source") or ""),
            metadata=source or {},
        )

    def exec_agent_run(self, session_id, executor_request):
        self.calls.append("exec_agent_run")
        return AgentRunExecution(session_id=session_id, status="dispatched")

    def heartbeat(self, session_id):
        return True

    def cancel(self, session_id):
        self.stopped.append(f"cancel:{session_id}")
        return True

    def stop_session(self, session_id):
        self.stopped.append(session_id)
        return True

    def gc(self):
        return {"ok": True}


def test_control_plane_starts_sandbox_session_for_sandbox_worker_run_and_stops_on_complete() -> None:
    provider = _FakeSandboxProvider()
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "docker_profile": {
                    "executor": "reuleauxcoder",
                    "execution_location": "remote_server",
                    "worker_kind": "sandbox_worker",
                    "sandbox": {},
                }
            },
            "agents": {"coder": {"runtime_profile": "docker_profile"}},
        },
        sandbox_provider=provider,
        sandbox_profile=SandboxProfile(image="worker:test"),
    )

    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="hello",
            metadata={"workspace_root": "G:/repo"},
        ),
        task_id="run-1",
    )

    assert provider.calls == [
        "ensure_sandbox",
        "start_session",
        "prepare_workspace",
        "exec_agent_run",
    ]
    assert task.sandbox_id == "sbx-test"
    assert task.sandbox_session_id == "ssn-run-1"
    assert task.workspace_ref == "G:/repo"
    assert task.workdir == "/workspace"
    assert task.metadata["source_workspace_root"] == "G:/repo"
    assert task.metadata["workspace_root"] == "/workspace"

    claim = control.claim_agent_run_activation(
        worker_id="ssn-run-1",
        worker_kind="sandbox_worker",
        executors=["reuleauxcoder"],
        peer_features=["agent_runs.remote_server", "worker_kind:sandbox_worker"],
        workspace_root="/workspace",
    )

    assert claim is not None
    assert claim.task.id == "run-1"

    control.complete_agent_run_activation(
        "run-1",
        ExecutorRunResult(task_id="run-1", status="completed", output="done"),
        activation_id=_current_activation_id(control, "run-1"),
    )

    assert provider.stopped == ["ssn-run-1"]


def test_control_plane_does_not_start_sandbox_for_server_worker_run() -> None:
    provider = _FakeSandboxProvider()
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "server_profile": {
                    "executor": "reuleauxcoder",
                    "execution_location": "remote_server",
                    "worker_kind": "server_worker",
                }
            },
            "agents": {"coder": {"runtime_profile": "server_profile"}},
        },
        sandbox_provider=provider,
        sandbox_profile=SandboxProfile(image="worker:test"),
    )

    task = control.submit_agent_run(
        AgentRunRequest(
            agent_id="coder",
            prompt="hello",
            metadata={"workspace_root": "G:/repo"},
        ),
        task_id="run-server-worker",
    )

    assert provider.calls == []
    assert task.sandbox_id is None
    assert task.sandbox_session_id is None
    assert task.workdir is None
    assert task.metadata["worker_kind"] == "server_worker"


def test_control_plane_cancels_sandbox_session_and_marks_agent_run_cancelled() -> None:
    provider = _FakeSandboxProvider()
    control = AgentRunControlPlane(
        runtime_snapshot={
            "runtime_profiles": {
                "docker_profile": {
                    "executor": "reuleauxcoder",
                    "execution_location": "remote_server",
                    "worker_kind": "sandbox_worker",
                    "sandbox": {},
                }
            },
            "agents": {"coder": {"runtime_profile": "docker_profile"}},
        },
        sandbox_provider=provider,
    )

    control.submit_agent_run(
        AgentRunRequest(agent_id="coder", prompt="hello"),
        task_id="run-cancel",
    )

    assert control.cancel_agent_run("run-cancel", reason="stop") is True

    task = control.get_agent_run("run-cancel")
    assert task.status.value == "cancelled"
    assert provider.stopped == ["cancel:ssn-run-cancel"]

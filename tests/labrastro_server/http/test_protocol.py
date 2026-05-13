"""Tests for remote execution protocol message models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labrastro_server.interfaces.http.remote.protocol import (
    CleanupRequest,
    CleanupResult,
    ChatRequest,
    ChatStartRequest,
    ChatStartResponse,
    ChatStatusRequest,
    ChatStatusResponse,
    ChatStreamRequest,
    ChatStreamResponse,
    DisconnectNotice,
    EnvironmentCLIToolManifest,
    EnvironmentMCPServerManifest,
    EnvironmentSkillManifest,
    EnvironmentManifestRequest,
    EnvironmentManifestResponse,
    ErrorMessage,
    ExecToolRequest,
    ExecToolResult,
    Heartbeat,
    MCPArtifactManifest,
    MCPLaunchManifest,
    MCPManifestRequest,
    MCPManifestResponse,
    MCPServerManifest,
    PeerDisconnectRequest,
    PeerPollRequest,
    PeerResultRequest,
    PeerMCPToolsReport,
    RemoteMCPToolInfo,
    RegisterRejected,
    RegisterRequest,
    RegisterResponse,
    RelayEnvelope,
    REMOTE_ENDPOINTS,
    SessionModelSwitchRequest,
    SessionListRequest,
    ToolPreviewRequest,
    ToolPreviewResult,
    ToolStreamChunk,
    endpoint_registry,
)


CONTRACT_FIXTURES_PATH = (
    Path(__file__).resolve().parents[3]
    / "labrastro_server"
    / "interfaces"
    / "http"
    / "remote"
    / "protocol"
    / "contracts.json"
)


def _contract_fixtures() -> dict:
    return json.loads(CONTRACT_FIXTURES_PATH.read_text(encoding="utf-8"))


class TestRelayEnvelope:
    def test_roundtrip(self) -> None:
        env = RelayEnvelope(
            type="exec_tool",
            request_id="req-123",
            peer_id="peer-456",
            payload={"tool_name": "shell", "args": {"command": "ls"}},
        )
        d = env.to_dict()
        restored = RelayEnvelope.from_dict(d)
        assert restored.type == "exec_tool"
        assert restored.request_id == "req-123"
        assert restored.peer_id == "peer-456"
        assert restored.payload["tool_name"] == "shell"


class TestRemoteHTTPContract:
    def test_endpoint_registry_is_serializable_and_unique(self) -> None:
        registry = endpoint_registry()

        assert registry
        assert len(registry) == len({(item["method"], item["path"]) for item in registry})
        assert registry == [endpoint.to_dict() for endpoint in REMOTE_ENDPOINTS]
        assert {
            "name",
            "method",
            "path",
            "request_model",
            "response_shape",
            "auth",
        } <= set(registry[0])

    def test_contract_fixtures_have_registered_endpoints_and_error_shape(self) -> None:
        contracts = _contract_fixtures()
        endpoints = {
            (endpoint.method, endpoint.path)
            for endpoint in REMOTE_ENDPOINTS
        }

        assert contracts["version"] == 1
        for fixture in contracts["fixtures"]:
            assert (fixture["method"], fixture["path"]) in endpoints
            response = fixture["response"]
            if isinstance(response, dict) and response.get("ok") is False:
                assert set(response) == {
                    "ok",
                    "error",
                    "message",
                    "details",
                    "request_id",
                }
                assert isinstance(response["error"], str)
                assert isinstance(response["message"], str)
                assert isinstance(response["details"], dict)
                assert isinstance(response["request_id"], str)

    def test_contract_fixtures_roundtrip_protocol_models(self) -> None:
        fixtures = {
            fixture["name"]: fixture
            for fixture in _contract_fixtures()["fixtures"]
        }

        register = fixtures["peer.register"]
        RegisterRequest.from_dict(register["request"])
        RegisterResponse.from_dict(register["response"]["payload"])
        Heartbeat.from_dict(fixtures["peer.heartbeat"]["request"])
        SessionListRequest.from_dict(fixtures["sessions.list"]["request"])
        SessionModelSwitchRequest.from_dict(fixtures["sessions.model"]["request"])
        ChatStartRequest.from_dict(fixtures["chat.start"]["request"])
        ChatStartResponse.from_dict(fixtures["chat.start"]["response"])
        ChatStreamRequest.from_dict(fixtures["chat.stream"]["request"])
        ChatStreamResponse.from_dict(fixtures["chat.stream"]["response"])
        ChatStatusRequest.from_dict(fixtures["chat.status"]["request"])
        ChatStatusResponse.from_dict(fixtures["chat.status"]["response"])
        EnvironmentManifestRequest.from_dict(fixtures["environment.manifest"]["request"])
        EnvironmentManifestResponse.from_dict(fixtures["environment.manifest"]["response"])

    def test_registry_matches_actual_peer_token_control_plane_routes(self) -> None:
        registry = {endpoint["name"]: endpoint for endpoint in endpoint_registry()}

        assert registry["mcp.artifact"]["auth"] == "peer_token"
        assert (
            registry["artifacts.get"]["path"]
            == "/remote/artifacts/{os}/{arch}/{artifact_name}"
        )
        assert registry["peer.disconnect"]["request_model"] == "PeerDisconnectRequest"
        for name in (
            "taskflow.get",
            "taskflow.post",
            "issues.get",
            "issues.post",
            "assignments.get",
            "assignments.post",
            "mentions.get",
            "mentions.post",
        ):
            assert registry[name]["auth"] == "peer_token"


class TestRegisterRequest:
    def test_roundtrip(self) -> None:
        req = RegisterRequest(
            bootstrap_token="bt_abc",
            cwd="/tmp",
            workspace_root="/workspace",
            features=["shell", "read_file"],
        )
        d = req.to_dict()
        restored = RegisterRequest.from_dict(d)
        assert restored.bootstrap_token == "bt_abc"
        assert restored.cwd == "/tmp"
        assert restored.workspace_root == "/workspace"
        assert restored.features == ["shell", "read_file"]


class TestRegisterResponse:
    def test_roundtrip(self) -> None:
        resp = RegisterResponse(
            peer_id="p1", peer_token="pt_xyz", heartbeat_interval_sec=15
        )
        d = resp.to_dict()
        restored = RegisterResponse.from_dict(d)
        assert restored.peer_id == "p1"
        assert restored.peer_token == "pt_xyz"
        assert restored.heartbeat_interval_sec == 15


class TestRegisterRejected:
    def test_roundtrip(self) -> None:
        rej = RegisterRejected(reason="bad token")
        d = rej.to_dict()
        restored = RegisterRejected.from_dict(d)
        assert restored.reason == "bad token"


class TestHeartbeat:
    def test_roundtrip(self) -> None:
        hb = Heartbeat(peer_token="pt_tok", ts=1234.5)
        d = hb.to_dict()
        restored = Heartbeat.from_dict(d)
        assert restored.peer_token == "pt_tok"
        assert restored.ts == 1234.5


class TestPeerControlPlaneRequests:
    def test_poll_request_roundtrip(self) -> None:
        req = PeerPollRequest(peer_token="pt_1")

        restored = PeerPollRequest.from_dict(req.to_dict())

        assert restored.peer_token == "pt_1"

    def test_result_request_roundtrip(self) -> None:
        req = PeerResultRequest(
            peer_token="pt_1",
            request_id="req-1",
            type="tool_result",
            payload={"ok": True},
        )

        restored = PeerResultRequest.from_dict(req.to_dict())

        assert restored.peer_token == "pt_1"
        assert restored.request_id == "req-1"
        assert restored.type == "tool_result"
        assert restored.payload == {"ok": True}

    def test_disconnect_request_roundtrip(self) -> None:
        req = PeerDisconnectRequest(peer_token="pt_1", reason="shutdown")

        restored = PeerDisconnectRequest.from_dict(req.to_dict())

        assert restored.peer_token == "pt_1"
        assert restored.reason == "shutdown"

    def test_disconnect_request_defaults_reason(self) -> None:
        restored = PeerDisconnectRequest.from_dict({"peer_token": "pt_1"})

        assert restored.peer_token == "pt_1"
        assert restored.reason == "peer_initiated"


class TestChatRequest:
    def test_roundtrip_preserves_mode_and_workflow(self) -> None:
        req = ChatRequest(
            peer_token="pt_1",
            prompt="plan this",
            mode="taskflow",
            workflow_mode="taskflow",
            taskflow_id="taskflow-1",
        )

        restored = ChatRequest.from_dict(req.to_dict())

        assert restored.peer_token == "pt_1"
        assert restored.prompt == "plan this"
        assert restored.mode == "taskflow"
        assert restored.workflow_mode == "taskflow"
        assert restored.taskflow_id == "taskflow-1"

    def test_accepts_legacy_taskflow_goal_id_input(self) -> None:
        restored = ChatRequest.from_dict(
            {
                "peer_token": "pt_1",
                "prompt": "continue",
                "taskflow_goal_id": "taskflow-legacy",
            }
        )

        assert restored.taskflow_id == "taskflow-legacy"
        assert "taskflow_goal_id" not in restored.to_dict()
        assert restored.to_dict()["taskflow_id"] == "taskflow-legacy"


class TestChatStartRequest:
    def test_roundtrip_preserves_mode_and_workflow(self) -> None:
        req = ChatStartRequest(
            peer_token="pt_1",
            prompt="plan this",
            session_hint="session-1",
            mode="taskflow",
            workflow_mode="taskflow",
            taskflow_id="taskflow-1",
        )

        restored = ChatStartRequest.from_dict(req.to_dict())

        assert restored.peer_token == "pt_1"
        assert restored.prompt == "plan this"
        assert restored.session_hint == "session-1"
        assert restored.mode == "taskflow"
        assert restored.workflow_mode == "taskflow"
        assert restored.taskflow_id == "taskflow-1"

    def test_accepts_legacy_taskflow_goal_id_input(self) -> None:
        restored = ChatStartRequest.from_dict(
            {
                "peer_token": "pt_1",
                "prompt": "continue",
                "taskflow_goal_id": "taskflow-legacy",
            }
        )

        assert restored.taskflow_id == "taskflow-legacy"
        assert "taskflow_goal_id" not in restored.to_dict()
        assert restored.to_dict()["taskflow_id"] == "taskflow-legacy"


class TestChatStatusProtocol:
    def test_roundtrip_preserves_recovery_diagnostics(self) -> None:
        req = ChatStatusRequest(peer_token="pt_1", chat_id="chat-1", cursor=7)
        restored_req = ChatStatusRequest.from_dict(req.to_dict())

        assert restored_req.peer_token == "pt_1"
        assert restored_req.chat_id == "chat-1"
        assert restored_req.cursor == 7

        resp = ChatStatusResponse(
            ok=True,
            chat_id="chat-1",
            peer_id="peer-1",
            status="running",
            running=True,
            done=False,
            reconnectable=True,
            cursor=7,
            next_cursor=9,
            first_available_seq=1,
            latest_seq=9,
            dropped_count=0,
            session_id="session-1",
            mode="planner",
            workflow_mode="taskflow",
            taskflow_id="taskflow-1",
            created_at=1.0,
            last_activity_at=2.0,
            finished_at=None,
            error=None,
        )
        restored_resp = ChatStatusResponse.from_dict(resp.to_dict())

        assert restored_resp.chat_id == "chat-1"
        assert restored_resp.status == "running"
        assert restored_resp.reconnectable is True
        assert restored_resp.next_cursor == 9
        assert restored_resp.session_id == "session-1"
        assert restored_resp.workflow_mode == "taskflow"


class TestSessionModelSwitchRequest:
    def test_roundtrip(self) -> None:
        req = SessionModelSwitchRequest(
            peer_token="pt_1",
            session_id="session-1",
            provider_id="deepseek",
            model_id="V4PRO",
            parameters={"max_tokens": 2048},
        )

        restored = SessionModelSwitchRequest.from_dict(req.to_dict())

        assert restored.peer_token == "pt_1"
        assert restored.session_id == "session-1"
        assert restored.provider_id == "deepseek"
        assert restored.model_id == "V4PRO"
        assert restored.parameters["max_tokens"] == 2048


class TestMCPManifest:
    def test_manifest_roundtrip(self) -> None:
        response = MCPManifestResponse(
            servers=[
                MCPServerManifest(
                    name="filesystem",
                    version="1.0.0",
                    artifact=MCPArtifactManifest(
                        platform="linux-amd64",
                        path="filesystem/1.0.0/linux-amd64.tar.gz",
                        sha256="abc",
                        url="/remote/mcp/artifacts/filesystem/1.0.0/linux-amd64.tar.gz",
                    ),
                    launch=MCPLaunchManifest(
                        command="{{bundle}}/filesystem-mcp",
                        args=["--root", "{{workspace}}"],
                        env={"MODE": "local"},
                    ),
                    permissions={"tools": {"write_file": "require_approval"}},
                    requirements={"node": "required", "npm": "required"},
                )
            ],
            diagnostics=[{"server": "missing", "level": "error"}],
        )

        restored = MCPManifestResponse.from_dict(response.to_dict())

        assert restored.servers[0].name == "filesystem"
        assert restored.servers[0].artifact is not None
        assert restored.servers[0].distribution == "artifact"
        assert restored.servers[0].artifact.platform == "linux-amd64"
        assert restored.servers[0].launch.args == ["--root", "{{workspace}}"]
        assert restored.servers[0].requirements["node"] == "required"
        assert restored.diagnostics[0]["server"] == "missing"

    def test_manifest_request_roundtrip(self) -> None:
        req = MCPManifestRequest(
            peer_token="pt_1", os="linux", arch="amd64", workspace="/repo"
        )
        restored = MCPManifestRequest.from_dict(req.to_dict())
        assert restored.peer_token == "pt_1"
        assert restored.os == "linux"
        assert restored.arch == "amd64"
        assert restored.workspace == "/repo"

    def test_tools_report_roundtrip(self) -> None:
        report = PeerMCPToolsReport(
            peer_token="pt_1",
            tools=[
                RemoteMCPToolInfo(
                    name="search",
                    description="Search docs",
                    input_schema={"type": "object"},
                    server_name="docs",
                )
            ],
            diagnostics=[{"level": "warning"}],
        )
        restored = PeerMCPToolsReport.from_dict(report.to_dict())
        assert restored.tools[0].name == "search"
        assert restored.tools[0].server_name == "docs"
        assert restored.diagnostics[0]["level"] == "warning"


class TestEnvironmentManifest:
    def test_manifest_roundtrip(self) -> None:
        response = EnvironmentManifestResponse(
            cli_tools=[
                EnvironmentCLIToolManifest(
                    name="gitnexus",
                    command="gitnexus",
                    tags=["repo_index"],
                    check="gitnexus --version",
                    install="npm install -g gitnexus",
                    version="latest",
                    source="npm",
                    docs=[{"title": "GitNexus", "url": "https://example.test/gitnexus"}],
                    install_prompt="Use npm install.",
                    verify_prompt="Run gitnexus --version.",
                    notes=["PATH changes need approval."],
                )
            ],
            mcp_servers=[
                EnvironmentMCPServerManifest(
                    name="gitnexus",
                    command="gitnexus",
                    args=["mcp"],
                    placement="peer",
                    distribution="command",
                    check="gitnexus --version",
                    install="npm install -g gitnexus@1.6.3",
                    requirements={"node": ">=20", "npm": "required"},
                    docs=[{"title": "GitNexus MCP", "url": "https://example.test/mcp"}],
                    install_prompt="Use npm package.",
                    verify_prompt="Run mcp launch check.",
                    notes=["Do not install node automatically."],
                )
            ],
            skills=[
                EnvironmentSkillManifest(
                    name="collaborating-with-claude",
                    scope="user",
                    check="Test-Path ~/.agents/skills/collaborating-with-claude/SKILL.md",
                    install="python install-skill.py",
                    version="1.0.0",
                    source="github",
                    description="Claude bridge skill",
                    path_hint="~/.agents/skills/collaborating-with-claude/SKILL.md",
                    docs=[{"title": "Claude skill", "url": "https://example.test/skill"}],
                    install_prompt="Install the skill files.",
                    verify_prompt="Check SKILL.md exists.",
                    notes=["Use user scope."],
                )
            ],
        )

        restored = EnvironmentManifestResponse.from_dict(response.to_dict())

        assert restored.cli_tools[0].name == "gitnexus"
        assert restored.cli_tools[0].tags == ["repo_index"]
        assert restored.cli_tools[0].check == "gitnexus --version"
        assert restored.cli_tools[0].install == "npm install -g gitnexus"
        assert restored.cli_tools[0].docs[0]["title"] == "GitNexus"
        assert restored.cli_tools[0].install_prompt == "Use npm install."
        assert restored.cli_tools[0].verify_prompt == "Run gitnexus --version."
        assert restored.cli_tools[0].notes == ["PATH changes need approval."]
        assert restored.mcp_servers[0].name == "gitnexus"
        assert restored.mcp_servers[0].args == ["mcp"]
        assert restored.mcp_servers[0].distribution == "command"
        assert restored.mcp_servers[0].requirements["node"] == ">=20"
        assert restored.mcp_servers[0].docs[0]["url"] == "https://example.test/mcp"
        assert restored.mcp_servers[0].install_prompt == "Use npm package."
        assert restored.skills[0].name == "collaborating-with-claude"
        assert restored.skills[0].scope == "user"
        assert restored.skills[0].path_hint == "~/.agents/skills/collaborating-with-claude/SKILL.md"
        assert restored.skills[0].docs[0]["title"] == "Claude skill"
        assert restored.skills[0].verify_prompt == "Check SKILL.md exists."
        assert restored.skills[0].notes == ["Use user scope."]
        assert "prompt" not in response.to_dict()

    def test_manifest_request_roundtrip(self) -> None:
        req = EnvironmentManifestRequest(
            peer_token="pt_1", os="windows", arch="amd64", workspace="G:/repo"
        )

        restored = EnvironmentManifestRequest.from_dict(req.to_dict())

        assert restored.peer_token == "pt_1"
        assert restored.os == "windows"
        assert restored.arch == "amd64"
        assert restored.workspace == "G:/repo"


class TestExecToolRequest:
    def test_roundtrip(self) -> None:
        req = ExecToolRequest(
            tool_name="shell",
            args={"command": "ls"},
            cwd="/tmp",
            timeout_sec=60,
            expected_state={"old_sha256": "abc"},
        )
        d = req.to_dict()
        restored = ExecToolRequest.from_dict(d)
        assert restored.tool_name == "shell"
        assert restored.args == {"command": "ls"}
        assert restored.cwd == "/tmp"
        assert restored.timeout_sec == 60
        assert restored.expected_state == {"old_sha256": "abc"}

    def test_defaults(self) -> None:
        req = ExecToolRequest(tool_name="read_file")
        assert req.args == {}
        assert req.cwd is None
        assert req.timeout_sec == 30
        assert req.expected_state == {}


class TestExecToolResult:
    def test_roundtrip(self) -> None:
        res = ExecToolResult(
            ok=False,
            result="",
            error_code="PEER_DISCONNECTED",
            error_message="peer gone",
            meta={"exit_code": 1},
        )
        d = res.to_dict()
        restored = ExecToolResult.from_dict(d)
        assert restored.ok is False
        assert restored.error_code == "PEER_DISCONNECTED"
        assert restored.meta["exit_code"] == 1


class TestToolPreview:
    def test_request_roundtrip(self) -> None:
        req = ToolPreviewRequest(
            tool_name="write_file",
            args={"file_path": "a.txt", "content": "hello"},
            cwd="/repo",
            timeout_sec=12,
        )

        restored = ToolPreviewRequest.from_dict(req.to_dict())

        assert restored.tool_name == "write_file"
        assert restored.args["file_path"] == "a.txt"
        assert restored.cwd == "/repo"
        assert restored.timeout_sec == 12

    def test_result_roundtrip(self) -> None:
        result = ToolPreviewResult(
            ok=True,
            sections=[
                {
                    "id": "diff",
                    "kind": "diff",
                    "content": "--- a/a.txt\n+++ b/a.txt\n",
                }
            ],
            resolved_path="/repo/a.txt",
            old_sha256="abc",
            old_exists=True,
            old_size=10,
            old_mtime_ns=123,
            diff="diff",
            original_text="old",
            modified_text="new",
            meta={"mode": "preview"},
        )

        restored = ToolPreviewResult.from_dict(result.to_dict())

        assert restored.ok is True
        assert restored.sections[0]["kind"] == "diff"
        assert restored.resolved_path == "/repo/a.txt"
        assert restored.old_sha256 == "abc"
        assert restored.old_exists is True
        assert restored.old_size == 10
        assert restored.old_mtime_ns == 123
        assert restored.original_text == "old"
        assert restored.modified_text == "new"
        assert restored.meta["mode"] == "preview"


class TestToolStreamChunk:
    def test_roundtrip(self) -> None:
        chunk = ToolStreamChunk(
            chunk_type="stdout",
            data="hello",
            meta={"seq": 1},
            tool_call_id="call-1",
        )
        d = chunk.to_dict()
        restored = ToolStreamChunk.from_dict(d)
        assert restored.chunk_type == "stdout"
        assert restored.data == "hello"
        assert restored.meta == {"seq": 1}
        assert restored.tool_call_id == "call-1"


class TestDisconnectNotice:
    def test_roundtrip(self) -> None:
        n = DisconnectNotice(reason="shutdown")
        d = n.to_dict()
        restored = DisconnectNotice.from_dict(d)
        assert restored.reason == "shutdown"

    def test_default_reason(self) -> None:
        n = DisconnectNotice.from_dict({})
        assert n.reason == "peer_initiated"


class TestCleanupRequest:
    def test_roundtrip(self) -> None:
        req = CleanupRequest()
        d = req.to_dict()
        restored = CleanupRequest.from_dict(d)
        assert isinstance(restored, CleanupRequest)


class TestCleanupResult:
    def test_roundtrip(self) -> None:
        res = CleanupResult(ok=True, removed_items=["/tmp/a"], error_message=None)
        d = res.to_dict()
        restored = CleanupResult.from_dict(d)
        assert restored.ok is True
        assert restored.removed_items == ["/tmp/a"]


class TestErrorMessage:
    def test_roundtrip(self) -> None:
        err = ErrorMessage(code="AUTH_FAILED", message="bad token")
        d = err.to_dict()
        restored = ErrorMessage.from_dict(d)
        assert restored.code == "AUTH_FAILED"
        assert restored.message == "bad token"

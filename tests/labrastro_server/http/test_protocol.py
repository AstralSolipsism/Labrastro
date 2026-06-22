"""Tests for remote execution protocol message models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import labrastro_server.interfaces.http.remote.protocol as remote_protocol
from labrastro_server.interfaces.http.remote.protocol import (
    AgentRunBranchRequest,
    AgentRunForkRequest,
    CleanupRequest,
    CleanupResult,
    SessionRunAgentRunSteerRequest,
    ChatCommandDispatchRequest,
    ChatCommandDispatchResponse,
    ApprovalReplyRequest,
    SessionRunCancelRequest,
    SessionRunContinueRequest,
    SessionRunRecoverRequest,
    SessionRunStartRequest,
    SessionRunStartResponse,
    SessionRunBranchSelectRequest,
    SessionRunStatusRequest,
    SessionRunStatusResponse,
    SessionRunEventsRequest,
    SessionRunEventsBatch,
    SessionRunUserInputReplyRequest,
    DisconnectNotice,
    EnvironmentManifestRequest,
    EnvironmentManifestResponse,
    EnvironmentRequirementManifest,
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
    @staticmethod
    def _is_runtime_convergence_endpoint(name: str) -> bool:
        return (
            name.startswith("session_run.")
            or name.startswith("agent_runs.")
            or name.startswith("agent_run_activations.")
            or name.startswith("admin.agent_runs.")
            or name == "admin.capability_packages.ingest_session_start"
        )

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

    def test_runtime_convergence_endpoints_have_contract_fixtures(self) -> None:
        fixture_names = {
            fixture["name"]
            for fixture in _contract_fixtures()["fixtures"]
        }
        missing = [
            endpoint.name
            for endpoint in REMOTE_ENDPOINTS
            if self._is_runtime_convergence_endpoint(endpoint.name)
            and endpoint.name not in fixture_names
        ]

        assert missing == []

    def test_runtime_convergence_protocol_shapes_are_exported_models(self) -> None:
        exported = set(remote_protocol.__all__)
        missing = []
        for endpoint in REMOTE_ENDPOINTS:
            if not self._is_runtime_convergence_endpoint(endpoint.name):
                continue
            if endpoint.request_model != "none" and endpoint.request_model not in exported:
                missing.append(f"{endpoint.name}: request {endpoint.request_model}")
            if endpoint.response_shape != "none" and endpoint.response_shape not in exported:
                missing.append(f"{endpoint.name}: response {endpoint.response_shape}")

        assert missing == []

    def test_runtime_convergence_fixtures_roundtrip_protocol_models(self) -> None:
        fixtures = {
            fixture["name"]: fixture
            for fixture in _contract_fixtures()["fixtures"]
        }
        for endpoint in REMOTE_ENDPOINTS:
            if not self._is_runtime_convergence_endpoint(endpoint.name):
                continue
            fixture = fixtures[endpoint.name]
            if endpoint.request_model != "none":
                request_model = getattr(remote_protocol, endpoint.request_model)
                request_model.from_dict(fixture["request"])
            if endpoint.response_shape != "none":
                response_model = getattr(remote_protocol, endpoint.response_shape)
                response_model.from_dict(fixture["response"])

    def test_contract_fixtures_roundtrip_protocol_models(self) -> None:
        fixtures = {
            fixture["name"]: fixture
            for fixture in _contract_fixtures()["fixtures"]
        }

        register = fixtures["peer.register"]
        register_request = RegisterRequest.from_dict(register["request"])
        assert register_request.host_info_min["shell"] == "bash"
        RegisterResponse.from_dict(register["response"]["payload"])
        Heartbeat.from_dict(fixtures["peer.heartbeat"]["request"])
        SessionListRequest.from_dict(fixtures["sessions.list"]["request"])
        SessionModelSwitchRequest.from_dict(fixtures["sessions.model"]["request"])
        SessionRunStartRequest.from_dict(fixtures["session_run.start"]["request"])
        SessionRunStartResponse.from_dict(fixtures["session_run.start"]["response"])
        ChatCommandDispatchRequest.from_dict(fixtures["chat.command_dispatch"]["request"])
        ChatCommandDispatchResponse.from_dict(fixtures["chat.command_dispatch"]["response"])
        SessionRunEventsRequest.from_dict(fixtures["session_run.events"]["request"])
        SessionRunEventsBatch.from_dict(fixtures["session_run.events"]["response"])
        SessionRunStatusRequest.from_dict(fixtures["session_run.status"]["request"])
        SessionRunStatusResponse.from_dict(fixtures["session_run.status"]["response"])
        SessionRunBranchSelectRequest.from_dict(fixtures["session_run.branch_select"]["request"])
        SessionRunStatusResponse.from_dict(fixtures["session_run.branch_select"]["response"])
        EnvironmentManifestRequest.from_dict(fixtures["environment.manifest"]["request"])
        EnvironmentManifestResponse.from_dict(fixtures["environment.manifest"]["response"])

    def test_registry_matches_actual_peer_token_control_plane_routes(self) -> None:
        registry = {endpoint["name"]: endpoint for endpoint in endpoint_registry()}

        assert "chat.stream" not in registry
        assert "chat.once" not in registry
        assert registry["mcp.artifact"]["auth"] == "peer_token"
        assert (
            registry["artifacts.get"]["path"]
            == "/remote/artifacts/{os}/{arch}/{artifact_name}"
        )
        assert registry["session_run.events"]["path"] == "/remote/session-runs/events"
        assert registry["session_run.events"]["response_shape"] == "SessionRunEventsBatch"
        assert registry["session_run.branch_select"]["path"] == "/remote/session-runs/branches/select"
        assert registry["session_run.branch_select"]["request_model"] == "SessionRunBranchSelectRequest"
        assert registry["chat.command_dispatch"]["path"] == "/remote/chat/command"
        assert registry["chat.command_dispatch"]["request_model"] == "ChatCommandDispatchRequest"
        assert registry["agent_runs.steer"]["path"] == (
            "/remote/agent-runs/{agent_run_id}/steer"
        )
        assert registry["agent_runs.steer"]["request_model"] == (
            "SessionRunAgentRunSteerRequest"
        )
        assert registry["agent_runs.steer"]["auth"] == "peer_token"
        assert registry["admin.models.delete"]["path"] == "/remote/admin/models/delete"
        assert registry["admin.models.delete"]["request_model"] == "ModelProfileDeleteRequest"
        assert registry["admin.models.delete"]["auth"] == "bearer"
        assert registry["admin.agent_runs.branch"]["path"] == (
            "/remote/admin/agent-runs/branch"
        )
        assert registry["admin.agent_runs.branch"]["request_model"] == (
            "AgentRunBranchRequest"
        )
        assert registry["admin.agent_runs.fork"]["path"] == (
            "/remote/admin/agent-runs/fork"
        )
        assert registry["admin.agent_runs.fork"]["request_model"] == (
            "AgentRunForkRequest"
        )
        assert registry["admin.agent_runs.steer"]["path"] == (
            "/remote/admin/agent-runs/steer"
        )
        assert registry["admin.agent_runs.steer"]["request_model"] == (
            "AgentRunSteerRequest"
        )
        assert registry["admin.agent_runs.steer"]["response_shape"] == (
            "AgentRunSteerResponse"
        )
        assert "admin.environment_requirements.behavior_catalog" not in registry
        assert registry["admin.behavior.catalog"]["path"] == "/remote/admin/behavior/catalog"
        assert registry["admin.skills.list"]["path"] == "/remote/admin/skills/list"
        assert registry["admin.skills.record"]["request_model"] == "SkillRecordRequest"
        assert registry["admin.skills.delete"]["auth"] == "bearer"
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


class TestSessionRunStartRequest:
    def test_roundtrip_preserves_mode_and_workflow(self) -> None:
        req = SessionRunStartRequest(
            peer_token="pt_1",
            prompt="plan this",
            session_hint="session-1",
            mode="taskflow",
            workflow_mode="taskflow",
            taskflow_id="taskflow-1",
            locale="zh-CN",
            mentions=[{"kind": "file", "name": "README.md", "path": "README.md"}],
        )

        restored = SessionRunStartRequest.from_dict(req.to_dict())

        assert restored.peer_token == "pt_1"
        assert restored.prompt == "plan this"
        assert restored.session_hint == "session-1"
        assert restored.mode == "taskflow"
        assert restored.workflow_mode == "taskflow"
        assert restored.taskflow_id == "taskflow-1"
        assert restored.locale == "zh-CN"
        assert restored.mentions == [
            {"kind": "file", "name": "README.md", "path": "README.md"}
        ]

    def test_serializes_canonical_taskflow_id_only(self) -> None:
        restored = SessionRunStartRequest.from_dict({
            "peer_token": "pt_1",
            "prompt": "continue",
            "taskflow_id": "taskflow-1",
        })

        assert restored.taskflow_id == "taskflow-1"
        assert restored.to_dict()["taskflow_id"] == "taskflow-1"


class TestChatCommandDispatchProtocol:
    def test_request_builds_command_text_from_text_or_trigger_args(self) -> None:
        explicit = ChatCommandDispatchRequest.from_dict(
            {
                "peer_token": "pt_1",
                "text": " /debug on ",
                "command_id": "system.debug",
                "session_hint": "session-1",
                "client_request_id": "req-1",
                "mentions": [{"kind": "file", "path": "README.md"}],
            }
        )
        fallback = ChatCommandDispatchRequest.from_dict(
            {
                "peer_token": "pt_1",
                "trigger": "/model",
                "args": "planner",
            }
        )

        assert explicit.command_text == "/debug on"
        assert explicit.command_id == "system.debug"
        assert explicit.session_hint == "session-1"
        assert explicit.client_request_id == "req-1"
        assert explicit.mentions == [{"kind": "file", "path": "README.md"}]
        assert fallback.command_text == "/model planner"

    def test_response_roundtrip_preserves_action_session_and_events(self) -> None:
        resp = ChatCommandDispatchResponse(
            ok=False,
            action="chat",
            session_id="session-1",
            events=[{"type": "error", "payload": {"code": "invalid_chat_command"}}],
            error="invalid_chat_command",
        )

        restored = ChatCommandDispatchResponse.from_dict(resp.to_dict())

        assert restored.ok is False
        assert restored.action == "chat"
        assert restored.session_id == "session-1"
        assert restored.events == [
            {"type": "error", "payload": {"code": "invalid_chat_command"}}
        ]
        assert restored.error == "invalid_chat_command"


class TestApprovalReplyProtocol:
    def test_roundtrip_preserves_approved_save_candidate(self) -> None:
        candidate = {
            "tool_name": "apply_patch",
            "operations": [
                {"kind": "update", "path": "src/app.py", "new_content": "edited"}
            ],
        }
        req = ApprovalReplyRequest(
            peer_token="pt_1",
            session_run_id="run-1",
            branch_binding_id="main",
            approval_id="approval-1",
            decision="allow_once",
            reason="saved candidate",
            approved_save_candidate=candidate,
        )

        restored = ApprovalReplyRequest.from_dict(req.to_dict())

        assert restored.approved_save_candidate == candidate
        assert restored.branch_binding_id == "main"
        assert restored.to_dict()["approved_save_candidate"] == candidate


class TestSessionRunStatusProtocol:
    @pytest.mark.parametrize(
        ("request_cls", "payload"),
        [
            (
                SessionRunEventsRequest,
                {
                    "peer_token": "pt_1",
                    "session_run_id": "run-1",
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            ),
            (
                SessionRunStatusRequest,
                {"peer_token": "pt_1", "session_run_id": "run-1", "cursor": 0},
            ),
            (
                SessionRunBranchSelectRequest,
                {"peer_token": "pt_1", "session_run_id": "run-1", "cursor": 0},
            ),
            (
                SessionRunCancelRequest,
                {
                    "peer_token": "pt_1",
                    "session_run_id": "run-1",
                    "reason": "user_cancelled",
                },
            ),
            (
                SessionRunContinueRequest,
                {
                    "peer_token": "pt_1",
                    "session_run_id": "run-1",
                    "prompt": "continue",
                },
            ),
            (
                SessionRunRecoverRequest,
                {"peer_token": "pt_1", "session_run_id": "run-1", "action": "continue"},
            ),
            (
                SessionRunUserInputReplyRequest,
                {
                    "peer_token": "pt_1",
                    "session_run_id": "run-1",
                    "input_id": "input-1",
                    "action": "submit",
                    "content": {"value": "ok"},
                },
            ),
            (
                ApprovalReplyRequest,
                {
                    "peer_token": "pt_1",
                    "session_run_id": "run-1",
                    "approval_id": "approval-1",
                    "decision": "allow_once",
                },
            ),
            (
                SessionRunAgentRunSteerRequest,
                {
                    "peer_token": "pt_1",
                    "agent_run_id": "agent-1",
                    "session_run_id": "run-1",
                    "payload": {"type": "user_text", "text": "guide"},
                },
            ),
            (
                AgentRunBranchRequest,
                {
                    "source_agent_run_id": "agent-1",
                    "base_session_item_id": "message-1",
                    "runtime_root": "D:/repo",
                    "prompt": "branch",
                },
            ),
            (
                AgentRunForkRequest,
                {
                    "source_agent_run_id": "agent-1",
                    "base_session_item_id": "message-1",
                    "fork_workspace_ref": "workspace-ref",
                    "target_owner_session_run_id": "run-1",
                    "prompt": "fork",
                },
            ),
        ],
    )
    def test_branch_scoped_requests_reject_missing_branch_binding(
        self,
        request_cls: type,
        payload: dict,
    ) -> None:
        with pytest.raises(ValueError, match="branch_binding_id_required"):
            request_cls.from_dict(payload)

    @pytest.mark.parametrize(
        ("request_cls", "payload"),
        [
            (
                SessionRunEventsRequest,
                {"peer_token": "pt_1", "branch_binding_id": "branch-1", "cursor": 0},
            ),
            (
                SessionRunStatusRequest,
                {"peer_token": "pt_1", "branch_binding_id": "branch-1", "cursor": 0},
            ),
            (
                SessionRunBranchSelectRequest,
                {"peer_token": "pt_1", "branch_binding_id": "branch-1", "cursor": 0},
            ),
            (
                SessionRunCancelRequest,
                {"peer_token": "pt_1", "branch_binding_id": "branch-1"},
            ),
            (
                SessionRunContinueRequest,
                {"peer_token": "pt_1", "branch_binding_id": "branch-1", "prompt": "continue"},
            ),
            (
                SessionRunRecoverRequest,
                {"peer_token": "pt_1", "branch_binding_id": "branch-1"},
            ),
            (
                SessionRunUserInputReplyRequest,
                {
                    "peer_token": "pt_1",
                    "branch_binding_id": "branch-1",
                    "input_id": "input-1",
                    "action": "submit",
                },
            ),
            (
                ApprovalReplyRequest,
                {
                    "peer_token": "pt_1",
                    "branch_binding_id": "branch-1",
                    "approval_id": "approval-1",
                    "decision": "allow_once",
                },
            ),
            (
                SessionRunAgentRunSteerRequest,
                {
                    "peer_token": "pt_1",
                    "agent_run_id": "agent-1",
                    "branch_binding_id": "branch-1",
                    "payload": {"type": "user_text", "text": "guide"},
                },
            ),
        ],
    )
    def test_branch_scoped_requests_reject_missing_session_run_id(
        self,
        request_cls: type,
        payload: dict,
    ) -> None:
        with pytest.raises(ValueError, match="session_run_id_required"):
            request_cls.from_dict(payload)

    @pytest.mark.parametrize(
        ("request_cls", "kwargs"),
        [
            (
                SessionRunEventsRequest,
                {"peer_token": "pt_1", "session_run_id": "run-1", "branch_binding_id": ""},
            ),
            (
                SessionRunStatusRequest,
                {"peer_token": "pt_1", "session_run_id": "run-1", "branch_binding_id": ""},
            ),
            (
                SessionRunBranchSelectRequest,
                {"peer_token": "pt_1", "session_run_id": "run-1", "branch_binding_id": ""},
            ),
            (
                SessionRunCancelRequest,
                {"peer_token": "pt_1", "session_run_id": "run-1", "branch_binding_id": ""},
            ),
            (
                SessionRunContinueRequest,
                {
                    "peer_token": "pt_1",
                    "session_run_id": "run-1",
                    "branch_binding_id": "",
                    "prompt": "continue",
                },
            ),
            (
                SessionRunRecoverRequest,
                {"peer_token": "pt_1", "session_run_id": "run-1", "branch_binding_id": ""},
            ),
            (
                SessionRunUserInputReplyRequest,
                {
                    "peer_token": "pt_1",
                    "session_run_id": "run-1",
                    "branch_binding_id": "",
                    "input_id": "input-1",
                    "action": "submit",
                },
            ),
            (
                ApprovalReplyRequest,
                {
                    "peer_token": "pt_1",
                    "session_run_id": "run-1",
                    "branch_binding_id": "",
                    "approval_id": "approval-1",
                    "decision": "allow_once",
                },
            ),
            (
                SessionRunAgentRunSteerRequest,
                {
                    "peer_token": "pt_1",
                    "agent_run_id": "agent-1",
                    "session_run_id": "run-1",
                    "branch_binding_id": "",
                    "payload": {"type": "user_text", "text": "guide"},
                },
            ),
            (
                AgentRunBranchRequest,
                {
                    "source_agent_run_id": "agent-1",
                    "base_session_item_id": "message-1",
                    "runtime_root": "D:/repo",
                    "prompt": "branch",
                    "branch_binding_id": "",
                },
            ),
            (
                AgentRunForkRequest,
                {
                    "source_agent_run_id": "agent-1",
                    "base_session_item_id": "message-1",
                    "fork_workspace_ref": "workspace-ref",
                    "target_owner_session_run_id": "run-1",
                    "prompt": "fork",
                    "branch_binding_id": "",
                },
            ),
        ],
    )
    def test_branch_scoped_requests_cannot_serialize_blank_branch_binding(
        self,
        request_cls: type,
        kwargs: dict,
    ) -> None:
        with pytest.raises(ValueError, match="branch_binding_id_required"):
            request_cls(**kwargs).to_dict()

    @pytest.mark.parametrize(
        ("request_cls", "kwargs"),
        [
            (
                SessionRunEventsRequest,
                {"peer_token": "pt_1", "session_run_id": "", "branch_binding_id": "branch-1"},
            ),
            (
                SessionRunStatusRequest,
                {"peer_token": "pt_1", "session_run_id": "", "branch_binding_id": "branch-1"},
            ),
            (
                SessionRunBranchSelectRequest,
                {"peer_token": "pt_1", "session_run_id": "", "branch_binding_id": "branch-1"},
            ),
            (
                SessionRunCancelRequest,
                {"peer_token": "pt_1", "session_run_id": "", "branch_binding_id": "branch-1"},
            ),
            (
                SessionRunContinueRequest,
                {
                    "peer_token": "pt_1",
                    "session_run_id": "",
                    "branch_binding_id": "branch-1",
                    "prompt": "continue",
                },
            ),
            (
                SessionRunRecoverRequest,
                {"peer_token": "pt_1", "session_run_id": "", "branch_binding_id": "branch-1"},
            ),
            (
                SessionRunUserInputReplyRequest,
                {
                    "peer_token": "pt_1",
                    "session_run_id": "",
                    "branch_binding_id": "branch-1",
                    "input_id": "input-1",
                    "action": "submit",
                },
            ),
            (
                ApprovalReplyRequest,
                {
                    "peer_token": "pt_1",
                    "session_run_id": "",
                    "branch_binding_id": "branch-1",
                    "approval_id": "approval-1",
                    "decision": "allow_once",
                },
            ),
            (
                SessionRunAgentRunSteerRequest,
                {
                    "peer_token": "pt_1",
                    "agent_run_id": "agent-1",
                    "session_run_id": "",
                    "branch_binding_id": "branch-1",
                    "payload": {"type": "user_text", "text": "guide"},
                },
            ),
        ],
    )
    def test_branch_scoped_requests_cannot_serialize_blank_session_run_id(
        self,
        request_cls: type,
        kwargs: dict,
    ) -> None:
        with pytest.raises(ValueError, match="session_run_id_required"):
            request_cls(**kwargs).to_dict()

    def test_branch_scoped_request_serialization_revalidates_mutated_scope_fields(
        self,
    ) -> None:
        session_scoped_requests = [
            SessionRunEventsRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
            ),
            SessionRunStatusRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
            ),
            SessionRunBranchSelectRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
            ),
            SessionRunCancelRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
            ),
            SessionRunContinueRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
                prompt="continue",
            ),
            SessionRunRecoverRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
            ),
            SessionRunUserInputReplyRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
                input_id="input-1",
                action="submit",
            ),
            ApprovalReplyRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
                approval_id="approval-1",
                decision="allow_once",
            ),
            SessionRunAgentRunSteerRequest(
                peer_token="pt_1",
                agent_run_id="agent-1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
                payload={"type": "user_text", "text": "guide"},
            ),
        ]
        for request in session_scoped_requests:
            request.session_run_id = ""
            with pytest.raises(ValueError, match="session_run_id_required"):
                request.to_dict()

        branch_scoped_requests = [
            SessionRunEventsRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
            ),
            SessionRunStatusRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
            ),
            SessionRunBranchSelectRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
            ),
            SessionRunCancelRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
            ),
            SessionRunContinueRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
                prompt="continue",
            ),
            SessionRunRecoverRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
            ),
            SessionRunUserInputReplyRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
                input_id="input-1",
                action="submit",
            ),
            ApprovalReplyRequest(
                peer_token="pt_1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
                approval_id="approval-1",
                decision="allow_once",
            ),
            SessionRunAgentRunSteerRequest(
                peer_token="pt_1",
                agent_run_id="agent-1",
                session_run_id="run-1",
                branch_binding_id="branch-1",
                payload={"type": "user_text", "text": "guide"},
            ),
            AgentRunBranchRequest(
                source_agent_run_id="agent-1",
                base_session_item_id="message-1",
                runtime_root="D:/repo",
                branch_binding_id="branch-1",
            ),
            AgentRunForkRequest(
                source_agent_run_id="agent-1",
                base_session_item_id="message-1",
                fork_workspace_ref="workspace-ref",
                target_owner_session_run_id="run-1",
                branch_binding_id="branch-1",
            ),
        ]
        for request in branch_scoped_requests:
            request.branch_binding_id = ""
            with pytest.raises(ValueError, match="branch_binding_id_required"):
                request.to_dict()

    def test_branch_select_request_roundtrip_preserves_branch_binding(self) -> None:
        req = SessionRunBranchSelectRequest(
            peer_token="pt_1",
            session_run_id="run-1",
            branch_binding_id="branch-2",
            cursor=11,
        )
        restored = SessionRunBranchSelectRequest.from_dict(req.to_dict())

        assert restored.peer_token == "pt_1"
        assert restored.session_run_id == "run-1"
        assert restored.branch_binding_id == "branch-2"
        assert restored.cursor == 11
        assert "branchBindingId" not in restored.to_dict()

    def test_roundtrip_preserves_recovery_diagnostics(self) -> None:
        req = SessionRunStatusRequest(
            peer_token="pt_1",
            session_run_id="run-1",
            branch_binding_id="branch-2",
            cursor=7,
        )
        restored_req = SessionRunStatusRequest.from_dict(req.to_dict())

        assert restored_req.peer_token == "pt_1"
        assert restored_req.session_run_id == "run-1"
        assert restored_req.branch_binding_id == "branch-2"
        assert restored_req.cursor == 7

        resp = SessionRunStatusResponse(
            ok=True,
            session_run_id="run-1",
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
            agent_run_id="agent-1",
            branch_binding_id="branch-2",
            scope_id="run-1:branch-2",
            selected=False,
            created_at=1.0,
            last_activity_at=2.0,
            finished_at=None,
            error=None,
            approvals=[
                {
                    "approval_id": "approval-1",
                    "tool_call_id": "call-1",
                    "tool_name": "shell",
                    "state": "requested",
                }
            ],
        )
        restored_resp = SessionRunStatusResponse.from_dict(resp.to_dict())

        assert restored_resp.session_run_id == "run-1"
        assert restored_resp.status == "running"
        assert restored_resp.reconnectable is True
        assert restored_resp.next_cursor == 9
        assert restored_resp.session_id == "session-1"
        assert restored_resp.workflow_mode == "taskflow"
        assert restored_resp.agent_run_id == "agent-1"
        assert restored_resp.branch_binding_id == "branch-2"
        assert restored_resp.scope_id == "run-1:branch-2"
        assert restored_resp.selected is False
        assert restored_resp.approvals == [
            {
                "approval_id": "approval-1",
                "tool_call_id": "call-1",
                "tool_name": "shell",
                "state": "requested",
            }
        ]


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
                    permissions={"tools": {"apply_patch": "require_approval"}},
                    environment_requirement_refs=[
                        "envreq:runtime:node",
                        "envreq:executable:npm",
                    ],
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
        assert restored.servers[0].environment_requirement_refs == [
            "envreq:runtime:node",
            "envreq:executable:npm",
        ]
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
            environment_requirements=[
                EnvironmentRequirementManifest(
                    id="envreq:executable:gitnexus",
                    kind="executable",
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
                ),
                EnvironmentRequirementManifest(
                    id="envreq:path:collaborating-with-claude-skill",
                    kind="path",
                    name="collaborating-with-claude-skill",
                    scope="user",
                    check="Test-Path ~/.agents/skills/collaborating-with-claude/SKILL.md",
                    install="python install-skill.py",
                    version="1.0.0",
                    source="github",
                    description="Claude bridge skill",
                    path="~/.agents/skills/collaborating-with-claude/SKILL.md",
                    docs=[{"title": "Claude skill", "url": "https://example.test/skill"}],
                    install_prompt="Install the skill files.",
                    verify_prompt="Check SKILL.md exists.",
                    notes=["Use user scope."],
                ),
            ],
        )

        restored = EnvironmentManifestResponse.from_dict(response.to_dict())

        executable = restored.environment_requirements[0]
        skill_path = restored.environment_requirements[1]
        assert executable.id == "envreq:executable:gitnexus"
        assert executable.kind == "executable"
        assert executable.name == "gitnexus"
        assert executable.tags == ["repo_index"]
        assert executable.check == "gitnexus --version"
        assert executable.install == "npm install -g gitnexus"
        assert executable.docs[0]["title"] == "GitNexus"
        assert executable.install_prompt == "Use npm install."
        assert executable.verify_prompt == "Run gitnexus --version."
        assert executable.notes == ["PATH changes need approval."]
        assert "mcp_servers" not in response.to_dict()
        assert skill_path.kind == "path"
        assert skill_path.scope == "user"
        assert skill_path.path == "~/.agents/skills/collaborating-with-claude/SKILL.md"
        assert skill_path.docs[0]["title"] == "Claude skill"
        assert skill_path.verify_prompt == "Check SKILL.md exists."
        assert skill_path.notes == ["Use user scope."]
        assert "prompt" not in response.to_dict()

    def test_manifest_request_roundtrip(self) -> None:
        req = EnvironmentManifestRequest(
            peer_token="pt_1",
            os="windows",
            arch="amd64",
            workspace="G:/repo",
            agent_id="reviewer",
        )

        restored = EnvironmentManifestRequest.from_dict(req.to_dict())

        assert restored.peer_token == "pt_1"
        assert restored.os == "windows"
        assert restored.arch == "amd64"
        assert restored.workspace == "G:/repo"
        assert restored.agent_id == "reviewer"


class TestExecToolRequest:
    def test_roundtrip(self) -> None:
        req = ExecToolRequest(
            tool_name="shell",
            args={"command": "ls"},
            cwd="/tmp",
            timeout_sec=60,
            permission_context={
                "agent_id": "main_chat",
                "decision": {"action": "allow", "authorized": True},
            },
        )
        d = req.to_dict()
        restored = ExecToolRequest.from_dict(d)
        assert restored.tool_name == "shell"
        assert restored.args == {"command": "ls"}
        assert restored.cwd == "/tmp"
        assert restored.timeout_sec == 60
        assert ("expected" + "_state") not in d
        assert restored.permission_context == {
            "agent_id": "main_chat",
            "decision": {"action": "allow", "authorized": True},
        }

    def test_defaults(self) -> None:
        req = ExecToolRequest(tool_name="read_file")
        assert req.args == {}
        assert req.cwd is None
        assert req.timeout_sec == 30
        assert req.permission_context == {}

    def test_roundtrip_save_candidate_fields(self) -> None:
        preview_identity = {
            "plan_id": "plan-1",
            "candidate_hash": "candidate-1",
            "tool_name": "apply_patch",
            "workspace_id": "/repo",
            "execution_target": "remote_peer",
            "path_space": "remote_peer_workspace",
            "args_hash": "args-1",
        }
        candidate = {
            "tool_name": "apply_patch",
            "preview_identity": preview_identity,
            "operations": [
                {
                    "kind": "update",
                    "path": "a.txt",
                    "new_content": "new\n",
                }
            ],
        }
        req = ExecToolRequest(
            tool_name="apply_patch",
            args={},
            cwd="/repo",
            preview_identity=preview_identity,
            approved_save_candidate=candidate,
        )

        restored = ExecToolRequest.from_dict(req.to_dict())

        assert restored.preview_identity == preview_identity
        assert restored.approved_save_candidate == candidate


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
            tool_name="apply_patch",
            args={"file_path": "a.txt", "content": "hello"},
            cwd="/repo",
            timeout_sec=12,
        )

        restored = ToolPreviewRequest.from_dict(req.to_dict())

        assert restored.tool_name == "apply_patch"
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
            diff="diff",
            original_text="old",
            modified_text="new",
            meta={"mode": "preview"},
        )

        restored = ToolPreviewResult.from_dict(result.to_dict())

        assert restored.ok is True
        assert restored.sections[0]["kind"] == "diff"
        assert restored.resolved_path == "/repo/a.txt"
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

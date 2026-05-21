"""Registry for the HTTP remote control-plane contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

AuthMode = Literal["none", "bearer", "peer_token", "bootstrap_token", "webhook"]


@dataclass(frozen=True)
class RemoteEndpoint:
    name: str
    method: str
    path: str
    request_model: str
    response_shape: str
    auth: AuthMode

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


REMOTE_ENDPOINTS: tuple[RemoteEndpoint, ...] = (
    RemoteEndpoint("auth.state", "GET", "/remote/auth/state", "none", "AuthState", "none"),
    RemoteEndpoint("auth.me", "GET", "/remote/auth/me", "none", "AuthMe", "bearer"),
    RemoteEndpoint("auth.login", "POST", "/remote/auth/login", "AuthLoginRequest", "AuthSession", "none"),
    RemoteEndpoint("auth.refresh", "POST", "/remote/auth/refresh", "AuthRefreshRequest", "AuthSession", "none"),
    RemoteEndpoint("auth.logout", "POST", "/remote/auth/logout", "AuthLogoutRequest", "Ok", "none"),
    RemoteEndpoint("auth.bootstrap_token", "POST", "/remote/auth/bootstrap-token", "Empty", "BootstrapToken", "bearer"),
    RemoteEndpoint("auth.password_change", "POST", "/remote/auth/password/change", "PasswordChangeRequest", "Ok", "bearer"),
    RemoteEndpoint("auth.users.list", "POST", "/remote/auth/users/list", "UserListRequest", "UserList", "bearer"),
    RemoteEndpoint("auth.users.create", "POST", "/remote/auth/users/create", "UserCreateRequest", "UserResult", "bearer"),
    RemoteEndpoint("auth.users.update", "POST", "/remote/auth/users/update", "UserUpdateRequest", "UserResult", "bearer"),
    RemoteEndpoint("auth.users.disable", "POST", "/remote/auth/users/disable", "UserDisableRequest", "UserResult", "bearer"),
    RemoteEndpoint("auth.users.reset_password", "POST", "/remote/auth/users/reset-password", "UserResetPasswordRequest", "UserResult", "bearer"),
    RemoteEndpoint("auth.devices.list", "POST", "/remote/auth/devices/list", "DeviceListRequest", "DeviceList", "bearer"),
    RemoteEndpoint("auth.devices.revoke", "POST", "/remote/auth/devices/revoke", "DeviceRevokeRequest", "DeviceResult", "bearer"),
    RemoteEndpoint("auth.audit.list", "POST", "/remote/auth/audit/list", "AuditListRequest", "AuditList", "bearer"),
    RemoteEndpoint("peer.features", "GET", "/remote/features", "none", "Features", "none"),
    RemoteEndpoint("peer.register", "POST", "/remote/register", "RegisterRequest", "RegisterEnvelope", "bootstrap_token"),
    RemoteEndpoint("peer.heartbeat", "POST", "/remote/heartbeat", "Heartbeat", "PeerHeartbeat", "peer_token"),
    RemoteEndpoint("peer.poll", "POST", "/remote/poll", "PeerPollRequest", "RelayEnvelope", "peer_token"),
    RemoteEndpoint("peer.result", "POST", "/remote/result", "PeerResultRequest", "Ok", "peer_token"),
    RemoteEndpoint("peer.disconnect", "POST", "/remote/disconnect", "PeerDisconnectRequest", "Ok", "peer_token"),
    RemoteEndpoint("sessions.list", "POST", "/remote/sessions/list", "SessionListRequest", "SessionListResponse", "peer_token"),
    RemoteEndpoint("sessions.load", "POST", "/remote/sessions/load", "SessionLoadRequest", "SessionLoadResponse", "peer_token"),
    RemoteEndpoint("sessions.new", "POST", "/remote/sessions/new", "SessionNewRequest", "SessionNewResponse", "peer_token"),
    RemoteEndpoint("sessions.delete", "POST", "/remote/sessions/delete", "SessionDeleteRequest", "Ok", "peer_token"),
    RemoteEndpoint("sessions.fork", "POST", "/remote/sessions/fork", "SessionForkRequest", "SessionLoadResponse", "peer_token"),
    RemoteEndpoint("sessions.model", "POST", "/remote/sessions/model", "SessionModelSwitchRequest", "Ok", "peer_token"),
    RemoteEndpoint("chat.once", "POST", "/remote/chat", "ChatRequest", "ChatResponse", "peer_token"),
    RemoteEndpoint("chat.start", "POST", "/remote/chat/start", "ChatStartRequest", "ChatStartResponse", "peer_token"),
    RemoteEndpoint("chat.events", "POST", "/remote/chat/events", "ChatEventsRequest", "ChatEventsBatch", "peer_token"),
    RemoteEndpoint("chat.status", "POST", "/remote/chat/status", "ChatStatusRequest", "ChatStatusResponse", "peer_token"),
    RemoteEndpoint("chat.recover", "POST", "/remote/chat/recover", "ChatRecoverRequest", "ChatRecoverResponse", "peer_token"),
    RemoteEndpoint("chat.cancel", "POST", "/remote/chat/cancel", "ChatCancelRequest", "ChatCancelResponse", "peer_token"),
    RemoteEndpoint("chat.follow_up", "POST", "/remote/chat/follow-up", "ChatFollowUpRequest", "ChatFollowUpResponse", "peer_token"),
    RemoteEndpoint("chat.follow_up_cancel", "POST", "/remote/chat/follow-up/cancel", "ChatFollowUpCancelRequest", "ChatFollowUpResponse", "peer_token"),
    RemoteEndpoint("chat.approval_reply", "POST", "/remote/approval/reply", "ApprovalReplyRequest", "ApprovalReplyResponse", "peer_token"),
    RemoteEndpoint("agent_runs.events", "GET", "/remote/agent-runs/{agent_run_id}/events", "AgentRunEventsQuery", "AgentRunEventsResponse", "peer_token"),
    RemoteEndpoint("agent_runs.claim", "POST", "/remote/agent-runs/claim", "AgentRunClaimRequest", "AgentRunClaimResponse", "peer_token"),
    RemoteEndpoint("agent_runs.heartbeat", "POST", "/remote/agent-runs/heartbeat", "AgentRunHeartbeatRequest", "AgentRunHeartbeatResponse", "peer_token"),
    RemoteEndpoint("agent_runs.session", "POST", "/remote/agent-runs/session", "AgentRunSessionRequest", "Ok", "peer_token"),
    RemoteEndpoint("agent_runs.event", "POST", "/remote/agent-runs/event", "AgentRunEventRequest", "Ok", "peer_token"),
    RemoteEndpoint("agent_runs.complete", "POST", "/remote/agent-runs/complete", "AgentRunCompleteRequest", "AgentRunCompleteResponse", "peer_token"),
    RemoteEndpoint("admin.status", "POST", "/remote/admin/status", "Empty", "AdminStatus", "bearer"),
    RemoteEndpoint("admin.github.status", "GET", "/remote/admin/github/status", "none", "GitHubStatus", "bearer"),
    RemoteEndpoint("admin.agent_runs.submit", "POST", "/remote/admin/agent-runs/submit", "AgentRunRequest", "AgentRunResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.events", "POST", "/remote/admin/agent-runs/events", "AgentRunAdminEventsRequest", "AgentRunEventsResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.cancel", "POST", "/remote/admin/agent-runs/cancel", "AgentRunCancelRequest", "AgentRunCancelResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.retry", "POST", "/remote/admin/agent-runs/retry", "AgentRunRetryRequest", "AgentRunResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.list", "POST", "/remote/admin/agent-runs/list", "AgentRunListRequest", "AgentRunListResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.load", "POST", "/remote/admin/agent-runs/load", "AgentRunLoadRequest", "AgentRunDetail", "bearer"),
    RemoteEndpoint("admin.environment.run", "POST", "/remote/admin/environment/run", "EnvironmentRunRequest", "AgentRunResponse", "bearer"),
    RemoteEndpoint("admin.server_settings.read", "POST", "/remote/admin/server-settings/read", "Empty", "ServerSettings", "bearer"),
    RemoteEndpoint("admin.server_settings.update", "POST", "/remote/admin/server-settings/update", "ServerSettingsUpdateRequest", "ServerSettings", "bearer"),
    RemoteEndpoint("admin.diagnostics.tool_diagnostics.stats", "POST", "/remote/admin/diagnostics/tool-diagnostics/stats", "Empty", "ToolDiagnosticsStats", "bearer"),
    RemoteEndpoint("admin.model_capabilities.status", "POST", "/remote/admin/model-capabilities/status", "Empty", "ModelCapabilityStatus", "bearer"),
    RemoteEndpoint("admin.model_capabilities.list", "POST", "/remote/admin/model-capabilities/list", "ModelCapabilityListRequest", "ModelCapabilityList", "bearer"),
    RemoteEndpoint("admin.model_capabilities.refresh", "POST", "/remote/admin/model-capabilities/refresh", "Empty", "ModelCapabilityRefreshResult", "bearer"),
    RemoteEndpoint("admin.model_capabilities.apply", "POST", "/remote/admin/model-capabilities/apply", "ModelCapabilityApplyRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.providers.list", "POST", "/remote/admin/providers/list", "Empty", "ProviderList", "bearer"),
    RemoteEndpoint("admin.providers.record", "POST", "/remote/admin/providers/record", "ProviderRecordRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.providers.test", "POST", "/remote/admin/providers/test", "ProviderTestRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.providers.delete", "POST", "/remote/admin/providers/delete", "ProviderDeleteRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.providers.copy", "POST", "/remote/admin/providers/copy", "ProviderCopyRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.providers.enable", "POST", "/remote/admin/providers/enable", "ProviderEnableRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.providers.models", "POST", "/remote/admin/providers/models", "ProviderModelsRequest", "ProviderModels", "bearer"),
    RemoteEndpoint("admin.models.list", "POST", "/remote/admin/models/list", "Empty", "ModelProfileList", "bearer"),
    RemoteEndpoint("admin.models.record", "POST", "/remote/admin/models/record", "ModelProfileRecordRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.models.activate", "POST", "/remote/admin/models/activate", "ModelProfileActivateRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.toolchains.list", "POST", "/remote/admin/toolchains/list", "Empty", "ToolchainList", "bearer"),
    RemoteEndpoint("admin.toolchains.dashboard", "POST", "/remote/admin/toolchains/dashboard", "Empty", "ToolchainDashboard", "bearer"),
    RemoteEndpoint("admin.toolchains.record", "POST", "/remote/admin/toolchains/record", "ToolchainRecordRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.toolchains.delete", "POST", "/remote/admin/toolchains/delete", "ToolchainDeleteRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.toolchains.enable", "POST", "/remote/admin/toolchains/enable", "ToolchainEnableRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("mcp.manifest", "POST", "/remote/mcp/manifest", "MCPManifestRequest", "MCPManifestResponse", "peer_token"),
    RemoteEndpoint("mcp.tools", "POST", "/remote/mcp/tools", "PeerMCPToolsReport", "Ok", "peer_token"),
    # Binary artifact download authenticates with the X-RC-Peer-Token header.
    RemoteEndpoint("mcp.artifact", "GET", "/remote/mcp/artifacts/{artifact_path}", "none", "Binary", "peer_token"),
    RemoteEndpoint("environment.manifest", "POST", "/remote/environment/manifest", "EnvironmentManifestRequest", "EnvironmentManifestResponse", "peer_token"),
    RemoteEndpoint("artifacts.get", "GET", "/remote/artifacts/{os}/{arch}/{artifact_name}", "none", "Binary", "none"),
    RemoteEndpoint("github.webhook", "POST", "/remote/github/webhook", "WebhookPayload", "Ok", "webhook"),
    RemoteEndpoint("taskflow.get", "GET", "/remote/taskflow/{path}", "TaskflowQuery", "TaskflowResponse", "peer_token"),
    RemoteEndpoint("taskflow.post", "POST", "/remote/taskflow/{path}", "TaskflowRequest", "TaskflowResponse", "peer_token"),
    RemoteEndpoint("issues.get", "GET", "/remote/issues/{path}", "IssueAssignmentQuery", "IssueAssignmentResponse", "peer_token"),
    RemoteEndpoint("issues.post", "POST", "/remote/issues/{path}", "IssueAssignmentRequest", "IssueAssignmentResponse", "peer_token"),
    RemoteEndpoint("assignments.get", "GET", "/remote/assignments/{path}", "IssueAssignmentQuery", "IssueAssignmentResponse", "peer_token"),
    RemoteEndpoint("assignments.post", "POST", "/remote/assignments/{path}", "IssueAssignmentRequest", "IssueAssignmentResponse", "peer_token"),
    RemoteEndpoint("mentions.get", "GET", "/remote/mentions/{path}", "MentionQuery", "MentionResponse", "peer_token"),
    RemoteEndpoint("mentions.post", "POST", "/remote/mentions/{path}", "MentionRequest", "MentionResponse", "peer_token"),
)


def endpoint_registry() -> list[dict[str, str]]:
    return [endpoint.to_dict() for endpoint in REMOTE_ENDPOINTS]

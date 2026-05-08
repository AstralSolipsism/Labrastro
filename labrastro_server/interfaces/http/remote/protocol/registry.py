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
    RemoteEndpoint("peer.capabilities", "GET", "/remote/capabilities", "none", "Capabilities", "none"),
    RemoteEndpoint("peer.register", "POST", "/remote/register", "RegisterRequest", "RegisterEnvelope", "bootstrap_token"),
    RemoteEndpoint("peer.heartbeat", "POST", "/remote/heartbeat", "Heartbeat", "PeerHeartbeat", "peer_token"),
    RemoteEndpoint("peer.poll", "POST", "/remote/poll", "PeerPollRequest", "RelayEnvelope", "peer_token"),
    RemoteEndpoint("peer.result", "POST", "/remote/result", "PeerResultRequest", "Ok", "peer_token"),
    RemoteEndpoint("peer.disconnect", "POST", "/remote/disconnect", "DisconnectNotice", "Ok", "peer_token"),
    RemoteEndpoint("sessions.list", "POST", "/remote/sessions/list", "SessionListRequest", "SessionListResponse", "peer_token"),
    RemoteEndpoint("sessions.load", "POST", "/remote/sessions/load", "SessionLoadRequest", "SessionLoadResponse", "peer_token"),
    RemoteEndpoint("sessions.new", "POST", "/remote/sessions/new", "SessionNewRequest", "SessionNewResponse", "peer_token"),
    RemoteEndpoint("sessions.delete", "POST", "/remote/sessions/delete", "SessionDeleteRequest", "Ok", "peer_token"),
    RemoteEndpoint("sessions.snapshot", "POST", "/remote/sessions/snapshot", "SessionSnapshotRequest", "Ok", "peer_token"),
    RemoteEndpoint("sessions.model", "POST", "/remote/sessions/model", "SessionModelSwitchRequest", "Ok", "peer_token"),
    RemoteEndpoint("chat.once", "POST", "/remote/chat", "ChatRequest", "ChatResponse", "peer_token"),
    RemoteEndpoint("chat.start", "POST", "/remote/chat/start", "ChatStartRequest", "ChatStartResponse", "peer_token"),
    RemoteEndpoint("chat.stream", "POST", "/remote/chat/stream", "ChatStreamRequest", "ChatStreamResponse", "peer_token"),
    RemoteEndpoint("chat.cancel", "POST", "/remote/chat/cancel", "ChatCancelRequest", "ChatCancelResponse", "peer_token"),
    RemoteEndpoint("chat.approval_reply", "POST", "/remote/approval/reply", "ApprovalReplyRequest", "ApprovalReplyResponse", "peer_token"),
    RemoteEndpoint("runtime.events", "GET", "/remote/agent-runtime/tasks/{task_id}/events", "RuntimeEventsQuery", "RuntimeEventsResponse", "peer_token"),
    RemoteEndpoint("runtime.claim", "POST", "/remote/runtime/claim", "RuntimeClaimRequest", "RuntimeClaimResponse", "peer_token"),
    RemoteEndpoint("runtime.heartbeat", "POST", "/remote/runtime/heartbeat", "RuntimeHeartbeatRequest", "RuntimeHeartbeatResponse", "peer_token"),
    RemoteEndpoint("runtime.session", "POST", "/remote/runtime/session", "RuntimeSessionRequest", "Ok", "peer_token"),
    RemoteEndpoint("runtime.event", "POST", "/remote/runtime/event", "RuntimeEventRequest", "Ok", "peer_token"),
    RemoteEndpoint("runtime.complete", "POST", "/remote/runtime/complete", "RuntimeCompleteRequest", "RuntimeCompleteResponse", "peer_token"),
    RemoteEndpoint("admin.status", "POST", "/remote/admin/status", "Empty", "AdminStatus", "bearer"),
    RemoteEndpoint("admin.github.status", "GET", "/remote/admin/github/status", "none", "GitHubStatus", "bearer"),
    RemoteEndpoint("admin.runtime.submit", "POST", "/remote/admin/runtime/submit", "RuntimeTaskRequest", "RuntimeTaskResponse", "bearer"),
    RemoteEndpoint("admin.runtime.events", "POST", "/remote/admin/runtime/events", "RuntimeAdminEventsRequest", "RuntimeEventsResponse", "bearer"),
    RemoteEndpoint("admin.runtime.cancel", "POST", "/remote/admin/runtime/cancel", "RuntimeCancelRequest", "RuntimeCancelResponse", "bearer"),
    RemoteEndpoint("admin.runtime.retry", "POST", "/remote/admin/runtime/retry", "RuntimeRetryRequest", "RuntimeTaskResponse", "bearer"),
    RemoteEndpoint("admin.runtime.tasks.list", "POST", "/remote/admin/runtime/tasks/list", "RuntimeTaskListRequest", "RuntimeTaskListResponse", "bearer"),
    RemoteEndpoint("admin.runtime.tasks.load", "POST", "/remote/admin/runtime/tasks/load", "RuntimeTaskLoadRequest", "RuntimeTaskDetail", "bearer"),
    RemoteEndpoint("admin.environment.run", "POST", "/remote/admin/environment/run", "EnvironmentRunRequest", "RuntimeTaskResponse", "bearer"),
    RemoteEndpoint("admin.server_settings.read", "POST", "/remote/admin/server-settings/read", "Empty", "ServerSettings", "bearer"),
    RemoteEndpoint("admin.server_settings.update", "POST", "/remote/admin/server-settings/update", "ServerSettingsUpdateRequest", "ServerSettings", "bearer"),
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
    RemoteEndpoint("mcp.artifact", "GET", "/remote/mcp/artifacts/{artifact_path}", "none", "Binary", "none"),
    RemoteEndpoint("environment.manifest", "POST", "/remote/environment/manifest", "EnvironmentManifestRequest", "EnvironmentManifestResponse", "peer_token"),
    RemoteEndpoint("artifacts.get", "GET", "/remote/artifacts/{artifact_id}", "none", "Binary", "none"),
    RemoteEndpoint("github.webhook", "POST", "/remote/github/webhook", "WebhookPayload", "Ok", "webhook"),
    RemoteEndpoint("taskflow.get", "GET", "/remote/taskflow/{path}", "TaskflowQuery", "TaskflowResponse", "bearer"),
    RemoteEndpoint("taskflow.post", "POST", "/remote/taskflow/{path}", "TaskflowRequest", "TaskflowResponse", "bearer"),
    RemoteEndpoint("issues.get", "GET", "/remote/issues/{path}", "IssueAssignmentQuery", "IssueAssignmentResponse", "bearer"),
    RemoteEndpoint("issues.post", "POST", "/remote/issues/{path}", "IssueAssignmentRequest", "IssueAssignmentResponse", "bearer"),
    RemoteEndpoint("assignments.get", "GET", "/remote/assignments/{path}", "IssueAssignmentQuery", "IssueAssignmentResponse", "bearer"),
    RemoteEndpoint("assignments.post", "POST", "/remote/assignments/{path}", "IssueAssignmentRequest", "IssueAssignmentResponse", "bearer"),
    RemoteEndpoint("mentions.get", "GET", "/remote/mentions/{path}", "MentionQuery", "MentionResponse", "bearer"),
    RemoteEndpoint("mentions.post", "POST", "/remote/mentions/{path}", "MentionRequest", "MentionResponse", "bearer"),
)


def endpoint_registry() -> list[dict[str, str]]:
    return [endpoint.to_dict() for endpoint in REMOTE_ENDPOINTS]

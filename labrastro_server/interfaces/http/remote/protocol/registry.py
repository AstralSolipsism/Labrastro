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
    RemoteEndpoint("sessions.model", "POST", "/remote/sessions/model", "SessionModelSwitchRequest", "Ok", "peer_token"),
    RemoteEndpoint("session_run.start", "POST", "/remote/session-runs/start", "SessionRunStartRequest", "SessionRunStartResponse", "peer_token"),
    RemoteEndpoint("session_run.continue", "POST", "/remote/session-runs/continue", "SessionRunContinueRequest", "SessionRunContinueResponse", "peer_token"),
    RemoteEndpoint("chat.command_dispatch", "POST", "/remote/chat/command", "ChatCommandDispatchRequest", "ChatCommandDispatchResponse", "peer_token"),
    RemoteEndpoint("session_run.events", "POST", "/remote/session-runs/events", "SessionRunEventsRequest", "SessionRunEventsBatch", "peer_token"),
    RemoteEndpoint("session_run.status", "POST", "/remote/session-runs/status", "SessionRunStatusRequest", "SessionRunStatusResponse", "peer_token"),
    RemoteEndpoint("session_run.branch_select", "POST", "/remote/session-runs/branches/select", "SessionRunBranchSelectRequest", "SessionRunStatusResponse", "peer_token"),
    RemoteEndpoint("session_run.recover", "POST", "/remote/session-runs/recover", "SessionRunRecoverRequest", "SessionRunRecoverResponse", "peer_token"),
    RemoteEndpoint("session_run.cancel", "POST", "/remote/session-runs/cancel", "SessionRunCancelRequest", "SessionRunCancelResponse", "peer_token"),
    RemoteEndpoint("session_run.user_input_reply", "POST", "/remote/session-runs/user-input/reply", "SessionRunUserInputReplyRequest", "SessionRunUserInputReplyResponse", "peer_token"),
    RemoteEndpoint("chat.approval_reply", "POST", "/remote/approval/reply", "ApprovalReplyRequest", "ApprovalReplyResponse", "peer_token"),
    RemoteEndpoint("agent_runs.events", "GET", "/remote/agent-runs/{agent_run_id}/events", "AgentRunEventsQuery", "AgentRunEventsResponse", "peer_token"),
    RemoteEndpoint("agent_runs.steer", "POST", "/remote/agent-runs/{agent_run_id}/steer", "SessionRunAgentRunSteerRequest", "AgentRunSteerResponse", "peer_token"),
    RemoteEndpoint("agent_run_activations.claim", "POST", "/remote/agent-run-activations/claim", "AgentRunActivationClaimRequest", "AgentRunActivationClaimResponse", "peer_token"),
    RemoteEndpoint("agent_run_activations.heartbeat", "POST", "/remote/agent-run-activations/heartbeat", "AgentRunActivationHeartbeatRequest", "AgentRunActivationHeartbeatResponse", "peer_token"),
    RemoteEndpoint("agent_run_activations.session", "POST", "/remote/agent-run-activations/session", "AgentRunActivationSessionPinRequest", "AgentRunActivationSessionPinResponse", "peer_token"),
    RemoteEndpoint("agent_run_activations.event", "POST", "/remote/agent-run-activations/event", "AgentRunActivationEventRequest", "Ok", "peer_token"),
    RemoteEndpoint("agent_run_activations.model_request", "POST", "/remote/agent-run-activations/model-request", "AgentRunActivationModelRequest", "AgentRunActivationModelResponse", "peer_token"),
    RemoteEndpoint("agent_run_activations.complete", "POST", "/remote/agent-run-activations/complete", "AgentRunActivationCompleteRequest", "AgentRunActivationCompleteResponse", "peer_token"),
    RemoteEndpoint("local_actions.claim", "POST", "/remote/local-actions/claim", "LocalActionClaimRequest", "LocalActionClaimResponse", "peer_token"),
    RemoteEndpoint("local_actions.progress", "POST", "/remote/local-actions/progress", "LocalActionProgressRequest", "LocalActionProgressResponse", "peer_token"),
    RemoteEndpoint("local_actions.complete", "POST", "/remote/local-actions/complete", "LocalActionCompleteRequest", "LocalActionCompleteResponse", "peer_token"),
    RemoteEndpoint("local_actions.cancel", "POST", "/remote/local-actions/cancel", "LocalActionCancelRequest", "LocalActionCancelResponse", "peer_token"),
    RemoteEndpoint("admin.status", "POST", "/remote/admin/status", "Empty", "AdminStatus", "bearer"),
    RemoteEndpoint("admin.github.status", "GET", "/remote/admin/github/status", "none", "GitHubStatus", "bearer"),
    RemoteEndpoint("admin.agent_runs.submit", "POST", "/remote/admin/agent-runs/submit", "AgentRunRequest", "AgentRunResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.events", "POST", "/remote/admin/agent-runs/events", "AgentRunAdminEventsRequest", "AgentRunEventsResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.cancel", "POST", "/remote/admin/agent-runs/cancel", "AgentRunCancelRequest", "AgentRunCancelResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.retry", "POST", "/remote/admin/agent-runs/retry", "AgentRunRetryRequest", "AgentRunResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.branch", "POST", "/remote/admin/agent-runs/branch", "AgentRunBranchRequest", "AgentRunResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.fork", "POST", "/remote/admin/agent-runs/fork", "AgentRunForkRequest", "AgentRunResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.steer", "POST", "/remote/admin/agent-runs/steer", "AgentRunSteerRequest", "AgentRunSteerResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.list", "POST", "/remote/admin/agent-runs/list", "AgentRunListRequest", "AgentRunListResponse", "bearer"),
    RemoteEndpoint("admin.agent_runs.load", "POST", "/remote/admin/agent-runs/load", "AgentRunLoadRequest", "AgentRunDetail", "bearer"),
    RemoteEndpoint("admin.environment.run", "POST", "/remote/admin/environment/run", "EnvironmentRunRequest", "AgentRunResponse", "bearer"),
    RemoteEndpoint("admin.capability_packages.ingest_session_start", "POST", "/remote/admin/capability-packages/ingest/session/start", "CapabilityPackageIngestSessionStartRequest", "SessionRunStartResponse", "bearer"),
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
    RemoteEndpoint("admin.models.delete", "POST", "/remote/admin/models/delete", "ModelProfileDeleteRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.models.activate", "POST", "/remote/admin/models/activate", "ModelProfileActivateRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.environment_requirements.list", "POST", "/remote/admin/environment-requirements/list", "Empty", "EnvironmentRequirementList", "bearer"),
    RemoteEndpoint("admin.environment_requirements.dashboard", "POST", "/remote/admin/environment-requirements/dashboard", "Empty", "EnvironmentRequirementDashboard", "bearer"),
    RemoteEndpoint("admin.behavior.catalog", "POST", "/remote/admin/behavior/catalog", "Empty", "BehaviorCatalog", "bearer"),
    RemoteEndpoint("admin.environment_requirements.record", "POST", "/remote/admin/environment-requirements/record", "EnvironmentRequirementRecordRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.environment_requirements.delete", "POST", "/remote/admin/environment-requirements/delete", "EnvironmentRequirementDeleteRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.environment_requirements.enable", "POST", "/remote/admin/environment-requirements/enable", "EnvironmentRequirementEnableRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.mcp_servers.list", "POST", "/remote/admin/mcp-servers/list", "Empty", "MCPServerList", "bearer"),
    RemoteEndpoint("admin.mcp_servers.dashboard", "POST", "/remote/admin/mcp-servers/dashboard", "Empty", "MCPServerDashboard", "bearer"),
    RemoteEndpoint("admin.mcp_servers.record", "POST", "/remote/admin/mcp-servers/record", "MCPServerRecordRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.mcp_servers.delete", "POST", "/remote/admin/mcp-servers/delete", "MCPServerDeleteRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.mcp_servers.enable", "POST", "/remote/admin/mcp-servers/enable", "MCPServerEnableRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.skills.list", "POST", "/remote/admin/skills/list", "Empty", "SkillList", "bearer"),
    RemoteEndpoint("admin.skills.dashboard", "POST", "/remote/admin/skills/dashboard", "Empty", "SkillDashboard", "bearer"),
    RemoteEndpoint("admin.skills.record", "POST", "/remote/admin/skills/record", "SkillRecordRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.skills.delete", "POST", "/remote/admin/skills/delete", "SkillDeleteRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.skills.enable", "POST", "/remote/admin/skills/enable", "SkillEnableRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("admin.lifecycle_hooks.trust", "POST", "/remote/admin/lifecycle-hooks/trust", "LifecycleHookTrustRequest", "AdminMutationResult", "bearer"),
    RemoteEndpoint("mcp.manifest", "POST", "/remote/mcp/manifest", "MCPManifestRequest", "MCPManifestResponse", "peer_token"),
    RemoteEndpoint("mcp.tools", "POST", "/remote/mcp/tools", "PeerMCPToolsReport", "Ok", "peer_token"),
    # Binary artifact download authenticates with the X-RC-Peer-Token header.
    RemoteEndpoint("mcp.artifact", "GET", "/remote/mcp/artifacts/{artifact_path}", "none", "Binary", "peer_token"),
    RemoteEndpoint("environment.manifest", "POST", "/remote/environment/manifest", "EnvironmentManifestRequest", "EnvironmentManifestResponse", "peer_token"),
    RemoteEndpoint("capability_package.install_plan", "POST", "/remote/capability-packages/install/plan", "CapabilityPackageInstallPlanRequest", "CapabilityPackageInstallPlanResponse", "peer_token"),
    RemoteEndpoint("capability_package.install_result", "POST", "/remote/capability-packages/install/result", "CapabilityPackageInstallResultRequest", "CapabilityPackageInstallResultResponse", "peer_token"),
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

package protocol

type ErrorResponse struct {
	OK        bool           `json:"ok"`
	Error     string         `json:"error"`
	Message   string         `json:"message"`
	Details   map[string]any `json:"details,omitempty"`
	RequestID string         `json:"request_id"`
}

type RegisterRequest struct {
	BootstrapToken string         `json:"bootstrap_token"`
	HostInfoMin    map[string]any `json:"host_info_min,omitempty"`
	CWD            string         `json:"cwd,omitempty"`
	WorkspaceRoot  string         `json:"workspace_root,omitempty"`
	Features       []string       `json:"features,omitempty"`
}

type RegisterResponseEnvelope struct {
	Type    string           `json:"type"`
	Payload RegisterResponse `json:"payload"`
}

type RegisterRejectedEnvelope struct {
	Type    string           `json:"type"`
	Payload RegisterRejected `json:"payload"`
}

type RegisterResponse struct {
	PeerID               string `json:"peer_id"`
	PeerToken            string `json:"peer_token"`
	HeartbeatIntervalSec int    `json:"heartbeat_interval_sec"`
}

type RegisterRejected struct {
	Reason string `json:"reason"`
}

type Heartbeat struct {
	PeerToken string  `json:"peer_token"`
	TS        float64 `json:"ts"`
}

type DisconnectRequest struct {
	PeerToken string `json:"peer_token"`
	Reason    string `json:"reason"`
}

type LocalActionRecord struct {
	Scope           string         `json:"scope"`
	LocalActionID   string         `json:"local_action_id"`
	ActionKind      string         `json:"action_kind"`
	Status          string         `json:"status,omitempty"`
	AgentRunID      string         `json:"agent_run_id,omitempty"`
	ActivationID    string         `json:"activation_id,omitempty"`
	SessionRunID    string         `json:"session_run_id,omitempty"`
	BranchBindingID string         `json:"branch_binding_id,omitempty"`
	AdminTaskID     string         `json:"admin_task_id,omitempty"`
	RequestedBy     string         `json:"requested_by,omitempty"`
	PeerID          string         `json:"peer_id,omitempty"`
	WorkspaceRoot   string         `json:"workspace_root,omitempty"`
	Payload         map[string]any `json:"payload,omitempty"`
	Progress        map[string]any `json:"progress,omitempty"`
	Result          map[string]any `json:"result,omitempty"`
	Error           string         `json:"error,omitempty"`
	LeaseID         string         `json:"lease_id,omitempty"`
	LeaseExpiresAt  float64        `json:"lease_expires_at,omitempty"`
	CreatedAt       float64        `json:"created_at,omitempty"`
	UpdatedAt       float64        `json:"updated_at,omitempty"`
}

type LocalActionClaimRequest struct {
	PeerToken     string   `json:"peer_token"`
	PeerID        string   `json:"peer_id"`
	WorkerKind    string   `json:"worker_kind"`
	Features      []string `json:"features,omitempty"`
	WorkspaceRoot string   `json:"workspace_root,omitempty"`
	MaxActions    int      `json:"max_actions,omitempty"`
}

type LocalActionClaimResponse struct {
	Actions []LocalActionRecord `json:"actions,omitempty"`
}

type LocalActionProgressRequest struct {
	PeerToken     string         `json:"peer_token"`
	LocalActionID string         `json:"local_action_id"`
	LeaseID       string         `json:"lease_id"`
	Status        string         `json:"status,omitempty"`
	Progress      map[string]any `json:"progress,omitempty"`
}

type LocalActionProgressResponse struct {
	OK     bool               `json:"ok"`
	Action *LocalActionRecord `json:"action,omitempty"`
	Error  string             `json:"error,omitempty"`
}

type LocalActionCompleteRequest struct {
	PeerToken     string         `json:"peer_token"`
	LocalActionID string         `json:"local_action_id"`
	LeaseID       string         `json:"lease_id"`
	Status        string         `json:"status"`
	Result        map[string]any `json:"result,omitempty"`
	Error         string         `json:"error,omitempty"`
}

type LocalActionCompleteResponse struct {
	OK     bool               `json:"ok"`
	Action *LocalActionRecord `json:"action,omitempty"`
	Error  string             `json:"error,omitempty"`
}

type LocalActionCancelRequest struct {
	PeerToken     string `json:"peer_token"`
	LocalActionID string `json:"local_action_id"`
	LeaseID       string `json:"lease_id"`
	Reason        string `json:"reason,omitempty"`
}

type LocalActionCancelResponse struct {
	OK     bool               `json:"ok"`
	Action *LocalActionRecord `json:"action,omitempty"`
	Error  string             `json:"error,omitempty"`
}

type SessionRunStartRequest struct {
	PeerToken       string           `json:"peer_token"`
	Prompt          string           `json:"prompt"`
	SessionHint     string           `json:"session_hint,omitempty"`
	ClientRequestID string           `json:"client_request_id,omitempty"`
	Mode            string           `json:"mode,omitempty"`
	WorkflowMode    string           `json:"workflow_mode,omitempty"`
	TaskflowID      string           `json:"taskflow_id,omitempty"`
	ProviderID      string           `json:"provider_id,omitempty"`
	ModelID         string           `json:"model_id,omitempty"`
	Parameters      map[string]any   `json:"parameters,omitempty"`
	Locale          string           `json:"locale,omitempty"`
	Mentions        []map[string]any `json:"mentions,omitempty"`
}

type SessionRunStartResponse struct {
	SessionRunID    string `json:"session_run_id"`
	BranchBindingID string `json:"branch_binding_id,omitempty"`
	Error           string `json:"error,omitempty"`
}

type SessionRunEventsRequest struct {
	PeerToken       string  `json:"peer_token"`
	SessionRunID    string  `json:"session_run_id"`
	BranchBindingID string  `json:"branch_binding_id,omitempty"`
	Cursor          int     `json:"cursor"`
	TimeoutSec      float64 `json:"timeout_sec,omitempty"`
}

type SessionRunEvent struct {
	SessionRunID string         `json:"session_run_id"`
	Seq          int            `json:"seq"`
	Type         string         `json:"type"`
	Payload      map[string]any `json:"payload,omitempty"`
}

type SessionRunEventsBatch struct {
	Events     []SessionRunEvent `json:"events,omitempty"`
	Done       bool              `json:"done"`
	NextCursor int               `json:"next_cursor"`
	Error      string            `json:"error,omitempty"`
}

type ApprovalReplyRequest struct {
	PeerToken       string `json:"peer_token"`
	SessionRunID    string `json:"session_run_id"`
	BranchBindingID string `json:"branch_binding_id"`
	ApprovalID      string `json:"approval_id"`
	Decision        string `json:"decision"`
	Reason          string `json:"reason,omitempty"`
}

type ApprovalReplyResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

type MCPManifestRequest struct {
	PeerToken string `json:"peer_token"`
	OS        string `json:"os"`
	Arch      string `json:"arch"`
	Workspace string `json:"workspace,omitempty"`
}

type MCPArtifactManifest struct {
	Platform string `json:"platform"`
	Path     string `json:"path"`
	SHA256   string `json:"sha256"`
	URL      string `json:"url"`
}

type MCPLaunchManifest struct {
	Command string            `json:"command"`
	Args    []string          `json:"args,omitempty"`
	Env     map[string]string `json:"env,omitempty"`
	CWD     string            `json:"cwd,omitempty"`
}

type MCPServerManifest struct {
	Name                       string               `json:"name"`
	Version                    string               `json:"version"`
	Distribution               string               `json:"distribution,omitempty"`
	Artifact                   *MCPArtifactManifest `json:"artifact,omitempty"`
	Launch                     MCPLaunchManifest    `json:"launch"`
	Permissions                map[string]any       `json:"permissions,omitempty"`
	EnvironmentRequirementRefs []string             `json:"environment_requirement_refs,omitempty"`
}

type MCPManifestResponse struct {
	Servers     []MCPServerManifest `json:"servers,omitempty"`
	Diagnostics []map[string]any    `json:"diagnostics,omitempty"`
}

type MCPToolInfo struct {
	Name        string         `json:"name"`
	Description string         `json:"description,omitempty"`
	InputSchema map[string]any `json:"input_schema,omitempty"`
	ServerName  string         `json:"server_name,omitempty"`
}

type MCPToolsReport struct {
	PeerToken   string           `json:"peer_token"`
	Tools       []MCPToolInfo    `json:"tools,omitempty"`
	Diagnostics []map[string]any `json:"diagnostics,omitempty"`
}

type MCPToolsReportResponse struct {
	OK     bool   `json:"ok"`
	PeerID string `json:"peer_id,omitempty"`
}

type EnvironmentManifestRequest struct {
	PeerToken string `json:"peer_token"`
	OS        string `json:"os"`
	Arch      string `json:"arch"`
	Workspace string `json:"workspace,omitempty"`
	AgentID   string `json:"agent_id,omitempty"`
}

type EnvironmentRequirementManifest struct {
	ID           string            `json:"id"`
	Kind         string            `json:"kind"`
	Name         string            `json:"name"`
	Command      string            `json:"command,omitempty"`
	Args         []string          `json:"args,omitempty"`
	Env          map[string]string `json:"env,omitempty"`
	CWD          string            `json:"cwd,omitempty"`
	Placement    string            `json:"placement,omitempty"`
	Tags         []string          `json:"tags,omitempty"`
	Requirements map[string]string `json:"requirements,omitempty"`
	Check        string            `json:"check,omitempty"`
	Install      string            `json:"install,omitempty"`
	Configure    string            `json:"configure,omitempty"`
	Version      string            `json:"version,omitempty"`
	Runtime      string            `json:"runtime,omitempty"`
	Language     string            `json:"language,omitempty"`
	Scope        string            `json:"scope,omitempty"`
	Path         string            `json:"path,omitempty"`
	Source       string            `json:"source,omitempty"`
	Description  string            `json:"description,omitempty"`
}

type EnvironmentManifestResponse struct {
	EnvironmentRequirements []EnvironmentRequirementManifest `json:"environment_requirements,omitempty"`
}

type ExecToolRequest struct {
	ToolName              string         `json:"tool_name"`
	Args                  map[string]any `json:"args"`
	CWD                   *string        `json:"cwd"`
	TimeoutSec            int            `json:"timeout_sec"`
	PreviewIdentity       map[string]any `json:"preview_identity,omitempty"`
	ApprovedSaveCandidate map[string]any `json:"approved_save_candidate,omitempty"`
	ToolCallID            string         `json:"tool_call_id,omitempty"`
}

type ExecToolResult struct {
	OK           bool           `json:"ok"`
	Result       string         `json:"result,omitempty"`
	ErrorCode    string         `json:"error_code,omitempty"`
	ErrorMessage string         `json:"error_message,omitempty"`
	Meta         map[string]any `json:"meta,omitempty"`
}

type ToolPreviewRequest struct {
	ToolName   string         `json:"tool_name"`
	Args       map[string]any `json:"args"`
	CWD        *string        `json:"cwd"`
	TimeoutSec int            `json:"timeout_sec"`
}

type ToolPreviewResult struct {
	OK           bool             `json:"ok"`
	Sections     []map[string]any `json:"sections,omitempty"`
	ResolvedPath string           `json:"resolved_path,omitempty"`
	Diff         string           `json:"diff,omitempty"`
	OriginalText string           `json:"original_text,omitempty"`
	ModifiedText string           `json:"modified_text,omitempty"`
	ErrorCode    string           `json:"error_code,omitempty"`
	ErrorMessage string           `json:"error_message,omitempty"`
	Meta         map[string]any   `json:"meta,omitempty"`
}

type ToolStreamChunk struct {
	ChunkType  string         `json:"chunk_type"`
	Data       string         `json:"data,omitempty"`
	ToolCallID string         `json:"tool_call_id,omitempty"`
	Meta       map[string]any `json:"meta,omitempty"`
}

type AgentRunActivationClaimRequest struct {
	PeerToken  string   `json:"peer_token"`
	WorkerID   string   `json:"worker_id,omitempty"`
	WorkerKind string   `json:"worker_kind,omitempty"`
	Executors  []string `json:"executors,omitempty"`
	WaitSec    int      `json:"wait_sec,omitempty"`
}

type AgentRunActivationClaimResponse struct {
	Claim *AgentRunActivationClaim `json:"claim,omitempty"`
}

type AgentRunActivationClaim struct {
	RequestID       string          `json:"request_id"`
	ActivationID    string          `json:"activation_id"`
	WorkerID        string          `json:"worker_id"`
	AgentRun        map[string]any  `json:"agent_run"`
	Activation      map[string]any  `json:"activation,omitempty"`
	ExecutorRequest ExecutorRequest `json:"executor_request"`
	RuntimeSnapshot map[string]any  `json:"runtime_snapshot,omitempty"`
}

type ExecutorRequest struct {
	TaskID             string         `json:"agent_run_id"`
	AgentID            string         `json:"agent_id"`
	Executor           string         `json:"executor"`
	Prompt             string         `json:"prompt"`
	ExecutionLocation  string         `json:"execution_location,omitempty"`
	RuntimeProfileID   string         `json:"runtime_profile_id,omitempty"`
	WorkerKind         string         `json:"worker_kind,omitempty"`
	ModelRequestOrigin string         `json:"model_request_origin,omitempty"`
	WorktreeRole       string         `json:"worktree_role,omitempty"`
	PublishPolicy      string         `json:"publish_policy,omitempty"`
	Workdir            string         `json:"workdir,omitempty"`
	Branch             string         `json:"branch,omitempty"`
	Model              string         `json:"model,omitempty"`
	ExecutorSessionID  string         `json:"executor_session_id,omitempty"`
	Metadata           map[string]any `json:"metadata,omitempty"`
}

type AgentRunActivationEventReport struct {
	PeerToken    string         `json:"peer_token"`
	RequestID    string         `json:"request_id,omitempty"`
	ActivationID string         `json:"activation_id"`
	TaskID       string         `json:"agent_run_id"`
	WorkerID     string         `json:"worker_id,omitempty"`
	Type         string         `json:"type"`
	Text         string         `json:"text,omitempty"`
	Data         map[string]any `json:"data,omitempty"`
}

type AgentRunEvent struct {
	TaskID  string         `json:"agent_run_id"`
	Seq     int            `json:"seq"`
	Type    string         `json:"type"`
	Payload map[string]any `json:"payload,omitempty"`
}

type AgentRunEventsResponse struct {
	OK      bool            `json:"ok"`
	Events  []AgentRunEvent `json:"events,omitempty"`
	NextSeq int             `json:"next_seq"`
	HasMore bool            `json:"has_more"`
}

type AgentRunActivationHeartbeatRequest struct {
	PeerToken         string   `json:"peer_token"`
	RequestID         string   `json:"request_id"`
	ActivationID      string   `json:"activation_id"`
	TaskID            string   `json:"agent_run_id"`
	WorkerID          string   `json:"worker_id"`
	LeaseSec          int      `json:"lease_sec,omitempty"`
	DeliveredSteerIDs []string `json:"delivered_steer_ids,omitempty"`
}

type AgentRunActivationHeartbeatResponse struct {
	OK               bool              `json:"ok"`
	CancelRequested  bool              `json:"cancel_requested"`
	Reason           string            `json:"reason,omitempty"`
	LeaseSec         int               `json:"lease_sec,omitempty"`
	ActivationSteers []ActivationSteer `json:"activation_steers,omitempty"`
}

type ActivationSteer struct {
	ID           string         `json:"id"`
	ActivationID string         `json:"activation_id"`
	Source       string         `json:"source"`
	Payload      map[string]any `json:"payload,omitempty"`
	CreatedAt    string         `json:"created_at,omitempty"`
	DeliveredAt  string         `json:"delivered_at,omitempty"`
	Status       string         `json:"status,omitempty"`
	Metadata     map[string]any `json:"metadata,omitempty"`
}

type AgentRunSteerRequest struct {
	PeerToken       string         `json:"peer_token,omitempty"`
	TaskID          string         `json:"agent_run_id,omitempty"`
	SessionRunID    string         `json:"session_run_id,omitempty"`
	BranchBindingID string         `json:"branch_binding_id,omitempty"`
	ActivationID    string         `json:"activation_id,omitempty"`
	Source          string         `json:"source,omitempty"`
	Payload         map[string]any `json:"payload"`
	IdempotencyKey  string         `json:"idempotency_key,omitempty"`
	ClientSteerID   string         `json:"client_steer_id,omitempty"`
	Metadata        map[string]any `json:"metadata,omitempty"`
}

type AgentRunSteerResponse struct {
	OK              bool            `json:"ok"`
	Status          string          `json:"status,omitempty"`
	ActivationSteer ActivationSteer `json:"activation_steer,omitempty"`
	Error           string          `json:"error,omitempty"`
}

type AgentRunActivationCompleteRequest struct {
	PeerToken    string                          `json:"peer_token"`
	RequestID    string                          `json:"request_id"`
	ActivationID string                          `json:"activation_id"`
	TaskID       string                          `json:"agent_run_id"`
	WorkerID     string                          `json:"worker_id,omitempty"`
	Status       string                          `json:"status"`
	Output       string                          `json:"output,omitempty"`
	Error        string                          `json:"error,omitempty"`
	SessionID    string                          `json:"executor_session_id,omitempty"`
	Usage        map[string]any                  `json:"usage,omitempty"`
	Artifacts    []map[string]any                `json:"artifacts,omitempty"`
	Events       []AgentRunActivationEventReport `json:"events,omitempty"`
}

type AgentRunActivationCompleteResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

type AgentRunActivationSessionPinRequest struct {
	PeerToken         string `json:"peer_token"`
	RequestID         string `json:"request_id"`
	ActivationID      string `json:"activation_id"`
	TaskID            string `json:"agent_run_id"`
	WorkerID          string `json:"worker_id"`
	Workdir           string `json:"workdir,omitempty"`
	Branch            string `json:"branch,omitempty"`
	RepoURL           string `json:"repo_url,omitempty"`
	CachePath         string `json:"cache_path,omitempty"`
	ExecutorSessionID string `json:"executor_session_id,omitempty"`
}

type AgentRunActivationSessionPinResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

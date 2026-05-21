package protocol

import "encoding/json"

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

type RelayEnvelope struct {
	Type      string         `json:"type"`
	RequestID string         `json:"request_id,omitempty"`
	PeerID    string         `json:"peer_id,omitempty"`
	Payload   map[string]any `json:"payload,omitempty"`
}

type PollRequest struct {
	PeerToken string `json:"peer_token"`
}

type ResultRequest struct {
	PeerToken string         `json:"peer_token"`
	RequestID string         `json:"request_id"`
	Type      string         `json:"type"`
	Payload   map[string]any `json:"payload"`
}

type DisconnectRequest struct {
	PeerToken string `json:"peer_token"`
	Reason    string `json:"reason"`
}

type ChatRequest struct {
	PeerToken    string `json:"peer_token"`
	Prompt       string `json:"prompt"`
	WorkflowMode string `json:"workflow_mode,omitempty"`
	TaskflowID   string `json:"taskflow_id,omitempty"`
}

type ChatResponse struct {
	Response string `json:"response"`
	Error    string `json:"error,omitempty"`
}

type ChatStartRequest struct {
	PeerToken    string `json:"peer_token"`
	Prompt       string `json:"prompt"`
	SessionHint  string `json:"session_hint,omitempty"`
	WorkflowMode string `json:"workflow_mode,omitempty"`
	TaskflowID   string `json:"taskflow_id,omitempty"`
}

type ChatStartResponse struct {
	ChatID string `json:"chat_id"`
	Error  string `json:"error,omitempty"`
}

type ChatEventsRequest struct {
	PeerToken  string  `json:"peer_token"`
	ChatID     string  `json:"chat_id"`
	Cursor     int     `json:"cursor"`
	TimeoutSec float64 `json:"timeout_sec,omitempty"`
}

type ChatEvent struct {
	ChatID  string         `json:"chat_id"`
	Seq     int            `json:"seq"`
	Type    string         `json:"type"`
	Payload map[string]any `json:"payload,omitempty"`
}

type ChatEventsBatch struct {
	Events     []ChatEvent `json:"events,omitempty"`
	Done       bool        `json:"done"`
	NextCursor int         `json:"next_cursor"`
	Error      string      `json:"error,omitempty"`
}

type ApprovalReplyRequest struct {
	PeerToken  string `json:"peer_token"`
	ChatID     string `json:"chat_id"`
	ApprovalID string `json:"approval_id"`
	Decision   string `json:"decision"`
	Reason     string `json:"reason,omitempty"`
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
	Name         string               `json:"name"`
	Version      string               `json:"version"`
	Distribution string               `json:"distribution,omitempty"`
	Artifact     *MCPArtifactManifest `json:"artifact,omitempty"`
	Launch       MCPLaunchManifest    `json:"launch"`
	Permissions  map[string]any       `json:"permissions,omitempty"`
	Requirements map[string]string    `json:"requirements,omitempty"`
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
}

type EnvironmentCLIToolManifest struct {
	Name        string   `json:"name"`
	Command     string   `json:"command,omitempty"`
	Tags        []string `json:"tags,omitempty"`
	Check       string   `json:"check,omitempty"`
	Install     string   `json:"install,omitempty"`
	Version     string   `json:"version,omitempty"`
	Source      string   `json:"source,omitempty"`
	Description string   `json:"description,omitempty"`
}

type EnvironmentMCPServerManifest struct {
	Name         string            `json:"name"`
	Command      string            `json:"command,omitempty"`
	Args         []string          `json:"args,omitempty"`
	Env          map[string]string `json:"env,omitempty"`
	CWD          string            `json:"cwd,omitempty"`
	Placement    string            `json:"placement,omitempty"`
	Distribution string            `json:"distribution,omitempty"`
	Requirements map[string]string `json:"requirements,omitempty"`
	Check        string            `json:"check,omitempty"`
	Install      string            `json:"install,omitempty"`
	Version      string            `json:"version,omitempty"`
	Source       string            `json:"source,omitempty"`
	Description  string            `json:"description,omitempty"`
}

type EnvironmentSkillManifest struct {
	Name         string            `json:"name"`
	Scope        string            `json:"scope,omitempty"`
	Check        string            `json:"check,omitempty"`
	Install      string            `json:"install,omitempty"`
	Version      string            `json:"version,omitempty"`
	Source       string            `json:"source,omitempty"`
	Description  string            `json:"description,omitempty"`
	PathHint     string            `json:"path_hint,omitempty"`
	Requirements map[string]string `json:"requirements,omitempty"`
}

type EnvironmentManifestResponse struct {
	CLITools   []EnvironmentCLIToolManifest   `json:"cli_tools,omitempty"`
	MCPServers []EnvironmentMCPServerManifest `json:"mcp_servers,omitempty"`
	Skills     []EnvironmentSkillManifest     `json:"skills,omitempty"`
}

type ExecToolRequest struct {
	ToolName      string                    `json:"tool_name"`
	Args          map[string]any            `json:"args"`
	CWD           *string                   `json:"cwd"`
	TimeoutSec    int                       `json:"timeout_sec"`
	ExpectedState *ToolMutationPreviewState `json:"expected_state,omitempty"`
	ToolCallID    string                    `json:"tool_call_id,omitempty"`
}

type ExecToolResult struct {
	OK           bool           `json:"ok"`
	Result       string         `json:"result,omitempty"`
	ErrorCode    string         `json:"error_code,omitempty"`
	ErrorMessage string         `json:"error_message,omitempty"`
	Meta         map[string]any `json:"meta,omitempty"`
}

type ToolMutationPreviewState struct {
	ResolvedPath string `json:"resolved_path,omitempty"`
	OldSHA256    string `json:"old_sha256,omitempty"`
	OldExists    *bool  `json:"old_exists,omitempty"`
	OldSize      *int64 `json:"old_size,omitempty"`
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
	OldSHA256    string           `json:"old_sha256,omitempty"`
	OldExists    *bool            `json:"old_exists,omitempty"`
	OldSize      *int64           `json:"old_size,omitempty"`
	OldMTimeNS   *int64           `json:"old_mtime_ns,omitempty"`
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

type CleanupResult struct {
	OK           bool     `json:"ok"`
	RemovedItems []string `json:"removed_items,omitempty"`
	ErrorMessage string   `json:"error_message,omitempty"`
}

type AgentRunClaimRequest struct {
	PeerToken string   `json:"peer_token"`
	WorkerID  string   `json:"worker_id,omitempty"`
	Executors []string `json:"executors,omitempty"`
	WaitSec   int      `json:"wait_sec,omitempty"`
}

type AgentRunClaimResponse struct {
	Claim *AgentRunClaim `json:"claim,omitempty"`
}

type AgentRunClaim struct {
	RequestID       string          `json:"request_id"`
	WorkerID        string          `json:"worker_id"`
	AgentRun        map[string]any  `json:"agent_run"`
	ExecutorRequest ExecutorRequest `json:"executor_request"`
	RuntimeSnapshot map[string]any  `json:"runtime_snapshot,omitempty"`
}

type ExecutorRequest struct {
	TaskID            string         `json:"agent_run_id"`
	AgentID           string         `json:"agent_id"`
	Executor          string         `json:"executor"`
	Prompt            string         `json:"prompt"`
	ExecutionLocation string         `json:"execution_location,omitempty"`
	IssueID           string         `json:"issue_id,omitempty"`
	RuntimeProfileID  string         `json:"runtime_profile_id,omitempty"`
	Workdir           string         `json:"workdir,omitempty"`
	Branch            string         `json:"branch,omitempty"`
	Model             string         `json:"model,omitempty"`
	ExecutorSessionID string         `json:"executor_session_id,omitempty"`
	Metadata          map[string]any `json:"metadata,omitempty"`
}

type AgentRunEventReport struct {
	PeerToken string         `json:"peer_token"`
	RequestID string         `json:"request_id,omitempty"`
	TaskID    string         `json:"agent_run_id"`
	WorkerID  string         `json:"worker_id,omitempty"`
	Type      string         `json:"type"`
	Text      string         `json:"text,omitempty"`
	Data      map[string]any `json:"data,omitempty"`
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

type AgentRunHeartbeatRequest struct {
	PeerToken string `json:"peer_token"`
	RequestID string `json:"request_id"`
	TaskID    string `json:"agent_run_id"`
	WorkerID  string `json:"worker_id"`
	LeaseSec  int    `json:"lease_sec,omitempty"`
}

type AgentRunHeartbeatResponse struct {
	OK              bool   `json:"ok"`
	CancelRequested bool   `json:"cancel_requested"`
	Reason          string `json:"reason,omitempty"`
	LeaseSec        int    `json:"lease_sec,omitempty"`
}

type AgentRunCompleteRequest struct {
	PeerToken string                `json:"peer_token"`
	RequestID string                `json:"request_id"`
	TaskID    string                `json:"agent_run_id"`
	WorkerID  string                `json:"worker_id,omitempty"`
	Status    string                `json:"status"`
	Output    string                `json:"output,omitempty"`
	Error     string                `json:"error,omitempty"`
	SessionID string                `json:"executor_session_id,omitempty"`
	Usage     map[string]any        `json:"usage,omitempty"`
	Artifacts []map[string]any      `json:"artifacts,omitempty"`
	Events    []AgentRunEventReport `json:"events,omitempty"`
}

type AgentRunCompleteResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

type AgentRunSessionPinRequest struct {
	PeerToken         string `json:"peer_token"`
	RequestID         string `json:"request_id"`
	TaskID            string `json:"agent_run_id"`
	WorkerID          string `json:"worker_id"`
	Workdir           string `json:"workdir,omitempty"`
	Branch            string `json:"branch,omitempty"`
	RepoURL           string `json:"repo_url,omitempty"`
	CachePath         string `json:"cache_path,omitempty"`
	ExecutorSessionID string `json:"executor_session_id,omitempty"`
}

type AgentRunSessionPinResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

type NoopEnvelope struct {
	Type    string         `json:"type"`
	Payload map[string]any `json:"payload"`
}

// DecodeExecToolRequest converts a map payload to ExecToolRequest via JSON roundtrip.
// The marshal-unmarshal pattern keeps map-to-struct conversion consistent.
func DecodeExecToolRequest(payload map[string]any) (ExecToolRequest, error) {
	var req ExecToolRequest
	buf, err := json.Marshal(payload)
	if err != nil {
		return req, err
	}
	err = json.Unmarshal(buf, &req)
	return req, err
}

func DecodeToolPreviewRequest(payload map[string]any) (ToolPreviewRequest, error) {
	var req ToolPreviewRequest
	buf, err := json.Marshal(payload)
	if err != nil {
		return req, err
	}
	err = json.Unmarshal(buf, &req)
	return req, err
}

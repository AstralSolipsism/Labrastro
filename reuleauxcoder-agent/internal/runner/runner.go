package runner

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/agentruntime"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/client"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/mcp"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/tools"
	"github.com/charmbracelet/glamour"
)

type Config struct {
	Host            string
	BootstrapToken  string
	CWD             string
	WorkspaceRoot   string
	PeerInfoFile    string
	PollInterval    time.Duration
	Interactive     bool
	AgentRun        bool
	WorkerSessionID string
}

type Runner struct {
	cfg      Config
	client   *client.HTTPClient
	scanner  *bufio.Scanner
	mdRender *glamour.TermRenderer
	mcp      *mcp.Supervisor
	activeMu sync.Mutex
	active   map[string]context.CancelFunc
}

func New(cfg Config) *Runner {
	renderer, err := glamour.NewTermRenderer(
		glamour.WithAutoStyle(),
		glamour.WithWordWrap(100),
	)
	if err != nil {
		log.Printf("markdown renderer init failed: %v", err)
	}
	return &Runner{
		cfg:      cfg,
		client:   client.New(cfg.Host),
		scanner:  bufio.NewScanner(os.Stdin),
		mdRender: renderer,
		active:   map[string]context.CancelFunc{},
	}
}

func (r *Runner) Run(ctx context.Context) error {
	cwd := r.cfg.CWD
	if cwd == "" {
		resolved, err := os.Getwd()
		if err != nil {
			return err
		}
		cwd = resolved
	}
	workspaceRoot := r.cfg.WorkspaceRoot
	if workspaceRoot == "" {
		workspaceRoot = cwd
	}

	features := []string{"shell", "read_file", "write_file", "edit_file", "glob", "grep", "list_file", "tool_preview"}
	hostInfo := map[string]any{
		"os":       runtimeOS(),
		"arch":     runtimeArch(),
		"hostname": runtimeHostname(),
		"shell":    runtimeShell(),
	}
	if r.cfg.AgentRun {
		features = append(
			features,
			"agent_runs",
			"agent_runs.local_workspace",
			"agent_runs.daemon_worktree",
			"agent_runs.remote_server",
		)
		hostInfo["agent_runs"] = map[string]any{
			"executors":           runtimeExecutors(),
			"execution_locations": runtimeExecutionLocations(),
			"workspace_root":      workspaceRoot,
			"runtime_root":        filepath.Join(workspaceRoot, ".rcoder", "agent-runs"),
			"executor_features":   runtimeExecutorFeatures(),
		}
	}
	registerResp, err := r.client.Register(ctx, protocol.RegisterRequest{
		BootstrapToken: r.cfg.BootstrapToken,
		CWD:            cwd,
		WorkspaceRoot:  workspaceRoot,
		Features:       features,
		HostInfoMin:    hostInfo,
	})
	if err != nil {
		return fmt.Errorf("register failed: %w", err)
	}
	if r.cfg.PeerInfoFile != "" {
		if err := writePeerInfoFile(r.cfg.PeerInfoFile, registerResp); err != nil {
			return fmt.Errorf("write peer info failed: %w", err)
		}
	}
	log.Printf("registered peer_id=%s", registerResp.PeerID)
	fmt.Printf("\n=== REMOTE PEER CONNECTED ===\nPeer: %s\nWorkspace: %s\nHost: %s\n============================\n\n", registerResp.PeerID, workspaceRoot, r.cfg.Host)

	heartbeatInterval := time.Duration(registerResp.HeartbeatIntervalSec) * time.Second
	if heartbeatInterval <= 0 {
		heartbeatInterval = 10 * time.Second
	}
	pollInterval := r.cfg.PollInterval
	if pollInterval <= 0 {
		pollInterval = 500 * time.Millisecond
	}

	childCtx, cancel := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer cancel()
	defer func() {
		if r.mcp != nil {
			r.mcp.Stop()
		}
		disconnectCtx, cancelDisconnect := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancelDisconnect()
		_ = r.client.Disconnect(disconnectCtx, protocol.DisconnectRequest{
			PeerToken: registerResp.PeerToken,
			Reason:    "peer_shutdown",
		})
	}()

	go r.heartbeatLoop(childCtx, registerResp.PeerToken, heartbeatInterval)
	if r.cfg.AgentRun {
		return r.runAgentRunLoop(childCtx, registerResp.PeerToken, pollInterval, workspaceRoot)
	}

	r.mcp = mcp.NewSupervisor(r.client, registerResp.PeerToken, workspaceRoot)
	r.mcp.Start(childCtx)

	if r.cfg.Interactive {
		errCh := make(chan error, 1)
		go func() {
			errCh <- r.runPollLoop(childCtx, registerResp.PeerToken, cwd, workspaceRoot, pollInterval)
		}()

		if err := r.runInteractiveLoop(childCtx, registerResp.PeerToken); err != nil {
			return err
		}
		cancel()
		select {
		case err := <-errCh:
			if err != nil && childCtx.Err() == nil {
				return err
			}
		default:
		}
		return nil
	}

	return r.runPollLoop(childCtx, registerResp.PeerToken, cwd, workspaceRoot, pollInterval)
}

func writePeerInfoFile(path string, resp protocol.RegisterResponse) error {
	payload, err := json.MarshalIndent(map[string]any{
		"peer_id":                resp.PeerID,
		"peer_token":             resp.PeerToken,
		"heartbeat_interval_sec": resp.HeartbeatIntervalSec,
	}, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, payload, 0o600)
}

func (r *Runner) runAgentRunLoop(ctx context.Context, peerToken string, pollInterval time.Duration, workspaceRoot string) error {
	workerID := r.cfg.WorkerSessionID
	if strings.TrimSpace(workerID) == "" {
		workerID = "peer-runtime"
	}
	backend := agentruntime.SubprocessBackend{}
	runtimeRoot := filepath.Join(workspaceRoot, ".rcoder", "agent-runs")
	workspaceID := runtimeWorkspaceID(workspaceRoot)
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		claimCtx, cancelClaim := context.WithTimeout(ctx, 30*time.Second)
		claimResp, err := r.client.ClaimAgentRun(claimCtx, protocol.AgentRunClaimRequest{
			PeerToken: peerToken,
			WorkerID:  workerID,
			Executors: runtimeExecutors(),
			WaitSec:   20,
		})
		cancelClaim()
		if err != nil {
			return fmt.Errorf("agent runtime claim failed: %w", err)
		}
		if claimResp.Claim == nil {
			time.Sleep(pollInterval)
			continue
		}

		claim := claimResp.Claim
		req := runtimeRunRequest(claim.ExecutorRequest)
		taskCtx, taskCancel := context.WithCancel(ctx)
		r.registerActiveRequest(claim.RequestID, taskCancel)
		go r.runtimeHeartbeatLoop(taskCtx, peerToken, workerID, claim.RequestID, req.TaskID, taskCancel)
		var result agentruntime.RunResult
		var runErr error
		manager, resolved, resolveErr := agentruntime.ResolveRunWithExecEnv(req, claim.RuntimeSnapshot, runtimeRoot, workspaceID)
		if resolveErr == nil {
			req = resolved.Request
			resolved, resolveErr = r.prepareAgentRunRun(
				taskCtx,
				peerToken,
				claim.RequestID,
				workerID,
				workspaceRoot,
				runtimeRoot,
				manager,
				resolved,
			)
			req = resolved.Request
		}
		if resolveErr == nil {
			if err := r.sendRuntimeEvent(context.Background(), peerToken, claim.RequestID, workerID, req.TaskID, agentruntime.Event{
				Type: agentruntime.EventStatus,
				Data: map[string]any{
					"status":     "running",
					"request_id": claim.RequestID,
					"workdir":    req.Workdir,
					"branch":     req.Branch,
				},
			}); err != nil {
				log.Printf("agent runtime start event failed: %v", err)
			}
		}
		if resolveErr != nil {
			runErr = resolveErr
			status := "failed"
			var blocked blockedRunError
			if errors.As(resolveErr, &blocked) {
				status = "blocked"
			}
			event := agentruntime.Event{
				Type: agentruntime.EventStatus,
				Data: map[string]any{
					"status": status,
					"error":  resolveErr.Error(),
				},
			}
			result = agentruntime.RunResult{
				TaskID: req.TaskID,
				Status: status,
				Error:  resolveErr.Error(),
				Events: []agentruntime.Event{event},
			}
		} else {
			session, startErr := backend.Start(taskCtx, resolved.Request, resolved.Options)
			if startErr != nil {
				runErr = startErr
				result = agentruntime.RunResult{TaskID: req.TaskID, Status: "failed", Error: startErr.Error()}
			} else {
				streamedEvents := []agentruntime.Event{}
				pinnedExecutorSessionID := ""
				for event := range session.Events {
					streamedEvents = append(streamedEvents, event)
					if threadID := eventThreadID(event); threadID != "" && threadID != pinnedExecutorSessionID {
						pinnedExecutorSessionID = threadID
						if err := r.pinRuntimeSession(context.Background(), peerToken, claim.RequestID, workerID, req.TaskID, "", "", "", "", threadID); err != nil {
							log.Printf("agent runtime executor session pin failed: %v", err)
						}
					}
					if err := r.sendRuntimeEvent(context.Background(), peerToken, claim.RequestID, workerID, req.TaskID, event); err != nil {
						log.Printf("agent runtime event failed: %v", err)
					}
				}
				select {
				case result = <-session.Result:
				default:
					result = agentruntime.RunResult{TaskID: req.TaskID, Status: "failed", Error: "executor session ended without result"}
				}
				if len(result.Events) == 0 && len(streamedEvents) > 0 {
					result.Events = streamedEvents
				}
			}
		}
		publishArtifacts := []map[string]any{}
		if taskCtx.Err() == nil && result.Status == "completed" && runtimeNeedsWorktree(req.ExecutionLocation) {
			publish := agentruntime.PublishWorktree(taskCtx, req, agentruntime.PublishOptions{
				EventSink: func(event agentruntime.Event) {
					if err := r.sendRuntimeEvent(context.Background(), peerToken, claim.RequestID, workerID, req.TaskID, event); err != nil {
						log.Printf("agent runtime publish event failed: %v", err)
					}
				},
			})
			publishArtifacts = append(publishArtifacts, publish.Artifacts...)
			result.Events = append(result.Events, publish.Events...)
		}
		cancelledBeforeStop := taskCtx.Err() == context.Canceled
		taskCancel()
		r.unregisterActiveRequest(claim.RequestID)
		if cancelledBeforeStop && result.Status != "timeout" {
			result.Status = "cancelled"
			if result.Error == "" {
				result.Error = "execution cancelled"
			}
		}
		completeReq := protocol.AgentRunCompleteRequest{
			PeerToken: peerToken,
			RequestID: claim.RequestID,
			TaskID:    req.TaskID,
			WorkerID:  workerID,
			Status:    result.Status,
			Output:    result.Output,
			Error:     result.Error,
			SessionID: result.ExecutorSessionID,
			Usage:     runtimeUsage(result.Usage),
			Artifacts: publishArtifacts,
			Events:    runtimeEvents(peerToken, claim.RequestID, workerID, req.TaskID, result.Events),
		}
		if runErr != nil && completeReq.Error == "" {
			completeReq.Error = runErr.Error()
			if completeReq.Status == "" {
				completeReq.Status = "failed"
			}
		}
		completeCtx, cancelComplete := context.WithTimeout(context.Background(), 30*time.Second)
		_, completeErr := r.client.CompleteAgentRun(completeCtx, completeReq)
		cancelComplete()
		if completeErr != nil {
			return fmt.Errorf("agent runtime complete failed: %w", completeErr)
		}
	}
}

func (r *Runner) runtimeHeartbeatLoop(ctx context.Context, peerToken, workerID, requestID, taskID string, cancel context.CancelFunc) {
	send := func() bool {
		heartbeatCtx, cancelHeartbeat := context.WithTimeout(context.Background(), 10*time.Second)
		resp, err := r.client.AgentRunHeartbeat(heartbeatCtx, protocol.AgentRunHeartbeatRequest{
			PeerToken: peerToken,
			RequestID: requestID,
			TaskID:    taskID,
			WorkerID:  workerID,
			LeaseSec:  15,
		})
		cancelHeartbeat()
		if err != nil {
			log.Printf("agent runtime heartbeat failed: %v", err)
			return false
		}
		if !resp.OK {
			log.Printf("AgentRun heartbeat rejected agent_run_id=%s reason=%s", taskID, resp.Reason)
			cancel()
			return true
		}
		if resp.CancelRequested {
			log.Printf("AgentRun cancellation requested agent_run_id=%s reason=%s", taskID, resp.Reason)
			cancel()
			return true
		}
		return false
	}
	if send() {
		return
	}
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if send() {
				return
			}
		}
	}
}

type blockedRunError struct {
	reason string
}

func (e blockedRunError) Error() string {
	return e.reason
}

func (r *Runner) prepareAgentRunRun(
	ctx context.Context,
	peerToken string,
	requestID string,
	workerID string,
	workspaceRoot string,
	runtimeRoot string,
	manager *agentruntime.ExecEnvManager,
	resolved agentruntime.ResolvedRun,
) (agentruntime.ResolvedRun, error) {
	req := resolved.Request
	if runtimeNeedsWorktree(req.ExecutionLocation) {
		if err := r.sendRuntimeEvent(context.Background(), peerToken, requestID, workerID, req.TaskID, agentruntime.Event{
			Type: agentruntime.EventStatus,
			Data: map[string]any{"status": "preparing_worktree"},
		}); err != nil {
			log.Printf("agent runtime preparing event failed: %v", err)
		}
		repoURL, err := r.resolveRepoURL(ctx, req, workspaceRoot)
		if err != nil {
			return resolved, err
		}
		repoWorkspaceID := metadataString(req.Metadata, "workspace_id")
		if repoWorkspaceID == "" {
			repoWorkspaceID = runtimeWorkspaceID(workspaceRoot)
		}
		cache, err := agentruntime.NewRepoCache(filepath.Join(runtimeRoot, "repos"))
		if err != nil {
			return resolved, err
		}
		worktree, err := cache.CreateWorktree(ctx, agentruntime.WorktreeParams{
			WorkspaceID: repoWorkspaceID,
			RepoURL:     repoURL,
			WorkDir:     resolved.Plan.WorkDir,
			AgentName:   req.AgentID,
			TaskID:      req.TaskID,
		})
		if err != nil {
			return resolved, err
		}
		resolved.Request.Workdir = worktree.Path
		resolved.Request.Branch = worktree.BranchName
		prepared, err := agentruntime.PrepareResolvedRun(manager, resolved, agentruntime.PromptFilesFromMetadata(resolved.Request.Metadata))
		if err != nil {
			return resolved, err
		}
		if err := r.pinRuntimeSession(context.Background(), peerToken, requestID, workerID, req.TaskID, worktree.Path, worktree.BranchName, worktree.RepoURL, worktree.CachePath, ""); err != nil {
			log.Printf("agent runtime worktree session pin failed: %v", err)
		}
		if err := r.sendRuntimeEvent(context.Background(), peerToken, requestID, workerID, req.TaskID, agentruntime.Event{
			Type: agentruntime.EventStatus,
			Data: map[string]any{
				"status":     "worktree_ready",
				"workdir":    worktree.Path,
				"branch":     worktree.BranchName,
				"repo_url":   worktree.RepoURL,
				"cache_path": worktree.CachePath,
			},
		}); err != nil {
			log.Printf("agent runtime worktree event failed: %v", err)
		}
		return prepared, nil
	}
	return agentruntime.PrepareResolvedRun(manager, resolved, agentruntime.PromptFilesFromMetadata(resolved.Request.Metadata))
}

func runtimeNeedsWorktree(location string) bool {
	return strings.EqualFold(location, "daemon_worktree") || strings.EqualFold(location, "remote_server")
}

func (r *Runner) resolveRepoURL(ctx context.Context, req agentruntime.RunRequest, workspaceRoot string) (string, error) {
	if repoURL := metadataString(req.Metadata, "repo_url"); repoURL != "" {
		return repoURL, nil
	}
	if root := metadataString(req.Metadata, "workspace_root"); root != "" {
		if repoURL, err := gitOriginURL(ctx, root); err == nil && strings.TrimSpace(repoURL) != "" {
			return strings.TrimSpace(repoURL), nil
		}
	}
	if repoURL, err := gitOriginURL(ctx, workspaceRoot); err == nil && strings.TrimSpace(repoURL) != "" {
		return strings.TrimSpace(repoURL), nil
	}
	return "", blockedRunError{reason: "repo_url missing and git origin could not be inferred from workspace_root"}
}

func gitOriginURL(ctx context.Context, workspaceRoot string) (string, error) {
	if strings.TrimSpace(workspaceRoot) == "" {
		return "", fmt.Errorf("workspace_root is empty")
	}
	cmd := exec.CommandContext(ctx, "git", "-C", workspaceRoot, "remote", "get-url", "origin")
	cmd.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0")
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("git remote get-url origin failed: %w: %s", err, strings.TrimSpace(string(out)))
	}
	return strings.TrimSpace(string(out)), nil
}

func metadataString(metadata map[string]any, key string) string {
	if len(metadata) == 0 {
		return ""
	}
	if value, ok := metadata[key]; ok && value != nil {
		return strings.TrimSpace(fmt.Sprint(value))
	}
	return ""
}

func eventThreadID(event agentruntime.Event) string {
	if event.Data == nil {
		return ""
	}
	for _, key := range []string{"thread_id", "threadId", "executor_session_id"} {
		if value, ok := event.Data[key]; ok && value != nil {
			if text := strings.TrimSpace(fmt.Sprint(value)); text != "" {
				return text
			}
		}
	}
	return ""
}

func (r *Runner) pinRuntimeSession(ctx context.Context, peerToken, requestID, workerID, taskID, workdir, branch, repoURL, cachePath, executorSessionID string) error {
	sessionCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	_, err := r.client.PinAgentRunSession(sessionCtx, protocol.AgentRunSessionPinRequest{
		PeerToken:         peerToken,
		RequestID:         requestID,
		TaskID:            taskID,
		WorkerID:          workerID,
		Workdir:           workdir,
		Branch:            branch,
		RepoURL:           repoURL,
		CachePath:         cachePath,
		ExecutorSessionID: executorSessionID,
	})
	return err
}

func (r *Runner) sendRuntimeEvent(ctx context.Context, peerToken, requestID, workerID, taskID string, event agentruntime.Event) error {
	eventCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	return r.client.SendAgentRunEvent(eventCtx, protocol.AgentRunEventReport{
		PeerToken: peerToken,
		RequestID: requestID,
		TaskID:    taskID,
		WorkerID:  workerID,
		Type:      string(event.Type),
		Text:      event.Text,
		Data:      event.Data,
	})
}

func runtimeRunRequest(req protocol.ExecutorRequest) agentruntime.RunRequest {
	return agentruntime.RunRequest{
		TaskID:            req.TaskID,
		AgentID:           req.AgentID,
		Executor:          req.Executor,
		Prompt:            req.Prompt,
		ExecutionLocation: req.ExecutionLocation,
		IssueID:           req.IssueID,
		RuntimeProfileID:  req.RuntimeProfileID,
		Workdir:           req.Workdir,
		Branch:            req.Branch,
		Model:             req.Model,
		ExecutorSessionID: req.ExecutorSessionID,
		Metadata:          req.Metadata,
	}
}

func runtimeEvents(peerToken, requestID, workerID, taskID string, events []agentruntime.Event) []protocol.AgentRunEventReport {
	reports := make([]protocol.AgentRunEventReport, 0, len(events))
	for _, event := range events {
		reports = append(reports, protocol.AgentRunEventReport{
			PeerToken: peerToken,
			RequestID: requestID,
			TaskID:    taskID,
			WorkerID:  workerID,
			Type:      string(event.Type),
			Text:      event.Text,
			Data:      event.Data,
		})
	}
	return reports
}

func runtimeUsage(usage map[string]agentruntime.TokenUsage) map[string]any {
	if len(usage) == 0 {
		return nil
	}
	out := make(map[string]any, len(usage))
	for key, val := range usage {
		out[key] = val
	}
	return out
}

func (r *Runner) runPollLoop(ctx context.Context, peerToken, cwd, workspaceRoot string, pollInterval time.Duration) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		pollCtx, cancelPoll := context.WithTimeout(ctx, 30*time.Second)
		env, err := r.client.Poll(pollCtx, protocol.PollRequest{PeerToken: peerToken})
		cancelPoll()
		if err != nil {
			return fmt.Errorf("poll failed: %w", err)
		}

		switch env.Type {
		case "noop", "":
			time.Sleep(pollInterval)
			continue
		case "exec_tool":
			execReq, err := protocol.DecodeExecToolRequest(env.Payload)
			if err != nil {
				if sendErr := r.sendToolResult(ctx, peerToken, env.RequestID, protocol.ExecToolResult{
					OK:           false,
					ErrorCode:    "REMOTE_TOOL_ERROR",
					ErrorMessage: err.Error(),
				}); sendErr != nil {
					return sendErr
				}
				continue
			}
			if result, invalid := execToolProtocolError(execReq); invalid {
				if sendErr := r.sendToolResult(ctx, peerToken, env.RequestID, result); sendErr != nil {
					return sendErr
				}
				continue
			}
			execCtx, cancelExec := context.WithCancel(ctx)
			r.registerActiveRequest(env.RequestID, cancelExec)
			go func(requestID string, execReq protocol.ExecToolRequest) {
				defer r.unregisterActiveRequest(requestID)
				var result protocol.ExecToolResult
				if execReq.ToolName == "mcp" {
					if r.mcp == nil {
						result = protocol.ExecToolResult{OK: false, ErrorCode: "REMOTE_MCP_ERROR", ErrorMessage: "MCP supervisor is not running"}
					} else {
						result = r.mcp.Execute(execReq.Args)
					}
				} else {
					result = tools.ExecuteWithContext(execCtx, execReq, cwd, workspaceRoot, func(chunk protocol.ToolStreamChunk) {
						chunk = attachToolCallIDToStreamChunk(chunk, execReq.ToolCallID)
						if sendErr := r.sendToolStream(context.Background(), peerToken, requestID, chunk); sendErr != nil {
							log.Printf("stream send failed: %v", sendErr)
						}
					})
				}
				if result.Meta == nil {
					result.Meta = map[string]any{}
				}
				result.Meta["tool_call_id"] = execReq.ToolCallID
				if sendErr := r.sendToolResult(context.Background(), peerToken, requestID, result); sendErr != nil {
					log.Printf("tool result send failed: %v", sendErr)
				}
			}(env.RequestID, execReq)
		case "cancel_tool":
			requestID := env.RequestID
			if requestID == "" {
				if payloadRequestID, _ := env.Payload["request_id"].(string); payloadRequestID != "" {
					requestID = payloadRequestID
				}
			}
			if requestID != "" {
				r.cancelActiveRequest(requestID)
			}
		case "preview_tool":
			previewReq, err := protocol.DecodeToolPreviewRequest(env.Payload)
			if err != nil {
				if sendErr := r.sendToolPreviewResult(ctx, peerToken, env.RequestID, protocol.ToolPreviewResult{
					OK:           false,
					ErrorCode:    "REMOTE_TOOL_ERROR",
					ErrorMessage: err.Error(),
				}); sendErr != nil {
					return sendErr
				}
				continue
			}
			result := tools.Preview(previewReq, cwd)
			if sendErr := r.sendToolPreviewResult(ctx, peerToken, env.RequestID, result); sendErr != nil {
				return sendErr
			}
		case "cleanup":
			cleanup := protocol.CleanupResult{OK: true, RemovedItems: []string{}}
			if err := r.sendCleanupResult(ctx, peerToken, env.RequestID, cleanup); err != nil {
				return err
			}
		default:
			log.Printf("ignoring unsupported envelope type=%s", env.Type)
			time.Sleep(pollInterval)
		}
	}
}

func execToolProtocolError(req protocol.ExecToolRequest) (protocol.ExecToolResult, bool) {
	if strings.TrimSpace(req.ToolCallID) != "" {
		return protocol.ExecToolResult{}, false
	}
	return protocol.ExecToolResult{
		OK:           false,
		ErrorCode:    "REMOTE_PROTOCOL_ERROR",
		ErrorMessage: "exec_tool request missing tool_call_id",
	}, true
}

func attachToolCallIDToStreamChunk(chunk protocol.ToolStreamChunk, toolCallID string) protocol.ToolStreamChunk {
	chunk.ToolCallID = toolCallID
	if chunk.Meta == nil {
		chunk.Meta = map[string]any{}
	}
	chunk.Meta["tool_call_id"] = toolCallID
	return chunk
}

func (r *Runner) registerActiveRequest(requestID string, cancel context.CancelFunc) {
	if requestID == "" {
		return
	}
	r.activeMu.Lock()
	r.active[requestID] = cancel
	r.activeMu.Unlock()
}

func (r *Runner) unregisterActiveRequest(requestID string) {
	if requestID == "" {
		return
	}
	r.activeMu.Lock()
	delete(r.active, requestID)
	r.activeMu.Unlock()
}

func (r *Runner) cancelActiveRequest(requestID string) {
	r.activeMu.Lock()
	cancel := r.active[requestID]
	r.activeMu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (r *Runner) runInteractiveLoop(ctx context.Context, peerToken string) error {
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		fmt.Print("You > ")
		if !r.scanner.Scan() {
			if err := r.scanner.Err(); err != nil {
				return err
			}
			return nil
		}
		userInput := strings.TrimSpace(r.scanner.Text())
		if userInput == "" {
			continue
		}
		if userInput == "/quit" || userInput == "/exit" {
			return nil
		}
		if err := r.runRemoteChat(ctx, peerToken, userInput); err != nil {
			return err
		}
	}
}

func (r *Runner) runRemoteChat(ctx context.Context, peerToken, prompt string) error {
	chatCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	startResp, err := r.client.ChatStart(chatCtx, protocol.ChatStartRequest{
		PeerToken: peerToken,
		Prompt:    prompt,
	})
	cancel()
	if err != nil {
		return fmt.Errorf("chat start failed: %w", err)
	}
	if strings.TrimSpace(startResp.Error) != "" {
		return fmt.Errorf("chat start failed: %s", startResp.Error)
	}
	if strings.TrimSpace(startResp.ChatID) == "" {
		return fmt.Errorf("chat start failed: empty chat id")
	}

	cursor := 0
	for {
		streamCtx, cancel := context.WithTimeout(ctx, 35*time.Second)
		streamResp, err := r.client.ChatStream(streamCtx, protocol.ChatStreamRequest{
			PeerToken:  peerToken,
			ChatID:     startResp.ChatID,
			Cursor:     cursor,
			TimeoutSec: 30,
		})
		cancel()
		if err != nil {
			return fmt.Errorf("chat stream failed: %w", err)
		}
		if strings.TrimSpace(streamResp.Error) != "" {
			return fmt.Errorf("chat stream failed: %s", streamResp.Error)
		}
		for _, event := range streamResp.Events {
			if err := r.handleChatEvent(ctx, peerToken, startResp.ChatID, event); err != nil {
				return err
			}
		}
		cursor = streamResp.NextCursor
		if streamResp.Done {
			return nil
		}
	}
}

func (r *Runner) handleChatEvent(ctx context.Context, peerToken, chatID string, event protocol.ChatEvent) error {
	switch event.Type {
	case "chat_start":
		return nil
	case "output":
		r.renderOutputEvent(event.Payload)
	case "tool_call_stream":
		r.renderToolStream(event.Payload)
	case "approval_request":
		return r.handleApprovalRequest(ctx, peerToken, chatID, event.Payload)
	case "approval_resolved":
		return nil
	case "tool_call_start":
		if name, _ := event.Payload["tool_name"].(string); name != "" {
			fmt.Printf("\n[tool] %s\n", name)
		}
	case "tool_call_end":
		return nil
	case "chat_end":
		if response, _ := event.Payload["response"].(string); strings.TrimSpace(response) != "" {
			fmt.Println()
		}
	case "error":
		msg, _ := event.Payload["message"].(string)
		if msg == "" {
			msg = "unknown error"
		}
		fmt.Fprintf(os.Stderr, "\nError: %s\n", msg)
	}
	return nil
}

func (r *Runner) handleApprovalRequest(ctx context.Context, peerToken, chatID string, payload map[string]any) error {
	r.renderOutputEvent(payload)

	approvalID, _ := payload["approval_id"].(string)
	if approvalID == "" {
		return fmt.Errorf("approval request missing approval_id")
	}

	fmt.Print("Approve? [y/N]: ")
	decision := "deny_once"
	if r.scanner.Scan() {
		answer := strings.ToLower(strings.TrimSpace(r.scanner.Text()))
		if answer == "y" || answer == "yes" || answer == "a" || answer == "allow" {
			decision = "allow_once"
		}
	} else if err := r.scanner.Err(); err != nil {
		return err
	}

	replyCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	replyResp, err := r.client.ApprovalReply(replyCtx, protocol.ApprovalReplyRequest{
		PeerToken:  peerToken,
		ChatID:     chatID,
		ApprovalID: approvalID,
		Decision:   decision,
	})
	if err != nil {
		return fmt.Errorf("approval reply failed: %w", err)
	}
	if !replyResp.OK {
		return fmt.Errorf("approval reply failed: %s", replyResp.Error)
	}
	return nil
}

func (r *Runner) renderOutputEvent(payload map[string]any) {
	format, _ := payload["format"].(string)
	content, _ := payload["content"].(string)
	if content == "" {
		return
	}

	switch format {
	case "markdown":
		if r.mdRender != nil {
			rendered, err := r.mdRender.Render(content)
			if err == nil {
				fmt.Print(rendered)
				return
			}
		}
		fmt.Print(content)
	case "plain", "terminal", "":
		fmt.Print(content)
	default:
		fmt.Print(content)
	}

	if newline, ok := payload["newline"].(bool); ok && newline {
		fmt.Print("\n")
	}
}

func (r *Runner) renderToolStream(payload map[string]any) {
	content, _ := payload["content"].(string)
	if content == "" {
		return
	}
	stream, _ := payload["stream"].(string)
	if stream == "stderr" {
		fmt.Fprint(os.Stderr, content)
		return
	}
	fmt.Print(content)
}

func (r *Runner) heartbeatLoop(ctx context.Context, peerToken string, interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			hbCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
			err := r.client.Heartbeat(hbCtx, protocol.Heartbeat{
				PeerToken: peerToken,
				TS:        float64(time.Now().UnixNano()) / 1e9,
			})
			cancel()
			if err != nil {
				log.Printf("heartbeat failed: %v", err)
			}
		}
	}
}

func (r *Runner) sendToolResult(ctx context.Context, peerToken, requestID string, result protocol.ExecToolResult) error {
	sendCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	return r.client.SendResult(sendCtx, protocol.ResultRequest{
		PeerToken: peerToken,
		RequestID: requestID,
		Type:      "tool_result",
		Payload:   mapFromStruct(result),
	})
}

func (r *Runner) sendToolStream(ctx context.Context, peerToken, requestID string, chunk protocol.ToolStreamChunk) error {
	sendCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	return r.client.SendResult(sendCtx, protocol.ResultRequest{
		PeerToken: peerToken,
		RequestID: requestID,
		Type:      "tool_stream",
		Payload:   mapFromStruct(chunk),
	})
}

func (r *Runner) sendToolPreviewResult(ctx context.Context, peerToken, requestID string, result protocol.ToolPreviewResult) error {
	sendCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	return r.client.SendResult(sendCtx, protocol.ResultRequest{
		PeerToken: peerToken,
		RequestID: requestID,
		Type:      "tool_preview_result",
		Payload:   mapFromStruct(result),
	})
}

func (r *Runner) sendCleanupResult(ctx context.Context, peerToken, requestID string, result protocol.CleanupResult) error {
	sendCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	return r.client.SendResult(sendCtx, protocol.ResultRequest{
		PeerToken: peerToken,
		RequestID: requestID,
		Type:      "cleanup_result",
		Payload:   mapFromStruct(result),
	})
}

// mapFromStruct converts a small control-plane struct to map[string]any via JSON roundtrip.
// This keeps field tags and omitempty behavior consistent with the wire protocol.
func mapFromStruct(v any) map[string]any {
	buf, err := json.Marshal(v)
	if err != nil {
		return map[string]any{}
	}
	out := map[string]any{}
	if err := json.Unmarshal(buf, &out); err != nil {
		return map[string]any{}
	}
	return out
}

func runtimeOS() string {
	return runtime.GOOS
}

func runtimeArch() string {
	return runtime.GOARCH
}

func runtimeHostname() string {
	hostname, err := os.Hostname()
	if err != nil {
		return ""
	}
	return hostname
}

func runtimeShell() string {
	if runtime.GOOS != "windows" {
		return "sh"
	}
	if _, err := exec.LookPath("bash"); err == nil {
		return "bash"
	}
	if _, err := exec.LookPath("pwsh"); err == nil {
		return "pwsh"
	}
	return "powershell.exe"
}

func runtimeExecutors() []string {
	return []string{"fake", "reuleauxcoder", "codex", "claude", "gemini"}
}

func runtimeExecutionLocations() []string {
	return []string{"local_workspace", "daemon_worktree", "remote_server"}
}

func runtimeExecutorFeatures() map[string]any {
	return map[string]any{
		"fake": map[string]any{
			"installed":              true,
			"version":                "builtin",
			"stream_json":            true,
			"session_discovery":      false,
			"resume_by_id":           false,
			"usage":                  false,
			"mcp_config":             false,
			"runtime_home_isolation": "none",
			"model_arg":              false,
			"limitations":            []string{"development executor only"},
		},
		"reuleauxcoder": commandExecutorFeature("rcoder", map[string]any{
			"stream_json":            false,
			"session_discovery":      true,
			"resume_by_id":           true,
			"usage":                  false,
			"mcp_config":             true,
			"runtime_home_isolation": "shared_or_entrypoint",
			"model_arg":              true,
			"limitations":            []string{"plain stdout compatibility backend"},
		}),
		"codex": commandExecutorFeature("codex", map[string]any{
			"stream_json":            true,
			"session_discovery":      true,
			"resume_by_id":           true,
			"usage":                  true,
			"mcp_config":             false,
			"runtime_home_isolation": "per_task",
			"model_arg":              true,
			"tested_version":         "0.100.0+",
			"limitations":            []string{"uses app-server jsonrpc_stdio transport"},
		}),
		"claude": commandExecutorFeature("claude", map[string]any{
			"stream_json":            true,
			"session_discovery":      true,
			"resume_by_id":           true,
			"usage":                  true,
			"mcp_config":             true,
			"runtime_home_isolation": "per_agent",
			"model_arg":              true,
			"tested_version":         "2.0.0+",
			"limitations":            []string{},
		}),
		"gemini": commandExecutorFeature("gemini", map[string]any{
			"stream_json":            true,
			"session_discovery":      true,
			"resume_by_id":           false,
			"usage":                  true,
			"mcp_config":             false,
			"runtime_home_isolation": "per_agent",
			"model_arg":              true,
			"limitations":            []string{"resume_by_id disabled until fixture verifies stable session id"},
		}),
	}
}

func commandExecutorFeature(command string, values map[string]any) map[string]any {
	out := map[string]any{}
	for key, value := range values {
		out[key] = value
	}
	_, err := exec.LookPath(command)
	out["installed"] = err == nil
	if err != nil {
		out["version"] = ""
		out["limitations"] = appendStringList(out["limitations"], "executable not found on PATH")
		return out
	}
	return out
}

func appendStringList(value any, item string) []string {
	out := []string{}
	if values, ok := value.([]string); ok {
		out = append(out, values...)
	}
	return append(out, item)
}

func runtimeWorkspaceID(workspaceRoot string) string {
	base := strings.TrimSpace(filepath.Base(filepath.Clean(workspaceRoot)))
	if base == "" || base == "." || base == string(filepath.Separator) {
		return "workspace"
	}
	return base
}

package runner

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
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

var (
	errExecutableNotFound       = errors.New("executable not found")
	lookPathExecutable          = exec.LookPath
	localActionRequestTimeout   = 30 * time.Second
	localActionRetryMinDelay    = 500 * time.Millisecond
	localActionRetryMaxDelay    = 10 * time.Second
	runtimeHeartbeatInterval    = 2 * time.Second
	runtimeSteerDeliveryTimeout = time.Second
)

type Config struct {
	Host            string
	BootstrapToken  string
	CWD             string
	WorkspaceRoot   string
	PeerInfoFile    string
	ClaimInterval   time.Duration
	Interactive     bool
	AgentRun        bool
	WorkerSessionID string
	WorkerKind      string
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

	var features []string
	if !r.cfg.AgentRun {
		features = baseFeatures(tools.LSPAvailable())
	}
	hostInfo := map[string]any{
		"os":       runtimeOS(),
		"arch":     runtimeArch(),
		"hostname": runtimeHostname(),
		"shell":    runtimeShell(),
	}
	if r.cfg.AgentRun {
		workerKind := runtimeWorkerKind(r.cfg.WorkerKind)
		locations := runtimeExecutionLocationsForWorker(workerKind)
		features = []string{"agent_runs", "worker_kind:" + workerKind}
		for _, location := range locations {
			features = append(features, "agent_runs."+location)
		}
		hostInfo["agent_runs"] = map[string]any{
			"executors":           runtimeExecutors(),
			"execution_locations": locations,
			"worker_kind":         workerKind,
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
	claimInterval := r.cfg.ClaimInterval
	if claimInterval <= 0 {
		claimInterval = 500 * time.Millisecond
	}

	childCtx, cancel := signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)
	defer cancel()
	defer func() {
		if r.mcp != nil {
			r.mcp.Stop()
		}
		tools.ShutdownLSP()
		disconnectCtx, cancelDisconnect := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancelDisconnect()
		_ = r.client.Disconnect(disconnectCtx, protocol.DisconnectRequest{
			PeerToken: registerResp.PeerToken,
			Reason:    "peer_shutdown",
		})
	}()

	go r.heartbeatLoop(childCtx, registerResp.PeerToken, heartbeatInterval)
	if r.cfg.AgentRun {
		return r.runAgentRunLoop(childCtx, registerResp.PeerToken, claimInterval, workspaceRoot)
	}

	r.mcp = mcp.NewSupervisor(r.client, registerResp.PeerToken, workspaceRoot)
	r.mcp.Start(childCtx)

	if r.cfg.Interactive {
		errCh := make(chan error, 1)
		go func() {
			errCh <- r.runLocalActionLoop(childCtx, registerResp.PeerToken, registerResp.PeerID, cwd, workspaceRoot, features, claimInterval)
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

	return r.runLocalActionLoop(childCtx, registerResp.PeerToken, registerResp.PeerID, cwd, workspaceRoot, features, claimInterval)
}

func baseFeatures(lspAvailable bool) []string {
	features := []string{"local_actions"}
	for _, actionKind := range []string{
		"shell",
		"read_file",
		"read_workspace_file",
		"apply_patch",
		"draft_document_commit",
		"glob",
		"grep",
		"list_file",
		"tool_preview",
		"mcp",
	} {
		features = append(features, actionKind, "local_action:"+actionKind)
	}
	if lspAvailable {
		features = append(features, "lsp", "local_action:lsp")
	}
	return features
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

func (r *Runner) runAgentRunLoop(ctx context.Context, peerToken string, claimInterval time.Duration, workspaceRoot string) error {
	workerID := r.cfg.WorkerSessionID
	if strings.TrimSpace(workerID) == "" {
		workerID = "peer-runtime"
	}
	workerKind := runtimeWorkerKind(r.cfg.WorkerKind)
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
		claimResp, err := r.client.ClaimAgentRunActivation(claimCtx, protocol.AgentRunActivationClaimRequest{
			PeerToken:  peerToken,
			WorkerID:   workerID,
			WorkerKind: workerKind,
			Executors:  runtimeExecutors(),
			WaitSec:    20,
		})
		cancelClaim()
		if err != nil {
			return fmt.Errorf("agent runtime claim failed: %w", err)
		}
		if claimResp.Claim == nil {
			time.Sleep(claimInterval)
			continue
		}

		claim := claimResp.Claim
		if strings.TrimSpace(claim.ActivationID) == "" {
			return fmt.Errorf("agent runtime claim missing activation_id")
		}
		req := runtimeRunRequest(claim.ExecutorRequest)
		taskCtx, taskCancel := context.WithCancel(ctx)
		r.registerActiveRequest(claim.RequestID, taskCancel)
		steers := make(chan agentruntime.Steer)
		go r.runtimeHeartbeatLoop(
			taskCtx,
			peerToken,
			workerID,
			claim.RequestID,
			claim.ActivationID,
			req.TaskID,
			taskCancel,
			func(deliveryCtx context.Context, steer protocol.ActivationSteer) error {
				return deliverActivationSteer(deliveryCtx, steers, steer)
			},
		)
		var result agentruntime.RunResult
		var runErr error
		eventsForComplete := []agentruntime.Event{}
		sendLiveRuntimeEvent := func(label string, event agentruntime.Event) {
			if !shouldForwardRuntimeEvent(event) {
				return
			}
			if err := r.sendRuntimeEvent(context.Background(), peerToken, claim.RequestID, claim.ActivationID, workerID, req.TaskID, event); err != nil {
				log.Printf("agent runtime %s event failed: %v", label, err)
				eventsForComplete = append(eventsForComplete, event)
			}
		}
		manager, resolved, resolveErr := agentruntime.ResolveRunWithExecEnv(req, claim.RuntimeSnapshot, runtimeRoot, workspaceID)
		if resolveErr == nil {
			req = resolved.Request
			resolved, resolveErr = r.prepareAgentRunRun(
				taskCtx,
				peerToken,
				claim.RequestID,
				claim.ActivationID,
				workerID,
				workspaceRoot,
				runtimeRoot,
				manager,
				resolved,
				sendLiveRuntimeEvent,
			)
			req = resolved.Request
		}
		if resolveErr == nil {
			if strings.EqualFold(resolved.Request.ModelRequestOrigin, "server") {
				resolved.Options.RemoteBaseURL = r.cfg.Host
				resolved.Options.PeerToken = peerToken
				resolved.Options.AgentRunRequestID = claim.RequestID
				resolved.Options.AgentRunActivationID = claim.ActivationID
				resolved.Options.AgentRunWorkerID = workerID
			}
			resolved.Options.Steers = steers
			sendLiveRuntimeEvent("start", agentruntime.Event{
				Type: agentruntime.EventStatus,
				Data: map[string]any{
					"status":     "running",
					"request_id": claim.RequestID,
					"workdir":    req.Workdir,
					"branch":     req.Branch,
				},
			})
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
			eventsForComplete = append(eventsForComplete, event)
			result = agentruntime.RunResult{
				TaskID: req.TaskID,
				Status: status,
				Error:  resolveErr.Error(),
			}
		} else {
			session, startErr := backend.Start(taskCtx, resolved.Request, resolved.Options)
			if startErr != nil {
				runErr = startErr
				result = agentruntime.RunResult{TaskID: req.TaskID, Status: "failed", Error: startErr.Error()}
			} else {
				pinnedExecutorSessionID := ""
				for event := range session.Events {
					if threadID := eventThreadID(event); threadID != "" && threadID != pinnedExecutorSessionID {
						pinnedExecutorSessionID = threadID
						if err := r.pinRuntimeSession(context.Background(), peerToken, claim.RequestID, claim.ActivationID, workerID, req.TaskID, "", "", "", "", threadID); err != nil {
							log.Printf("agent runtime executor session pin failed: %v", err)
						}
					}
					sendLiveRuntimeEvent("stream", event)
				}
				select {
				case result = <-session.Result:
				default:
					result = agentruntime.RunResult{TaskID: req.TaskID, Status: "failed", Error: "executor session ended without result"}
				}
			}
		}
		publishArtifacts := []map[string]any{}
		if taskCtx.Err() == nil && result.Status == "completed" && shouldPublishWorktree(req) {
			publish := agentruntime.PublishWorktree(taskCtx, req, agentruntime.PublishOptions{
				EventSink: func(event agentruntime.Event) {
					sendLiveRuntimeEvent("publish", event)
				},
			})
			publishArtifacts = append(publishArtifacts, publish.Artifacts...)
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
		result.Events = eventsForComplete
		completeReq := protocol.AgentRunActivationCompleteRequest{
			PeerToken:    peerToken,
			RequestID:    claim.RequestID,
			ActivationID: claim.ActivationID,
			TaskID:       req.TaskID,
			WorkerID:     workerID,
			Status:       result.Status,
			Output:       result.Output,
			Error:        result.Error,
			SessionID:    result.ExecutorSessionID,
			Usage:        runtimeUsage(result.Usage),
			Artifacts:    publishArtifacts,
			Events:       runtimeEvents(peerToken, claim.RequestID, claim.ActivationID, workerID, req.TaskID, result.Events),
		}
		if runErr != nil && completeReq.Error == "" {
			completeReq.Error = runErr.Error()
			if completeReq.Status == "" {
				completeReq.Status = "failed"
			}
		}
		completeCtx, cancelComplete := context.WithTimeout(context.Background(), 30*time.Second)
		_, completeErr := r.client.CompleteAgentRunActivation(completeCtx, completeReq)
		cancelComplete()
		if completeErr != nil {
			return fmt.Errorf("agent runtime complete failed: %w", completeErr)
		}
	}
}

type activationSteerDeliverer func(context.Context, protocol.ActivationSteer) error

func (r *Runner) runtimeHeartbeatLoop(
	ctx context.Context,
	peerToken,
	workerID,
	requestID,
	activationID,
	taskID string,
	cancel context.CancelFunc,
	deliver activationSteerDeliverer,
) {
	deliveredSteerIDs := []string{}
	deliveredSteers := map[string]struct{}{}
	pendingSteers := []protocol.ActivationSteer{}
	pendingSteerIDs := map[string]struct{}{}
	send := func() bool {
		deliveredSteerIDs = append(
			deliveredSteerIDs,
			deliverPendingActivationSteers(ctx, &pendingSteers, deliveredSteers, deliver)...,
		)
		pendingSteerIDs = activationSteerIDSet(pendingSteers)
		heartbeatCtx, cancelHeartbeat := context.WithTimeout(context.Background(), 10*time.Second)
		resp, err := r.client.AgentRunActivationHeartbeat(heartbeatCtx, protocol.AgentRunActivationHeartbeatRequest{
			PeerToken:         peerToken,
			RequestID:         requestID,
			ActivationID:      activationID,
			TaskID:            taskID,
			WorkerID:          workerID,
			LeaseSec:          15,
			DeliveredSteerIDs: append([]string(nil), deliveredSteerIDs...),
		})
		cancelHeartbeat()
		if err != nil {
			log.Printf("agent runtime heartbeat failed: %v", err)
			return false
		}
		if !resp.OK {
			log.Printf("AgentRun activation heartbeat rejected agent_run_id=%s activation_id=%s reason=%s", taskID, activationID, resp.Reason)
			cancel()
			return true
		}
		if resp.CancelRequested {
			log.Printf("AgentRun activation cancellation requested agent_run_id=%s activation_id=%s reason=%s", taskID, activationID, resp.Reason)
			cancel()
			return true
		}
		for _, steer := range resp.ActivationSteers {
			steerID := strings.TrimSpace(steer.ID)
			if steerID == "" {
				continue
			}
			if _, ok := deliveredSteers[steerID]; ok {
				continue
			}
			if _, ok := pendingSteerIDs[steerID]; ok {
				continue
			}
			pendingSteers = append(pendingSteers, steer)
			pendingSteerIDs[steerID] = struct{}{}
		}
		newDelivered := deliverPendingActivationSteers(ctx, &pendingSteers, deliveredSteers, deliver)
		pendingSteerIDs = activationSteerIDSet(pendingSteers)
		for _, steerID := range newDelivered {
			log.Printf("AgentRun activation steer delivered agent_run_id=%s activation_id=%s steer_id=%s", taskID, activationID, steerID)
		}
		deliveredSteerIDs = append(deliveredSteerIDs, newDelivered...)
		return false
	}
	if send() {
		return
	}
	ticker := time.NewTicker(runtimeHeartbeatInterval)
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

func activationSteerIDSet(steers []protocol.ActivationSteer) map[string]struct{} {
	result := make(map[string]struct{}, len(steers))
	for _, steer := range steers {
		steerID := strings.TrimSpace(steer.ID)
		if steerID != "" {
			result[steerID] = struct{}{}
		}
	}
	return result
}

func deliverPendingActivationSteers(
	ctx context.Context,
	pendingSteers *[]protocol.ActivationSteer,
	deliveredSteers map[string]struct{},
	deliver activationSteerDeliverer,
) []string {
	if deliver == nil || len(*pendingSteers) == 0 {
		return nil
	}
	deliveredIDs := []string{}
	remaining := make([]protocol.ActivationSteer, 0, len(*pendingSteers))
	for _, steer := range *pendingSteers {
		steerID := strings.TrimSpace(steer.ID)
		if steerID == "" {
			continue
		}
		if _, ok := deliveredSteers[steerID]; ok {
			continue
		}
		deliveryCtx, cancelDelivery := context.WithTimeout(ctx, runtimeSteerDeliveryTimeout)
		err := deliver(deliveryCtx, steer)
		cancelDelivery()
		if err != nil {
			remaining = append(remaining, steer)
			continue
		}
		deliveredSteers[steerID] = struct{}{}
		deliveredIDs = append(deliveredIDs, steerID)
	}
	*pendingSteers = remaining
	return deliveredIDs
}

func deliverActivationSteer(ctx context.Context, steers chan<- agentruntime.Steer, steer protocol.ActivationSteer) error {
	runtimeSteer := agentruntime.Steer{
		ID:           strings.TrimSpace(steer.ID),
		ActivationID: strings.TrimSpace(steer.ActivationID),
		Source:       strings.TrimSpace(steer.Source),
		Payload:      cloneProtocolMap(steer.Payload),
		Metadata:     cloneProtocolMap(steer.Metadata),
	}
	select {
	case steers <- runtimeSteer:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func cloneProtocolMap(value map[string]any) map[string]any {
	if len(value) == 0 {
		return nil
	}
	out := make(map[string]any, len(value))
	for key, item := range value {
		out[key] = item
	}
	return out
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
	activationID string,
	workerID string,
	workspaceRoot string,
	runtimeRoot string,
	manager *agentruntime.ExecEnvManager,
	resolved agentruntime.ResolvedRun,
	sendLiveRuntimeEvent func(string, agentruntime.Event),
) (agentruntime.ResolvedRun, error) {
	req := resolved.Request
	if runtimeNeedsWorktree(req.ExecutionLocation) {
		sendLiveRuntimeEvent("preparing", agentruntime.Event{
			Type: agentruntime.EventStatus,
			Data: map[string]any{"status": "preparing_worktree"},
		})
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
		if err := r.pinRuntimeSession(context.Background(), peerToken, requestID, activationID, workerID, req.TaskID, worktree.Path, worktree.BranchName, worktree.RepoURL, worktree.CachePath, ""); err != nil {
			log.Printf("agent runtime worktree session pin failed: %v", err)
		}
		sendLiveRuntimeEvent("worktree", agentruntime.Event{
			Type: agentruntime.EventStatus,
			Data: map[string]any{
				"status":     "worktree_ready",
				"workdir":    worktree.Path,
				"branch":     worktree.BranchName,
				"repo_url":   worktree.RepoURL,
				"cache_path": worktree.CachePath,
			},
		})
		return prepared, nil
	}
	return agentruntime.PrepareResolvedRun(manager, resolved, agentruntime.PromptFilesFromMetadata(resolved.Request.Metadata))
}

func runtimeNeedsWorktree(location string) bool {
	return strings.EqualFold(location, "daemon_worktree") || strings.EqualFold(location, "remote_server")
}

func publishPolicy(req agentruntime.RunRequest) string {
	policy := strings.TrimSpace(strings.ToLower(req.PublishPolicy))
	if policy == "" {
		policy = strings.TrimSpace(strings.ToLower(metadataString(req.Metadata, "publish_policy")))
	}
	if policy == "" {
		return "never"
	}
	return policy
}

func shouldPublishWorktree(req agentruntime.RunRequest) bool {
	if !runtimeNeedsWorktree(req.ExecutionLocation) {
		return false
	}
	if worktreeRole(req) != "target" {
		return false
	}
	switch publishPolicy(req) {
	case "branch", "pr":
		return true
	default:
		return false
	}
}

func worktreeRole(req agentruntime.RunRequest) string {
	role := strings.TrimSpace(strings.ToLower(req.WorktreeRole))
	if role == "" {
		role = strings.TrimSpace(strings.ToLower(metadataString(req.Metadata, "worktree_role")))
	}
	if role == "" {
		return "target"
	}
	return role
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

func shouldForwardRuntimeEvent(event agentruntime.Event) bool {
	if event.Type != agentruntime.EventStatus || event.Data == nil {
		return true
	}
	status := strings.TrimSpace(fmt.Sprint(event.Data["status"]))
	return !strings.EqualFold(status, "session_pinned")
}

func (r *Runner) pinRuntimeSession(ctx context.Context, peerToken, requestID, activationID, workerID, taskID, workdir, branch, repoURL, cachePath, executorSessionID string) error {
	sessionCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	_, err := r.client.PinAgentRunActivationSession(sessionCtx, protocol.AgentRunActivationSessionPinRequest{
		PeerToken:         peerToken,
		RequestID:         requestID,
		ActivationID:      activationID,
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

func (r *Runner) sendRuntimeEvent(ctx context.Context, peerToken, requestID, activationID, workerID, taskID string, event agentruntime.Event) error {
	eventCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	return r.client.SendAgentRunActivationEvent(eventCtx, protocol.AgentRunActivationEventReport{
		PeerToken:    peerToken,
		RequestID:    requestID,
		ActivationID: activationID,
		TaskID:       taskID,
		WorkerID:     workerID,
		Type:         string(event.Type),
		Text:         event.Text,
		Data:         event.Data,
	})
}

func runtimeRunRequest(req protocol.ExecutorRequest) agentruntime.RunRequest {
	return agentruntime.RunRequest{
		TaskID:             req.TaskID,
		AgentID:            req.AgentID,
		Executor:           req.Executor,
		Prompt:             req.Prompt,
		ExecutionLocation:  req.ExecutionLocation,
		RuntimeProfileID:   req.RuntimeProfileID,
		WorkerKind:         req.WorkerKind,
		ModelRequestOrigin: req.ModelRequestOrigin,
		WorktreeRole:       req.WorktreeRole,
		PublishPolicy:      req.PublishPolicy,
		Workdir:            req.Workdir,
		Branch:             req.Branch,
		Model:              req.Model,
		ExecutorSessionID:  req.ExecutorSessionID,
		Metadata:           req.Metadata,
	}
}

func runtimeEvents(peerToken, requestID, activationID, workerID, taskID string, events []agentruntime.Event) []protocol.AgentRunActivationEventReport {
	reports := make([]protocol.AgentRunActivationEventReport, 0, len(events))
	for _, event := range events {
		reports = append(reports, protocol.AgentRunActivationEventReport{
			PeerToken:    peerToken,
			RequestID:    requestID,
			ActivationID: activationID,
			TaskID:       taskID,
			WorkerID:     workerID,
			Type:         string(event.Type),
			Text:         event.Text,
			Data:         event.Data,
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

func (r *Runner) runLocalActionLoop(ctx context.Context, peerToken, peerID, cwd, workspaceRoot string, features []string, claimInterval time.Duration) error {
	retry := localActionRetryBackoff{}
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		claimCtx, cancelClaim := context.WithTimeout(ctx, localActionRequestTimeout)
		claimResp, err := r.client.ClaimLocalActions(claimCtx, protocol.LocalActionClaimRequest{
			PeerToken:     peerToken,
			PeerID:        peerID,
			WorkerKind:    "local_peer",
			Features:      features,
			WorkspaceRoot: workspaceRoot,
			MaxActions:    1,
		})
		cancelClaim()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			if isTransientLocalActionError(err) {
				attempt, delay := retry.recordFailure(claimInterval)
				log.Printf("local action claim transient error: %v (attempt=%d retry_in=%s)", err, attempt, delay)
				if !sleepWithContext(ctx, delay) {
					return nil
				}
				continue
			}
			return fmt.Errorf("local action claim failed: %w", err)
		}
		retry.reset()

		if len(claimResp.Actions) == 0 {
			if !sleepWithContext(ctx, claimInterval) {
				return nil
			}
			continue
		}
		for _, action := range claimResp.Actions {
			actionID := strings.TrimSpace(action.LocalActionID)
			if actionID == "" || strings.TrimSpace(action.LeaseID) == "" {
				return fmt.Errorf("local action claim missing local_action_id or lease_id")
			}
			actionCtx, cancelAction := context.WithCancel(ctx)
			r.registerActiveRequest(actionID, cancelAction)
			err := r.executeLocalAction(actionCtx, peerToken, cwd, workspaceRoot, action)
			r.unregisterActiveRequest(actionID)
			cancelAction()
			if err != nil {
				return err
			}
		}
	}
}

func (r *Runner) executeLocalAction(ctx context.Context, peerToken, cwd, workspaceRoot string, action protocol.LocalActionRecord) error {
	_ = r.reportLocalActionProgress(ctx, peerToken, action, map[string]any{
		"status":      "started",
		"action_kind": action.ActionKind,
	})
	if localActionKind(action) == "tool_preview" {
		previewReq, err := localActionPreviewRequest(action)
		if err != nil {
			result := protocol.ToolPreviewResult{OK: false, ErrorCode: "LOCAL_ACTION_ERROR", ErrorMessage: err.Error()}
			return r.completeLocalAction(context.Background(), peerToken, action, "failed", mapFromStruct(result), err.Error())
		}
		result := tools.Preview(previewReq, cwd)
		status := "completed"
		errMessage := ""
		if !result.OK {
			status = "failed"
			errMessage = firstNonEmpty(result.ErrorMessage, result.ErrorCode)
		}
		return r.completeLocalAction(context.Background(), peerToken, action, status, mapFromStruct(result), errMessage)
	}

	req, err := localActionToolRequest(action)
	var result protocol.ExecToolResult
	if err != nil {
		result = protocol.ExecToolResult{OK: false, ErrorCode: "LOCAL_ACTION_ERROR", ErrorMessage: err.Error()}
	} else if req.ToolName == "mcp" {
		if r.mcp == nil {
			result = protocol.ExecToolResult{OK: false, ErrorCode: "REMOTE_MCP_ERROR", ErrorMessage: "MCP supervisor is not running"}
		} else {
			result = r.mcp.Execute(req.Args)
		}
	} else {
		result = tools.ExecuteWithContext(ctx, req, cwd, workspaceRoot, func(chunk protocol.ToolStreamChunk) {
			chunk = attachLocalActionIDToStreamChunk(chunk, action.LocalActionID)
			if sendErr := r.reportLocalActionProgress(context.Background(), peerToken, action, mapFromStruct(chunk)); sendErr != nil {
				log.Printf("local action progress report failed: %v", sendErr)
			}
		})
	}
	if result.Meta == nil {
		result.Meta = map[string]any{}
	}
	result.Meta["local_action_id"] = action.LocalActionID
	if req.ToolCallID != "" {
		result.Meta["tool_call_id"] = req.ToolCallID
	}
	status := "completed"
	errMessage := ""
	if !result.OK {
		status = "failed"
		errMessage = firstNonEmpty(result.ErrorMessage, result.ErrorCode)
	}
	return r.completeLocalAction(context.Background(), peerToken, action, status, mapFromStruct(result), errMessage)
}

type localActionRetryBackoff struct {
	attempt int
	delay   time.Duration
}

func (b *localActionRetryBackoff) recordFailure(claimInterval time.Duration) (int, time.Duration) {
	b.attempt++
	if b.delay <= 0 {
		b.delay = localActionRetryMinDelay
		if claimInterval > b.delay {
			b.delay = claimInterval
		}
	} else {
		b.delay *= 2
		if b.delay > localActionRetryMaxDelay {
			b.delay = localActionRetryMaxDelay
		}
	}
	return b.attempt, b.delay
}

func (b *localActionRetryBackoff) reset() {
	b.attempt = 0
	b.delay = 0
}

func isTransientLocalActionError(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}

	var httpErr *client.HTTPError
	if errors.As(err, &httpErr) {
		switch httpErr.StatusCode {
		case http.StatusRequestTimeout,
			http.StatusTooManyRequests,
			http.StatusInternalServerError,
			http.StatusBadGateway,
			http.StatusServiceUnavailable,
			http.StatusGatewayTimeout,
			521, 522, 523, 524:
			return true
		default:
			return false
		}
	}

	var netErr net.Error
	if errors.As(err, &netErr) && (netErr.Timeout() || netErr.Temporary()) {
		return true
	}
	if errors.Is(err, syscall.ECONNRESET) ||
		errors.Is(err, syscall.ECONNREFUSED) ||
		errors.Is(err, syscall.ETIMEDOUT) {
		return true
	}

	message := strings.ToLower(err.Error())
	return strings.Contains(message, "tls: bad record mac") ||
		strings.Contains(message, "connection reset by peer") ||
		strings.Contains(message, "connection refused") ||
		strings.Contains(message, "i/o timeout") ||
		strings.Contains(message, "temporary failure") ||
		strings.Contains(message, "server misbehaving")
}

func localActionToolRequest(action protocol.LocalActionRecord) (protocol.ExecToolRequest, error) {
	payload := action.Payload
	if payload == nil {
		payload = map[string]any{}
	}
	toolName := localActionToolName(action, payload)
	if toolName == "" || toolName == "tool_preview" {
		return protocol.ExecToolRequest{}, fmt.Errorf("unsupported local action kind %q", action.ActionKind)
	}
	cwd := optionalString(payload, "cwd", "workdir")
	req := protocol.ExecToolRequest{
		ToolName:              toolName,
		Args:                  localActionArgs(toolName, payload),
		CWD:                   optionalStringPointer(cwd),
		TimeoutSec:            optionalInt(payload, "timeout_sec"),
		PreviewIdentity:       optionalMap(payload, "preview_identity"),
		ApprovedSaveCandidate: firstOptionalMap(payload, "approved_save_candidate", "save_candidate", "candidate"),
		ToolCallID:            firstNonEmpty(optionalString(payload, "tool_call_id"), action.LocalActionID),
	}
	return req, nil
}

func localActionPreviewRequest(action protocol.LocalActionRecord) (protocol.ToolPreviewRequest, error) {
	payload := action.Payload
	if payload == nil {
		payload = map[string]any{}
	}
	toolName := firstNonEmpty(optionalString(payload, "preview_target_tool"), optionalString(payload, "tool_name"))
	if toolName == "" {
		return protocol.ToolPreviewRequest{}, fmt.Errorf("tool_preview local action requires tool_name")
	}
	cwd := optionalString(payload, "cwd", "workdir")
	return protocol.ToolPreviewRequest{
		ToolName:   toolName,
		Args:       localActionArgs(toolName, payload),
		CWD:        optionalStringPointer(cwd),
		TimeoutSec: optionalInt(payload, "timeout_sec"),
	}, nil
}

func localActionToolName(action protocol.LocalActionRecord, payload map[string]any) string {
	switch localActionKind(action) {
	case "read_workspace_file":
		return "read_file"
	case "shell", "read_file", "apply_patch", "draft_document_commit", "glob", "grep", "list_file", "lsp", "mcp", "tool_preview":
		return localActionKind(action)
	}
	return optionalString(payload, "tool_name")
}

func localActionKind(action protocol.LocalActionRecord) string {
	return strings.TrimSpace(action.ActionKind)
}

func localActionArgs(toolName string, payload map[string]any) map[string]any {
	if args := optionalMap(payload, "args"); args != nil {
		return args
	}
	args := map[string]any{}
	for key, value := range payload {
		if isLocalActionControlField(toolName, key) {
			continue
		}
		args[key] = value
	}
	return args
}

func isLocalActionControlField(toolName, key string) bool {
	switch key {
	case "args", "cwd", "workdir", "timeout_sec", "preview_identity", "approved_save_candidate", "save_candidate", "candidate", "tool_call_id", "preview_target_tool":
		return true
	case "tool_name":
		return toolName != "mcp"
	default:
		return false
	}
}

func optionalString(payload map[string]any, keys ...string) string {
	for _, key := range keys {
		if value, ok := payload[key].(string); ok {
			if trimmed := strings.TrimSpace(value); trimmed != "" {
				return trimmed
			}
		}
	}
	return ""
}

func optionalStringPointer(value string) *string {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	copied := value
	return &copied
}

func optionalInt(payload map[string]any, key string) int {
	switch value := payload[key].(type) {
	case int:
		return value
	case int64:
		return int(value)
	case float64:
		return int(value)
	case json.Number:
		n, _ := value.Int64()
		return int(n)
	default:
		return 0
	}
}

func optionalMap(payload map[string]any, key string) map[string]any {
	value, ok := payload[key]
	if !ok {
		return nil
	}
	if mapped, ok := value.(map[string]any); ok {
		return mapped
	}
	return nil
}

func firstOptionalMap(payload map[string]any, keys ...string) map[string]any {
	for _, key := range keys {
		if value := optionalMap(payload, key); value != nil {
			return value
		}
	}
	return nil
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if trimmed := strings.TrimSpace(value); trimmed != "" {
			return trimmed
		}
	}
	return ""
}

func attachLocalActionIDToStreamChunk(chunk protocol.ToolStreamChunk, localActionID string) protocol.ToolStreamChunk {
	chunk.ToolCallID = localActionID
	if chunk.Meta == nil {
		chunk.Meta = map[string]any{}
	}
	chunk.Meta["local_action_id"] = localActionID
	return chunk
}

func sleepWithContext(ctx context.Context, delay time.Duration) bool {
	if delay <= 0 {
		return ctx.Err() == nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
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
		if err := r.runRemoteSessionRun(ctx, peerToken, userInput); err != nil {
			return err
		}
	}
}

func (r *Runner) runRemoteSessionRun(ctx context.Context, peerToken, prompt string) error {
	sessionRunCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	startResp, err := r.client.SessionRunStart(sessionRunCtx, protocol.SessionRunStartRequest{
		PeerToken: peerToken,
		Prompt:    prompt,
	})
	cancel()
	if err != nil {
		return fmt.Errorf("session run start failed: %w", err)
	}
	if strings.TrimSpace(startResp.Error) != "" {
		return fmt.Errorf("session run start failed: %s", startResp.Error)
	}
	if strings.TrimSpace(startResp.SessionRunID) == "" {
		return fmt.Errorf("session run start failed: empty session run id")
	}
	branchBindingID := strings.TrimSpace(startResp.BranchBindingID)
	if branchBindingID == "" {
		branchBindingID = "main"
	}

	return r.client.SessionRunEvents(
		ctx,
		protocol.SessionRunEventsRequest{
			PeerToken:       peerToken,
			SessionRunID:    startResp.SessionRunID,
			BranchBindingID: branchBindingID,
			Cursor:          0,
			TimeoutSec:      30,
		},
		func(batch protocol.SessionRunEventsBatch) error {
			if strings.TrimSpace(batch.Error) != "" {
				return fmt.Errorf("session run events failed: %s", batch.Error)
			}
			for _, event := range batch.Events {
				if err := r.handleSessionRunEvent(ctx, peerToken, startResp.SessionRunID, branchBindingID, event); err != nil {
					return err
				}
			}
			return nil
		},
	)
}

func (r *Runner) handleSessionRunEvent(ctx context.Context, peerToken, sessionRunID, branchBindingID string, event protocol.SessionRunEvent) error {
	switch event.Type {
	case "session_run_start":
		return nil
	case "output":
		r.renderOutputEvent(event.Payload)
	case "tool_call_stream":
		r.renderToolStream(event.Payload)
	case "approval_request":
		return r.handleApprovalRequest(ctx, peerToken, sessionRunID, branchBindingID, event.Payload)
	case "approval_resolved":
		return nil
	case "tool_call_start":
		if name, _ := event.Payload["tool_name"].(string); name != "" {
			fmt.Printf("\n[tool] %s\n", name)
		}
	case "tool_call_end":
		return nil
	case "session_run_end":
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

func (r *Runner) handleApprovalRequest(ctx context.Context, peerToken, sessionRunID, branchBindingID string, payload map[string]any) error {
	r.renderOutputEvent(payload)

	approvalID, _ := payload["approval_id"].(string)
	if approvalID == "" {
		return fmt.Errorf("approval request missing approval_id")
	}
	targetBranchBindingID, _ := payload["branch_binding_id"].(string)
	targetBranchBindingID = strings.TrimSpace(targetBranchBindingID)
	if targetBranchBindingID == "" {
		targetBranchBindingID = branchBindingID
	}
	if targetBranchBindingID == "" {
		targetBranchBindingID = "main"
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
		PeerToken:       peerToken,
		SessionRunID:    sessionRunID,
		BranchBindingID: targetBranchBindingID,
		ApprovalID:      approvalID,
		Decision:        decision,
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

func (r *Runner) reportLocalActionProgress(ctx context.Context, peerToken string, action protocol.LocalActionRecord, progress map[string]any) error {
	sendCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	resp, err := r.client.ReportLocalActionProgress(sendCtx, protocol.LocalActionProgressRequest{
		PeerToken:     peerToken,
		LocalActionID: action.LocalActionID,
		LeaseID:       action.LeaseID,
		Status:        "progress",
		Progress:      progress,
	})
	if err != nil {
		return err
	}
	if !resp.OK {
		return fmt.Errorf("local action progress failed: %s", resp.Error)
	}
	return nil
}

func (r *Runner) completeLocalAction(ctx context.Context, peerToken string, action protocol.LocalActionRecord, status string, result map[string]any, errorMessage string) error {
	sendCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	resp, err := r.client.CompleteLocalAction(sendCtx, protocol.LocalActionCompleteRequest{
		PeerToken:     peerToken,
		LocalActionID: action.LocalActionID,
		LeaseID:       action.LeaseID,
		Status:        status,
		Result:        result,
		Error:         errorMessage,
	})
	if err != nil {
		return err
	}
	if !resp.OK {
		return fmt.Errorf("local action complete failed: %s", resp.Error)
	}
	return nil
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
	features := runtimeExecutorFeatures()
	order := []string{"fake", "reuleauxcoder", "codex", "claude", "gemini"}
	executors := make([]string, 0, len(order))
	for _, executor := range order {
		if feature, ok := features[executor].(map[string]any); ok {
			if installed, _ := feature["installed"].(bool); installed {
				executors = append(executors, executor)
			}
		}
	}
	return executors
}

func runtimeExecutionLocations() []string {
	return runtimeExecutionLocationsForWorker("local_peer")
}

func runtimeExecutionLocationsForWorker(workerKind string) []string {
	switch runtimeWorkerKind(workerKind) {
	case "server_worker":
		return []string{"daemon_worktree", "remote_server"}
	case "sandbox_worker":
		return []string{"remote_server"}
	default:
		return []string{"local_workspace"}
	}
}

func runtimeWorkerKind(value string) string {
	switch strings.TrimSpace(value) {
	case "server_worker", "sandbox_worker", "local_peer":
		return strings.TrimSpace(value)
	default:
		return "local_peer"
	}
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
	_, err := lookPathExecutable(command)
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

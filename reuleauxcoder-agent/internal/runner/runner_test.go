package runner

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/client"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestMain(m *testing.M) {
	if capturePath := os.Getenv("RUNNER_HELPER_CAPTURE_PATH"); capturePath != "" {
		configContent := ""
		if configPath := os.Getenv("RCODER_CONFIG_PATH"); configPath != "" {
			if raw, err := os.ReadFile(configPath); err == nil {
				configContent = string(raw)
			}
		}
		payload := map[string]any{
			"args": os.Args[1:],
			"env": map[string]string{
				"RCODER_CONFIG_PATH":                os.Getenv("RCODER_CONFIG_PATH"),
				"LABRASTRO_REMOTE_BASE_URL":         os.Getenv("LABRASTRO_REMOTE_BASE_URL"),
				"LABRASTRO_PEER_TOKEN":              os.Getenv("LABRASTRO_PEER_TOKEN"),
				"LABRASTRO_AGENT_RUN_ID":            os.Getenv("LABRASTRO_AGENT_RUN_ID"),
				"LABRASTRO_AGENT_RUN_REQUEST_ID":    os.Getenv("LABRASTRO_AGENT_RUN_REQUEST_ID"),
				"LABRASTRO_AGENT_RUN_ACTIVATION_ID": os.Getenv("LABRASTRO_AGENT_RUN_ACTIVATION_ID"),
				"LABRASTRO_AGENT_RUN_WORKER_ID":     os.Getenv("LABRASTRO_AGENT_RUN_WORKER_ID"),
				"RUNNER_HELPER_CAPTURE_PATH":        capturePath,
			},
			"config": configContent,
		}
		raw, err := json.MarshalIndent(payload, "", "  ")
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		if err := os.WriteFile(capturePath, raw, 0o600); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		if target := strings.TrimSpace(os.Getenv("RUNNER_HELPER_WRITE_FILE")); target != "" {
			if err := os.WriteFile(target, []byte("created by helper\n"), 0o644); err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(2)
			}
		}
		fmt.Println(`{"type":"status","status":"session_pinned","executor_session_id":"labrastro-agent-run-run-1"}`)
		fmt.Println(`{"type":"text","text":"helper ok"}`)
		fmt.Println(`{"type":"result","status":"completed","output":"helper ok","executor_session_id":"labrastro-agent-run-run-1"}`)
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func TestLocalActionToolRequestBindsActionIdentity(t *testing.T) {
	req, err := localActionToolRequest(protocol.LocalActionRecord{
		LocalActionID: "local-action-1",
		ActionKind:    "read_workspace_file",
		Payload: map[string]any{
			"args": map[string]any{"file_path": "README.md"},
		},
	})

	if err != nil {
		t.Fatalf("localActionToolRequest returned error: %v", err)
	}
	if req.ToolName != "read_file" {
		t.Fatalf("tool name = %q, want read_file", req.ToolName)
	}
	if req.ToolCallID != "local-action-1" {
		t.Fatalf("tool_call_id = %q, want local-action-1", req.ToolCallID)
	}
	if req.Args["file_path"] != "README.md" {
		t.Fatalf("args = %#v", req.Args)
	}
}

func TestAttachLocalActionIDToStreamChunk(t *testing.T) {
	chunk := attachLocalActionIDToStreamChunk(protocol.ToolStreamChunk{
		ChunkType: "stdout",
		Data:      "hello",
	}, "local-action-1")

	if chunk.ToolCallID != "local-action-1" {
		t.Fatalf("chunk.ToolCallID = %q, want local-action-1", chunk.ToolCallID)
	}
	if chunk.Meta["local_action_id"] != "local-action-1" {
		t.Fatalf("chunk meta = %#v, want local_action_id", chunk.Meta)
	}
}

func TestBaseFeaturesAdvertisesLSPOnlyWhenAvailable(t *testing.T) {
	withLSP := baseFeatures(true)
	withoutLSP := baseFeatures(false)

	if !containsFeature(withLSP, "lsp") {
		t.Fatalf("features = %#v, want lsp", withLSP)
	}
	if !containsFeature(withoutLSP, "local_actions") || !containsFeature(withoutLSP, "local_action:read_workspace_file") {
		t.Fatalf("features = %#v, want local action claim features", withoutLSP)
	}
	if containsFeature(withoutLSP, "lsp") {
		t.Fatalf("features = %#v, want no lsp", withoutLSP)
	}
}

func TestRuntimeExecutorsAdvertisesOnlyInstalledCLIExecutors(t *testing.T) {
	oldLookPath := lookPathExecutable
	defer func() { lookPathExecutable = oldLookPath }()

	lookPathExecutable = func(command string) (string, error) {
		if command == "codex" {
			return "/usr/bin/codex", nil
		}
		return "", errExecutableNotFound
	}

	executors := runtimeExecutors()

	if !containsFeature(executors, "fake") {
		t.Fatalf("executors = %#v, want fake", executors)
	}
	if !containsFeature(executors, "codex") {
		t.Fatalf("executors = %#v, want codex", executors)
	}
	if containsFeature(executors, "claude") || containsFeature(executors, "gemini") {
		t.Fatalf("executors = %#v, want only installed CLI executors", executors)
	}
}

func TestLocalPeerRuntimeLocationsDoNotAdvertiseRemoteServer(t *testing.T) {
	locations := runtimeExecutionLocations()

	if containsFeature(locations, "remote_server") {
		t.Fatalf("locations = %#v, local peer must not advertise remote_server", locations)
	}
	if containsFeature(locations, "daemon_worktree") {
		t.Fatalf("locations = %#v, local peer must not advertise daemon_worktree", locations)
	}
	if !containsFeature(locations, "local_workspace") {
		t.Fatalf("locations = %#v, want local_workspace", locations)
	}
}

func TestServerWorkerRuntimeLocationsAdvertiseServerLocations(t *testing.T) {
	locations := runtimeExecutionLocationsForWorker("server_worker")

	if !containsFeature(locations, "remote_server") {
		t.Fatalf("locations = %#v, want remote_server", locations)
	}
	if !containsFeature(locations, "daemon_worktree") {
		t.Fatalf("locations = %#v, want daemon_worktree", locations)
	}
	if containsFeature(locations, "local_workspace") {
		t.Fatalf("locations = %#v, server worker must not advertise local_workspace", locations)
	}
}

func TestTransientLocalActionErrorClassification(t *testing.T) {
	cases := []struct {
		name      string
		err       error
		transient bool
	}{
		{
			name:      "context deadline",
			err:       context.DeadlineExceeded,
			transient: true,
		},
		{
			name:      "http 503",
			err:       &client.HTTPError{StatusCode: http.StatusServiceUnavailable, Body: "unavailable"},
			transient: true,
		},
		{
			name:      "cloudflare 521",
			err:       &client.HTTPError{StatusCode: 521, Body: "web server down"},
			transient: true,
		},
		{
			name:      "invalid peer token",
			err:       &client.HTTPError{StatusCode: http.StatusUnauthorized, Body: "invalid_peer_token"},
			transient: false,
		},
		{
			name:      "forbidden",
			err:       &client.HTTPError{StatusCode: http.StatusForbidden, Body: "forbidden"},
			transient: false,
		},
		{
			name:      "tls bad record mac",
			err:       errors.New("local action claim failed: tls: bad record MAC"),
			transient: true,
		},
		{
			name:      "context canceled",
			err:       context.Canceled,
			transient: false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := isTransientLocalActionError(tc.err); got != tc.transient {
				t.Fatalf("isTransientLocalActionError(%v) = %v, want %v", tc.err, got, tc.transient)
			}
		})
	}
}

func TestLocalActionRetryBackoffResetsAfterSuccess(t *testing.T) {
	restore := overrideLocalActionTimingForTest(t, 50*time.Millisecond, 10*time.Millisecond, 40*time.Millisecond)
	defer restore()

	backoff := localActionRetryBackoff{}
	attempt, delay := backoff.recordFailure(5 * time.Millisecond)
	if attempt != 1 || delay != 10*time.Millisecond {
		t.Fatalf("first retry = (%d, %s), want (1, 10ms)", attempt, delay)
	}
	attempt, delay = backoff.recordFailure(5 * time.Millisecond)
	if attempt != 2 || delay != 20*time.Millisecond {
		t.Fatalf("second retry = (%d, %s), want (2, 20ms)", attempt, delay)
	}

	backoff.reset()
	attempt, delay = backoff.recordFailure(5 * time.Millisecond)
	if attempt != 1 || delay != 10*time.Millisecond {
		t.Fatalf("retry after reset = (%d, %s), want (1, 10ms)", attempt, delay)
	}
}

func TestRunLocalActionLoopRetriesContextDeadlineAndContinues(t *testing.T) {
	restore := overrideLocalActionTimingForTest(t, 20*time.Millisecond, time.Millisecond, 5*time.Millisecond)
	defer restore()

	var claims atomic.Int32
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		switch claims.Add(1) {
		case 1:
			time.Sleep(50 * time.Millisecond)
		default:
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"actions":[]}`))
			cancel()
		}
	}))
	defer server.Close()

	err := runLocalActionLoopForTest(ctx, server.URL, t.TempDir())
	if err != nil {
		t.Fatalf("runLocalActionLoop returned error after transient timeout: %v", err)
	}
	if claims.Load() < 2 {
		t.Fatalf("claims = %d, want retry after timeout", claims.Load())
	}
}

func TestRunLocalActionLoopRetriesTransientHTTPStatusAndContinues(t *testing.T) {
	restore := overrideLocalActionTimingForTest(t, 50*time.Millisecond, time.Millisecond, 5*time.Millisecond)
	defer restore()

	var claims atomic.Int32
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		switch claims.Add(1) {
		case 1:
			http.Error(w, "temporary upstream failure", http.StatusBadGateway)
		default:
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"actions":[]}`))
			cancel()
		}
	}))
	defer server.Close()

	err := runLocalActionLoopForTest(ctx, server.URL, t.TempDir())
	if err != nil {
		t.Fatalf("runLocalActionLoop returned error after transient HTTP status: %v", err)
	}
	if claims.Load() < 2 {
		t.Fatalf("claims = %d, want retry after HTTP status", claims.Load())
	}
}

func TestRunLocalActionLoopExitsOnInvalidPeerToken(t *testing.T) {
	restore := overrideLocalActionTimingForTest(t, 50*time.Millisecond, time.Millisecond, 5*time.Millisecond)
	defer restore()

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "invalid_peer_token", http.StatusUnauthorized)
	}))
	defer server.Close()

	err := runLocalActionLoopForTest(context.Background(), server.URL, t.TempDir())
	if err == nil {
		t.Fatal("runLocalActionLoop returned nil error for invalid peer token")
	}
	if !strings.Contains(err.Error(), "local action claim failed") || !strings.Contains(err.Error(), "401") {
		t.Fatalf("error = %v, want fatal local action claim failure with 401", err)
	}
}

func TestRunLocalActionLoopClaimsReportsProgressAndCompletes(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "README.md"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var claimReq protocol.LocalActionClaimRequest
	var progressReq protocol.LocalActionProgressRequest
	var completeReq protocol.LocalActionCompleteRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch req.URL.Path {
		case "/remote/local-actions/claim":
			if err := json.NewDecoder(req.Body).Decode(&claimReq); err != nil {
				t.Errorf("decode claim: %v", err)
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(w).Encode(protocol.LocalActionClaimResponse{
				Actions: []protocol.LocalActionRecord{{
					Scope:         "activation_scoped",
					LocalActionID: "local-action-1",
					ActionKind:    "read_workspace_file",
					Status:        "started",
					WorkspaceRoot: root,
					Payload:       map[string]any{"args": map[string]any{"file_path": "README.md"}},
					LeaseID:       "lease-1",
				}},
			})
		case "/remote/local-actions/progress":
			if err := json.NewDecoder(req.Body).Decode(&progressReq); err != nil {
				t.Errorf("decode progress: %v", err)
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(w).Encode(protocol.LocalActionProgressResponse{OK: true})
		case "/remote/local-actions/complete":
			defer cancel()
			if err := json.NewDecoder(req.Body).Decode(&completeReq); err != nil {
				t.Errorf("decode complete: %v", err)
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(w).Encode(protocol.LocalActionCompleteResponse{OK: true})
		default:
			t.Errorf("unexpected path: %s", req.URL.Path)
			http.NotFound(w, req)
		}
	}))
	defer server.Close()

	err := runLocalActionLoopForTest(ctx, server.URL, root)
	if err != nil {
		t.Fatalf("runLocalActionLoop returned error: %v", err)
	}
	if claimReq.PeerToken != "peer-token" || claimReq.PeerID != "peer-1" || claimReq.WorkerKind != "local_peer" {
		t.Fatalf("claim request = %#v", claimReq)
	}
	if !containsFeature(claimReq.Features, "local_actions") || !containsFeature(claimReq.Features, "local_action:read_workspace_file") {
		t.Fatalf("claim features = %#v", claimReq.Features)
	}
	if progressReq.LocalActionID != "local-action-1" || progressReq.LeaseID != "lease-1" {
		t.Fatalf("progress request = %#v", progressReq)
	}
	if completeReq.LocalActionID != "local-action-1" || completeReq.LeaseID != "lease-1" || completeReq.Status != "completed" {
		t.Fatalf("complete request = %#v", completeReq)
	}
	if completeReq.Result["ok"] != true || !strings.Contains(fmt.Sprint(completeReq.Result["result"]), "hello") {
		t.Fatalf("complete result = %#v", completeReq.Result)
	}
}

func TestRuntimeHeartbeatLoopAcknowledgesActivationSteers(t *testing.T) {
	restore := overrideRuntimeHeartbeatIntervalForTest(t, 10*time.Millisecond)
	defer restore()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	requests := make(chan protocol.AgentRunActivationHeartbeatRequest, 4)
	var heartbeatCount atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path != "/remote/agent-run-activations/heartbeat" {
			t.Errorf("unexpected path: %s", req.URL.Path)
			http.NotFound(w, req)
			return
		}
		var body protocol.AgentRunActivationHeartbeatRequest
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			t.Errorf("decode heartbeat: %v", err)
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		requests <- body
		w.Header().Set("Content-Type", "application/json")
		if heartbeatCount.Add(1) == 1 {
			_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationHeartbeatResponse{
				OK:              true,
				CancelRequested: false,
				ActivationSteers: []protocol.ActivationSteer{
					{
						ID:           "steer-1",
						ActivationID: "run-1:activation:1",
						Source:       "user",
						Payload: map[string]any{
							"items": []any{map[string]any{"type": "text", "text": "add context"}},
						},
						Status: "delivering",
					},
				},
			})
			return
		}
		cancel()
		_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationHeartbeatResponse{
			OK:              true,
			CancelRequested: false,
		})
	}))
	defer server.Close()

	runner := &Runner{
		client: client.New(server.URL),
		active: map[string]context.CancelFunc{},
	}
	deliveredSteers := make(chan protocol.ActivationSteer, 1)
	done := make(chan struct{})
	go func() {
		defer close(done)
		runner.runtimeHeartbeatLoop(
			ctx,
			"peer-token",
			"worker-1",
			"claim-1",
			"run-1:activation:1",
			"run-1",
			cancel,
			func(ctx context.Context, steer protocol.ActivationSteer) error {
				select {
				case deliveredSteers <- steer:
					return nil
				case <-ctx.Done():
					return ctx.Err()
				}
			},
		)
	}()

	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("runtime heartbeat loop did not stop")
	}

	first := <-requests
	second := <-requests
	if len(first.DeliveredSteerIDs) != 0 {
		t.Fatalf("first heartbeat delivered ids = %#v", first.DeliveredSteerIDs)
	}
	if len(second.DeliveredSteerIDs) != 1 || second.DeliveredSteerIDs[0] != "steer-1" {
		t.Fatalf("second heartbeat delivered ids = %#v", second.DeliveredSteerIDs)
	}
	delivered := <-deliveredSteers
	if delivered.ID != "steer-1" || delivered.Payload["items"] == nil {
		t.Fatalf("delivered steer = %#v", delivered)
	}
	if second.PeerToken != "peer-token" ||
		second.RequestID != "claim-1" ||
		second.ActivationID != "run-1:activation:1" ||
		second.TaskID != "run-1" ||
		second.WorkerID != "worker-1" {
		t.Fatalf("heartbeat identity drifted: %#v", second)
	}
}

func TestRuntimeHeartbeatLoopDoesNotAcknowledgeUndeliveredActivationSteers(t *testing.T) {
	restore := overrideRuntimeHeartbeatIntervalForTest(t, 10*time.Millisecond)
	defer restore()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	requests := make(chan protocol.AgentRunActivationHeartbeatRequest, 4)
	var heartbeatCount atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path != "/remote/agent-run-activations/heartbeat" {
			t.Errorf("unexpected path: %s", req.URL.Path)
			http.NotFound(w, req)
			return
		}
		var body protocol.AgentRunActivationHeartbeatRequest
		if err := json.NewDecoder(req.Body).Decode(&body); err != nil {
			t.Errorf("decode heartbeat: %v", err)
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		requests <- body
		w.Header().Set("Content-Type", "application/json")
		if heartbeatCount.Add(1) == 1 {
			_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationHeartbeatResponse{
				OK: true,
				ActivationSteers: []protocol.ActivationSteer{
					{
						ID:           "steer-1",
						ActivationID: "run-1:activation:1",
						Source:       "user",
						Payload: map[string]any{
							"items": []any{map[string]any{"type": "text", "text": "add context"}},
						},
						Status: "delivering",
					},
				},
			})
			return
		}
		cancel()
		_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationHeartbeatResponse{OK: true})
	}))
	defer server.Close()

	runner := &Runner{
		client: client.New(server.URL),
		active: map[string]context.CancelFunc{},
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		runner.runtimeHeartbeatLoop(
			ctx,
			"peer-token",
			"worker-1",
			"claim-1",
			"run-1:activation:1",
			"run-1",
			cancel,
			func(context.Context, protocol.ActivationSteer) error {
				return errors.New("executor not ready")
			},
		)
	}()

	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("runtime heartbeat loop did not stop")
	}

	first := <-requests
	second := <-requests
	if len(first.DeliveredSteerIDs) != 0 {
		t.Fatalf("first heartbeat delivered ids = %#v", first.DeliveredSteerIDs)
	}
	if len(second.DeliveredSteerIDs) != 0 {
		t.Fatalf("second heartbeat delivered ids = %#v", second.DeliveredSteerIDs)
	}
}

func TestAgentRunLoopInjectsServerOriginBridgeOptions(t *testing.T) {
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	capturePath := filepath.Join(root, "helper-capture.json")
	repoURL := createRunnerSourceRepo(t, root)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var completed atomic.Bool
	var completeReq protocol.AgentRunActivationCompleteRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch req.URL.Path {
		case "/remote/agent-run-activations/claim":
			if completed.Load() {
				_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationClaimResponse{})
				return
			}
			_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationClaimResponse{
				Claim: &protocol.AgentRunActivationClaim{
					RequestID:    "claim-1",
					ActivationID: "run-1:activation:1",
					WorkerID:     "worker-1",
					AgentRun:     map[string]any{"id": "run-1"},
					ExecutorRequest: protocol.ExecutorRequest{
						TaskID:             "run-1",
						AgentID:            "capability_packager",
						Executor:           "reuleauxcoder",
						Prompt:             "package repo",
						ExecutionLocation:  "remote_server",
						RuntimeProfileID:   "capability_packager_remote",
						WorkerKind:         "sandbox_worker",
						ModelRequestOrigin: "server",
						Model:              "deepseek-v4-pro",
						ExecutorSessionID:  "labrastro-agent-run-run-1",
						Metadata: map[string]any{
							"repo_url": repoURL,
							"model_binding": map[string]any{
								"provider": "deepseek",
								"model":    "deepseek-v4-pro",
								"parameters": map[string]any{
									"max_tokens":         384000,
									"max_context_tokens": 1000000,
									"temperature":        0.2,
								},
							},
						},
					},
					RuntimeSnapshot: map[string]any{
						"runtime_profiles": map[string]any{
							"capability_packager_remote": map[string]any{
								"executor":             "reuleauxcoder",
								"execution_location":   "remote_server",
								"worker_kind":          "sandbox_worker",
								"model_request_origin": "server",
								"command":              executable,
								"env": map[string]any{
									"RUNNER_HELPER_CAPTURE_PATH": capturePath,
								},
							},
						},
						"agents": map[string]any{
							"capability_packager": map[string]any{
								"runtime_profile": "capability_packager_remote",
							},
						},
					},
				},
			})
		case "/remote/agent-run-activations/event", "/remote/agent-run-activations/heartbeat", "/remote/agent-run-activations/session":
			_, _ = w.Write([]byte(`{"ok":true,"cancel_requested":false}`))
		case "/remote/agent-run-activations/complete":
			defer cancel()
			completed.Store(true)
			if err := json.NewDecoder(req.Body).Decode(&completeReq); err != nil {
				t.Errorf("decode complete request: %v", err)
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			_, _ = w.Write([]byte(`{"ok":true}`))
		default:
			t.Errorf("unexpected path: %s", req.URL.Path)
			http.NotFound(w, req)
		}
	}))
	defer server.Close()

	runner := New(Config{
		Host:            server.URL,
		AgentRun:        true,
		WorkerKind:      "sandbox_worker",
		WorkerSessionID: "worker-1",
	})
	if err := runner.runAgentRunLoop(ctx, "peer-token", time.Millisecond, root); err != nil {
		t.Fatalf("runAgentRunLoop error: %v", err)
	}
	if completeReq.Status != "completed" {
		t.Fatalf("complete status = %q error = %q", completeReq.Status, completeReq.Error)
	}
	if len(completeReq.Events) != 0 {
		t.Fatalf("streamed runtime events must not be replayed on complete: %#v", completeReq.Events)
	}
	if completeReq.TaskID != "run-1" || completeReq.RequestID != "claim-1" || completeReq.ActivationID != "run-1:activation:1" || completeReq.WorkerID != "worker-1" {
		t.Fatalf("complete request identifiers not preserved: %#v", completeReq)
	}

	raw, err := os.ReadFile(capturePath)
	if err != nil {
		t.Fatalf("read helper capture: %v", err)
	}
	var capture struct {
		Args   []string          `json:"args"`
		Env    map[string]string `json:"env"`
		Config string            `json:"config"`
	}
	if err := json.Unmarshal(raw, &capture); err != nil {
		t.Fatalf("parse helper capture: %v", err)
	}
	if capture.Env["LABRASTRO_REMOTE_BASE_URL"] != server.URL ||
		capture.Env["LABRASTRO_PEER_TOKEN"] != "peer-token" ||
		capture.Env["LABRASTRO_AGENT_RUN_ID"] != "run-1" ||
		capture.Env["LABRASTRO_AGENT_RUN_REQUEST_ID"] != "claim-1" ||
		capture.Env["LABRASTRO_AGENT_RUN_ACTIVATION_ID"] != "run-1:activation:1" ||
		capture.Env["LABRASTRO_AGENT_RUN_WORKER_ID"] != "worker-1" {
		t.Fatalf("server-origin bridge env not injected: %#v", capture.Env)
	}
	if capture.Env["RCODER_CONFIG_PATH"] == "" {
		t.Fatalf("RCODER_CONFIG_PATH missing: %#v", capture.Env)
	}
	for _, want := range []string{`"type": "labrastro_server"`, `"model": "deepseek-v4-pro"`, `"max_tokens": 384000`} {
		if !strings.Contains(capture.Config, want) {
			t.Fatalf("generated config missing %s:\n%s", want, capture.Config)
		}
	}
	joinedArgs := strings.Join(capture.Args, " ")
	if !strings.Contains(joinedArgs, "--model agent-run") || strings.Contains(joinedArgs, "deepseek-v4-pro") {
		t.Fatalf("server-origin runner args must select generated profile, got: %v", capture.Args)
	}
	for _, want := range []string{"agent-run", "--session labrastro-agent-run-run-1", "--events jsonl"} {
		if !strings.Contains(joinedArgs, want) {
			t.Fatalf("server-origin runner args missing %s, got: %v", want, capture.Args)
		}
	}
	if strings.Contains(capture.Config, "peer-token") || strings.Contains(capture.Config, "api_key") {
		t.Fatalf("generated config leaked server credentials:\n%s", capture.Config)
	}
}

func TestAgentRunLoopCompletesOnlyFailedLiveEvents(t *testing.T) {
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	capturePath := filepath.Join(root, "helper-capture.json")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var completed atomic.Bool
	var failedTextEvents atomic.Int32
	var forwardedSessionPinned atomic.Bool
	var sessionPins atomic.Int32
	var completeReq protocol.AgentRunActivationCompleteRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch req.URL.Path {
		case "/remote/agent-run-activations/claim":
			if completed.Load() {
				_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationClaimResponse{})
				return
			}
			_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationClaimResponse{
				Claim: &protocol.AgentRunActivationClaim{
					RequestID:    "claim-1",
					ActivationID: "run-1:activation:1",
					WorkerID:     "worker-1",
					AgentRun:     map[string]any{"id": "run-1"},
					ExecutorRequest: protocol.ExecutorRequest{
						TaskID:            "run-1",
						AgentID:           "capability_packager",
						Executor:          "reuleauxcoder",
						Prompt:            "package repo",
						ExecutionLocation: "local_workspace",
						RuntimeProfileID:  "capability_packager_local",
						WorkerKind:        "local_peer",
						ExecutorSessionID: "labrastro-agent-run-run-1",
						Metadata: map[string]any{
							"prompt_files": map[string]any{
								"AGENTS.md": "Use test conventions.\n",
							},
						},
					},
					RuntimeSnapshot: map[string]any{
						"runtime_profiles": map[string]any{
							"capability_packager_local": map[string]any{
								"executor":           "reuleauxcoder",
								"execution_location": "local_workspace",
								"worker_kind":        "local_peer",
								"command":            executable,
								"env": map[string]any{
									"RUNNER_HELPER_CAPTURE_PATH": capturePath,
								},
							},
						},
						"agents": map[string]any{
							"capability_packager": map[string]any{
								"runtime_profile": "capability_packager_local",
							},
						},
					},
				},
			})
		case "/remote/agent-run-activations/event":
			var event protocol.AgentRunActivationEventReport
			if err := json.NewDecoder(req.Body).Decode(&event); err != nil {
				t.Errorf("decode event request: %v", err)
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			if event.ActivationID != "run-1:activation:1" {
				t.Errorf("event activation_id = %q", event.ActivationID)
			}
			if event.Type == "status" && event.Data["status"] == "session_pinned" {
				forwardedSessionPinned.Store(true)
			}
			if event.Type == "text" && event.Text == "helper ok" && failedTextEvents.Add(1) == 1 {
				http.Error(w, "temporary event failure", http.StatusBadGateway)
				return
			}
			_, _ = w.Write([]byte(`{"ok":true}`))
		case "/remote/agent-run-activations/session":
			sessionPins.Add(1)
			_, _ = w.Write([]byte(`{"ok":true,"cancel_requested":false}`))
		case "/remote/agent-run-activations/heartbeat":
			_, _ = w.Write([]byte(`{"ok":true,"cancel_requested":false}`))
		case "/remote/agent-run-activations/complete":
			defer cancel()
			completed.Store(true)
			if err := json.NewDecoder(req.Body).Decode(&completeReq); err != nil {
				t.Errorf("decode complete request: %v", err)
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			_, _ = w.Write([]byte(`{"ok":true}`))
		default:
			t.Errorf("unexpected path: %s", req.URL.Path)
			http.NotFound(w, req)
		}
	}))
	defer server.Close()

	runner := New(Config{
		Host:            server.URL,
		AgentRun:        true,
		WorkerKind:      "local_peer",
		WorkerSessionID: "worker-1",
	})
	if err := runner.runAgentRunLoop(ctx, "peer-token", time.Millisecond, root); err != nil {
		t.Fatalf("runAgentRunLoop error: %v", err)
	}
	if completeReq.Status != "completed" {
		t.Fatalf("complete status = %q error = %q", completeReq.Status, completeReq.Error)
	}
	if len(completeReq.Events) != 1 {
		t.Fatalf("complete events = %#v, want only failed live event", completeReq.Events)
	}
	if completeReq.ActivationID != "run-1:activation:1" || completeReq.Events[0].ActivationID != "run-1:activation:1" {
		t.Fatalf("activation_id not preserved in complete: %#v", completeReq)
	}
	if completeReq.Events[0].Type != "text" || completeReq.Events[0].Text != "helper ok" {
		t.Fatalf("failed live event not preserved: %#v", completeReq.Events[0])
	}
	if forwardedSessionPinned.Load() {
		t.Fatal("session_pinned status must not be forwarded as a visible runtime event")
	}
	if sessionPins.Load() == 0 {
		t.Fatal("session_pinned status should still pin the executor session")
	}
}

func TestAgentRunLoopCompletesFailedPrepareEvents(t *testing.T) {
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	capturePath := filepath.Join(root, "helper-capture.json")
	repoURL := createRunnerSourceRepo(t, root)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var completed atomic.Bool
	var failedPrepareEvents atomic.Int32
	var completeReq protocol.AgentRunActivationCompleteRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch req.URL.Path {
		case "/remote/agent-run-activations/claim":
			if completed.Load() {
				_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationClaimResponse{})
				return
			}
			_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationClaimResponse{
				Claim: &protocol.AgentRunActivationClaim{
					RequestID:    "claim-1",
					ActivationID: "run-1:activation:1",
					WorkerID:     "worker-1",
					AgentRun:     map[string]any{"id": "run-1"},
					ExecutorRequest: protocol.ExecutorRequest{
						TaskID:             "run-1",
						AgentID:            "capability_packager",
						Executor:           "reuleauxcoder",
						Prompt:             "package repo",
						ExecutionLocation:  "remote_server",
						RuntimeProfileID:   "capability_packager_remote",
						WorkerKind:         "sandbox_worker",
						ModelRequestOrigin: "local_cli",
						ExecutorSessionID:  "labrastro-agent-run-run-1",
						Metadata: map[string]any{
							"repo_url":       repoURL,
							"publish_policy": "never",
							"prompt_files": map[string]any{
								"AGENTS.md": "Use test conventions.\n",
							},
						},
					},
					RuntimeSnapshot: map[string]any{
						"runtime_profiles": map[string]any{
							"capability_packager_remote": map[string]any{
								"executor":             "reuleauxcoder",
								"execution_location":   "remote_server",
								"worker_kind":          "sandbox_worker",
								"model_request_origin": "local_cli",
								"command":              executable,
								"env": map[string]any{
									"RUNNER_HELPER_CAPTURE_PATH": capturePath,
								},
							},
						},
						"agents": map[string]any{
							"capability_packager": map[string]any{
								"runtime_profile": "capability_packager_remote",
							},
						},
					},
				},
			})
		case "/remote/agent-run-activations/event":
			var event protocol.AgentRunActivationEventReport
			if err := json.NewDecoder(req.Body).Decode(&event); err != nil {
				t.Errorf("decode event request: %v", err)
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			if event.ActivationID != "run-1:activation:1" {
				t.Errorf("event activation_id = %q", event.ActivationID)
			}
			if event.Type == "status" && event.Data["status"] == "preparing_worktree" && failedPrepareEvents.Add(1) == 1 {
				http.Error(w, "temporary prepare event failure", http.StatusBadGateway)
				return
			}
			_, _ = w.Write([]byte(`{"ok":true}`))
		case "/remote/agent-run-activations/heartbeat", "/remote/agent-run-activations/session":
			_, _ = w.Write([]byte(`{"ok":true,"cancel_requested":false}`))
		case "/remote/agent-run-activations/complete":
			defer cancel()
			completed.Store(true)
			if err := json.NewDecoder(req.Body).Decode(&completeReq); err != nil {
				t.Errorf("decode complete request: %v", err)
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			_, _ = w.Write([]byte(`{"ok":true}`))
		default:
			t.Errorf("unexpected path: %s", req.URL.Path)
			http.NotFound(w, req)
		}
	}))
	defer server.Close()

	runner := New(Config{
		Host:            server.URL,
		AgentRun:        true,
		WorkerKind:      "sandbox_worker",
		WorkerSessionID: "worker-1",
	})
	if err := runner.runAgentRunLoop(ctx, "peer-token", time.Millisecond, root); err != nil {
		t.Fatalf("runAgentRunLoop error: %v", err)
	}
	if completeReq.Status != "completed" {
		t.Fatalf("complete status = %q error = %q", completeReq.Status, completeReq.Error)
	}
	if len(completeReq.Events) != 1 {
		t.Fatalf("complete events = %#v, want failed prepare event only", completeReq.Events)
	}
	if completeReq.ActivationID != "run-1:activation:1" || completeReq.Events[0].ActivationID != "run-1:activation:1" {
		t.Fatalf("activation_id not preserved in complete: %#v", completeReq)
	}
	if completeReq.Events[0].Type != "status" || completeReq.Events[0].Data["status"] != "preparing_worktree" {
		t.Fatalf("failed prepare event not preserved: %#v", completeReq.Events[0])
	}
}

func TestAgentRunLoopCompletesFailedPublishEvents(t *testing.T) {
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	capturePath := filepath.Join(root, "helper-capture.json")
	repoURL := createRunnerSourceRepo(t, root)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var completed atomic.Bool
	var failedPublishEvents atomic.Int32
	var completeReq protocol.AgentRunActivationCompleteRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch req.URL.Path {
		case "/remote/agent-run-activations/claim":
			if completed.Load() {
				_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationClaimResponse{})
				return
			}
			_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationClaimResponse{
				Claim: &protocol.AgentRunActivationClaim{
					RequestID:    "claim-1",
					ActivationID: "run-1:activation:1",
					WorkerID:     "worker-1",
					AgentRun:     map[string]any{"id": "run-1"},
					ExecutorRequest: protocol.ExecutorRequest{
						TaskID:             "run-1",
						AgentID:            "capability_packager",
						Executor:           "reuleauxcoder",
						Prompt:             "package repo",
						ExecutionLocation:  "remote_server",
						RuntimeProfileID:   "capability_packager_remote",
						WorkerKind:         "sandbox_worker",
						ModelRequestOrigin: "local_cli",
						ExecutorSessionID:  "labrastro-agent-run-run-1",
						Metadata: map[string]any{
							"repo_url":       repoURL,
							"publish_policy": "branch",
							"prompt_files": map[string]any{
								"AGENTS.md": "Use test conventions.\n",
							},
						},
					},
					RuntimeSnapshot: map[string]any{
						"runtime_profiles": map[string]any{
							"capability_packager_remote": map[string]any{
								"executor":             "reuleauxcoder",
								"execution_location":   "remote_server",
								"worker_kind":          "sandbox_worker",
								"model_request_origin": "local_cli",
								"command":              executable,
								"env": map[string]any{
									"RUNNER_HELPER_CAPTURE_PATH": capturePath,
									"RUNNER_HELPER_WRITE_FILE":   "agent-output.txt",
								},
							},
						},
						"agents": map[string]any{
							"capability_packager": map[string]any{
								"runtime_profile": "capability_packager_remote",
							},
						},
					},
				},
			})
		case "/remote/agent-run-activations/event":
			var event protocol.AgentRunActivationEventReport
			if err := json.NewDecoder(req.Body).Decode(&event); err != nil {
				t.Errorf("decode event request: %v", err)
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			if event.ActivationID != "run-1:activation:1" {
				t.Errorf("event activation_id = %q", event.ActivationID)
			}
			if event.Type == "status" && event.Data["status"] == "branch_pushed" && failedPublishEvents.Add(1) == 1 {
				http.Error(w, "temporary publish event failure", http.StatusBadGateway)
				return
			}
			_, _ = w.Write([]byte(`{"ok":true}`))
		case "/remote/agent-run-activations/heartbeat", "/remote/agent-run-activations/session":
			_, _ = w.Write([]byte(`{"ok":true,"cancel_requested":false}`))
		case "/remote/agent-run-activations/complete":
			defer cancel()
			completed.Store(true)
			if err := json.NewDecoder(req.Body).Decode(&completeReq); err != nil {
				t.Errorf("decode complete request: %v", err)
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			_, _ = w.Write([]byte(`{"ok":true}`))
		default:
			t.Errorf("unexpected path: %s", req.URL.Path)
			http.NotFound(w, req)
		}
	}))
	defer server.Close()

	runner := New(Config{
		Host:            server.URL,
		AgentRun:        true,
		WorkerKind:      "sandbox_worker",
		WorkerSessionID: "worker-1",
	})
	if err := runner.runAgentRunLoop(ctx, "peer-token", time.Millisecond, root); err != nil {
		t.Fatalf("runAgentRunLoop error: %v", err)
	}
	if completeReq.Status != "completed" {
		t.Fatalf("complete status = %q error = %q", completeReq.Status, completeReq.Error)
	}
	if len(completeReq.Events) != 1 {
		t.Fatalf("complete events = %#v, want failed publish event only", completeReq.Events)
	}
	if completeReq.ActivationID != "run-1:activation:1" || completeReq.Events[0].ActivationID != "run-1:activation:1" {
		t.Fatalf("activation_id not preserved in complete: %#v", completeReq)
	}
	if completeReq.Events[0].Type != "status" || completeReq.Events[0].Data["status"] != "branch_pushed" {
		t.Fatalf("failed publish event not preserved: %#v", completeReq.Events[0])
	}
}

func runLocalActionLoopForTest(ctx context.Context, serverURL string, workspaceRoot string) error {
	r := &Runner{
		client: client.New(serverURL),
		active: map[string]context.CancelFunc{},
	}
	return r.runLocalActionLoop(ctx, "peer-token", "peer-1", workspaceRoot, workspaceRoot, baseFeatures(false), time.Millisecond)
}

func createRunnerSourceRepo(t *testing.T, root string) string {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git is required for runner worktree integration test")
	}
	repo := filepath.Join(root, "source-repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repo, "README.md"), []byte("runner fixture\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	commands := [][]string{
		{"init", "-b", "main"},
		{"config", "user.email", "runner@example.test"},
		{"config", "user.name", "Runner Test"},
		{"add", "README.md"},
		{"commit", "-m", "init"},
	}
	for _, args := range commands {
		cmd := exec.Command("git", args...)
		cmd.Dir = repo
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %s failed: %v\n%s", strings.Join(args, " "), err, out)
		}
	}
	return repo
}

func overrideLocalActionTimingForTest(
	t *testing.T,
	requestTimeout time.Duration,
	minDelay time.Duration,
	maxDelay time.Duration,
) func() {
	t.Helper()
	oldRequestTimeout := localActionRequestTimeout
	oldMinDelay := localActionRetryMinDelay
	oldMaxDelay := localActionRetryMaxDelay
	localActionRequestTimeout = requestTimeout
	localActionRetryMinDelay = minDelay
	localActionRetryMaxDelay = maxDelay
	return func() {
		localActionRequestTimeout = oldRequestTimeout
		localActionRetryMinDelay = oldMinDelay
		localActionRetryMaxDelay = oldMaxDelay
	}
}

func overrideRuntimeHeartbeatIntervalForTest(
	t *testing.T,
	interval time.Duration,
) func() {
	t.Helper()
	oldInterval := runtimeHeartbeatInterval
	runtimeHeartbeatInterval = interval
	return func() {
		runtimeHeartbeatInterval = oldInterval
	}
}

func containsFeature(features []string, target string) bool {
	for _, feature := range features {
		if feature == target {
			return true
		}
	}
	return false
}

package client

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestHTTPClientDoesNotExposeLegacyChatOnceProtocol(t *testing.T) {
	source, err := os.ReadFile("http.go")
	if err != nil {
		t.Fatalf("read http.go: %v", err)
	}
	if bytes := string(source); bytes != "" {
		if strings.Contains(bytes, "func (c *HTTPClient) Chat(") {
			t.Fatal("HTTP client still exposes legacy Chat one-shot protocol")
		}
		if strings.Contains(bytes, "\"/remote/chat\"") {
			t.Fatal("HTTP client still posts to legacy /remote/chat endpoint")
		}
	}
}

func TestSessionRunEventsStreamsSSEBatches(t *testing.T) {
	var requestPath string
	var requestBody protocol.SessionRunEventsRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requestPath = r.URL.Path
		if r.Header.Get("Accept") != "text/event-stream" {
			t.Fatalf("Accept = %q, want text/event-stream", r.Header.Get("Accept"))
		}
		if err := json.NewDecoder(r.Body).Decode(&requestBody); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = w.Write([]byte(": ping\n\n"))
		_, _ = w.Write([]byte("event: session_run\n"))
		_, _ = w.Write([]byte(`data: {"events":[{"type":"assistant_delta","payload":{"content":"hello"}}],"done":false,"next_cursor":2}` + "\n\n"))
		_, _ = w.Write([]byte("event: session_run\n"))
		_, _ = w.Write([]byte(`data: {"events":[{"type":"session_run_end","payload":{"response":"ok"}}],"done":true,"next_cursor":3}` + "\n\n"))
	}))
	defer server.Close()

	client := New(server.URL)
	var batches []protocol.SessionRunEventsBatch
	err := client.SessionRunEvents(
		context.Background(),
		protocol.SessionRunEventsRequest{
			PeerToken:       "peer-token",
			SessionRunID:    "run-1",
			BranchBindingID: "main",
			Cursor:          1,
			TimeoutSec:      2,
		},
		func(batch protocol.SessionRunEventsBatch) error {
			batches = append(batches, batch)
			return nil
		},
	)

	if err != nil {
		t.Fatalf("SessionRunEvents returned error: %v", err)
	}
	if requestPath != "/remote/session-runs/events" {
		t.Fatalf("path = %s, want /remote/session-runs/events", requestPath)
	}
	if requestBody.PeerToken != "peer-token" || requestBody.SessionRunID != "run-1" || requestBody.BranchBindingID != "main" || requestBody.Cursor != 1 {
		t.Fatalf("request body = %+v", requestBody)
	}
	if len(batches) != 2 {
		t.Fatalf("got %d batches, want 2: %+v", len(batches), batches)
	}
	if batches[0].Events[0].Type != "assistant_delta" || batches[0].NextCursor != 2 || batches[0].Done {
		t.Fatalf("first batch = %+v", batches[0])
	}
	if batches[1].Events[0].Type != "session_run_end" || batches[1].NextCursor != 3 || !batches[1].Done {
		t.Fatalf("second batch = %+v", batches[1])
	}
}

func TestSessionRunEventsReturnsHTTPError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "bad peer", http.StatusUnauthorized)
	}))
	defer server.Close()

	err := New(server.URL).SessionRunEvents(
		context.Background(),
		protocol.SessionRunEventsRequest{PeerToken: "bad", SessionRunID: "run-1"},
		func(protocol.SessionRunEventsBatch) error { return nil },
	)

	if err == nil {
		t.Fatal("SessionRunEvents returned nil error")
	}
}

func TestLocalActionClaimReturnsStructuredHTTPError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "service unavailable", http.StatusServiceUnavailable)
	}))
	defer server.Close()

	_, err := New(server.URL).ClaimLocalActions(
		context.Background(),
		protocol.LocalActionClaimRequest{
			PeerToken:  "peer-token",
			PeerID:     "peer-1",
			WorkerKind: "local_peer",
			Features:   []string{"local_actions"},
		},
	)
	if err == nil {
		t.Fatal("ClaimLocalActions returned nil error")
	}

	var httpErr *HTTPError
	if !errors.As(err, &httpErr) {
		t.Fatalf("error = %T %[1]v, want HTTPError", err)
	}
	if httpErr.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want %d", httpErr.StatusCode, http.StatusServiceUnavailable)
	}
	if !strings.Contains(httpErr.Body, "service unavailable") {
		t.Fatalf("body = %q, want service unavailable", httpErr.Body)
	}
}

func TestLocalActionClientUsesTypedEndpoints(t *testing.T) {
	var claimBody protocol.LocalActionClaimRequest
	var progressBody protocol.LocalActionProgressRequest
	var completeBody protocol.LocalActionCompleteRequest
	var cancelBody protocol.LocalActionCancelRequest
	seen := map[string]bool{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen[r.URL.Path] = true
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/remote/local-actions/claim":
			if err := json.NewDecoder(r.Body).Decode(&claimBody); err != nil {
				t.Fatalf("decode claim: %v", err)
			}
			_ = json.NewEncoder(w).Encode(protocol.LocalActionClaimResponse{
				Actions: []protocol.LocalActionRecord{{
					Scope:         "activation_scoped",
					LocalActionID: "local-action-1",
					ActionKind:    "read_workspace_file",
					Status:        "started",
					LeaseID:       "lease-1",
					Payload:       map[string]any{"args": map[string]any{"path": "README.md"}},
				}},
			})
		case "/remote/local-actions/progress":
			if err := json.NewDecoder(r.Body).Decode(&progressBody); err != nil {
				t.Fatalf("decode progress: %v", err)
			}
			_ = json.NewEncoder(w).Encode(protocol.LocalActionProgressResponse{OK: true})
		case "/remote/local-actions/complete":
			if err := json.NewDecoder(r.Body).Decode(&completeBody); err != nil {
				t.Fatalf("decode complete: %v", err)
			}
			_ = json.NewEncoder(w).Encode(protocol.LocalActionCompleteResponse{OK: true})
		case "/remote/local-actions/cancel":
			if err := json.NewDecoder(r.Body).Decode(&cancelBody); err != nil {
				t.Fatalf("decode cancel: %v", err)
			}
			_ = json.NewEncoder(w).Encode(protocol.LocalActionCancelResponse{OK: true})
		default:
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
	}))
	defer server.Close()

	httpClient := New(server.URL)
	claim, err := httpClient.ClaimLocalActions(context.Background(), protocol.LocalActionClaimRequest{
		PeerToken:     "peer-token",
		PeerID:        "peer-1",
		WorkerKind:    "local_peer",
		Features:      []string{"local_actions", "local_action:read_workspace_file"},
		WorkspaceRoot: "D:/repo",
		MaxActions:    1,
	})
	if err != nil {
		t.Fatalf("ClaimLocalActions returned error: %v", err)
	}
	if len(claim.Actions) != 1 || claim.Actions[0].LocalActionID != "local-action-1" {
		t.Fatalf("claim response = %#v", claim)
	}
	_, err = httpClient.ReportLocalActionProgress(context.Background(), protocol.LocalActionProgressRequest{
		PeerToken:     "peer-token",
		LocalActionID: "local-action-1",
		LeaseID:       "lease-1",
		Status:        "progress",
		Progress:      map[string]any{"chunk_type": "stdout", "data": "hello"},
	})
	if err != nil {
		t.Fatalf("ReportLocalActionProgress returned error: %v", err)
	}
	_, err = httpClient.CompleteLocalAction(context.Background(), protocol.LocalActionCompleteRequest{
		PeerToken:     "peer-token",
		LocalActionID: "local-action-1",
		LeaseID:       "lease-1",
		Status:        "completed",
		Result:        map[string]any{"result": "ok"},
	})
	if err != nil {
		t.Fatalf("CompleteLocalAction returned error: %v", err)
	}
	_, err = httpClient.CancelLocalAction(context.Background(), protocol.LocalActionCancelRequest{
		PeerToken:     "peer-token",
		LocalActionID: "local-action-1",
		LeaseID:       "lease-1",
		Reason:        "cancelled",
	})
	if err != nil {
		t.Fatalf("CancelLocalAction returned error: %v", err)
	}

	for _, path := range []string{
		"/remote/local-actions/claim",
		"/remote/local-actions/progress",
		"/remote/local-actions/complete",
		"/remote/local-actions/cancel",
	} {
		if !seen[path] {
			t.Fatalf("endpoint %s was not called; seen=%#v", path, seen)
		}
	}
	if claimBody.PeerID != "peer-1" || claimBody.WorkerKind != "local_peer" || claimBody.Features[1] != "local_action:read_workspace_file" {
		t.Fatalf("claim body = %#v", claimBody)
	}
	if progressBody.LocalActionID != "local-action-1" || progressBody.LeaseID != "lease-1" {
		t.Fatalf("progress body = %#v", progressBody)
	}
	if completeBody.Status != "completed" || completeBody.Result["result"] != "ok" {
		t.Fatalf("complete body = %#v", completeBody)
	}
	if cancelBody.Reason != "cancelled" {
		t.Fatalf("cancel body = %#v", cancelBody)
	}
}

func TestAgentRunActivationHeartbeatRoundTripsSteerDelivery(t *testing.T) {
	var requestBody protocol.AgentRunActivationHeartbeatRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/remote/agent-run-activations/heartbeat" {
			t.Fatalf("path = %s", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&requestBody); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(protocol.AgentRunActivationHeartbeatResponse{
			OK:              true,
			CancelRequested: false,
			ActivationSteers: []protocol.ActivationSteer{
				{
					ID:           "steer-1",
					ActivationID: "run-1:activation:1",
					Source:       "user",
					Payload: map[string]any{
						"items": []any{
							map[string]any{"type": "text", "text": "add context"},
						},
					},
					Status: "delivering",
				},
			},
		})
	}))
	defer server.Close()

	resp, err := New(server.URL).AgentRunActivationHeartbeat(
		context.Background(),
		protocol.AgentRunActivationHeartbeatRequest{
			PeerToken:         "peer-token",
			RequestID:         "claim-1",
			ActivationID:      "run-1:activation:1",
			TaskID:            "run-1",
			WorkerID:          "worker-1",
			LeaseSec:          15,
			DeliveredSteerIDs: []string{"steer-0"},
		},
	)

	if err != nil {
		t.Fatalf("heartbeat returned error: %v", err)
	}
	if requestBody.DeliveredSteerIDs[0] != "steer-0" {
		t.Fatalf("delivered_steer_ids = %#v", requestBody.DeliveredSteerIDs)
	}
	if len(resp.ActivationSteers) != 1 || resp.ActivationSteers[0].ID != "steer-1" {
		t.Fatalf("activation steers = %#v", resp.ActivationSteers)
	}
	if resp.ActivationSteers[0].Payload["items"] == nil {
		t.Fatalf("steer payload was not decoded: %#v", resp.ActivationSteers[0])
	}
}

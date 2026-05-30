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
			PeerToken:    "peer-token",
			SessionRunID: "run-1",
			Cursor:       1,
			TimeoutSec:   2,
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
	if requestBody.PeerToken != "peer-token" || requestBody.SessionRunID != "run-1" || requestBody.Cursor != 1 {
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

func TestPollReturnsStructuredHTTPError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "service unavailable", http.StatusServiceUnavailable)
	}))
	defer server.Close()

	_, err := New(server.URL).Poll(
		context.Background(),
		protocol.PollRequest{PeerToken: "peer-token"},
	)
	if err == nil {
		t.Fatal("Poll returned nil error")
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

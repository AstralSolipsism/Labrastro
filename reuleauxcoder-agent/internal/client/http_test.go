package client

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestChatEventsStreamsSSEBatches(t *testing.T) {
	var requestPath string
	var requestBody protocol.ChatEventsRequest
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
		_, _ = w.Write([]byte("event: chat\n"))
		_, _ = w.Write([]byte(`data: {"events":[{"type":"assistant_delta","payload":{"content":"hello"}}],"done":false,"next_cursor":2}` + "\n\n"))
		_, _ = w.Write([]byte("event: chat\n"))
		_, _ = w.Write([]byte(`data: {"events":[{"type":"chat_end","payload":{"response":"ok"}}],"done":true,"next_cursor":3}` + "\n\n"))
	}))
	defer server.Close()

	client := New(server.URL)
	var batches []protocol.ChatEventsBatch
	err := client.ChatEvents(
		context.Background(),
		protocol.ChatEventsRequest{
			PeerToken:  "peer-token",
			ChatID:     "chat-1",
			Cursor:     1,
			TimeoutSec: 2,
		},
		func(batch protocol.ChatEventsBatch) error {
			batches = append(batches, batch)
			return nil
		},
	)

	if err != nil {
		t.Fatalf("ChatEvents returned error: %v", err)
	}
	if requestPath != "/remote/chat/events" {
		t.Fatalf("path = %s, want /remote/chat/events", requestPath)
	}
	if requestBody.PeerToken != "peer-token" || requestBody.ChatID != "chat-1" || requestBody.Cursor != 1 {
		t.Fatalf("request body = %+v", requestBody)
	}
	if len(batches) != 2 {
		t.Fatalf("got %d batches, want 2: %+v", len(batches), batches)
	}
	if batches[0].Events[0].Type != "assistant_delta" || batches[0].NextCursor != 2 || batches[0].Done {
		t.Fatalf("first batch = %+v", batches[0])
	}
	if batches[1].Events[0].Type != "chat_end" || batches[1].NextCursor != 3 || !batches[1].Done {
		t.Fatalf("second batch = %+v", batches[1])
	}
}

func TestChatEventsReturnsHTTPError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "bad peer", http.StatusUnauthorized)
	}))
	defer server.Close()

	err := New(server.URL).ChatEvents(
		context.Background(),
		protocol.ChatEventsRequest{PeerToken: "bad", ChatID: "chat-1"},
		func(protocol.ChatEventsBatch) error { return nil },
	)

	if err == nil {
		t.Fatal("ChatEvents returned nil error")
	}
}

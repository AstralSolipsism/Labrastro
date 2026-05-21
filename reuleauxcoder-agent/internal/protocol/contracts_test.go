package protocol

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

type contractDoc struct {
	Fixtures []contractFixture `json:"fixtures"`
}

type contractFixture struct {
	Name     string          `json:"name"`
	Request  json.RawMessage `json:"request"`
	Response json.RawMessage `json:"response"`
}

func TestRemoteContractFixturesDecodePeerProtocolSamples(t *testing.T) {
	fixtures := loadContractFixtures(t)

	mustDecode(t, fixtures["peer.register"].Request, &RegisterRequest{})
	mustDecode(t, fixtures["peer.register"].Response, &RegisterResponseEnvelope{})
	mustDecode(t, fixtures["peer.heartbeat"].Request, &Heartbeat{})
	mustDecode(t, fixtures["chat.start"].Request, &ChatStartRequest{})
	mustDecode(t, fixtures["chat.start"].Response, &ChatStartResponse{})
	mustDecode(t, fixtures["chat.events"].Request, &ChatEventsRequest{})
	mustDecode(t, fixtures["chat.events"].Response, &ChatEventsBatch{})
	mustDecode(t, fixtures["agent_runs.claim"].Request, &AgentRunClaimRequest{})
	mustDecode(t, fixtures["agent_runs.claim"].Response, &AgentRunClaimResponse{})
	mustDecode(t, fixtures["agent_runs.events"].Response, &AgentRunEventsResponse{})
	mustDecode(t, fixtures["agent_runs.complete"].Request, &AgentRunCompleteRequest{})
	mustDecode(t, fixtures["agent_runs.complete"].Response, &AgentRunCompleteResponse{})
	mustDecode(t, fixtures["environment.manifest"].Request, &EnvironmentManifestRequest{})
	mustDecode(t, fixtures["environment.manifest"].Response, &EnvironmentManifestResponse{})
	mustDecode(t, fixtures["error.invalid_peer_token"].Response, &ErrorResponse{})
}

func TestChatStartRequestMarshalsTaskflowID(t *testing.T) {
	raw, err := json.Marshal(ChatStartRequest{
		PeerToken:  "pt_1",
		Prompt:     "continue",
		TaskflowID: "taskflow-1",
	})
	if err != nil {
		t.Fatalf("marshal ChatStartRequest: %v", err)
	}

	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("decode marshaled payload: %v", err)
	}
	if payload["taskflow_id"] != "taskflow-1" {
		t.Fatalf("taskflow_id = %v, want taskflow-1", payload["taskflow_id"])
	}
	if _, ok := payload["taskflow_goal_id"]; ok {
		t.Fatalf("unexpected legacy taskflow_goal_id in payload: %s", raw)
	}
}

func loadContractFixtures(t *testing.T) map[string]contractFixture {
	t.Helper()
	path := filepath.Join("..", "..", "..", "labrastro_server", "interfaces", "http", "remote", "protocol", "contracts.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read contract fixtures: %v", err)
	}
	var doc contractDoc
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("decode contract fixtures: %v", err)
	}
	fixtures := make(map[string]contractFixture, len(doc.Fixtures))
	for _, fixture := range doc.Fixtures {
		fixtures[fixture.Name] = fixture
	}
	return fixtures
}

func mustDecode(t *testing.T, raw json.RawMessage, target any) {
	t.Helper()
	if len(raw) == 0 {
		t.Fatalf("missing fixture body for %T", target)
	}
	if err := json.Unmarshal(raw, target); err != nil {
		t.Fatalf("decode %T: %v", target, err)
	}
}

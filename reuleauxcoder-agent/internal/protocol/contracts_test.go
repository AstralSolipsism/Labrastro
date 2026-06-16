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
	mustDecode(t, fixtures["session_run.start"].Request, &SessionRunStartRequest{})
	mustDecode(t, fixtures["session_run.start"].Response, &SessionRunStartResponse{})
	mustDecode(t, fixtures["session_run.events"].Request, &SessionRunEventsRequest{})
	mustDecode(t, fixtures["session_run.events"].Response, &SessionRunEventsBatch{})
	mustDecode(t, fixtures["agent_run_activations.claim"].Request, &AgentRunActivationClaimRequest{})
	mustDecode(t, fixtures["agent_run_activations.claim"].Response, &AgentRunActivationClaimResponse{})
	mustDecode(t, fixtures["agent_runs.events"].Response, &AgentRunEventsResponse{})
	mustDecode(t, fixtures["agent_run_activations.complete"].Request, &AgentRunActivationCompleteRequest{})
	mustDecode(t, fixtures["agent_run_activations.complete"].Response, &AgentRunActivationCompleteResponse{})
	mustDecode(t, fixtures["environment.manifest"].Request, &EnvironmentManifestRequest{})
	mustDecode(t, fixtures["environment.manifest"].Response, &EnvironmentManifestResponse{})
	mustDecode(t, fixtures["error.invalid_peer_token"].Response, &ErrorResponse{})
}

func TestSessionRunStartRequestMarshalsOptionalStartContext(t *testing.T) {
	raw, err := json.Marshal(SessionRunStartRequest{
		PeerToken:       "pt_1",
		Prompt:          "continue",
		SessionHint:     "session-1",
		ClientRequestID: "request-1",
		Mode:            "chat",
		WorkflowMode:    "taskflow",
		TaskflowID:      "taskflow-1",
		ProviderID:      "deepseek",
		ModelID:         "V4FLASH",
		Parameters:      map[string]any{"max_context_tokens": float64(1000000)},
		Locale:          "zh-CN",
		Mentions: []map[string]any{
			{"kind": "file", "name": "README.md", "path": "README.md"},
		},
	})
	if err != nil {
		t.Fatalf("marshal SessionRunStartRequest: %v", err)
	}

	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		t.Fatalf("decode marshaled payload: %v", err)
	}
	if payload["taskflow_id"] != "taskflow-1" {
		t.Fatalf("taskflow_id = %v, want taskflow-1", payload["taskflow_id"])
	}
	if payload["client_request_id"] != "request-1" {
		t.Fatalf("client_request_id = %v, want request-1", payload["client_request_id"])
	}
	if payload["provider_id"] != "deepseek" {
		t.Fatalf("provider_id = %v, want deepseek", payload["provider_id"])
	}
	if payload["model_id"] != "V4FLASH" {
		t.Fatalf("model_id = %v, want V4FLASH", payload["model_id"])
	}
	if payload["locale"] != "zh-CN" {
		t.Fatalf("locale = %v, want zh-CN", payload["locale"])
	}
	if _, ok := payload["parameters"].(map[string]any); !ok {
		t.Fatalf("parameters missing from payload: %s", raw)
	}
	mentions, ok := payload["mentions"].([]any)
	if !ok || len(mentions) != 1 {
		t.Fatalf("mentions = %v, want one item", payload["mentions"])
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

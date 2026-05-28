package runner

import (
	"testing"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestExecToolProtocolErrorRequiresToolCallID(t *testing.T) {
	result, invalid := execToolProtocolError(protocol.ExecToolRequest{
		ToolName: "shell",
		Args: map[string]any{
			"command": "echo should-not-run",
		},
	})

	if !invalid {
		t.Fatal("missing tool_call_id should be invalid")
	}
	if result.OK || result.ErrorCode != "REMOTE_PROTOCOL_ERROR" {
		t.Fatalf("result = %#v, want protocol error", result)
	}
	if result.ErrorMessage == "" {
		t.Fatalf("missing protocol error message: %#v", result)
	}
}

func TestAttachToolCallIDToStreamChunk(t *testing.T) {
	chunk := attachToolCallIDToStreamChunk(protocol.ToolStreamChunk{
		ChunkType: "stdout",
		Data:      "hello",
	}, "call-1")

	if chunk.ToolCallID != "call-1" {
		t.Fatalf("chunk.ToolCallID = %q, want call-1", chunk.ToolCallID)
	}
	if chunk.Meta["tool_call_id"] != "call-1" {
		t.Fatalf("chunk meta = %#v, want tool_call_id", chunk.Meta)
	}
}

func TestBaseFeaturesAdvertisesLSPOnlyWhenAvailable(t *testing.T) {
	withLSP := baseFeatures(true)
	withoutLSP := baseFeatures(false)

	if !containsFeature(withLSP, "lsp") {
		t.Fatalf("features = %#v, want lsp", withLSP)
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

func containsFeature(features []string, target string) bool {
	for _, feature := range features {
		if feature == target {
			return true
		}
	}
	return false
}

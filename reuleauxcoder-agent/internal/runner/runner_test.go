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

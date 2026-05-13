package protocol

import (
	"encoding/json"
	"testing"
)

func TestToolStreamChunkIncludesToolCallID(t *testing.T) {
	chunk := ToolStreamChunk{
		ChunkType:  "stdout",
		Data:       "hello",
		ToolCallID: "call-1",
		Meta: map[string]any{
			"seq": float64(1),
		},
	}

	payload := mapFromTestStruct(t, chunk)

	if payload["tool_call_id"] != "call-1" {
		t.Fatalf("payload tool_call_id = %#v, want call-1", payload["tool_call_id"])
	}
}

func mapFromTestStruct(t *testing.T, value any) map[string]any {
	t.Helper()
	buf, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	payload := map[string]any{}
	if err := json.Unmarshal(buf, &payload); err != nil {
		t.Fatal(err)
	}
	return payload
}

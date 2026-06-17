package agentruntime

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"sync"
)

type steerBuffer struct {
	cancel context.CancelFunc
	done   chan struct{}
	mu     sync.Mutex
	items  []Steer
}

func startSteerBuffer(ctx context.Context, steers <-chan Steer) *steerBuffer {
	if steers == nil {
		return nil
	}
	bufferCtx, cancel := context.WithCancel(ctx)
	buffer := &steerBuffer{
		cancel: cancel,
		done:   make(chan struct{}),
	}
	go func() {
		defer close(buffer.done)
		for {
			select {
			case <-bufferCtx.Done():
				return
			case steer, ok := <-steers:
				if !ok {
					return
				}
				if strings.TrimSpace(steer.ID) == "" {
					continue
				}
				buffer.mu.Lock()
				buffer.items = append(buffer.items, steer)
				buffer.mu.Unlock()
			}
		}
	}()
	return buffer
}

func (b *steerBuffer) drain() []Steer {
	if b == nil {
		return nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.items) == 0 {
		return nil
	}
	items := append([]Steer{}, b.items...)
	b.items = nil
	return items
}

func (b *steerBuffer) stop() {
	if b == nil {
		return
	}
	b.cancel()
	<-b.done
}

func promptFromSteers(steers []Steer) string {
	lines := []string{}
	for _, steer := range steers {
		for _, item := range steerPayloadItems(steer.Payload) {
			itemType := strings.TrimSpace(fmt.Sprint(item["type"]))
			if itemType == "" {
				continue
			}
			if itemType == "text" {
				text := strings.TrimSpace(fmt.Sprint(item["text"]))
				if text != "" {
					lines = append(lines, text)
				}
				continue
			}
			ref := firstSteerItemString(item, "uri", "id", "ref", "path", "artifact_id", "session_item_id")
			if ref == "" {
				if raw, err := json.Marshal(item); err == nil {
					ref = string(raw)
				}
			}
			if ref != "" {
				lines = append(lines, itemType+": "+ref)
			}
		}
	}
	return strings.Join(lines, "\n\n")
}

func firstSteerItemString(item map[string]any, keys ...string) string {
	for _, key := range keys {
		value := strings.TrimSpace(fmt.Sprint(item[key]))
		if value != "" && value != "<nil>" {
			return value
		}
	}
	return ""
}

func steerPayloadItems(payload map[string]any) []map[string]any {
	rawItems, ok := payload["items"].([]any)
	if !ok {
		return nil
	}
	items := make([]map[string]any, 0, len(rawItems))
	for _, raw := range rawItems {
		item, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		items = append(items, item)
	}
	return items
}

func cloneAnyMap(value map[string]any) map[string]any {
	out := make(map[string]any, len(value))
	for key, item := range value {
		out[key] = item
	}
	return out
}

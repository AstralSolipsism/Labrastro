package agentruntime

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type EventType string

const (
	EventText       EventType = "text"
	EventThinking   EventType = "thinking"
	EventToolUse    EventType = "tool_use"
	EventToolResult EventType = "tool_result"
	EventStatus     EventType = "status"
	EventError      EventType = "error"
	EventLog        EventType = "log"
	EventResult     EventType = "result"
)

type Event struct {
	Type EventType      `json:"type"`
	Text string         `json:"text,omitempty"`
	Data map[string]any `json:"data,omitempty"`
}

type TokenUsage struct {
	InputTokens      int64 `json:"input_tokens,omitempty"`
	OutputTokens     int64 `json:"output_tokens,omitempty"`
	CacheReadTokens  int64 `json:"cache_read_tokens,omitempty"`
	CacheWriteTokens int64 `json:"cache_write_tokens,omitempty"`
}

type RunRequest struct {
	TaskID             string         `json:"task_id"`
	AgentID            string         `json:"agent_id"`
	Executor           string         `json:"executor"`
	Prompt             string         `json:"prompt"`
	ExecutionLocation  string         `json:"execution_location,omitempty"`
	IssueID            string         `json:"issue_id,omitempty"`
	RuntimeProfileID   string         `json:"runtime_profile_id,omitempty"`
	WorkerKind         string         `json:"worker_kind,omitempty"`
	ModelRequestOrigin string         `json:"model_request_origin,omitempty"`
	Workdir            string         `json:"workdir,omitempty"`
	Branch             string         `json:"branch,omitempty"`
	Model              string         `json:"model,omitempty"`
	ExecutorSessionID  string         `json:"executor_session_id,omitempty"`
	Metadata           map[string]any `json:"metadata,omitempty"`
}

type RunOptions struct {
	Timeout          time.Duration
	Command          string
	SystemPrompt     string
	ExtraArgs        []string
	CustomArgs       []string
	Env              map[string]string
	ApprovalMode     string
	MCPConfigJSON    []byte
	RuntimeHome      string
	SemanticIdleTime time.Duration
	EventSink        func(Event)
}

type RunResult struct {
	TaskID            string                `json:"task_id"`
	Status            string                `json:"status"`
	Output            string                `json:"output,omitempty"`
	Error             string                `json:"error,omitempty"`
	ExecutorSessionID string                `json:"executor_session_id,omitempty"`
	Usage             map[string]TokenUsage `json:"usage,omitempty"`
	Events            []Event               `json:"events,omitempty"`
}

type Backend interface {
	Execute(ctx context.Context, req RunRequest, opts RunOptions) (RunResult, error)
}

type Session struct {
	Events <-chan Event
	Result <-chan RunResult
}

type StreamingBackend interface {
	Start(ctx context.Context, req RunRequest, opts RunOptions) (*Session, error)
	Execute(ctx context.Context, req RunRequest, opts RunOptions) (RunResult, error)
}

type FakeBackend struct {
	Output string
}

func (b FakeBackend) Start(ctx context.Context, req RunRequest, opts RunOptions) (*Session, error) {
	return startExecuteSession(ctx, req, opts, b.Execute), nil
}

func (b FakeBackend) Execute(ctx context.Context, req RunRequest, opts RunOptions) (RunResult, error) {
	output := b.Output
	if output == "" {
		output = req.Prompt
	}
	events := []Event{
		{Type: EventStatus, Data: map[string]any{"status": "running"}},
	}
	emitEvent(opts, events[0])
	if delay := metadataDurationSeconds(req.Metadata, "fake_sleep_sec"); delay > 0 {
		select {
		case <-ctx.Done():
			event := Event{Type: EventStatus, Data: map[string]any{"status": "cancelled"}}
			events = append(events, event)
			emitEvent(opts, event)
			return RunResult{
				TaskID: req.TaskID,
				Status: "cancelled",
				Output: output,
				Error:  ctx.Err().Error(),
				Events: events,
			}, ctx.Err()
		case <-time.After(delay):
		}
	}
	textEvent := Event{Type: EventText, Text: output}
	events = append(events, textEvent)
	emitEvent(opts, textEvent)
	if err := writeFakeFiles(req); err != nil {
		events = append(events, Event{Type: EventStatus, Data: map[string]any{"status": "failed", "error": err.Error()}})
		emitEvent(opts, events[len(events)-1])
		return RunResult{
			TaskID: req.TaskID,
			Status: "failed",
			Output: output,
			Error:  err.Error(),
			Events: events,
		}, err
	}
	events = append(events, Event{Type: EventStatus, Data: map[string]any{"status": "completed"}})
	emitEvent(opts, events[len(events)-1])
	return RunResult{
		TaskID: req.TaskID,
		Status: "completed",
		Output: output,
		Events: events,
	}, nil
}

func writeFakeFiles(req RunRequest) error {
	if len(req.Metadata) == 0 {
		return nil
	}
	raw, ok := req.Metadata["fake_files"]
	if !ok || raw == nil {
		raw = req.Metadata["fake_changes"]
	}
	files, ok := raw.(map[string]any)
	if !ok || len(files) == 0 {
		return nil
	}
	if strings.TrimSpace(req.Workdir) == "" {
		return fmt.Errorf("fake_files requires a workdir")
	}
	root, err := filepath.Abs(req.Workdir)
	if err != nil {
		return err
	}
	for name, value := range files {
		relative := filepath.Clean(strings.TrimSpace(name))
		if relative == "." || filepath.IsAbs(relative) || strings.HasPrefix(relative, ".."+string(filepath.Separator)) || relative == ".." {
			return fmt.Errorf("fake file path escapes workdir: %s", name)
		}
		target := filepath.Join(root, relative)
		if err := assertChildPath(root, target); err != nil {
			return err
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(target, []byte(fmt.Sprint(value)), 0o600); err != nil {
			return err
		}
	}
	return nil
}

func metadataDurationSeconds(metadata map[string]any, key string) time.Duration {
	if len(metadata) == 0 {
		return 0
	}
	value, ok := metadata[key]
	if !ok || value == nil {
		return 0
	}
	var seconds float64
	switch typed := value.(type) {
	case int:
		seconds = float64(typed)
	case int64:
		seconds = float64(typed)
	case float64:
		seconds = typed
	case string:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(typed), 64)
		if err != nil {
			return 0
		}
		seconds = parsed
	default:
		parsed, err := strconv.ParseFloat(strings.TrimSpace(fmt.Sprint(value)), 64)
		if err != nil {
			return 0
		}
		seconds = parsed
	}
	if seconds <= 0 {
		return 0
	}
	return time.Duration(seconds * float64(time.Second))
}

func emitEvent(opts RunOptions, event Event) {
	if opts.EventSink != nil {
		opts.EventSink(event)
	}
}

func startExecuteSession(
	ctx context.Context,
	req RunRequest,
	opts RunOptions,
	execute func(context.Context, RunRequest, RunOptions) (RunResult, error),
) *Session {
	events := make(chan Event, 32)
	results := make(chan RunResult, 1)
	originalSink := opts.EventSink
	opts.EventSink = func(event Event) {
		if originalSink != nil {
			originalSink(event)
		}
		select {
		case events <- event:
		case <-ctx.Done():
		}
	}
	go func() {
		defer close(events)
		defer close(results)
		result, err := execute(ctx, req, opts)
		if result.TaskID == "" {
			result.TaskID = req.TaskID
		}
		if err != nil && result.Error == "" {
			result.Error = err.Error()
		}
		if result.Status == "" {
			if err != nil {
				result.Status = "failed"
			} else {
				result.Status = "completed"
			}
		}
		results <- result
	}()
	return &Session{Events: events, Result: results}
}

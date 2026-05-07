package agentruntime

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestMain(m *testing.M) {
	if fixture := os.Getenv("AGENTRUNTIME_HELPER_FIXTURE"); fixture != "" {
		f, err := os.Open(fixture)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		_, _ = io.Copy(os.Stdout, f)
		_ = f.Close()
		os.Exit(0)
	}
	if os.Getenv("AGENTRUNTIME_HELPER_STREAM") == "1" {
		fmt.Println(`{"type":"thinking","message":"plan"}`)
		fmt.Println(`{"type":"text","text":"hello"}`)
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func TestBuildInvocationFiltersProtocolCriticalArgs(t *testing.T) {
	req := RunRequest{
		TaskID:            "task-1",
		AgentID:           "coder",
		Executor:          "claude",
		Prompt:            "hello",
		Model:             "claude-sonnet",
		Workdir:           "/tmp/work",
		ExecutorSessionID: "claude-session-1",
	}
	inv, err := BuildInvocation(req, RunOptions{
		SystemPrompt: "system",
		CustomArgs: []string{
			"--output-format", "text",
			"--model", "hijacked-model",
			"--resume", "hijacked-session",
			"--dangerously-skip-permissions",
		},
	})
	if err != nil {
		t.Fatalf("BuildInvocation error: %v", err)
	}
	joined := strings.Join(inv.Args, " ")
	if strings.Contains(joined, "text") || strings.Contains(joined, "hijacked") {
		t.Fatalf("filtered value leaked into args: %v", inv.Args)
	}
	if countArg(inv.Args, "--resume") != 1 {
		t.Fatalf("daemon-managed resume should appear exactly once: %v", inv.Args)
	}
	if !strings.Contains(joined, "--dangerously-skip-permissions") {
		t.Fatalf("safe custom arg missing: %v", inv.Args)
	}
	if inv.Transport != "stream_json" {
		t.Fatalf("transport = %q", inv.Transport)
	}
}

func TestBuildGeminiInvocationFiltersProtocolCriticalArgs(t *testing.T) {
	inv, err := BuildInvocation(RunRequest{
		Executor:          "gemini",
		Prompt:            "hello",
		Model:             "gemini-pro",
		ExecutorSessionID: "gemini-session-1",
	}, RunOptions{
		CustomArgs: []string{
			"--prompt", "hijacked prompt",
			"--output-format", "text",
			"--resume", "hijacked-session",
			"--model", "hijacked-model",
			"--debug",
		},
	})
	if err != nil {
		t.Fatalf("BuildInvocation error: %v", err)
	}
	joined := strings.Join(inv.Args, " ")
	if strings.Contains(joined, "hijacked") || strings.Contains(joined, "text") {
		t.Fatalf("filtered gemini value leaked: %v", inv.Args)
	}
	if countArg(inv.Args, "-r") != 1 {
		t.Fatalf("daemon-managed -r should appear exactly once: %v", inv.Args)
	}
	if !strings.Contains(joined, "--debug") {
		t.Fatalf("safe custom arg missing: %v", inv.Args)
	}
}

func TestBuildCodexInvocationUsesAppServerTransport(t *testing.T) {
	inv, err := BuildInvocation(RunRequest{Executor: "codex"}, RunOptions{
		RuntimeHome: "/tmp/codex-home",
		CustomArgs:  []string{"--listen", "tcp://0.0.0.0:1", "--color", "never"},
	})
	if err != nil {
		t.Fatalf("BuildInvocation error: %v", err)
	}
	if inv.Command != "codex" || inv.Transport != "jsonrpc_stdio" {
		t.Fatalf("unexpected invocation: %#v", inv)
	}
	joined := strings.Join(inv.Args, " ")
	if strings.Contains(joined, "tcp://") {
		t.Fatalf("blocked listen value leaked: %v", inv.Args)
	}
	if inv.Env["CODEX_HOME"] != "/tmp/codex-home" {
		t.Fatalf("CODEX_HOME not set: %#v", inv.Env)
	}
}

func TestBuildReuleauxCoderInvocationUsesOneShotPrompt(t *testing.T) {
	inv, err := BuildInvocation(RunRequest{
		Executor: "reuleauxcoder",
		Prompt:   "check environment",
		Model:    "gpt-5.2",
		Workdir:  "/tmp/work",
	}, RunOptions{
		Command:    "rcoder-dev",
		CustomArgs: []string{"--prompt", "override", "--config", "/tmp/config.yaml"},
	})
	if err != nil {
		t.Fatalf("BuildInvocation error: %v", err)
	}
	if inv.Command != "rcoder-dev" || inv.Transport != "plain_stdout" {
		t.Fatalf("unexpected invocation: %#v", inv)
	}
	joined := strings.Join(inv.Args, " ")
	if strings.Contains(joined, "override") {
		t.Fatalf("blocked prompt override leaked: %v", inv.Args)
	}
	if !strings.Contains(joined, "--prompt check environment") || !strings.Contains(joined, "--model gpt-5.2") {
		t.Fatalf("missing rcoder prompt/model args: %v", inv.Args)
	}
	if !strings.Contains(joined, "--config /tmp/config.yaml") {
		t.Fatalf("custom config arg missing: %v", inv.Args)
	}
}

func TestBuildInvocationRejectsUnsupportedExecutor(t *testing.T) {
	_, err := BuildInvocation(RunRequest{Executor: "unknown-cli"}, RunOptions{})
	if err == nil || !strings.Contains(err.Error(), "unsupported executor") {
		t.Fatalf("expected unsupported executor error, got %v", err)
	}
}

func TestNormalizeStreamLineMapsCommonCLIEvents(t *testing.T) {
	cases := []struct {
		line string
		want EventType
	}{
		{`{"type":"tool_use","name":"read_file"}`, EventToolUse},
		{`{"type":"tool-result","content":"ok"}`, EventToolResult},
		{`{"type":"error","message":"bad"}`, EventError},
		{`{"type":"result","usage":{"input_tokens":1}}`, EventResult},
		{`{"type":"thinking","message":"plan"}`, EventThinking},
		{`{"text":"hello"}`, EventText},
	}
	for _, tc := range cases {
		if got := normalizeStreamLine("claude", tc.line); got.Type != tc.want {
			t.Fatalf("line %s event type = %s want %s", tc.line, got.Type, tc.want)
		}
	}
}

func TestClaudeStreamParserExtractsSessionOutputToolsAndUsage(t *testing.T) {
	parser := parseFixture(t, "claude", "testdata/claude_fresh.jsonl")
	if parser.SessionID() != "claude-session-1" {
		t.Fatalf("session id = %q", parser.SessionID())
	}
	if parser.Output() != "hello done" {
		t.Fatalf("output = %q", parser.Output())
	}
	usage := parser.Usage()["claude-sonnet"]
	if usage.InputTokens != 10 || usage.OutputTokens != 5 || usage.CacheReadTokens != 2 || usage.CacheWriteTokens != 3 {
		t.Fatalf("usage = %#v", usage)
	}
}

func TestClaudeStreamParserMarksResultError(t *testing.T) {
	parser := parseFixture(t, "claude", "testdata/claude_error.jsonl")
	if parser.SessionID() != "claude-session-error" {
		t.Fatalf("session id = %q", parser.SessionID())
	}
	if parser.StatusOverride() != "failed" || parser.ErrorText() != "model not found" {
		t.Fatalf("status/error = %q %q", parser.StatusOverride(), parser.ErrorText())
	}
}

func TestGeminiStreamParserExtractsSessionOutputToolsAndUsage(t *testing.T) {
	parser := parseFixture(t, "gemini", "testdata/gemini_fresh.jsonl")
	if parser.SessionID() != "gemini-session-1" {
		t.Fatalf("session id = %q", parser.SessionID())
	}
	if parser.Output() != "hello " {
		t.Fatalf("output = %q", parser.Output())
	}
	usage := parser.Usage()["gemini-pro"]
	if usage.InputTokens != 13 || usage.OutputTokens != 8 || usage.CacheReadTokens != 4 {
		t.Fatalf("usage = %#v", usage)
	}
}

func TestGeminiStreamParserMarksResultError(t *testing.T) {
	parser := parseFixture(t, "gemini", "testdata/gemini_error.jsonl")
	if parser.SessionID() != "gemini-session-error" {
		t.Fatalf("session id = %q", parser.SessionID())
	}
	if parser.StatusOverride() != "failed" || parser.ErrorText() != "quota exceeded" {
		t.Fatalf("status/error = %q %q", parser.StatusOverride(), parser.ErrorText())
	}
}

func TestExecEnvManagerRejectsEscapingPromptFile(t *testing.T) {
	manager, err := NewExecEnvManager(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	plan, err := manager.Plan("workspace/one", "task:123", "coder")
	if err != nil {
		t.Fatal(err)
	}
	if plan.BranchName != "agent/coder/task-123" {
		t.Fatalf("branch = %q", plan.BranchName)
	}
	if err := manager.Prepare(plan, map[string]string{"../AGENTS.md": "bad"}); err == nil {
		t.Fatal("expected escaping prompt file to fail")
	}
}

func TestFakeBackendReturnsNormalizedEvents(t *testing.T) {
	sinkEvents := []Event{}
	result, err := FakeBackend{Output: "ok"}.Execute(
		context.Background(),
		RunRequest{TaskID: "task-1", Prompt: "ignored"},
		RunOptions{EventSink: func(event Event) {
			sinkEvents = append(sinkEvents, event)
		}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "completed" || len(result.Events) != 3 {
		t.Fatalf("unexpected result: %#v", result)
	}
	if result.Events[1].Type != EventText {
		t.Fatalf("event type = %s", result.Events[1].Type)
	}
	if len(sinkEvents) != len(result.Events) || sinkEvents[1].Text != "ok" {
		t.Fatalf("sink events = %#v", sinkEvents)
	}
}

func TestFakeBackendStartStreamsSessionEvents(t *testing.T) {
	session, err := FakeBackend{Output: "ok"}.Start(
		context.Background(),
		RunRequest{TaskID: "task-session", Prompt: "ignored"},
		RunOptions{},
	)
	if err != nil {
		t.Fatal(err)
	}
	var events []Event
	for event := range session.Events {
		events = append(events, event)
	}
	result := <-session.Result
	if result.Status != "completed" || result.Output != "ok" {
		t.Fatalf("unexpected result: %#v", result)
	}
	if len(events) != len(result.Events) || events[1].Text != "ok" {
		t.Fatalf("events = %#v result events = %#v", events, result.Events)
	}
}

func TestFakeBackendSleepCanBeCancelled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	session, err := FakeBackend{Output: "ok"}.Start(
		ctx,
		RunRequest{
			TaskID:   "task-sleep",
			Prompt:   "ignored",
			Metadata: map[string]any{"fake_sleep_sec": 5},
		},
		RunOptions{},
	)
	if err != nil {
		t.Fatal(err)
	}
	first := <-session.Events
	if first.Type != EventStatus || first.Data["status"] != "running" {
		t.Fatalf("first event = %#v", first)
	}
	cancel()
	result := <-session.Result
	if result.Status != "cancelled" {
		t.Fatalf("result = %#v", result)
	}
}

func TestSubprocessBackendStreamsEventsToSink(t *testing.T) {
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	sinkEvents := []Event{}
	result, err := SubprocessBackend{}.Execute(
		context.Background(),
		RunRequest{TaskID: "task-stream", Executor: "reuleauxcoder", Prompt: "ignored"},
		RunOptions{
			Command: executable,
			Env:     map[string]string{"AGENTRUNTIME_HELPER_STREAM": "1"},
			EventSink: func(event Event) {
				sinkEvents = append(sinkEvents, event)
			},
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.Status != "completed" || result.Output != "hello" {
		t.Fatalf("unexpected result: %#v", result)
	}
	if len(sinkEvents) != len(result.Events) {
		t.Fatalf("sink events = %#v result events = %#v", sinkEvents, result.Events)
	}
	if sinkEvents[0].Type != EventThinking || sinkEvents[1].Type != EventText || sinkEvents[2].Type != EventStatus {
		t.Fatalf("unexpected sink event order: %#v", sinkEvents)
	}
}

func TestSubprocessBackendReturnsExecutorSessionIDFromClaudeStream(t *testing.T) {
	fixture, err := filepath.Abs("testdata/claude_fresh.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	var sink []Event
	result, err := SubprocessBackend{}.Execute(
		context.Background(),
		RunRequest{TaskID: "task-claude", Executor: "claude", Prompt: "ignored"},
		RunOptions{
			Command: os.Args[0],
			Env: map[string]string{
				"AGENTRUNTIME_HELPER_FIXTURE": fixture,
			},
			EventSink: func(event Event) {
				sink = append(sink, event)
			},
		},
	)
	if err != nil {
		t.Fatalf("Execute error: %v", err)
	}
	if result.ExecutorSessionID != "claude-session-1" {
		t.Fatalf("executor session id = %q", result.ExecutorSessionID)
	}
	if result.Output != "hello done" {
		t.Fatalf("output = %q", result.Output)
	}
	if !hasSessionPinnedEvent(sink, "claude-session-1") {
		t.Fatalf("missing session pin event: %#v", sink)
	}
}

func parseFixture(t *testing.T, provider, path string) *streamParser {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	parser := newStreamParser(provider)
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		parser.ParseLine(line)
	}
	return parser
}

func hasSessionPinnedEvent(events []Event, sessionID string) bool {
	for _, event := range events {
		if event.Type == EventStatus &&
			event.Data["status"] == "session_pinned" &&
			event.Data["executor_session_id"] == sessionID {
			return true
		}
	}
	return false
}

func countArg(args []string, target string) int {
	count := 0
	for _, arg := range args {
		if arg == target {
			count++
		}
	}
	return count
}

func TestCodexAppServerNotificationsNormalizeEvents(t *testing.T) {
	sinkEvents := []Event{}
	rpc := &codexRPC{
		done:      make(chan struct{}),
		pending:   map[int]chan rpcResponse{},
		activity:  make(chan struct{}, 8),
		eventSink: func(event Event) { sinkEvents = append(sinkEvents, event) },
	}
	rpc.threadID = "thread-1"
	rpc.handleNotification("turn/started", map[string]any{"threadId": "thread-1"})
	rpc.handleNotification("item/completed", map[string]any{
		"threadId": "thread-1",
		"item": map[string]any{
			"type":  "agentMessage",
			"text":  "final answer",
			"phase": "final_answer",
		},
	})

	events := rpc.snapshotEvents()
	if len(events) != 2 {
		t.Fatalf("events = %#v", events)
	}
	if events[0].Type != EventStatus || events[1].Type != EventText {
		t.Fatalf("unexpected event order: %#v", events)
	}
	if len(sinkEvents) != len(events) || sinkEvents[1].Text != "final answer" {
		t.Fatalf("sink events = %#v", sinkEvents)
	}
	if rpc.output.String() != "final answer" {
		t.Fatalf("output = %q", rpc.output.String())
	}
	select {
	case <-rpc.done:
	default:
		t.Fatal("expected final_answer to complete turn")
	}
}

func TestResolveAndPrepareRunUsesRuntimeSnapshotAndPromptFiles(t *testing.T) {
	root := t.TempDir()
	resolved, err := ResolveAndPrepareRun(
		RunRequest{
			TaskID:  "task-1",
			AgentID: "coder",
			Prompt:  "fix",
			Metadata: map[string]any{
				"prompt_files": map[string]any{
					"AGENTS.md": "Use project conventions.\n",
				},
				"system_prompt":     "system",
				"semantic_idle_sec": float64(2),
				"custom_args":       []any{"--color", "never"},
				"timeout_sec":       "3",
			},
		},
		map[string]any{
			"runtime_profiles": map[string]any{
				"codex_remote": map[string]any{
					"executor":            "codex",
					"model":               "gpt-5.2-codex",
					"command":             "codex-beta",
					"args":                []any{"--profile", "default"},
					"env":                 map[string]any{"LABRASTRO_TEST": "1"},
					"runtime_home_policy": "per_task",
					"approval_mode":       "autonomous",
					"mcp":                 map[string]any{"servers": []any{"github"}},
				},
			},
			"agents": map[string]any{
				"coder": map[string]any{"runtime_profile": "codex_remote"},
			},
		},
		root,
		"workspace/one",
	)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.Request.Executor != "codex" || resolved.Request.Model != "gpt-5.2-codex" {
		t.Fatalf("request not resolved from snapshot: %#v", resolved.Request)
	}
	if resolved.Options.RuntimeHome == "" || !strings.Contains(resolved.Options.RuntimeHome, "codex-home") {
		t.Fatalf("CODEX_HOME not planned: %#v", resolved.Options)
	}
	if resolved.Options.Env["LABRASTRO_TEST"] != "1" {
		t.Fatalf("env not resolved: %#v", resolved.Options.Env)
	}
	if resolved.Options.Command != "codex-beta" {
		t.Fatalf("command = %q", resolved.Options.Command)
	}
	if resolved.Options.Timeout != 3*time.Second || resolved.Options.SemanticIdleTime != 2*time.Second {
		t.Fatalf("durations not resolved: %#v", resolved.Options)
	}
	if !bytes.Contains(resolved.Options.MCPConfigJSON, []byte("github")) {
		t.Fatalf("mcp config missing: %s", resolved.Options.MCPConfigJSON)
	}
	content, err := os.ReadFile(filepath.Join(resolved.Request.Workdir, "AGENTS.md"))
	if err != nil {
		t.Fatal(err)
	}
	if string(content) != "Use project conventions.\n" {
		t.Fatalf("prompt file content = %q", string(content))
	}
}

func TestCodexAppServerAutoApprovesServerRequestsAndCapturesUsage(t *testing.T) {
	var stdin bytes.Buffer
	rpc := &codexRPC{
		stdin:    &stdin,
		pending:  map[int]chan rpcResponse{},
		activity: make(chan struct{}, 8),
		done:     make(chan struct{}),
	}
	rpc.handleLine(`{"jsonrpc":"2.0","id":7,"method":"item/commandExecution/requestApproval","params":{}}`)
	if !strings.Contains(stdin.String(), `"decision":"accept"`) {
		t.Fatalf("approval response not written: %s", stdin.String())
	}
	rpc.handleNotification("turn/completed", map[string]any{
		"threadId": "thread-1",
		"turn": map[string]any{
			"status": "completed",
			"usage": map[string]any{
				"input_tokens":  float64(10),
				"output_tokens": float64(4),
			},
		},
	})
	usage := rpc.snapshotUsage("gpt-test")
	if usage["gpt-test"].InputTokens != 10 || usage["gpt-test"].OutputTokens != 4 {
		t.Fatalf("usage = %#v", usage)
	}
}

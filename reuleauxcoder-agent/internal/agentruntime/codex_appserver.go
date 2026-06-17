package agentruntime

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

type codexRPC struct {
	stdin     io.Writer
	mu        sync.Mutex
	nextID    int
	pending   map[int]chan rpcResponse
	activity  chan struct{}
	events    []Event
	output    strings.Builder
	usage     TokenUsage
	eventSink func(Event)
	threadID  string
	started   bool
	turnErr   string
	done      chan struct{}
	doneOnce  sync.Once
}

type rpcResponse struct {
	result json.RawMessage
	err    error
}

const defaultCodexSemanticIdleTime = 10 * time.Minute

func executeCodexAppServer(ctx context.Context, req RunRequest, opts RunOptions, inv Invocation) (RunResult, error) {
	runCtx := ctx
	cancel := func() {}
	if opts.Timeout > 0 {
		runCtx, cancel = context.WithTimeout(ctx, opts.Timeout)
	}
	defer cancel()

	cmd := exec.CommandContext(runCtx, inv.Command, inv.Args...)
	if inv.CWD != "" {
		cmd.Dir = inv.CWD
	}
	cmd.Env = mergeEnv(os.Environ(), inv.Env)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return RunResult{TaskID: req.TaskID, Status: "failed", Error: err.Error()}, err
	}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return RunResult{TaskID: req.TaskID, Status: "failed", Error: err.Error()}, err
	}
	stderr := newStderrTail(nil, agentStderrTailBytes)
	cmd.Stderr = stderr
	start := time.Now()
	if err := cmd.Start(); err != nil {
		return RunResult{TaskID: req.TaskID, Status: "failed", Error: err.Error()}, err
	}

	rpc := &codexRPC{
		stdin:     stdin,
		pending:   map[int]chan rpcResponse{},
		activity:  make(chan struct{}, 256),
		eventSink: opts.EventSink,
		done:      make(chan struct{}),
	}
	readerDone := make(chan struct{})
	go func() {
		defer close(readerDone)
		rpc.readLoop(stdout)
	}()
	defer func() {
		_ = stdin.Close()
		_ = cmd.Wait()
	}()

	if _, err := rpc.request(runCtx, "initialize", map[string]any{
		"clientInfo": map[string]any{
			"name":    "reuleauxcoder-agent-runs",
			"title":   "ReuleauxCoder AgentRun",
			"version": "0.1.0",
		},
		"capabilities": map[string]any{"experimentalApi": true},
	}); err != nil {
		return codexFailed(req.TaskID, rpc, "codex initialize failed: "+err.Error(), stderr.Tail()), err
	}
	_ = rpc.notify("initialized", nil)

	threadID, err := rpc.startOrResumeThread(runCtx, req, opts)
	if err != nil {
		return codexFailed(req.TaskID, rpc, err.Error(), stderr.Tail()), err
	}
	rpc.threadID = threadID
	semanticIdle := opts.SemanticIdleTime
	if semanticIdle <= 0 {
		semanticIdle = defaultCodexSemanticIdleTime
	}

	pendingSteers := []Steer{}
	turnInput := []map[string]any{{"type": "text", "text": req.Prompt}}
	for len(turnInput) > 0 {
		if err := rpc.startTurn(runCtx, threadID, turnInput); err != nil {
			return codexFailed(req.TaskID, rpc, "codex turn/start failed: "+err.Error(), stderr.Tail()), err
		}
		if err := waitCodexTurn(runCtx, rpc, semanticIdle, opts.Steers, &pendingSteers, readerDone); err != nil {
			if err == context.Canceled || err == context.DeadlineExceeded {
				return codexStopped(req.TaskID, rpc, err, time.Since(start).Milliseconds())
			}
			return codexFailed(req.TaskID, rpc, err.Error(), stderr.Tail()), err
		}
		if rpc.turnErr != "" {
			return codexFailed(req.TaskID, rpc, rpc.turnErr, stderr.Tail()), errors.New(rpc.turnErr)
		}
		drainCodexSteers(opts.Steers, &pendingSteers)
		if len(pendingSteers) == 0 {
			break
		}
		turnInput = codexInputFromSteers(pendingSteers)
		pendingSteers = nil
	}
	rpc.appendEvent(Event{
		Type: EventStatus,
		Data: map[string]any{
			"status":      "completed",
			"thread_id":   threadID,
			"duration_ms": time.Since(start).Milliseconds(),
		},
	})
	return RunResult{
		TaskID:            req.TaskID,
		Status:            "completed",
		Output:            rpc.output.String(),
		ExecutorSessionID: threadID,
		Usage:             rpc.snapshotUsage(req.Model),
		Events:            rpc.snapshotEvents(),
	}, nil
}

func (r *codexRPC) startOrResumeThread(ctx context.Context, req RunRequest, opts RunOptions) (string, error) {
	if req.ExecutorSessionID != "" {
		result, err := r.request(ctx, "thread/resume", map[string]any{
			"threadId":              req.ExecutorSessionID,
			"cwd":                   req.Workdir,
			"model":                 nilIfEmpty(req.Model),
			"developerInstructions": nilIfEmpty(opts.SystemPrompt),
		})
		if err == nil {
			if threadID := extractThreadID(result); threadID != "" {
				return threadID, nil
			}
		}
	}
	result, err := r.request(ctx, "thread/start", map[string]any{
		"model":                  nilIfEmpty(req.Model),
		"modelProvider":          nil,
		"profile":                nil,
		"cwd":                    req.Workdir,
		"approvalPolicy":         nil,
		"sandbox":                nil,
		"config":                 nil,
		"baseInstructions":       nil,
		"developerInstructions":  nilIfEmpty(opts.SystemPrompt),
		"compactPrompt":          nil,
		"includeApplyPatchTool":  nil,
		"experimentalRawEvents":  false,
		"persistExtendedHistory": true,
	})
	if err != nil {
		return "", fmt.Errorf("codex thread/start failed: %w", err)
	}
	threadID := extractThreadID(result)
	if threadID == "" {
		return "", errors.New("codex thread/start returned no thread id")
	}
	return threadID, nil
}

func (r *codexRPC) startTurn(ctx context.Context, threadID string, input []map[string]any) error {
	r.resetTurn()
	_, err := r.request(ctx, "turn/start", map[string]any{
		"threadId": threadID,
		"input":    input,
	})
	return err
}

func (r *codexRPC) resetTurn() {
	r.started = false
	r.turnErr = ""
	r.done = make(chan struct{})
	r.doneOnce = sync.Once{}
}

func waitCodexTurn(
	ctx context.Context,
	rpc *codexRPC,
	semanticIdle time.Duration,
	steers <-chan Steer,
	pendingSteers *[]Steer,
	readerDone <-chan struct{},
) error {
	semanticTimer := time.NewTimer(semanticIdle)
	defer semanticTimer.Stop()
	done := rpc.done
	for {
		select {
		case <-done:
			return nil
		case steer := <-steers:
			if strings.TrimSpace(steer.ID) != "" {
				*pendingSteers = append(*pendingSteers, steer)
			}
		case <-rpc.activity:
			resetTimer(semanticTimer, semanticIdle)
		case <-semanticTimer.C:
			return fmt.Errorf("codex semantic inactivity timeout after %s", semanticIdle)
		case <-ctx.Done():
			return ctx.Err()
		case <-readerDone:
			if rpc.turnErr == "" {
				rpc.turnErr = "codex app-server exited before turn completed"
			}
			return nil
		}
	}
}

func drainCodexSteers(steers <-chan Steer, pendingSteers *[]Steer) {
	for {
		select {
		case steer := <-steers:
			if strings.TrimSpace(steer.ID) != "" {
				*pendingSteers = append(*pendingSteers, steer)
			}
		default:
			return
		}
	}
}

func codexInputFromSteers(steers []Steer) []map[string]any {
	input := []map[string]any{}
	for _, steer := range steers {
		for _, item := range steerPayloadItems(steer.Payload) {
			itemType := strings.TrimSpace(fmt.Sprint(item["type"]))
			if itemType == "" {
				continue
			}
			normalized := cloneAnyMap(item)
			normalized["type"] = itemType
			input = append(input, normalized)
		}
	}
	return input
}

func (r *codexRPC) request(ctx context.Context, method string, params any) (json.RawMessage, error) {
	r.mu.Lock()
	r.nextID++
	id := r.nextID
	ch := make(chan rpcResponse, 1)
	r.pending[id] = ch
	r.mu.Unlock()
	if err := r.write(map[string]any{
		"jsonrpc": "2.0",
		"id":      id,
		"method":  method,
		"params":  params,
	}); err != nil {
		return nil, err
	}
	select {
	case resp := <-ch:
		return resp.result, resp.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (r *codexRPC) notify(method string, params any) error {
	msg := map[string]any{"jsonrpc": "2.0", "method": method}
	if params != nil {
		msg["params"] = params
	}
	return r.write(msg)
}

func (r *codexRPC) write(msg map[string]any) error {
	buf, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	buf = append(buf, '\n')
	_, err = r.stdin.Write(buf)
	return err
}

func (r *codexRPC) readLoop(stdout io.Reader) {
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 0, 1024*1024), 10*1024*1024)
	for scanner.Scan() {
		r.handleLine(scanner.Text())
	}
	r.mu.Lock()
	for id, ch := range r.pending {
		delete(r.pending, id)
		ch <- rpcResponse{err: errors.New("codex app-server exited")}
	}
	r.mu.Unlock()
}

func (r *codexRPC) handleLine(line string) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal([]byte(strings.TrimSpace(line)), &raw); err != nil {
		r.appendEvent(Event{Type: EventLog, Text: line})
		return
	}
	if idRaw, ok := raw["id"]; ok {
		var id int
		_ = json.Unmarshal(idRaw, &id)
		if methodRaw, hasMethod := raw["method"]; hasMethod {
			if _, hasResult := raw["result"]; !hasResult {
				if _, hasError := raw["error"]; !hasError {
					var method string
					_ = json.Unmarshal(methodRaw, &method)
					r.handleServerRequest(id, method)
					return
				}
			}
		}
		r.mu.Lock()
		ch := r.pending[id]
		delete(r.pending, id)
		r.mu.Unlock()
		if ch == nil {
			return
		}
		if errRaw, ok := raw["error"]; ok {
			var rpcErr struct {
				Message string `json:"message"`
			}
			_ = json.Unmarshal(errRaw, &rpcErr)
			if rpcErr.Message == "" {
				rpcErr.Message = string(errRaw)
			}
			ch <- rpcResponse{err: errors.New(rpcErr.Message)}
			return
		}
		ch <- rpcResponse{result: raw["result"]}
		return
	}
	var method string
	_ = json.Unmarshal(raw["method"], &method)
	var params map[string]any
	if p, ok := raw["params"]; ok {
		_ = json.Unmarshal(p, &params)
	}
	r.handleNotification(method, params)
}

func (r *codexRPC) handleServerRequest(id int, method string) {
	switch method {
	case "item/commandExecution/requestApproval", "execCommandApproval",
		"item/fileChange/requestApproval", "applyPatchApproval":
		r.respond(id, map[string]any{"decision": "accept"})
	default:
		r.respond(id, map[string]any{})
	}
}

func (r *codexRPC) respond(id int, result any) {
	_ = r.write(map[string]any{
		"jsonrpc": "2.0",
		"id":      id,
		"result":  result,
	})
}

func (r *codexRPC) handleNotification(method string, params map[string]any) {
	if method == "codex/event" || strings.HasPrefix(method, "codex/event/") {
		msg, _ := params["msg"].(map[string]any)
		r.handleLegacyEvent(msg)
		return
	}
	if threadID, ok := params["threadId"].(string); ok && r.threadID != "" && threadID != r.threadID {
		return
	}
	switch method {
	case "turn/started":
		r.started = true
		r.appendEvent(Event{Type: EventStatus, Data: map[string]any{"status": "running", "thread_id": r.threadID}})
	case "turn/completed":
		status := nestedString(params, "turn", "status")
		if turn, ok := params["turn"].(map[string]any); ok {
			r.extractUsageFromMap(turn)
		}
		if status == "failed" {
			r.turnErr = nestedString(params, "turn", "error", "message")
			if r.turnErr == "" {
				r.turnErr = "codex turn failed"
			}
		}
		if status == "cancelled" || status == "canceled" || status == "aborted" || status == "interrupted" {
			r.turnErr = "codex turn was aborted"
		}
		r.markDone()
	case "thread/status/changed":
		if nestedString(params, "status", "type") == "idle" && r.started {
			r.markDone()
		}
	case "error":
		r.turnErr = nestedString(params, "error", "message")
		if r.turnErr == "" {
			r.turnErr = nestedString(params, "message")
		}
		if r.turnErr != "" {
			r.appendEvent(Event{Type: EventError, Text: r.turnErr})
		}
	default:
		if strings.HasPrefix(method, "item/") {
			r.handleItem(method, params)
		}
	}
}

func (r *codexRPC) handleLegacyEvent(msg map[string]any) {
	switch msgType, _ := msg["type"].(string); msgType {
	case "task_started":
		r.started = true
		r.appendEvent(Event{Type: EventStatus, Data: map[string]any{"status": "running", "thread_id": r.threadID}})
	case "agent_message":
		if text, _ := msg["message"].(string); text != "" {
			r.appendText(text)
		}
	case "exec_command_begin":
		r.appendEvent(Event{Type: EventToolUse, Data: map[string]any{"tool": "exec_command", "input": msg}})
	case "exec_command_end":
		r.appendEvent(Event{Type: EventToolResult, Data: map[string]any{"tool": "exec_command", "input": msg, "output": msg["output"]}})
	case "patch_apply_begin":
		r.appendEvent(Event{Type: EventToolUse, Data: map[string]any{"tool": "patch_apply", "input": msg}})
	case "patch_apply_end":
		r.appendEvent(Event{Type: EventToolResult, Data: map[string]any{"tool": "patch_apply", "output": msg}})
	case "task_complete":
		r.extractUsageFromMap(msg)
		r.markDone()
	case "turn_aborted":
		r.turnErr = "codex turn was aborted"
		r.markDone()
	}
}

func (r *codexRPC) handleItem(method string, params map[string]any) {
	item, _ := params["item"].(map[string]any)
	itemType, _ := item["type"].(string)
	switch {
	case method == "item/started" && itemType == "commandExecution":
		r.appendEvent(Event{Type: EventToolUse, Data: map[string]any{"tool": "exec_command", "input": item}})
	case method == "item/completed" && itemType == "commandExecution":
		r.appendEvent(Event{Type: EventToolResult, Data: map[string]any{"tool": "exec_command", "input": item, "output": item["aggregatedOutput"]}})
	case method == "item/started" && itemType == "fileChange":
		r.appendEvent(Event{Type: EventToolUse, Data: map[string]any{"tool": "patch_apply", "input": item}})
	case method == "item/completed" && itemType == "fileChange":
		r.appendEvent(Event{Type: EventToolResult, Data: map[string]any{"tool": "patch_apply", "output": item}})
	case method == "item/completed" && itemType == "agentMessage":
		if text, _ := item["text"].(string); text != "" {
			r.appendText(text)
		}
		if phase, _ := item["phase"].(string); phase == "final_answer" && r.started {
			r.markDone()
		}
	}
}

func (r *codexRPC) appendText(text string) {
	r.output.WriteString(text)
	r.appendEvent(Event{Type: EventText, Text: text})
}

func (r *codexRPC) appendEvent(event Event) {
	r.mu.Lock()
	r.events = append(r.events, event)
	r.mu.Unlock()
	if r.eventSink != nil {
		r.eventSink(event)
	}
	select {
	case r.activity <- struct{}{}:
	default:
	}
}

func (r *codexRPC) snapshotEvents() []Event {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]Event{}, r.events...)
}

func (r *codexRPC) snapshotUsage(model string) map[string]TokenUsage {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.usage.InputTokens == 0 && r.usage.OutputTokens == 0 && r.usage.CacheReadTokens == 0 && r.usage.CacheWriteTokens == 0 {
		return nil
	}
	if strings.TrimSpace(model) == "" {
		model = "unknown"
	}
	return map[string]TokenUsage{model: r.usage}
}

func (r *codexRPC) extractUsageFromMap(data map[string]any) {
	var usageMap map[string]any
	for _, key := range []string{"usage", "token_usage", "tokens"} {
		if value, ok := data[key].(map[string]any); ok {
			usageMap = value
			break
		}
	}
	if usageMap == nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.usage.InputTokens += codexInt64(usageMap, "input_tokens", "input", "prompt_tokens")
	r.usage.OutputTokens += codexInt64(usageMap, "output_tokens", "output", "completion_tokens")
	r.usage.CacheReadTokens += codexInt64(usageMap, "cache_read_tokens", "cache_read_input_tokens", "cached_input_tokens")
	r.usage.CacheWriteTokens += codexInt64(usageMap, "cache_write_tokens", "cache_creation_input_tokens")
}

func codexInt64(data map[string]any, keys ...string) int64 {
	for _, key := range keys {
		switch value := data[key].(type) {
		case int64:
			if value != 0 {
				return value
			}
		case int:
			if value != 0 {
				return int64(value)
			}
		case float64:
			if value != 0 {
				return int64(value)
			}
		}
	}
	return 0
}

func (r *codexRPC) markDone() {
	r.doneOnce.Do(func() { close(r.done) })
}

func codexFailed(taskID string, rpc *codexRPC, message, stderr string) RunResult {
	if tail := strings.TrimSpace(stderr); tail != "" {
		message = message + "\n[codex stderr]\n" + tail
	}
	rpc.appendEvent(Event{Type: EventError, Text: message})
	return RunResult{
		TaskID: taskID,
		Status: "failed",
		Output: rpc.output.String(),
		Error:  message,
		Events: rpc.snapshotEvents(),
	}
}

func codexStopped(taskID string, rpc *codexRPC, err error, durationMs int64) (RunResult, error) {
	status := "cancelled"
	if err == context.DeadlineExceeded {
		status = "timeout"
	}
	rpc.appendEvent(Event{
		Type: EventStatus,
		Data: map[string]any{"status": status, "duration_ms": durationMs},
	})
	return RunResult{
		TaskID: taskID,
		Status: status,
		Output: rpc.output.String(),
		Error:  err.Error(),
		Events: rpc.snapshotEvents(),
	}, err
}

func extractThreadID(raw json.RawMessage) string {
	var data map[string]any
	_ = json.Unmarshal(raw, &data)
	for _, key := range []string{"threadId", "thread_id", "id"} {
		if value, _ := data[key].(string); value != "" {
			return value
		}
	}
	return nestedString(data, "thread", "id")
}

func nestedString(data map[string]any, keys ...string) string {
	var cur any = data
	for _, key := range keys {
		m, ok := cur.(map[string]any)
		if !ok {
			return ""
		}
		cur = m[key]
	}
	value, _ := cur.(string)
	return value
}

func nilIfEmpty(value string) any {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	return value
}

type writerFunc func([]byte) (int, error)

func (f writerFunc) Write(p []byte) (int, error) {
	return f(p)
}

func resetTimer(timer *time.Timer, d time.Duration) {
	if !timer.Stop() {
		select {
		case <-timer.C:
		default:
		}
	}
	timer.Reset(d)
}

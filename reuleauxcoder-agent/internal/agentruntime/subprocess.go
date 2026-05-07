package agentruntime

import (
	"bufio"
	"bytes"
	"context"
	"os"
	"os/exec"
	"strings"
	"time"
)

type SubprocessBackend struct{}

func (b SubprocessBackend) Start(ctx context.Context, req RunRequest, opts RunOptions) (*Session, error) {
	return startExecuteSession(ctx, req, opts, b.Execute), nil
}

func (b SubprocessBackend) Execute(ctx context.Context, req RunRequest, opts RunOptions) (RunResult, error) {
	if strings.EqualFold(req.Executor, "fake") {
		return FakeBackend{}.Execute(ctx, req, opts)
	}
	inv, err := BuildInvocation(req, opts)
	if err != nil {
		return RunResult{TaskID: req.TaskID, Status: "failed", Error: err.Error()}, err
	}
	if inv.Cleanup != nil {
		defer inv.Cleanup()
	}
	if inv.Transport == "jsonrpc_stdio" {
		return executeCodexAppServer(ctx, req, opts, inv)
	}

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
	if len(inv.StdinJSON) > 0 {
		cmd.Stdin = bytes.NewReader(inv.StdinJSON)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return RunResult{TaskID: req.TaskID, Status: "failed", Error: err.Error()}, err
	}
	stderr := newStderrTail(&bytes.Buffer{}, agentStderrTailBytes)
	cmd.Stderr = stderr
	start := time.Now()
	if err := cmd.Start(); err != nil {
		return RunResult{TaskID: req.TaskID, Status: "failed", Error: err.Error()}, err
	}

	var events []Event
	parser := newStreamParser(req.Executor)
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 0, 1024*1024), 10*1024*1024)
	for scanner.Scan() {
		for _, event := range parser.ParseLine(scanner.Text()) {
			events = append(events, event)
			emitEvent(opts, event)
		}
	}
	if err := scanner.Err(); err != nil {
		event := Event{Type: EventError, Text: err.Error()}
		events = append(events, event)
		emitEvent(opts, event)
	}
	waitErr := cmd.Wait()
	if waitErr != nil {
		if runCtx.Err() != nil {
			status := "cancelled"
			if runCtx.Err() == context.DeadlineExceeded {
				status = "timeout"
			}
			errText := runCtx.Err().Error()
			event := Event{Type: EventStatus, Data: map[string]any{"status": status}}
			events = append(events, event)
			emitEvent(opts, event)
			return RunResult{
				TaskID:            req.TaskID,
				Status:            status,
				Output:            parser.Output(),
				Error:             errText,
				ExecutorSessionID: parser.SessionID(),
				Usage:             parser.Usage(),
				Events:            events,
			}, waitErr
		}
		errText := strings.TrimSpace(stderr.Tail())
		if errText == "" {
			errText = waitErr.Error()
		} else {
			errText = withAgentStderr(waitErr.Error(), req.Executor, errText)
		}
		return RunResult{
			TaskID:            req.TaskID,
			Status:            "failed",
			Output:            parser.Output(),
			Error:             errText,
			ExecutorSessionID: parser.SessionID(),
			Usage:             parser.Usage(),
			Events:            events,
		}, waitErr
	}
	finalStatus := "completed"
	finalError := ""
	if parser.StatusOverride() != "" {
		finalStatus = parser.StatusOverride()
		finalError = parser.ErrorText()
	}
	events = append(events, Event{
		Type: EventStatus,
		Data: map[string]any{
			"status":              finalStatus,
			"duration_ms":         time.Since(start).Milliseconds(),
			"executor_session_id": parser.SessionID(),
		},
	})
	emitEvent(opts, events[len(events)-1])
	return RunResult{
		TaskID:            req.TaskID,
		Status:            finalStatus,
		Output:            parser.Output(),
		Error:             finalError,
		ExecutorSessionID: parser.SessionID(),
		Usage:             parser.Usage(),
		Events:            events,
	}, nil
}

func mergeEnv(base []string, extra map[string]string) []string {
	if len(extra) == 0 {
		return base
	}
	seen := map[string]int{}
	for i, entry := range base {
		key, _, ok := strings.Cut(entry, "=")
		if ok {
			seen[key] = i
		}
	}
	out := append([]string{}, base...)
	for key, val := range extra {
		entry := key + "=" + val
		if idx, ok := seen[key]; ok {
			out[idx] = entry
			continue
		}
		out = append(out, entry)
	}
	return out
}

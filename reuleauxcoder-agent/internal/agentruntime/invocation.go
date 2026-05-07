package agentruntime

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

type Invocation struct {
	Command   string
	Args      []string
	Env       map[string]string
	CWD       string
	StdinJSON []byte
	Transport string
	Cleanup   func()
}

type blockedArgMode int

const (
	blockedWithValue blockedArgMode = iota
	blockedStandalone
)

var claudeBlockedArgs = map[string]blockedArgMode{
	"-p":                  blockedStandalone,
	"-r":                  blockedWithValue,
	"--resume":            blockedWithValue,
	"--model":             blockedWithValue,
	"--print":             blockedStandalone,
	"--output-format":     blockedWithValue,
	"--input-format":      blockedWithValue,
	"--verbose":           blockedStandalone,
	"--permission-mode":   blockedWithValue,
	"--mcp-config":        blockedWithValue,
	"--strict-mcp-config": blockedStandalone,
}

var codexBlockedArgs = map[string]blockedArgMode{
	"--listen": blockedWithValue,
}

var geminiBlockedArgs = map[string]blockedArgMode{
	"-p":              blockedWithValue,
	"--prompt":        blockedWithValue,
	"--yolo":          blockedStandalone,
	"-o":              blockedWithValue,
	"--output-format": blockedWithValue,
	"-r":              blockedWithValue,
	"--resume":        blockedWithValue,
	"-m":              blockedWithValue,
	"--model":         blockedWithValue,
}

var reuleauxcoderBlockedArgs = map[string]blockedArgMode{
	"-p":       blockedWithValue,
	"--prompt": blockedWithValue,
	"-m":       blockedWithValue,
	"--model":  blockedWithValue,
	"--server": blockedStandalone,
}

func BuildInvocation(req RunRequest, opts RunOptions) (Invocation, error) {
	switch strings.ToLower(strings.TrimSpace(req.Executor)) {
	case "reuleauxcoder":
		return buildReuleauxCoderInvocation(req, opts), nil
	case "codex":
		return buildCodexInvocation(req, opts), nil
	case "claude":
		return buildClaudeInvocation(req, opts)
	case "gemini":
		return buildGeminiInvocation(req, opts), nil
	case "fake":
		return Invocation{Command: "fake", CWD: req.Workdir}, nil
	default:
		return Invocation{}, fmt.Errorf("unsupported executor %q", req.Executor)
	}
}

func buildReuleauxCoderInvocation(req RunRequest, opts RunOptions) Invocation {
	command := firstNonEmpty(opts.Command, "rcoder")
	args := []string{"--prompt", req.Prompt}
	if req.Model != "" {
		args = append(args, "--model", req.Model)
	}
	args = append(args, filterCustomArgs(opts.ExtraArgs, reuleauxcoderBlockedArgs)...)
	args = append(args, filterCustomArgs(opts.CustomArgs, reuleauxcoderBlockedArgs)...)
	return Invocation{
		Command:   command,
		Args:      args,
		Env:       cloneEnv(opts.Env),
		CWD:       req.Workdir,
		Transport: "plain_stdout",
	}
}

func buildCodexInvocation(req RunRequest, opts RunOptions) Invocation {
	command := firstNonEmpty(opts.Command, "codex")
	args := []string{"app-server", "--listen", "stdio://"}
	args = append(args, filterCustomArgs(opts.ExtraArgs, codexBlockedArgs)...)
	args = append(args, filterCustomArgs(opts.CustomArgs, codexBlockedArgs)...)
	env := cloneEnv(opts.Env)
	if opts.RuntimeHome != "" {
		env["CODEX_HOME"] = opts.RuntimeHome
	}
	return Invocation{
		Command:   command,
		Args:      args,
		Env:       env,
		CWD:       req.Workdir,
		Transport: "jsonrpc_stdio",
	}
}

func buildClaudeInvocation(req RunRequest, opts RunOptions) (Invocation, error) {
	command := firstNonEmpty(opts.Command, "claude")
	args := []string{
		"-p",
		"--output-format", "stream-json",
		"--input-format", "stream-json",
		"--verbose",
		"--strict-mcp-config",
		"--permission-mode", "bypassPermissions",
	}
	if req.Model != "" {
		args = append(args, "--model", req.Model)
	}
	if opts.SystemPrompt != "" {
		args = append(args, "--append-system-prompt", opts.SystemPrompt)
	}
	if req.ExecutorSessionID != "" {
		args = append(args, "--resume", req.ExecutorSessionID)
	}
	args = append(args, filterCustomArgs(opts.ExtraArgs, claudeBlockedArgs)...)
	args = append(args, filterCustomArgs(opts.CustomArgs, claudeBlockedArgs)...)
	var cleanup func()
	if len(opts.MCPConfigJSON) > 0 {
		path, err := writeMCPConfigToTemp(opts.MCPConfigJSON)
		if err != nil {
			return Invocation{}, err
		}
		args = append(args, "--mcp-config", path)
		cleanup = func() { _ = os.Remove(path) }
	}
	stdin, err := buildClaudeInput(req.Prompt)
	if err != nil {
		if cleanup != nil {
			cleanup()
		}
		return Invocation{}, err
	}
	return Invocation{
		Command:   command,
		Args:      args,
		Env:       cloneEnv(opts.Env),
		CWD:       req.Workdir,
		StdinJSON: stdin,
		Transport: "stream_json",
		Cleanup:   cleanup,
	}, nil
}

func buildGeminiInvocation(req RunRequest, opts RunOptions) Invocation {
	command := firstNonEmpty(opts.Command, "gemini")
	args := []string{"-p", req.Prompt, "--yolo", "-o", "stream-json"}
	if req.Model != "" {
		args = append(args, "-m", req.Model)
	}
	if req.ExecutorSessionID != "" {
		args = append(args, "-r", req.ExecutorSessionID)
	}
	args = append(args, filterCustomArgs(opts.ExtraArgs, geminiBlockedArgs)...)
	args = append(args, filterCustomArgs(opts.CustomArgs, geminiBlockedArgs)...)
	return Invocation{
		Command:   command,
		Args:      args,
		Env:       cloneEnv(opts.Env),
		CWD:       req.Workdir,
		Transport: "stream_json",
	}
}

func buildClaudeInput(prompt string) ([]byte, error) {
	msg := map[string]any{
		"type": "user",
		"message": map[string]any{
			"role": "user",
			"content": []map[string]string{
				{"type": "text", "text": prompt},
			},
		},
	}
	buf, err := json.Marshal(msg)
	if err != nil {
		return nil, err
	}
	return append(buf, '\n'), nil
}

func filterCustomArgs(args []string, blocked map[string]blockedArgMode) []string {
	if len(args) == 0 {
		return nil
	}
	var filtered []string
	skipNext := false
	for _, arg := range args {
		if skipNext {
			skipNext = false
			continue
		}
		name := arg
		if i := strings.Index(arg, "="); i >= 0 {
			name = arg[:i]
		}
		if mode, ok := blocked[name]; ok {
			if mode == blockedWithValue && !strings.Contains(arg, "=") {
				skipNext = true
			}
			continue
		}
		filtered = append(filtered, arg)
	}
	return filtered
}

func cloneEnv(env map[string]string) map[string]string {
	if len(env) == 0 {
		return map[string]string{}
	}
	out := make(map[string]string, len(env))
	for k, v := range env {
		out[k] = v
	}
	return out
}

func writeMCPConfigToTemp(raw []byte) (string, error) {
	file, err := os.CreateTemp("", "labrastro-mcp-*.json")
	if err != nil {
		return "", fmt.Errorf("create mcp config temp file: %w", err)
	}
	path := file.Name()
	if _, err := file.Write(raw); err != nil {
		_ = file.Close()
		_ = os.Remove(path)
		return "", fmt.Errorf("write mcp config temp file: %w", err)
	}
	if err := file.Close(); err != nil {
		_ = os.Remove(path)
		return "", fmt.Errorf("close mcp config temp file: %w", err)
	}
	return path, nil
}

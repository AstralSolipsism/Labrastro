package agentruntime

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
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
	"-p":        blockedWithValue,
	"--prompt":  blockedWithValue,
	"--session": blockedWithValue,
	"--events":  blockedWithValue,
	"-m":        blockedWithValue,
	"--model":   blockedWithValue,
	"--server":  blockedStandalone,
}

var reuleauxcoderServerOriginBlockedArgs = withBlockedArgs(
	reuleauxcoderBlockedArgs,
	map[string]blockedArgMode{
		"-c":       blockedWithValue,
		"--config": blockedWithValue,
	},
)

const reuleauxcoderServerOriginModelProfile = "agent-run"

func BuildInvocation(req RunRequest, opts RunOptions) (Invocation, error) {
	switch strings.ToLower(strings.TrimSpace(req.Executor)) {
	case "reuleauxcoder":
		return buildReuleauxCoderInvocation(req, opts)
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

func buildReuleauxCoderInvocation(req RunRequest, opts RunOptions) (Invocation, error) {
	command := firstNonEmpty(opts.Command, "rcoder")
	serverOrigin := strings.EqualFold(strings.TrimSpace(req.ModelRequestOrigin), "server")
	sessionID := strings.TrimSpace(req.ExecutorSessionID)
	if sessionID == "" {
		return Invocation{}, fmt.Errorf("executor_session_id is required for ReuleauxCoder AgentRun invocation")
	}
	args := []string{}
	if serverOrigin {
		args = append(args, "--model", reuleauxcoderServerOriginModelProfile)
	} else if req.Model != "" {
		args = append(args, "--model", req.Model)
	}
	blockedArgs := reuleauxcoderBlockedArgs
	if serverOrigin {
		blockedArgs = reuleauxcoderServerOriginBlockedArgs
	}
	args = append(args, filterReuleauxCoderAgentRunRootArgs(opts.ExtraArgs, blockedArgs)...)
	args = append(args, filterReuleauxCoderAgentRunRootArgs(opts.CustomArgs, blockedArgs)...)
	args = append(args, "agent-run", "--prompt", req.Prompt, "--session", sessionID, "--events", "jsonl")
	env := cloneEnv(opts.Env)
	var cleanup func()
	if serverOrigin {
		configPath, configCleanup, err := writeLabrastroServerOriginConfig(req)
		if err != nil {
			return Invocation{}, err
		}
		cleanup = configCleanup
		env["RCODER_CONFIG_PATH"] = configPath
		env["LABRASTRO_REMOTE_BASE_URL"] = strings.TrimRight(opts.RemoteBaseURL, "/")
		env["LABRASTRO_PEER_TOKEN"] = opts.PeerToken
		env["LABRASTRO_AGENT_RUN_ID"] = req.TaskID
		env["LABRASTRO_AGENT_RUN_REQUEST_ID"] = opts.AgentRunRequestID
		env["LABRASTRO_AGENT_RUN_ACTIVATION_ID"] = opts.AgentRunActivationID
		env["LABRASTRO_AGENT_RUN_WORKER_ID"] = opts.AgentRunWorkerID
		for _, key := range []string{
			"LABRASTRO_REMOTE_BASE_URL",
			"LABRASTRO_PEER_TOKEN",
			"LABRASTRO_AGENT_RUN_ID",
			"LABRASTRO_AGENT_RUN_REQUEST_ID",
			"LABRASTRO_AGENT_RUN_ACTIVATION_ID",
			"LABRASTRO_AGENT_RUN_WORKER_ID",
		} {
			if strings.TrimSpace(env[key]) == "" {
				if cleanup != nil {
					cleanup()
				}
				return Invocation{}, fmt.Errorf("%s is required for server-origin ReuleauxCoder invocation", key)
			}
		}
	}
	return Invocation{
		Command:   command,
		Args:      args,
		Env:       env,
		CWD:       req.Workdir,
		Transport: "plain_stdout",
		Cleanup:   cleanup,
	}, nil
}

func filterReuleauxCoderAgentRunRootArgs(args []string, blocked map[string]blockedArgMode) []string {
	filtered := filterCustomArgs(args, blocked)
	out := make([]string, 0, len(filtered))
	for i := 0; i < len(filtered); i++ {
		arg := filtered[i]
		if arg != "-c" && arg != "--config" {
			continue
		}
		if i+1 >= len(filtered) {
			continue
		}
		out = append(out, arg, filtered[i+1])
		i++
	}
	return out
}

func withBlockedArgs(base map[string]blockedArgMode, extra map[string]blockedArgMode) map[string]blockedArgMode {
	result := make(map[string]blockedArgMode, len(base)+len(extra))
	for key, value := range base {
		result[key] = value
	}
	for key, value := range extra {
		result[key] = value
	}
	return result
}

func writeLabrastroServerOriginConfig(req RunRequest) (string, func(), error) {
	dir, err := os.MkdirTemp("", "labrastro-agent-run-*")
	if err != nil {
		return "", nil, fmt.Errorf("create server-origin config temp dir: %w", err)
	}
	cleanup := func() { _ = os.RemoveAll(dir) }
	configPath := filepath.Join(dir, "config.yaml")
	binding := mapValue(req.Metadata["model_binding"])
	params := mapValue(binding["parameters"])
	model := firstNonEmpty(req.Model, stringValue(binding["model"]), "server-origin")
	profile := map[string]any{
		"provider":           "labrastro-server",
		"model":              model,
		"max_tokens":         intValue(params["max_tokens"], 4096),
		"max_context_tokens": intValue(params["max_context_tokens"], 128000),
		"temperature":        floatValue(params["temperature"], 0.0),
	}
	copyOptionalProfileValue(profile, params, "reasoning_effort")
	copyOptionalProfileValue(profile, params, "thinking_enabled")
	copyOptionalProfileValue(profile, params, "preserve_reasoning_content")
	copyOptionalProfileValue(profile, params, "backfill_reasoning_content_for_tool_calls")
	copyOptionalProfileValue(profile, params, "reasoning_replay_mode")
	copyOptionalProfileValue(profile, params, "reasoning_replay_placeholder")
	raw := map[string]any{
		"providers": map[string]any{
			"items": map[string]any{
				"labrastro-server": map[string]any{
					"type":    "labrastro_server",
					"enabled": true,
				},
			},
		},
		"models": map[string]any{
			"active_main": reuleauxcoderServerOriginModelProfile,
			"profiles": map[string]any{
				reuleauxcoderServerOriginModelProfile: profile,
			},
		},
	}
	buf, err := json.MarshalIndent(raw, "", "  ")
	if err != nil {
		cleanup()
		return "", nil, fmt.Errorf("marshal server-origin config: %w", err)
	}
	if err := os.WriteFile(configPath, buf, 0o600); err != nil {
		cleanup()
		return "", nil, fmt.Errorf("write server-origin config: %w", err)
	}
	return configPath, cleanup, nil
}

func copyOptionalProfileValue(profile map[string]any, params map[string]any, key string) {
	if value, ok := params[key]; ok && value != nil {
		profile[key] = value
	}
}

func intValue(value any, fallback int) int {
	switch v := value.(type) {
	case int:
		if v > 0 {
			return v
		}
	case int64:
		if v > 0 {
			return int(v)
		}
	case float64:
		if v > 0 {
			return int(v)
		}
	case string:
		text := strings.TrimSpace(v)
		if text != "" {
			var parsed int
			if _, err := fmt.Sscanf(text, "%d", &parsed); err == nil && parsed > 0 {
				return parsed
			}
		}
	}
	return fallback
}

func floatValue(value any, fallback float64) float64 {
	switch v := value.(type) {
	case float64:
		return v
	case float32:
		return float64(v)
	case int:
		return float64(v)
	case int64:
		return float64(v)
	case string:
		text := strings.TrimSpace(v)
		if text != "" {
			var parsed float64
			if _, err := fmt.Sscanf(text, "%f", &parsed); err == nil {
				return parsed
			}
		}
	}
	return fallback
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

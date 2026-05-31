package agentruntime

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type ResolvedRun struct {
	Request RunRequest
	Options RunOptions
	Plan    ExecEnvPlan
}

func ResolveAndPrepareRun(req RunRequest, runtimeSnapshot map[string]any, runtimeRoot, workspaceID string) (ResolvedRun, error) {
	manager, resolved, err := ResolveRunWithExecEnv(req, runtimeSnapshot, runtimeRoot, workspaceID)
	if err != nil {
		return ResolvedRun{}, err
	}
	return PrepareResolvedRun(manager, resolved, PromptFilesFromMetadata(resolved.Request.Metadata))
}

func ResolveRunWithExecEnv(req RunRequest, runtimeSnapshot map[string]any, runtimeRoot, workspaceID string) (*ExecEnvManager, ResolvedRun, error) {
	manager, err := NewExecEnvManager(runtimeRoot)
	if err != nil {
		return nil, ResolvedRun{}, err
	}
	plan, err := manager.Plan(workspaceID, req.TaskID, req.AgentID)
	if err != nil {
		return nil, ResolvedRun{}, err
	}
	resolved, err := ResolveRun(req, runtimeSnapshot, plan)
	if err != nil {
		return nil, ResolvedRun{}, err
	}
	return manager, resolved, nil
}

func PrepareResolvedRun(manager *ExecEnvManager, resolved ResolvedRun, promptFiles map[string]string) (ResolvedRun, error) {
	if resolved.Request.Workdir == "" {
		resolved.Request.Workdir = resolved.Plan.WorkDir
	}
	if len(promptFiles) > 0 {
		if err := manager.PrepareWorkDir(resolved.Plan, resolved.Request.Workdir, promptFiles); err != nil {
			return ResolvedRun{}, err
		}
	} else if err := manager.PrepareRuntimeDirs(resolved.Plan); err != nil {
		return ResolvedRun{}, err
	}
	return resolved, nil
}

func ResolveRun(req RunRequest, runtimeSnapshot map[string]any, plan ExecEnvPlan) (ResolvedRun, error) {
	agents := mapValue(runtimeSnapshot["agents"])
	profiles := mapValue(runtimeSnapshot["runtime_profiles"])
	agent := mapValue(agents[req.AgentID])

	profileID := firstNonEmpty(req.RuntimeProfileID, stringValue(agent["runtime_profile"]))
	profile := mapValue(profiles[profileID])
	if profileID != "" && len(profile) == 0 {
		return ResolvedRun{}, fmt.Errorf("runtime profile not found: %s", profileID)
	}

	if req.Executor == "" {
		req.Executor = stringValue(profile["executor"])
	}
	if req.Executor == "" {
		req.Executor = "fake"
	}
	if req.ExecutionLocation == "" {
		req.ExecutionLocation = stringValue(profile["execution_location"])
	}
	if req.ExecutionLocation == "" {
		req.ExecutionLocation = "local_workspace"
	}
	if req.WorkerKind == "" {
		req.WorkerKind = stringValue(profile["worker_kind"])
	}
	if req.WorkerKind == "" {
		req.WorkerKind = inferWorkerKind(req.ExecutionLocation, profile)
	}
	if req.ModelRequestOrigin == "" {
		req.ModelRequestOrigin = stringValue(profile["model_request_origin"])
	}
	if req.ModelRequestOrigin == "" {
		req.ModelRequestOrigin = inferModelRequestOrigin(req.Executor, req.WorkerKind)
	}
	if req.RuntimeProfileID == "" {
		req.RuntimeProfileID = profileID
	}
	if req.Model == "" {
		req.Model = stringValue(profile["model"])
	}
	if req.Workdir == "" {
		req.Workdir = plan.WorkDir
	} else if !filepath.IsAbs(req.Workdir) {
		req.Workdir = filepath.Join(plan.RuntimeRoot, req.Workdir)
	}
	req.Workdir = filepath.Clean(req.Workdir)
	if req.Branch == "" {
		req.Branch = plan.BranchName
	}

	env := stringMapValue(profile["env"])
	overlay := mapValue(req.Metadata["capability_overlay"])
	if overlayEnv := stringMapValue(overlay["env"]); len(overlayEnv) > 0 {
		if env == nil {
			env = map[string]string{}
		}
		for key, value := range overlayEnv {
			env[key] = value
		}
	}
	if skillRoots := stringSliceValue(overlay["skill_roots"]); len(skillRoots) > 0 {
		if env == nil {
			env = map[string]string{}
		}
		env["EZCODE_SKILL_ROOTS"] = strings.Join(skillRoots, string(os.PathListSeparator))
	}
	opts := RunOptions{
		Timeout:          durationValue(firstAny(req.Metadata["timeout_sec"], profile["timeout_sec"]), 0),
		Command:          stringValue(profile["command"]),
		SystemPrompt:     systemPromptValue(req.Metadata),
		ExtraArgs:        stringSliceValue(profile["args"]),
		CustomArgs:       stringSliceValue(firstAny(req.Metadata["custom_args"], profile["custom_args"])),
		Env:              env,
		ApprovalMode:     stringValue(profile["approval_mode"]),
		SemanticIdleTime: durationValue(firstAny(req.Metadata["semantic_idle_sec"], profile["semantic_idle_sec"]), 0),
	}
	if opts.CustomArgs == nil {
		opts.CustomArgs = stringSliceValue(firstAny(req.Metadata["customArgs"], profile["customArgs"]))
	}
	mcpConfig := mapValue(profile["mcp"])
	if overlayMCP := mapValue(overlay["mcp"]); len(overlayMCP) > 0 {
		mcpConfig = mergeNestedMaps(mcpConfig, overlayMCP)
	}
	if len(mcpConfig) > 0 {
		raw, err := json.Marshal(mcpConfig)
		if err != nil {
			return ResolvedRun{}, err
		}
		opts.MCPConfigJSON = raw
	}

	runtimeHomePolicy := strings.ToLower(strings.TrimSpace(stringValue(profile["runtime_home_policy"])))
	if strings.EqualFold(req.Executor, "codex") && runtimeHomePolicy != "shared" && runtimeHomePolicy != "inherit" && runtimeHomePolicy != "none" {
		opts.RuntimeHome = plan.CodexHome
	}
	if opts.RuntimeHome == "" && strings.EqualFold(req.Executor, "codex") {
		opts.RuntimeHome = plan.CodexHome
	}
	return ResolvedRun{Request: req, Options: opts, Plan: plan}, nil
}

func inferWorkerKind(executionLocation string, profile map[string]any) string {
	if len(mapValue(profile["sandbox"])) > 0 {
		return "sandbox_worker"
	}
	if executionLocation == "local_workspace" {
		return "local_peer"
	}
	return "server_worker"
}

func inferModelRequestOrigin(executor, workerKind string) string {
	switch executor {
	case "codex", "claude", "gemini":
		if workerKind == "local_peer" {
			return "local_cli"
		}
		return "server_worker_cli"
	default:
		return "server"
	}
}

func mergeNestedMaps(base, overlay map[string]any) map[string]any {
	if len(base) == 0 && len(overlay) == 0 {
		return map[string]any{}
	}
	out := make(map[string]any, len(base)+len(overlay))
	for key, value := range base {
		out[key] = value
	}
	for key, value := range overlay {
		if existing, ok := out[key]; ok {
			existingMap := mapValue(existing)
			overlayMap := mapValue(value)
			if len(existingMap) > 0 && len(overlayMap) > 0 {
				out[key] = mergeNestedMaps(existingMap, overlayMap)
				continue
			}
		}
		out[key] = value
	}
	return out
}

func PromptFilesFromMetadata(metadata map[string]any) map[string]string {
	raw := firstAny(metadata["prompt_files"], metadata["promptFiles"])
	values := mapValue(raw)
	if len(values) == 0 {
		return nil
	}
	files := make(map[string]string, len(values))
	for key, val := range values {
		text, ok := val.(string)
		if !ok {
			continue
		}
		if strings.TrimSpace(key) == "" {
			continue
		}
		files[key] = text
	}
	return files
}

func systemPromptValue(metadata map[string]any) string {
	if value := stringValue(metadata["system_prompt"]); value != "" {
		return value
	}
	promptMetadata := mapValue(metadata["prompt_metadata"])
	return stringValue(promptMetadata["system_prompt"])
}

func mapValue(value any) map[string]any {
	if value == nil {
		return map[string]any{}
	}
	if typed, ok := value.(map[string]any); ok {
		return typed
	}
	if typed, ok := value.(map[string]interface{}); ok {
		out := make(map[string]any, len(typed))
		for key, val := range typed {
			out[key] = val
		}
		return out
	}
	return map[string]any{}
}

func stringMapValue(value any) map[string]string {
	values := mapValue(value)
	if len(values) == 0 {
		return nil
	}
	out := make(map[string]string, len(values))
	for key, val := range values {
		if strings.TrimSpace(key) == "" || val == nil {
			continue
		}
		out[key] = fmt.Sprint(val)
	}
	return out
}

func stringSliceValue(value any) []string {
	switch typed := value.(type) {
	case []string:
		return append([]string{}, typed...)
	case []any:
		out := make([]string, 0, len(typed))
		for _, item := range typed {
			text := strings.TrimSpace(fmt.Sprint(item))
			if text != "" {
				out = append(out, text)
			}
		}
		return out
	default:
		if value == nil {
			return nil
		}
		text := strings.TrimSpace(fmt.Sprint(value))
		if text == "" {
			return nil
		}
		return []string{text}
	}
}

func stringValue(value any) string {
	if value == nil {
		return ""
	}
	return strings.TrimSpace(fmt.Sprint(value))
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func firstAny(values ...any) any {
	for _, value := range values {
		if value != nil {
			return value
		}
	}
	return nil
}

func durationValue(value any, fallback time.Duration) time.Duration {
	if value == nil {
		return fallback
	}
	switch typed := value.(type) {
	case time.Duration:
		if typed > 0 {
			return typed
		}
	case int:
		if typed > 0 {
			return time.Duration(typed) * time.Second
		}
	case int64:
		if typed > 0 {
			return time.Duration(typed) * time.Second
		}
	case float64:
		if typed > 0 {
			return time.Duration(typed * float64(time.Second))
		}
	case string:
		text := strings.TrimSpace(typed)
		if text == "" {
			return fallback
		}
		if parsed, err := time.ParseDuration(text); err == nil && parsed > 0 {
			return parsed
		}
		if seconds, err := strconv.ParseFloat(text, 64); err == nil && seconds > 0 {
			return time.Duration(seconds * float64(time.Second))
		}
	}
	return fallback
}

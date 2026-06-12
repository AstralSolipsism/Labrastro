package tools

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	pathpkg "path"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/lsp"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

var skipDirs = map[string]struct{}{
	".git":         {},
	"node_modules": {},
	"__pycache__":  {},
	".venv":        {},
	"venv":         {},
	".tox":         {},
	"dist":         {},
	"build":        {},
}

func Execute(
	req protocol.ExecToolRequest,
	currentCWD string,
	onStream func(protocol.ToolStreamChunk),
) protocol.ExecToolResult {
	return ExecuteWithContext(context.Background(), req, currentCWD, currentCWD, onStream)
}

func ExecuteWithContext(
	ctx context.Context,
	req protocol.ExecToolRequest,
	currentCWD string,
	workspaceRoot string,
	onStream func(protocol.ToolStreamChunk),
) protocol.ExecToolResult {
	cwd, staleWarning := resolveRequestedCWD(currentCWD, req.CWD)

	var result protocol.ExecToolResult
	switch req.ToolName {
	case "shell":
		result = runShell(ctx, req.Args, cwd, req.TimeoutSec, onStream)
	case "read_file":
		result = readFile(req.Args, cwd)
	case "apply_patch":
		result = applyPatch(req.Args, cwd, req.ExpectedState)
	case "draft_document_commit":
		result = draftDocumentCommit(req.Args, cwd, req.ExpectedState)
	case "glob":
		result = globFiles(req.Args, cwd)
	case "grep":
		result = grepFiles(req.Args, cwd)
	case "list_file":
		result = listFile(req.Args, cwd)
	case "lsp":
		result = lspTool(req.Args, cwd)
	default:
		result = errorResult("REMOTE_TOOL_ERROR", fmt.Sprintf("unsupported tool %q", req.ToolName))
	}
	result = prependWarning(result, staleWarning)
	return finalizeToolResult(req, result, workspaceRoot)
}

func LSPAvailable() bool {
	return lsp.Available()
}

func ShutdownLSP() {
	lsp.ShutdownDefault()
}

func Preview(req protocol.ToolPreviewRequest, currentCWD string) protocol.ToolPreviewResult {
	cwd, staleWarning := resolveRequestedCWD(currentCWD, req.CWD)
	if req.ToolName != "apply_patch" && req.ToolName != "draft_document_commit" {
		return protocol.ToolPreviewResult{
			OK:           false,
			ErrorCode:    "REMOTE_TOOL_PREVIEW_UNSUPPORTED",
			ErrorMessage: fmt.Sprintf("tool %q does not support preview", req.ToolName),
		}
	}
	mutations, err := buildMutationPlan(req.ToolName, req.Args, cwd, nil)
	if err != nil {
		return protocol.ToolPreviewResult{
			OK:           false,
			ErrorCode:    "REMOTE_TOOL_ERROR",
			ErrorMessage: err.Error(),
		}
	}
	if len(mutations) == 0 {
		return protocol.ToolPreviewResult{
			OK:           false,
			ErrorCode:    "REMOTE_TOOL_ERROR",
			ErrorMessage: "patch contains no file operations",
		}
	}
	sections := make([]map[string]any, 0, len(mutations))
	var combinedDiff strings.Builder
	for idx, mutation := range mutations {
		section := map[string]any{
			"id":            fmt.Sprintf("diff-%d", idx+1),
			"title":         mutation.diffTitle(),
			"kind":          "diff",
			"change_kind":   mutation.kind,
			"content":       mutation.diff,
			"path":          mutation.filePath,
			"resolved_path": mutation.resolvedPath,
		}
		if mutation.movePath != "" {
			section["move_path"] = mutation.movePath
		}
		originalText, modifiedText := previewTexts(mutation.oldContent, mutation.newContent)
		if originalText != "" || modifiedText != "" {
			section["original_text"] = originalText
			section["modified_text"] = modifiedText
		}
		sections = append(sections, section)
		if mutation.diff != "" {
			if combinedDiff.Len() > 0 {
				combinedDiff.WriteString("\n")
			}
			combinedDiff.WriteString(mutation.diff)
		}
	}
	first := mutations[0]
	oldExists := first.oldState.exists
	oldSize := first.oldState.size
	oldMTimeNS := first.oldState.mtimeNS
	originalText, modifiedText := previewTexts(first.oldContent, first.newContent)
	result := protocol.ToolPreviewResult{
		OK:           true,
		Sections:     sections,
		ResolvedPath: first.resolvedPath,
		OldSHA256:    first.oldState.sha256,
		OldExists:    &oldExists,
		OldSize:      &oldSize,
		OldMTimeNS:   &oldMTimeNS,
		Diff:         combinedDiff.String(),
		OriginalText: originalText,
		ModifiedText: modifiedText,
	}
	if staleWarning != "" {
		result.Meta = map[string]any{"warning": strings.TrimSpace(staleWarning)}
	}
	if result.Meta == nil {
		result.Meta = map[string]any{}
	}
	operations := mutationOperationStates(mutations)
	result.Meta["plan_id"] = fmt.Sprintf("mutation-%x", sha256.Sum256([]byte(time.Now().String()+first.resolvedPath)))[:41]
	result.Meta["plan_hash"] = mutationPlanHash(req.ToolName, operations, combinedDiff.String())
	result.Meta["operations"] = operationStatesAsMaps(operations)
	return result
}

func resolveRequestedCWD(currentCWD string, requested *string) (string, string) {
	cwd := currentCWD
	if requested == nil || *requested == "" {
		return cwd, ""
	}
	cwd = *requested
	if info, err := os.Stat(cwd); err != nil || !info.IsDir() {
		return currentCWD, fmt.Sprintf(
			"Warning: working directory no longer exists (%s). Reset to %s.\n",
			cwd,
			currentCWD,
		)
	}
	return cwd, ""
}

func prependWarning(r protocol.ExecToolResult, warning string) protocol.ExecToolResult {
	if warning == "" || !r.OK {
		return r
	}
	r.Result = warning + r.Result
	return r
}

func runShell(
	parentCtx context.Context,
	args map[string]any,
	cwd string,
	timeoutSec int,
	onStream func(protocol.ToolStreamChunk),
) protocol.ExecToolResult {
	command, ok := args["command"].(string)
	if !ok || strings.TrimSpace(command) == "" {
		return errorResult("REMOTE_TOOL_ERROR", "shell command must be a non-empty string")
	}
	if timeout, ok := asInt(args["timeout"]); ok && timeout > 0 {
		timeoutSec = timeout
	}
	if timeoutSec <= 0 {
		timeoutSec = 120
	}

	ctx, cancel := context.WithTimeout(parentCtx, time.Duration(timeoutSec)*time.Second)
	defer cancel()

	shell, shellArgs := buildShellCommand(command, runtime.GOOS, exec.LookPath)
	cmd := exec.CommandContext(ctx, shell, shellArgs...)
	cmd.Dir = cwd

	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}

	if ctx.Err() == context.Canceled {
		return errorResult("REMOTE_CANCELLED", "Remote execution was cancelled")
	}
	if ctx.Err() == context.DeadlineExceeded {
		return errorResult("REMOTE_TIMEOUT", fmt.Sprintf("Remote execution timed out after %ds", timeoutSec))
	}

	if err := cmd.Start(); err != nil {
		if ctx.Err() == context.Canceled {
			return errorResult("REMOTE_CANCELLED", "Remote execution was cancelled")
		}
		if ctx.Err() == context.DeadlineExceeded {
			return errorResult("REMOTE_TIMEOUT", fmt.Sprintf("Remote execution timed out after %ds", timeoutSec))
		}
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}

	var stdoutBuf bytes.Buffer
	var stderrBuf bytes.Buffer
	var mu sync.Mutex
	var wg sync.WaitGroup
	readStream := func(r io.Reader, kind string, target *bytes.Buffer) {
		defer wg.Done()
		buf := make([]byte, 4096)
		for {
			n, readErr := r.Read(buf)
			if n > 0 {
				chunk := string(buf[:n])
				mu.Lock()
				target.WriteString(chunk)
				mu.Unlock()
				if onStream != nil {
					onStream(protocol.ToolStreamChunk{ChunkType: kind, Data: chunk})
				}
			}
			if readErr != nil {
				if readErr == io.EOF {
					return
				}
				return
			}
		}
	}

	wg.Add(2)
	go readStream(stdoutPipe, "stdout", &stdoutBuf)
	go readStream(stderrPipe, "stderr", &stderrBuf)

	err = cmd.Wait()
	wg.Wait()

	if ctx.Err() == context.DeadlineExceeded {
		return errorResult("REMOTE_TIMEOUT", fmt.Sprintf("Remote execution timed out after %ds", timeoutSec))
	}
	if ctx.Err() == context.Canceled {
		return errorResult("REMOTE_CANCELLED", "Remote execution was cancelled")
	}

	out := stdoutBuf.String()
	if stderrBuf.Len() > 0 {
		if out != "" {
			out += "\n"
		}
		out += "[stderr]\n" + stderrBuf.String()
	}
	exitCode := 0
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
			if out != "" {
				out += "\n"
			}
			out += fmt.Sprintf("[exit code: %d]", exitCode)
		} else {
			return errorResult("REMOTE_TOOL_ERROR", err.Error())
		}
	}
	if strings.TrimSpace(out) == "" {
		out = "(no output)"
	}
	return protocol.ExecToolResult{OK: true, Result: out, Meta: map[string]any{"exit_code": exitCode}}
}

func buildShellCommand(
	command string,
	goos string,
	lookPath func(string) (string, error),
) (string, []string) {
	if goos != "windows" {
		return "sh", []string{"-lc", command}
	}

	if shell, ok := findGitBash(lookPath); ok {
		return shell, []string{"-c", command}
	}

	shell := "powershell.exe"
	if resolved, err := lookPath("pwsh"); err == nil {
		shell = resolved
	} else if resolved, err := lookPath("powershell.exe"); err == nil {
		shell = resolved
	}
	normalized := strings.ReplaceAll(command, "&&", ";")
	return shell, []string{
		"-NoProfile",
		"-NonInteractive",
		"-ExecutionPolicy",
		"Bypass",
		"-Command",
		normalized,
	}
}

func findGitBash(lookPath func(string) (string, error)) (string, bool) {
	for _, name := range []string{"bash.exe", "bash"} {
		if resolved, err := lookPath(name); err == nil && isGitBashPath(resolved) {
			return resolved, true
		}
	}
	for _, candidate := range commonGitBashPaths() {
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate, true
		}
	}
	return "", false
}

func isGitBashPath(candidate string) bool {
	normalized := strings.ToLower(filepath.ToSlash(candidate))
	return strings.Contains(normalized, "/git/bin/bash.exe") ||
		strings.Contains(normalized, "/git/usr/bin/bash.exe")
}

func commonGitBashPaths() []string {
	var paths []string
	if programFiles := os.Getenv("ProgramFiles"); programFiles != "" {
		paths = append(paths,
			filepath.Join(programFiles, "Git", "bin", "bash.exe"),
			filepath.Join(programFiles, "Git", "usr", "bin", "bash.exe"),
		)
	}
	if programFilesX86 := os.Getenv("ProgramFiles(x86)"); programFilesX86 != "" {
		paths = append(paths,
			filepath.Join(programFilesX86, "Git", "bin", "bash.exe"),
			filepath.Join(programFilesX86, "Git", "usr", "bin", "bash.exe"),
		)
	}
	if localAppData := os.Getenv("LOCALAPPDATA"); localAppData != "" {
		paths = append(paths,
			filepath.Join(localAppData, "Programs", "Git", "bin", "bash.exe"),
			filepath.Join(localAppData, "Programs", "Git", "usr", "bin", "bash.exe"),
		)
	}
	return paths
}

const maxOutputChars = 15_000
const maxOutputLines = 2_000

func finalizeToolResult(req protocol.ExecToolRequest, result protocol.ExecToolResult, workspaceRoot string) protocol.ExecToolResult {
	if !result.OK || result.Result == "" {
		return result
	}
	if shouldBypassOutputFinalizer(req) {
		return result
	}
	lineCount := len(strings.Split(result.Result, "\n"))
	charCount := len(result.Result)
	if lineCount <= maxOutputLines && charCount <= maxOutputChars {
		return result
	}
	if strings.TrimSpace(workspaceRoot) == "" {
		return errorResult("REMOTE_PROTOCOL_ERROR", "workspace_root is required for remote peer tool output archiving")
	}
	archivePath, err := archiveToolOutput(workspaceRoot, req.ToolName, result.Result)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	truncatedLines := strings.Split(result.Result, "\n")
	if len(truncatedLines) > maxOutputLines {
		truncatedLines = truncatedLines[:maxOutputLines]
	}
	truncated := strings.Join(truncatedLines, "\n")
	if len(truncated) > maxOutputChars {
		truncated = strings.TrimRight(truncated[:maxOutputChars], " \t\r\n")
	}
	result.Result = strings.Join([]string{
		fmt.Sprintf("[truncated] Tool output exceeded limits (%d lines, %d chars).", lineCount, charCount),
		fmt.Sprintf("Showing first %d lines and up to %d chars.", min(lineCount, maxOutputLines), maxOutputChars),
		fmt.Sprintf("Full output saved to: %s", archivePath),
		"To recover the full archived output, call read_file on that path with override=true.",
		"",
		"--- BEGIN TRUNCATED OUTPUT ---",
		truncated,
		"--- END TRUNCATED OUTPUT ---",
	}, "\n")
	if result.Meta == nil {
		result.Meta = map[string]any{}
	}
	result.Meta["tool_output_path"] = archivePath
	return result
}

func shouldBypassOutputFinalizer(req protocol.ExecToolRequest) bool {
	if req.ToolName != "read_file" {
		return false
	}
	override, _ := req.Args["override"].(bool)
	return override
}

func archiveToolOutput(workspaceRoot, toolName, content string) (string, error) {
	dayDir := filepath.Join(workspaceRoot, ".rcoder", "tool-outputs", time.Now().Format("2006-01-02"))
	if err := os.MkdirAll(dayDir, 0o755); err != nil {
		return "", err
	}
	safeTool := sanitizeArchiveName(toolName)
	if safeTool == "" {
		safeTool = "tool"
	}
	filename := fmt.Sprintf("%s-%d.txt", safeTool, time.Now().UnixNano())
	path := filepath.Join(dayDir, filename)
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		return "", err
	}
	return path, nil
}

func sanitizeArchiveName(value string) string {
	var b strings.Builder
	for _, r := range value {
		switch {
		case r >= 'a' && r <= 'z':
			b.WriteRune(r)
		case r >= 'A' && r <= 'Z':
			b.WriteRune(r)
		case r >= '0' && r <= '9':
			b.WriteRune(r)
		case r == '-' || r == '_':
			b.WriteRune(r)
		default:
			b.WriteRune('-')
		}
	}
	return strings.Trim(b.String(), "-")
}

func readFile(args map[string]any, cwd string) protocol.ExecToolResult {
	filePath, ok := args["file_path"].(string)
	if !ok || filePath == "" {
		return errorResult("REMOTE_TOOL_ERROR", "file_path must be a non-empty string")
	}
	offset, _ := asInt(args["offset"])
	if offset <= 0 {
		offset = 1
	}
	limit, _ := asInt(args["limit"])
	if limit <= 0 {
		limit = 2000
	}
	override, _ := args["override"].(bool)

	resolved, err := resolvePath(cwd, filePath)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	f, err := os.Open(resolved)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	var lines []string
	lineNo := 0
	start := offset - 1
	end := start + limit
	for scanner.Scan() {
		lineNo++
		if override {
			lines = append(lines, scanner.Text())
			continue
		}
		if lineNo > start && lineNo <= end {
			lines = append(lines, scanner.Text())
		}
	}
	if err := scanner.Err(); err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if override {
		return protocol.ExecToolResult{OK: true, Result: joinNumbered(lines, 0)}
	}
	if start >= lineNo {
		return protocol.ExecToolResult{OK: true, Result: "(empty file)"}
	}
	result := joinNumbered(lines, start)
	if result == "" {
		result = "(empty file)"
	}
	if end < lineNo {
		result += fmt.Sprintf("\n... (%d lines total, showing %d-%d; use override=true to read full file)", lineNo, start+1, min(end, lineNo))
	}
	return protocol.ExecToolResult{OK: true, Result: result}
}

type fileState struct {
	exists  bool
	sha256  string
	size    int64
	mtimeNS int64
}

type fileMutation struct {
	kind             string
	filePath         string
	movePath         string
	resolvedPath     string
	moveResolvedPath string
	oldContent       string
	newContent       string
	oldState         fileState
	diff             string
	lineCount        int
}

func (m fileMutation) diffTitle() string {
	switch m.kind {
	case "add":
		return "Proposed file add"
	case "delete":
		return "Proposed file deletion"
	case "move":
		return "Proposed file move"
	default:
		return "Proposed file diff"
	}
}

func applyPatch(args map[string]any, cwd string, expectedState *protocol.ToolMutationPreviewState) protocol.ExecToolResult {
	mutations, err := buildMutationPlan("apply_patch", args, cwd, expectedState)
	if err != nil {
		if strings.HasPrefix(err.Error(), "stale preview: ") {
			return errorResult("REMOTE_TOOL_STALE_PREVIEW", strings.TrimPrefix(err.Error(), "stale preview: "))
		}
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if err := applyMutationsTransactionally(mutations); err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	var result strings.Builder
	result.WriteString(fmt.Sprintf("Applied patch (%d file changes)", len(mutations)))
	for _, mutation := range mutations {
		if mutation.diff != "" {
			result.WriteString("\n")
			result.WriteString(mutation.diff)
		}
	}
	return protocol.ExecToolResult{OK: true, Result: result.String()}
}

func draftDocumentCommit(args map[string]any, cwd string, expectedState *protocol.ToolMutationPreviewState) protocol.ExecToolResult {
	mutations, err := buildMutationPlan("draft_document_commit", args, cwd, expectedState)
	if err != nil {
		if strings.HasPrefix(err.Error(), "stale preview: ") {
			return errorResult("REMOTE_TOOL_STALE_PREVIEW", strings.TrimPrefix(err.Error(), "stale preview: "))
		}
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if err := applyMutationsTransactionally(mutations); err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	target := ""
	if len(mutations) > 0 {
		target = mutations[0].filePath
	}
	return protocol.ExecToolResult{OK: true, Result: fmt.Sprintf("Committed document %s", target)}
}

type filePathSnapshot struct {
	path   string
	exists bool
	data   []byte
	mode   os.FileMode
}

func applyMutationsTransactionally(mutations []fileMutation) error {
	snapshots, err := snapshotMutationPaths(mutations)
	if err != nil {
		return err
	}
	if err := applyMutations(mutations); err != nil {
		_ = restoreMutationSnapshots(snapshots)
		return err
	}
	return nil
}

func applyMutations(mutations []fileMutation) error {
	for _, mutation := range mutations {
		if mutation.kind == "delete" {
			if err := os.Remove(mutation.resolvedPath); err != nil {
				return err
			}
			continue
		}
		target := mutation.resolvedPath
		if mutation.kind == "move" {
			target = mutation.moveResolvedPath
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(target, []byte(mutation.newContent), 0o644); err != nil {
			return err
		}
		if mutation.kind == "move" && filepath.Clean(mutation.resolvedPath) != filepath.Clean(target) {
			if err := os.Remove(mutation.resolvedPath); err != nil {
				return err
			}
		}
	}
	return nil
}

func snapshotMutationPaths(mutations []fileMutation) ([]filePathSnapshot, error) {
	seen := map[string]struct{}{}
	var snapshots []filePathSnapshot
	add := func(path string) error {
		if strings.TrimSpace(path) == "" {
			return nil
		}
		clean := filepath.Clean(path)
		if _, ok := seen[clean]; ok {
			return nil
		}
		seen[clean] = struct{}{}
		stat, err := os.Stat(clean)
		if err != nil {
			if os.IsNotExist(err) {
				snapshots = append(snapshots, filePathSnapshot{path: clean, exists: false})
				return nil
			}
			return err
		}
		if stat.IsDir() {
			return fmt.Errorf("%s is a directory", clean)
		}
		data, err := os.ReadFile(clean)
		if err != nil {
			return err
		}
		snapshots = append(snapshots, filePathSnapshot{
			path:   clean,
			exists: true,
			data:   data,
			mode:   stat.Mode().Perm(),
		})
		return nil
	}
	for _, mutation := range mutations {
		if err := add(mutation.resolvedPath); err != nil {
			return nil, err
		}
		if mutation.kind == "move" {
			if err := add(mutation.moveResolvedPath); err != nil {
				return nil, err
			}
		}
	}
	return snapshots, nil
}

func restoreMutationSnapshots(snapshots []filePathSnapshot) error {
	var firstErr error
	for i := len(snapshots) - 1; i >= 0; i-- {
		snapshot := snapshots[i]
		if snapshot.exists {
			if err := os.MkdirAll(filepath.Dir(snapshot.path), 0o755); err != nil && firstErr == nil {
				firstErr = err
				continue
			}
			if err := os.WriteFile(snapshot.path, snapshot.data, snapshot.mode); err != nil && firstErr == nil {
				firstErr = err
			}
			continue
		}
		if err := os.Remove(snapshot.path); err != nil && !os.IsNotExist(err) && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func lspTool(args map[string]any, cwd string) protocol.ExecToolResult {
	result, err := lsp.ExecuteOperation(args, cwd)
	if err != nil {
		return errorResult("REMOTE_LSP_ERROR", err.Error())
	}
	return protocol.ExecToolResult{OK: true, Result: result}
}

func buildMutationPlan(toolName string, args map[string]any, cwd string, expectedState *protocol.ToolMutationPreviewState) ([]fileMutation, error) {
	switch toolName {
	case "apply_patch":
		return buildPatchMutations(args, cwd, expectedState)
	case "draft_document_commit":
		return buildDocumentCommitMutations(args, cwd, expectedState)
	default:
		return nil, fmt.Errorf("unsupported mutation tool %q", toolName)
	}
}

func buildPatchMutations(args map[string]any, cwd string, expectedState *protocol.ToolMutationPreviewState) ([]fileMutation, error) {
	patch, ok := args["patch"].(string)
	if !ok || strings.TrimSpace(patch) == "" {
		return nil, fmt.Errorf("patch must be a non-empty string")
	}
	operations, err := parsePatchOperations(patch)
	if err != nil {
		return nil, err
	}
	mutations := make([]fileMutation, 0, len(operations))
	for index, op := range operations {
		resolved, err := resolveWorkspaceRelativePath(cwd, op.path)
		if err != nil {
			return nil, err
		}
		oldContent, oldState, err := readFileSnapshot(resolved)
		if err != nil {
			return nil, err
		}
		if expected := expectedOperationState(expectedState, index); expected != nil {
			if stale := validateExpectedOperationState(expected, oldState, resolved); stale != "" {
				return nil, fmt.Errorf("stale preview: %s", stale)
			}
		} else if expectedState != nil && len(expectedState.Operations) == 0 && index == 0 {
			if stale := validateExpectedState(expectedState, oldState, resolved); stale != "" {
				return nil, fmt.Errorf("stale preview: %s", stale)
			}
		}
		mutation := fileMutation{
			kind:         op.kind,
			filePath:     op.path,
			movePath:     op.movePath,
			resolvedPath: resolved,
			oldContent:   oldContent,
			oldState:     oldState,
		}
		switch op.kind {
		case "add":
			if oldState.exists {
				return nil, fmt.Errorf("file already exists: %s", op.path)
			}
			mutation.newContent, err = contentFromAddLines(op.lines)
			if err != nil {
				return nil, err
			}
		case "delete":
			if !oldState.exists {
				return nil, fmt.Errorf("file does not exist: %s", op.path)
			}
		case "update":
			if !oldState.exists {
				return nil, fmt.Errorf("file does not exist: %s", op.path)
			}
			mutation.newContent, err = applyUpdateHunks(oldContent, op.lines, op.path)
			if err != nil {
				return nil, err
			}
		case "move":
			if !oldState.exists {
				return nil, fmt.Errorf("file does not exist: %s", op.path)
			}
			if strings.TrimSpace(op.movePath) == "" {
				return nil, fmt.Errorf("move patch requires target path")
			}
			mutation.moveResolvedPath, err = resolveWorkspaceRelativePath(cwd, op.movePath)
			if err != nil {
				return nil, err
			}
			_, targetState, err := readFileSnapshot(mutation.moveResolvedPath)
			if err != nil {
				return nil, err
			}
			if targetState.exists && filepath.Clean(mutation.moveResolvedPath) != filepath.Clean(mutation.resolvedPath) {
				return nil, fmt.Errorf("move target already exists: %s", op.movePath)
			}
			if len(op.lines) == 0 {
				mutation.newContent = oldContent
			} else {
				mutation.newContent, err = applyUpdateHunks(oldContent, op.lines, op.path)
				if err != nil {
					return nil, err
				}
			}
		default:
			return nil, fmt.Errorf("unsupported patch operation %q", op.kind)
		}
		diffName := mutation.filePath
		if mutation.movePath != "" {
			diffName = mutation.movePath
		}
		mutation.diff = unifiedWholeFileDiff(oldContent, mutation.newContent, diffName)
		mutation.lineCount = countLines(mutation.newContent)
		mutations = append(mutations, mutation)
	}
	if stale := validateExpectedPlanHash("apply_patch", expectedState, mutations); stale != "" {
		return nil, fmt.Errorf("stale preview: %s", stale)
	}
	return mutations, nil
}

func buildDocumentCommitMutations(args map[string]any, cwd string, expectedState *protocol.ToolMutationPreviewState) ([]fileMutation, error) {
	targetPath, ok := args["target_path"].(string)
	if !ok || strings.TrimSpace(targetPath) == "" {
		return nil, fmt.Errorf("target_path must be a non-empty string")
	}
	content, ok := args["content"].(string)
	if !ok {
		return nil, fmt.Errorf("content must be a string")
	}
	if strings.Contains(content, "\x00") {
		return nil, fmt.Errorf("document content appears to be binary")
	}
	resolved, err := resolveWorkspaceRelativePath(cwd, targetPath)
	if err != nil {
		return nil, err
	}
	oldContent, oldState, err := readFileSnapshot(resolved)
	if err != nil {
		return nil, err
	}
	if expected := expectedOperationState(expectedState, 0); expected != nil {
		if stale := validateExpectedOperationState(expected, oldState, resolved); stale != "" {
			return nil, fmt.Errorf("stale preview: %s", stale)
		}
	} else if expectedState != nil && len(expectedState.Operations) == 0 {
		if stale := validateExpectedState(expectedState, oldState, resolved); stale != "" {
			return nil, fmt.Errorf("stale preview: %s", stale)
		}
	}
	if oldState.exists {
		return nil, fmt.Errorf("draft document target already exists; use apply_patch to modify existing files: %s", targetPath)
	}
	kind := "add"
	mutation := fileMutation{
		kind:         kind,
		filePath:     targetPath,
		resolvedPath: resolved,
		oldContent:   oldContent,
		oldState:     oldState,
		newContent:   content,
		diff:         unifiedWholeFileDiff(oldContent, content, targetPath),
		lineCount:    countLines(content),
	}
	mutations := []fileMutation{mutation}
	if stale := validateExpectedPlanHash("draft_document_commit", expectedState, mutations); stale != "" {
		return nil, fmt.Errorf("stale preview: %s", stale)
	}
	return mutations, nil
}

type patchOperation struct {
	kind     string
	path     string
	movePath string
	lines    []string
}

func parsePatchOperations(patch string) ([]patchOperation, error) {
	normalized := strings.ReplaceAll(strings.ReplaceAll(patch, "\r\n", "\n"), "\r", "\n")
	lines := strings.Split(normalized, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	if len(lines) < 2 || strings.TrimSpace(lines[0]) != "*** Begin Patch" {
		return nil, fmt.Errorf("patch must start with *** Begin Patch")
	}
	if strings.TrimSpace(lines[len(lines)-1]) != "*** End Patch" {
		return nil, fmt.Errorf("patch must end with *** End Patch")
	}
	var operations []patchOperation
	for index := 1; index < len(lines)-1; {
		line := lines[index]
		switch {
		case strings.HasPrefix(line, "*** Add File: "):
			body, next := collectPatchBody(lines, index+1)
			operations = append(operations, patchOperation{
				kind:  "add",
				path:  strings.TrimSpace(strings.TrimPrefix(line, "*** Add File: ")),
				lines: body,
			})
			index = next
		case strings.HasPrefix(line, "*** Delete File: "):
			operations = append(operations, patchOperation{
				kind: "delete",
				path: strings.TrimSpace(strings.TrimPrefix(line, "*** Delete File: ")),
			})
			index++
		case strings.HasPrefix(line, "*** Update File: "):
			op := patchOperation{
				kind: "update",
				path: strings.TrimSpace(strings.TrimPrefix(line, "*** Update File: ")),
			}
			index++
			if index < len(lines)-1 && strings.HasPrefix(lines[index], "*** Move to: ") {
				op.kind = "move"
				op.movePath = strings.TrimSpace(strings.TrimPrefix(lines[index], "*** Move to: "))
				index++
			}
			op.lines, index = collectPatchBody(lines, index)
			operations = append(operations, op)
		default:
			return nil, fmt.Errorf("unexpected patch line: %s", line)
		}
	}
	if len(operations) == 0 {
		return nil, fmt.Errorf("patch contains no file operations")
	}
	return operations, nil
}

func collectPatchBody(lines []string, index int) ([]string, int) {
	var body []string
	for index < len(lines)-1 && !strings.HasPrefix(lines[index], "*** ") {
		body = append(body, lines[index])
		index++
	}
	return body, index
}

func contentFromAddLines(lines []string) (string, error) {
	content := make([]string, 0, len(lines))
	for _, line := range lines {
		if !strings.HasPrefix(line, "+") {
			return "", fmt.Errorf("Add File lines must start with +")
		}
		content = append(content, strings.TrimPrefix(line, "+"))
	}
	if len(content) == 0 {
		return "", nil
	}
	return strings.Join(content, "\n") + "\n", nil
}

func applyUpdateHunks(oldContent string, lines []string, filePath string) (string, error) {
	current := splitDiffLines(oldContent)
	hunks, err := splitPatchHunks(lines)
	if err != nil {
		return "", err
	}
	if len(hunks) == 0 {
		return "", fmt.Errorf("Update File requires at least one hunk: %s", filePath)
	}
	cursor := 0
	for _, hunk := range hunks {
		var oldSegment []string
		var newSegment []string
		for _, line := range hunk {
			if line == "" {
				return "", fmt.Errorf("hunk lines must start with space, -, or +")
			}
			text := line[1:]
			switch line[0] {
			case ' ':
				oldSegment = append(oldSegment, text)
				newSegment = append(newSegment, text)
			case '-':
				oldSegment = append(oldSegment, text)
			case '+':
				newSegment = append(newSegment, text)
			default:
				return "", fmt.Errorf("hunk lines must start with space, -, or +")
			}
		}
		if len(oldSegment) == 0 {
			return "", fmt.Errorf("update hunk must include context or removed lines")
		}
		match, err := findUniqueSegment(current, oldSegment, cursor)
		if err != nil {
			return "", err
		}
		next := append([]string{}, current[:match]...)
		next = append(next, newSegment...)
		next = append(next, current[match+len(oldSegment):]...)
		current = next
		cursor = match + len(newSegment)
	}
	result := strings.Join(current, "\n")
	if strings.HasSuffix(oldContent, "\n") || strings.HasSuffix(oldContent, "\r") {
		result += "\n"
	}
	return result, nil
}

func splitPatchHunks(lines []string) ([][]string, error) {
	var hunks [][]string
	var current []string
	started := false
	for _, line := range lines {
		if strings.HasPrefix(line, "@@") {
			if started {
				hunks = append(hunks, current)
			}
			current = []string{}
			started = true
			continue
		}
		if !started {
			if strings.TrimSpace(line) == "" {
				continue
			}
			return nil, fmt.Errorf("Update File hunks must start with @@")
		}
		current = append(current, line)
	}
	if started {
		hunks = append(hunks, current)
	}
	return hunks, nil
}

func findUniqueSegment(lines []string, segment []string, start int) (int, error) {
	var matches []int
	for i := start; i <= len(lines)-len(segment); i++ {
		if equalStringSlices(lines[i:i+len(segment)], segment) {
			matches = append(matches, i)
		}
	}
	for i := 0; i < start && i <= len(lines)-len(segment); i++ {
		if equalStringSlices(lines[i:i+len(segment)], segment) {
			matches = append(matches, i)
		}
	}
	if len(matches) == 0 {
		return 0, fmt.Errorf("patch context does not match file")
	}
	if len(matches) > 1 {
		return 0, fmt.Errorf("patch context matches multiple locations")
	}
	return matches[0], nil
}

func equalStringSlices(left []string, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for i := range left {
		if left[i] != right[i] {
			return false
		}
	}
	return true
}

func resolveWorkspaceRelativePath(cwd, path string) (string, error) {
	if strings.TrimSpace(path) == "" {
		return "", fmt.Errorf("file path must be a non-empty string")
	}
	if filepath.IsAbs(path) {
		return "", fmt.Errorf("absolute paths are not allowed: %s", path)
	}
	clean := filepath.Clean(path)
	for _, part := range strings.Split(filepath.ToSlash(clean), "/") {
		if part == ".." {
			return "", fmt.Errorf("path traversal is not allowed: %s", path)
		}
	}
	root, err := filepath.Abs(cwd)
	if err != nil {
		return "", err
	}
	rootAbs := filepath.Clean(root)
	root, err = filepath.EvalSymlinks(root)
	if err != nil {
		root = rootAbs
	}
	resolved, err := resolveMutationPathThroughSymlinks(root, clean)
	if err != nil {
		return "", err
	}
	if !pathWithinRoot(root, resolved) {
		return "", fmt.Errorf("path escapes workspace root: %s", path)
	}
	return resolved, nil
}

func resolveMutationPathThroughSymlinks(root, rel string) (string, error) {
	current := root
	for _, part := range strings.Split(filepath.ToSlash(rel), "/") {
		if part == "" || part == "." {
			continue
		}
		next := filepath.Join(current, part)
		info, err := os.Lstat(next)
		if err != nil {
			if os.IsNotExist(err) {
				current = next
				continue
			}
			return "", err
		}
		if info.Mode()&os.ModeSymlink == 0 {
			current = next
			continue
		}
		evaluated, err := filepath.EvalSymlinks(next)
		if err != nil {
			return "", err
		}
		current = evaluated
	}
	return filepath.Clean(current), nil
}

func pathWithinRoot(root, path string) bool {
	rel, err := filepath.Rel(filepath.Clean(root), filepath.Clean(path))
	if err != nil {
		return false
	}
	return rel == "." || (rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator)))
}

func readFileSnapshot(path string) (string, fileState, error) {
	stat, err := os.Stat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return "", fileState{exists: false}, nil
		}
		return "", fileState{}, err
	}
	if stat.IsDir() {
		return "", fileState{}, fmt.Errorf("%s is a directory", path)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", fileState{}, err
	}
	sum := sha256.Sum256(data)
	return string(data), fileState{
		exists:  true,
		sha256:  fmt.Sprintf("%x", sum),
		size:    stat.Size(),
		mtimeNS: stat.ModTime().UnixNano(),
	}, nil
}

func expectedOperationState(expected *protocol.ToolMutationPreviewState, index int) *protocol.ToolMutationOperationState {
	if expected == nil || len(expected.Operations) == 0 || index < 0 || index >= len(expected.Operations) {
		return nil
	}
	return &expected.Operations[index]
}

func validateExpectedOperationState(expected *protocol.ToolMutationOperationState, current fileState, resolvedPath string) string {
	if expected == nil {
		return ""
	}
	if expected.ResolvedPath != "" && filepath.Clean(expected.ResolvedPath) != filepath.Clean(resolvedPath) {
		return fmt.Sprintf("approved preview targeted %s, current execution targets %s", expected.ResolvedPath, resolvedPath)
	}
	if expected.OldExists != nil && *expected.OldExists != current.exists {
		return "file changed since approval preview was generated"
	}
	if current.exists && expected.OldSHA256 != "" && expected.OldSHA256 != current.sha256 {
		return "file content changed since approval preview was generated"
	}
	if expected.OldSize != nil && current.exists && *expected.OldSize != current.size {
		return "file size changed since approval preview was generated"
	}
	return ""
}

func validateExpectedPlanHash(toolName string, expected *protocol.ToolMutationPreviewState, mutations []fileMutation) string {
	if expected == nil || expected.PlanHash == "" || len(expected.Operations) == 0 {
		return ""
	}
	currentHash := mutationPlanHash(toolName, mutationOperationStates(mutations), combinedMutationDiff(mutations))
	if expected.PlanHash != currentHash {
		return "approved preview plan no longer matches current file state"
	}
	return ""
}

func validateExpectedState(expected *protocol.ToolMutationPreviewState, current fileState, resolvedPath string) string {
	if expected == nil {
		return ""
	}
	if expected.ResolvedPath != "" && filepath.Clean(expected.ResolvedPath) != filepath.Clean(resolvedPath) {
		return fmt.Sprintf("approved preview targeted %s, current execution targets %s", expected.ResolvedPath, resolvedPath)
	}
	if expected.OldExists != nil && *expected.OldExists != current.exists {
		return "file changed since approval preview was generated"
	}
	if current.exists && expected.OldSHA256 != "" && expected.OldSHA256 != current.sha256 {
		return "file content changed since approval preview was generated"
	}
	if expected.OldSize != nil && current.exists && *expected.OldSize != current.size {
		return "file size changed since approval preview was generated"
	}
	return ""
}

func mutationOperationStates(mutations []fileMutation) []protocol.ToolMutationOperationState {
	states := make([]protocol.ToolMutationOperationState, 0, len(mutations))
	for _, mutation := range mutations {
		oldExists := mutation.oldState.exists
		oldSize := mutation.oldState.size
		state := protocol.ToolMutationOperationState{
			Kind:             mutation.kind,
			Path:             mutation.filePath,
			MovePath:         mutation.movePath,
			ResolvedPath:     mutation.resolvedPath,
			MoveResolvedPath: mutation.moveResolvedPath,
			OldExists:        &oldExists,
		}
		if mutation.oldState.exists {
			state.OldSHA256 = mutation.oldState.sha256
			state.OldSize = &oldSize
		}
		states = append(states, state)
	}
	return states
}

func operationStatesAsMaps(states []protocol.ToolMutationOperationState) []map[string]any {
	items := make([]map[string]any, 0, len(states))
	for _, state := range states {
		item := map[string]any{
			"kind":          state.Kind,
			"path":          state.Path,
			"resolved_path": state.ResolvedPath,
			"old_exists":    state.OldExists,
		}
		if state.MovePath != "" {
			item["move_path"] = state.MovePath
		}
		if state.MoveResolvedPath != "" {
			item["move_resolved_path"] = state.MoveResolvedPath
		}
		if state.OldSHA256 != "" {
			item["old_sha256"] = state.OldSHA256
		}
		if state.OldSize != nil {
			item["old_size"] = state.OldSize
		}
		items = append(items, item)
	}
	return items
}

func mutationPlanHash(toolName string, operations []protocol.ToolMutationOperationState, diff string) string {
	payload := map[string]any{
		"tool_name":  toolName,
		"operations": operationStatesAsMaps(operations),
		"diff":       diff,
	}
	data, err := json.Marshal(payload)
	if err != nil {
		sum := sha256.Sum256([]byte(fmt.Sprintf("%#v", payload)))
		return fmt.Sprintf("%x", sum)
	}
	sum := sha256.Sum256(data)
	return fmt.Sprintf("%x", sum)
}

func combinedMutationDiff(mutations []fileMutation) string {
	var b strings.Builder
	for _, mutation := range mutations {
		if mutation.diff == "" {
			continue
		}
		if b.Len() > 0 {
			b.WriteString("\n")
		}
		b.WriteString(mutation.diff)
	}
	return b.String()
}

func buildEditedContent(oldContent, oldString, newString string) (string, int) {
	count := strings.Count(oldContent, oldString)
	if count == 1 {
		return strings.Replace(oldContent, oldString, newString, 1), 1
	}
	if count != 0 {
		return "", count
	}
	return buildEditedContentByNormalizedLineEndings(oldContent, oldString, newString)
}

func buildEditedContentByNormalizedLineEndings(oldContent, oldString, newString string) (string, int) {
	normalizedContent, starts, ends := normalizeLineEndingsWithMap(oldContent)
	normalizedOld := normalizeLineEndings(oldString)
	count := strings.Count(normalizedContent, normalizedOld)
	if count != 1 {
		return "", count
	}
	start := strings.Index(normalizedContent, normalizedOld)
	if start < 0 || len(normalizedOld) == 0 {
		return "", count
	}
	end := start + len(normalizedOld) - 1
	if start >= len(starts) || end >= len(ends) {
		return "", 0
	}
	originalStart := starts[start]
	originalEnd := ends[end]
	matched := oldContent[originalStart:originalEnd]
	replacement := convertLineEndings(newString, dominantLineEnding(matched))
	return oldContent[:originalStart] + replacement + oldContent[originalEnd:], 1
}

func normalizeLineEndings(value string) string {
	value = strings.ReplaceAll(value, "\r\n", "\n")
	return strings.ReplaceAll(value, "\r", "\n")
}

func normalizeLineEndingsWithMap(value string) (string, []int, []int) {
	var b strings.Builder
	b.Grow(len(value))
	starts := make([]int, 0, len(value))
	ends := make([]int, 0, len(value))
	for i := 0; i < len(value); {
		if value[i] == '\r' {
			if i+1 < len(value) && value[i+1] == '\n' {
				b.WriteByte('\n')
				starts = append(starts, i)
				ends = append(ends, i+2)
				i += 2
				continue
			}
			b.WriteByte('\n')
			starts = append(starts, i)
			ends = append(ends, i+1)
			i++
			continue
		}
		b.WriteByte(value[i])
		starts = append(starts, i)
		ends = append(ends, i+1)
		i++
	}
	return b.String(), starts, ends
}

func dominantLineEnding(value string) string {
	crlf := strings.Count(value, "\r\n")
	withoutCRLF := strings.ReplaceAll(value, "\r\n", "")
	lf := strings.Count(withoutCRLF, "\n")
	cr := strings.Count(withoutCRLF, "\r")
	if crlf > 0 && crlf >= lf && crlf >= cr {
		return "\r\n"
	}
	if lf > 0 && lf >= cr {
		return "\n"
	}
	if cr > 0 {
		return "\r"
	}
	return "\n"
}

func convertLineEndings(value, newline string) string {
	normalized := normalizeLineEndings(value)
	if newline == "\n" {
		return normalized
	}
	return strings.ReplaceAll(normalized, "\n", newline)
}

func unifiedWholeFileDiff(oldContent, newContent, filename string) string {
	if oldContent == newContent {
		return ""
	}
	oldLines := splitDiffLines(oldContent)
	newLines := splitDiffLines(newContent)
	var b strings.Builder
	b.WriteString(fmt.Sprintf("--- a/%s\n", filename))
	b.WriteString(fmt.Sprintf("+++ b/%s\n", filename))
	b.WriteString(fmt.Sprintf("@@ -1,%d +1,%d @@\n", len(oldLines), len(newLines)))
	for _, line := range oldLines {
		b.WriteString("-")
		b.WriteString(line)
		b.WriteString("\n")
	}
	for _, line := range newLines {
		b.WriteString("+")
		b.WriteString(line)
		b.WriteString("\n")
	}
	result := b.String()
	if len(result) > 20000 {
		return result[:19000] + "\n... (diff truncated)\n"
	}
	return result
}

func splitDiffLines(content string) []string {
	if content == "" {
		return []string{}
	}
	normalized := strings.ReplaceAll(content, "\r\n", "\n")
	lines := strings.Split(normalized, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	return lines
}

func previewTexts(oldContent, newContent string) (string, string) {
	if len(oldContent)+len(newContent) > 512000 {
		return "", ""
	}
	return oldContent, newContent
}

func countLines(content string) int {
	if content == "" {
		return 0
	}
	return strings.Count(content, "\n") + 1
}

func globFiles(args map[string]any, cwd string) protocol.ExecToolResult {
	pattern, ok := args["pattern"].(string)
	if !ok || pattern == "" {
		return errorResult("REMOTE_TOOL_ERROR", "pattern must be a non-empty string")
	}
	pathValue, _ := args["path"].(string)
	if pathValue == "" {
		pathValue = "."
	}
	base, err := resolvePath(cwd, pathValue)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	hasGlobstar := strings.Contains(pattern, "**")
	var re *regexp.Regexp
	if hasGlobstar {
		re, err = compileGlobRegex(pattern)
		if err != nil {
			return errorResult("REMOTE_TOOL_ERROR", fmt.Sprintf("invalid glob pattern: %v", err))
		}
	}
	var matches []string
	walkErr := filepath.WalkDir(base, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			if _, skip := skipDirs[d.Name()]; skip && path != base {
				return filepath.SkipDir
			}
			return nil
		}
		rel, err := filepath.Rel(base, path)
		if err != nil {
			return nil
		}
		relNorm := filepath.ToSlash(rel)
		if hasGlobstar {
			if re.MatchString(relNorm) {
				matches = append(matches, path)
			}
		} else {
			matched, err := pathpkg.Match(pattern, relNorm)
			if err == nil && matched {
				matches = append(matches, path)
			}
		}
		return nil
	})
	if walkErr != nil {
		return errorResult("REMOTE_TOOL_ERROR", walkErr.Error())
	}
	sortByMtime(matches)
	if len(matches) == 0 {
		return protocol.ExecToolResult{OK: true, Result: "No files matched."}
	}
	if len(matches) > 100 {
		matches = append(matches[:100], fmt.Sprintf("... (%d matches, showing first 100)", len(matches)))
	}
	return protocol.ExecToolResult{OK: true, Result: strings.Join(dedupe(matches), "\n")}
}

func sortByMtime(paths []string) {
	type entry struct {
		path  string
		mtime int64
	}
	entries := make([]entry, len(paths))
	for i, p := range paths {
		entries[i].path = p
		if info, err := os.Stat(p); err == nil {
			entries[i].mtime = info.ModTime().UnixNano()
		}
	}
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].mtime > entries[j].mtime
	})
	for i, e := range entries {
		paths[i] = e.path
	}
}

func compileGlobRegex(pattern string) (*regexp.Regexp, error) {
	var buf strings.Builder
	buf.WriteString("^")
	for i := 0; i < len(pattern); i++ {
		c := pattern[i]
		switch {
		case c == '*' && i+1 < len(pattern) && pattern[i+1] == '*':
			if i+2 < len(pattern) && pattern[i+2] == '/' {
				buf.WriteString("(?:.*/)?")
				i += 2
			} else {
				buf.WriteString(".*")
				i++
			}
		case c == '*':
			buf.WriteString("[^/]*")
		case c == '?':
			buf.WriteString("[^/]")
		case strings.ContainsRune(`.+()|^${}\`, rune(c)):
			buf.WriteByte('\\')
			buf.WriteByte(c)
		default:
			buf.WriteByte(c)
		}
	}
	buf.WriteString("$")
	return regexp.Compile(buf.String())
}

func grepFiles(args map[string]any, cwd string) protocol.ExecToolResult {
	pattern, ok := args["pattern"].(string)
	if !ok || pattern == "" {
		return errorResult("REMOTE_TOOL_ERROR", "pattern must be a non-empty string")
	}
	pathValue, _ := args["path"].(string)
	if pathValue == "" {
		pathValue = "."
	}
	include, _ := args["include"].(string)
	base, err := resolvePath(cwd, pathValue)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	re, err := regexp.Compile(pattern)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", fmt.Sprintf("Invalid regex: %v", err))
	}
	var files []string
	stat, err := os.Stat(base)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if stat.IsDir() {
		_ = filepath.WalkDir(base, func(path string, d os.DirEntry, err error) error {
			if err != nil {
				return nil
			}
			if d.IsDir() {
				if _, skip := skipDirs[d.Name()]; skip && path != base {
					return filepath.SkipDir
				}
				return nil
			}
			if include != "" {
				matched, matchErr := filepath.Match(include, filepath.Base(path))
				if matchErr != nil || !matched {
					return nil
				}
			}
			files = append(files, path)
			return nil
		})
	} else {
		files = append(files, base)
	}
	var matches []string
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			continue
		}
		for idx, line := range strings.Split(strings.ReplaceAll(string(data), "\r\n", "\n"), "\n") {
			if re.MatchString(line) {
				matches = append(matches, fmt.Sprintf("%s:%d: %s", file, idx+1, line))
				if len(matches) >= 200 {
					matches = append(matches, "... (200 match limit reached)")
					return protocol.ExecToolResult{OK: true, Result: strings.Join(matches, "\n")}
				}
			}
		}
	}
	if len(matches) == 0 {
		return protocol.ExecToolResult{OK: true, Result: "No matches found."}
	}
	return protocol.ExecToolResult{OK: true, Result: strings.Join(matches, "\n")}
}

func listFile(args map[string]any, cwd string) protocol.ExecToolResult {
	pathValue, _ := args["path"].(string)
	if pathValue == "" {
		pathValue = "."
	}
	all := true
	if v, ok := args["all"].(bool); ok {
		all = v
	}
	long := true
	if v, ok := args["long"].(bool); ok {
		long = v
	}
	recursive := false
	if v, ok := args["recursive"].(bool); ok {
		recursive = v
	}
	pattern, _ := args["pattern"].(string)

	base, err := resolvePath(cwd, pathValue)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	info, err := os.Stat(base)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if !info.IsDir() {
		name := sanitizeName(info.Name())
		if long {
			return protocol.ExecToolResult{
				OK:     true,
				Result: fmt.Sprintf("%s  %8d  %s  %s", modeString(info.Mode()), info.Size(), mtimeString(info.ModTime()), name),
			}
		}
		return protocol.ExecToolResult{OK: true, Result: name}
	}

	lines, err := collectDirEntries(base, all, long, pattern, recursive)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if len(lines) == 0 {
		if pattern != "" {
			return protocol.ExecToolResult{OK: true, Result: fmt.Sprintf("(no entries matching %q in %q)", pattern, base)}
		}
		return protocol.ExecToolResult{OK: true, Result: fmt.Sprintf("(empty directory: %q)", base)}
	}
	header := ""
	if long {
		header = fmt.Sprintf("%s:\n", base)
	}
	return protocol.ExecToolResult{OK: true, Result: header + strings.Join(lines, "\n")}
}

func collectDirEntries(
	base string,
	all bool,
	long bool,
	pattern string,
	recursive bool,
) ([]string, error) {
	entries, err := os.ReadDir(base)
	if err != nil {
		return nil, err
	}

	type dirEntry struct {
		name  string
		mode  os.FileMode
		isDir bool
		size  int64
		mtime time.Time
	}
	list := make([]dirEntry, 0, len(entries))
	for _, entry := range entries {
		if !all && strings.HasPrefix(entry.Name(), ".") {
			continue
		}
		if pattern != "" {
			matched, matchErr := filepath.Match(pattern, entry.Name())
			if matchErr != nil || !matched {
				continue
			}
		}
		info, infoErr := entry.Info()
		if infoErr != nil {
			list = append(list, dirEntry{name: entry.Name(), isDir: entry.IsDir()})
			continue
		}
		list = append(list, dirEntry{
			name:  entry.Name(),
			mode:  info.Mode(),
			isDir: entry.IsDir(),
			size:  info.Size(),
			mtime: info.ModTime(),
		})
	}

	sort.Slice(list, func(i, j int) bool {
		if list[i].isDir != list[j].isDir {
			return list[i].isDir
		}
		return strings.ToLower(list[i].name) < strings.ToLower(list[j].name)
	})

	lines := make([]string, 0, len(list))
	for _, entry := range list {
		safeName := sanitizeName(entry.name)
		if entry.isDir {
			safeName += "/"
		}
		if long {
			lines = append(lines, fmt.Sprintf("%s  %8d  %s  %s", modeString(entry.mode), entry.size, mtimeString(entry.mtime), safeName))
		} else {
			lines = append(lines, safeName)
		}
	}

	if recursive {
		for _, entry := range list {
			if !entry.isDir {
				continue
			}
			if !all && strings.HasPrefix(entry.name, ".") {
				continue
			}
			subPath := filepath.Join(base, entry.name)
			subLines, subErr := collectDirEntries(subPath, all, long, pattern, true)
			if subErr == nil && len(subLines) > 0 {
				lines = append(lines, "")
				lines = append(lines, subLines...)
			}
		}
	}

	return lines, nil
}

func modeString(mode os.FileMode) string {
	b := make([]byte, 10)
	if mode.IsDir() {
		b[0] = 'd'
	} else {
		b[0] = '-'
	}
	perm := mode.Perm()
	b[1] = rwx(perm, 2)
	b[2] = rwx(perm, 1)
	b[3] = rwx(perm, 0)
	b[4] = rwx(perm>>3, 2)
	b[5] = rwx(perm>>3, 1)
	b[6] = rwx(perm>>3, 0)
	b[7] = rwx(perm>>6, 2)
	b[8] = rwx(perm>>6, 1)
	b[9] = rwx(perm>>6, 0)
	return string(b)
}

func sanitizeName(name string) string {
	var b strings.Builder
	for _, r := range name {
		switch r {
		case '`', '*', '_', '[', ']', '|', '<', '>':
			b.WriteRune('\\')
		}
		b.WriteRune(r)
	}
	return b.String()
}

func rwx(perm os.FileMode, bit uint) byte {
	if perm&(1<<(2-bit)) == 0 {
		return '-'
	}
	switch bit {
	case 2:
		return 'r'
	case 1:
		return 'w'
	default:
		return 'x'
	}
}

func mtimeString(t time.Time) string {
	return t.Format("Jan _2 15:04")
}

func resolvePath(cwd, path string) (string, error) {
	if filepath.IsAbs(path) {
		return filepath.Clean(path), nil
	}
	if cwd == "" {
		return filepath.Abs(path)
	}
	return filepath.Abs(filepath.Join(cwd, path))
}

func joinNumbered(lines []string, start int) string {
	if len(lines) == 0 {
		return "(empty file)"
	}
	parts := make([]string, 0, len(lines))
	for i, line := range lines {
		parts = append(parts, fmt.Sprintf("%d\t%s", start+i+1, line))
	}
	return strings.Join(parts, "\n")
}

func errorResult(code, message string) protocol.ExecToolResult {
	return protocol.ExecToolResult{OK: false, ErrorCode: code, ErrorMessage: message}
}

func asInt(v any) (int, bool) {
	switch n := v.(type) {
	case int:
		return n, true
	case int32:
		return int(n), true
	case int64:
		return int(n), true
	case float64:
		return int(n), true
	default:
		return 0, false
	}
}

func dedupe(items []string) []string {
	seen := map[string]struct{}{}
	out := make([]string, 0, len(items))
	for _, item := range items {
		if _, ok := seen[item]; ok {
			continue
		}
		seen[item] = struct{}{}
		out = append(out, item)
	}
	return out
}

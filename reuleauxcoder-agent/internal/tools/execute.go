package tools

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
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
	case "write_file":
		result = writeFile(req.Args, cwd, req.ExpectedState)
	case "edit_file":
		result = editFile(req.Args, cwd, req.ExpectedState)
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
	if result.OK && (req.ToolName == "write_file" || req.ToolName == "edit_file") {
		result = appendLSPDiagnostics(result, req.Args, cwd)
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
	if req.ToolName != "write_file" && req.ToolName != "edit_file" {
		return protocol.ToolPreviewResult{
			OK:           false,
			ErrorCode:    "REMOTE_TOOL_PREVIEW_UNSUPPORTED",
			ErrorMessage: fmt.Sprintf("tool %q does not support preview", req.ToolName),
		}
	}
	mutation, err := buildFileMutation(req.ToolName, req.Args, cwd)
	if err != nil {
		return protocol.ToolPreviewResult{
			OK:           false,
			ErrorCode:    "REMOTE_TOOL_ERROR",
			ErrorMessage: err.Error(),
		}
	}
	oldExists := mutation.oldState.exists
	oldSize := mutation.oldState.size
	oldMTimeNS := mutation.oldState.mtimeNS
	section := map[string]any{
		"id":            "diff",
		"title":         mutation.diffTitle(),
		"kind":          "diff",
		"content":       mutation.diff,
		"path":          mutation.filePath,
		"resolved_path": mutation.resolvedPath,
	}
	originalText, modifiedText := previewTexts(mutation.oldContent, mutation.newContent)
	if originalText != "" || modifiedText != "" {
		section["original_text"] = originalText
		section["modified_text"] = modifiedText
	}
	result := protocol.ToolPreviewResult{
		OK:           true,
		Sections:     []map[string]any{section},
		ResolvedPath: mutation.resolvedPath,
		OldSHA256:    mutation.oldState.sha256,
		OldExists:    &oldExists,
		OldSize:      &oldSize,
		OldMTimeNS:   &oldMTimeNS,
		Diff:         mutation.diff,
		OriginalText: originalText,
		ModifiedText: modifiedText,
	}
	if staleWarning != "" {
		result.Meta = map[string]any{"warning": strings.TrimSpace(staleWarning)}
	}
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

func writeFile(args map[string]any, cwd string, expectedState *protocol.ToolMutationPreviewState) protocol.ExecToolResult {
	mutation, err := buildFileMutation("write_file", args, cwd)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if stale := validateExpectedState(expectedState, mutation.oldState, mutation.resolvedPath); stale != "" {
		return errorResult("REMOTE_TOOL_STALE_PREVIEW", stale)
	}
	if err := os.MkdirAll(filepath.Dir(mutation.resolvedPath), 0o755); err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if err := os.WriteFile(mutation.resolvedPath, []byte(mutation.newContent), 0o644); err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	return protocol.ExecToolResult{OK: true, Result: fmt.Sprintf("Wrote %d lines to %s", mutation.lineCount, mutation.filePath)}
}

func editFile(args map[string]any, cwd string, expectedState *protocol.ToolMutationPreviewState) protocol.ExecToolResult {
	mutation, err := buildFileMutation("edit_file", args, cwd)
	if err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	if stale := validateExpectedState(expectedState, mutation.oldState, mutation.resolvedPath); stale != "" {
		return errorResult("REMOTE_TOOL_STALE_PREVIEW", stale)
	}
	if err := os.WriteFile(mutation.resolvedPath, []byte(mutation.newContent), 0o644); err != nil {
		return errorResult("REMOTE_TOOL_ERROR", err.Error())
	}
	result := fmt.Sprintf("Edited %s", mutation.filePath)
	if mutation.diff != "" {
		result += "\n" + mutation.diff
	}
	return protocol.ExecToolResult{OK: true, Result: result}
}

func lspTool(args map[string]any, cwd string) protocol.ExecToolResult {
	result, err := lsp.ExecuteOperation(args, cwd)
	if err != nil {
		return errorResult("REMOTE_LSP_ERROR", err.Error())
	}
	return protocol.ExecToolResult{OK: true, Result: result}
}

func appendLSPDiagnostics(result protocol.ExecToolResult, args map[string]any, cwd string) protocol.ExecToolResult {
	filePath, _ := args["file_path"].(string)
	if strings.TrimSpace(filePath) == "" {
		return result
	}
	diagnostics := lsp.DiagnosticsAfterEdit(filePath, cwd)
	if strings.TrimSpace(diagnostics) == "" {
		return result
	}
	result.Result = strings.TrimRight(result.Result, "\n") + "\n\n" + diagnostics
	return result
}

type fileState struct {
	exists  bool
	sha256  string
	size    int64
	mtimeNS int64
}

type fileMutation struct {
	toolName     string
	filePath     string
	resolvedPath string
	oldContent   string
	newContent   string
	oldState     fileState
	diff         string
	lineCount    int
}

func (m fileMutation) diffTitle() string {
	if m.toolName == "edit_file" {
		return "Proposed edit diff"
	}
	return "Proposed file diff"
}

func buildFileMutation(toolName string, args map[string]any, cwd string) (*fileMutation, error) {
	filePath, ok := args["file_path"].(string)
	if !ok || filePath == "" {
		return nil, fmt.Errorf("file_path must be a non-empty string")
	}
	resolved, err := resolvePath(cwd, filePath)
	if err != nil {
		return nil, err
	}
	oldContent, oldState, err := readFileSnapshot(resolved)
	if err != nil {
		return nil, err
	}

	var newContent string
	switch toolName {
	case "write_file":
		content, ok := args["content"].(string)
		if !ok {
			return nil, fmt.Errorf("content must be a string")
		}
		newContent = content
	case "edit_file":
		if !oldState.exists {
			return nil, fmt.Errorf("file does not exist: %s", filePath)
		}
		oldString, ok := args["old_string"].(string)
		if !ok {
			return nil, fmt.Errorf("old_string must be a string")
		}
		newString, ok := args["new_string"].(string)
		if !ok {
			return nil, fmt.Errorf("new_string must be a string")
		}
		if oldString == newString {
			return nil, fmt.Errorf("old_string and new_string must differ")
		}
		var count int
		newContent, count = buildEditedContent(oldContent, oldString, newString)
		if count == 0 {
			return nil, fmt.Errorf("old_string not found in %s", filePath)
		}
		if count > 1 {
			return nil, fmt.Errorf("old_string appears %d times in %s", count, filePath)
		}
	default:
		return nil, fmt.Errorf("unsupported tool %q", toolName)
	}

	return &fileMutation{
		toolName:     toolName,
		filePath:     filePath,
		resolvedPath: resolved,
		oldContent:   oldContent,
		newContent:   newContent,
		oldState:     oldState,
		diff:         unifiedWholeFileDiff(oldContent, newContent, filePath),
		lineCount:    countLines(newContent),
	}, nil
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

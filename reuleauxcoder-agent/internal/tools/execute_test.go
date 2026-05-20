package tools

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestBuildShellCommandUsesShOutsideWindows(t *testing.T) {
	shell, args := buildShellCommand("echo hi", "linux", func(string) (string, error) {
		return "", errors.New("unused")
	})

	if shell != "sh" {
		t.Fatalf("shell = %q, want sh", shell)
	}
	wantArgs := []string{"-lc", "echo hi"}
	if !reflect.DeepEqual(args, wantArgs) {
		t.Fatalf("args = %#v, want %#v", args, wantArgs)
	}
}

func TestBuildShellCommandPrefersBashOnWindows(t *testing.T) {
	shell, args := buildShellCommand("echo hi", "windows", func(name string) (string, error) {
		if name == "bash" {
			return "C:/Program Files/Git/bin/bash.exe", nil
		}
		return "", errors.New("not found")
	})

	if shell != "C:/Program Files/Git/bin/bash.exe" {
		t.Fatalf("shell = %q, want Git Bash", shell)
	}
	wantArgs := []string{"-c", "echo hi"}
	if !reflect.DeepEqual(args, wantArgs) {
		t.Fatalf("args = %#v, want %#v", args, wantArgs)
	}
}

func TestBuildShellCommandFallsBackToPwshOnWindows(t *testing.T) {
	shell, args := buildShellCommand("echo hi", "windows", func(name string) (string, error) {
		if name == "bash" || name == "bash.exe" {
			return "C:/Windows/System32/bash.exe", nil
		}
		if name == "pwsh" {
			return "C:/Program Files/PowerShell/7/pwsh.exe", nil
		}
		return "", errors.New("not found")
	})

	if shell != "C:/Program Files/PowerShell/7/pwsh.exe" {
		t.Fatalf("shell = %q, want pwsh path", shell)
	}
	wantArgs := []string{"-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "echo hi"}
	if !reflect.DeepEqual(args, wantArgs) {
		t.Fatalf("args = %#v, want %#v", args, wantArgs)
	}
}

func TestBuildShellCommandFallsBackToWindowsPowerShell(t *testing.T) {
	shell, args := buildShellCommand("echo a && echo b", "windows", func(name string) (string, error) {
		if name == "powershell.exe" {
			return "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", nil
		}
		return "", errors.New("not found")
	})

	if shell != "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe" {
		t.Fatalf("shell = %q, want powershell.exe path", shell)
	}
	if got := args[len(args)-1]; got != "echo a ; echo b" {
		t.Fatalf("normalized command = %q, want %q", got, "echo a ; echo b")
	}
}

func TestExecuteShellReturnsRemoteCancelledWhenContextCancelled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	result := ExecuteWithContext(ctx, protocol.ExecToolRequest{
		ToolName:   "shell",
		Args:       map[string]any{"command": "echo should-not-run"},
		TimeoutSec: 30,
	}, t.TempDir(), t.TempDir(), nil)

	if result.OK || result.ErrorCode != "REMOTE_CANCELLED" {
		t.Fatalf("result = %#v, want REMOTE_CANCELLED", result)
	}
}

func TestExecuteShellNonZeroExitReturnsToolOutput(t *testing.T) {
	dir := t.TempDir()

	result := Execute(protocol.ExecToolRequest{
		ToolName: "shell",
		Args:     map[string]any{"command": "exit 7"},
	}, dir, nil)

	if !result.OK {
		t.Fatalf("result = %#v, want OK tool result", result)
	}
	if result.Meta["exit_code"] != 7 {
		t.Fatalf("exit_code = %#v, want 7", result.Meta["exit_code"])
	}
	if !strings.Contains(result.Result, "[exit code: 7]") {
		t.Fatalf("result output = %q, want exit code text", result.Result)
	}
}

func TestPreviewWriteFileDoesNotWriteAndExecuteDetectsStaleFile(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "notes.txt")
	if err := os.WriteFile(target, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	req := protocol.ToolPreviewRequest{
		ToolName: "write_file",
		Args: map[string]any{
			"file_path": "notes.txt",
			"content":   "new\n",
		},
	}
	preview := Preview(req, dir)
	if !preview.OK {
		t.Fatalf("preview failed: %s", preview.ErrorMessage)
	}
	if got := readFileForTest(t, target); got != "old\n" {
		t.Fatalf("preview wrote file, got %q", got)
	}
	if !strings.Contains(preview.Diff, "-old") || !strings.Contains(preview.Diff, "+new") {
		t.Fatalf("preview diff = %q", preview.Diff)
	}
	if preview.OriginalText != "old\n" || preview.ModifiedText != "new\n" {
		t.Fatalf("preview texts = %q -> %q", preview.OriginalText, preview.ModifiedText)
	}

	if err := os.WriteFile(target, []byte("changed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	result := Execute(protocol.ExecToolRequest{
		ToolName:      "write_file",
		Args:          req.Args,
		ExpectedState: expectedStateFromPreview(preview),
	}, dir, nil)
	if result.OK || result.ErrorCode != "REMOTE_TOOL_STALE_PREVIEW" {
		t.Fatalf("result = %#v, want stale preview error", result)
	}
	if got := readFileForTest(t, target); got != "changed\n" {
		t.Fatalf("stale execute changed file, got %q", got)
	}
}

func TestExecuteWriteFileIgnoresPreviewMTimeWhenContentMatches(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "notes.txt")
	if err := os.WriteFile(target, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	req := protocol.ToolPreviewRequest{
		ToolName: "write_file",
		Args: map[string]any{
			"file_path": "notes.txt",
			"content":   "new\n",
		},
	}
	preview := Preview(req, dir)
	if !preview.OK {
		t.Fatalf("preview failed: %s", preview.ErrorMessage)
	}
	older := time.Now().Add(-2 * time.Hour)
	if err := os.Chtimes(target, older, older); err != nil {
		t.Fatal(err)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName:      "write_file",
		Args:          req.Args,
		ExpectedState: expectedStateFromPreview(preview),
	}, dir, nil)
	if !result.OK {
		t.Fatalf("execute failed: %#v", result)
	}
	if got := readFileForTest(t, target); got != "new\n" {
		t.Fatalf("execute content = %q", got)
	}
}

func TestPreviewAndExecuteEditFileShareValidationAndState(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "main.txt")
	if err := os.WriteFile(target, []byte("alpha beta\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	preview := Preview(protocol.ToolPreviewRequest{
		ToolName: "edit_file",
		Args: map[string]any{
			"file_path":  "main.txt",
			"old_string": "alpha",
			"new_string": "omega",
		},
	}, dir)
	if !preview.OK {
		t.Fatalf("preview failed: %s", preview.ErrorMessage)
	}
	if got := readFileForTest(t, target); got != "alpha beta\n" {
		t.Fatalf("preview edited file, got %q", got)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName: "edit_file",
		Args: map[string]any{
			"file_path":  "main.txt",
			"old_string": "alpha",
			"new_string": "omega",
		},
		ExpectedState: expectedStateFromPreview(preview),
	}, dir, nil)
	if !result.OK {
		t.Fatalf("execute failed: %#v", result)
	}
	if !strings.Contains(result.Result, "--- a/main.txt") || !strings.Contains(result.Result, "+omega beta") {
		t.Fatalf("execute diff = %q", result.Result)
	}
	if got := readFileForTest(t, target); got != "omega beta\n" {
		t.Fatalf("execute content = %q", got)
	}
}

func TestEditFileMatchesOldStringAcrossLineEndings(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "main.txt")
	if err := os.WriteFile(target, []byte("alpha\r\nbeta\r\ngamma\r\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	req := protocol.ToolPreviewRequest{
		ToolName: "edit_file",
		Args: map[string]any{
			"file_path":  "main.txt",
			"old_string": "alpha\nbeta",
			"new_string": "one\ntwo",
		},
	}
	preview := Preview(req, dir)
	if !preview.OK {
		t.Fatalf("preview failed: %s", preview.ErrorMessage)
	}
	if got := readFileForTest(t, target); got != "alpha\r\nbeta\r\ngamma\r\n" {
		t.Fatalf("preview edited file, got %q", got)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName:      "edit_file",
		Args:          req.Args,
		ExpectedState: expectedStateFromPreview(preview),
	}, dir, nil)
	if !result.OK {
		t.Fatalf("execute failed: %#v", result)
	}
	if got := readFileForTest(t, target); got != "one\r\ntwo\r\ngamma\r\n" {
		t.Fatalf("execute content = %q", got)
	}
}

func TestEditFileLineEndingFallbackPreservesSafeMatchCounts(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "main.txt")
	if err := os.WriteFile(target, []byte("alpha\r\nbeta\r\nalpha\r\nbeta\r\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	duplicate := Preview(protocol.ToolPreviewRequest{
		ToolName: "edit_file",
		Args: map[string]any{
			"file_path":  "main.txt",
			"old_string": "alpha\nbeta",
			"new_string": "one\ntwo",
		},
	}, dir)
	if duplicate.OK || !strings.Contains(duplicate.ErrorMessage, "appears 2 times") {
		t.Fatalf("preview = %#v, want duplicate old_string error", duplicate)
	}

	missing := Preview(protocol.ToolPreviewRequest{
		ToolName: "edit_file",
		Args: map[string]any{
			"file_path":  "main.txt",
			"old_string": "missing\nbeta",
			"new_string": "one\ntwo",
		},
	}, dir)
	if missing.OK || !strings.Contains(missing.ErrorMessage, "old_string not found") {
		t.Fatalf("preview = %#v, want missing old_string error", missing)
	}
}

func TestPreviewEditFileRejectsDuplicateOldString(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "main.txt")
	if err := os.WriteFile(target, []byte("same same\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	preview := Preview(protocol.ToolPreviewRequest{
		ToolName: "edit_file",
		Args: map[string]any{
			"file_path":  "main.txt",
			"old_string": "same",
			"new_string": "other",
		},
	}, dir)
	if preview.OK || !strings.Contains(preview.ErrorMessage, "appears 2 times") {
		t.Fatalf("preview = %#v, want duplicate old_string error", preview)
	}
	if got := readFileForTest(t, target); got != "same same\n" {
		t.Fatalf("failed preview changed file, got %q", got)
	}
}

func TestExecuteFallsBackWhenRequestedCWDIsStale(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "notes.txt"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	stale := filepath.Join(dir, "missing")

	result := Execute(protocol.ExecToolRequest{
		ToolName: "read_file",
		CWD:      &stale,
		Args:     map[string]any{"file_path": "notes.txt"},
	}, dir, nil)

	if !result.OK {
		t.Fatalf("execute failed: %#v", result)
	}
	if !strings.Contains(result.Result, "Warning: working directory no longer exists") {
		t.Fatalf("missing stale cwd warning: %q", result.Result)
	}
	if !strings.Contains(result.Result, "1\thello") {
		t.Fatalf("read result = %q", result.Result)
	}
}

func TestReadFileUsesOffsetAndLimitWithoutReadingFullFile(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "notes.txt")
	if err := os.WriteFile(target, []byte("one\ntwo\nthree\nfour\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName: "read_file",
		Args: map[string]any{
			"file_path": "notes.txt",
			"offset":    2,
			"limit":     2,
		},
	}, dir, nil)

	if !result.OK {
		t.Fatalf("read failed: %#v", result)
	}
	if !strings.Contains(result.Result, "2\ttwo") || !strings.Contains(result.Result, "3\tthree") {
		t.Fatalf("read result = %q", result.Result)
	}
	if strings.Contains(result.Result, "1\tone") || strings.Contains(result.Result, "4\tfour") {
		t.Fatalf("read leaked outside requested range: %q", result.Result)
	}
}

func TestGlobSupportsGlobstarAndSortsByNewestMtime(t *testing.T) {
	dir := t.TempDir()
	rootFile := filepath.Join(dir, "root.txt")
	nestedDir := filepath.Join(dir, "nested")
	if err := os.Mkdir(nestedDir, 0o755); err != nil {
		t.Fatal(err)
	}
	nestedFile := filepath.Join(nestedDir, "newer.txt")
	if err := os.WriteFile(rootFile, []byte("root"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(nestedFile, []byte("nested"), 0o644); err != nil {
		t.Fatal(err)
	}
	oldTime := time.Now().Add(-2 * time.Hour)
	newTime := time.Now().Add(-1 * time.Hour)
	if err := os.Chtimes(rootFile, oldTime, oldTime); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(nestedFile, newTime, newTime); err != nil {
		t.Fatal(err)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName: "glob",
		Args: map[string]any{
			"pattern": "**/*.txt",
			"path":    ".",
		},
	}, dir, nil)

	if !result.OK {
		t.Fatalf("glob failed: %#v", result)
	}
	lines := strings.Split(result.Result, "\n")
	if len(lines) < 2 {
		t.Fatalf("glob result = %q, want two matches", result.Result)
	}
	if lines[0] != nestedFile || lines[1] != rootFile {
		t.Fatalf("glob order = %#v, want newest first", lines)
	}
}

func TestListFileListsAndSanitizesNames(t *testing.T) {
	dir := t.TempDir()
	if err := os.Mkdir(filepath.Join(dir, "src"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "src", "main.go"), []byte("package main\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "README.md"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "tricky`name`.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".hidden"), []byte("secret"), 0o644); err != nil {
		t.Fatal(err)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName: "list_file",
		Args: map[string]any{
			"path": ".",
		},
	}, dir, nil)

	if !result.OK {
		t.Fatalf("list_file failed: %#v", result)
	}
	if !strings.Contains(result.Result, "src/") {
		t.Fatalf("directory missing from list_file output: %q", result.Result)
	}
	if !strings.Contains(result.Result, "README.md") {
		t.Fatalf("file missing from list_file output: %q", result.Result)
	}
	if !strings.Contains(result.Result, "tricky\\`name\\`.txt") {
		t.Fatalf("markdown-sensitive name was not escaped: %q", result.Result)
	}
	if !strings.Contains(result.Result, ".hidden") {
		t.Fatalf("default list_file output should include dotfiles: %q", result.Result)
	}
}

func TestListFileFiltersDotfilesPatternAndSingleFile(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "main.go"), []byte("package main\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "README.md"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".hidden"), []byte("secret"), 0o644); err != nil {
		t.Fatal(err)
	}

	filtered := Execute(protocol.ExecToolRequest{
		ToolName: "list_file",
		Args: map[string]any{
			"path":    ".",
			"all":     false,
			"long":    false,
			"pattern": "*.go",
		},
	}, dir, nil)

	if !filtered.OK {
		t.Fatalf("list_file failed: %#v", filtered)
	}
	if strings.TrimSpace(filtered.Result) != "main.go" {
		t.Fatalf("filtered list_file output = %q, want only main.go", filtered.Result)
	}

	single := Execute(protocol.ExecToolRequest{
		ToolName: "list_file",
		Args: map[string]any{
			"path": "README.md",
			"long": false,
		},
	}, dir, nil)

	if !single.OK {
		t.Fatalf("single file list_file failed: %#v", single)
	}
	if single.Result != "README.md" {
		t.Fatalf("single file output = %q, want README.md", single.Result)
	}
}

func TestExecuteLSPRejectsUnsupportedFileTypeBeforeStartingServer(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "notes.txt"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName: "lsp",
		Args: map[string]any{
			"operation": "documentSymbol",
			"filePath":  "notes.txt",
		},
	}, dir, nil)

	if result.OK || result.ErrorCode != "REMOTE_LSP_ERROR" {
		t.Fatalf("result = %#v, want REMOTE_LSP_ERROR", result)
	}
	if !strings.Contains(result.ErrorMessage, "unsupported file type") {
		t.Fatalf("error = %q, want unsupported file type", result.ErrorMessage)
	}
}

func TestExecuteArchivesLongOutputUnderWorkspaceRoot(t *testing.T) {
	cwd := t.TempDir()
	workspaceRoot := t.TempDir()
	target := filepath.Join(cwd, "large.txt")
	content := strings.Repeat("alpha", maxOutputChars+100)
	if err := os.WriteFile(target, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}

	result := ExecuteWithContext(context.Background(), protocol.ExecToolRequest{
		ToolName: "read_file",
		Args: map[string]any{
			"file_path": "large.txt",
		},
	}, cwd, workspaceRoot, nil)

	if !result.OK {
		t.Fatalf("execute failed: %#v", result)
	}
	if !strings.Contains(result.Result, "[truncated] Tool output exceeded limits") {
		t.Fatalf("missing truncation summary: %q", result.Result)
	}
	path := archivedPathFromResult(t, result.Result)
	if !strings.HasPrefix(filepath.Clean(path), filepath.Join(workspaceRoot, ".rcoder", "tool-outputs")) {
		t.Fatalf("archive path %q is not under workspace root %q", path, workspaceRoot)
	}
	archived, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(archived) != joinNumbered(strings.Split(strings.TrimSuffix(content, "\n"), "\n"), 0) {
		t.Fatalf("archived content mismatch")
	}
}

func TestExecuteRequiresWorkspaceRootForLongOutputArchiving(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "large.txt")
	if err := os.WriteFile(target, []byte(strings.Repeat("x", maxOutputChars+100)), 0o644); err != nil {
		t.Fatal(err)
	}

	result := ExecuteWithContext(context.Background(), protocol.ExecToolRequest{
		ToolName: "read_file",
		Args: map[string]any{
			"file_path": "large.txt",
		},
	}, dir, "", nil)

	if result.OK || result.ErrorCode != "REMOTE_PROTOCOL_ERROR" {
		t.Fatalf("result = %#v, want missing workspace root protocol error", result)
	}
}

func archivedPathFromResult(t *testing.T, result string) string {
	t.Helper()
	for _, line := range strings.Split(result, "\n") {
		const prefix = "Full output saved to: "
		if strings.HasPrefix(line, prefix) {
			return strings.TrimSpace(strings.TrimPrefix(line, prefix))
		}
	}
	t.Fatalf("archive path missing from result: %q", result)
	return ""
}

func expectedStateFromPreview(preview protocol.ToolPreviewResult) *protocol.ToolMutationPreviewState {
	return &protocol.ToolMutationPreviewState{
		ResolvedPath: preview.ResolvedPath,
		OldSHA256:    preview.OldSHA256,
		OldExists:    preview.OldExists,
		OldSize:      preview.OldSize,
	}
}

func readFileForTest(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

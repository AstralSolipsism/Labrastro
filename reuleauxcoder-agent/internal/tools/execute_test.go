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

func TestPreviewApplyPatchDoesNotWriteAndExecuteDetectsStaleFile(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "notes.txt")
	if err := os.WriteFile(target, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	patch := strings.Join([]string{
		"*** Begin Patch",
		"*** Update File: notes.txt",
		"@@",
		"-old",
		"+new",
		"*** End Patch",
	}, "\n")
	req := protocol.ToolPreviewRequest{
		ToolName: "apply_patch",
		Args:     map[string]any{"patch": patch},
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
		ToolName:      "apply_patch",
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

func TestExecuteApplyPatchIgnoresPreviewMTimeWhenContentMatches(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "notes.txt")
	if err := os.WriteFile(target, []byte("old\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	patch := strings.Join([]string{
		"*** Begin Patch",
		"*** Update File: notes.txt",
		"@@",
		"-old",
		"+new",
		"*** End Patch",
	}, "\n")
	req := protocol.ToolPreviewRequest{
		ToolName: "apply_patch",
		Args:     map[string]any{"patch": patch},
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
		ToolName:      "apply_patch",
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

func TestPreviewAndExecuteApplyPatchShareValidationAndState(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "main.txt")
	if err := os.WriteFile(target, []byte("alpha beta\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	patch := strings.Join([]string{
		"*** Begin Patch",
		"*** Update File: main.txt",
		"@@",
		"-alpha beta",
		"+omega beta",
		"*** End Patch",
	}, "\n")
	preview := Preview(protocol.ToolPreviewRequest{
		ToolName: "apply_patch",
		Args:     map[string]any{"patch": patch},
	}, dir)
	if !preview.OK {
		t.Fatalf("preview failed: %s", preview.ErrorMessage)
	}
	if got := readFileForTest(t, target); got != "alpha beta\n" {
		t.Fatalf("preview edited file, got %q", got)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName:      "apply_patch",
		Args:          map[string]any{"patch": patch},
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

func TestExecuteApplyPatchApprovedMultiFileState(t *testing.T) {
	dir := t.TempDir()
	patch := strings.Join([]string{
		"*** Begin Patch",
		"*** Add File: one.txt",
		"+one",
		"*** Add File: two.txt",
		"+two",
		"*** End Patch",
	}, "\n")
	preview := Preview(protocol.ToolPreviewRequest{
		ToolName: "apply_patch",
		Args:     map[string]any{"patch": patch},
	}, dir)
	if !preview.OK {
		t.Fatalf("preview failed: %s", preview.ErrorMessage)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName:      "apply_patch",
		Args:          map[string]any{"patch": patch},
		ExpectedState: expectedStateFromPreview(preview),
	}, dir, nil)
	if !result.OK {
		t.Fatalf("execute failed: %#v", result)
	}
	if got := readFileForTest(t, filepath.Join(dir, "one.txt")); got != "one\n" {
		t.Fatalf("one.txt = %q", got)
	}
	if got := readFileForTest(t, filepath.Join(dir, "two.txt")); got != "two\n" {
		t.Fatalf("two.txt = %q", got)
	}
}

func TestApplyPatchRejectsInvalidAddFileLine(t *testing.T) {
	dir := t.TempDir()
	patch := strings.Join([]string{
		"*** Begin Patch",
		"*** Add File: bad.txt",
		"missing-plus",
		"*** End Patch",
	}, "\n")

	preview := Preview(protocol.ToolPreviewRequest{
		ToolName: "apply_patch",
		Args:     map[string]any{"patch": patch},
	}, dir)
	if preview.OK || !strings.Contains(preview.ErrorMessage, "Add File lines must start with +") {
		t.Fatalf("preview = %#v, want add-line validation error", preview)
	}
}

func TestApplyPatchRollsBackMultiFileWriteFailure(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "blocker"), []byte("file parent\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	patch := strings.Join([]string{
		"*** Begin Patch",
		"*** Add File: one.txt",
		"+one",
		"*** Add File: blocker/two.txt",
		"+two",
		"*** End Patch",
	}, "\n")

	result := Execute(protocol.ExecToolRequest{
		ToolName: "apply_patch",
		Args:     map[string]any{"patch": patch},
	}, dir, nil)
	if result.OK {
		t.Fatalf("execute unexpectedly succeeded: %#v", result)
	}
	if _, err := os.Stat(filepath.Join(dir, "one.txt")); !os.IsNotExist(err) {
		t.Fatalf("one.txt should have been rolled back, stat err=%v", err)
	}
	if got := readFileForTest(t, filepath.Join(dir, "blocker")); got != "file parent\n" {
		t.Fatalf("blocker changed: %q", got)
	}
}

func TestApplyPatchMatchesContextAcrossLineEndings(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "main.txt")
	if err := os.WriteFile(target, []byte("alpha\r\nbeta\r\ngamma\r\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	patch := strings.Join([]string{
		"*** Begin Patch",
		"*** Update File: main.txt",
		"@@",
		"-alpha",
		"-beta",
		"+one",
		"+two",
		" gamma",
		"*** End Patch",
	}, "\n")
	req := protocol.ToolPreviewRequest{
		ToolName: "apply_patch",
		Args:     map[string]any{"patch": patch},
	}
	preview := Preview(req, dir)
	if !preview.OK {
		t.Fatalf("preview failed: %s", preview.ErrorMessage)
	}
	if got := readFileForTest(t, target); got != "alpha\r\nbeta\r\ngamma\r\n" {
		t.Fatalf("preview edited file, got %q", got)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName:      "apply_patch",
		Args:          req.Args,
		ExpectedState: expectedStateFromPreview(preview),
	}, dir, nil)
	if !result.OK {
		t.Fatalf("execute failed: %#v", result)
	}
	if got := readFileForTest(t, target); got != "one\ntwo\ngamma\n" {
		t.Fatalf("execute content = %q", got)
	}
}

func TestApplyPatchRejectsMissingAndDuplicateContext(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "main.txt")
	if err := os.WriteFile(target, []byte("alpha\r\nbeta\r\nalpha\r\nbeta\r\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	duplicatePatch := strings.Join([]string{
		"*** Begin Patch",
		"*** Update File: main.txt",
		"@@",
		"-alpha",
		"-beta",
		"+one",
		"+two",
		"*** End Patch",
	}, "\n")
	duplicate := Preview(protocol.ToolPreviewRequest{
		ToolName: "apply_patch",
		Args:     map[string]any{"patch": duplicatePatch},
	}, dir)
	if duplicate.OK || !strings.Contains(duplicate.ErrorMessage, "matches multiple locations") {
		t.Fatalf("preview = %#v, want duplicate context error", duplicate)
	}

	missingPatch := strings.Join([]string{
		"*** Begin Patch",
		"*** Update File: main.txt",
		"@@",
		"-missing",
		"-beta",
		"+one",
		"+two",
		"*** End Patch",
	}, "\n")
	missing := Preview(protocol.ToolPreviewRequest{
		ToolName: "apply_patch",
		Args:     map[string]any{"patch": missingPatch},
	}, dir)
	if missing.OK || !strings.Contains(missing.ErrorMessage, "context does not match") {
		t.Fatalf("preview = %#v, want missing context error", missing)
	}
}

func TestPreviewApplyPatchRejectsDuplicateContext(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "main.txt")
	if err := os.WriteFile(target, []byte("same\nsame\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	patch := strings.Join([]string{
		"*** Begin Patch",
		"*** Update File: main.txt",
		"@@",
		"-same",
		"+other",
		"*** End Patch",
	}, "\n")
	preview := Preview(protocol.ToolPreviewRequest{
		ToolName: "apply_patch",
		Args:     map[string]any{"patch": patch},
	}, dir)
	if preview.OK || !strings.Contains(preview.ErrorMessage, "matches multiple locations") {
		t.Fatalf("preview = %#v, want duplicate context error", preview)
	}
	if got := readFileForTest(t, target); got != "same\nsame\n" {
		t.Fatalf("failed preview changed file, got %q", got)
	}
}

func TestPreviewDraftDocumentCommitRejectsExistingTarget(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "docs", "architecture.md")
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, []byte("existing\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	preview := Preview(protocol.ToolPreviewRequest{
		ToolName: "draft_document_commit",
		Args: map[string]any{
			"target_path": "docs/architecture.md",
			"content":     "# Replacement\n",
		},
	}, dir)

	if preview.OK {
		t.Fatalf("preview unexpectedly succeeded: %#v", preview)
	}
	if !strings.Contains(preview.ErrorMessage, "already exists") || !strings.Contains(preview.ErrorMessage, "apply_patch") {
		t.Fatalf("error = %q, want existing-target apply_patch guidance", preview.ErrorMessage)
	}
	if got := readFileForTest(t, target); got != "existing\n" {
		t.Fatalf("failed preview changed file, got %q", got)
	}
}

func TestExecuteDraftDocumentCommitRejectsExistingTarget(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "docs", "architecture.md")
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, []byte("existing\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName: "draft_document_commit",
		Args: map[string]any{
			"target_path": "docs/architecture.md",
			"content":     "# Replacement\n",
		},
	}, dir, nil)

	if result.OK || result.ErrorCode != "REMOTE_TOOL_ERROR" {
		t.Fatalf("result = %#v, want REMOTE_TOOL_ERROR", result)
	}
	if !strings.Contains(result.ErrorMessage, "already exists") || !strings.Contains(result.ErrorMessage, "apply_patch") {
		t.Fatalf("error = %q, want existing-target apply_patch guidance", result.ErrorMessage)
	}
	if got := readFileForTest(t, target); got != "existing\n" {
		t.Fatalf("failed execute changed file, got %q", got)
	}
}

func TestPreviewAndExecuteDraftDocumentCommitCreatesNewFile(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "docs", "architecture.md")
	args := map[string]any{
		"target_path": "docs/architecture.md",
		"content":     "# Architecture\n",
	}

	preview := Preview(protocol.ToolPreviewRequest{
		ToolName: "draft_document_commit",
		Args:     args,
	}, dir)
	if !preview.OK {
		t.Fatalf("preview failed: %s", preview.ErrorMessage)
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Fatalf("preview should not create target, stat err=%v", err)
	}

	result := Execute(protocol.ExecToolRequest{
		ToolName:      "draft_document_commit",
		Args:          args,
		ExpectedState: expectedStateFromPreview(preview),
	}, dir, nil)

	if !result.OK {
		t.Fatalf("execute failed: %#v", result)
	}
	if !strings.Contains(result.Result, "Committed document docs/architecture.md") {
		t.Fatalf("result = %q, want committed document message", result.Result)
	}
	if got := readFileForTest(t, target); got != "# Architecture\n" {
		t.Fatalf("created content = %q", got)
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
	state := &protocol.ToolMutationPreviewState{
		ResolvedPath: preview.ResolvedPath,
		OldSHA256:    preview.OldSHA256,
		OldExists:    preview.OldExists,
		OldSize:      preview.OldSize,
	}
	if planID, ok := preview.Meta["plan_id"].(string); ok {
		state.PlanID = planID
	}
	if planHash, ok := preview.Meta["plan_hash"].(string); ok {
		state.PlanHash = planHash
	}
	state.Operations = operationStatesFromPreviewMeta(preview.Meta["operations"])
	return state
}

func operationStatesFromPreviewMeta(value any) []protocol.ToolMutationOperationState {
	var items []map[string]any
	switch typed := value.(type) {
	case []map[string]any:
		items = typed
	case []any:
		for _, item := range typed {
			if mapped, ok := item.(map[string]any); ok {
				items = append(items, mapped)
			}
		}
	}
	states := make([]protocol.ToolMutationOperationState, 0, len(items))
	for _, item := range items {
		oldExists := boolFromAny(item["old_exists"])
		oldSize := int64FromAny(item["old_size"])
		states = append(states, protocol.ToolMutationOperationState{
			Kind:             stringFromAny(item["kind"]),
			Path:             stringFromAny(item["path"]),
			MovePath:         stringFromAny(item["move_path"]),
			ResolvedPath:     stringFromAny(item["resolved_path"]),
			MoveResolvedPath: stringFromAny(item["move_resolved_path"]),
			OldSHA256:        stringFromAny(item["old_sha256"]),
			OldExists:        oldExists,
			OldSize:          oldSize,
		})
	}
	return states
}

func stringFromAny(value any) string {
	if text, ok := value.(string); ok {
		return text
	}
	return ""
}

func boolFromAny(value any) *bool {
	switch typed := value.(type) {
	case bool:
		return &typed
	case *bool:
		return typed
	default:
		return nil
	}
}

func int64FromAny(value any) *int64 {
	switch typed := value.(type) {
	case int64:
		return &typed
	case int:
		converted := int64(typed)
		return &converted
	case float64:
		converted := int64(typed)
		return &converted
	case *int64:
		return typed
	default:
		return nil
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

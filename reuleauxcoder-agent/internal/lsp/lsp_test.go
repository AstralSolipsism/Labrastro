package lsp

import (
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestResolveWorkspaceRootUsesLanguageMarkers(t *testing.T) {
	dir := t.TempDir()
	project := filepath.Join(dir, "project")
	src := filepath.Join(project, "pkg")
	if err := mkdirAll(src); err != nil {
		t.Fatal(err)
	}
	if err := writeFile(filepath.Join(project, "go.mod"), "module example\n"); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(src, "main.go")
	if err := writeFile(target, "package main\n"); err != nil {
		t.Fatal(err)
	}

	root := resolveWorkspaceRoot(target, goLang, dir)

	if root != project {
		t.Fatalf("root = %q, want %q", root, project)
	}
}

func TestURIFormattingRoundTripsWorkspaceRelativePath(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "src", "main.go")
	if err := mkdirAll(filepath.Dir(target)); err != nil {
		t.Fatal(err)
	}
	if err := writeFile(target, "package main\n"); err != nil {
		t.Fatal(err)
	}

	uri := pathToURI(target)
	display := uriDisplayPath(uri, dir)

	if display != "src/main.go" {
		t.Fatalf("display = %q, want src/main.go", display)
	}
	if runtime.GOOS == "windows" && !strings.HasPrefix(uri, "file:///") {
		t.Fatalf("windows file uri = %q, want file:///", uri)
	}
}

func TestFormatDocumentSymbolsFlattensChildren(t *testing.T) {
	result := []any{
		map[string]any{
			"name": "Outer",
			"selectionRange": map[string]any{
				"start": map[string]any{"line": float64(2), "character": float64(4)},
			},
			"children": []any{
				map[string]any{
					"name": "Inner",
					"selectionRange": map[string]any{
						"start": map[string]any{"line": float64(3), "character": float64(1)},
					},
				},
			},
		},
	}

	formatted := formatDocumentSymbols(result)

	if !strings.Contains(formatted, "Outer:3:5") {
		t.Fatalf("missing outer symbol: %q", formatted)
	}
	if !strings.Contains(formatted, "Outer.Inner:4:2") {
		t.Fatalf("missing child symbol: %q", formatted)
	}
}

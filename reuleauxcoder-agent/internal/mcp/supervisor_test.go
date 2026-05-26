package mcp

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/client"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

func TestExpandLaunchTemplates(t *testing.T) {
	launch := expandLaunch(protocol.MCPLaunchManifest{
		Command: "{{bundle}}/server",
		Args:    []string{"--root", "{{workspace}}"},
		Env:     map[string]string{"CACHE": "{{cache}}"},
		CWD:     "{{bundle}}",
	}, templateVars{
		workspace: "/workspace",
		cache:     "/workspace/.rcoder/mcp-cache/s/1/linux-amd64",
		bundle:    "/workspace/.rcoder/mcp-cache/s/1/linux-amd64/bundle",
	})

	if launch.Command != filepath.Clean("/workspace/.rcoder/mcp-cache/s/1/linux-amd64/bundle/server") {
		t.Fatalf("command = %q", launch.Command)
	}
	if launch.Args[1] != "/workspace" {
		t.Fatalf("workspace arg = %q", launch.Args[1])
	}
	if launch.Env["CACHE"] != "/workspace/.rcoder/mcp-cache/s/1/linux-amd64" {
		t.Fatalf("CACHE env = %q", launch.Env["CACHE"])
	}
}

func TestExtractTarGz(t *testing.T) {
	tmp := t.TempDir()
	archivePath := filepath.Join(tmp, "artifact.tar.gz")
	dest := filepath.Join(tmp, "bundle")
	var buf bytes.Buffer
	gz := gzip.NewWriter(&buf)
	tw := tar.NewWriter(gz)
	content := []byte("hello")
	if err := tw.WriteHeader(&tar.Header{Name: "bin/server", Mode: 0o755, Size: int64(len(content))}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(content); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(archivePath, buf.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := extractArchive(archivePath, dest); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(filepath.Join(dest, "bin", "server"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "hello" {
		t.Fatalf("extracted content = %q", got)
	}
}

func TestSupervisorDownloadsStartsReportsAndExecutesMCP(t *testing.T) {
	if os.Getenv("RCODER_FAKE_MCP") == "1" {
		runFakeMCPServer()
		return
	}

	artifact := buildFakeMCPArtifact(t)
	sum := sha256.Sum256(artifact)
	sha := hex.EncodeToString(sum[:])
	var reported protocol.MCPToolsReport

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/remote/mcp/manifest":
			var req protocol.MCPManifestRequest
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				t.Errorf("decode manifest request: %v", err)
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			if req.PeerToken != "pt_test" {
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			resp := protocol.MCPManifestResponse{Servers: []protocol.MCPServerManifest{
				{
					Name:    "fake",
					Version: "1.0.0",
					Artifact: &protocol.MCPArtifactManifest{
						Platform: runtime.GOOS + "-" + runtime.GOARCH,
						Path:     "fake/1.0.0/fake.zip",
						SHA256:   sha,
						URL:      "/remote/mcp/artifacts/fake/1.0.0/fake.zip",
					},
					Launch: protocol.MCPLaunchManifest{
						Command: "{{bundle}}/" + filepath.Base(os.Args[0]),
						Args:    []string{"-test.run=TestSupervisorDownloadsStartsReportsAndExecutesMCP"},
						Env:     map[string]string{"RCODER_FAKE_MCP": "1"},
					},
				},
			}}
			_ = json.NewEncoder(w).Encode(resp)
		case "/remote/mcp/artifacts/fake/1.0.0/fake.zip":
			if r.Header.Get("X-RC-Peer-Token") != "pt_test" {
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			_, _ = w.Write(artifact)
		case "/remote/mcp/tools":
			if err := json.NewDecoder(r.Body).Decode(&reported); err != nil {
				t.Errorf("decode tools report: %v", err)
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(w).Encode(protocol.MCPToolsReportResponse{OK: true, PeerID: "peer"})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	supervisor := NewSupervisor(client.New(server.URL), "pt_test", t.TempDir())
	supervisor.Start(ctx)
	defer supervisor.Stop()

	if len(reported.Tools) != 1 {
		t.Fatalf("reported tools = %#v", reported.Tools)
	}
	if reported.Tools[0].Name != "echo" || reported.Tools[0].ServerName != "fake" {
		t.Fatalf("unexpected reported tool = %#v", reported.Tools[0])
	}
	result := supervisor.Execute(map[string]any{
		"server_name": "fake",
		"tool_name":   "echo",
		"arguments":   map[string]any{"text": "hello"},
	})
	if !result.OK || result.Result != "echo:hello" {
		t.Fatalf("execute result = %#v", result)
	}
}

func TestSupervisorCommandDistributionStartsLocalMCP(t *testing.T) {
	if os.Getenv("RCODER_FAKE_MCP") == "1" {
		runFakeMCPServer()
		return
	}

	var artifactHits int
	var reported protocol.MCPToolsReport
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/remote/mcp/manifest":
			resp := protocol.MCPManifestResponse{Servers: []protocol.MCPServerManifest{
				{
					Name:         "fake-command",
					Version:      "1.0.0",
					Distribution: "command",
					Launch: protocol.MCPLaunchManifest{
						Command: os.Args[0],
						Args:    []string{"-test.run=TestSupervisorCommandDistributionStartsLocalMCP"},
						Env:     map[string]string{"RCODER_FAKE_MCP": "1"},
					},
				},
			}}
			_ = json.NewEncoder(w).Encode(resp)
		case "/remote/mcp/artifacts/fake-command/1.0.0/fake.zip":
			artifactHits++
			w.WriteHeader(http.StatusInternalServerError)
		case "/remote/mcp/tools":
			if err := json.NewDecoder(r.Body).Decode(&reported); err != nil {
				t.Errorf("decode tools report: %v", err)
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(w).Encode(protocol.MCPToolsReportResponse{OK: true, PeerID: "peer"})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	supervisor := NewSupervisor(client.New(server.URL), "pt_test", t.TempDir())
	supervisor.Start(ctx)
	defer supervisor.Stop()

	if artifactHits != 0 {
		t.Fatalf("artifact downloads = %d", artifactHits)
	}
	if len(reported.Diagnostics) != 0 {
		t.Fatalf("reported diagnostics = %#v", reported.Diagnostics)
	}
	if len(reported.Tools) != 1 {
		t.Fatalf("reported tools = %#v", reported.Tools)
	}
	if reported.Tools[0].Name != "echo" || reported.Tools[0].ServerName != "fake-command" {
		t.Fatalf("unexpected reported tool = %#v", reported.Tools[0])
	}
}

func TestSupervisorCommandDistributionReportsStderrOnStartupFailure(t *testing.T) {
	if os.Getenv("RCODER_FAIL_MCP") == "1" {
		_, _ = os.Stderr.WriteString("gitnexus: libstdc++.so.6: version `GLIBCXX_3.4.32' not found\n")
		time.Sleep(250 * time.Millisecond)
		os.Exit(1)
	}

	var reported protocol.MCPToolsReport
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/remote/mcp/manifest":
			resp := protocol.MCPManifestResponse{Servers: []protocol.MCPServerManifest{
				{
					Name:         "broken-command",
					Version:      "1.0.0",
					Distribution: "command",
					Launch: protocol.MCPLaunchManifest{
						Command: os.Args[0],
						Args:    []string{"-test.run=TestSupervisorCommandDistributionReportsStderrOnStartupFailure"},
						Env:     map[string]string{"RCODER_FAIL_MCP": "1"},
					},
				},
			}}
			_ = json.NewEncoder(w).Encode(resp)
		case "/remote/mcp/tools":
			if err := json.NewDecoder(r.Body).Decode(&reported); err != nil {
				t.Errorf("decode tools report: %v", err)
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(w).Encode(protocol.MCPToolsReportResponse{OK: true, PeerID: "peer"})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	supervisor := NewSupervisor(client.New(server.URL), "pt_test", t.TempDir())
	supervisor.Start(ctx)
	defer supervisor.Stop()

	if len(reported.Tools) != 0 {
		t.Fatalf("reported tools = %#v", reported.Tools)
	}
	if len(reported.Diagnostics) != 1 {
		t.Fatalf("reported diagnostics = %#v", reported.Diagnostics)
	}
	message, _ := reported.Diagnostics[0]["message"].(string)
	if !strings.Contains(message, "stderr:") || !strings.Contains(message, "GLIBCXX_3.4.32") {
		t.Fatalf("diagnostic message = %q", message)
	}
}

func buildFakeMCPArtifact(t *testing.T) []byte {
	t.Helper()
	exe, err := os.Open(os.Args[0])
	if err != nil {
		t.Fatal(err)
	}
	defer exe.Close()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	header := &zip.FileHeader{Name: filepath.Base(os.Args[0])}
	header.SetMode(0o755)
	writer, err := zw.CreateHeader(header)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(writer, exe); err != nil {
		t.Fatal(err)
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func runFakeMCPServer() {
	decoder := json.NewDecoder(os.Stdin)
	encoder := json.NewEncoder(os.Stdout)
	for {
		var req map[string]any
		if err := decoder.Decode(&req); err != nil {
			return
		}
		id, hasID := req["id"]
		method, _ := req["method"].(string)
		if !hasID {
			continue
		}
		switch method {
		case "initialize":
			_ = encoder.Encode(map[string]any{
				"jsonrpc": "2.0",
				"id":      id,
				"result":  map[string]any{"capabilities": map[string]any{"tools": map[string]any{}}},
			})
		case "tools/list":
			_ = encoder.Encode(map[string]any{
				"jsonrpc": "2.0",
				"id":      id,
				"result": map[string]any{
					"tools": []map[string]any{
						{
							"name":        "echo",
							"description": "Echo text",
							"inputSchema": map[string]any{"type": "object"},
						},
					},
				},
			})
		case "tools/call":
			params, _ := req["params"].(map[string]any)
			arguments, _ := params["arguments"].(map[string]any)
			text, _ := arguments["text"].(string)
			_ = encoder.Encode(map[string]any{
				"jsonrpc": "2.0",
				"id":      id,
				"result": map[string]any{
					"content": []map[string]any{{"type": "text", "text": "echo:" + text}},
				},
			})
		}
	}
}

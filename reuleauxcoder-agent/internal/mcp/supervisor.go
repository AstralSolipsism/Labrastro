package mcp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/client"
	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

type Supervisor struct {
	http      *client.HTTPClient
	peerToken string
	workspace string

	ctx    context.Context
	cancel context.CancelFunc

	mu          sync.RWMutex
	servers     map[string]*stdioClient
	tools       []protocol.MCPToolInfo
	diagnostics []map[string]any
}

func NewSupervisor(httpClient *client.HTTPClient, peerToken, workspace string) *Supervisor {
	return &Supervisor{
		http:      httpClient,
		peerToken: peerToken,
		workspace: workspace,
		servers:   map[string]*stdioClient{},
	}
}

func (s *Supervisor) Start(parent context.Context) {
	s.ctx, s.cancel = context.WithCancel(parent)
	setupCtx, cancel := context.WithTimeout(s.ctx, 3*time.Minute)
	defer cancel()

	manifest, err := s.http.MCPManifest(setupCtx, protocol.MCPManifestRequest{
		PeerToken: s.peerToken,
		OS:        runtime.GOOS,
		Arch:      runtime.GOARCH,
		Workspace: s.workspace,
	})
	if err != nil {
		s.addDiagnostic("", "error", fmt.Sprintf("MCP manifest unavailable: %v", err))
		_ = s.report(setupCtx)
		return
	}
	for _, diagnostic := range manifest.Diagnostics {
		s.addRawDiagnostic(diagnostic)
	}
	for _, server := range manifest.Servers {
		if err := s.startServer(setupCtx, server); err != nil {
			s.addDiagnostic(server.Name, "error", err.Error())
		}
	}
	if err := s.report(setupCtx); err != nil {
		log.Printf("MCP tool report failed: %v", err)
	}
}

func (s *Supervisor) Stop() {
	if s.cancel != nil {
		s.cancel()
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, server := range s.servers {
		server.stop()
	}
	s.servers = map[string]*stdioClient{}
}

func (s *Supervisor) Execute(args map[string]any) protocol.ExecToolResult {
	serverName, _ := args["server_name"].(string)
	toolName, _ := args["tool_name"].(string)
	rawArguments, _ := args["arguments"].(map[string]any)
	if strings.TrimSpace(serverName) == "" || strings.TrimSpace(toolName) == "" {
		return protocol.ExecToolResult{
			OK:           false,
			ErrorCode:    "REMOTE_MCP_ERROR",
			ErrorMessage: "mcp tool call requires server_name and tool_name",
		}
	}
	if rawArguments == nil {
		rawArguments = map[string]any{}
	}
	s.mu.RLock()
	server := s.servers[serverName]
	s.mu.RUnlock()
	if server == nil {
		return protocol.ExecToolResult{
			OK:           false,
			ErrorCode:    "REMOTE_MCP_ERROR",
			ErrorMessage: fmt.Sprintf("MCP server %q is not running on peer", serverName),
		}
	}
	callCtx := s.ctx
	if callCtx == nil {
		callCtx = context.Background()
	}
	return server.callTool(callCtx, toolName, rawArguments)
}

func (s *Supervisor) startServer(ctx context.Context, server protocol.MCPServerManifest) error {
	if strings.TrimSpace(server.Name) == "" {
		return fmt.Errorf("peer MCP manifest contains an empty server name")
	}
	distribution := strings.TrimSpace(server.Distribution)
	if distribution == "" {
		if server.Artifact != nil && strings.TrimSpace(server.Artifact.URL) != "" {
			distribution = "artifact"
		} else {
			distribution = "command"
		}
	}
	if distribution != "command" && distribution != "artifact" {
		return fmt.Errorf("MCP server %s has unsupported distribution %q", server.Name, distribution)
	}

	platform := runtime.GOOS + "-" + runtime.GOARCH
	if server.Artifact != nil && strings.TrimSpace(server.Artifact.Platform) != "" {
		platform = server.Artifact.Platform
	}
	version := strings.TrimSpace(server.Version)
	if version == "" {
		version = "command"
	}
	cacheDir := filepath.Join(s.workspace, ".rcoder", "mcp-cache", server.Name, version, platform)
	bundleDir := filepath.Join(cacheDir, "bundle")
	if distribution == "artifact" {
		if err := s.ensureArtifact(ctx, server, cacheDir, bundleDir); err != nil {
			return fmt.Errorf("prepare MCP server %s: %w", server.Name, err)
		}
	}
	launch := expandLaunch(server.Launch, templateVars{
		workspace: s.workspace,
		cache:     cacheDir,
		bundle:    bundleDir,
	})
	if strings.TrimSpace(launch.Command) == "" {
		return fmt.Errorf("MCP server %s launch command is empty", server.Name)
	}
	if launch.CWD == "" {
		if distribution == "artifact" {
			launch.CWD = bundleDir
		} else {
			launch.CWD = s.workspace
		}
	}
	client, err := startStdioClient(s.ctx, server.Name, launch)
	if err != nil {
		return fmt.Errorf("start MCP server %s: %w", server.Name, err)
	}
	initCtx, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	tools, err := client.initialize(initCtx)
	if err != nil {
		stderrSummary := client.stderrSummary()
		client.stop()
		if stderrSummary != "" {
			return fmt.Errorf("initialize MCP server %s: %w; stderr: %s", server.Name, err, stderrSummary)
		}
		return fmt.Errorf("initialize MCP server %s: %w", server.Name, err)
	}
	s.mu.Lock()
	s.servers[server.Name] = client
	s.tools = append(s.tools, tools...)
	s.mu.Unlock()
	return nil
}

func (s *Supervisor) ensureArtifact(ctx context.Context, server protocol.MCPServerManifest, cacheDir, bundleDir string) error {
	if server.Artifact == nil || server.Artifact.URL == "" || server.Artifact.SHA256 == "" {
		return fmt.Errorf("artifact URL and sha256 are required")
	}
	markerPath := filepath.Join(cacheDir, ".sha256")
	if marker, err := os.ReadFile(markerPath); err == nil &&
		strings.TrimSpace(string(marker)) == strings.ToLower(server.Artifact.SHA256) &&
		dirExists(bundleDir) {
		return nil
	}
	if err := os.MkdirAll(cacheDir, 0o755); err != nil {
		return err
	}
	content, err := s.http.DownloadMCPArtifact(ctx, s.peerToken, server.Artifact.URL)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(content)
	actual := hex.EncodeToString(sum[:])
	if !strings.EqualFold(actual, server.Artifact.SHA256) {
		return fmt.Errorf("artifact sha256 mismatch: expected %s got %s", server.Artifact.SHA256, actual)
	}
	artifactName := filepath.Base(server.Artifact.Path)
	if artifactName == "." || artifactName == string(filepath.Separator) || artifactName == "" {
		artifactName = "artifact"
	}
	artifactPath := filepath.Join(cacheDir, artifactName)
	if err := os.WriteFile(artifactPath, content, 0o644); err != nil {
		return err
	}
	if err := os.RemoveAll(bundleDir); err != nil {
		return err
	}
	if err := os.MkdirAll(bundleDir, 0o755); err != nil {
		return err
	}
	if err := extractArchive(artifactPath, bundleDir); err != nil {
		return err
	}
	return os.WriteFile(markerPath, []byte(strings.ToLower(server.Artifact.SHA256)), 0o644)
}

func (s *Supervisor) report(ctx context.Context) error {
	s.mu.RLock()
	tools := append([]protocol.MCPToolInfo(nil), s.tools...)
	diagnostics := append([]map[string]any(nil), s.diagnostics...)
	s.mu.RUnlock()
	_, err := s.http.ReportMCPTools(ctx, protocol.MCPToolsReport{
		PeerToken:   s.peerToken,
		Tools:       tools,
		Diagnostics: diagnostics,
	})
	return err
}

func (s *Supervisor) addDiagnostic(server, level, message string) {
	s.addRawDiagnostic(map[string]any{
		"server":  server,
		"level":   level,
		"message": message,
	})
}

func (s *Supervisor) addRawDiagnostic(diagnostic map[string]any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.diagnostics = append(s.diagnostics, diagnostic)
}

type templateVars struct {
	workspace string
	cache     string
	bundle    string
}

func expandLaunch(launch protocol.MCPLaunchManifest, vars templateVars) protocol.MCPLaunchManifest {
	home, _ := os.UserHomeDir()
	replacer := strings.NewReplacer(
		"{{workspace}}", vars.workspace,
		"{{cache}}", vars.cache,
		"{{bundle}}", vars.bundle,
		"{{home}}", home,
	)
	out := protocol.MCPLaunchManifest{
		Command: replacer.Replace(launch.Command),
		Args:    make([]string, 0, len(launch.Args)),
		Env:     map[string]string{},
		CWD:     replacer.Replace(launch.CWD),
	}
	if out.Command != "" {
		out.Command = filepath.Clean(out.Command)
	}
	for _, arg := range launch.Args {
		out.Args = append(out.Args, replacer.Replace(arg))
	}
	for key, value := range launch.Env {
		out.Env[key] = replacer.Replace(value)
	}
	if out.CWD != "" {
		out.CWD = filepath.Clean(out.CWD)
	}
	return out
}

func dirExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}

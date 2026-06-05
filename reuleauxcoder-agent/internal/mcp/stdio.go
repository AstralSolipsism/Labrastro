package mcp

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

type stdioClient struct {
	name    string
	cmd     *exec.Cmd
	stdin   io.WriteCloser
	pending map[int]chan rpcResponse
	nextID  int
	mu      sync.Mutex

	stderrMu    sync.Mutex
	stderrLines []string
}

type rpcResponse struct {
	Result map[string]any
	Err    error
}

type rpcEnvelope struct {
	JSONRPC string          `json:"jsonrpc,omitempty"`
	ID      any             `json:"id,omitempty"`
	Method  string          `json:"method,omitempty"`
	Params  map[string]any  `json:"params,omitempty"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   any             `json:"error,omitempty"`
}

const mcpProtocolVersion = "2025-11-25"

var mcpSupportedProtocolVersions = map[string]struct{}{
	"2025-11-25": {},
	"2025-06-18": {},
	"2025-03-26": {},
	"2024-11-05": {},
	"2024-10-07": {},
}

func startStdioClient(ctx context.Context, name string, launch protocol.MCPLaunchManifest) (*stdioClient, error) {
	cmd := exec.CommandContext(ctx, launch.Command, launch.Args...)
	if launch.CWD != "" {
		cmd.Dir = launch.CWD
	}
	cmd.Env = os.Environ()
	for key, value := range launch.Env {
		cmd.Env = append(cmd.Env, key+"="+value)
	}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}

	client := &stdioClient{
		name:    name,
		cmd:     cmd,
		stdin:   stdin,
		pending: map[int]chan rpcResponse{},
	}
	go client.receiveLoop(stdout)
	go logMCPStderr(client, stderr)
	return client, nil
}

func (c *stdioClient) initialize(ctx context.Context) ([]protocol.MCPToolInfo, error) {
	result, err := c.request(ctx, "initialize", map[string]any{
		"protocolVersion": mcpProtocolVersion,
		"capabilities":    map[string]any{},
		"clientInfo":      map[string]any{"name": "reuleauxcoder-peer", "version": "0.1.0"},
	})
	if err != nil {
		return nil, err
	}
	protocolVersion, _ := result["protocolVersion"].(string)
	protocolVersion = strings.TrimSpace(protocolVersion)
	if _, ok := mcpSupportedProtocolVersions[protocolVersion]; !ok {
		if protocolVersion == "" {
			protocolVersion = "<missing>"
		}
		return nil, fmt.Errorf("unsupported MCP protocol version: %s", protocolVersion)
	}
	if err := c.notify("notifications/initialized", map[string]any{}); err != nil {
		return nil, err
	}
	if !serverSupportsTools(result["capabilities"]) {
		return []protocol.MCPToolInfo{}, nil
	}
	result, err = c.request(ctx, "tools/list", map[string]any{})
	if err != nil {
		return nil, err
	}
	rawTools, _ := result["tools"].([]any)
	tools := make([]protocol.MCPToolInfo, 0, len(rawTools))
	for _, item := range rawTools {
		toolMap, ok := item.(map[string]any)
		if !ok {
			continue
		}
		name, _ := toolMap["name"].(string)
		if strings.TrimSpace(name) == "" {
			continue
		}
		description, _ := toolMap["description"].(string)
		inputSchema, _ := toolMap["inputSchema"].(map[string]any)
		if inputSchema == nil {
			inputSchema = map[string]any{"type": "object", "properties": map[string]any{}}
		}
		tools = append(tools, protocol.MCPToolInfo{
			Name:        name,
			Description: description,
			InputSchema: inputSchema,
			ServerName:  c.name,
		})
	}
	return tools, nil
}

func serverSupportsTools(capabilities any) bool {
	values, ok := capabilities.(map[string]any)
	if !ok {
		return false
	}
	_, ok = values["tools"].(map[string]any)
	return ok
}

func (c *stdioClient) callTool(ctx context.Context, name string, arguments map[string]any) protocol.ExecToolResult {
	result, err := c.request(ctx, "tools/call", map[string]any{
		"name":      name,
		"arguments": arguments,
	})
	if err != nil {
		return protocol.ExecToolResult{OK: false, ErrorCode: "REMOTE_MCP_ERROR", ErrorMessage: err.Error()}
	}
	text := renderMCPContent(result)
	if isError, _ := result["isError"].(bool); isError {
		return protocol.ExecToolResult{OK: false, ErrorCode: "REMOTE_MCP_ERROR", ErrorMessage: text}
	}
	if strings.TrimSpace(text) == "" {
		text = "(no output)"
	}
	return protocol.ExecToolResult{OK: true, Result: text}
}

func (c *stdioClient) stop() {
	_ = c.stdin.Close()
	if c.cmd != nil && c.cmd.Process != nil {
		_ = c.cmd.Process.Kill()
	}
	if c.cmd != nil {
		done := make(chan struct{})
		go func() {
			_ = c.cmd.Wait()
			close(done)
		}()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
		}
	}
}

func (c *stdioClient) addStderrLine(line string) {
	c.stderrMu.Lock()
	defer c.stderrMu.Unlock()
	c.stderrLines = append(c.stderrLines, line)
	if len(c.stderrLines) > 8 {
		c.stderrLines = c.stderrLines[len(c.stderrLines)-8:]
	}
}

func (c *stdioClient) stderrSummary() string {
	c.stderrMu.Lock()
	defer c.stderrMu.Unlock()
	if len(c.stderrLines) == 0 {
		return ""
	}
	return strings.Join(c.stderrLines, " | ")
}

func (c *stdioClient) request(ctx context.Context, method string, params map[string]any) (map[string]any, error) {
	c.mu.Lock()
	c.nextID++
	id := c.nextID
	ch := make(chan rpcResponse, 1)
	c.pending[id] = ch
	message := rpcEnvelope{JSONRPC: "2.0", ID: id, Method: method, Params: params}
	line, err := json.Marshal(message)
	if err == nil {
		_, err = c.stdin.Write(append(line, '\n'))
	}
	if err != nil {
		delete(c.pending, id)
		c.mu.Unlock()
		return nil, err
	}
	c.mu.Unlock()

	select {
	case resp := <-ch:
		return resp.Result, resp.Err
	case <-ctx.Done():
		c.mu.Lock()
		delete(c.pending, id)
		c.mu.Unlock()
		return nil, ctx.Err()
	case <-time.After(30 * time.Second):
		c.mu.Lock()
		delete(c.pending, id)
		c.mu.Unlock()
		return nil, fmt.Errorf("MCP request timed out: %s", method)
	}
}

func (c *stdioClient) notify(method string, params map[string]any) error {
	message := rpcEnvelope{JSONRPC: "2.0", Method: method, Params: params}
	line, err := json.Marshal(message)
	if err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	_, err = c.stdin.Write(append(line, '\n'))
	return err
}

func (c *stdioClient) receiveLoop(stdout io.Reader) {
	scanner := bufio.NewScanner(stdout)
	scanner.Buffer(make([]byte, 4096), 1024*1024)
	for scanner.Scan() {
		var msg rpcEnvelope
		if err := json.Unmarshal(scanner.Bytes(), &msg); err != nil {
			continue
		}
		id, ok := rpcID(msg.ID)
		if !ok {
			continue
		}
		var result map[string]any
		if len(msg.Result) > 0 {
			_ = json.Unmarshal(msg.Result, &result)
		}
		if result == nil {
			result = map[string]any{}
		}
		var err error
		if msg.Error != nil {
			err = fmt.Errorf("MCP error: %v", msg.Error)
		}
		c.mu.Lock()
		ch := c.pending[id]
		delete(c.pending, id)
		c.mu.Unlock()
		if ch != nil {
			ch <- rpcResponse{Result: result, Err: err}
		}
	}
	if err := scanner.Err(); err != nil {
		c.failPending(fmt.Errorf("MCP stdout closed: %w", err))
	} else {
		c.failPending(fmt.Errorf("MCP stdout closed"))
	}
}

func (c *stdioClient) failPending(err error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for id, ch := range c.pending {
		delete(c.pending, id)
		ch <- rpcResponse{Err: err}
	}
}

func rpcID(value any) (int, bool) {
	switch v := value.(type) {
	case float64:
		return int(v), true
	case int:
		return v, true
	default:
		return 0, false
	}
}

func logMCPStderr(client *stdioClient, stderr io.Reader) {
	scanner := bufio.NewScanner(stderr)
	for scanner.Scan() {
		line := scanner.Text()
		client.addStderrLine(line)
		log.Printf("[mcp:%s] %s", client.name, line)
	}
}

func renderMCPContent(result map[string]any) string {
	rawContent, _ := result["content"].([]any)
	if len(rawContent) == 0 {
		return "(no output)"
	}
	parts := make([]string, 0, len(rawContent))
	for _, item := range rawContent {
		content, ok := item.(map[string]any)
		if !ok {
			continue
		}
		switch content["type"] {
		case "text":
			if text, _ := content["text"].(string); text != "" {
				parts = append(parts, text)
			}
		case "resource":
			if resource, _ := content["resource"].(map[string]any); resource != nil {
				uri, _ := resource["uri"].(string)
				if uri == "" {
					uri = "unknown"
				}
				parts = append(parts, "[Resource: "+uri+"]")
			}
		case "image":
			mimeType, _ := content["mimeType"].(string)
			data, _ := content["data"].(string)
			parts = append(parts, fmt.Sprintf("[Image: %s, %d chars base64]", mimeType, len(data)))
		case "audio":
			mimeType, _ := content["mimeType"].(string)
			data, _ := content["data"].(string)
			parts = append(parts, fmt.Sprintf("[Audio: %s, %d chars base64]", mimeType, len(data)))
		}
	}
	if len(parts) == 0 {
		return "(no output)"
	}
	return strings.Join(parts, "\n")
}

package client

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/RC-CHN/ReuleauxCoder/reuleauxcoder-agent/internal/protocol"
)

type HTTPClient struct {
	baseURL string
	http    *http.Client
}

func New(baseURL string) *HTTPClient {
	return &HTTPClient{
		baseURL: strings.TrimRight(baseURL, "/"),
		http: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

func (c *HTTPClient) Register(ctx context.Context, req protocol.RegisterRequest) (protocol.RegisterResponse, error) {
	var env protocol.RegisterResponseEnvelope
	if err := c.postJSON(ctx, "/remote/register", req, &env); err != nil {
		return protocol.RegisterResponse{}, err
	}
	return env.Payload, nil
}

func (c *HTTPClient) Heartbeat(ctx context.Context, req protocol.Heartbeat) error {
	return c.postJSON(ctx, "/remote/heartbeat", req, nil)
}

func (c *HTTPClient) Poll(ctx context.Context, req protocol.PollRequest) (protocol.RelayEnvelope, error) {
	var env protocol.RelayEnvelope
	if err := c.postJSON(ctx, "/remote/poll", req, &env); err != nil {
		return protocol.RelayEnvelope{}, err
	}
	return env, nil
}

func (c *HTTPClient) SendResult(ctx context.Context, req protocol.ResultRequest) error {
	return c.postJSON(ctx, "/remote/result", req, nil)
}

func (c *HTTPClient) Disconnect(ctx context.Context, req protocol.DisconnectRequest) error {
	return c.postJSON(ctx, "/remote/disconnect", req, nil)
}

func (c *HTTPClient) Chat(ctx context.Context, req protocol.ChatRequest) (protocol.ChatResponse, error) {
	var resp protocol.ChatResponse
	if err := c.postJSON(ctx, "/remote/chat", req, &resp); err != nil {
		return protocol.ChatResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) ChatStart(ctx context.Context, req protocol.ChatStartRequest) (protocol.ChatStartResponse, error) {
	var resp protocol.ChatStartResponse
	if err := c.postJSON(ctx, "/remote/chat/start", req, &resp); err != nil {
		return protocol.ChatStartResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) ChatEvents(ctx context.Context, reqBody protocol.ChatEventsRequest, onBatch func(protocol.ChatEventsBatch) error) error {
	buf, err := json.Marshal(reqBody)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/remote/chat/events", bytes.NewReader(buf))
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("Content-Type", "application/json")
	streamClient := *c.http
	streamClient.Timeout = 0
	resp, err := streamClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("http %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	reader := bufio.NewReader(resp.Body)
	for {
		eventName, data, err := readSSEFrame(reader)
		if err != nil {
			if err == io.EOF {
				return fmt.Errorf("chat events stream closed before done")
			}
			return err
		}
		if eventName != "chat" || strings.TrimSpace(data) == "" {
			continue
		}
		var batch protocol.ChatEventsBatch
		if err := json.Unmarshal([]byte(data), &batch); err != nil {
			return err
		}
		if onBatch != nil {
			if err := onBatch(batch); err != nil {
				return err
			}
		}
		if batch.Done {
			return nil
		}
	}
}

func (c *HTTPClient) ApprovalReply(ctx context.Context, req protocol.ApprovalReplyRequest) (protocol.ApprovalReplyResponse, error) {
	var resp protocol.ApprovalReplyResponse
	if err := c.postJSON(ctx, "/remote/approval/reply", req, &resp); err != nil {
		return protocol.ApprovalReplyResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) MCPManifest(ctx context.Context, req protocol.MCPManifestRequest) (protocol.MCPManifestResponse, error) {
	var resp protocol.MCPManifestResponse
	if err := c.postJSON(ctx, "/remote/mcp/manifest", req, &resp); err != nil {
		return protocol.MCPManifestResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) DownloadMCPArtifact(ctx context.Context, peerToken, artifactURL string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+artifactURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-RC-Peer-Token", peerToken)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("http %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return body, nil
}

func (c *HTTPClient) ReportMCPTools(ctx context.Context, req protocol.MCPToolsReport) (protocol.MCPToolsReportResponse, error) {
	var resp protocol.MCPToolsReportResponse
	if err := c.postJSON(ctx, "/remote/mcp/tools", req, &resp); err != nil {
		return protocol.MCPToolsReportResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) EnvironmentManifest(ctx context.Context, req protocol.EnvironmentManifestRequest) (protocol.EnvironmentManifestResponse, error) {
	var resp protocol.EnvironmentManifestResponse
	if err := c.postJSON(ctx, "/remote/environment/manifest", req, &resp); err != nil {
		return protocol.EnvironmentManifestResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) ClaimAgentRun(ctx context.Context, req protocol.AgentRunClaimRequest) (protocol.AgentRunClaimResponse, error) {
	var resp protocol.AgentRunClaimResponse
	if err := c.postJSON(ctx, "/remote/agent-runs/claim", req, &resp); err != nil {
		return protocol.AgentRunClaimResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) SendAgentRunEvent(ctx context.Context, req protocol.AgentRunEventReport) error {
	return c.postJSON(ctx, "/remote/agent-runs/event", req, nil)
}

func (c *HTTPClient) AgentRunHeartbeat(ctx context.Context, req protocol.AgentRunHeartbeatRequest) (protocol.AgentRunHeartbeatResponse, error) {
	var resp protocol.AgentRunHeartbeatResponse
	if err := c.postJSON(ctx, "/remote/agent-runs/heartbeat", req, &resp); err != nil {
		return protocol.AgentRunHeartbeatResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) PinAgentRunSession(ctx context.Context, req protocol.AgentRunSessionPinRequest) (protocol.AgentRunSessionPinResponse, error) {
	var resp protocol.AgentRunSessionPinResponse
	if err := c.postJSON(ctx, "/remote/agent-runs/session", req, &resp); err != nil {
		return protocol.AgentRunSessionPinResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) CompleteAgentRun(ctx context.Context, req protocol.AgentRunCompleteRequest) (protocol.AgentRunCompleteResponse, error) {
	var resp protocol.AgentRunCompleteResponse
	if err := c.postJSON(ctx, "/remote/agent-runs/complete", req, &resp); err != nil {
		return protocol.AgentRunCompleteResponse{}, err
	}
	return resp, nil
}

func readSSEFrame(reader *bufio.Reader) (string, string, error) {
	eventName := "message"
	var dataLines []string
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			return "", "", err
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			return eventName, strings.Join(dataLines, "\n"), nil
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		field, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		value = strings.TrimPrefix(value, " ")
		switch field {
		case "event":
			eventName = value
		case "data":
			dataLines = append(dataLines, value)
		}
	}
}

func (c *HTTPClient) postJSON(ctx context.Context, path string, reqBody any, out any) error {
	buf, err := json.Marshal(reqBody)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(buf))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode >= 400 {
		return fmt.Errorf("http %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	if out == nil || len(body) == 0 {
		return nil
	}
	return json.Unmarshal(body, out)
}

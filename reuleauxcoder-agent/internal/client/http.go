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

type HTTPError struct {
	StatusCode int
	Body       string
}

func (e *HTTPError) Error() string {
	if e.Body == "" {
		return fmt.Sprintf("http %d", e.StatusCode)
	}
	return fmt.Sprintf("http %d: %s", e.StatusCode, e.Body)
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

func (c *HTTPClient) Disconnect(ctx context.Context, req protocol.DisconnectRequest) error {
	return c.postJSON(ctx, "/remote/disconnect", req, nil)
}

func (c *HTTPClient) ClaimLocalActions(ctx context.Context, req protocol.LocalActionClaimRequest) (protocol.LocalActionClaimResponse, error) {
	var resp protocol.LocalActionClaimResponse
	if err := c.postJSON(ctx, "/remote/local-actions/claim", req, &resp); err != nil {
		return protocol.LocalActionClaimResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) ReportLocalActionProgress(ctx context.Context, req protocol.LocalActionProgressRequest) (protocol.LocalActionProgressResponse, error) {
	var resp protocol.LocalActionProgressResponse
	if err := c.postJSON(ctx, "/remote/local-actions/progress", req, &resp); err != nil {
		return protocol.LocalActionProgressResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) CompleteLocalAction(ctx context.Context, req protocol.LocalActionCompleteRequest) (protocol.LocalActionCompleteResponse, error) {
	var resp protocol.LocalActionCompleteResponse
	if err := c.postJSON(ctx, "/remote/local-actions/complete", req, &resp); err != nil {
		return protocol.LocalActionCompleteResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) CancelLocalAction(ctx context.Context, req protocol.LocalActionCancelRequest) (protocol.LocalActionCancelResponse, error) {
	var resp protocol.LocalActionCancelResponse
	if err := c.postJSON(ctx, "/remote/local-actions/cancel", req, &resp); err != nil {
		return protocol.LocalActionCancelResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) SessionRunStart(ctx context.Context, req protocol.SessionRunStartRequest) (protocol.SessionRunStartResponse, error) {
	var resp protocol.SessionRunStartResponse
	if err := c.postJSON(ctx, "/remote/session-runs/start", req, &resp); err != nil {
		return protocol.SessionRunStartResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) SessionRunEvents(ctx context.Context, reqBody protocol.SessionRunEventsRequest, onBatch func(protocol.SessionRunEventsBatch) error) error {
	buf, err := json.Marshal(reqBody)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/remote/session-runs/events", bytes.NewReader(buf))
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
		return &HTTPError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(body))}
	}

	reader := bufio.NewReader(resp.Body)
	for {
		eventName, data, err := readSSEFrame(reader)
		if err != nil {
			if err == io.EOF {
				return fmt.Errorf("session run events stream closed before done")
			}
			return err
		}
		if eventName != "session_run" || strings.TrimSpace(data) == "" {
			continue
		}
		var batch protocol.SessionRunEventsBatch
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
		return nil, &HTTPError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(body))}
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

func (c *HTTPClient) ClaimAgentRunActivation(ctx context.Context, req protocol.AgentRunActivationClaimRequest) (protocol.AgentRunActivationClaimResponse, error) {
	var resp protocol.AgentRunActivationClaimResponse
	if err := c.postJSON(ctx, "/remote/agent-run-activations/claim", req, &resp); err != nil {
		return protocol.AgentRunActivationClaimResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) SendAgentRunActivationEvent(ctx context.Context, req protocol.AgentRunActivationEventReport) error {
	return c.postJSON(ctx, "/remote/agent-run-activations/event", req, nil)
}

func (c *HTTPClient) AgentRunActivationHeartbeat(ctx context.Context, req protocol.AgentRunActivationHeartbeatRequest) (protocol.AgentRunActivationHeartbeatResponse, error) {
	var resp protocol.AgentRunActivationHeartbeatResponse
	if err := c.postJSON(ctx, "/remote/agent-run-activations/heartbeat", req, &resp); err != nil {
		return protocol.AgentRunActivationHeartbeatResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) PinAgentRunActivationSession(ctx context.Context, req protocol.AgentRunActivationSessionPinRequest) (protocol.AgentRunActivationSessionPinResponse, error) {
	var resp protocol.AgentRunActivationSessionPinResponse
	if err := c.postJSON(ctx, "/remote/agent-run-activations/session", req, &resp); err != nil {
		return protocol.AgentRunActivationSessionPinResponse{}, err
	}
	return resp, nil
}

func (c *HTTPClient) CompleteAgentRunActivation(ctx context.Context, req protocol.AgentRunActivationCompleteRequest) (protocol.AgentRunActivationCompleteResponse, error) {
	var resp protocol.AgentRunActivationCompleteResponse
	if err := c.postJSON(ctx, "/remote/agent-run-activations/complete", req, &resp); err != nil {
		return protocol.AgentRunActivationCompleteResponse{}, err
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
		return &HTTPError{StatusCode: resp.StatusCode, Body: strings.TrimSpace(string(body))}
	}
	if out == nil || len(body) == 0 {
		return nil
	}
	return json.Unmarshal(body, out)
}

package agentruntime

import (
	"encoding/json"
	"fmt"
	"strings"
)

type streamParser struct {
	provider        string
	output          strings.Builder
	sessionID       string
	pinnedSessionID string
	statusOverride  string
	errorText       string
	usage           map[string]TokenUsage
}

func newStreamParser(provider string) *streamParser {
	return &streamParser{
		provider: strings.ToLower(strings.TrimSpace(provider)),
		usage:    map[string]TokenUsage{},
	}
}

func (p *streamParser) ParseLine(line string) []Event {
	line = strings.TrimSpace(line)
	if line == "" {
		return nil
	}
	switch p.provider {
	case "claude":
		return p.parseClaude(line)
	case "gemini":
		return p.parseGemini(line)
	default:
		event := normalizeGenericStreamLine(p.provider, line)
		p.applyGenericEvent(event)
		if event.Type == EventText {
			p.output.WriteString(event.Text)
		}
		return []Event{event}
	}
}

func (p *streamParser) applyGenericEvent(event Event) {
	if len(event.Data) == 0 {
		if event.Type == EventError && p.errorText == "" {
			p.statusOverride = "failed"
			p.errorText = event.Text
		}
		return
	}
	if sid := eventStringValue(event.Data, "executor_session_id", "session_id", "thread_id", "threadId"); sid != "" {
		p.sessionID = sid
	}
	switch event.Type {
	case EventResult:
		if output := eventStringValue(event.Data, "output", "result"); output != "" {
			p.output.Reset()
			p.output.WriteString(output)
		}
		status := strings.ToLower(strings.TrimSpace(eventStringValue(event.Data, "status")))
		if status == "failed" || status == "cancelled" || status == "blocked" || status == "timeout" {
			p.statusOverride = status
			p.errorText = eventStringValue(event.Data, "error", "message")
		}
	case EventError:
		p.statusOverride = "failed"
		if p.errorText == "" {
			p.errorText = firstNonEmpty(event.Text, eventStringValue(event.Data, "message", "error"))
		}
	case EventStatus:
		status := strings.ToLower(strings.TrimSpace(eventStringValue(event.Data, "status")))
		if status == "failed" || status == "cancelled" || status == "blocked" || status == "timeout" {
			p.statusOverride = status
			p.errorText = eventStringValue(event.Data, "error", "message")
		}
	}
}

func (p *streamParser) Output() string {
	return p.output.String()
}

func (p *streamParser) SessionID() string {
	return p.sessionID
}

func (p *streamParser) Usage() map[string]TokenUsage {
	if len(p.usage) == 0 {
		return nil
	}
	out := make(map[string]TokenUsage, len(p.usage))
	for model, usage := range p.usage {
		out[model] = usage
	}
	return out
}

func (p *streamParser) StatusOverride() string {
	return p.statusOverride
}

func (p *streamParser) ErrorText() string {
	return p.errorText
}

func (p *streamParser) sessionEvents(sessionID string) []Event {
	sessionID = strings.TrimSpace(sessionID)
	if sessionID == "" {
		return nil
	}
	p.sessionID = sessionID
	if p.pinnedSessionID == sessionID {
		return nil
	}
	p.pinnedSessionID = sessionID
	return []Event{{
		Type: EventStatus,
		Data: map[string]any{
			"status":              "session_pinned",
			"executor_session_id": sessionID,
			"provider":            p.provider,
		},
	}}
}

func (p *streamParser) parseClaude(line string) []Event {
	var msg claudeStreamMessage
	if err := json.Unmarshal([]byte(line), &msg); err != nil {
		return []Event{{Type: EventLog, Text: line, Data: map[string]any{"provider": p.provider}}}
	}
	var events []Event
	events = append(events, p.sessionEvents(msg.SessionID)...)
	switch msg.Type {
	case "system":
		events = append(events, Event{Type: EventStatus, Data: map[string]any{
			"status":              "running",
			"executor_session_id": p.sessionID,
			"provider":            p.provider,
		}})
	case "assistant":
		events = append(events, p.parseClaudeAssistant(msg)...)
	case "user":
		events = append(events, p.parseClaudeUser(msg)...)
	case "result":
		if msg.ResultText != "" {
			p.output.Reset()
			p.output.WriteString(msg.ResultText)
		}
		if msg.IsError {
			p.statusOverride = "failed"
			p.errorText = msg.ResultText
		}
		events = append(events, Event{Type: EventResult, Data: mustJSONMap(line, p.provider)})
	case "log":
		if msg.Log != nil {
			events = append(events, Event{Type: EventLog, Text: msg.Log.Message, Data: map[string]any{
				"level":    msg.Log.Level,
				"provider": p.provider,
			}})
		}
	case "error":
		p.statusOverride = "failed"
		if msg.ResultText != "" {
			p.errorText = msg.ResultText
		}
		events = append(events, Event{Type: EventError, Text: p.errorText, Data: mustJSONMap(line, p.provider)})
	default:
		events = append(events, normalizeGenericStreamLine(p.provider, line))
	}
	return compactEvents(events)
}

func (p *streamParser) parseClaudeAssistant(msg claudeStreamMessage) []Event {
	var content claudeMessageContent
	if err := json.Unmarshal(msg.Message, &content); err != nil {
		return []Event{{Type: EventLog, Text: string(msg.Message), Data: map[string]any{"provider": p.provider}}}
	}
	if content.Usage != nil {
		model := strings.TrimSpace(content.Model)
		if model == "" {
			model = "claude"
		}
		u := p.usage[model]
		u.InputTokens += content.Usage.InputTokens
		u.OutputTokens += content.Usage.OutputTokens
		u.CacheReadTokens += content.Usage.CacheReadInputTokens
		u.CacheWriteTokens += content.Usage.CacheCreationInputTokens
		p.usage[model] = u
	}
	events := make([]Event, 0, len(content.Content))
	for _, block := range content.Content {
		switch block.Type {
		case "text":
			if block.Text != "" {
				p.output.WriteString(block.Text)
				events = append(events, Event{Type: EventText, Text: block.Text, Data: map[string]any{"provider": p.provider}})
			}
		case "thinking":
			if block.Text != "" {
				events = append(events, Event{Type: EventThinking, Text: block.Text, Data: map[string]any{"provider": p.provider}})
			}
		case "tool_use":
			var input map[string]any
			if len(block.Input) > 0 {
				_ = json.Unmarshal(block.Input, &input)
			}
			events = append(events, Event{Type: EventToolUse, Data: map[string]any{
				"provider": p.provider,
				"name":     block.Name,
				"id":       block.ID,
				"input":    input,
			}})
		}
	}
	return events
}

func (p *streamParser) parseClaudeUser(msg claudeStreamMessage) []Event {
	var content claudeMessageContent
	if err := json.Unmarshal(msg.Message, &content); err != nil {
		return []Event{{Type: EventLog, Text: string(msg.Message), Data: map[string]any{"provider": p.provider}}}
	}
	var events []Event
	for _, block := range content.Content {
		if block.Type != "tool_result" {
			continue
		}
		output := ""
		if len(block.Content) > 0 {
			output = string(block.Content)
		}
		events = append(events, Event{Type: EventToolResult, Text: output, Data: map[string]any{
			"provider":    p.provider,
			"tool_use_id": block.ToolUseID,
			"output":      output,
		}})
	}
	return events
}

func (p *streamParser) parseGemini(line string) []Event {
	var evt geminiStreamEvent
	if err := json.Unmarshal([]byte(line), &evt); err != nil {
		return []Event{{Type: EventLog, Text: line, Data: map[string]any{"provider": p.provider}}}
	}
	var events []Event
	events = append(events, p.sessionEvents(evt.SessionID)...)
	switch evt.Type {
	case "init":
		events = append(events, Event{Type: EventStatus, Data: map[string]any{
			"status":              "running",
			"executor_session_id": p.sessionID,
			"provider":            p.provider,
		}})
	case "message":
		if evt.Role == "assistant" && evt.Content != "" {
			p.output.WriteString(evt.Content)
			events = append(events, Event{Type: EventText, Text: evt.Content, Data: map[string]any{"provider": p.provider}})
		}
	case "tool_use":
		var input map[string]any
		if len(evt.Parameters) > 0 {
			_ = json.Unmarshal(evt.Parameters, &input)
		}
		events = append(events, Event{Type: EventToolUse, Data: map[string]any{
			"provider":  p.provider,
			"tool_name": evt.ToolName,
			"tool_id":   evt.ToolID,
			"input":     input,
		}})
	case "tool_result":
		events = append(events, Event{Type: EventToolResult, Text: evt.Output, Data: map[string]any{
			"provider": p.provider,
			"tool_id":  evt.ToolID,
			"status":   evt.Status,
			"output":   evt.Output,
		}})
	case "error":
		p.statusOverride = "failed"
		p.errorText = evt.Message
		events = append(events, Event{Type: EventError, Text: evt.Message, Data: mustJSONMap(line, p.provider)})
	case "result":
		if evt.Status == "error" && evt.Error != nil {
			p.statusOverride = "failed"
			p.errorText = evt.Error.Message
		}
		if evt.Stats != nil {
			p.accumulateGeminiUsage(evt)
		}
		events = append(events, Event{Type: EventResult, Data: mustJSONMap(line, p.provider)})
	default:
		events = append(events, normalizeGenericStreamLine(p.provider, line))
	}
	return compactEvents(events)
}

func (p *streamParser) accumulateGeminiUsage(evt geminiStreamEvent) {
	if evt.Stats == nil {
		return
	}
	for model, stats := range evt.Stats.Models {
		u := p.usage[model]
		u.InputTokens += int64(stats.InputTokens)
		u.OutputTokens += int64(stats.OutputTokens)
		u.CacheReadTokens += int64(stats.Cached)
		p.usage[model] = u
	}
	if len(evt.Stats.Models) == 0 {
		model := strings.TrimSpace(evt.Model)
		if model == "" {
			model = "gemini"
		}
		u := p.usage[model]
		u.InputTokens += int64(evt.Stats.InputTokens)
		u.OutputTokens += int64(evt.Stats.OutputTokens)
		p.usage[model] = u
	}
}

func normalizeStreamLine(provider, line string) Event {
	parser := newStreamParser(provider)
	events := parser.ParseLine(line)
	if len(events) == 0 {
		return Event{Type: EventLog, Text: strings.TrimSpace(line), Data: map[string]any{"provider": provider}}
	}
	for _, event := range events {
		if event.Type != EventStatus || event.Data["status"] != "session_pinned" {
			return event
		}
	}
	return events[0]
}

func normalizeGenericStreamLine(provider, line string) Event {
	var raw map[string]any
	if err := json.Unmarshal([]byte(line), &raw); err != nil {
		return Event{Type: EventLog, Text: line, Data: map[string]any{"provider": provider}}
	}
	if typ, ok := raw["type"].(string); ok {
		switch strings.ReplaceAll(typ, "-", "_") {
		case "text":
			if text, ok := raw["text"].(string); ok && text != "" {
				return Event{Type: EventText, Text: text, Data: raw}
			}
			if text, ok := raw["content"].(string); ok && text != "" {
				return Event{Type: EventText, Text: text, Data: raw}
			}
			return Event{Type: EventText, Data: raw}
		case "thinking":
			return Event{Type: EventThinking, Text: eventStringValue(raw, "text", "message", "content"), Data: raw}
		case "log":
			data := eventPayloadData(raw)
			return Event{Type: EventLog, Text: firstNonEmpty(eventStringValue(raw, "text", "message", "content"), eventStringValue(data, "text", "message", "content")), Data: data}
		case "tool_use":
			return Event{Type: EventToolUse, Data: eventPayloadData(raw)}
		case "tool_result":
			data := eventPayloadData(raw)
			return Event{Type: EventToolResult, Text: firstNonEmpty(eventStringValue(raw, "text", "output", "content"), eventStringValue(data, "output", "text", "content")), Data: data}
		case "error":
			return Event{Type: EventError, Text: eventStringValue(raw, "text", "message", "error"), Data: raw}
		case "usage":
			return Event{Type: EventUsage, Data: eventPayloadData(raw)}
		case "result":
			return Event{Type: EventResult, Text: eventStringValue(raw, "output", "result", "text"), Data: raw}
		}
	}
	if text, ok := raw["content"].(string); ok && text != "" {
		return Event{Type: EventText, Text: text, Data: raw}
	}
	if text, ok := raw["text"].(string); ok && text != "" {
		return Event{Type: EventText, Text: text, Data: raw}
	}
	return Event{Type: EventStatus, Data: raw}
}

func eventPayloadData(raw map[string]any) map[string]any {
	if data, ok := raw["data"].(map[string]any); ok {
		return data
	}
	return raw
}

func compactEvents(events []Event) []Event {
	out := events[:0]
	for _, event := range events {
		if event.Type == "" {
			continue
		}
		out = append(out, event)
	}
	return out
}

func mustJSONMap(line, provider string) map[string]any {
	var raw map[string]any
	if err := json.Unmarshal([]byte(line), &raw); err != nil {
		return map[string]any{"provider": provider}
	}
	if raw == nil {
		raw = map[string]any{}
	}
	raw["provider"] = provider
	return raw
}

func eventStringValue(data map[string]any, keys ...string) string {
	for _, key := range keys {
		if value, ok := data[key]; ok && value != nil {
			text := strings.TrimSpace(fmt.Sprint(value))
			if text != "" {
				return text
			}
		}
	}
	return ""
}

type claudeStreamMessage struct {
	Type       string          `json:"type"`
	Message    json.RawMessage `json:"message,omitempty"`
	Subtype    string          `json:"subtype,omitempty"`
	SessionID  string          `json:"session_id,omitempty"`
	ResultText string          `json:"result,omitempty"`
	IsError    bool            `json:"is_error,omitempty"`
	Log        *claudeLogEntry `json:"log,omitempty"`
}

type claudeLogEntry struct {
	Level   string `json:"level"`
	Message string `json:"message"`
}

type claudeMessageContent struct {
	Role    string               `json:"role"`
	Model   string               `json:"model"`
	Content []claudeContentBlock `json:"content"`
	Usage   *claudeUsage         `json:"usage,omitempty"`
}

type claudeUsage struct {
	InputTokens              int64 `json:"input_tokens"`
	OutputTokens             int64 `json:"output_tokens"`
	CacheReadInputTokens     int64 `json:"cache_read_input_tokens"`
	CacheCreationInputTokens int64 `json:"cache_creation_input_tokens"`
}

type claudeContentBlock struct {
	Type      string          `json:"type"`
	Text      string          `json:"text,omitempty"`
	ID        string          `json:"id,omitempty"`
	Name      string          `json:"name,omitempty"`
	Input     json.RawMessage `json:"input,omitempty"`
	ToolUseID string          `json:"tool_use_id,omitempty"`
	Content   json.RawMessage `json:"content,omitempty"`
}

type geminiStreamEvent struct {
	Type       string          `json:"type"`
	SessionID  string          `json:"session_id,omitempty"`
	Model      string          `json:"model,omitempty"`
	Role       string          `json:"role,omitempty"`
	Content    string          `json:"content,omitempty"`
	ToolName   string          `json:"tool_name,omitempty"`
	ToolID     string          `json:"tool_id,omitempty"`
	Parameters json.RawMessage `json:"parameters,omitempty"`
	Status     string          `json:"status,omitempty"`
	Output     string          `json:"output,omitempty"`
	Message    string          `json:"message,omitempty"`
	Error      *geminiError    `json:"error,omitempty"`
	Stats      *geminiStats    `json:"stats,omitempty"`
}

type geminiError struct {
	Type    string `json:"type"`
	Message string `json:"message"`
}

type geminiStats struct {
	TotalTokens  int                    `json:"total_tokens"`
	InputTokens  int                    `json:"input_tokens"`
	OutputTokens int                    `json:"output_tokens"`
	Models       map[string]geminiModel `json:"models,omitempty"`
}

type geminiModel struct {
	TotalTokens  int `json:"total_tokens"`
	InputTokens  int `json:"input_tokens"`
	OutputTokens int `json:"output_tokens"`
	Cached       int `json:"cached"`
}

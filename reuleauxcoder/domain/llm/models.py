"""LLM domain models - response and tool call structures."""

from dataclasses import dataclass, field
import json


EMPTY_ASSISTANT_CONTENT_PLACEHOLDER = "[No assistant content returned.]"


@dataclass
class ToolCall:
    """Represents a tool call from the LLM."""

    id: str
    name: str
    arguments: dict
    argument_error: str | None = None
    argument_diagnostics: list[dict] = field(default_factory=list)


@dataclass
class LLMResponse:
    """Response from the LLM including content and tool calls."""

    content: str = ""
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    reasoning_details: list[dict] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    usage_extra: dict = field(default_factory=dict)
    provider_response_id: str | None = None
    provider_extra: dict = field(default_factory=dict)
    tokens: list[str] = field(
        default_factory=list
    )  # Streamed tokens for event emission
    stream_status: str = "completed"
    interruption: dict | None = None
    recovery: dict | None = None

    @property
    def message(self) -> dict:
        """Convert to OpenAI message format for appending to history."""
        content = self.content or None
        if content is None and not self.tool_calls:
            content = EMPTY_ASSISTANT_CONTENT_PLACEHOLDER
        msg: dict = {"role": "assistant", "content": content}
        if self.reasoning_content is not None:
            msg["reasoning_content"] = self.reasoning_content
        if self.reasoning_signature is not None:
            msg["reasoning_signature"] = self.reasoning_signature
        if self.reasoning_details:
            msg["reasoning_details"] = list(self.reasoning_details)
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in self.tool_calls
            ]
        if self.stream_status != "completed":
            msg["stream_status"] = self.stream_status
        if self.interruption:
            msg["interruption"] = dict(self.interruption)
        if self.recovery:
            msg["recovery"] = dict(self.recovery)
        return msg

    @property
    def total_tokens(self) -> int:
        """Total tokens used in this response."""
        return self.prompt_tokens + self.completion_tokens

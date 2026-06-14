"""Controlled Mention Agent tool."""

from __future__ import annotations

import json
from typing import Any

from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.extensions.tools.spec import ToolExposure, ToolRisk
from labrastro_server.services.collaboration.service import IssueAssignmentService


class MentionAgentTool(Tool):
    """Tool for proposing Agent collaboration through mention records."""

    name = "mention_agent"
    namespace = "collaboration"
    risk = ToolRisk.CAPABILITY
    exposure = ToolExposure.HIDDEN
    permission_policy = "capability"
    description = (
        "Parse or record an @agent mention for issue/task collaboration. This "
        "tool can create mention and assignment records, but it cannot dispatch "
        "or create AgentRuns."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["parse_mention", "create_mention"],
            },
            "raw_text": {"type": "string"},
            "agent_ref": {"type": "string"},
            "issue_id": {"type": "string"},
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "context_type": {"type": "string"},
            "context_id": {"type": "string"},
            "metadata": {"type": "object"},
        },
        "required": ["operation", "raw_text"],
    }

    def __init__(
        self,
        service: IssueAssignmentService,
        *,
        peer_id: str | None = None,
    ) -> None:
        super().__init__(backend=None)
        self.service = service
        self.peer_id = peer_id

    def execute(self, operation: str, raw_text: str, **kwargs: Any) -> str:
        try:
            if operation == "parse_mention":
                mention = self.service.parse_mention(
                    raw_text=raw_text,
                    agent_ref=self._optional(kwargs.get("agent_ref")),
                    peer_id=self.peer_id,
                )
                return self._json({"ok": True, "mention": mention.to_dict()})
            if operation == "create_mention":
                mention = self.service.create_mention(
                    raw_text=raw_text,
                    peer_id=self.peer_id,
                    agent_ref=self._optional(kwargs.get("agent_ref")),
                    issue_id=self._optional(kwargs.get("issue_id")),
                    title=self._optional(kwargs.get("title")),
                    prompt=self._optional(kwargs.get("prompt")),
                    context_type=str(kwargs.get("context_type") or "chat"),
                    context_id=self._optional(kwargs.get("context_id")),
                    metadata=self._dict(kwargs.get("metadata")),
                )
                return self._json({"ok": True, "mention": mention.to_dict()})
            return f"Error: unknown Mention Agent operation: {operation}"
        except Exception as exc:
            return f"Error: {exc}"

    def _json(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _dict(self, value: Any) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    def _optional(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

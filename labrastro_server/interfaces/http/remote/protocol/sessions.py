"""Remote relay protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass
class SessionListRequest:
    peer_token: str
    limit: int = 20
    if_list_etag: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"peer_token": self.peer_token, "limit": self.limit}
        if self.if_list_etag:
            payload["if_list_etag"] = self.if_list_etag
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionListRequest":
        if_list_etag = d.get("if_list_etag")
        if not isinstance(if_list_etag, str):
            if_list_etag = d.get("ifListEtag")
        return cls(
            peer_token=d["peer_token"],
            limit=int(d.get("limit", 20)),
            if_list_etag=if_list_etag if isinstance(if_list_etag, str) else None,
        )

@dataclass
class SessionLoadRequest:
    peer_token: str
    session_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"peer_token": self.peer_token, "session_id": self.session_id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionLoadRequest":
        return cls(peer_token=d["peer_token"], session_id=d["session_id"])

@dataclass
class SessionNewRequest:
    peer_token: str

    def to_dict(self) -> dict[str, Any]:
        return {"peer_token": self.peer_token}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionNewRequest":
        return cls(peer_token=d["peer_token"])

@dataclass
class SessionDeleteRequest:
    peer_token: str
    session_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"peer_token": self.peer_token, "session_id": self.session_id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionDeleteRequest":
        return cls(peer_token=d["peer_token"], session_id=d["session_id"])

@dataclass
class SessionForkRequest:
    peer_token: str
    source_session_id: str
    keep_through_message_index: int = -1
    snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "peer_token": self.peer_token,
            "source_session_id": self.source_session_id,
            "keep_through_message_index": self.keep_through_message_index,
        }
        if self.snapshot:
            payload["snapshot"] = self.snapshot
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionForkRequest":
        snapshot = d.get("snapshot")
        if not isinstance(snapshot, dict):
            snapshot = {}
        return cls(
            peer_token=d["peer_token"],
            source_session_id=str(
                d.get("source_session_id") or d.get("sourceSessionId") or ""
            ),
            keep_through_message_index=int(
                d.get("keep_through_message_index")
                if d.get("keep_through_message_index") is not None
                else d.get("keepThroughMessageIndex", -1)
            ),
            snapshot=snapshot,
        )

@dataclass
class SessionSnapshotRequest:
    peer_token: str
    session_id: str
    snapshot: dict[str, Any] = field(default_factory=dict)
    snapshot_digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "peer_token": self.peer_token,
            "session_id": self.session_id,
            "snapshot": self.snapshot,
        }
        if self.snapshot_digest:
            payload["snapshot_digest"] = self.snapshot_digest
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionSnapshotRequest":
        snapshot = d.get("snapshot")
        if not isinstance(snapshot, dict):
            snapshot = {}
        snapshot_digest = d.get("snapshot_digest")
        if not isinstance(snapshot_digest, str):
            snapshot_digest = d.get("snapshotDigest")
        return cls(
            peer_token=d["peer_token"],
            session_id=d["session_id"],
            snapshot=snapshot,
            snapshot_digest=snapshot_digest
            if isinstance(snapshot_digest, str)
            else None,
        )

@dataclass
class SessionModelSwitchRequest:
    peer_token: str
    provider_id: str
    model_id: str
    session_id: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "peer_token": self.peer_token,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
        }
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        if self.parameters:
            payload["parameters"] = self.parameters
        return payload

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionModelSwitchRequest":
        parameters = d.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        return cls(
            peer_token=d["peer_token"],
            provider_id=str(d.get("provider_id") or d.get("provider") or ""),
            model_id=str(d.get("model_id") or d.get("model") or ""),
            session_id=str(d["session_id"]) if d.get("session_id") is not None else None,
            parameters=dict(parameters),
        )

__all__ = [
    "SessionListRequest",
    "SessionLoadRequest",
    "SessionNewRequest",
    "SessionDeleteRequest",
    "SessionForkRequest",
    "SessionSnapshotRequest",
    "SessionModelSwitchRequest",
]

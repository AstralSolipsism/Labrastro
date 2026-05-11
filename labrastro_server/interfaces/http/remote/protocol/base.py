"""Remote relay protocol models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass
class RelayEnvelope:
    """Top-level message wrapper for all relay communications."""

    type: str
    request_id: str | None = None
    peer_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "request_id": self.request_id,
            "peer_id": self.peer_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RelayEnvelope":
        return cls(
            type=d["type"],
            request_id=d.get("request_id"),
            peer_id=d.get("peer_id"),
            payload=d.get("payload", {}),
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@dataclass
class RegisterRequest:
    bootstrap_token: str
    host_info_min: dict[str, Any] = field(default_factory=dict)
    cwd: str = "."
    workspace_root: str | None = None
    features: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bootstrap_token": self.bootstrap_token,
            "host_info_min": self.host_info_min,
            "cwd": self.cwd,
            "workspace_root": self.workspace_root,
            "features": self.features,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegisterRequest":
        return cls(
            bootstrap_token=d["bootstrap_token"],
            host_info_min=d.get("host_info_min", {}),
            cwd=d.get("cwd", "."),
            workspace_root=d.get("workspace_root"),
            features=d.get("features", []),
        )

@dataclass
class RegisterResponse:
    peer_id: str
    peer_token: str
    heartbeat_interval_sec: int = 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "peer_token": self.peer_token,
            "heartbeat_interval_sec": self.heartbeat_interval_sec,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegisterResponse":
        return cls(
            peer_id=d["peer_id"],
            peer_token=d["peer_token"],
            heartbeat_interval_sec=d.get("heartbeat_interval_sec", 10),
        )

@dataclass
class RegisterRejected:
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegisterRejected":
        return cls(reason=d.get("reason", "unknown"))


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

@dataclass
class Heartbeat:
    peer_token: str
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"peer_token": self.peer_token, "ts": self.ts}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Heartbeat":
        return cls(peer_token=d["peer_token"], ts=d.get("ts", 0.0))


# ---------------------------------------------------------------------------
# Peer control-plane requests
# ---------------------------------------------------------------------------

@dataclass
class PeerPollRequest:
    peer_token: str

    def to_dict(self) -> dict[str, Any]:
        return {"peer_token": self.peer_token}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PeerPollRequest":
        return cls(peer_token=d["peer_token"])


@dataclass
class PeerResultRequest:
    peer_token: str
    request_id: str
    type: str = "tool_result"
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "request_id": self.request_id,
            "type": self.type,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PeerResultRequest":
        return cls(
            peer_token=d["peer_token"],
            request_id=d["request_id"],
            type=d.get("type", "tool_result"),
            payload=d.get("payload", {}),
        )


@dataclass
class PeerDisconnectRequest:
    peer_token: str
    reason: str = "peer_initiated"

    def to_dict(self) -> dict[str, Any]:
        return {"peer_token": self.peer_token, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PeerDisconnectRequest":
        return cls(
            peer_token=d["peer_token"],
            reason=d.get("reason", "peer_initiated"),
        )


@dataclass
class DisconnectNotice:
    reason: str = "peer_initiated"

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DisconnectNotice":
        return cls(reason=d.get("reason", "peer_initiated"))

__all__ = [
    "RelayEnvelope",
    "RegisterRequest",
    "RegisterResponse",
    "RegisterRejected",
    "Heartbeat",
    "PeerPollRequest",
    "PeerResultRequest",
    "PeerDisconnectRequest",
    "DisconnectNotice",
]

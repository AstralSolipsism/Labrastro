"""Host-side relay server for remote tool execution."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable

from labrastro_server.relay.auth import TokenManager
from labrastro_server.relay.errors import RegisterRejectedError
from labrastro_server.relay.peer_registry import PeerRegistry
from labrastro_server.interfaces.http.remote.protocol import (
    ErrorMessage,
    Heartbeat,
    RemoteMCPToolInfo,
    RegisterRejected,
    RegisterRequest,
    RegisterResponse,
    RelayEnvelope,
)


SendFn = Callable[[str, RelayEnvelope], None]


class RelayServer:
    """Host relay server: manages peer lifecycle and peer capability facts.

    Transport-agnostic: inject a ``send_fn(peer_id, envelope)`` that performs
    actual I/O (WebSocket, in-memory queue, etc.).
    """

    def __init__(
        self,
        send_fn: SendFn | None = None,
        heartbeat_interval_sec: int = 10,
        heartbeat_timeout_sec: int = 30,
        default_tool_timeout_sec: int = 30,
        shell_timeout_sec: int = 120,
        peer_token_ttl_sec: int = 3600,
    ):
        self._send_fn = send_fn
        self._token_manager = TokenManager()
        self._registry = PeerRegistry(heartbeat_timeout_sec=heartbeat_timeout_sec)
        self._heartbeat_interval_sec = heartbeat_interval_sec
        self._peer_token_ttl_sec = peer_token_ttl_sec

        # asyncio plumbing
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._prune_task: asyncio.Task | None = None
        self._shutdown_event = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the relay server in a background daemon thread."""
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        # schedule periodic prune
        future = asyncio.run_coroutine_threadsafe(self._prune_loop(), self._loop)
        # wait for prune loop to start so we know loop is running
        try:
            future.result(timeout=2.0)
        except Exception:
            pass

    def stop(self) -> None:
        """Stop the relay server."""
        self._shutdown_event.set()
        if self._loop is not None:
            if self._prune_task is not None:
                self._loop.call_soon_threadsafe(self._prune_task.cancel)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._loop = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.close()

    # ------------------------------------------------------------------
    # Public: inbound message handling (called by transport layer)
    # ------------------------------------------------------------------

    def handle_inbound(self, peer_id: str | None, envelope: RelayEnvelope) -> None:
        """Process a message received from a peer."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_inbound_async(peer_id, envelope), self._loop
        )

    async def _handle_inbound_async(
        self, peer_id: str | None, envelope: RelayEnvelope
    ) -> None:
        msg_type = envelope.type
        payload = envelope.payload
        req_id = envelope.request_id

        if msg_type == "register":
            req = RegisterRequest.from_dict(payload)
            try:
                resp = self._on_register(req)
                if isinstance(resp, RegisterResponse):
                    self._send(
                        resp.peer_id,
                        RelayEnvelope(
                            type="register_ok",
                            request_id=req_id,
                            peer_id=resp.peer_id,
                            payload=resp.to_dict(),
                        ),
                    )
                else:
                    self._send(
                        "",
                        RelayEnvelope(
                            type="register_rejected",
                            request_id=req_id,
                            payload=resp.to_dict(),
                        ),
                    )
            except RegisterRejectedError as e:
                self._send(
                    "",
                    RelayEnvelope(
                        type="register_rejected",
                        request_id=req_id,
                        payload=RegisterRejected(reason=e.message).to_dict(),
                    ),
                )

        elif msg_type == "heartbeat":
            hb = Heartbeat.from_dict(payload)
            peer = self._token_manager.refresh_peer_token(
                hb.peer_token, ttl_sec=self._peer_token_ttl_sec
            )
            if peer:
                self._registry.update_heartbeat(peer)

        elif msg_type == "disconnect":
            if peer_id:
                self.disconnect_peer(peer_id, "peer_initiated")

        elif msg_type == "error":
            err = ErrorMessage.from_dict(payload)
            if req_id:
                self._send(
                    peer_id or "",
                    RelayEnvelope(
                        type="error_ack",
                        request_id=req_id,
                        peer_id=peer_id or "",
                        payload=err.to_dict(),
                    ),
                )

    # ------------------------------------------------------------------
    # Public: peer facts
    # ------------------------------------------------------------------

    def update_peer_mcp_tools(
        self,
        peer_id: str,
        tools: list[RemoteMCPToolInfo],
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Persist peer-hosted MCP tool metadata for future peer agent creation."""
        peer = self._registry.get(peer_id)
        if peer is None:
            return False
        peer.meta["mcp_tools"] = [tool.to_dict() for tool in tools if tool.name]
        peer.meta["mcp_diagnostics"] = list(diagnostics or [])
        if tools and "mcp" not in peer.features:
            peer.features.append("mcp")
        return True

    def get_peer_mcp_tools(self, peer_id: str) -> list[RemoteMCPToolInfo]:
        peer = self._registry.get(peer_id)
        if peer is None:
            return []
        raw_tools = peer.meta.get("mcp_tools", [])
        if not isinstance(raw_tools, list):
            return []
        return [
            RemoteMCPToolInfo.from_dict(item)
            for item in raw_tools
            if isinstance(item, dict)
        ]

    def disconnect_peer(self, peer_id: str, reason: str = "peer_initiated") -> None:
        """Mark a peer disconnected."""
        self._registry.mark_disconnected(peer_id, reason)
        self._fail_pending_for_peer(peer_id)

    # ------------------------------------------------------------------
    # Internal: request/response correlation
    # ------------------------------------------------------------------

    def _send(self, peer_id: str, envelope: RelayEnvelope) -> None:
        if self._send_fn is not None:
            try:
                self._send_fn(peer_id, envelope)
            except Exception:
                pass

    def _fail_pending_for_peer(self, peer_id: str) -> None:
        return None

    # ------------------------------------------------------------------
    # Internal: registration / heartbeat
    # ------------------------------------------------------------------

    def _on_register(self, req: RegisterRequest) -> RegisterResponse | RegisterRejected:
        claims = self._token_manager.consume_bootstrap_token(req.bootstrap_token)
        if claims is None:
            raise RegisterRejectedError("Invalid or expired bootstrap token")

        meta = {
            "cwd": req.cwd,
            "workspace_root": req.workspace_root,
            "features": req.features,
            "host_info_min": req.host_info_min,
        }
        if claims:
            meta["auth_principal"] = dict(claims)
        peer_id = self._registry.register(meta=meta)
        peer_token = self._token_manager.issue_peer_token(
            peer_id, ttl_sec=self._peer_token_ttl_sec
        )
        return RegisterResponse(
            peer_id=peer_id,
            peer_token=peer_token,
            heartbeat_interval_sec=self._heartbeat_interval_sec,
        )

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    def issue_bootstrap_token(
        self, ttl_sec: int = 300, claims: dict | None = None
    ) -> str:
        """Host API: issue a one-time bootstrap token for a new peer."""
        return self._token_manager.issue_bootstrap_token(
            ttl_sec=ttl_sec, claims=claims
        )

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def _prune_loop(self) -> None:
        """Periodic cleanup of stale peers and expired tokens."""
        self._prune_task = asyncio.current_task()
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self._heartbeat_interval_sec)
            except asyncio.CancelledError:
                break
            stale = self._registry.prune_stale()
            for pid in stale:
                self._fail_pending_for_peer(pid)
            self._token_manager.prune_expired()

    @property
    def registry(self) -> PeerRegistry:
        return self._registry

    @property
    def token_manager(self) -> TokenManager:
        return self._token_manager

    @property
    def peer_token_ttl_sec(self) -> int:
        return self._peer_token_ttl_sec

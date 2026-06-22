"""Tests for remote execution relay server."""

from __future__ import annotations

import time

import pytest

from labrastro_server.relay.errors import (
    RegisterRejectedError,
)
from labrastro_server.interfaces.http.remote.protocol import (
    Heartbeat,
    RegisterRequest,
    RegisterResponse,
    RelayEnvelope,
)
from labrastro_server.relay.server import RelayServer


class TestRelayServerLifecycle:
    def test_start_stop(self) -> None:
        srv = RelayServer()
        srv.start()
        assert srv._loop is not None
        srv.stop()
        assert srv._loop is None

    def test_stop_idempotent(self) -> None:
        srv = RelayServer()
        srv.stop()
        srv.stop()


class TestRegistration:
    def test_register_success(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            req = RegisterRequest(bootstrap_token=bt, cwd="/tmp")
            resp = srv._on_register(req)
            assert isinstance(resp, RegisterResponse)
            assert resp.peer_id
            assert resp.peer_token.startswith("pt_")
        finally:
            srv.stop()
    def test_register_carries_bootstrap_claims_into_peer_meta(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(
                ttl_sec=60,
                claims={"user_id": "u1", "username": "alice", "device_id": "d1"},
            )
            resp = srv._on_register(RegisterRequest(bootstrap_token=bt, cwd="/tmp"))
            peer = srv.registry.get(resp.peer_id)
            assert peer is not None
            assert peer.meta["auth_principal"] == {
                "user_id": "u1",
                "username": "alice",
                "device_id": "d1",
            }
        finally:
            srv.stop()

    def test_register_uses_configured_peer_token_ttl(self) -> None:
        srv = RelayServer(peer_token_ttl_sec=0)
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            resp = srv._on_register(RegisterRequest(bootstrap_token=bt, cwd="/tmp"))
            time.sleep(0.01)
            assert srv.token_manager.verify_peer_token(resp.peer_token) is None
        finally:
            srv.stop()

    def test_register_rejected_bad_token(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            req = RegisterRequest(bootstrap_token="bt_invalid", cwd="/tmp")
            with pytest.raises(RegisterRejectedError):
                srv._on_register(req)
        finally:
            srv.stop()

    def test_register_rejected_used_token(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            req = RegisterRequest(bootstrap_token=bt, cwd="/tmp")
            srv._on_register(req)
            with pytest.raises(RegisterRejectedError):
                srv._on_register(req)
        finally:
            srv.stop()


class TestHeartbeat:
    def test_heartbeat_updates_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            req = RegisterRequest(bootstrap_token=bt, cwd="/tmp")
            resp = srv._on_register(req)

            before = srv.registry.get(resp.peer_id).last_seen_at
            time.sleep(0.02)
            hb = Heartbeat(peer_token=resp.peer_token)
            env = RelayEnvelope(
                type="heartbeat",
                peer_id=resp.peer_id,
                payload=hb.to_dict(),
            )
            srv.handle_inbound(resp.peer_id, env)
            time.sleep(0.05)
            after = srv.registry.get(resp.peer_id).last_seen_at
            assert after > before
        finally:
            srv.stop()

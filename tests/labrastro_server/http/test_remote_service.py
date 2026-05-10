"""Tests for the HTTP transport adapter around the remote relay host."""

from __future__ import annotations

import gzip
import json
import hashlib
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
from urllib import request
from urllib.error import HTTPError

import pytest


_URLOPEN = request.build_opener(request.ProxyHandler({})).open

_GO_AVAILABLE = shutil.which("go") is not None

from labrastro_server.interfaces.http.remote.service import (
    RemoteRelayHTTPService as _RemoteRelayHTTPService,
)
from labrastro_server.services.auth.models import AuthPrincipal
from labrastro_server.services.admin.service import RemoteAdminConfigManager
from reuleauxcoder.domain.config.models import (
    EnvironmentCLIToolConfig,
    AgentRuntimeConfig,
    MCPArtifactConfig,
    MCPLaunchConfig,
    MCPServerConfig,
)
from labrastro_server.services.agent_runtime.control_plane import AgentRuntimeControlPlane
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from labrastro_server.interfaces.http.remote.protocol import (
    ChatResponse,
    ChatStartRequest,
    CleanupResult,
    ExecToolResult,
    SessionDeleteRequest,
    SessionForkRequest,
    SessionListRequest,
    SessionLoadRequest,
    SessionNewRequest,
    SessionSnapshotRequest,
    ToolPreviewRequest,
    ToolPreviewResult,
    RelayEnvelope,
)
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.extensions.tools.builtin.write import WriteFileTool
from labrastro_server.adapters.reuleauxcoder.remote_backend import RemoteRelayToolBackend
from reuleauxcoder.interfaces.entrypoint.runner import (
    _default_create_remote_artifact_provider,
)
from reuleauxcoder.interfaces.events import UIEventBus


TEST_ADMIN_TOKEN = "test-admin-token"
TEST_ADMIN_HEADERS = {"Authorization": f"Bearer {TEST_ADMIN_TOKEN}"}


class _TestAuditStore:
    def __init__(self) -> None:
        self.audit_events: list[dict] = []

    def append_audit_event(self, event: dict) -> None:
        self.audit_events.append(dict(event))


class _TestAuthService:
    def __init__(self) -> None:
        self.store = _TestAuditStore()

    def state(self) -> dict:
        return {"ok": True, "auth_enabled": True, "login_required": True}

    def authenticate_access_token(self, token: str):
        if token == TEST_ADMIN_TOKEN:
            return AuthPrincipal("usr_test", "admin", "superadmin", "dev_test")
        return None

    def me(self, principal):
        return {"ok": True, "user": principal.public_user(), "device": None}

    def bootstrap_token(self, principal, ttl_sec: int):
        return {
            "ok": True,
            "bootstrap_token": self._issue_bootstrap_token(ttl_sec),
            "expires_in": ttl_sec,
        }

    def login(self, username: str, password: str, device_label: str):
        raise NotImplementedError

    def refresh(self, refresh_token: str):
        raise NotImplementedError

    def logout(self, refresh_token: str) -> None:
        return None


def RemoteRelayHTTPService(*args, **kwargs):  # noqa: N802
    service = _TestAuthService()
    auth_service = kwargs.pop("auth_service", service)
    instance = _RemoteRelayHTTPService(*args, auth_service=auth_service, **kwargs)
    service._issue_bootstrap_token = instance.issue_bootstrap_token
    return instance


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_request(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=request_headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body) if body else {}


def _raw_request(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=request_headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        return resp.status, dict(resp.headers.items()), resp.read()


def _text_request(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    req = request.Request(url, headers=headers or {}, method="GET")
    with _URLOPEN(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _bytes_request(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    req = request.Request(url, headers=headers or {}, method="GET")
    with _URLOPEN(req, timeout=5) as resp:
        return resp.status, resp.read()


def _build_go_agent_binary() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    agent_dir = repo_root / "reuleauxcoder-agent"
    target_dir = Path(tempfile.mkdtemp(prefix="rc-go-agent-bin-"))
    binary_path = target_dir / "reuleauxcoder-agent"
    subprocess.run(
        ["go", "build", "-o", str(binary_path), "./cmd/reuleauxcoder-agent"],
        cwd=agent_dir,
        check=True,
        timeout=120,
    )
    return binary_path


def _cleanup_provider_build_dir(provider: object) -> None:
    build_dir = getattr(provider, "_build_dir", None)
    if isinstance(build_dir, Path):
        shutil.rmtree(build_dir, ignore_errors=True)


def _fake_gh_env(tmp_path: Path, *, pr_url: str = "https://example.test/pr/fake") -> tuple[dict[str, str], Path]:
    gh_dir = tmp_path / "fake-gh"
    gh_dir.mkdir()
    log_path = gh_dir / "gh.log"
    if os.name == "nt":
        gh_path = gh_dir / "gh.bat"
        gh_path.write_text(
            "@echo off\r\n"
            "if \"%1\"==\"pr\" if \"%2\"==\"view\" goto view\r\n"
            "if \"%1\"==\"pr\" if \"%2\"==\"create\" goto create\r\n"
            "exit /b 2\r\n"
            ":view\r\n"
            "echo pr view>>\"%LABRASTRO_FAKE_GH_LOG%\"\r\n"
            "exit /b 1\r\n"
            ":create\r\n"
            "echo pr create>>\"%LABRASTRO_FAKE_GH_LOG%\"\r\n"
            "echo %LABRASTRO_FAKE_GH_CREATE_URL%\r\n"
            "exit /b 0\r\n",
            encoding="utf-8",
        )
    else:
        gh_path = gh_dir / "gh"
        gh_path.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = pr ] && [ \"$2\" = view ]; then echo pr view >> \"$LABRASTRO_FAKE_GH_LOG\"; exit 1; fi\n"
            "if [ \"$1\" = pr ] && [ \"$2\" = create ]; then echo pr create >> \"$LABRASTRO_FAKE_GH_LOG\"; echo \"$LABRASTRO_FAKE_GH_CREATE_URL\"; exit 0; fi\n"
            "exit 2\n",
            encoding="utf-8",
        )
        gh_path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(gh_dir) + os.pathsep + env.get("PATH", "")
    env["LABRASTRO_FAKE_GH_LOG"] = str(log_path)
    env["LABRASTRO_FAKE_GH_CREATE_URL"] = pr_url
    return env, log_path


class TestRemoteRelayHTTPService:
    def test_relay_send_preview_request_roundtrips_result(self) -> None:
        captured: list[RelayEnvelope] = []

        def send_fn(peer_id: str, envelope: RelayEnvelope) -> None:
            captured.append(envelope)
            relay.handle_inbound(
                peer_id,
                RelayEnvelope(
                    type="tool_preview_result",
                    request_id=envelope.request_id,
                    peer_id=peer_id,
                    payload=ToolPreviewResult(
                        ok=True,
                        sections=[
                            {
                                "id": "diff",
                                "kind": "diff",
                                "content": "--- a/a.txt\n+++ b/a.txt\n",
                            }
                        ],
                        resolved_path="/repo/a.txt",
                        old_sha256="abc",
                        old_exists=True,
                    ).to_dict(),
                ),
            )

        relay = RelayServer(send_fn=send_fn)
        relay.start()
        try:
            peer_id = relay.registry.register(
                {"capabilities": ["tool_preview"], "cwd": "/repo"}
            )
            result = relay.send_preview_request(
                peer_id,
                ToolPreviewRequest(
                    tool_name="write_file",
                    args={"file_path": "a.txt", "content": "new"},
                    cwd="/repo",
                ),
                timeout_sec=2,
            )

            assert captured[0].type == "preview_tool"
            assert result.ok is True
            assert result.sections[0]["kind"] == "diff"
            assert result.resolved_path == "/repo/a.txt"
            assert result.old_sha256 == "abc"
            assert result.old_exists is True
        finally:
            relay.stop()

    def test_admin_provider_and_model_endpoints_require_login_and_mask_keys(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        reloads: list[str] = []
        config_path = tmp_path / "config.yaml"
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            admin_config_reload_handler=lambda: reloads.append("reload"),
            admin_provider_test_handler=lambda provider, model, prompt: {
                "ok": True,
                "provider_id": provider.id,
                "model": model,
                "prompt": prompt,
            },
            admin_provider_models_handler=lambda provider: {
                "ok": True,
                "provider_id": provider.id,
                "unsupported": False,
                "models": [
                    {"id": "deepseek-chat", "owned_by": "deepseek"},
                    {"id": "deepseek-reasoner", "owned_by": "deepseek"},
                ],
            },
        )
        service.start()
        try:
            try:
                _json_request(
                    "POST", f"{service.base_url}/remote/admin/providers/list", {}
                )
                raise AssertionError("admin endpoint should require login")
            except HTTPError as exc:
                assert exc.code == 401

            admin_headers = TEST_ADMIN_HEADERS
            status, record = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/record",
                {
                    "provider_id": "deepseek",
                    "type": "openai_chat",
                    "compat": "deepseek",
                    "api_key": "sk-secret-value",
                    "base_url": "https://api.deepseek.com",
                },
                headers=admin_headers,
            )
            assert status == 200
            assert record["ok"] is True
            assert record["provider"]["api_key_hint"] == "sk-s...alue"
            assert "api_key" not in record["provider"]

            _, update = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/record",
                {
                    "provider_id": "deepseek",
                    "type": "openai_chat",
                    "compat": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                },
                headers=admin_headers,
            )
            assert update["created"] is False

            _, providers = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/list",
                {},
                headers=admin_headers,
            )
            assert providers["providers"][0]["api_key_hint"] == "sk-s...alue"
            assert "api_key" not in providers["providers"][0]
            assert providers["providers"][0]["enabled"] is True

            _, model_list = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/models",
                {"provider_id": "deepseek"},
                headers=admin_headers,
            )
            assert model_list["models"][0]["id"] == "deepseek-chat"

            _, test_result = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/test",
                {"provider_id": "deepseek", "model": "deepseek-chat", "prompt": "ping"},
                headers=admin_headers,
            )
            assert test_result == {
                "ok": True,
                "provider_id": "deepseek",
                "model": "deepseek-chat",
                "prompt": "ping",
            }

            _, profile = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/models/record",
                {
                    "profile_id": "deepseek-main",
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "max_tokens": 4096,
                    "max_context_tokens": 128000,
                    "temperature": 0,
                    "thinking_enabled": True,
                },
                headers=admin_headers,
            )
            assert profile["model_profile"]["provider"] == "deepseek"
            assert "api_key" not in profile["model_profile"]

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/providers/delete",
                    {"provider_id": "deepseek"},
                    headers=admin_headers,
                )
                raise AssertionError("delete should be blocked while profiles reference provider")
            except HTTPError as exc:
                assert exc.code == 409
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "provider_in_use"
                assert body["details"]["blockers"][0]["profile_id"] == "deepseek-main"

            _, active = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/models/activate",
                {"profile_id": "deepseek-main", "target": "both"},
                headers=admin_headers,
            )
            assert active["active_main"] == "deepseek-main"
            assert active["active_sub"] == "deepseek-main"

            _, disabled = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/enable",
                {"provider_id": "deepseek", "enabled": False},
                headers=admin_headers,
            )
            assert disabled["provider"]["enabled"] is False
            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/models/activate",
                    {"profile_id": "deepseek-main", "target": "main"},
                    headers=admin_headers,
                )
                raise AssertionError("disabled provider should block activation")
            except HTTPError as exc:
                assert exc.code == 409
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "provider_disabled"

            _, copied = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/copy",
                {"provider_id": "deepseek", "target_id": "deepseek-copy"},
                headers=admin_headers,
            )
            assert copied["provider"]["id"] == "deepseek-copy"
            assert copied["provider"]["enabled"] is True
            assert copied["provider"]["api_key_hint"] == "sk-s...alue"

            _, models = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/models/list",
                {},
                headers=admin_headers,
            )
            assert models["active_main"] == "deepseek-main"
            assert models["model_profiles"][0]["id"] == "deepseek-main"
            _, deleted = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/delete",
                {"provider_id": "deepseek-copy"},
                headers=admin_headers,
            )
            assert deleted["ok"] is True
            assert deleted["provider_id"] == "deepseek-copy"
            assert "config_etag" in deleted
            assert len(reloads) == 8
            raw = config_path.read_text(encoding="utf-8")
            assert "sk-secret-value" in raw
            assert "active_main: deepseek-main" in raw
            assert "deepseek-copy" not in raw
        finally:
            service.stop()
            relay.stop()

    def test_admin_status_returns_chat_modes(self) -> None:
        config_data = {
            "modes": {
                "active": "planner",
                "profiles": {
                    "coder": {"description": "Code changes"},
                    "planner": {
                        "description": "Plan first",
                        "tools": ["read_file"],
                    },
                },
            }
        }
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=Path("unused-config.yaml"),
        )
        service.start()
        try:
            with patch.object(RemoteAdminConfigManager, "_load_data", return_value=config_data):
                status, body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/status",
                    {},
                    headers=TEST_ADMIN_HEADERS,
                )

            assert status == 200
            assert body["active_mode"] == "planner"
            modes = {mode["name"]: mode for mode in body["modes"]}
            assert set(modes) >= {"coder", "planner", "debugger", "taskflow"}
            assert modes["coder"]["description"] == "Code changes"
            assert modes["planner"]["description"] == "Plan first"
            assert modes["planner"]["tools"] == ["read_file"]
        finally:
            service.stop()
            relay.stop()

    def test_admin_status_falls_back_to_builtin_chat_modes(self) -> None:
        manager = RemoteAdminConfigManager(Path("unused-config.yaml"))
        with patch.object(manager, "_load_data", return_value={}):
            status = manager.status()

        assert status["active_mode"] == "coder"
        assert {mode["name"] for mode in status["modes"]} >= {
            "coder",
            "planner",
            "debugger",
        }

    def test_admin_toolchain_endpoints_manage_manifest(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        reloads: list[str] = []
        config_path = tmp_path / "config.yaml"
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            admin_config_reload_handler=lambda: reloads.append("reload"),
        )
        service.start()
        try:
            admin_headers = TEST_ADMIN_HEADERS

            _, cli = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/toolchains/record",
                {
                    "kind": "cli",
                    "payload": {
                        "name": "gitnexus",
                        "command": "gitnexus",
                        "placement": "both",
                        "capabilities": ["repo_index"],
                        "requirements": {"node": ">=20"},
                        "check": "gitnexus --version",
                        "install": "npm install -g gitnexus",
                        "repo_url": "https://example.test/gitnexus/repo",
                        "docs": [{"title": "GitNexus", "url": "https://example.test/gitnexus"}],
                        "evidence": [
                            {
                                "field": "install",
                                "title": "GitNexus install",
                                "url": "https://example.test/gitnexus",
                                "excerpt": "Install with npm.",
                            }
                        ],
                        "install_prompt": "Use npm.",
                        "verify_prompt": "Run version check.",
                        "notes": ["PATH changes need approval."],
                        "credentials": ["GITNEXUS_TOKEN"],
                        "risk_level": "medium",
                    },
                },
                headers=admin_headers,
            )
            assert cli["toolchain"]["name"] == "gitnexus"
            assert cli["toolchain"]["placement"] == "both"
            assert cli["toolchain"]["docs"][0]["title"] == "GitNexus"
            assert cli["toolchain"]["evidence"][0]["field"] == "install"

            _, mcp = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/toolchains/record",
                {
                    "kind": "mcp",
                    "payload": {
                        "name": "gitnexus-mcp",
                        "command": "gitnexus",
                        "args": ["mcp"],
                        "placement": "peer",
                        "distribution": "command",
                        "requirements": {"node": ">=20"},
                        "check": "gitnexus --version",
                        "install_prompt": "Start MCP with args.",
                    },
                },
                headers=admin_headers,
            )
            assert mcp["toolchain"]["args"] == ["mcp"]
            assert mcp["toolchain"]["requirements"]["node"] == ">=20"

            _, skill = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/toolchains/record",
                {
                    "kind": "skill",
                    "payload": {
                        "name": "collaborating-with-claude",
                        "scope": "user",
                        "check": "Test-Path ~/.agents/skills/collaborating-with-claude/SKILL.md",
                        "install": "python install-skill.py",
                        "path_hint": "~/.agents/skills/collaborating-with-claude/SKILL.md",
                        "verify_prompt": "Check SKILL.md.",
                    },
                },
                headers=admin_headers,
            )
            assert skill["toolchain"]["scope"] == "user"

            _, listed = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/toolchains/list",
                {},
                headers=admin_headers,
            )
            assert listed["cli_tools"][0]["name"] == "gitnexus"
            assert listed["mcp_servers"][0]["name"] == "gitnexus-mcp"
            assert listed["skills"][0]["name"] == "collaborating-with-claude"

            _, dashboard = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/toolchains/dashboard",
                {},
                headers=admin_headers,
            )
            rows = {item["id"]: item for item in dashboard["items"]}
            assert dashboard["summary"]["total"] == 3
            assert rows["cli:gitnexus"]["kind"] == "cli"
            assert rows["cli:gitnexus"]["placement"] == "both"
            assert rows["cli:gitnexus"]["repo_url"] == "https://example.test/gitnexus/repo"
            assert rows["cli:gitnexus"]["credentials"] == ["GITNEXUS_TOKEN"]
            assert rows["mcp:gitnexus-mcp"]["placement"] == "peer"
            assert rows["skill:collaborating-with-claude"]["scope"] == "user"

            _, disabled = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/toolchains/enable",
                {"kind": "cli", "name": "gitnexus", "enabled": False},
                headers=admin_headers,
            )
            assert disabled["toolchain"]["enabled"] is False

            _, deleted = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/toolchains/delete",
                {"kind": "mcp", "name": "gitnexus-mcp"},
                headers=admin_headers,
            )
            assert deleted["ok"] is True
            assert deleted["kind"] == "mcp"
            assert deleted["name"] == "gitnexus-mcp"
            assert "config_etag" in deleted

            raw = config_path.read_text(encoding="utf-8")
            assert "gitnexus:" in raw
            assert "enabled: false" in raw
            assert "collaborating-with-claude:" in raw
            assert "gitnexus-mcp" not in raw
            assert len(reloads) == 5
        finally:
            service.stop()
            relay.stop()

    def test_admin_write_rolls_back_when_reload_fails(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "providers:\n"
            "  items:\n"
            "    existing:\n"
            "      type: openai_chat\n"
            "      api_key: sk-existing\n"
            "      base_url: https://example.invalid/v1\n",
            encoding="utf-8",
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            admin_config_reload_handler=lambda: (_ for _ in ()).throw(
                RuntimeError("reload failed")
            ),
        )
        service.start()
        try:
            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/providers/record",
                    {
                        "provider_id": "broken",
                        "type": "openai_chat",
                        "api_key": "sk-broken",
                        "base_url": "https://broken.invalid/v1",
                    },
                    headers=TEST_ADMIN_HEADERS,
                )
                raise AssertionError("reload failure should surface as HTTP 500")
            except HTTPError as exc:
                assert exc.code == 500
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "config_reload_failed"
            raw = config_path.read_text(encoding="utf-8")
            assert "existing" in raw
            assert "sk-existing" in raw
            assert "broken" not in raw
            assert "sk-broken" not in raw
            audit_text = json.dumps(service.auth_service.store.audit_events)
            assert "admin_config_failed" in audit_text
            assert "sk-broken" not in audit_text
        finally:
            service.stop()
            relay.stop()

    def test_admin_config_etag_conflict_and_audit_redacts_secrets(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        config_path = tmp_path / "config.yaml"
        config_path.write_text("models:\n  profiles: {}\n", encoding="utf-8")
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
        )
        service.start()
        try:
            _, read_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/server-settings/read",
                {},
                headers=TEST_ADMIN_HEADERS,
            )
            initial_etag = read_body["config_etag"]

            _, created = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/record",
                {
                    "if_match": initial_etag,
                    "provider_id": "deepseek",
                    "type": "openai_chat",
                    "api_key": "sk-secret-value",
                    "base_url": "https://api.deepseek.com/v1",
                },
                headers=TEST_ADMIN_HEADERS,
            )
            assert created["ok"] is True
            assert created["config_etag"] != initial_etag

            _, enabled = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/providers/enable",
                {"provider_id": "deepseek", "enabled": False},
                headers={**TEST_ADMIN_HEADERS, "If-Match": created["config_etag"]},
            )
            assert enabled["ok"] is True

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/providers/record",
                    {
                        "if_match": initial_etag,
                        "provider_id": "blocked",
                        "type": "openai_chat",
                        "base_url": "https://blocked.invalid/v1",
                    },
                    headers=TEST_ADMIN_HEADERS,
                )
                raise AssertionError("stale if_match should be rejected")
            except HTTPError as exc:
                assert exc.code == 409
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "config_version_conflict"
                assert body["details"]["config_etag"] == enabled["config_etag"]

            raw = config_path.read_text(encoding="utf-8")
            assert "blocked" not in raw
            events = service.auth_service.store.audit_events
            event_types = [event["type"] for event in events]
            assert "admin_config_updated" in event_types
            assert "admin_config_conflict" in event_types
            audit_text = json.dumps(events)
            assert "sk-secret-value" not in audit_text
            assert "***" in audit_text
        finally:
            service.stop()
            relay.stop()

    def test_auth_bootstrap_token_and_artifact_endpoint(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            artifact_provider=lambda os_name, arch, name: (
                (
                    b"peer-binary",
                    "application/octet-stream",
                )
                if (os_name, arch, name) == ("linux", "amd64", "rcoder-peer")
                else None
            ),
            bootstrap_token_ttl_sec=60,
        )
        service.start()
        try:
            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/auth/bootstrap-token",
                    {},
                )
                raise AssertionError("bootstrap token should require auth")
            except HTTPError as exc:
                assert exc.code == 401
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "unauthorized"

            status, body = _json_request(
                "POST",
                f"{service.base_url}/remote/auth/bootstrap-token",
                {},
                headers=TEST_ADMIN_HEADERS,
            )
            assert status == 200
            assert body["bootstrap_token"].startswith("bt_")

            with _URLOPEN(
                f"{service.base_url}/remote/artifacts/linux/amd64/rcoder-peer",
                timeout=5,
            ) as resp:
                assert resp.status == 200
                etag = resp.headers["ETag"]
                assert etag.startswith('"sha256-')
                assert resp.read() == b"peer-binary"
            req = request.Request(
                f"{service.base_url}/remote/artifacts/linux/amd64/rcoder-peer",
                headers={"If-None-Match": etag},
                method="GET",
            )
            with pytest.raises(HTTPError) as excinfo:
                _URLOPEN(req, timeout=5)
            assert excinfo.value.code == 304
            assert excinfo.value.headers["ETag"] == etag
        finally:
            service.stop()
            relay.stop()

    def test_peer_mcp_manifest_artifact_and_tools_report(self, tmp_path: Path) -> None:
        artifact_root = tmp_path / "artifacts"
        artifact_path = artifact_root / "local-filesystem" / "1.0.0" / "linux-amd64.tar.gz"
        artifact_path.parent.mkdir(parents=True)
        artifact_content = b"fake-archive"
        artifact_path.write_bytes(artifact_content)
        artifact_sha = hashlib.sha256(artifact_content).hexdigest()

        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            mcp_artifact_root=artifact_root,
            mcp_servers=[
                MCPServerConfig(
                    name="github",
                    command="github-mcp",
                    placement="server",
                ),
                MCPServerConfig(
                    name="local-filesystem",
                    command="",
                    placement="peer",
                    distribution="artifact",
                    version="1.0.0",
                    launch=MCPLaunchConfig(
                        command="{{bundle}}/filesystem-mcp",
                        args=["--root", "{{workspace}}"],
                    ),
                    artifacts={
                        "linux-amd64": MCPArtifactConfig(
                            path="local-filesystem/1.0.0/linux-amd64.tar.gz",
                            sha256=artifact_sha,
                            launch=MCPLaunchConfig(
                                command="{{bundle}}/run.sh",
                                args=["--root", "{{workspace}}"],
                            ),
                        )
                    },
                    requirements={"node": "required", "npm": "required"},
                    permissions={"tools": {"write_file": "require_approval"}},
                ),
                MCPServerConfig(
                    name="missing-platform",
                    command="missing",
                    placement="peer",
                    distribution="artifact",
                    version="1.0.0",
                    launch=MCPLaunchConfig(command="{{bundle}}/missing"),
                    artifacts={},
                ),
                MCPServerConfig(
                    name="shared-browser",
                    command="npx",
                    args=["-y", "@demo/browser@1.0.0"],
                    placement="both",
                    distribution="artifact",
                    version="1.0.0",
                    launch=MCPLaunchConfig(command="{{bundle}}/browser"),
                    artifacts={
                        "linux-amd64": MCPArtifactConfig(
                            path="shared-browser/1.0.0/linux-amd64.tar.gz",
                            sha256=artifact_sha,
                        )
                    },
                ),
            ],
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_id = register_body["payload"]["peer_id"]
            peer_token = register_body["payload"]["peer_token"]

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/mcp/manifest",
                    {"peer_token": "bad", "os": "linux", "arch": "amd64"},
                )
                raise AssertionError("manifest should require valid peer token")
            except HTTPError as exc:
                assert exc.code == 401

            status, manifest = _json_request(
                "POST",
                f"{service.base_url}/remote/mcp/manifest",
                {
                    "peer_token": peer_token,
                    "os": "linux",
                    "arch": "amd64",
                    "workspace": "/tmp/peer",
                },
            )
            assert status == 200
            assert [server["name"] for server in manifest["servers"]] == [
                "local-filesystem",
                "shared-browser",
            ]
            server_manifest = manifest["servers"][0]
            assert server_manifest["artifact"]["sha256"] == artifact_sha
            assert server_manifest["distribution"] == "artifact"
            assert server_manifest["launch"]["command"] == "{{bundle}}/run.sh"
            assert server_manifest["launch"]["args"] == ["--root", "{{workspace}}"]
            assert server_manifest["requirements"] == {
                "node": "required",
                "npm": "required",
            }
            assert server_manifest["permissions"]["tools"]["write_file"] == "require_approval"
            assert manifest["diagnostics"][0]["server"] == "missing-platform"

            try:
                _bytes_request(
                    f"{service.base_url}{server_manifest['artifact']['url']}"
                )
                raise AssertionError("artifact should require peer token")
            except HTTPError as exc:
                assert exc.code == 401

            status, body = _bytes_request(
                f"{service.base_url}{server_manifest['artifact']['url']}",
                headers={"X-RC-Peer-Token": peer_token},
            )
            assert status == 200
            assert body == artifact_content
            req = request.Request(
                f"{service.base_url}{server_manifest['artifact']['url']}",
                headers={
                    "X-RC-Peer-Token": peer_token,
                    "If-None-Match": f'"sha256-{artifact_sha}"',
                },
                method="GET",
            )
            with pytest.raises(HTTPError) as excinfo:
                _URLOPEN(req, timeout=5)
            assert excinfo.value.code == 304
            assert excinfo.value.headers["ETag"] == f'"sha256-{artifact_sha}"'

            status, report = _json_request(
                "POST",
                f"{service.base_url}/remote/mcp/tools",
                {
                    "peer_token": peer_token,
                    "tools": [
                        {
                            "name": "read_file",
                            "description": "Read a local file",
                            "input_schema": {"type": "object"},
                            "server_name": "local-filesystem",
                        }
                    ],
                },
            )
            assert status == 200
            assert report["ok"] is True
            assert relay.get_peer_mcp_tools(peer_id)[0].server_name == "local-filesystem"
        finally:
            service.stop()
            relay.stop()

    def test_peer_mcp_manifest_command_distribution_without_artifact(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            mcp_servers=[
                MCPServerConfig(
                    name="gitnexus",
                    command="gitnexus",
                    args=["mcp"],
                    placement="peer",
                    distribution="command",
                    version="1.6.3",
                    check="gitnexus --version",
                    install="npm install -g gitnexus@1.6.3",
                    requirements={"node": ">=20", "npm": "required"},
                    artifacts={
                        "linux-amd64": MCPArtifactConfig(
                            path="gitnexus/1.6.3/linux-amd64.tar.gz",
                            sha256="legacy",
                        )
                    },
                )
            ],
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            status, manifest = _json_request(
                "POST",
                f"{service.base_url}/remote/mcp/manifest",
                {
                    "peer_token": peer_token,
                    "os": "linux",
                    "arch": "amd64",
                    "workspace": "/tmp/peer",
                },
            )

            assert status == 200
            assert manifest["diagnostics"] == []
            server = manifest["servers"][0]
            assert server["name"] == "gitnexus"
            assert server["distribution"] == "command"
            assert server["artifact"] is None
            assert server["launch"]["command"] == "gitnexus"
            assert server["launch"]["args"] == ["mcp"]
            assert server["requirements"]["node"] == ">=20"
        finally:
            service.stop()
            relay.stop()

    def test_environment_manifest_endpoint_returns_structured_manifest(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            mcp_servers=[
                MCPServerConfig(
                    name="gitnexus-mcp",
                    command="gitnexus",
                    args=["mcp"],
                    placement="peer",
                    distribution="command",
                    check="gitnexus --version",
                    install="npm install -g gitnexus@1.6.3",
                    requirements={"node": ">=20"},
                    docs=[{"title": "GitNexus MCP", "url": "https://example.test/mcp"}],
                    install_prompt="Install MCP through npm only.",
                    verify_prompt="Verify MCP starts with the mcp argument.",
                    notes=["Do not install node automatically."],
                ),
                MCPServerConfig(
                    name="disabled-mcp",
                    command="disabled-mcp",
                    enabled=False,
                    placement="peer",
                )
            ],
            environment_cli_tools={
                "beads": {
                    "command": "beads",
                    "capabilities": ["issue_tracking"],
                    "check": "beads --version",
                    "install": "npm install -g beads",
                    "source": "npm",
                    "docs": [{"title": "Beads", "url": "https://example.test/beads"}],
                    "install_prompt": "Use npm global install for beads.",
                    "verify_prompt": "Run beads --version after install.",
                    "notes": ["Do not install node automatically."],
                },
                "gitnexus": EnvironmentCLIToolConfig(
                    name="gitnexus",
                    command="gitnexus",
                    capabilities=["repo_index"],
                    check="gitnexus --version",
                    install="npm install -g gitnexus",
                    source="npm",
                    docs=[{"title": "GitNexus", "url": "https://example.test/gitnexus"}],
                    install_prompt="Use the configured npm command.",
                    verify_prompt="Check gitnexus version output.",
                    notes=["PATH changes require explicit approval."],
                ),
                "disabled-cli": EnvironmentCLIToolConfig(
                    name="disabled-cli",
                    command="disabled-cli",
                    enabled=False,
                    check="disabled-cli --version",
                )
            },
            environment_skills={
                "collaborating-with-claude": {
                    "scope": "user",
                    "check": "Test-Path ~/.agents/skills/collaborating-with-claude/SKILL.md",
                    "install": "python install-skill.py",
                    "version": "1.0.0",
                    "source": "github",
                    "description": "Claude bridge skill",
                    "path_hint": "~/.agents/skills/collaborating-with-claude/SKILL.md",
                    "docs": [{"title": "Claude skill", "url": "https://example.test/skill"}],
                    "install_prompt": "Install from the curated skill source.",
                    "verify_prompt": "Verify SKILL.md exists.",
                    "notes": ["Use user scope."],
                },
                "disabled-skill": {
                    "enabled": False,
                    "check": "Test-Path disabled",
                }
            },
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/environment/manifest",
                    {"peer_token": "bad", "os": "linux", "arch": "amd64"},
                )
                raise AssertionError("environment manifest should require valid token")
            except HTTPError as exc:
                assert exc.code == 401

            status, manifest = _json_request(
                "POST",
                f"{service.base_url}/remote/environment/manifest",
                {
                    "peer_token": peer_token,
                    "os": "linux",
                    "arch": "amd64",
                    "workspace": "/tmp/peer",
                },
            )

            assert status == 200
            tools = {tool["name"]: tool for tool in manifest["cli_tools"]}
            mcp_servers = {server["name"]: server for server in manifest["mcp_servers"]}
            skills = {skill["name"]: skill for skill in manifest["skills"]}
            assert "disabled-cli" not in tools
            assert "disabled-mcp" not in mcp_servers
            assert "disabled-skill" not in skills
            assert tools["gitnexus"]["check"] == "gitnexus --version"
            assert tools["gitnexus"]["docs"][0]["title"] == "GitNexus"
            assert tools["gitnexus"]["install_prompt"] == "Use the configured npm command."
            assert tools["gitnexus"]["verify_prompt"] == "Check gitnexus version output."
            assert tools["gitnexus"]["notes"] == ["PATH changes require explicit approval."]
            assert tools["beads"]["capabilities"] == ["issue_tracking"]
            assert tools["beads"]["install_prompt"] == "Use npm global install for beads."
            assert mcp_servers["gitnexus-mcp"]["distribution"] == "command"
            assert mcp_servers["gitnexus-mcp"]["requirements"]["node"] == ">=20"
            assert mcp_servers["gitnexus-mcp"]["docs"][0]["url"] == "https://example.test/mcp"
            assert mcp_servers["gitnexus-mcp"]["install_prompt"] == "Install MCP through npm only."
            assert skills["collaborating-with-claude"]["scope"] == "user"
            assert (
                skills["collaborating-with-claude"]["path_hint"]
                == "~/.agents/skills/collaborating-with-claude/SKILL.md"
            )
            assert skills["collaborating-with-claude"]["docs"][0]["title"] == "Claude skill"
            assert skills["collaborating-with-claude"]["verify_prompt"] == "Verify SKILL.md exists."
            assert "prompt" not in manifest
        finally:
            service.stop()
            relay.stop()

    def test_environment_run_endpoint_submits_agent_runtime_task(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRuntimeControlPlane(
            runtime_snapshot={
                "runtime_profiles": {
                    "environment_local": {
                        "executor": "fake",
                        "execution_location": "local_workspace",
                    }
                },
                "agents": {
                    "environment_configurator": {
                        "runtime_profile": "environment_local",
                        "capabilities": [
                            "environment.check",
                            "environment.configure",
                        ],
                    }
                },
            }
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
            environment_cli_tools={
                "gitnexus": EnvironmentCLIToolConfig(
                    name="gitnexus",
                    command="gitnexus",
                    check="gitnexus --version",
                    install="npm install -g gitnexus",
                )
            },
        )
        service.start()
        try:
            status, body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/environment/run",
                {
                    "mode": "configure",
                    "workspace_root": "/tmp/peer",
                    "entry_ids": ["cli:gitnexus"],
                },
                headers=TEST_ADMIN_HEADERS,
            )

            assert status == 200
            task = body["task"]
            assert body["ok"] is True
            assert body["agent_id"] == "environment_configurator"
            assert task["trigger_mode"] == "environment_config"
            assert task["metadata"]["workflow"] == "environment_config"
            assert task["metadata"]["environment_mode"] == "configure"
            assert {
                "entry_id": "cli:gitnexus",
                "kind": "cli",
                "name": "gitnexus",
                "phase": "install",
                "command": "npm install -g gitnexus",
            } in task["metadata"]["allowed_commands"]
        finally:
            service.stop()
            relay.stop()

    def test_register_poll_result_disconnect_and_cleanup(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        try:
            bootstrap_token = relay.issue_bootstrap_token(ttl_sec=60)
            status, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": bootstrap_token,
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp",
                    "capabilities": ["shell", "read_file"],
                },
            )
            assert status == 200
            assert register_body["type"] == "register_ok"
            payload = register_body["payload"]
            peer_id = payload["peer_id"]
            peer_token = payload["peer_token"]

            status, heartbeat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/heartbeat",
                {"peer_token": peer_token, "ts": time.time()},
            )
            assert status == 200
            assert heartbeat_body["peer_id"] == peer_id

            status, poll_body = _json_request(
                "POST",
                f"{service.base_url}/remote/poll",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert poll_body["type"] == "noop"

            result_holder: dict[str, object] = {}

            def run_exec() -> None:
                result_holder["result"] = relay.send_exec_request(
                    peer_id,
                    request=__import__(
                        "labrastro_server.interfaces.http.remote.protocol",
                        fromlist=["ExecToolRequest"],
                    ).ExecToolRequest(tool_name="shell", args={"command": "echo hi"}),
                    timeout_sec=2,
                )

            exec_thread = threading.Thread(target=run_exec)
            exec_thread.start()
            time.sleep(0.1)

            status, poll_body = _json_request(
                "POST",
                f"{service.base_url}/remote/poll",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert poll_body["type"] == "exec_tool"
            assert poll_body["payload"]["tool_name"] == "shell"
            req_id = poll_body["request_id"]

            status, result_body = _json_request(
                "POST",
                f"{service.base_url}/remote/result",
                {
                    "peer_token": peer_token,
                    "request_id": req_id,
                    "type": "tool_result",
                    "payload": ExecToolResult(
                        ok=True, result="hello from peer"
                    ).to_dict(),
                },
            )
            assert status == 200
            assert result_body["ok"] is True
            exec_thread.join(timeout=2)
            assert result_holder["result"].result == "hello from peer"

            cleanup_holder: dict[str, object] = {}

            def run_cleanup() -> None:
                cleanup_holder["result"] = relay.request_cleanup(peer_id, timeout_sec=2)

            cleanup_thread = threading.Thread(target=run_cleanup)
            cleanup_thread.start()
            time.sleep(0.1)

            status, poll_body = _json_request(
                "POST",
                f"{service.base_url}/remote/poll",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert poll_body["type"] == "cleanup"
            cleanup_req_id = poll_body["request_id"]

            status, cleanup_body = _json_request(
                "POST",
                f"{service.base_url}/remote/result",
                {
                    "peer_token": peer_token,
                    "request_id": cleanup_req_id,
                    "type": "cleanup_result",
                    "payload": CleanupResult(
                        ok=True, removed_items=["/tmp/rc-peer"]
                    ).to_dict(),
                },
            )
            assert status == 200
            assert cleanup_body["ok"] is True
            cleanup_thread.join(timeout=2)
            assert cleanup_holder["result"].ok is True
            assert cleanup_holder["result"].removed_items == ["/tmp/rc-peer"]

            status, disconnect_body = _json_request(
                "POST",
                f"{service.base_url}/remote/disconnect",
                {"peer_token": peer_token, "reason": "peer_initiated"},
            )
            assert status == 200
            assert disconnect_body["ok"] is True
            assert relay.registry.get(peer_id) is None
        finally:
            service.stop()
            relay.stop()

    def test_all_remote_builtin_tools_dispatch_over_http_contract(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_id = register_body["payload"]["peer_id"]
            peer_token = register_body["payload"]["peer_token"]

            backend = RemoteRelayToolBackend(relay_server=relay)
            backend.context.peer_id = peer_id
            cases = [
                (
                    ShellTool(backend=backend),
                    {"command": "echo hello"},
                    "shell",
                    "shell-ok",
                ),
                (
                    ReadFileTool(backend=backend),
                    {"file_path": "/tmp/demo.txt"},
                    "read_file",
                    "read-ok",
                ),
                (
                    WriteFileTool(backend=backend),
                    {"file_path": "/tmp/demo.txt", "content": "hello"},
                    "write_file",
                    "write-ok",
                ),
                (
                    EditFileTool(backend=backend),
                    {
                        "file_path": "/tmp/demo.txt",
                        "old_string": "a",
                        "new_string": "b",
                    },
                    "edit_file",
                    "edit-ok",
                ),
                (
                    GlobTool(backend=backend),
                    {"pattern": "*.py", "path": "/tmp"},
                    "glob",
                    "glob-ok",
                ),
                (
                    GrepTool(backend=backend),
                    {"pattern": "hello", "path": "/tmp"},
                    "grep",
                    "grep-ok",
                ),
            ]

            for tool, kwargs, expected_name, expected_result in cases:
                holder: dict[str, object] = {}

                def run_tool(current_tool=tool, current_kwargs=kwargs) -> None:
                    holder["result"] = current_tool.execute(**current_kwargs)

                t = threading.Thread(target=run_tool)
                t.start()
                time.sleep(0.1)

                status, poll_body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/poll",
                    {"peer_token": peer_token},
                )
                assert status == 200
                assert poll_body["type"] == "exec_tool"
                assert poll_body["payload"]["tool_name"] == expected_name
                for key, value in kwargs.items():
                    assert poll_body["payload"]["args"][key] == value

                status, result_body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/result",
                    {
                        "peer_token": peer_token,
                        "request_id": poll_body["request_id"],
                        "type": "tool_result",
                        "payload": ExecToolResult(
                            ok=True, result=expected_result
                        ).to_dict(),
                    },
                )
                assert status == 200
                assert result_body["ok"] is True

                t.join(timeout=2)
                assert holder["result"] == expected_result
        finally:
            service.stop()
            relay.stop()

    def test_register_rejected_over_http(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        try:
            req = request.Request(
                f"{service.base_url}/remote/register",
                data=json.dumps(
                    {"bootstrap_token": "bt_invalid", "cwd": "/tmp"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                _URLOPEN(req, timeout=5)
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 403
                body = json.loads(exc.read().decode("utf-8"))
                assert body["ok"] is False
                assert body["error"] == "register_rejected"
                assert body["details"]["reason"]
        finally:
            service.stop()
            relay.stop()

    def test_chat_endpoint_routes_to_host_chat_handler(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            chat_handler=lambda peer_id, prompt: ChatResponse(
                response=f"{peer_id}:{prompt}"
            ),
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_id = register_body["payload"]["peer_id"]
            peer_token = register_body["payload"]["peer_token"]

            status, chat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat",
                {
                    "peer_token": peer_token,
                    "prompt": "hello",
                },
            )
            assert status == 200
            assert chat_body["response"] == f"{peer_id}:hello"
            assert chat_body.get("error") in (None, "")
        finally:
            service.stop()
            relay.stop()

    def test_chat_endpoint_sanitizes_host_handler_exceptions(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def chat_handler(_peer_id: str, _prompt: str) -> ChatResponse:
            raise RuntimeError("secret chat failure")

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            chat_handler=chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            status, chat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat",
                {
                    "peer_token": register_body["payload"]["peer_token"],
                    "prompt": "hello",
                },
            )

            assert status == 200
            assert chat_body["error"] == "chat_handler_failed"
            assert "secret chat failure" not in json.dumps(chat_body)
        finally:
            service.stop()
            relay.stop()

    def test_chat_endpoint_routes_taskflow_to_stream_chat_handler(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        seen: dict[str, str | None] = {}

        def stream_chat_handler(peer_id: str, prompt: str, session) -> None:
            seen["peer_id"] = peer_id
            seen["prompt"] = prompt
            seen["mode"] = session.mode
            seen["workflow_mode"] = session.workflow_mode
            seen["taskflow_id"] = session.taskflow_id
            session.append_event("chat_end", {"response": "taskflow ok"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            status, chat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat",
                {
                    "peer_token": peer_token,
                    "prompt": "turn this into a taskflow",
                    "mode": "taskflow",
                    "workflow_mode": "taskflow",
                    "taskflow_id": "taskflow-1",
                },
            )
            assert status == 200
            assert chat_body["response"] == "taskflow ok"
            assert chat_body.get("error") in (None, "")
            assert seen == {
                "peer_id": register_body["payload"]["peer_id"],
                "prompt": "turn this into a taskflow",
                "mode": "taskflow",
                "workflow_mode": "taskflow",
                "taskflow_id": "taskflow-1",
            }
        finally:
            service.stop()
            relay.stop()

    def test_chat_endpoint_allows_concurrent_requests_across_peers(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def chat_handler(peer_id: str, prompt: str) -> ChatResponse:
            time.sleep(0.3)
            return ChatResponse(response=f"{peer_id}:{prompt}")

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            chat_handler=chat_handler,
        )
        service.start()
        try:
            _, register_a = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/a",
                },
            )
            _, register_b = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/b",
                },
            )

            token_a = register_a["payload"]["peer_token"]
            token_b = register_b["payload"]["peer_token"]
            results: dict[str, dict] = {}

            def run_chat(label: str, token: str) -> None:
                _, body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/chat",
                    {"peer_token": token, "prompt": label},
                )
                results[label] = body

            started = time.time()
            t1 = threading.Thread(target=run_chat, args=("p1", token_a))
            t2 = threading.Thread(target=run_chat, args=("p2", token_b))
            t1.start()
            t2.start()
            t1.join(timeout=3)
            t2.join(timeout=3)
            elapsed = time.time() - started

            assert "p1" in results and "p2" in results
            assert elapsed < 0.55
        finally:
            service.stop()
            relay.stop()

    def test_disconnect_aborts_active_stream_chat_session(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            # Wait long enough so test can force disconnect first.
            session.wait_approval("hold", timeout_sec=2)

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {
                    "peer_token": peer_token,
                    "prompt": "long-run",
                },
            )
            chat_id = start_body["chat_id"]

            status, _ = _json_request(
                "POST",
                f"{service.base_url}/remote/disconnect",
                {"peer_token": peer_token, "reason": "test_disconnect"},
            )
            assert status == 200

            _, stream_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            )
            assert stream_body["done"] is True
            event_types = [event["type"] for event in stream_body["events"]]
            assert "chat_start" in event_types
            assert "error" in event_types
        finally:
            service.stop()
            relay.stop()

    def test_chat_start_preserves_requested_mode_on_stream_session(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        seen: dict[str, str | None] = {}

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            seen["mode"] = session.mode
            session.append_event("chat_end", {"response": "ok"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {
                    "peer_token": peer_token,
                    "prompt": "use planner mode",
                    "mode": "planner",
                },
            )
            chat_id = start_body["chat_id"]

            _, stream_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            )

            assert stream_body["done"] is True
            assert seen["mode"] == "planner"
        finally:
            service.stop()
            relay.stop()

    def test_chat_status_reports_running_done_and_error(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        running_started = threading.Event()
        release_running = threading.Event()

        def stream_chat_handler(_peer_id: str, prompt: str, session) -> None:
            if prompt == "boom":
                session.append_event("error", {"message": "intentional_failure"})
                return
            running_started.set()
            release_running.wait(timeout=2)
            session.append_event("chat_end", {"response": "ok"})

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "hold"},
            )
            chat_id = start_body["chat_id"]
            assert running_started.wait(2)

            _, running_status = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/status",
                {"peer_token": peer_token, "chat_id": chat_id, "cursor": 0},
            )
            assert running_status["ok"] is True
            assert running_status["status"] == "running"
            assert running_status["running"] is True
            assert running_status["done"] is False
            assert running_status["reconnectable"] is True
            assert running_status["latest_seq"] >= 1

            release_running.set()
            _, stream_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": 0,
                    "timeout_sec": 2,
                },
            )
            assert stream_body["done"] is True

            _, done_status = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/status",
                {"peer_token": peer_token, "chat_id": chat_id, "cursor": 0},
            )
            assert done_status["status"] == "done"
            assert done_status["running"] is False
            assert done_status["done"] is True
            assert done_status["reconnectable"] is False

            _, error_start = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "boom"},
            )
            _, error_stream = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": error_start["chat_id"],
                    "cursor": 0,
                    "timeout_sec": 2,
                },
            )
            assert error_stream["done"] is True

            _, error_status = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/status",
                {
                    "peer_token": peer_token,
                    "chat_id": error_start["chat_id"],
                    "cursor": 0,
                },
            )
            assert error_status["status"] == "error"
            assert error_status["done"] is True
            assert error_status["error"] == "intentional_failure"
        finally:
            service.stop()
            relay.stop()

    def test_chat_stream_cursor_resume_reads_events_created_between_polls(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        first_delta_sent = threading.Event()
        release_second_delta = threading.Event()
        finished = threading.Event()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                session.append_event("delta", {"text": "first"})
                first_delta_sent.set()
                release_second_delta.wait(timeout=2)
                session.append_event("delta", {"text": "second"})
                session.append_event("chat_end", {"response": "done"})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "resume"},
            )
            chat_id = start_body["chat_id"]
            assert first_delta_sent.wait(2)

            _, first_poll = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": 0,
                    "timeout_sec": 0,
                },
            )
            assert first_poll["done"] is False
            assert [event["type"] for event in first_poll["events"]] == [
                "chat_start",
                "delta",
            ]
            first_cursor = first_poll["next_cursor"]

            release_second_delta.set()
            assert finished.wait(2)
            _, resumed_poll = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": first_cursor,
                    "timeout_sec": 1,
                },
            )

            assert resumed_poll["done"] is True
            assert [event["type"] for event in resumed_poll["events"]] == [
                "delta",
                "chat_end",
            ]
            assert resumed_poll["events"][0]["payload"]["text"] == "second"
        finally:
            service.stop()
            relay.stop()

    def test_chat_stream_accepts_replacement_peer_token_for_existing_chat(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        finished = threading.Event()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                session.append_event("delta", {"text": "after-reconnect"})
                session.append_event("chat_end", {"response": "done"})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, first_register = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer-a",
                },
            )
            _, replacement_register = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer-b",
                },
            )
            first_token = first_register["payload"]["peer_token"]
            replacement_token = replacement_register["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": first_token, "prompt": "resume from replacement"},
            )
            assert finished.wait(2)

            _, stream_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": replacement_token,
                    "chat_id": start_body["chat_id"],
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            )

            assert stream_body["done"] is True
            assert [event["type"] for event in stream_body["events"]] == [
                "chat_start",
                "delta",
                "chat_end",
            ]
            assert stream_body["events"][1]["payload"]["text"] == "after-reconnect"
        finally:
            service.stop()
            relay.stop()

    def test_chat_cancel_adds_terminal_event_and_marks_done(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            session.wait_approval("hold", timeout_sec=2)

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "cancel me"},
            )
            chat_id = start_body["chat_id"]

            _, cancel_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/cancel",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "reason": "user_cancelled",
                },
            )
            assert cancel_body["ok"] is True

            _, stream_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            )
            assert stream_body["done"] is True
            event_types = [event["type"] for event in stream_body["events"]]
            assert "chat_cancel_requested" in event_types
            assert "chat_cancelled" in event_types
        finally:
            service.stop()
            relay.stop()

    def test_chat_stream_reports_lost_events_when_buffer_pruned(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        finished = threading.Event()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                for idx in range(6):
                    session.append_event("delta", {"idx": idx})
                session.append_event("chat_end", {"response": "done"})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
            chat_max_events=3,
            chat_artifact_root=tmp_path / "chat-events",
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "overflow"},
            )
            assert finished.wait(2)

            _, stream_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": start_body["chat_id"],
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            )

            events = stream_body["events"]
            assert events[0]["type"] == "events_lost"
            assert events[0]["payload"]["first_available_seq"] > 1
            assert events[0]["payload"]["dropped_count"] >= 1
            assert [event["type"] for event in events[1:]] == [
                "delta",
                "delta",
                "chat_end",
            ]
            assert [event["payload"].get("idx") for event in events if event["type"] == "delta"] == [4, 5]
            assert stream_body["next_cursor"] == events[-1]["seq"]
            assert stream_body["done"] is True
        finally:
            service.stop()
            relay.stop()

    def test_chat_stream_spills_oversized_payload_to_gzip_artifact(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        finished = threading.Event()
        large_text = "x" * 512

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                session.append_event("delta", {"text": large_text})
                session.append_event("chat_end", {"response": "done"})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
            chat_max_payload_bytes=128,
            chat_artifact_root=tmp_path / "chat-events",
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "large"},
            )
            assert finished.wait(2)

            _, stream_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": start_body["chat_id"],
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            )

            delta_event = next(
                event for event in stream_body["events"] if event["type"] == "delta"
            )
            artifact = delta_event["payload"]["artifact_ref"]
            artifact_path = Path(artifact["path"])
            assert artifact["encoding"] == "json+gzip"
            assert artifact["bytes"] > 128
            assert artifact_path.exists()
            with gzip.open(artifact_path, "rt", encoding="utf-8") as fh:
                assert json.load(fh) == {"text": large_text}
        finally:
            service.stop()
            relay.stop()

    def test_chat_gc_removes_closed_idle_sessions_and_artifacts(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        finished = threading.Event()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            try:
                session.append_event("delta", {"text": "x" * 512})
            finally:
                finished.set()

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
            chat_max_payload_bytes=128,
            chat_artifact_root=tmp_path / "chat-events",
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "gc"},
            )
            chat_id = start_body["chat_id"]
            assert finished.wait(2)

            _, stream_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            )
            delta_event = next(
                event for event in stream_body["events"] if event["type"] == "delta"
            )
            artifact_path = Path(delta_event["payload"]["artifact_ref"]["path"])
            assert artifact_path.exists()

            session = service._get_chat_session(chat_id)
            assert session is not None
            service._chat_session_ttl_sec = 0
            session.finished_at = time.time() - 1
            service._gc_chat_sessions()

            assert service._get_chat_session(chat_id) is None
            assert not artifact_path.exists()
        finally:
            service.stop()
            relay.stop()

    def test_approval_reply_routes_to_matching_chat_session_only(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            approval_id = "approval-1"
            session.register_approval(approval_id)
            session.append_event(
                "approval_request",
                {
                    "approval_id": approval_id,
                    "tool_name": "shell",
                    "tool_source": "builtin",
                    "reason": "need approval",
                },
            )
            decision, reason = session.wait_approval(approval_id, timeout_sec=2)
            session.append_event(
                "approval_resolved",
                {"approval_id": approval_id, "decision": decision, "reason": reason},
            )

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "approve me"},
            )
            chat_id = start_body["chat_id"]

            _, stream_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            )
            approval_events = [
                event
                for event in stream_body["events"]
                if event["type"] == "approval_request"
            ]
            assert approval_events
            approval_id = approval_events[0]["payload"]["approval_id"]

            status, reply_body = _json_request(
                "POST",
                f"{service.base_url}/remote/approval/reply",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "approval_id": approval_id,
                    "decision": "allow_once",
                    "reason": "ok",
                },
            )
            assert status == 200
            assert reply_body["ok"] is True

            _, resolved_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": stream_body["next_cursor"],
                    "timeout_sec": 1,
                },
            )
            resolved_events = [
                event
                for event in resolved_body["events"]
                if event["type"] == "approval_resolved"
            ]
            assert resolved_events
            assert resolved_events[0]["payload"]["decision"] == "allow_once"
            assert resolved_body["done"] is True

            bad_chat_req = request.Request(
                f"{service.base_url}/remote/approval/reply",
                data=json.dumps(
                    {
                        "peer_token": peer_token,
                        "chat_id": "missing-chat",
                        "approval_id": approval_id,
                        "decision": "allow_once",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                _URLOPEN(bad_chat_req, timeout=5)
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 404
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "chat_not_found"

            bad_approval_req = request.Request(
                f"{service.base_url}/remote/approval/reply",
                data=json.dumps(
                    {
                        "peer_token": peer_token,
                        "chat_id": chat_id,
                        "approval_id": "missing-approval",
                        "decision": "allow_once",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                _URLOPEN(bad_approval_req, timeout=5)
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 404
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "approval_not_found"
        finally:
            service.stop()
            relay.stop()

    def test_session_protocol_models_roundtrip(self) -> None:
        assert ChatStartRequest.from_dict(
            {
                "peer_token": "peer-token",
                "prompt": "hello",
                "session_hint": "session-1",
                "mode": "planner",
            }
        ).to_dict() == {
            "peer_token": "peer-token",
            "prompt": "hello",
            "session_hint": "session-1",
            "mode": "planner",
        }
        assert ChatStartRequest.from_dict(
            {
                "peer_token": "peer-token",
                "prompt": "hello",
                "mode": "taskflow",
                "workflow_mode": "taskflow",
                "taskflow_id": "taskflow-1",
            }
        ).to_dict() == {
            "peer_token": "peer-token",
            "prompt": "hello",
            "session_hint": None,
            "mode": "taskflow",
            "workflow_mode": "taskflow",
            "taskflow_id": "taskflow-1",
        }
        assert SessionListRequest.from_dict(
            {"peer_token": "peer-token", "limit": 5, "if_list_etag": "etag-1"}
        ).to_dict() == {
            "peer_token": "peer-token",
            "limit": 5,
            "if_list_etag": "etag-1",
        }
        assert SessionLoadRequest.from_dict(
            {"peer_token": "peer-token", "session_id": "session-1"}
        ).to_dict() == {"peer_token": "peer-token", "session_id": "session-1"}
        assert SessionNewRequest.from_dict(
            {"peer_token": "peer-token"}
        ).to_dict() == {"peer_token": "peer-token"}
        assert SessionDeleteRequest.from_dict(
            {"peer_token": "peer-token", "session_id": "session-1"}
        ).to_dict() == {"peer_token": "peer-token", "session_id": "session-1"}
        assert SessionForkRequest.from_dict(
            {
                "peer_token": "peer-token",
                "source_session_id": "session-1",
                "keep_through_message_index": 3,
                "snapshot": {"version": 1},
            }
        ).to_dict() == {
            "peer_token": "peer-token",
            "source_session_id": "session-1",
            "keep_through_message_index": 3,
            "snapshot": {"version": 1},
        }
        assert SessionSnapshotRequest.from_dict(
            {
                "peer_token": "peer-token",
                "session_id": "session-1",
                "snapshot": {"version": 1},
                "snapshot_digest": "digest-1",
            }
        ).to_dict() == {
            "peer_token": "peer-token",
            "session_id": "session-1",
            "snapshot": {"version": 1},
            "snapshot_digest": "digest-1",
        }

    def test_sessions_routes_verify_peer_token_and_dispatch(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        calls: list[tuple[str, str, dict]] = []

        def session_handler(action: str, peer_id: str, payload: dict) -> dict:
            calls.append((action, peer_id, payload))
            if action == "load" and payload.get("session_id") == "missing":
                return {"ok": False, "error": "session_not_found", "_status": 404}
            if action == "new":
                raise RuntimeError("secret session failure")
            return {"ok": True, "action": action}

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_handler=session_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            status, list_body = _json_request(
                "POST",
                f"{service.base_url}/remote/sessions/list",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert list_body["ok"] is True
            assert list_body["action"] == "list"
            assert calls[-1][0] == "list"

            status, tagged_list_body = _json_request(
                "POST",
                f"{service.base_url}/remote/sessions/list",
                {"peer_token": peer_token, "if_list_etag": "etag-1"},
            )
            assert status == 200
            assert tagged_list_body["ok"] is True
            assert calls[-1][0] == "list"
            assert calls[-1][2]["if_list_etag"] == "etag-1"

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/sessions/list",
                    {"peer_token": "bad-token"},
                )
                raise AssertionError("expected invalid token to fail")
            except HTTPError as exc:
                assert exc.code == 401

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/sessions/load",
                    {"peer_token": peer_token, "session_id": "missing"},
                )
                raise AssertionError("expected missing session to fail")
            except HTTPError as exc:
                assert exc.code == 404

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/sessions/new",
                    {"peer_token": peer_token},
                )
                raise AssertionError("expected session handler error to fail")
            except HTTPError as exc:
                body = json.loads(exc.read().decode("utf-8"))
                assert exc.code == 500
                assert body["error"] == "session_request_failed"
                assert "secret session failure" not in body["message"]

            status, delete_body = _json_request(
                "POST",
                f"{service.base_url}/remote/sessions/delete",
                {"peer_token": peer_token, "session_id": "session-ok"},
            )
            assert status == 200
            assert delete_body["ok"] is True
            assert delete_body["action"] == "delete"
            assert calls[-1][0] == "delete"

            status, fork_body = _json_request(
                "POST",
                f"{service.base_url}/remote/sessions/fork",
                {
                    "peer_token": peer_token,
                    "source_session_id": "session-ok",
                    "keep_through_message_index": 1,
                },
            )
            assert status == 200
            assert fork_body["ok"] is True
            assert fork_body["action"] == "fork"
            assert calls[-1][0] == "fork"
        finally:
            service.stop()
            relay.stop()

    def test_json_responses_support_gzip_when_requested(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_handler(action: str, peer_id: str, payload: dict) -> dict:
            del peer_id, payload
            return {"ok": True, "action": action, "blob": "x" * 2048}

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_handler=session_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            status, headers, body = _raw_request(
                "POST",
                f"{service.base_url}/remote/sessions/list",
                {"peer_token": peer_token},
                headers={"Accept-Encoding": "gzip"},
            )
            assert status == 200
            assert headers["Content-Encoding"] == "gzip"
            decoded = json.loads(gzip.decompress(body).decode("utf-8"))
            assert decoded["blob"] == "x" * 2048

            status, headers, body = _raw_request(
                "POST",
                f"{service.base_url}/remote/sessions/list",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert "Content-Encoding" not in headers
            assert json.loads(body.decode("utf-8"))["blob"] == "x" * 2048
        finally:
            service.stop()
            relay.stop()

    def test_capabilities_report_available_backend_surfaces(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def session_handler(action: str, peer_id: str, payload: dict) -> dict:
            return {"ok": True, "action": action, "peer_id": peer_id}

        def stream_handler(peer_id: str, prompt: str, session) -> None:
            del peer_id, prompt, session

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            session_handler=session_handler,
            stream_chat_handler=stream_handler,
        )
        service.start()
        try:
            status, body = _json_request(
                "GET", f"{service.base_url}/remote/capabilities"
            )
            assert status == 200
            assert body["ok"] is True
            assert body["api_version"] == 1
            assert isinstance(body["server_version"], str)
            assert body["capabilities"]["sessions"] is True
            assert body["capabilities"]["session_auto_save"] is True
            assert body["capabilities"]["session_history_writable"] is True
            assert body["capabilities"]["chat_stream"] is True
            assert body["capabilities"]["taskflow"] is True
            assert body["capabilities"]["issue_assignment"] is True
            assert body["capabilities"]["fresh_session_without_session_hint"] is True
            assert body["capabilities"]["peer_token_heartbeat_refresh"] is True
            assert body["capabilities"]["agent_runtime"] == {
                "executor_capabilities": {}
            }
        finally:
            service.stop()
            relay.stop()

    def test_capabilities_report_missing_optional_handlers(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
        )
        service.start()
        try:
            _, body = _json_request("GET", f"{service.base_url}/remote/capabilities")
            assert body["capabilities"]["sessions"] is False
            assert body["capabilities"]["session_auto_save"] is True
            assert body["capabilities"]["session_history_writable"] is False
            assert body["capabilities"]["chat_stream"] is False
            assert body["capabilities"]["fresh_session_without_session_hint"] is False
            assert body["capabilities"]["peer_token_heartbeat_refresh"] is True
            assert body["capabilities"]["agent_runtime"]["executor_capabilities"] == {}
        finally:
            service.stop()
            relay.stop()

    def test_capabilities_include_peer_executor_capabilities(self) -> None:
        relay = RelayServer()
        relay.registry.register(
            meta={
                "host_info_min": {
                    "agent_runtime": {
                        "executor_capabilities": {
                            "claude": {
                                "installed": True,
                                "version": "2.0.0",
                                "stream_json": True,
                                "resume_by_id": True,
                                "limitations": [],
                            },
                            "gemini": {
                                "installed": False,
                                "limitations": ["executable not found on PATH"],
                            },
                        }
                    }
                }
            }
        )
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        try:
            _, body = _json_request("GET", f"{service.base_url}/remote/capabilities")
            executor_capabilities = body["capabilities"]["agent_runtime"][
                "executor_capabilities"
            ]
            assert executor_capabilities["claude"]["resume_by_id"] is True
            assert executor_capabilities["gemini"]["installed"] is False
        finally:
            service.stop()
            relay.stop()

    def test_http_heartbeat_refreshes_peer_token(self) -> None:
        relay = RelayServer(peer_token_ttl_sec=300)
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            before = relay.token_manager._peers[peer_token].expires_at
            time.sleep(0.01)

            status, heartbeat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/heartbeat",
                {"peer_token": peer_token},
            )

            assert status == 200
            assert heartbeat_body["ok"] is True
            assert relay.token_manager._peers[peer_token].expires_at > before
        finally:
            service.stop()
            relay.stop()

    def test_runtime_heartbeat_and_admin_cancel_roundtrip(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRuntimeControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "G:/repo/main",
                    "workspace_root": "G:/repo/main",
                    "capabilities": [
                        "agent_runtime",
                        "agent_runtime.local_workspace",
                    ],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            admin_headers = TEST_ADMIN_HEADERS

            _, submit_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/runtime/submit",
                {
                    "task_id": "task-http-runtime",
                    "issue_id": "issue-1",
                    "agent_id": "coder",
                    "prompt": "run fake",
                    "executor": "fake",
                    "execution_location": "local_workspace",
                    "workspace_root": "G:/repo/main",
                },
                headers=admin_headers,
            )
            assert submit_body["ok"] is True

            _, claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/runtime/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "worker-1",
                    "executors": ["fake"],
                },
            )
            claim = claim_body["claim"]
            assert claim is not None
            assert claim["task"]["id"] == "task-http-runtime"

            _, heartbeat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/runtime/heartbeat",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "task_id": "task-http-runtime",
                    "worker_id": "worker-1",
                },
            )
            assert heartbeat_body["ok"] is True
            assert heartbeat_body["cancel_requested"] is False

            _, session_body = _json_request(
                "POST",
                f"{service.base_url}/remote/runtime/session",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "task_id": "task-http-runtime",
                    "worker_id": "worker-1",
                    "workdir": "G:/repo/main/.rcoder/agent-runtime/ws/task/workdir/repo",
                    "branch": "agent/coder/task-http",
                    "repo_url": "file:///repo/main",
                    "cache_path": "G:/repo/main/.rcoder/agent-runtime/repos/ws/repo.git",
                },
            )
            assert session_body["ok"] is True

            _, event_body = _json_request(
                "POST",
                f"{service.base_url}/remote/runtime/event",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "task_id": "task-http-runtime",
                    "worker_id": "worker-1",
                    "type": "text",
                    "text": "hello",
                },
            )
            assert event_body["ok"] is True

            _, admin_events = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/runtime/events",
                {"task_id": "task-http-runtime", "after_seq": 0, "limit": 2},
                headers=admin_headers,
            )
            assert len(admin_events["events"]) == 2
            assert admin_events["next_seq"] == admin_events["events"][-1]["seq"]
            assert admin_events["has_more"] is True

            _, peer_events = _json_request(
                "GET",
                f"{service.base_url}/remote/agent-runtime/tasks/task-http-runtime/events?peer_token={peer_token}&after_seq=0&limit=2",
            )
            assert len(peer_events["events"]) == 2
            assert peer_events["next_seq"] == peer_events["events"][-1]["seq"]
            assert peer_events["has_more"] is True

            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/runtime/event",
                    {
                        "peer_token": peer_token,
                        "request_id": claim["request_id"],
                        "task_id": "task-http-runtime",
                        "worker_id": "other-worker",
                        "type": "text",
                        "text": "bad",
                    },
                )
                raise AssertionError("non-owner runtime event should be rejected")
            except HTTPError as exc:
                assert exc.code == 403

            _, cancel_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/runtime/cancel",
                {"task_id": "task-http-runtime", "reason": "user_stop"},
                headers=admin_headers,
            )
            assert cancel_body == {"ok": True, "task_id": "task-http-runtime"}

            _, cancelled_heartbeat = _json_request(
                "POST",
                f"{service.base_url}/remote/runtime/heartbeat",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "task_id": "task-http-runtime",
                    "worker_id": "worker-1",
                },
            )
            assert cancelled_heartbeat["ok"] is True
            assert cancelled_heartbeat["cancel_requested"] is True
            assert cancelled_heartbeat["reason"] == "user_stop"

            _, complete_body = _json_request(
                "POST",
                f"{service.base_url}/remote/runtime/complete",
                {
                    "peer_token": peer_token,
                    "request_id": claim["request_id"],
                    "task_id": "task-http-runtime",
                    "worker_id": "worker-1",
                    "status": "cancelled",
                    "output": "",
                    "error": "execution cancelled",
                    "events": [
                        {
                            "request_id": claim["request_id"],
                            "task_id": "task-http-runtime",
                            "worker_id": "worker-1",
                            "type": "status",
                            "data": {"status": "cancelled"},
                        }
                    ],
                },
            )
            assert complete_body["ok"] is True

            _, retry_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/runtime/retry",
                {
                    "task_id": "task-http-runtime",
                    "new_task_id": "task-http-runtime-retry",
                },
                headers=admin_headers,
            )
            assert retry_body["ok"] is True
            assert retry_body["task"]["id"] == "task-http-runtime-retry"
            assert retry_body["task"]["status"] == "queued"
            assert retry_body["task"]["metadata"]["retry_of"] == "task-http-runtime"
        finally:
            service.stop()
            relay.stop()

    def test_taskflow_http_api_uses_taskflow_and_work_item_resources(self) -> None:
        route_source = Path("labrastro_server/interfaces/http/remote/routes/taskflow.py").read_text(encoding="utf-8")
        assert "/remote/taskflow/goals" not in route_source
        assert "task-drafts" not in route_source
        assert "taskflows" in route_source
        assert "work-items" in route_source
    def test_issue_assignment_and_mention_http_api_reuses_taskflow_dispatch(
        self,
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRuntimeControlPlane(
            runtime_snapshot={
                "runtime_profiles": {
                    "docs_profile": {
                        "executor": "fake",
                        "execution_location": "remote_server",
                    }
                },
                "agents": {
                    "docs": {
                        "name": "Docs Agent",
                        "aliases": ["writer"],
                        "runtime_profile": "docs_profile",
                        "capabilities": ["docs", "research"],
                    }
                },
            }
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "G:/repo/main",
                    "workspace_root": "G:/repo/main",
                    "capabilities": ["agent_runtime"],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            _, issue_body = _json_request(
                "POST",
                f"{service.base_url}/remote/issues",
                {
                    "peer_token": peer_token,
                    "title": "Docs issue",
                    "description": "Write docs.",
                },
            )
            issue_id = issue_body["issue"]["id"]

            _, assignment_body = _json_request(
                "POST",
                f"{service.base_url}/remote/issues/{issue_id}/assignments",
                {
                    "peer_token": peer_token,
                    "target_agent_id": "docs",
                    "required_capabilities": ["docs"],
                    "preferred_capabilities": ["research"],
                    "task_type": "docs",
                },
            )
            assignment = assignment_body["assignment"]
            assert assignment["status"] == "ready"
            assert control.list_tasks() == []

            _, reassigned_body = _json_request(
                "POST",
                f"{service.base_url}/remote/assignments/{assignment['id']}/assign",
                {
                    "peer_token": peer_token,
                    "agent_id": "docs",
                    "reason": "manual confirmation",
                },
            )
            assert reassigned_body["assignment"]["target_agent_id"] == "docs"

            _, parse_body = _json_request(
                "POST",
                f"{service.base_url}/remote/mentions/parse",
                {"peer_token": peer_token, "raw_text": "@writer please help"},
            )
            assert parse_body["mention"]["resolved_agent_id"] == "docs"

            _, mention_body = _json_request(
                "POST",
                f"{service.base_url}/remote/mentions",
                {
                    "peer_token": peer_token,
                    "issue_id": issue_id,
                    "raw_text": "@writer please draft this.",
                    "prompt": "Draft this.",
                },
            )
            assert mention_body["mention"]["status"] == "ready"
            assert mention_body["mention"]["assignment_id"]
            assert control.list_tasks() == []

            _, dispatch_body = _json_request(
                "POST",
                f"{service.base_url}/remote/assignments/{assignment['id']}/dispatch",
                {"peer_token": peer_token},
            )
            dispatched = dispatch_body["assignment"]
            assert dispatched["status"] == "dispatched"
            task = control.get_task(dispatched["runtime_task_id"])
            assert task.metadata["dispatch_source"] == "assignment"
            assert task.metadata["issue_id"] == issue_id

            _, issue_detail = _json_request(
                "GET",
                f"{service.base_url}/remote/issues/{issue_id}?peer_token={peer_token}",
            )
            assert issue_detail["assignments"][0]["id"] == assignment["id"]
            assert issue_detail["taskflow"]["outputs"]["task_run_refs"]

            _, events_body = _json_request(
                "GET",
                f"{service.base_url}/remote/issues/{issue_id}/events?peer_token={peer_token}&after_seq=0",
            )
            assert {event["type"] for event in events_body["events"]} >= {
                "issue_created",
                "assignment_dispatched",
                "mention_created",
            }
        finally:
            service.stop()
            relay.stop()

    def test_server_settings_update_refreshes_runtime_snapshot_for_agent_submit(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.host.yaml"
        save_yaml_config(
            config_path,
            {"agent_runtime": {"max_running_agents": 1, "max_shells_per_agent": 1}},
        )
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRuntimeControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            runtime_control_plane=control,
        )

        def reload_runtime_config() -> None:
            data = load_yaml_config(config_path)
            runtime = AgentRuntimeConfig.from_dict(data.get("agent_runtime", {}))
            control.configure(
                max_running_tasks=runtime.max_running_agents,
                runtime_snapshot=runtime.to_runtime_snapshot(),
            )

        service.admin_manager.reload_handler = reload_runtime_config
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/repo",
                    "workspace_root": "/tmp/repo",
                    "capabilities": [
                        "agent_runtime",
                        "agent_runtime.daemon_worktree",
                    ],
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            admin_headers = TEST_ADMIN_HEADERS

            _, update_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/server-settings/update",
                {
                    "agent_runtime": {
                        "max_running_agents": 4,
                        "max_shells_per_agent": 1,
                        "runtime_profiles": {
                            "smoke_fake_profile": {
                                "executor": "fake",
                                "execution_location": "daemon_worktree",
                                "credential_refs": {"model": "smoke_model_ref"},
                            }
                        },
                        "agents": {
                            "smoke_reviewer": {
                                "name": "Smoke Reviewer",
                                "runtime_profile": "smoke_fake_profile",
                                "capabilities": ["read_repo", "code_review"],
                                "prompt": {
                                    "system_append": (
                                        "You are the smoke reviewer agent."
                                    )
                                },
                            }
                        },
                    }
                },
                headers=admin_headers,
            )
            assert update_body["ok"] is True
            assert control.max_running_tasks == 4

            _, submit_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/runtime/submit",
                {
                    "task_id": "task-agent-only",
                    "issue_id": "issue-1",
                    "agent_id": "smoke_reviewer",
                    "prompt": "run smoke",
                },
                headers=admin_headers,
            )
            assert submit_body["ok"] is True
            assert submit_body["task"]["executor"] == "fake"
            assert submit_body["task"]["execution_location"] == "daemon_worktree"
            assert submit_body["task"]["runtime_profile_id"] == "smoke_fake_profile"

            _, claim_body = _json_request(
                "POST",
                f"{service.base_url}/remote/runtime/claim",
                {
                    "peer_token": peer_token,
                    "worker_id": "worker-1",
                    "executors": ["fake"],
                },
            )
            claim = claim_body["claim"]
            assert claim is not None
            assert claim["executor_request"]["executor"] == "fake"
            assert (
                claim["executor_request"]["runtime_profile_id"]
                == "smoke_fake_profile"
            )
            prompt_files = claim["executor_request"]["metadata"]["prompt_files"]
            assert "AGENT_RUNTIME.md" in prompt_files
            assert "Smoke Reviewer" in prompt_files["AGENT_RUNTIME.md"]
            assert (
                "You are the smoke reviewer agent."
                in prompt_files["AGENT_RUNTIME.md"]
            )
            assert (
                claim["runtime_snapshot"]["runtime_profiles"]["smoke_fake_profile"][
                    "credential_refs"
                ]["model"]
                == "smoke_model_ref"
            )
        finally:
            service.stop()
            relay.stop()

    def test_server_settings_update_replace_removes_runtime_profiles_and_agents(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.host.yaml"
        save_yaml_config(
            config_path,
            {
                "agent_runtime": {
                    "max_running_agents": 2,
                    "max_shells_per_agent": 1,
                    "runtime_profiles": {
                        "old_profile": {
                            "executor": "fake",
                            "execution_location": "daemon_worktree",
                        }
                    },
                    "agents": {"old_agent": {"runtime_profile": "old_profile"}},
                }
            },
        )
        relay = RelayServer()
        relay.start()
        port = _free_port()
        runtime = AgentRuntimeConfig.from_dict(
            load_yaml_config(config_path).get("agent_runtime", {})
        )
        control = AgentRuntimeControlPlane(
            max_running_tasks=runtime.max_running_agents,
            runtime_snapshot=runtime.to_runtime_snapshot(),
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            admin_config_path=config_path,
            runtime_control_plane=control,
        )

        def reload_runtime_config() -> None:
            data = load_yaml_config(config_path)
            runtime = AgentRuntimeConfig.from_dict(data.get("agent_runtime", {}))
            control.configure(
                max_running_tasks=runtime.max_running_agents,
                runtime_snapshot=runtime.to_runtime_snapshot(),
            )

        service.admin_manager.reload_handler = reload_runtime_config
        service.start()
        try:
            _, update_body = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/server-settings/update",
                {
                    "agent_runtime_update_mode": "replace",
                    "agent_runtime": {
                        "max_running_agents": 3,
                        "runtime_profiles": {},
                        "agents": {},
                    },
                },
                headers=TEST_ADMIN_HEADERS,
            )

            assert update_body["ok"] is True
            runtime_settings = update_body["settings"]["agent_runtime"]
            assert runtime_settings["max_running_agents"] == 3
            assert runtime_settings["max_shells_per_agent"] == 1
            assert set(runtime_settings["runtime_profiles"]) == {"environment_local"}
            assert set(runtime_settings["agents"]) == {"environment_configurator"}
            assert control.max_running_tasks == 3
            assert set(control.runtime_snapshot["runtime_profiles"]) == {
                "environment_local"
            }
            assert set(control.runtime_snapshot["agents"]) == {"environment_configurator"}
        finally:
            service.stop()
            relay.stop()

    def test_admin_runtime_submit_rejects_missing_agent_profile(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRuntimeControlPlane(
            runtime_snapshot={
                "agents": {"smoke_reviewer": {"runtime_profile": "missing_profile"}}
            }
        )
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        try:
            try:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/admin/runtime/submit",
                    {
                        "task_id": "task-missing-profile",
                        "issue_id": "issue-1",
                        "agent_id": "smoke_reviewer",
                        "prompt": "run smoke",
                    },
                    headers=TEST_ADMIN_HEADERS,
                )
            except HTTPError as exc:
                body = json.loads(exc.read().decode("utf-8"))
                assert exc.code == 400
                assert body["error"] == "invalid_runtime_task"
                assert "missing_profile" not in body["message"]
            else:
                raise AssertionError("submit should reject missing runtime profile")
        finally:
            service.stop()
            relay.stop()

    def test_default_artifact_provider_prefers_prebuilt_binary(
        self, tmp_path: Path
    ) -> None:
        provider = _default_create_remote_artifact_provider(UIEventBus())
        artifact_root = getattr(provider, "_artifact_root")
        prebuilt_path = artifact_root / "linux" / "amd64" / "rcoder-peer"
        prebuilt_path.parent.mkdir(parents=True, exist_ok=True)
        prebuilt_path.write_bytes(b"prebuilt-peer")
        try:
            with patch(
                "reuleauxcoder.interfaces.entrypoint.dependencies.subprocess.run"
            ) as mock_run:
                content, content_type = provider("linux", "amd64", "rcoder-peer") or (
                    None,
                    None,
                )
            assert content == b"prebuilt-peer"
            assert content_type == "application/octet-stream"
            mock_run.assert_not_called()
        finally:
            _cleanup_provider_build_dir(provider)
            prebuilt_path.unlink(missing_ok=True)
            for parent in [
                prebuilt_path.parent,
                prebuilt_path.parent.parent,
                artifact_root,
            ]:
                try:
                    parent.rmdir()
                except OSError:
                    pass

    def test_default_artifact_provider_raises_without_prebuilt_or_go(self) -> None:
        provider = _default_create_remote_artifact_provider(UIEventBus())
        try:
            with patch(
                "reuleauxcoder.interfaces.entrypoint.dependencies.shutil.which",
                return_value=None,
            ):
                with pytest.raises(RuntimeError, match="no prebuilt binary found"):
                    provider("linux", "amd64", "rcoder-peer")
        finally:
            _cleanup_provider_build_dir(provider)

    @pytest.mark.skipif(not _GO_AVAILABLE, reason="go toolchain is not installed")
    def test_default_artifact_provider_builds_real_agent_binary(self) -> None:
        provider = _default_create_remote_artifact_provider(UIEventBus())
        try:
            content, content_type = provider("linux", "amd64", "rcoder-peer") or (
                None,
                None,
            )
            assert content_type == "application/octet-stream"
            assert isinstance(content, bytes)
            assert len(content) > 0
        finally:
            _cleanup_provider_build_dir(provider)

    def test_artifact_endpoint_returns_clear_error_when_unavailable(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            artifact_provider=lambda _os_name, _arch, _name: (_ for _ in ()).throw(
                RuntimeError(
                    "peer artifact unavailable: no prebuilt binary found and local 'go' toolchain is not installed"
                )
            ),
        )
        service.start()
        try:
            try:
                _URLOPEN(
                    f"{service.base_url}/remote/artifacts/linux/amd64/rcoder-peer",
                    timeout=5,
                )
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 404
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "artifact_unavailable"
                assert "no prebuilt binary found" in body["message"]
        finally:
            service.stop()
            relay.stop()

    @pytest.mark.skipif(not _GO_AVAILABLE, reason="go toolchain is not installed")
    def test_go_agent_runtime_fake_daemon_worktree_end_to_end(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        control = AgentRuntimeControlPlane()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            runtime_control_plane=control,
        )
        service.start()
        agent_binary = _build_go_agent_binary()
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, timeout=30)
        subprocess.run(["git", "checkout", "-B", "main"], cwd=repo, check=True, timeout=30)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=repo,
            check=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=repo,
            check=True,
            timeout=30,
        )
        (repo / "tracked.txt").write_text("initial\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, timeout=30)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, timeout=30)
        agent_env, gh_log = _fake_gh_env(tmp_path)
        proc = subprocess.Popen(
            [
                str(agent_binary),
                "--host",
                service.base_url,
                "--bootstrap-token",
                relay.issue_bootstrap_token(ttl_sec=60),
                "--cwd",
                str(repo),
                "--workspace-root",
                str(repo),
                "--poll-interval",
                "100ms",
                "--agent-runtime",
                "--runtime-worker-id",
                "worker-runtime-1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=agent_env,
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not relay.registry.list_online():
                time.sleep(0.1)
            assert relay.registry.list_online()

            admin_headers = TEST_ADMIN_HEADERS
            _, submit = _json_request(
                "POST",
                f"{service.base_url}/remote/admin/runtime/submit",
                {
                    "task_id": "task-go-runtime-worktree",
                    "issue_id": "issue-1",
                    "agent_id": "coder",
                    "prompt": "hello from fake runtime",
                    "executor": "fake",
                    "execution_location": "daemon_worktree",
                    "metadata": {
                        "repo_url": repo.resolve().as_uri(),
                        "workspace_id": "test-workspace",
                        "prompt_files": {
                            "AGENTS.md": "Use project conventions.\n",
                        },
                        "fake_files": {
                            "agent-output.txt": "created by fake executor\n",
                        },
                        "pr_body": "body",
                    },
                },
                headers=admin_headers,
            )
            assert submit["ok"] is True

            deadline = time.time() + 25
            task = control.get_task("task-go-runtime-worktree")
            while time.time() < deadline and not task.is_terminal:
                time.sleep(0.2)
                task = control.get_task("task-go-runtime-worktree")

            assert task.status.value == "completed"
            assert task.output == "hello from fake runtime"
            assert task.workdir is not None
            assert task.branch_name is not None
            workdir = Path(task.workdir)
            assert (workdir / "tracked.txt").exists()
            assert (workdir / "agent-output.txt").read_text() == "created by fake executor\n"
            assert (workdir / "AGENTS.md").read_text() == "Use project conventions.\n"
            artifacts = control.artifacts_to_dict("task-go-runtime-worktree")
            artifact_types = {artifact["type"]: artifact for artifact in artifacts}
            assert artifact_types["branch"]["status"] == "pushed"
            assert "pull_request" not in artifact_types
            assert task.pr_url is None
            pushed = subprocess.run(
                ["git", "ls-remote", "--heads", "origin", task.branch_name],
                cwd=workdir,
                check=True,
                timeout=30,
                capture_output=True,
                text=True,
            )
            assert task.branch_name in pushed.stdout
            assert not gh_log.exists() or gh_log.read_text(encoding="utf-8") == ""
            events = control.list_events("task-go-runtime-worktree")
            assert any(event.type == "session_pinned" for event in events)
            assert any(
                event.type == "status"
                and event.payload.get("data", {}).get("status") == "worktree_ready"
                for event in events
            )
            assert any(
                event.type == "text" and event.payload.get("text") == "hello from fake runtime"
                for event in events
            )
            assert any(
                event.type == "status"
                and event.payload.get("data", {}).get("status") == "branch_pushed"
                for event in events
            )
            assert not any(
                event.type == "status"
                and event.payload.get("data", {}).get("status") == "pr_created"
                for event in events
            )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            service.stop()
            relay.stop()

    @pytest.mark.skipif(not _GO_AVAILABLE, reason="go toolchain is not installed")
    def test_go_agent_end_to_end_with_http_host(self, tmp_path: Path) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        agent_binary = _build_go_agent_binary()
        work_dir = tmp_path / "peer-work"
        work_dir.mkdir()
        target_file = work_dir / "demo.txt"
        target_file.write_text("hello world\n")
        proc = subprocess.Popen(
            [
                str(agent_binary),
                "--host",
                service.base_url,
                "--bootstrap-token",
                relay.issue_bootstrap_token(ttl_sec=60),
                "--cwd",
                str(work_dir),
                "--workspace-root",
                str(work_dir),
                "--poll-interval",
                "100ms",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 10
            peer_id = None
            while time.time() < deadline:
                online = relay.registry.list_online()
                if online:
                    peer_id = online[0].peer_id
                    break
                time.sleep(0.1)
            assert peer_id is not None

            backend = RemoteRelayToolBackend(relay_server=relay)
            backend.context.peer_id = peer_id

            shell_result = ShellTool(backend=backend).execute(
                command="printf 'hi-from-agent'"
            )
            assert "hi-from-agent" in shell_result

            read_result = ReadFileTool(backend=backend).execute(
                file_path=str(target_file)
            )
            assert "1\thello world" in read_result

            write_result = WriteFileTool(backend=backend).execute(
                file_path=str(target_file),
                content="alpha\nbeta\n",
            )
            assert "Wrote" in write_result
            assert target_file.read_text() == "alpha\nbeta\n"

            edit_result = EditFileTool(backend=backend).execute(
                file_path=str(target_file),
                old_string="beta",
                new_string="gamma",
            )
            assert "--- a/" in edit_result
            assert "+++ b/" in edit_result
            assert "-beta" in edit_result
            assert "+gamma" in edit_result
            assert target_file.read_text() == "alpha\ngamma\n"

            glob_result = GlobTool(backend=backend).execute(
                pattern="*.txt", path=str(work_dir)
            )
            assert str(target_file) in glob_result

            grep_result = GrepTool(backend=backend).execute(
                pattern="gamma", path=str(work_dir)
            )
            assert str(target_file) in grep_result
            assert "gamma" in grep_result
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            service.stop()
            relay.stop()

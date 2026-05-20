"""Tests for runner integration with remote execution."""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib import request
from urllib.error import HTTPError


_URLOPEN = request.build_opener(request.ProxyHandler({})).open

from reuleauxcoder.domain.agent.events import AgentEvent, ToolFailureKind
from reuleauxcoder.domain.config.models import (
    AgentRegistryConfig,
    AuthConfig,
    AuthSuperadminConfig,
    Config,
    ContextConfig,
    MCPServerConfig,
    ModelProfileConfig,
    ModeConfig,
    PromptConfig,
    ProviderConfig,
    ProvidersConfig,
    RemoteExecConfig,
    RunLimitsConfig,
    RuntimeProfilesConfig,
)
from reuleauxcoder.domain.session.models import Session, SessionRuntimeState
from reuleauxcoder.domain.session.document import (
    apply_session_event,
    update_session_document_metadata,
)

TEST_AUTH_PASSWORD = "admin-password"
from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.llm.models import LLMResponse, ToolCall
from labrastro_server.adapters.reuleauxcoder.remote_backend import RemoteRelayToolBackend
from labrastro_server.interfaces.http.remote.service import RemoteRelayHTTPService
from labrastro_server.interfaces.http.remote.protocol import (
    ToolPreviewResult,
    ToolStreamChunk,
)
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.entrypoint.runner import (
    AppDependencies,
    AppOptions,
    AppRunner,
)
from reuleauxcoder.services.config.loader import ConfigValidationError
from reuleauxcoder.interfaces.entrypoint.remote_relay import (
    _chat_locale_prompt_append,
    _runtime_config_with_chat_locale,
    _structured_ui_event_payload,
    _structured_ui_event_type,
    switch_session_model,
)
from reuleauxcoder.interfaces.events import UIEvent, UIEventKind
from reuleauxcoder.services.llm.factory import resolve_model_runtime


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_chat_locale_prompt_append_maps_supported_locales() -> None:
    assert _chat_locale_prompt_append("zh-CN") == (
        "Language: Use Simplified Chinese for user-visible assistant replies. "
        "Keep code, commands, paths, API names, and quoted errors unchanged."
    )
    assert _chat_locale_prompt_append("zh-Hans").startswith(
        "Language: Use Simplified Chinese"
    )
    assert _chat_locale_prompt_append("en") == (
        "Language: Use English for user-visible assistant replies. "
        "Keep code, commands, paths, API names, and quoted errors unchanged."
    )
    assert _chat_locale_prompt_append("ja").startswith("Language: Use English")
    assert _chat_locale_prompt_append(None) == ""


def test_runtime_config_with_chat_locale_merges_prompt_without_mutating_source() -> None:
    config = Config(prompt=PromptConfig(system_append="Use repo conventions."))

    localized = _runtime_config_with_chat_locale(config, "zh-CN")

    assert localized is not config
    assert localized is not None
    assert config.prompt.system_append == "Use repo conventions."
    assert localized.prompt.system_append == (
        "Use repo conventions.\n\n"
        "Language: Use Simplified Chinese for user-visible assistant replies. "
        "Keep code, commands, paths, API names, and quoted errors unchanged."
    )


def _json_request(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    data = None
    headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body) if body else {}


def _test_auth_config(store_dir: Path | None = None) -> AuthConfig:
    if store_dir is None:
        store_dir = Path(tempfile.mkdtemp(prefix="labrastro-auth-"))
    return AuthConfig(
        enabled=True,
        token_secret="test-token-secret",
        password_hash_iterations=1000,
        store_path=str(store_dir / "auth.json"),
        superadmins=[
            AuthSuperadminConfig(
                username="admin",
                password=TEST_AUTH_PASSWORD,
            )
        ],
    )


def _login_admin(base_url: str) -> str:
    _, body = _json_request(
        "POST",
        f"{base_url}/remote/auth/login",
        {
            "username": "admin",
            "password": TEST_AUTH_PASSWORD,
            "device_label": "pytest",
        },
    )
    return str(body["access_token"])


class FakeLLM:
    def __init__(self, model: str = "fake-model") -> None:
        self.model = model
        self.debug_trace = False
        self.api_key = "key"
        self.base_url = None
        self.temperature = 0.0
        self.max_tokens = 2048
        self.ui_bus = None

    def reconfigure(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class MemorySessionStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.documents: dict[str, dict] = {}
        self._next_id = 0

    def generate_session_id(self) -> str:
        self._next_id += 1
        return f"memory-session-{self._next_id}"

    def load(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def save(
        self,
        messages: list[dict],
        model: str,
        session_id: str | None = None,
        total_prompt_tokens: int = 0,
        total_completion_tokens: int = 0,
        active_mode: str | None = None,
        runtime_state: SessionRuntimeState | None = None,
        fingerprint: str = "local",
        **_kwargs,
    ) -> str:
        session_id = session_id or self.generate_session_id()
        effective_runtime = runtime_state or SessionRuntimeState(
            model=model, active_mode=active_mode
        )
        session = Session(
            id=session_id,
            model=effective_runtime.model or model,
            saved_at="memory",
            fingerprint=fingerprint,
            messages=[dict(message) for message in messages],
            active_mode=effective_runtime.active_mode or active_mode,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            runtime_state=effective_runtime,
        )
        self.sessions[session_id] = session
        self.documents[session_id] = update_session_document_metadata(
            self.documents.get(session_id),
            session_id=session_id,
            model=session.model,
            saved_at=session.saved_at,
            preview=session.get_preview(),
            fingerprint=session.fingerprint,
            runtime_state=effective_runtime.to_dict(),
        )
        return session_id

    def save_runtime_state(
        self,
        session_id: str,
        model: str,
        runtime_state: SessionRuntimeState,
        *,
        messages: list[dict] | None = None,
        total_prompt_tokens: int = 0,
        total_completion_tokens: int = 0,
        active_mode: str | None = None,
        fingerprint: str = "local",
    ) -> str:
        return self.save(
            messages or [],
            model,
            session_id=session_id,
            total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens,
            active_mode=active_mode,
            runtime_state=runtime_state,
            fingerprint=fingerprint,
        )

    def list(self, limit: int = 20, fingerprint: str | None = None) -> list[Session]:
        items = [
            session
            for session in self.sessions.values()
            if fingerprint is None or session.fingerprint == fingerprint
        ]
        items = [
            session
            for session in items
            if self.documents.get(session.id, {}).get("turns")
            or SessionStore.has_history_content(session.messages)
        ]
        return items[:limit]

    def get_latest(self, fingerprint: str | None = None) -> Session | None:
        items = self.list(limit=1, fingerprint=fingerprint)
        return items[0] if items else None

    def load_document(self, session_id: str) -> dict | None:
        document = self.documents.get(session_id)
        return json.loads(json.dumps(document)) if document is not None else None

    def save_document(self, session_id: str, document: dict) -> None:
        self.documents[session_id] = json.loads(json.dumps(document))

    def append_trace_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict | None = None,
        *,
        chat_id: str | None = None,
        chat_seq: int | None = None,
        source: str = "remote_chat",
        replayable: bool = True,
    ) -> int:
        events = getattr(self, "trace_events", None)
        if events is None:
            self.trace_events = {}
            events = self.trace_events
        session_events = events.setdefault(session_id, [])
        seq = len(session_events) + 1
        session_events.append(
            {
                "session_id": session_id,
                "session_event_seq": seq,
                "seq": chat_seq or seq,
                "chat_id": chat_id,
                "chat_seq": chat_seq,
                "type": event_type,
                "payload": payload or {},
                "source": source,
                "replayable": replayable,
            }
        )
        self.documents[session_id] = apply_session_event(
            self.documents.get(session_id),
            session_id=session_id,
            event_type=event_type,
            payload=payload or {},
            session_event_seq=seq,
            chat_id=chat_id,
            chat_seq=chat_seq,
        )
        return seq

    def list_trace_events(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int | None = None,
        replayable_only: bool = True,
    ) -> list[dict]:
        events = list(getattr(self, "trace_events", {}).get(session_id, []))
        events = [
            event
            for event in events
            if event["session_event_seq"] > after_seq
            and (not replayable_only or event.get("replayable") is not False)
        ]
        return events[:limit] if limit is not None else events

    def latest_trace_event_seq(self, session_id: str) -> int:
        events = getattr(self, "trace_events", {}).get(session_id, [])
        return max([0, *[event["session_event_seq"] for event in events]])

    def delete_trace_events(self, session_id: str) -> bool:
        return getattr(self, "trace_events", {}).pop(session_id, None) is not None

    def delete(self, session_id: str) -> bool:
        self.documents.pop(session_id, None)
        return self.sessions.pop(session_id, None) is not None


class FakeContext:
    def __init__(self) -> None:
        self.max_tokens = 64000
        self._ui_bus = None

    def reconfigure(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens


class FakeAgent:
    def __init__(self, llm: FakeLLM, tools=None, chat_behavior=None) -> None:
        self.llm = llm
        self.tools = list(tools or [])
        self.context = FakeContext()
        self.state = SimpleNamespace(
            messages=[],
            total_prompt_tokens=0,
            total_completion_tokens=0,
            current_round=0,
        )
        self.messages = self.state.messages
        self.available_modes = {
            "coder": ModeConfig(name="coder", description="Default coding mode"),
            "debugger": ModeConfig(name="debugger", description="Debug mode"),
        }
        self.active_mode = "coder"
        self.active_main_model_profile = None
        self.active_sub_model_profile = None
        self.session_fingerprint = "local"
        self.hook_registry = HookRegistry()
        self._event_handlers = []
        self.approval_provider = None
        self._stop_requested = False
        self._chat_behavior = chat_behavior or (lambda _agent, prompt: f"ok:{prompt}")

    def register_hook(self, hook_point, hook) -> None:
        self.hook_registry.register(hook_point, hook)

    def add_event_handler(self, handler) -> None:
        self._event_handlers.append(handler)

    def set_mode(self, mode_name: str) -> None:
        self.active_mode = mode_name

    def chat(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})
        response = self._chat_behavior(self, user_input)
        self.messages.append({"role": "assistant", "content": response})
        return response

    def request_stop(self) -> None:
        self._stop_requested = True

    def clear_stop_request(self) -> None:
        self._stop_requested = False

    def stop_requested(self) -> bool:
        return self._stop_requested


def _build_runner_with_fake_agent(
    relay_bind: str,
    chat_behavior=None,
    load_tools=None,
    session_dir: str | None = None,
    session_auto_save: bool = True,
    model_profiles: dict[str, ModelProfileConfig] | None = None,
    active_main_model_profile: str | None = None,
    providers: ProvidersConfig | None = None,
    session_store=None,
) -> AppRunner:
    default_providers = ProvidersConfig(
        items={
            "fake": ProviderConfig(
                id="fake",
                api_key="key",
                base_url="https://api.fake.test",
            )
        }
    )
    default_model_profiles = {
        "fake-main": ModelProfileConfig(
            name="fake-main",
            model="fake-model",
            provider="fake",
            max_tokens=2048,
            max_context_tokens=128000,
        )
    }

    default_dependencies = AppDependencies()

    def create_remote_http_service(config, relay, ui_bus):
        service = default_dependencies.create_remote_http_service(config, relay, ui_bus)
        if service is not None:
            service.require_explicit_chat_model = False
        return service

    config = Config(
        remote_exec=RemoteExecConfig(
            enabled=True, host_mode=True, relay_bind=relay_bind
        ),
        auth=_test_auth_config(),
        modes={
            "coder": ModeConfig(name="coder", description="Default coding mode"),
            "debugger": ModeConfig(name="debugger", description="Debug mode"),
        },
        active_mode="coder",
        session_dir=session_dir,
        session_auto_save=session_auto_save,
        model_profiles=model_profiles if model_profiles is not None else default_model_profiles,
        active_main_model_profile=active_main_model_profile or (
            "fake-main" if model_profiles is None else None
        ),
        providers=providers or default_providers,
    )
    config.skills.enabled = False
    return AppRunner(
        options=AppOptions(),
        dependencies=AppDependencies(
            load_config=lambda _: config,
            create_llm=lambda cfg: FakeLLM(resolve_model_runtime(cfg).model),
            load_tools=load_tools or (lambda _backend: []),
            create_agent=lambda llm, _tools, _config: FakeAgent(
                llm, tools=_tools, chat_behavior=chat_behavior
            ),
            create_remote_http_service=create_remote_http_service,
            create_configured_session_store=(
                (lambda _config, _sessions_dir: session_store)
                if session_store is not None
                else default_dependencies.create_configured_session_store
            ),
        ),
    )


def _register_peer(
    base_url: str,
    bootstrap_token: str,
    cwd: str,
    features: list[str] | None = None,
    host_info_min: dict | None = None,
) -> tuple[str, str]:
    _, register_body = _json_request(
        "POST",
        f"{base_url}/remote/register",
        {
            "bootstrap_token": bootstrap_token,
            "cwd": cwd,
            "workspace_root": cwd,
            "features": features or [],
            "host_info_min": host_info_min
            or {
                "os": "test-os",
                "arch": "test-arch",
                "shell": "test-shell",
                "hostname": "test-peer",
            },
        },
    )
    payload = register_body["payload"]
    return payload["peer_id"], payload["peer_token"]


def _collect_stream_events(
    base_url: str, peer_token: str, chat_id: str, timeout_sec: float = 3.0
) -> list[dict]:
    deadline = time.time() + timeout_sec
    cursor = 0
    events: list[dict] = []
    while time.time() < deadline:
        _, stream_body = _json_request(
            "POST",
            f"{base_url}/remote/chat/stream",
            {
                "peer_token": peer_token,
                "chat_id": chat_id,
                "cursor": cursor,
                "timeout_sec": 0.5,
            },
        )
        events.extend(stream_body["events"])
        cursor = stream_body["next_cursor"]
        if stream_body["done"]:
            return events
    raise AssertionError("timed out waiting for stream events")


def test_remote_relay_maps_all_ui_event_kinds_to_structured_events() -> None:
    expected = {
        UIEventKind.VIEW: "view",
        UIEventKind.CONTEXT: "context_event",
        UIEventKind.REMOTE: "remote_event",
        UIEventKind.MCP: "mcp_event",
        UIEventKind.MODEL: "model_event",
        UIEventKind.SESSION: "session_event",
        UIEventKind.COMMAND: "command_event",
        UIEventKind.APPROVAL: "approval_event",
        UIEventKind.SYSTEM: "system_event",
        UIEventKind.AGENT: "agent_event",
    }
    for kind, event_type in expected.items():
        event = UIEvent.info("hello", kind=kind, detail="value")
        assert _structured_ui_event_type(event) == event_type
        payload = _structured_ui_event_payload(event)
        assert payload["message"] == "hello"
        assert payload["kind"] == kind.value
        assert payload["detail"] == "value"


def test_remote_relay_maps_memory_context_to_dedicated_event_type() -> None:
    event = UIEvent.info(
        "Injected private memory context.",
        kind=UIEventKind.CONTEXT,
        schema="memory_context.v1",
        context_kind="memory_injection",
        provided_items=1,
    )

    assert _structured_ui_event_type(event) == "memory_context"
    payload = _structured_ui_event_payload(event)
    assert payload["schema"] == "memory_context.v1"
    assert payload["context_kind"] == "memory_injection"
    assert payload["provided_items"] == 1


def test_switch_session_model_updates_runtime_without_transcript_pollution() -> None:
    store = MemorySessionStore()
    config = Config(
        active_mode="coder",
        providers=ProvidersConfig(
            items={
                "deepseek": ProviderConfig(
                    id="deepseek",
                    api_key="key",
                    base_url="https://api.deepseek.test",
                )
            }
        ),
    )
    store.save(
        [{"role": "user", "content": "hello"}],
        "old-model",
        session_id="session-model",
        fingerprint="fp",
    )

    result = switch_session_model(
        config,
        store,
        "fp",
        {
            "session_id": "session-model",
            "provider_id": "deepseek",
            "model_id": "V4PRO",
            "parameters": {"max_tokens": 4096},
        },
    )

    assert result["ok"] is True
    assert result["active_model"]["provider_id"] == "deepseek"
    assert result["active_model"]["model_id"] == "V4PRO"
    assert result["runtime_state"]["active_model_provider"] == "deepseek"
    assert result["runtime_state"]["active_model"] == "V4PRO"
    saved = store.load("session-model")
    assert saved is not None
    assert len(saved.messages) == 1
    assert saved.messages[0]["role"] == "user"
    assert saved.messages[0]["content"] == "hello"
    assert saved.runtime_state.active_model == "V4PRO"


def test_switch_session_model_rejects_unknown_or_disabled_provider() -> None:
    store = MemorySessionStore()
    config = Config(
        providers=ProvidersConfig(
            items={
                "disabled": ProviderConfig(
                    id="disabled",
                    api_key="key",
                    enabled=False,
                )
            }
        )
    )

    missing = switch_session_model(
        config,
        store,
        "fp",
        {"provider_id": "missing", "model_id": "V4PRO"},
    )
    disabled = switch_session_model(
        config,
        store,
        "fp",
        {"provider_id": "disabled", "model_id": "V4PRO"},
    )

    assert missing["_status"] == 404
    assert missing["error"] == "provider_not_found"
    assert disabled["_status"] == 409
    assert disabled["error"] == "provider_disabled"


def test_runner_model_option_selects_configured_profile() -> None:
    config = Config(
        providers=ProvidersConfig(
            items={"fake": ProviderConfig(id="fake", api_key="key")}
        ),
        model_profiles={
            "alpha": ModelProfileConfig(
                name="alpha",
                provider="fake",
                model="alpha-model",
                max_tokens=2048,
                max_context_tokens=64000,
            ),
            "beta": ModelProfileConfig(
                name="beta",
                provider="fake",
                model="beta-model",
                max_tokens=4096,
                max_context_tokens=128000,
            ),
        },
        active_main_model_profile="alpha",
        remote_exec=RemoteExecConfig(enabled=False),
    )
    runner = AppRunner(
        options=AppOptions(model="beta"),
        dependencies=AppDependencies(
            load_config=lambda _: config,
            create_llm=lambda cfg: FakeLLM(resolve_model_runtime(cfg).model),
            load_tools=lambda _backend: [],
            create_agent=lambda llm, tools, _config: FakeAgent(llm, tools=tools),
        ),
    )

    ctx = runner.initialize()

    assert ctx.config.active_main_model_profile == "beta"
    assert ctx.agent.llm.model == "beta-model"
    runner.cleanup(ctx.agent)


def test_runner_model_option_rejects_unknown_profile() -> None:
    config = Config(
        providers=ProvidersConfig(
            items={"fake": ProviderConfig(id="fake", api_key="key")}
        ),
        model_profiles={
            "alpha": ModelProfileConfig(
                name="alpha",
                provider="fake",
                model="alpha-model",
                max_tokens=2048,
                max_context_tokens=64000,
            )
        },
        active_main_model_profile="alpha",
        remote_exec=RemoteExecConfig(enabled=False),
    )
    runner = AppRunner(
        options=AppOptions(model="missing"),
        dependencies=AppDependencies(load_config=lambda _: config),
    )

    try:
        runner.initialize()
    except ConfigValidationError as exc:
        assert "model profile 'missing' does not exist" in str(exc)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("expected missing model profile to fail")


class TestRunnerRemoteExec:
    def test_local_mode_no_relay(self, tmp_path: Path) -> None:
        """When remote_exec is disabled, runner starts normally with local backend."""
        config = Config(remote_exec=RemoteExecConfig(enabled=False))
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
                create_llm=lambda _: FakeLLM(),
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is None
        assert ctx.agent is not None
        runner.cleanup(ctx.agent)

    def test_local_mode_smoke_startup_uses_local_backends(self, tmp_path: Path) -> None:
        """Smoke test: normal local startup should not initialize remote services."""
        config = Config(
            remote_exec=RemoteExecConfig(enabled=False, host_mode=False),
        )
        runner = AppRunner(
            options=AppOptions(server_mode=False),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is None
            assert runner._relay_http_service is None
            assert ctx.agent is not None
            assert len(ctx.agent.tools) > 0
            assert all(
                getattr(tool.backend, "backend_id", None) == "local"
                for tool in ctx.agent.tools
            )
        finally:
            runner.cleanup(ctx.agent)

    def test_remote_enabled_host_mode_starts_relay(self, tmp_path: Path) -> None:
        config = Config(
            remote_exec=RemoteExecConfig(
                enabled=True,
                host_mode=True,
                relay_bind=f"127.0.0.1:{_free_port()}",
            ),
            auth=_test_auth_config(tmp_path),
        )
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
                create_llm=lambda _: FakeLLM(),
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is not None
        assert isinstance(runner._relay_server, RelayServer)
        assert all(
            isinstance(tool.backend, RemoteRelayToolBackend) for tool in ctx.agent.tools
        )
        runner.cleanup(ctx.agent)
        assert runner._relay_server is None

    def test_remote_relay_uses_configured_peer_token_ttl(
        self, tmp_path: Path
    ) -> None:
        config = Config(
            remote_exec=RemoteExecConfig(
                enabled=True,
                host_mode=True,
                relay_bind=f"127.0.0.1:{_free_port()}",
                peer_token_ttl_sec=123,
            ),
            auth=_test_auth_config(tmp_path),
        )
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
                create_llm=lambda _: FakeLLM(),
            ),
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_server._peer_token_ttl_sec == 123
        finally:
            runner.cleanup(ctx.agent)

    def test_remote_init_failure_does_not_crash(self, tmp_path: Path) -> None:
        def bad_relay_factory(_config: Config) -> RelayServer:
            raise RuntimeError("boom")

        config = Config(
            remote_exec=RemoteExecConfig(enabled=True, host_mode=True),
            auth=_test_auth_config(tmp_path),
        )
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
                create_llm=lambda _: FakeLLM(),
                create_remote_relay_server=bad_relay_factory,
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is None
        assert ctx.agent is not None
        runner.cleanup(ctx.agent)

    def test_cleanup_runs_relay_cleanup(self, tmp_path: Path) -> None:
        config = Config(
            remote_exec=RemoteExecConfig(
                enabled=True,
                host_mode=True,
                relay_bind=f"127.0.0.1:{_free_port()}",
            ),
            auth=_test_auth_config(tmp_path),
        )
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
                create_llm=lambda _: FakeLLM(),
            ),
        )
        ctx = runner.initialize()
        assert runner._relay_server is not None
        # no peers connected, cleanup should still complete without error
        runner.cleanup(ctx.agent)
        assert runner._relay_server is None

    def test_runner_preserves_context_config_on_agent(self, tmp_path: Path) -> None:
        config = Config(
            context=ContextConfig(
                snip_keep_recent_tools=9,
                snip_threshold_chars=3210,
                snip_min_lines=8,
                summarize_keep_recent_turns=6,
            ),
            remote_exec=RemoteExecConfig(enabled=False),
        )
        runner = AppRunner(
            options=AppOptions(),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        assert (
            getattr(ctx.agent, "config", None) is None
            or getattr(ctx.agent, "config", None) == config
        )
        assert ctx.agent.max_context_tokens == 1
        runner.cleanup(ctx.agent)

    def test_attach_mcp_starts_server_and_both_placements(self, monkeypatch) -> None:
        config = Config(
            mcp_servers=[
                MCPServerConfig(name="server-only", command="a", placement="server"),
                MCPServerConfig(name="peer-only", command="b", placement="peer"),
                MCPServerConfig(name="shared", command="c", placement="both"),
            ]
        )
        runner = AppRunner(options=AppOptions())
        agent = SimpleNamespace()
        started: list[str] = []

        def fake_init_mcp(servers, _agent, _ui_bus):
            started.extend(server.name for server in servers)
            return "manager"

        monkeypatch.setattr(runner, "_init_mcp", fake_init_mcp)

        manager = runner._attach_mcp_if_configured(config, agent, None)

        assert manager == "manager"
        assert agent.mcp_manager == "manager"
        assert started == ["server-only", "shared"]

    def test_server_mode_smoke_auth_bootstrap_token(self, tmp_path: Path) -> None:
        relay_bind = "127.0.0.1:18765"
        config = Config(
            remote_exec=RemoteExecConfig(
                enabled=True,
                host_mode=True,
                relay_bind=relay_bind,
            ),
            auth=_test_auth_config(tmp_path),
        )
        runner = AppRunner(
            options=AppOptions(server_mode=True),
            dependencies=AppDependencies(
                load_config=lambda _: config,
            ),
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            assert isinstance(runner._relay_http_service, RemoteRelayHTTPService)

            access_token = _login_admin(f"http://{relay_bind}")
            status, body = _json_request(
                "POST",
                f"http://{relay_bind}/remote/auth/bootstrap-token",
                {},
                headers={"Authorization": f"Bearer {access_token}"},
            )

            assert status == 200
            assert body["bootstrap_token"].startswith("bt_")
        finally:
            runner.cleanup(ctx.agent)

    def test_server_settings_reload_refreshes_runtime_control_plane_snapshot(
        self, tmp_path: Path
    ) -> None:
        port = _free_port()
        relay_bind = f"127.0.0.1:{port}"
        config_path = tmp_path / "config.host.yaml"
        save_yaml_config(
            config_path,
            {
                "remote_exec": {
                    "enabled": True,
                    "host_mode": True,
                    "relay_bind": relay_bind,
                },
                "auth": _test_auth_config(tmp_path).to_dict(),
                "run_limits": {
                    "max_running_agents": 1,
                    "max_shells_per_agent": 1,
                },
                "skills": {"enabled": False},
            },
        )

        def load_test_config(path: Path | None) -> Config:
            data = load_yaml_config(path or config_path)
            remote_exec = data.get("remote_exec", {})
            config = Config(
                remote_exec=RemoteExecConfig(
                    enabled=bool(remote_exec.get("enabled", True)),
                    host_mode=bool(remote_exec.get("host_mode", True)),
                    relay_bind=str(remote_exec.get("relay_bind", relay_bind)),
                ),
                auth=AuthConfig.from_dict(data.get("auth", {})),
                agent_registry=AgentRegistryConfig.from_dict(data.get("agent_registry", {})),
                runtime_profiles=RuntimeProfilesConfig.from_dict(data.get("runtime_profiles", {})),
                run_limits=RunLimitsConfig.from_dict(data.get("run_limits", {})),
                modes={
                    "coder": ModeConfig(
                        name="coder", description="Default coding mode"
                    )
                },
                active_mode="coder",
            )
            config.skills.enabled = False
            return config

        runner = AppRunner(
            options=AppOptions(config_path=config_path, server_mode=True),
            dependencies=AppDependencies(
                load_config=load_test_config,
                create_llm=lambda cfg: FakeLLM(resolve_model_runtime(cfg).model),
                load_tools=lambda _backend: [],
                create_agent=lambda llm, tools, _config: FakeAgent(llm, tools=tools),
            ),
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_http_service is not None
            control = runner._relay_http_service.runtime_control_plane
            assert control is not None

            access_token = _login_admin(runner._relay_http_service.base_url)
            _, body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/admin/server-settings/update",
                {
                    "run_limits": {
                        "max_running_agents": 4,
                        "max_shells_per_agent": 1,
                    },
                    "runtime_profiles": {
                        "smoke_fake_profile": {
                            "executor": "fake",
                            "execution_location": "daemon_worktree",
                        }
                    },
                    "agent_registry": {
                        "agents": {
                            "smoke_reviewer": {"runtime_profile": "smoke_fake_profile"}
                        }
                    },
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )

            assert body["ok"] is True
            assert control.max_running_tasks == 4
            assert (
                control.runtime_snapshot["runtime_profiles"]["smoke_fake_profile"][
                    "executor"
                ]
                == "fake"
            )
            assert (
                control.runtime_snapshot["agents"]["smoke_reviewer"][
                    "runtime_profile"
                ]
                == "smoke_fake_profile"
            )
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_emits_startup_event(self) -> None:
        workspace = Path(__file__).resolve().parent
        port = _free_port()
        runner = _build_runner_with_fake_agent(f"127.0.0.1:{port}")
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            peer_id, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "hello"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            ready_events = [
                event for event in events if event["type"] == "remote_peer_ready"
            ]
            assert ready_events
            payload = ready_events[0]["payload"]
            assert payload["peer_id"] == peer_id
            assert payload["session_id"]
            assert payload["fingerprint"]
            assert payload["mode"] == "coder"
            assert payload["model"]
            assert not any(
                event["type"] == "output"
                and "REMOTE PEER READY" in event["payload"].get("content", "")
                for event in events
            )
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_includes_llm_diagnostic_fields_on_failure(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        port = _free_port()

        def chat_behavior(_agent: FakeAgent, _prompt: str) -> str:
            error = RuntimeError("Connection error.")
            setattr(
                error,
                "llm_diagnostic_path",
                "/app/.rcoder/diagnostics/llm_error_session.json",
            )
            setattr(error, "provider_error_phase", "request_start")
            raise error

        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}", chat_behavior=chat_behavior
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "hello"},
            )

            events = _collect_stream_events(
                runner._relay_http_service.base_url,
                peer_token,
                start_body["chat_id"],
            )
            failed_events = [
                event for event in events if event["type"] == "chat_failed"
            ]

            assert failed_events
            payload = failed_events[0]["payload"]
            assert payload["message"] == "Connection error."
            assert payload["code"] == "REMOTE_CHAT_ERROR"
            assert payload["error_type"] == "RuntimeError"
            assert payload["provider_error_phase"] == "request_start"
            assert payload["diagnostic_path"].endswith("llm_error_session.json")
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_exposes_running_document_after_ready(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        port = _free_port()
        release = threading.Event()

        def chat_behavior(agent: FakeAgent, prompt: str) -> str:
            assert getattr(agent, "current_session_id", "")
            release.wait(timeout=3)
            return f"done:{prompt}"

        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}",
            chat_behavior=chat_behavior,
            session_dir=str(tmp_path / "sessions"),
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "hello"},
            )
            cursor = 0
            ready = None
            deadline = time.time() + 3
            while time.time() < deadline and ready is None:
                _, stream_body = _json_request(
                    "POST",
                    f"{runner._relay_http_service.base_url}/remote/chat/stream",
                    {
                        "peer_token": peer_token,
                        "chat_id": start_body["chat_id"],
                        "cursor": cursor,
                        "timeout_sec": 0.5,
                    },
                )
                cursor = stream_body["next_cursor"]
                ready = next(
                    (
                        event
                        for event in stream_body["events"]
                        if event["type"] == "remote_peer_ready"
                    ),
                    None,
                )
            assert ready is not None
            session_id = ready["payload"]["session_id"]

            _, listed = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/list",
                {"peer_token": peer_token},
            )
            assert any(item["id"] == session_id for item in listed["sessions"])
            _, running_doc = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/load",
                {"peer_token": peer_token, "session_id": session_id},
            )
            assert running_doc["document"]["turns"][0]["userMessage"]["text"] == "hello"

            release.set()
            _collect_stream_events(
                runner._relay_http_service.base_url,
                peer_token,
                start_body["chat_id"],
            )
            _, listed_after = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/list",
                {"peer_token": peer_token},
            )
            assert any(item["id"] == session_id for item in listed_after["sessions"])
        finally:
            release.set()
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_uses_explicit_session_hint(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        port = _free_port()

        def chat_behavior(agent: FakeAgent, prompt: str) -> str:
            existing = [
                message.get("content")
                for message in agent.messages
                if message.get("role") == "user"
            ]
            return f"{prompt}|history={','.join(existing)}|sid={getattr(agent, 'current_session_id', '-')}"

        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}",
            chat_behavior=chat_behavior,
            session_dir=str(tmp_path / "sessions"),
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            peer_id, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace),
            )
            store = SessionStore(tmp_path / "sessions")
            fingerprint = f"remote:{peer_id}:{workspace}"
            store.save(
                [{"role": "user", "content": "old-context"}],
                "fake-model",
                "session-old",
                fingerprint=fingerprint,
            )

            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {
                    "peer_token": peer_token,
                    "prompt": "fresh",
                    "session_hint": "session-new",
                },
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            ready = [
                event["payload"]
                for event in events
                if event["type"] == "remote_peer_ready"
            ][0]
            end = [event for event in events if event["type"] == "chat_end"][-1]
            assert ready["session_id"] == "session-new"
            assert "old-context" not in end["payload"]["response"]
            assert "fresh|history=fresh|sid=session-new" in end["payload"]["response"]
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_without_session_hint_starts_fresh_session(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        port = _free_port()

        def chat_behavior(agent: FakeAgent, prompt: str) -> str:
            existing = [
                message.get("content")
                for message in agent.messages
                if message.get("role") == "user"
            ]
            return f"{prompt}|history={','.join(existing)}|sid={getattr(agent, 'current_session_id', '-')}"

        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}",
            chat_behavior=chat_behavior,
            session_dir=str(tmp_path / "sessions"),
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            peer_id, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace),
            )
            store = SessionStore(tmp_path / "sessions")
            fingerprint = f"remote:{peer_id}:{workspace}"
            store.save(
                [{"role": "user", "content": "old-context"}],
                "fake-model",
                "session-old",
                fingerprint=fingerprint,
            )

            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {
                    "peer_token": peer_token,
                    "prompt": "fresh",
                },
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            ready = [
                event["payload"]
                for event in events
                if event["type"] == "remote_peer_ready"
            ][0]
            end = [event for event in events if event["type"] == "chat_end"][-1]
            assert ready["session_id"] != "session-old"
            assert "old-context" not in end["payload"]["response"]
            assert "fresh|history=fresh|sid=session_" in end["payload"]["response"]
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_skips_session_save_when_auto_save_disabled(
        self, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        port = _free_port()
        sessions_dir = tmp_path / "sessions"
        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}",
            session_dir=str(sessions_dir),
            session_auto_save=False,
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace),
            )
            _, features = _json_request(
                "GET", f"{runner._relay_http_service.base_url}/remote/features"
            )
            assert features["features"]["session_auto_save"] is False
            assert features["features"]["session_history_writable"] is False

            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "fresh"},
            )
            _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )

            _, listed = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/list",
                {"peer_token": peer_token},
            )
            assert listed["sessions"] == []
            assert SessionStore(sessions_dir).list(limit=10, fingerprint=None) == []
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_remote_session_endpoints_roundtrip(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        port = _free_port()
        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}", session_dir=str(tmp_path / "sessions")
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace),
            )
            _, created = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/new",
                {"peer_token": peer_token},
            )
            session_id = created["metadata"]["id"]
            assert created["ok"] is True
            assert session_id

            _, listed = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/list",
                {"peer_token": peer_token},
            )
            assert not any(item["id"] == session_id for item in listed["sessions"])

            _, empty_loaded = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/load",
                {"peer_token": peer_token, "session_id": session_id},
            )
            assert empty_loaded["document"]["turns"] == []

            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {
                    "peer_token": peer_token,
                    "prompt": "hello",
                    "session_hint": session_id,
                },
            )
            _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )

            _, listed = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/list",
                {"peer_token": peer_token},
            )
            assert any(item["id"] == session_id for item in listed["sessions"])
            assert listed["list_etag"]
            _, unchanged_list = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/list",
                {
                    "peer_token": peer_token,
                    "if_list_etag": listed["list_etag"],
                },
            )
            assert unchanged_list["sessions_unchanged"] is True
            assert unchanged_list["list_etag"] == listed["list_etag"]
            assert "sessions" not in unchanged_list

            _, loaded = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/load",
                {"peer_token": peer_token, "session_id": session_id},
            )
            assert loaded["metadata"]["id"] == session_id
            assert loaded["document"]["turns"][0]["userMessage"]["text"] == "hello"
            assert loaded["last_event_seq"] >= 1

            _, deleted = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/delete",
                {"peer_token": peer_token, "session_id": session_id},
            )
            assert deleted["ok"] is True
            try:
                _json_request(
                    "POST",
                    f"{runner._relay_http_service.base_url}/remote/sessions/load",
                    {"peer_token": peer_token, "session_id": session_id},
                )
                raise AssertionError("expected deleted session to fail")
            except HTTPError as exc:
                assert exc.code == 404
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_remote_session_model_switch_updates_session_runtime(self) -> None:
        workspace = Path(__file__).resolve().parent
        port = _free_port()
        session_store = MemorySessionStore()

        def chat_behavior(agent: FakeAgent, prompt: str) -> str:
            return f"{prompt}|model={agent.llm.model}|sid={getattr(agent, 'current_session_id', '-')}"

        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}",
            chat_behavior=chat_behavior,
            session_store=session_store,
            providers=ProvidersConfig(
                items={
                    "deepseek": ProviderConfig(
                        id="deepseek",
                        api_key="key",
                        base_url="https://api.deepseek.test",
                    )
                }
            ),
            model_profiles={
                "main-fast": ModelProfileConfig(
                    name="main-fast",
                    model="fast-model",
                    provider="deepseek",
                    max_tokens=1111,
                    max_context_tokens=2222,
                ),
                "main-deep": ModelProfileConfig(
                    name="main-deep",
                    model="deep-model",
                    provider="deepseek",
                    max_tokens=3333,
                    max_context_tokens=4444,
                ),
            },
            active_main_model_profile="main-fast",
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace),
            )

            try:
                _json_request(
                    "POST",
                    f"{runner._relay_http_service.base_url}/remote/sessions/model",
                    {
                        "peer_token": peer_token,
                        "session_id": "session-model",
                        "provider_id": "missing-provider",
                        "model_id": "deep-model",
                    },
                )
                raise AssertionError("expected unknown provider to fail")
            except HTTPError as exc:
                assert exc.code == 404

            _, switched = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/model",
                {
                        "peer_token": peer_token,
                        "session_id": "session-model",
                        "provider_id": "deepseek",
                        "model_id": "deep-model",
                        "parameters": {"max_tokens": 3333, "max_context_tokens": 4444},
                    },
                )

            assert switched["ok"] is True
            assert switched["session_id"] == "session-model"
            assert switched["active_model"]["provider_id"] == "deepseek"
            assert switched["active_model"]["model_id"] == "deep-model"
            assert switched["runtime_state"]["active_model_provider"] == "deepseek"
            assert switched["runtime_state"]["active_model"] == "deep-model"
            assert switched["runtime_state"]["model"] == "deep-model"

            stored = session_store.load("session-model")
            assert stored is not None
            assert stored.messages == []
            assert stored.runtime_state.active_model_provider == "deepseek"
            assert stored.runtime_state.active_model == "deep-model"
            assert session_store.list(limit=10, fingerprint=None) == []

            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {
                    "peer_token": peer_token,
                    "prompt": "hello",
                    "session_hint": "session-model",
                },
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            ready = [
                event["payload"]
                for event in events
                if event["type"] == "remote_peer_ready"
            ][0]
            end = [event for event in events if event["type"] == "chat_end"][-1]
            assert ready["model"] == "deep-model"
            assert "hello|model=deep-model|sid=session-model" in end["payload"]["response"]
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_remote_session_fork_clones_history_and_reuses_it_for_chat(self) -> None:
        workspace = Path(__file__).resolve().parent
        port = _free_port()
        session_store = MemorySessionStore()

        def chat_behavior(agent: FakeAgent, prompt: str) -> str:
            existing = [
                message.get("content")
                for message in agent.state.messages
                if message.get("role") == "user"
            ]
            return f"{prompt}|history={','.join(existing)}|sid={getattr(agent, 'current_session_id', '-')}"

        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}",
            chat_behavior=chat_behavior,
            session_store=session_store,
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace),
            )

            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "hello"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            ready = [
                event["payload"]
                for event in events
                if event["type"] == "remote_peer_ready"
            ][0]
            source_session_id = ready["session_id"]

            _, loaded = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/load",
                {"peer_token": peer_token, "session_id": source_session_id},
            )
            assert loaded["document"]["turns"][0]["userMessage"]["text"] == "hello"

            _, forked = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/sessions/fork",
                {
                    "peer_token": peer_token,
                    "source_session_id": source_session_id,
                    "keep_through_message_index": 0,
                },
            )
            forked_session_id = forked["metadata"]["id"]
            assert forked["document"]["session"]["parentSessionId"] == source_session_id
            assert forked["document"]["turns"][0]["userMessage"]["text"] == "hello"

            _, fork_start = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {
                    "peer_token": peer_token,
                    "prompt": "follow up",
                    "session_hint": forked_session_id,
                },
            )
            fork_events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, fork_start["chat_id"]
            )
            fork_ready = [
                event["payload"]
                for event in fork_events
                if event["type"] == "remote_peer_ready"
            ][0]
            fork_end = [event for event in fork_events if event["type"] == "chat_end"][-1]
            assert fork_ready["session_id"] == forked_session_id
            assert "follow up|history=hello|sid=" in fork_end["payload"]["response"]

            forked_session = session_store.load(forked_session_id)
            assert forked_session is not None
            assert any(
                message.get("role") == "user" and message.get("content") == "follow up"
                for message in forked_session.messages
            )
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_uses_structured_tool_events(self) -> None:
        workspace = Path(__file__).resolve().parent
        long_result = "file body\n" + ("x" * 700)

        def emit(agent: FakeAgent, event: AgentEvent) -> None:
            for handler in list(agent._event_handlers):
                handler(event)

        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            emit(agent, AgentEvent.reasoning_token("Need file"))
            emit(agent, AgentEvent.stream_token("Before tool"))
            emit(
                agent,
                AgentEvent.tool_call_start(
                    "read_file",
                    {"file_path": str(workspace / "decision.md")},
                    tool_call_id="call-read-1",
                ),
            )
            emit(
                agent,
                AgentEvent.tool_call_end(
                    "read_file",
                    long_result,
                    tool_call_id="call-read-1",
                ),
            )
            return "done"

        port = _free_port()
        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}", chat_behavior=chat_behavior
        )
        ctx = runner.initialize()
        try:
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "read"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            assistant_text = "".join(
                event["payload"].get("content", "")
                for event in events
                if event["type"] == "assistant_delta"
            )
            reasoning_text = "".join(
                event["payload"].get("content", "")
                for event in events
                if event["type"] == "reasoning_delta"
            )
            final_reasoning_text = "".join(
                event["payload"].get("content", "")
                for event in events
                if event["type"] == "reasoning_message"
            )
            final_assistant_text = "".join(
                event["payload"].get("content", "")
                for event in events
                if event["type"] == "assistant_message"
            )
            terminal_text = "\n".join(
                event["payload"].get("content", "")
                for event in events
                if event["type"] == "output"
            )

            assert reasoning_text == "Need file"
            assert assistant_text == "Before tool"
            assert final_reasoning_text == "Need file"
            assert final_assistant_text == "done"
            assert "TOOL CALL" not in terminal_text
            assert "read_file(" not in terminal_text
            assert "file body" not in terminal_text
            assert any(
                event["type"] == "tool_call_start"
                and event["payload"].get("tool_name") == "read_file"
                for event in events
            )
            assert any(
                event["type"] == "tool_call_end"
                and event["payload"].get("tool_name") == "read_file"
                and event["payload"].get("tool_result") == long_result
                and len(event["payload"].get("tool_result", "")) > 500
                and ("tool_" + "success") not in event["payload"]
                for event in events
            )
            assert any(
                event["type"] == "chat_end"
                and event["payload"].get("response_rendered") is True
                for event in events
            )
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_forwards_context_ui_events(
        self, tmp_path: Path
    ) -> None:
        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            assert agent.context._ui_bus is not None
            agent.context._ui_bus.info(
                "Context auto-compression triggered at 900 tokens / 8 messages.",
                kind=UIEventKind.CONTEXT,
                phase="before",
                trigger_tokens=900,
                trigger_message_count=8,
                applied_layers=["hard_collapse"],
            )
            agent.context._ui_bus.success(
                "Context auto-compression completed: 900 → 300 tokens, 8 → 6 messages.",
                kind=UIEventKind.CONTEXT,
                phase="after",
                before_tokens=900,
                after_tokens=300,
                applied_layers=["hard_collapse"],
            )
            return "done"

        port = _free_port()
        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}", chat_behavior=chat_behavior
        )
        ctx = runner.initialize()
        try:
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "compress"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            context_events = [
                event for event in events if event["type"] == "context_event"
            ]

            assert [event["payload"]["phase"] for event in context_events] == [
                "before",
                "after",
            ]
            assert context_events[0]["payload"]["kind"] == "context"
            assert context_events[0]["payload"]["applied_layers"] == ["hard_collapse"]
            assert context_events[1]["payload"]["after_tokens"] == 300
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_remote_approval_uses_peer_preview(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        preview_calls: list[dict] = []

        def fake_preview(self, peer_id, request, timeout_sec=None):
            preview_calls.append(
                {"peer_id": peer_id, "tool_name": request.tool_name, "args": request.args}
            )
            return ToolPreviewResult(
                ok=True,
                sections=[
                    {
                        "id": "diff",
                        "title": "Proposed file diff",
                        "kind": "diff",
                        "content": "--- a/demo.txt\n+++ b/demo.txt\n-peer\n+host\n",
                        "resolved_path": str(tmp_path / "demo.txt"),
                        "original_text": "peer\n",
                        "modified_text": "host\n",
                    }
                ],
                resolved_path=str(tmp_path / "demo.txt"),
                old_sha256="peer-sha",
                old_exists=True,
            )

        monkeypatch.setattr(RelayServer, "send_preview_request", fake_preview)

        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            decision = agent.approval_provider.request_approval(
                ApprovalRequest(
                    tool_name="write_file",
                    tool_args={"file_path": "demo.txt", "content": "host\n"},
                    tool_source="builtin",
                    reason="confirm write",
                )
            )
            assert decision.approved
            return "approved"

        port = _free_port()
        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}",
            chat_behavior=chat_behavior,
            load_tools=lambda backend: [SimpleNamespace(name="write_file", backend=backend)],
        )
        ctx = runner.initialize()
        try:
            peer_id, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
                features=["tool_preview"],
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "write"},
            )
            cursor = 0
            approval_events: list[dict] = []
            deadline = time.time() + 3
            while time.time() < deadline and not approval_events:
                _, stream_body = _json_request(
                    "POST",
                    f"{runner._relay_http_service.base_url}/remote/chat/stream",
                    {
                        "peer_token": peer_token,
                        "chat_id": start_body["chat_id"],
                        "cursor": cursor,
                        "timeout_sec": 0.5,
                    },
                )
                cursor = stream_body["next_cursor"]
                approval_events = [
                    event
                    for event in stream_body["events"]
                    if event["type"] == "approval_request"
                ]
            assert approval_events
            payload = approval_events[0]["payload"]
            assert preview_calls[0]["peer_id"] == peer_id
            assert payload["preview_unavailable"] is False
            assert payload["sections"][0]["kind"] == "diff"
            assert payload["sections"][0]["original_text"] == "peer\n"
            assert "confirm write" in payload["content"]

            _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/approval/reply",
                {
                    "peer_token": peer_token,
                    "chat_id": start_body["chat_id"],
                    "approval_id": payload["approval_id"],
                    "decision": "allow_once",
                },
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url,
                peer_token,
                start_body["chat_id"],
            )
            assert any(
                event["type"] == "chat_end"
                and event["payload"].get("response") == "approved"
                for event in events
            )
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_remote_write_preview_failure_is_tool_denial(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def fake_preview(self, peer_id, request, timeout_sec=None):
            return ToolPreviewResult(
                ok=False,
                error_code="REMOTE_TOOL_ERROR",
                error_message="old_string not found in demo.txt",
            )

        monkeypatch.setattr(RelayServer, "send_preview_request", fake_preview)
        decisions: list[tuple[bool, str | None, dict]] = []

        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            decision = agent.approval_provider.request_approval(
                ApprovalRequest(
                    tool_name="write_file",
                    tool_args={"file_path": "demo.txt", "content": "host\n"},
                    tool_source="builtin",
                    reason="confirm write",
                    metadata={"tool_call_id": "call-write-preview-failed"},
                )
            )
            decisions.append((decision.approved, decision.reason, decision.meta))
            return decision.reason or "missing preview failure"

        port = _free_port()
        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}",
            chat_behavior=chat_behavior,
            load_tools=lambda backend: [SimpleNamespace(name="write_file", backend=backend)],
        )
        ctx = runner.initialize()
        try:
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
                features=["tool_preview"],
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "write"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url,
                peer_token,
                start_body["chat_id"],
            )

            assert decisions == [
                (
                    False,
                    "Error [REMOTE_TOOL_ERROR]: old_string not found in demo.txt",
                    {
                        "failure_kind": ToolFailureKind.TOOL_RESULT_ERROR.value,
                        "code": "REMOTE_TOOL_ERROR",
                        "message": "old_string not found in demo.txt",
                        "tool_diagnostics": [
                            {
                                "stage": "preview",
                                "kind": "tool_result_error",
                                "severity": "error",
                                "code": "REMOTE_TOOL_ERROR",
                                "message": "old_string not found in demo.txt",
                                "repairable": True,
                                "tool_name": "write_file",
                                "tool_call_id": "call-write-preview-failed",
                            }
                        ],
                    },
                )
            ]
            assert not any(event["type"] == "approval_request" for event in events)
            assert not any(
                event["type"] == "tool_call_protocol_error" for event in events
            )
            assert not any(event["type"] == "chat_failed" for event in events)
            assert any(
                event["type"] == "chat_end"
                and event["payload"].get("response")
                == "Error [REMOTE_TOOL_ERROR]: old_string not found in demo.txt"
                for event in events
            )
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_remote_write_approval_requires_peer_preview_capability(
        self, tmp_path: Path
    ) -> None:
        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            agent.approval_provider.request_approval(
                ApprovalRequest(
                    tool_name="write_file",
                    tool_args={"file_path": "demo.txt", "content": "host\n"},
                    tool_source="builtin",
                    reason="confirm write",
                    metadata={"tool_call_id": "call-write-1"},
                )
            )
            return "unexpected"

        port = _free_port()
        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}",
            chat_behavior=chat_behavior,
            load_tools=lambda backend: [SimpleNamespace(name="write_file", backend=backend)],
        )
        ctx = runner.initialize()
        try:
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
                features=[],
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "write"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url,
                peer_token,
                start_body["chat_id"],
            )

            assert not any(event["type"] == "approval_request" for event in events)
            protocol_events = [
                event for event in events if event["type"] == "tool_call_protocol_error"
            ]
            assert protocol_events
            payload = protocol_events[0]["payload"]
            assert payload["tool_name"] == "write_file"
            assert payload["tool_call_id"] == "call-write-1"
            assert payload["code"] == "REMOTE_PREVIEW_REQUIRED"
            assert payload["failure_kind"] == ToolFailureKind.TOOL_PROTOCOL_ERROR.value
            assert payload["tool_diagnostics"][0]["stage"] == "protocol"
            assert payload["tool_diagnostics"][0]["kind"] == "tool_protocol_error"
            assert any(event["type"] == "error" for event in events)
            failed_events = [
                event for event in events if event["type"] == "chat_failed"
            ]
            assert failed_events
            assert failed_events[0]["payload"]["code"] == "REMOTE_PREVIEW_REQUIRED"
            assert failed_events[0]["payload"]["recoverable"] is False
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_remote_write_approval_rejects_empty_peer_preview(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        def fake_preview(self, peer_id, request, timeout_sec=None):
            return ToolPreviewResult(ok=True, sections=[], diff="")

        monkeypatch.setattr(RelayServer, "send_preview_request", fake_preview)

        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            agent.approval_provider.request_approval(
                ApprovalRequest(
                    tool_name="write_file",
                    tool_args={"file_path": "demo.txt", "content": "host\n"},
                    tool_source="builtin",
                    reason="confirm write",
                    metadata={"tool_call_id": "call-write-empty"},
                )
            )
            return "unexpected"

        port = _free_port()
        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}",
            chat_behavior=chat_behavior,
            load_tools=lambda backend: [SimpleNamespace(name="write_file", backend=backend)],
        )
        ctx = runner.initialize()
        try:
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
                features=["tool_preview"],
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "write"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url,
                peer_token,
                start_body["chat_id"],
            )

            assert not any(event["type"] == "approval_request" for event in events)
            protocol_events = [
                event for event in events if event["type"] == "tool_call_protocol_error"
            ]
            assert protocol_events[0]["payload"]["code"] == "REMOTE_PREVIEW_EMPTY"
            assert (
                protocol_events[0]["payload"]["failure_kind"]
                == ToolFailureKind.TOOL_PROTOCOL_ERROR.value
            )
            assert protocol_events[0]["payload"]["tool_diagnostics"][0]["stage"] == "protocol"
            failed_events = [
                event for event in events if event["type"] == "chat_failed"
            ]
            assert failed_events[0]["payload"]["code"] == "REMOTE_PREVIEW_EMPTY"
            assert failed_events[0]["payload"]["tool_diagnostics"][0]["stage"] == "chat"
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_remote_shell_approval_does_not_require_peer_preview(
        self, tmp_path: Path
    ) -> None:
        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            decision = agent.approval_provider.request_approval(
                ApprovalRequest(
                    tool_name="shell",
                    tool_args={"command": "pwd"},
                    tool_source="builtin",
                    reason="confirm shell",
                    metadata={"tool_call_id": "call-shell-1"},
                )
            )
            return "approved" if decision.approved else "denied"

        port = _free_port()
        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}", chat_behavior=chat_behavior
        )
        ctx = runner.initialize()
        try:
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
                features=[],
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "shell"},
            )
            cursor = 0
            approval_payload: dict | None = None
            deadline = time.time() + 3
            while time.time() < deadline and approval_payload is None:
                _, stream_body = _json_request(
                    "POST",
                    f"{runner._relay_http_service.base_url}/remote/chat/stream",
                    {
                        "peer_token": peer_token,
                        "chat_id": start_body["chat_id"],
                        "cursor": cursor,
                        "timeout_sec": 0.5,
                    },
                )
                cursor = stream_body["next_cursor"]
                for event in stream_body["events"]:
                    if event["type"] == "approval_request":
                        approval_payload = event["payload"]
                        break
            assert approval_payload is not None
            assert approval_payload["tool_name"] == "shell"
            assert approval_payload["preview_unavailable"] is True

            _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/approval/reply",
                {
                    "peer_token": peer_token,
                    "chat_id": start_body["chat_id"],
                    "approval_id": approval_payload["approval_id"],
                    "decision": "deny_once",
                },
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url,
                peer_token,
                start_body["chat_id"],
            )
            assert not any(
                event["type"] == "tool_call_protocol_error" for event in events
            )
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_remote_stream_without_tool_call_id_is_protocol_error(
        self, tmp_path: Path
    ) -> None:
        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            backend = agent.tools[0].backend
            backend.context.remote_stream_handler(
                "shell",
                ToolStreamChunk(chunk_type="stdout", data="hello"),
                None,
            )
            return "done"

        port = _free_port()
        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}",
            chat_behavior=chat_behavior,
            load_tools=lambda backend: [SimpleNamespace(name="shell", backend=backend)],
        )
        ctx = runner.initialize()
        try:
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "stream"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url,
                peer_token,
                start_body["chat_id"],
            )

            assert not any(event["type"] == "tool_call_stream" for event in events)
            protocol_events = [
                event for event in events if event["type"] == "tool_call_protocol_error"
            ]
            assert protocol_events
            assert protocol_events[0]["payload"]["code"] == "REMOTE_TOOL_CALL_ID_REQUIRED"
            assert protocol_events[0]["payload"]["tool_name"] == "shell"
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_chat_cancel_denies_pending_approval(self, tmp_path: Path) -> None:
        decisions: list[tuple[bool, str | None]] = []

        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            decision = agent.approval_provider.request_approval(
                ApprovalRequest(
                    tool_name="shell",
                    tool_args={"command": "gitnexus --version"},
                    tool_source="builtin_tool",
                    reason="Tool 'shell' requires approval by policy",
                )
            )
            decisions.append((decision.approved, decision.reason))
            return "cancelled" if not decision.approved else "approved"

        port = _free_port()
        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}", chat_behavior=chat_behavior
        )
        ctx = runner.initialize()
        try:
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "run shell"},
            )

            cursor = 0
            approval_payload: dict | None = None
            deadline = time.time() + 3
            while time.time() < deadline and approval_payload is None:
                _, stream_body = _json_request(
                    "POST",
                    f"{runner._relay_http_service.base_url}/remote/chat/stream",
                    {
                        "peer_token": peer_token,
                        "chat_id": start_body["chat_id"],
                        "cursor": cursor,
                        "timeout_sec": 0.5,
                    },
                )
                cursor = stream_body["next_cursor"]
                for event in stream_body["events"]:
                    if event["type"] == "approval_request":
                        approval_payload = event["payload"]
                        break
            assert approval_payload is not None

            _, cancelled = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/cancel",
                {
                    "peer_token": peer_token,
                    "chat_id": start_body["chat_id"],
                    "reason": "user_cancelled",
                },
            )
            assert cancelled["ok"] is True

            events = _collect_stream_events(
                runner._relay_http_service.base_url,
                peer_token,
                start_body["chat_id"],
            )
            assert decisions == [(False, "user_cancelled")]
            assert any(event["type"] == "chat_cancel_requested" for event in events)
            assert any(
                event["type"] == "approval_resolved"
                and event["payload"].get("approval_id")
                == approval_payload["approval_id"]
                and event["payload"].get("decision") == "deny_once"
                and event["payload"].get("reason") == "user_cancelled"
                for event in events
            )
            assert any(event["type"] == "chat_cancelled" for event in events)
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_keeps_peer_sessions_isolated(self) -> None:
        workspace = Path(__file__).resolve().parent
        port = _free_port()

        def chat_behavior(agent: FakeAgent, prompt: str) -> str:
            time.sleep(0.15)
            return f"reply:{prompt}:{getattr(agent, 'current_session_id', '-')}"

        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}", chat_behavior=chat_behavior
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            peer_a, token_a = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace / "peer-a"),
            )
            peer_b, token_b = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace / "peer-b"),
            )

            starts: dict[str, dict] = {}

            def start_chat(label: str, token: str) -> None:
                _, body = _json_request(
                    "POST",
                    f"{runner._relay_http_service.base_url}/remote/chat/start",
                    {"peer_token": token, "prompt": label},
                )
                starts[label] = body

            t1 = threading.Thread(target=start_chat, args=("alpha", token_a))
            t2 = threading.Thread(target=start_chat, args=("beta", token_b))
            t1.start()
            t2.start()
            t1.join(timeout=3)
            t2.join(timeout=3)

            events_a = _collect_stream_events(
                runner._relay_http_service.base_url, token_a, starts["alpha"]["chat_id"]
            )
            events_b = _collect_stream_events(
                runner._relay_http_service.base_url, token_b, starts["beta"]["chat_id"]
            )

            ready_a = [
                event["payload"]
                for event in events_a
                if event["type"] == "remote_peer_ready"
            ][0]
            ready_b = [
                event["payload"]
                for event in events_b
                if event["type"] == "remote_peer_ready"
            ][0]
            end_a = [event for event in events_a if event["type"] == "chat_end"][-1]
            end_b = [event for event in events_b if event["type"] == "chat_end"][-1]

            assert ready_a["peer_id"] == peer_a
            assert ready_b["peer_id"] == peer_b
            assert peer_b not in ready_a["fingerprint"]
            assert peer_a not in ready_b["fingerprint"]
            assert end_a["payload"]["response"].startswith("reply:alpha:")
            assert end_b["payload"]["response"].startswith("reply:beta:")
            assert end_a["payload"]["response"] != end_b["payload"]["response"]
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_sets_remote_runtime_working_directory(
        self, tmp_path: Path
    ) -> None:
        port = _free_port()

        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            return f"cwd:{getattr(agent, 'runtime_working_directory', '<missing>')}"

        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{port}", chat_behavior=chat_behavior
        )
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "hello"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            end_event = [event for event in events if event["type"] == "chat_end"][-1]
            assert end_event["payload"]["response"] == f"cwd:{tmp_path}"
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_uses_peer_registered_runtime_context(
        self, tmp_path: Path
    ) -> None:
        captured: dict[str, object] = {}

        def chat_behavior(agent: FakeAgent, _prompt: str) -> str:
            captured["target"] = getattr(agent, "runtime_execution_target", None)
            captured["context"] = getattr(agent, "runtime_peer_context", None)
            return "ok"

        runner = _build_runner_with_fake_agent(
            f"127.0.0.1:{_free_port()}", chat_behavior=chat_behavior
        )
        ctx = runner.initialize()
        try:
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(tmp_path),
                host_info_min={
                    "os": "windows",
                    "arch": "amd64",
                    "shell": "bash",
                    "hostname": "peer-box",
                },
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "hello"},
            )
            _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )

            assert captured["target"] == "remote_peer"
            peer_context = captured["context"]
            assert isinstance(peer_context, dict)
            assert peer_context["cwd"] == str(tmp_path)
            assert peer_context["workspace_root"] == str(tmp_path)
            assert peer_context["host_info_min"]["os"] == "windows"
            assert peer_context["host_info_min"]["arch"] == "amd64"
            assert peer_context["host_info_min"]["shell"] == "bash"
        finally:
            runner.cleanup(ctx.agent)

    def test_runner_stream_chat_slash_command_renders_structured_view(self) -> None:
        workspace = Path(__file__).resolve().parent
        port = _free_port()
        runner = _build_runner_with_fake_agent(f"127.0.0.1:{port}")
        ctx = runner.initialize()
        try:
            assert runner._relay_server is not None
            assert runner._relay_http_service is not None
            _, peer_token = _register_peer(
                runner._relay_http_service.base_url,
                runner._relay_server.issue_bootstrap_token(ttl_sec=60),
                str(workspace),
            )
            _, start_body = _json_request(
                "POST",
                f"{runner._relay_http_service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "/help"},
            )
            events = _collect_stream_events(
                runner._relay_http_service.base_url, peer_token, start_body["chat_id"]
            )
            view_events = [event for event in events if event["type"] == "view"]
            view_payloads = "\n".join(
                json.dumps(event["payload"], ensure_ascii=False)
                for event in view_events
            )
            terminal_outputs = [
                event["payload"]["content"]
                for event in events
                if event["type"] == "output"
                and event["payload"].get("format") == "terminal"
            ]
            merged = "\n".join(terminal_outputs)
            assert any(event["type"] == "remote_peer_ready" for event in events)
            assert "REMOTE PEER READY" not in merged
            assert view_events
            assert "Available commands" in view_payloads or "/help" in view_payloads
            assert not any(
                event["type"] == "output"
                and event["payload"].get("format") == "plain"
                and "Open view:" in event["payload"].get("content", "")
                for event in events
            )
        finally:
            runner.cleanup(ctx.agent)

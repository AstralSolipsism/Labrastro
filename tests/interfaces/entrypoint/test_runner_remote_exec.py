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

import pytest


_URLOPEN = request.build_opener(request.ProxyHandler({})).open
_SESSION_RUN_BRANCH_BINDINGS: dict[str, str] = {}

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.config.models import (
    AgentRegistryConfig,
    AuthConfig,
    AuthSuperadminConfig,
    Config,
    ContextConfig,
    DEFAULT_MAIN_CHAT_AGENT,
    DEFAULT_MAIN_CHAT_AGENT_ID,
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
from reuleauxcoder.domain.hooks.lifecycle import (
    FunctionLifecycleHookRuntimeAdapter,
    LifecycleHookDeclaration,
    LifecycleHookDispatcher,
    LifecycleHookOutput,
    LifecycleHookRegistry,
    LifecycleHookRuntimeAdapterRegistry,
)
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.llm.models import LLMResponse, ToolCall
from labrastro_server.interfaces.http.remote.service import RemoteRelayHTTPService
from labrastro_server.relay.server import RelayServer
from reuleauxcoder.infrastructure.yaml.loader import load_yaml_config, save_yaml_config
from reuleauxcoder.infrastructure.persistence.session_store import SessionStore
from reuleauxcoder.interfaces.entrypoint.runner import (
    AppDependencies,
    AppOptions,
    AppRunner,
)
from reuleauxcoder.services.config.loader import ConfigLoader, ConfigValidationError
from reuleauxcoder.interfaces.entrypoint.remote_relay import (
    _chat_locale_prompt_append,
    _runtime_config_with_chat_locale,
    _structured_ui_event_payload,
    _structured_ui_event_type,
    switch_session_model,
)
from reuleauxcoder.interfaces.events import UIEvent, UIEventBus, UIEventKind
from reuleauxcoder.services.llm.factory import resolve_model_runtime


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


SESSION_RUN_PEER_FEATURES = ["shell", "agent_runs.local_workspace", "worker_kind:local_peer"]


def test_chat_locale_prompt_append_maps_supported_locales() -> None:
    assert _chat_locale_prompt_append("zh-CN") == (
        "语言要求：所有用户可见的生成内容都必须使用简体中文，包括助手回复、"
        "过程叙述、公开展示的思考摘要，以及生成草案中的自然语言字段。"
        "JSON key、标识符、代码、命令、路径、URL、API 名称和引用的原始错误"
        "必须保持原文，不要翻译。"
    )
    assert _chat_locale_prompt_append("zh-Hans").startswith(
        "语言要求：所有用户可见的生成内容都必须使用简体中文"
    )
    assert _chat_locale_prompt_append("en") == (
        "Language: Use English for all user-visible generated content, "
        "including assistant replies, progress narration, publicly displayed "
        "reasoning/thinking summaries, and natural-language fields in generated drafts. "
        "Keep JSON keys, identifiers, code, commands, paths, URLs, API names, "
        "and quoted errors unchanged."
    )
    assert _chat_locale_prompt_append("ja").startswith("Language: Use English")
    assert _chat_locale_prompt_append(None) == ""


def test_reload_rebuilds_agent_lifecycle_dispatcher_from_config_hooks() -> None:
    from reuleauxcoder.domain.config.models import (
        SkillRegistrationConfig,
        SkillsConfig,
    )
    from reuleauxcoder.interfaces.entrypoint.remote_relay import (
        _rebuild_agent_lifecycle_dispatcher,
    )

    agent = FakeAgent(FakeLLM("fake-model"), lifecycle_dispatcher=None)
    config = SimpleNamespace(
        skills=SkillsConfig(
            items={
                "reload-audit": SkillRegistrationConfig(
                    name="reload-audit",
                    hooks=[
                        {
                            "event": "ConfigChange",
                            "handler_type": "prompt",
                            "handler_ref": "skills/reload-audit/SKILL.md",
                            "display_name": "Reload audit",
                            "summary": "Audits config reloads.",
                            "permissions": ["prompt.read"],
                            "trust": "trusted",
                        }
                    ],
                )
            }
        ),
        mcp_servers=[],
        capability_packages={},
        capability_components={},
    )

    _rebuild_agent_lifecycle_dispatcher(agent, config)

    assert agent.lifecycle_dispatcher is not None
    declarations = agent.lifecycle_dispatcher.registry.query(event="ConfigChange")
    assert [declaration.id for declaration in declarations] == [
        "hook:skill:reload-audit:ConfigChange:0"
    ]
    assert declarations[0].trust == "trusted"


def test_remote_config_change_lifecycle_context_uses_runtime_authority() -> None:
    port = _free_port()
    contexts = []

    def lifecycle_handler(_declaration, context):
        contexts.append(context)
        return LifecycleHookOutput()

    lifecycle_dispatcher = LifecycleHookDispatcher(
        LifecycleHookRegistry([
            LifecycleHookDeclaration.from_dict(
                "hook:test:config-change",
                {
                    "event": "ConfigChange",
                    "source": "admin_managed",
                    "placement": "server",
                    "handler_type": "internal",
                    "handler_ref": "config-change",
                    "display_name": "Config change",
                    "summary": "Audits config changes.",
                    "permissions": [],
                    "trust": "trusted",
                },
            )
        ]),
        runtime_adapters=LifecycleHookRuntimeAdapterRegistry([
            FunctionLifecycleHookRuntimeAdapter("internal", lifecycle_handler),
        ]),
    )
    runner = _build_runner_with_fake_agent(
        f"127.0.0.1:{port}",
        lifecycle_dispatcher=lifecycle_dispatcher,
    )
    ctx = runner.initialize()
    try:
        agent = ctx.agent
        agent.current_session_id = "session-authority"
        agent.runtime_agent_run_id = "run-authority"
        agent.runtime_turn_id = "turn-authority"
        agent.locale = "zh-CN"

        handler = runner._relay_http_service.admin_manager.config_change_handler
        assert handler is not None
        handler({
            "event_name": "PayloadEvent",
            "placement": "peer",
            "trigger_source": "admin",
            "session_run_id": "session-payload",
            "agent_run_id": "run-payload",
            "turn_id": "turn-payload",
            "origin": "payload-origin",
            "locale": "en",
            "timestamp": "payload-time",
            "metadata": {"payload": "metadata"},
            "status": "committed",
            "changed_sections": ["skills"],
        })

        config_contexts = [
            context for context in contexts if context.event_name == "ConfigChange"
        ]
        assert len(config_contexts) == 1
        context = config_contexts[0]
        assert context.event_name == "ConfigChange"
        assert context.placement == "server"
        assert context.source == "admin"
        assert context.session_run_id == "session-authority"
        assert context.agent_run_id == "run-authority"
        assert context.turn_id == "turn-authority"
        assert context.origin == "admin"
        assert context.locale == "zh-CN"
        assert context.payload["event_name"] == "ConfigChange"
        assert context.payload["placement"] == "server"
        assert context.payload["session_run_id"] == "session-authority"
        assert context.payload["agent_run_id"] == "run-authority"
        assert context.payload["turn_id"] == "turn-authority"
        assert "origin" not in context.payload
        assert "locale" not in context.payload
        assert "metadata" not in context.payload
        assert context.payload["changed_sections"] == ["skills"]
        assert context.metadata["status"] == "committed"
        assert context.metadata["changed_sections"] == ["skills"]
    finally:
        runner.cleanup(ctx.agent)


def test_runtime_config_with_chat_locale_merges_prompt_without_mutating_source() -> None:
    config = Config(prompt=PromptConfig(system_append="Use repo conventions."))

    localized = _runtime_config_with_chat_locale(config, "zh-CN")

    assert localized is not config
    assert localized is not None
    assert config.prompt.system_append == "Use repo conventions."
    assert localized.prompt.system_append == (
        "Use repo conventions.\n\n"
        "语言要求：所有用户可见的生成内容都必须使用简体中文，包括助手回复、"
        "过程叙述、公开展示的思考摘要，以及生成草案中的自然语言字段。"
        "JSON key、标识符、代码、命令、路径、URL、API 名称和引用的原始错误"
        "必须保持原文，不要翻译。"
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
        response_body = json.loads(body) if body else {}
        _remember_session_run_branch_binding(response_body)
        return resp.status, response_body


def _remember_session_run_branch_binding(payload: dict) -> None:
    session_run_id = str(payload.get("session_run_id") or "").strip()
    branch_binding_id = str(payload.get("branch_binding_id") or "").strip()
    if session_run_id and branch_binding_id:
        _SESSION_RUN_BRANCH_BINDINGS[session_run_id] = branch_binding_id


def _known_session_run_branch_binding_id(session_run_id: str) -> str:
    branch_binding_id = _SESSION_RUN_BRANCH_BINDINGS.get(str(session_run_id or "").strip(), "")
    if not branch_binding_id:
        raise AssertionError("session run branch binding proof was not captured")
    return branch_binding_id


def _session_run_events_request(base_url: str, peer_token: str, session_run_id: str) -> list[dict]:
    status, _cursor, events, _done = _session_run_events_until(
        base_url,
        peer_token,
        session_run_id,
        lambda _event: False,
        until_done=True,
    )
    assert status == 200
    return events


def _session_run_events_until(
    base_url: str,
    peer_token: str,
    session_run_id: str,
    predicate,
    *,
    until_done: bool = False,
    timeout_sec: float = 3.0,
) -> tuple[int, int, list[dict], bool]:
    payload = {
        "peer_token": peer_token,
        "session_run_id": session_run_id,
        "branch_binding_id": _known_session_run_branch_binding_id(session_run_id),
        "cursor": 0,
        "timeout_sec": 0.5,
    }
    req = request.Request(
        f"{base_url}/remote/session-runs/events",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Accept": "text/event-stream", "Content-Type": "application/json"},
        method="POST",
    )
    deadline = time.time() + timeout_sec
    events: list[dict] = []
    cursor = 0
    done = False
    with _URLOPEN(req, timeout=5) as resp:
        while time.time() < deadline:
            frame = _read_sse_frame(resp)
            if frame is None:
                break
            data = frame.get("data", {})
            batch_events = data.get("events", [])
            events.extend(batch_events)
            cursor = int(data.get("next_cursor", cursor))
            done = bool(data.get("done", False))
            if any(predicate(event) for event in batch_events):
                return resp.status, cursor, events, done
            if done and until_done:
                return resp.status, cursor, events, done
            if done:
                break
    return 200, cursor, events, done


def _read_sse_frame(resp) -> dict | None:
    lines: list[str] = []
    while True:
        line = resp.readline().decode("utf-8")
        if line == "":
            return None
        if line in {"\n", "\r\n"}:
            break
        lines.append(line)
    event = "message"
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if not line or line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    if not data_lines:
        return {"event": event, "data": {}}
    return {"event": event, "data": json.loads("\n".join(data_lines))}


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
        session_run_id: str | None = None,
        session_run_seq: int | None = None,
        source: str = "remote_session_run",
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
                "seq": session_run_seq or seq,
                "session_run_id": session_run_id,
                "session_run_seq": session_run_seq,
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
            session_run_id=session_run_id,
            session_run_seq=session_run_seq,
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
    def __init__(
        self,
        llm: FakeLLM,
        tools=None,
        chat_behavior=None,
        lifecycle_dispatcher=None,
    ) -> None:
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
        self.lifecycle_dispatcher = lifecycle_dispatcher
        self._stop_requested = False
        self._chat_behavior = chat_behavior or (lambda _agent, prompt: f"ok:{prompt}")

    def register_hook(self, hook_point, hook) -> None:
        self.hook_registry.register(hook_point, hook)

    def add_event_handler(self, handler) -> None:
        self._event_handlers.append(handler)

    def set_mode(self, mode_name: str) -> None:
        if mode_name not in self.available_modes:
            raise ValueError(f"Unknown mode: {mode_name}")
        self.active_mode = mode_name

    def chat(self, user_input: str) -> str:
        for handler in list(self._event_handlers):
            handler(AgentEvent.session_run_start(user_input))
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
    lifecycle_dispatcher=None,
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
        agent_registry=AgentRegistryConfig.from_dict(
            {"agents": {DEFAULT_MAIN_CHAT_AGENT_ID: DEFAULT_MAIN_CHAT_AGENT}}
        ),
    )
    config.skills.enabled = False
    return AppRunner(
        options=AppOptions(),
        dependencies=AppDependencies(
            load_config=lambda _: config,
            create_llm=lambda cfg: FakeLLM(resolve_model_runtime(cfg).model),
            load_tools=load_tools or (lambda _backend: []),
            create_agent=lambda llm, _tools, _config: FakeAgent(
                llm,
                tools=_tools,
                chat_behavior=chat_behavior,
                lifecycle_dispatcher=lifecycle_dispatcher,
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
    advertised_features = (
        list(features)
        if features is not None
        else ["shell", "agent_runs.local_workspace", "worker_kind:local_peer"]
    )
    _, register_body = _json_request(
        "POST",
        f"{base_url}/remote/register",
        {
            "bootstrap_token": bootstrap_token,
            "cwd": cwd,
            "workspace_root": cwd,
            "features": advertised_features,
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
    base_url: str, peer_token: str, session_run_id: str, timeout_sec: float = 3.0
) -> list[dict]:
    status, _cursor, events, done = _session_run_events_until(
        base_url,
        peer_token,
        session_run_id,
        lambda _event: False,
        until_done=True,
        timeout_sec=timeout_sec,
    )
    if status == 200 and done:
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


def test_remote_relay_maps_notification_lifecycle_audit_to_lifecycle_hook() -> None:
    event = UIEvent.info(
        "Notification audited.",
        kind=UIEventKind.AGENT,
        event_type="lifecycle_hook",
        event_name="Notification",
        payload={"message": "MCP server needs attention."},
    )

    assert _structured_ui_event_type(event) == "lifecycle_hook"
    payload = _structured_ui_event_payload(event)
    assert payload["event_name"] == "Notification"
    assert payload["payload"]["message"] == "MCP server needs attention."


def test_remote_relay_maps_session_lifecycle_audit_to_lifecycle_hook() -> None:
    ui_bus = UIEventBus()
    event = UIEvent.info(
        "Session end audited.",
        kind=UIEventKind.AGENT,
        event_type="lifecycle_hook",
        event_name="SessionEnd",
        session_run_id="session-1",
        trigger_source="session",
        source="system_builtin",
        hook_id="hook:system_builtin:session_save:observer",
        payload={"technical": {"old_hook_point": "session_save"}},
        diagnostics=[{"code": "session_end_observed"}],
    )
    ui_bus.emit(event)

    assert _structured_ui_event_type(ui_bus._history[0]) == "lifecycle_hook"
    payload = _structured_ui_event_payload(ui_bus._history[0])
    assert payload["event_name"] == "SessionEnd"
    assert payload["session_run_id"] == "session-1"
    assert payload["trigger_source"] == "session"
    assert payload["source"] == "system_builtin"
    assert payload["hook_id"] == "hook:system_builtin:session_save:observer"
    assert payload["payload"]["technical"]["old_hook_point"] == "session_save"
    assert payload["diagnostics"] == [{"code": "session_end_observed"}]


def test_remote_relay_maps_chat_mentions_to_reference_only_context() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "reuleauxcoder"
        / "interfaces"
        / "entrypoint"
        / "remote_relay.py"
    ).read_text(encoding="utf-8")

    assert '"mention_context"' in source
    assert '"reference_only": True' in source
    assert "These @ mentions are context references only and do not grant new tool permissions." in source


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
            getattr(tool.backend, "backend_id", None) == "local"
            for tool in ctx.agent.tools
        )
        runner.cleanup(ctx.agent)
        assert runner._relay_server is None

    def test_remote_host_service_binds_session_trace_sink_before_chat_handler(
        self,
        tmp_path: Path,
    ) -> None:
        session_store = MemorySessionStore()
        runner = _build_runner_with_fake_agent(
            relay_bind=f"127.0.0.1:{_free_port()}",
            session_store=session_store,
        )

        ctx = runner.initialize()
        try:
            service = runner._relay_http_service
            assert service is not None
            session = service._create_session_run(
                "peer-1",
                "session-admin",
                mode="capability_package",
                workflow_mode="capability_package_ingest",
                agent_run_id="agent-run-main",
                branch_binding_id="main",
            )
            session.enable_trace_persistence("session-admin")
            session.append_event(
                "workflow_decision",
                {
                    "branch_binding_id": "main",
                    "approval_id": "approval-1",
                    "tool_name": "install_capability_package",
                    "tool_call_id": "install-1",
                    "decision_type": "capability_package_install",
                },
            )

            events = session_store.list_trace_events("session-admin")
            assert [event["type"] for event in events] == ["workflow_decision"]
            assert events[0]["session_run_id"] == session.session_run_id
        finally:
            runner.cleanup(ctx.agent)

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

    def test_attach_mcp_uses_agent_capability_scope(self, monkeypatch) -> None:
        config = ConfigLoader()._parse_config(
            {
                "agent_registry": {
                    "agents": {
                        "main_chat": {
                            "runtime_profile": "agent_remote",
                            "capability_refs": ["review", "disabled-review"],
                            "chat_entrypoint": True,
                            "visibility": "user",
                        }
                    }
                },
                "runtime_profiles": {
                    "agent_remote": {
                        "executor": "reuleauxcoder",
                        "execution_location": "remote_server",
                    }
                },
                "capability_packages": {
                    "review": {
                        "enabled": True,
                        "components": ["mcp:github"],
                    },
                    "disabled-review": {
                        "enabled": False,
                        "components": ["mcp:disabled-github"],
                    },
                },
                "capability_components": {
                    "mcp:github": {
                        "kind": "mcp",
                        "name": "github",
                        "config": {"command": "github-mcp"},
                    },
                    "mcp:disabled-github": {
                        "kind": "mcp",
                        "name": "disabled-github",
                        "config": {"command": "disabled-github-mcp"},
                    },
                },
                "mcp": {
                    "servers": {
                        "github": {"command": "github-mcp", "placement": "server"},
                        "disabled-github": {
                            "command": "disabled-github-mcp",
                            "placement": "server",
                        },
                    }
                },
            }
        )
        runner = AppRunner(options=AppOptions())
        agent = SimpleNamespace(agent_config_id="main_chat")
        started: list[str] = []

        def fake_init_mcp(servers, _agent, _ui_bus):
            started.extend(server.name for server in servers)
            return "manager"

        monkeypatch.setattr(runner, "_init_mcp", fake_init_mcp)

        manager = runner._attach_mcp_if_configured(config, agent, None)

        assert manager == "manager"
        assert agent.mcp_manager == "manager"
        assert started == ["github"]

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

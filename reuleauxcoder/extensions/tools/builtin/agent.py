"""Agent discovery and invocation tools."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from labrastro_server.services.agent_runtime.control_plane import (
    AgentCallDispatchError,
    AgentRunRequest,
)
from reuleauxcoder.domain.agent.runtime_boundary import (
    runtime_agent_run_id,
    runtime_working_directory,
)
from reuleauxcoder.domain.agent_runtime.models import (
    AGENT_CALLABLE_SCOPES,
    AgentCallableAuthorizationStatus,
    AgentCallableExposure,
    AgentCallableProjectionEntry,
    AgentCallGrant,
    AgentRunFeedbackKind,
    AgentRunFeedbackSource,
    AgentRunFeedbackVisibility,
    AgentRunRelation,
    AgentRunRelationType,
)
from reuleauxcoder.domain.permission_gateway import PermissionAction, PermissionGateway
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool
from reuleauxcoder.extensions.tools.spec import ToolRisk


def _agent_registry(parent: object) -> dict:
    config = getattr(parent, "runtime_config", None)
    registry = getattr(config, "agent_registry", None)
    agents = getattr(registry, "agents", {}) if registry is not None else {}
    return dict(agents) if isinstance(agents, dict) else {}


def _normalize_conversation_scope(value: object) -> str:
    text = str(value or "ephemeral").strip() or "ephemeral"
    if text not in AGENT_CALLABLE_SCOPES:
        raise ValueError("conversation_scope must be ephemeral or persistent")
    return text


def _agent_permission_source(conversation_scope: str) -> str:
    return f"agent_call_{conversation_scope}"


def _agent_matches_query(agent_id: str, agent: object, terms: list[str]) -> bool:
    if not terms:
        return True
    haystack = " ".join(
        [
            str(agent_id),
            str(getattr(agent, "name", "") or ""),
            str(getattr(agent, "description", "") or ""),
            str(getattr(agent, "role", "") or ""),
        ]
    ).lower()
    return all(term in haystack for term in terms)


def _authorization_status(decision: object) -> AgentCallableAuthorizationStatus:
    action = getattr(decision, "action", None)
    if action == PermissionAction.REQUIRE_APPROVAL:
        return AgentCallableAuthorizationStatus.REQUIRES_APPROVAL
    if getattr(decision, "allowed", False):
        return AgentCallableAuthorizationStatus.ALLOWED
    return AgentCallableAuthorizationStatus.DENIED


def _agent_capability_scope(agent: object) -> dict:
    capability_refs = [
        str(item)
        for item in list(getattr(agent, "capability_refs", []) or [])
        if str(item).strip()
    ]
    scope: dict[str, object] = {}
    if capability_refs:
        scope["capability_refs"] = capability_refs
    runtime_profile = str(getattr(agent, "runtime_profile", "") or "").strip()
    if runtime_profile:
        scope["runtime_profile"] = runtime_profile
    return scope


def _target_config_version(agent: object) -> str:
    to_dict = getattr(agent, "to_dict", None)
    payload = to_dict() if callable(to_dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    payload = {
        "id": str(getattr(agent, "id", "") or ""),
        **payload,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _agent_call_user_id(parent: object) -> str:
    for name in ("runtime_user_id", "current_user_id", "user_id"):
        value = str(getattr(parent, name, "") or "").strip()
        if value:
            return value
    return "default"


def _agent_call_grant_scope(parent: object) -> str:
    workspace = runtime_working_directory(parent)
    if workspace:
        return f"workspace:{workspace}"
    session_id = str(getattr(parent, "current_session_id", "") or "").strip()
    if session_id:
        return f"session:{session_id}"
    return "global"


def _main_agent_id(parent: object) -> str:
    for name in ("agent_config_id", "main_agent_id", "runtime_agent_id"):
        value = str(getattr(parent, name, "") or "").strip()
        if value:
            return value
    return "unknown"


def _agent_call_grant_fields(
    parent: object,
    agent_id: str,
    conversation_scope: str,
) -> dict:
    agent = _agent_registry(parent).get(str(agent_id).strip())
    if agent is None:
        return {}
    return {
        "user_id": _agent_call_user_id(parent),
        "grant_scope": _agent_call_grant_scope(parent),
        "main_agent_id": _main_agent_id(parent),
        "target_agent_id": str(agent_id).strip(),
        "conversation_scope": conversation_scope,
        "capability_scope": _agent_capability_scope(agent),
        "target_config_version": _target_config_version(agent),
    }


def _find_agent_call_grant(parent: object, fields: dict) -> AgentCallGrant | None:
    control = getattr(parent, "agent_run_control_plane", None)
    finder = getattr(control, "find_agent_call_grant", None)
    if not callable(finder) or not fields:
        return None
    try:
        return finder(**fields)
    except Exception:
        return None


def _grant_is_active(grant: AgentCallGrant | None) -> bool:
    if grant is None:
        return False
    if str(grant.revoked_at or "").strip():
        return False
    expires_at = str(grant.expires_at or "").strip()
    if not expires_at:
        return True
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


def _agent_callable_scopes(agent: object) -> list[str]:
    return [
        str(item)
        for item in list(getattr(agent, "callable_scopes", []) or [])
        if str(item) in AGENT_CALLABLE_SCOPES
    ]


def _existing_threads(parent: object, agent_id: str) -> list[dict]:
    control = getattr(parent, "agent_run_control_plane", None)
    load_detail = getattr(control, "load_agent_run_detail", None)
    owner_agent_run_id = runtime_agent_run_id(parent)
    if not callable(load_detail) or not owner_agent_run_id:
        return []
    try:
        detail = load_detail(owner_agent_run_id)
    except Exception:
        return []
    bindings = (
        (detail if isinstance(detail, dict) else {}).get("agent_thread_bindings") or []
    )
    threads: list[dict] = []
    for item in list(bindings):
        if not isinstance(item, dict):
            continue
        if str(item.get("agent_id") or "") != str(agent_id):
            continue
        thread_key = str(item.get("thread_key") or "")
        threads.append(
            {
                "binding_id": str(item.get("id") or ""),
                "thread_key": thread_key,
                "summary": str(item.get("thread_summary") or thread_key),
                "status": str(item.get("status") or ""),
                "last_used_at": str(item.get("updated_at") or item.get("created_at") or ""),
                "pending_results_count": 0,
            }
        )
    return threads


def _projection_to_dict(entry: AgentCallableProjectionEntry, *, reason: str = "") -> dict:
    payload = {
        "agent_id": entry.agent_id,
        "display_name": entry.display_name,
        "summary": entry.summary,
        "exposure": entry.exposure.value,
        "callable_scopes": list(entry.callable_scopes),
        "authorization_status": entry.authorization_status.value,
        "existing_threads": [dict(item) for item in entry.existing_threads],
        "capability_scope": dict(entry.capability_scope),
        "source": entry.source,
    }
    if reason:
        payload["reason"] = reason
    return payload


def _agent_search_result(
    parent: object,
    agent_id: str,
    agent: object,
) -> dict:
    callable_scopes = _agent_callable_scopes(agent)
    authorization_status = AgentCallableAuthorizationStatus.DENIED
    reason = ""
    for scope in callable_scopes:
        decision = PermissionGateway().evaluate_agent_invocation(
            agent,
            source=_agent_permission_source(scope),
            interactive=False,
        )
        if not decision.allowed:
            reason = reason or decision.reason
            continue
        grant_fields = _agent_call_grant_fields(parent, agent_id, scope)
        grant = _find_agent_call_grant(parent, grant_fields)
        authorization_status = (
            AgentCallableAuthorizationStatus.ALLOWED
            if _grant_is_active(grant)
            else AgentCallableAuthorizationStatus.REQUIRES_APPROVAL
        )
        reason = ""
        break
    summary = str(getattr(agent, "description", "") or getattr(agent, "role", "") or "")
    projection = AgentCallableProjectionEntry(
        agent_id=agent_id,
        display_name=str(getattr(agent, "name", "") or agent_id),
        summary=summary,
        exposure=AgentCallableExposure.DEFERRED,
        callable_scopes=callable_scopes,
        authorization_status=authorization_status,
        existing_threads=_existing_threads(parent, agent_id),
        capability_scope=_agent_capability_scope(agent),
        source="agent_registry",
    )
    return _projection_to_dict(projection, reason=reason)


@register_tool
class AgentSearchTool(Tool):
    name = "agent_search"
    risk = ToolRisk.CAPABILITY
    permission_policy = "capability"
    description = (
        "Search callable Agents from the Agent Registry. Returns agent_id values "
        "for call_agent; it never returns tool_id values."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search text for agent id, name, role, or description.",
            },
            "conversation_scope": {
                "type": "string",
                "enum": ["ephemeral", "persistent"],
                "description": "Optional call scope filter.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of Agent results.",
            },
        },
        "required": ["query"],
    }

    _parent_agent = None

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def execute(
        self,
        query: str = "",
        conversation_scope: str = "",
        limit: int = 8,
    ) -> str:
        if self._parent_agent is None:
            return "Error: agent_search is not initialized with a parent Agent."
        scope_filter = ""
        if str(conversation_scope or "").strip():
            try:
                scope_filter = _normalize_conversation_scope(conversation_scope)
            except ValueError as exc:
                return f"Error: {exc}"
        try:
            max_results = max(1, min(20, int(limit or 8)))
        except (TypeError, ValueError):
            max_results = 8
        terms = [term for term in _split_terms(str(query or "").lower()) if term]
        results = []
        for agent_id, agent in sorted(_agent_registry(self._parent_agent).items()):
            callable_scopes = _agent_callable_scopes(agent)
            if scope_filter and scope_filter not in callable_scopes:
                continue
            if not callable_scopes:
                continue
            if not _agent_matches_query(agent_id, agent, terms):
                continue
            entry = _agent_search_result(self._parent_agent, agent_id, agent)
            if entry["authorization_status"] in {"allowed", "requires_approval"}:
                results.append(entry)
            if len(results) >= max_results:
                break
        return json.dumps(
            {
                "query": str(query or ""),
                "conversation_scope": scope_filter,
                "results": results,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _split_terms(value: str) -> list[str]:
    return [part for part in value.replace("_", " ").replace("-", " ").split() if part]


def _bool_argument(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _valid_thread_key(value: str) -> bool:
    return all(ch.islower() or ch.isdigit() or ch in {"-", "_"} for ch in value)


def _thread_key_from(thread_key: str) -> str:
    return str(thread_key or "").strip()


def _agent_call_error_code(error: str) -> str:
    text = str(error or "")
    for code in {
        "agent_thread_busy",
        "agent_thread_summary_mismatch",
        "invalid_agent_call_arguments",
    }:
        if code in text:
            return code
    return ""


@register_tool
class CallAgentTool(Tool):
    name = "call_agent"
    risk = ToolRisk.CAPABILITY
    permission_policy = "capability"
    description = (
        "Invoke a callable Agent by exact agent_id. conversation_scope='ephemeral' "
        "creates a temporary target AgentRun; conversation_scope='persistent' "
        "creates or reuses an Agent thread binding."
    )
    parameters = {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Exact Agent Registry id returned by agent_search.",
            },
            "request": {
                "type": "string",
                "description": "Concrete request for the target Agent.",
            },
            "conversation_scope": {
                "type": "string",
                "enum": ["ephemeral", "persistent"],
                "description": "Target conversation lifetime.",
            },
            "wait": {
                "type": "boolean",
                "description": "Whether the mainline should wait for the target result.",
            },
            "thread_key": {
                "type": "string",
                "description": "Persistent thread key. Ignored for ephemeral calls.",
            },
            "thread_summary": {
                "type": "string",
                "description": "Short summary for a new persistent thread.",
            },
        },
        "required": ["agent_id", "request"],
    }

    _parent_agent = None

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def preflight_validate(self, **kwargs) -> str | None:
        agent_id = str(kwargs.get("agent_id") or "").strip()
        request = str(kwargs.get("request") or "").strip()
        try:
            conversation_scope = _normalize_conversation_scope(
                kwargs.get("conversation_scope")
            )
        except ValueError as exc:
            return f"Error: {exc}"
        thread_key = str(kwargs.get("thread_key") or "").strip()
        thread_summary = str(kwargs.get("thread_summary") or "").strip()
        if conversation_scope == "ephemeral" and (thread_key or thread_summary):
            return (
                "Error: invalid_agent_call_arguments: thread_key and "
                "thread_summary are only valid for persistent Agent calls"
            )
        if conversation_scope == "persistent" and thread_key and not _valid_thread_key(thread_key):
            return (
                "Error: invalid_agent_call_arguments: thread_key must contain "
                "only lowercase letters, digits, '-' or '_'"
            )
        if not agent_id:
            return "Error: 'agent_id' is required."
        if not request:
            return "Error: 'request' is required."
        if self._parent_agent is None:
            return "Error: call_agent is not initialized with a parent Agent."
        if self._runtime_control_plane() is None:
            return "Error: AgentRun control plane is unavailable."
        invocation_error = self._agent_invocation_error(agent_id, conversation_scope)
        if invocation_error:
            return invocation_error
        return None

    def agent_call_grant_context(self, arguments: dict) -> dict:
        args = arguments if isinstance(arguments, dict) else {}
        agent_id = str(args.get("agent_id") or "").strip()
        try:
            conversation_scope = _normalize_conversation_scope(
                args.get("conversation_scope")
            )
        except ValueError:
            conversation_scope = "ephemeral"
        fields = _agent_call_grant_fields(
            self._parent_agent,
            agent_id,
            conversation_scope,
        )
        grant = _find_agent_call_grant(self._parent_agent, fields)
        return {
            **fields,
            "granted": _grant_is_active(grant),
        }

    def on_preflight_failed(self, *, arguments: dict, error: str) -> None:
        self._record_agent_call_failed(
            agent_id=str(arguments.get("agent_id") or ""),
            conversation_scope=str(arguments.get("conversation_scope") or "ephemeral"),
            request=str(arguments.get("request") or ""),
            wait=_bool_argument(arguments.get("wait"), True),
            thread_key=str(arguments.get("thread_key") or ""),
            error=error,
            error_code=_agent_call_error_code(error),
        )

    def execute(
        self,
        agent_id: str = "",
        request: str = "",
        conversation_scope: str = "ephemeral",
        wait: bool = True,
        thread_key: str = "",
        thread_summary: str = "",
    ) -> str:
        validation_error = self.preflight_validate(
            agent_id=agent_id,
            request=request,
            conversation_scope=conversation_scope,
            thread_key=thread_key,
            thread_summary=thread_summary,
        )
        normalized_wait = _bool_argument(wait, True)
        try:
            normalized_scope = _normalize_conversation_scope(conversation_scope)
        except ValueError:
            normalized_scope = "ephemeral"
        if validation_error:
            self._record_agent_call_failed(
                agent_id=agent_id,
                conversation_scope=normalized_scope,
                request=request,
                wait=normalized_wait,
                thread_key=thread_key,
                error=validation_error,
                error_code=_agent_call_error_code(validation_error),
            )
            return validation_error
        return self.run_backend(
            agent_id=agent_id,
            request=request,
            conversation_scope=normalized_scope,
            wait=normalized_wait,
            thread_key=thread_key,
            thread_summary=thread_summary,
        )

    @backend_handler("local")
    def _execute_local(
        self,
        agent_id: str = "",
        request: str = "",
        conversation_scope: str = "ephemeral",
        wait: bool = True,
        thread_key: str = "",
        thread_summary: str = "",
    ) -> str:
        parent = self._parent_agent
        control = self._runtime_control_plane()
        owner_agent_run_id = runtime_agent_run_id(parent)
        owner_session_run_id = str(getattr(parent, "current_session_id", "") or "")
        workspace_root = runtime_working_directory(parent)
        scope_value = _normalize_conversation_scope(conversation_scope)
        wait_value = _bool_argument(wait, True)
        stable_thread_key = _thread_key_from(thread_key)
        if scope_value == "persistent":
            try:
                run = control.call_persistent_agent(
                    owner_agent_run_id=owner_agent_run_id,
                    owner_session_run_id=owner_session_run_id,
                    agent_id=str(agent_id).strip(),
                    prompt=str(request).strip(),
                    thread_key=stable_thread_key,
                    thread_summary=str(thread_summary or "").strip(),
                    wait=wait_value,
                    workdir=str(workspace_root) if workspace_root else None,
                )
            except AgentCallDispatchError as exc:
                self._record_agent_call_failed(
                    agent_id=agent_id,
                    conversation_scope=scope_value,
                    request=request,
                    wait=wait_value,
                    thread_key=stable_thread_key,
                    error=exc.message,
                    error_code=exc.code,
                )
                return f"Error: {exc.code}: {exc.message}"
        else:
            run = control.submit_agent_run(
                AgentRunRequest(
                    agent_id=str(agent_id).strip(),
                    prompt=str(request).strip(),
                    owner_session_run_id=owner_session_run_id,
                    source="delegation",
                    workdir=str(workspace_root) if workspace_root else None,
                    relation=AgentRunRelation(
                        id="",
                        owner_agent_run_id=owner_agent_run_id,
                        related_agent_run_id="",
                        relation_type=AgentRunRelationType.AGENT_CALL_EPHEMERAL,
                        metadata={
                            "conversation_scope": scope_value,
                            "wait": wait_value,
                            **(
                                {"parent_session_id": owner_session_run_id}
                                if owner_session_run_id
                                else {}
                            ),
                            **(
                                {"workspace_root": str(workspace_root)}
                                if workspace_root
                                else {}
                            ),
                        },
                    ),
                )
            )
        self._record_agent_call_grant(
            agent_id=agent_id,
            conversation_scope=scope_value,
        )
        if wait_value:
            control.mark_agent_call_waiting(
                owner_agent_run_id,
                target_agent_run_id=run.id,
                conversation_scope=scope_value,
                thread_key=stable_thread_key if scope_value == "persistent" else "",
                wait=True,
            )
        payload = {
            "agent_run_id": run.id,
            "agent_id": run.agent_id,
            "conversation_scope": scope_value,
            "wait": wait_value,
            "source": run.source.value,
            "status": run.status.value,
        }
        if scope_value == "persistent":
            payload["thread_key"] = stable_thread_key
        return "Agent call submitted: " + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )

    def _runtime_control_plane(self):
        parent = self._parent_agent
        return getattr(parent, "agent_run_control_plane", None)

    def _has_authorized_execution_context(self) -> bool:
        context = getattr(getattr(self, "backend", None), "context", None)
        if not str(getattr(context, "current_tool_call_id", "") or "").strip():
            return False
        permission_context = getattr(context, "permission_context", {})
        if not isinstance(permission_context, dict):
            return False
        decision = permission_context.get("decision")
        if not isinstance(decision, dict):
            return False
        return bool(decision.get("authorized"))

    def _record_agent_call_grant(
        self,
        *,
        agent_id: str,
        conversation_scope: str,
    ) -> None:
        if not self._has_authorized_execution_context():
            return
        control = self._runtime_control_plane()
        writer = getattr(control, "upsert_agent_call_grant", None)
        if not callable(writer):
            return
        fields = _agent_call_grant_fields(
            self._parent_agent,
            agent_id,
            conversation_scope,
        )
        if not fields:
            return
        try:
            writer(
                AgentCallGrant(
                    **fields,
                    granted_at=datetime.now(timezone.utc).isoformat(),
                    metadata={
                        "source": "call_agent",
                        "agent_run_id": runtime_agent_run_id(self._parent_agent),
                    },
                )
            )
        except Exception:
            return

    def _record_agent_call_failed(
        self,
        *,
        agent_id: str,
        conversation_scope: str,
        request: str,
        wait: bool,
        thread_key: str,
        error: str,
        error_code: str = "",
    ) -> None:
        parent = self._parent_agent
        control = self._runtime_control_plane()
        owner_agent_run_id = runtime_agent_run_id(parent)
        if control is None or not owner_agent_run_id:
            return
        append_feedback = getattr(control, "append_agent_run_feedback", None)
        if not callable(append_feedback):
            return
        payload = {
            "agent_id": str(agent_id or "").strip(),
            "conversation_scope": str(conversation_scope or "").strip(),
            "wait": bool(wait),
            "status": "failed",
            "error": str(error or "").strip(),
            "thread_key": str(thread_key or "").strip(),
        }
        if str(error_code or "").strip():
            payload["error_code"] = str(error_code or "").strip()
        if request:
            payload["request_preview"] = str(request).strip()[:200]
        try:
            append_feedback(
                owner_agent_run_id,
                source=AgentRunFeedbackSource.SYSTEM,
                kind=AgentRunFeedbackKind.AGENT_CALL_FAILED,
                payload=payload,
                visibility=AgentRunFeedbackVisibility.INTERNAL,
                requires_activation=False,
                metadata={
                    "tool_name": self.name,
                    "conversation_scope": payload["conversation_scope"],
                },
            )
        except Exception:
            return

    def _agent_invocation_error(
        self,
        agent_id: str,
        conversation_scope: str,
    ) -> str | None:
        agent = _agent_registry(self._parent_agent).get(str(agent_id).strip())
        if agent is None:
            return f"Error: AgentConfig not found: {agent_id}"
        if conversation_scope not in set(_agent_callable_scopes(agent)):
            return (
                "Error: AgentConfig does not expose conversation_scope "
                f"{conversation_scope}: {agent_id}"
            )
        decision = PermissionGateway().evaluate_agent_invocation(
            agent,
            source=_agent_permission_source(conversation_scope),
            interactive=False,
        )
        if not decision.allowed:
            return (
                "Error: AgentConfig invocation denied by permission gateway: "
                f"{decision.reason}"
            )
        return None

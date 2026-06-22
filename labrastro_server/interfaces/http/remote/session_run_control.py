from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from labrastro_server.services.agent_runtime.session_branch_runtime import scope_id_for


SessionRunControlKind = Literal[
    "ok",
    "invalid_peer_token",
    "session_run_not_found",
    "session_run_projection_unavailable",
    "session_run_binding_store_unavailable",
    "agent_runs_unavailable",
    "session_run_bindings_unavailable",
    "branch_binding_id_required",
    "session_run_branch_binding_not_found",
    "session_run_binding_not_found",
    "session_run_binding_peer_mismatch",
    "session_run_scope_proof_invalid",
]

BindingLookupKind = Literal["ok", "not_found", "unavailable", "store_unavailable"]


@dataclass(frozen=True)
class SessionRunControlPolicy:
    branch_binding_id: str | None = None
    require_branch_binding_id: bool = True


@dataclass(frozen=True)
class SessionRunControlScopeProof:
    session_run_id: str
    branch_binding_id: str
    agent_run_id: str
    scope_id: str
    selected: bool

    @classmethod
    def from_binding(
        cls,
        *,
        session_run_id: str,
        binding: Any,
    ) -> "SessionRunControlScopeProof | None":
        resolved_session_run_id = _normalized(
            getattr(binding, "session_run_id", None)
        ) or _normalized(session_run_id)
        branch_binding_id = _normalized(getattr(binding, "branch_binding_id", None))
        agent_run_id = _normalized(getattr(binding, "agent_run_id", None))
        if not resolved_session_run_id or not branch_binding_id or not agent_run_id:
            return None
        return cls(
            session_run_id=resolved_session_run_id,
            branch_binding_id=branch_binding_id,
            agent_run_id=agent_run_id,
            scope_id=scope_id_for(resolved_session_run_id, branch_binding_id),
            selected=bool(getattr(binding, "selected", False)),
        )

    def response_fields(self) -> dict[str, Any]:
        return {
            "branch_binding_id": self.branch_binding_id,
            "agent_run_id": self.agent_run_id,
            "scope_id": self.scope_id,
            "selected": self.selected,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_run_id": self.session_run_id,
            **self.response_fields(),
        }


@dataclass(frozen=True)
class SessionRunControlResolution:
    kind: SessionRunControlKind
    peer_id: str | None = None
    session: Any | None = None
    binding: Any | None = None
    scope: SessionRunControlScopeProof | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(
        cls,
        peer_id: str,
        session: Any,
        binding: Any,
        scope: SessionRunControlScopeProof,
    ) -> "SessionRunControlResolution":
        return cls("ok", peer_id=peer_id, session=session, binding=binding, scope=scope)


@dataclass(frozen=True)
class _BindingLookup:
    kind: BindingLookupKind
    binding: Any | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, binding: Any) -> "_BindingLookup":
        return cls("ok", binding=binding)


class SessionRunControlResolver:
    def __init__(
        self,
        service: Any,
        *,
        binding_peer_matches: Callable[[Any, str | None], bool],
    ) -> None:
        self._service = service
        self._binding_peer_matches = binding_peer_matches

    def resolve(
        self,
        peer_token: str,
        session_run_id: str,
        policy: SessionRunControlPolicy,
    ) -> SessionRunControlResolution:
        peer_id = self._service.relay_server.token_manager.verify_peer_token(peer_token)
        if peer_id is None:
            return SessionRunControlResolution("invalid_peer_token")

        session = self._service._get_session_run(session_run_id)
        if session is None:
            return self._resolve_missing_projection(peer_id, session_run_id, policy)

        binding_result = self._resolve_binding(peer_id, session, policy)
        if binding_result.kind != "ok":
            return binding_result
        return binding_result

    def _resolve_missing_projection(
        self,
        peer_id: str,
        session_run_id: str,
        policy: SessionRunControlPolicy,
    ) -> SessionRunControlResolution:
        branch_id = str(policy.branch_binding_id or "").strip()
        if not branch_id and policy.require_branch_binding_id:
            return SessionRunControlResolution("branch_binding_id_required", peer_id=peer_id)

        runtime = self._service.runtime_control_plane
        if runtime is None:
            return SessionRunControlResolution("session_run_not_found")

        lookup = self._lookup_binding(
            runtime,
            session_run_id,
            branch_id or None,
        )
        if lookup.kind == "unavailable":
            return SessionRunControlResolution("session_run_bindings_unavailable", peer_id=peer_id)
        if lookup.kind == "store_unavailable":
            return SessionRunControlResolution(
                "session_run_binding_store_unavailable",
                peer_id=peer_id,
                details=lookup.details,
            )

        if lookup.kind == "not_found":
            return SessionRunControlResolution(
                "session_run_branch_binding_not_found" if branch_id else "session_run_binding_not_found",
                peer_id=peer_id,
            )
        binding = lookup.binding
        if not self._binding_peer_matches(binding, peer_id):
            return SessionRunControlResolution("session_run_binding_peer_mismatch", peer_id=peer_id)
        scope = SessionRunControlScopeProof.from_binding(
            session_run_id=session_run_id,
            binding=binding,
        )
        if scope is None:
            return SessionRunControlResolution(
                "session_run_scope_proof_invalid",
                peer_id=peer_id,
                binding=binding,
            )
        return SessionRunControlResolution(
            "session_run_projection_unavailable",
            peer_id=peer_id,
            binding=binding,
            scope=scope,
            details={
                **scope.to_dict(),
            },
        )

    def _resolve_binding(
        self,
        peer_id: str,
        session: Any,
        policy: SessionRunControlPolicy,
    ) -> SessionRunControlResolution:
        branch_id = str(policy.branch_binding_id or "").strip()
        if not branch_id and policy.require_branch_binding_id:
            return SessionRunControlResolution("branch_binding_id_required", peer_id=peer_id, session=session)

        runtime = self._service.runtime_control_plane
        if runtime is None:
            return SessionRunControlResolution("agent_runs_unavailable", peer_id=peer_id, session=session)

        lookup = self._lookup_binding(
            runtime,
            session.session_run_id,
            branch_id or None,
        )
        if lookup.kind == "unavailable":
            return SessionRunControlResolution("session_run_bindings_unavailable", peer_id=peer_id, session=session)
        if lookup.kind == "store_unavailable":
            return SessionRunControlResolution(
                "session_run_binding_store_unavailable",
                peer_id=peer_id,
                session=session,
                details=lookup.details,
            )

        if lookup.kind == "not_found":
            return SessionRunControlResolution(
                "session_run_branch_binding_not_found" if branch_id else "session_run_binding_not_found",
                peer_id=peer_id,
                session=session,
            )
        binding = lookup.binding
        if not self._binding_peer_matches(binding, peer_id):
            return SessionRunControlResolution(
                "session_run_binding_peer_mismatch",
                peer_id=peer_id,
                session=session,
                binding=binding,
            )

        sync_result = self._sync_session_bindings(runtime, session, binding)
        if sync_result is not None:
            return sync_result
        scope = SessionRunControlScopeProof.from_binding(
            session_run_id=session.session_run_id,
            binding=binding,
        )
        if scope is None:
            return SessionRunControlResolution(
                "session_run_scope_proof_invalid",
                peer_id=peer_id,
                session=session,
                binding=binding,
            )
        return SessionRunControlResolution.ok(peer_id, session, binding, scope)

    def _lookup_binding(
        self,
        runtime: Any,
        session_run_id: str,
        branch_binding_id: str | None,
    ) -> _BindingLookup:
        finder = getattr(runtime, "find_session_run_binding", None)
        if not callable(finder):
            return _BindingLookup("unavailable")
        try:
            if not branch_binding_id:
                return _BindingLookup("not_found")
            binding = finder(
                session_run_id=session_run_id,
                branch_binding_id=branch_binding_id,
                selected_only=False,
            )
        except Exception as exc:
            return _BindingLookup(
                "store_unavailable",
                details={"message": str(exc) or "SessionRun binding store is unavailable."},
            )
        if binding is None:
            return _BindingLookup("not_found")
        return _BindingLookup.ok(binding)

    def _list_bindings(self, runtime: Any, session_run_id: str) -> list[Any] | None:
        lister = getattr(runtime, "list_session_run_bindings", None)
        if not callable(lister):
            return None
        return list(lister(session_run_id=session_run_id))

    def _sync_session_bindings(
        self,
        runtime: Any,
        session: Any,
        binding: Any,
    ) -> SessionRunControlResolution | None:
        lister = getattr(runtime, "list_session_run_bindings", None)
        if callable(lister) and hasattr(session, "sync_branch_bindings"):
            try:
                session.sync_branch_bindings(
                    lister(session_run_id=session.session_run_id)
                )
            except Exception as exc:
                return SessionRunControlResolution(
                    "session_run_binding_store_unavailable",
                    details={"message": str(exc) or "SessionRun binding store is unavailable."},
                )
        elif hasattr(session, "record_branch_binding"):
            session.record_branch_binding(binding)
        return None


def _normalized(value: Any) -> str:
    return str(value or "").strip()

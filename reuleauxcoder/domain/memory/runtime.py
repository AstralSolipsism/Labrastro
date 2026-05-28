"""Runtime coordination and scope helpers for private memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reuleauxcoder.domain.memory.models import (
    MemoryBundle,
    MemoryBundleFragment,
    MemoryCaptureEvent,
    MemoryCaptureReceipt,
    MemoryMutationResult,
    MemoryProviderConfigurationError,
    MemoryProviderDiagnostic,
    MemoryProvideRequest,
    MemoryRememberItem,
    MemoryForgetSelector,
    MemoryScope,
)
from reuleauxcoder.domain.memory.registry import MemoryProviderRegistry
from reuleauxcoder.domain.memory.tool_surface import MemoryToolSurfacePolicy


MAIN_CHAT_MEMORY_NAMESPACE = "main-chat"
GLOBAL_MEMORY_PROJECT_ID = "__global__"
ACCOUNT_MEMORY_OWNER_PREFIX = "account:"


@dataclass(frozen=True, slots=True)
class MemoryAgentPolicy:
    enabled: bool = True
    primary_provider: str = ""
    read_providers: list[str] = field(default_factory=list)
    inject: bool = True
    capture: bool = True
    token_budget: int | None = None
    scope_mode: str = "isolated"
    expose_tools: bool = False

    @classmethod
    def from_dict(
        cls, data: dict[str, Any] | None, *, default_provider: str = ""
    ) -> "MemoryAgentPolicy":
        raw = data if isinstance(data, dict) else {}
        primary = str(raw.get("primary_provider") or default_provider or "").strip()
        read_raw = raw.get("read_providers")
        read_providers = (
            [str(item).strip() for item in read_raw if str(item or "").strip()]
            if isinstance(read_raw, list)
            else []
        )
        if not read_providers and primary:
            read_providers = [primary]
        token_budget_raw = raw.get("token_budget")
        token_budget = int(token_budget_raw) if token_budget_raw is not None else None
        return cls(
            enabled=bool(raw.get("enabled", True)),
            primary_provider=primary,
            read_providers=read_providers,
            inject=bool(raw.get("inject", True)),
            capture=bool(raw.get("capture", True)),
            token_budget=token_budget,
            scope_mode=str(raw.get("scope_mode") or "isolated"),
            expose_tools=bool(raw.get("expose_tools", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "enabled": self.enabled,
            "primary_provider": self.primary_provider,
            "read_providers": list(self.read_providers),
            "inject": self.inject,
            "capture": self.capture,
            "scope_mode": self.scope_mode,
            "expose_tools": self.expose_tools,
        }
        if self.token_budget is not None:
            data["token_budget"] = self.token_budget
        return data


class MemoryRuntime:
    """Coordinate memory provider adapters for context and capture flows."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        provider_registry: MemoryProviderRegistry | None = None,
        default_provider: str = "",
        default_agent_id: str = "core",
        default_namespace: str = "",
        inject_default: bool = True,
        capture_default: bool = True,
        token_budget_default: int = 800,
        fail_mode: str = "open",
        trace_enabled: bool = True,
        trust_policy: str = "wrap_external",
        tool_surface_policy: MemoryToolSurfacePolicy | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.provider_registry = provider_registry or MemoryProviderRegistry()
        self.default_provider = str(default_provider or "").strip()
        self.default_agent_id = str(default_agent_id or "core").strip() or "core"
        self.default_namespace = str(default_namespace or "").strip()
        self.inject_default = bool(inject_default)
        self.capture_default = bool(capture_default)
        self.token_budget_default = max(1, int(token_budget_default or 800))
        self.fail_mode = str(fail_mode or "open").strip() or "open"
        self.trace_enabled = bool(trace_enabled)
        self.trust_policy = str(trust_policy or "wrap_external").strip()
        self.tool_surface_policy = tool_surface_policy or MemoryToolSurfacePolicy()

    @classmethod
    def from_config(cls, config: Any) -> "MemoryRuntime":
        memory_config = getattr(config, "memory", None)
        enabled = bool(getattr(memory_config, "enabled", False))
        providers = {
            str(provider_id): provider.to_dict()
            if hasattr(provider, "to_dict")
            else dict(provider)
            for provider_id, provider in getattr(memory_config, "providers", {}).items()
        }
        runtime_config = getattr(memory_config, "runtime", None)
        tools_config = getattr(memory_config, "tools", None)
        runtime = cls(
            enabled=enabled,
            provider_registry=MemoryProviderRegistry(providers),
            default_provider=str(getattr(memory_config, "default_provider", "") or ""),
            default_agent_id=str(getattr(memory_config, "default_agent_id", "core") or "core"),
            default_namespace=str(getattr(memory_config, "default_namespace", "") or ""),
            inject_default=bool(getattr(runtime_config, "inject_default", True)),
            capture_default=bool(getattr(runtime_config, "capture_default", True)),
            token_budget_default=int(
                getattr(runtime_config, "token_budget_default", 800) or 800
            ),
            fail_mode=str(getattr(runtime_config, "fail_mode", "open") or "open"),
            trace_enabled=bool(getattr(runtime_config, "trace_enabled", True)),
            trust_policy=str(
                getattr(runtime_config, "trust_policy", "wrap_external")
                or "wrap_external"
            ),
            tool_surface_policy=MemoryToolSurfacePolicy(
                enabled=bool(getattr(tools_config, "enabled", False)),
                provider=str(getattr(tools_config, "provider", "") or ""),
                allowed_agents=list(getattr(tools_config, "allowed_agents", [])),
                recall=bool(getattr(tools_config, "recall", False)),
                remember=bool(getattr(tools_config, "remember", False)),
                forget=bool(getattr(tools_config, "forget", False)),
                list=bool(getattr(tools_config, "list", False)),
            ),
        )
        if enabled:
            runtime.validate_ready()
        return runtime

    def validate_ready(self) -> None:
        if not self.enabled:
            return
        if not self.default_provider:
            raise MemoryProviderConfigurationError(
                "memory.default_provider is required when memory.enabled is true"
            )
        self.provider_registry.validate_provider_config(self.default_provider)
        provider = self.provider_registry.provider(self.default_provider)
        if self.inject_default and not provider.capabilities.provide:
            raise MemoryProviderConfigurationError(
                f"memory provider '{self.default_provider}' does not support provide"
            )
        if self.capture_default and not provider.capabilities.capture:
            raise MemoryProviderConfigurationError(
                f"memory provider '{self.default_provider}' does not support capture"
            )

    def resolve_policy(self, raw_policy: dict[str, Any] | None = None) -> MemoryAgentPolicy:
        policy = MemoryAgentPolicy.from_dict(
            raw_policy,
            default_provider=self.default_provider,
        )
        if not policy.primary_provider and self.default_provider:
            policy = MemoryAgentPolicy(
                enabled=policy.enabled,
                primary_provider=self.default_provider,
                read_providers=[self.default_provider],
                inject=policy.inject,
                capture=policy.capture,
                token_budget=policy.token_budget,
                scope_mode=policy.scope_mode,
                expose_tools=policy.expose_tools,
            )
        return policy

    def provide_for_llm_request(
        self,
        scope: MemoryScope,
        request: MemoryProvideRequest,
        *,
        policy: MemoryAgentPolicy | dict[str, Any] | None = None,
    ) -> MemoryBundle:
        resolved_policy = (
            self.resolve_policy(policy)
            if isinstance(policy, dict)
            else policy or self.resolve_policy()
        )
        if not self.enabled or not resolved_policy.enabled or not resolved_policy.inject:
            return MemoryBundle(scope=scope, provenance={"status": "disabled"})
        read_providers = list(resolved_policy.read_providers)
        if not read_providers and resolved_policy.primary_provider:
            read_providers = [resolved_policy.primary_provider]
        if not read_providers:
            raise MemoryProviderConfigurationError("memory read provider is required")
        token_budget = resolved_policy.token_budget or request.token_budget
        request = MemoryProvideRequest(
            query=request.query,
            token_budget=max(1, int(token_budget or self.token_budget_default)),
            limit=request.limit,
            source_kind_filter=request.source_kind_filter,
        )
        fragments: list[MemoryBundleFragment] = []
        diagnostics: list[MemoryProviderDiagnostic] = []
        warnings: list[str] = []
        for provider_id in read_providers:
            try:
                provider = self.provider_registry.provider(provider_id)
                if not provider.capabilities.provide:
                    diagnostics.append(
                        MemoryProviderDiagnostic(
                            provider_id=provider_id,
                            severity="warning",
                            code="capability_missing",
                            message="provider does not support provide",
                        )
                    )
                    continue
                bundle = provider.provide(scope, request)
                fragments.extend(self._normalize_fragments(bundle.fragments, provider_id))
                diagnostics.extend(bundle.diagnostics)
                warnings.extend(bundle.warnings)
            except MemoryProviderConfigurationError:
                raise
            except Exception as exc:
                if self.fail_mode == "closed":
                    raise
                diagnostic = self._provider_unavailable_diagnostic(provider_id, exc)
                diagnostics.append(diagnostic)
                warnings.append(diagnostic.message)
        selected = self._fit_budget(self._sort_fragments(fragments), request.token_budget)
        return MemoryBundle(
            scope=scope,
            fragments=selected,
            token_estimate=sum(fragment.token_estimate for fragment in selected),
            provenance={
                "providers": read_providers,
                "fail_mode": self.fail_mode,
                "trace_enabled": self.trace_enabled,
            },
            diagnostics=diagnostics,
            warnings=warnings,
        )

    def capture_event(
        self,
        scope: MemoryScope,
        event: MemoryCaptureEvent,
        *,
        policy: MemoryAgentPolicy | dict[str, Any] | None = None,
    ) -> MemoryCaptureReceipt | None:
        resolved_policy = (
            self.resolve_policy(policy)
            if isinstance(policy, dict)
            else policy or self.resolve_policy()
        )
        if not self.enabled or not resolved_policy.enabled or not resolved_policy.capture:
            return None
        provider_id = resolved_policy.primary_provider or self.default_provider
        if not provider_id:
            raise MemoryProviderConfigurationError("memory primary provider is required")
        try:
            provider = self.provider_registry.provider(provider_id)
            if not provider.capabilities.capture:
                return MemoryCaptureReceipt(
                    provider_id=provider_id,
                    accepted=False,
                    status="capability_missing",
                    diagnostics=[
                        MemoryProviderDiagnostic(
                            provider_id=provider_id,
                            severity="warning",
                            code="capability_missing",
                            message="provider does not support capture",
                        )
                    ],
                )
            return provider.capture(scope, event)
        except MemoryProviderConfigurationError:
            raise
        except Exception as exc:
            if self.fail_mode == "closed":
                raise
            return self._capture_unavailable_receipt(provider_id, exc)

    def remember(
        self,
        scope: MemoryScope,
        item: MemoryRememberItem,
        *,
        policy: MemoryAgentPolicy | dict[str, Any] | None = None,
    ) -> MemoryMutationResult:
        resolved_policy = (
            self.resolve_policy(policy)
            if isinstance(policy, dict)
            else policy or self.resolve_policy()
        )
        provider_id, rejection = self._memory_tool_provider_or_rejection(
            scope,
            resolved_policy,
            "remember",
        )
        if rejection is not None:
            return rejection
        try:
            provider = self.provider_registry.provider(provider_id)
            if not provider.capabilities.remember:
                return MemoryMutationResult(
                    provider_id=provider_id,
                    accepted=False,
                    status="capability_missing",
                )
            return provider.remember(scope, item)
        except MemoryProviderConfigurationError:
            raise
        except Exception as exc:
            if self.fail_mode == "closed":
                raise
            return self._mutation_unavailable_result(provider_id, exc)

    def forget(
        self,
        scope: MemoryScope,
        selector: MemoryForgetSelector,
        *,
        policy: MemoryAgentPolicy | dict[str, Any] | None = None,
    ) -> MemoryMutationResult:
        resolved_policy = (
            self.resolve_policy(policy)
            if isinstance(policy, dict)
            else policy or self.resolve_policy()
        )
        provider_id, rejection = self._memory_tool_provider_or_rejection(
            scope,
            resolved_policy,
            "forget",
        )
        if rejection is not None:
            return rejection
        try:
            provider = self.provider_registry.provider(provider_id)
            if not provider.capabilities.forget:
                return MemoryMutationResult(
                    provider_id=provider_id,
                    accepted=False,
                    status="capability_missing",
                )
            return provider.forget(scope, selector)
        except MemoryProviderConfigurationError:
            raise
        except Exception as exc:
            if self.fail_mode == "closed":
                raise
            return self._mutation_unavailable_result(provider_id, exc)

    def _memory_tool_provider_or_rejection(
        self,
        scope: MemoryScope,
        policy: MemoryAgentPolicy,
        operation: str,
    ) -> tuple[str, MemoryMutationResult | None]:
        provider_id = self.tool_surface_policy.provider.strip()
        if (
            not self.enabled
            or not policy.enabled
            or not policy.expose_tools
            or not self.tool_surface_policy.enabled
        ):
            return provider_id, MemoryMutationResult(
                provider_id=provider_id,
                accepted=False,
                status="tool_surface_disabled",
            )
        if not self.tool_surface_policy.allows_agent(scope.owner_agent_id):
            return provider_id, MemoryMutationResult(
                provider_id=provider_id,
                accepted=False,
                status="tool_not_allowed",
            )
        operation_enabled = bool(getattr(self.tool_surface_policy, operation, False))
        if not operation_enabled:
            return provider_id, MemoryMutationResult(
                provider_id=provider_id,
                accepted=False,
                status="operation_disabled",
            )
        if not provider_id:
            raise MemoryProviderConfigurationError(
                "memory.tools.provider is required when memory tools mutate memory"
            )
        return provider_id, None

    @staticmethod
    def _provider_error_message(exc: Exception) -> str:
        return str(exc) or exc.__class__.__name__

    def _provider_unavailable_diagnostic(
        self, provider_id: str, exc: Exception
    ) -> MemoryProviderDiagnostic:
        return MemoryProviderDiagnostic(
            provider_id=provider_id,
            severity="warning",
            code="provider_unavailable",
            message=self._provider_error_message(exc),
            metadata={"error_type": exc.__class__.__name__},
        )

    def _capture_unavailable_receipt(
        self, provider_id: str, exc: Exception
    ) -> MemoryCaptureReceipt:
        return MemoryCaptureReceipt(
            provider_id=provider_id,
            accepted=False,
            status="provider_unavailable",
            diagnostics=[self._provider_unavailable_diagnostic(provider_id, exc)],
        )

    def _mutation_unavailable_result(
        self, provider_id: str, exc: Exception
    ) -> MemoryMutationResult:
        return MemoryMutationResult(
            provider_id=provider_id,
            accepted=False,
            status="provider_unavailable",
            diagnostics=[self._provider_unavailable_diagnostic(provider_id, exc)],
        )

    def _normalize_fragments(
        self, fragments: list[MemoryBundleFragment], provider_id: str
    ) -> list[MemoryBundleFragment]:
        normalized: list[MemoryBundleFragment] = []
        for fragment in fragments:
            source_provider = fragment.source_provider or provider_id
            text = fragment.text
            if self.trust_policy == "wrap_external" and fragment.trust_tier == "external":
                text = f"[External memory from {source_provider}]\n{text}"
            normalized.append(
                MemoryBundleFragment(
                    id=fragment.id,
                    text=text,
                    source_provider=source_provider,
                    source_kind=fragment.source_kind,
                    trust_tier=fragment.trust_tier,
                    score=fragment.score,
                    token_estimate=fragment.token_estimate,
                    metadata=dict(fragment.metadata),
                )
            )
        return normalized

    @staticmethod
    def _sort_fragments(
        fragments: list[MemoryBundleFragment],
    ) -> list[MemoryBundleFragment]:
        return sorted(fragments, key=lambda fragment: fragment.score, reverse=True)

    @staticmethod
    def _fit_budget(
        fragments: list[MemoryBundleFragment], token_budget: int
    ) -> list[MemoryBundleFragment]:
        budget = max(0, int(token_budget or 0))
        if budget <= 0:
            return []
        selected: list[MemoryBundleFragment] = []
        used = 0
        for fragment in fragments:
            estimate = max(0, int(fragment.token_estimate or 0))
            if selected and used + estimate > budget:
                break
            if estimate > budget and not selected:
                continue
            selected.append(fragment)
            used += estimate
        return selected


def memory_metadata_from_agent(agent: Any) -> dict[str, Any]:
    """Extract memory scope metadata from a ReuleauxCoder core agent."""

    config = getattr(agent, "runtime_config", None) or getattr(agent, "config", None)
    memory_config = getattr(config, "memory", None)
    default_agent_id = getattr(memory_config, "default_agent_id", "core")
    owner = (
        getattr(agent, "memory_owner_agent_id", None)
        or getattr(agent, "agent_id", None)
        or default_agent_id
        or ""
    )
    namespace = (
        getattr(agent, "memory_namespace", None)
        or getattr(memory_config, "default_namespace", "")
        or owner
    )
    workspace_id = getattr(agent, "memory_workspace_id", None) or getattr(
        agent, "workspace_id", None
    )
    if not workspace_id and not getattr(agent, "memory_disable_workspace_fallback", False):
        runtime_cwd = getattr(agent, "runtime_working_directory", None)
        workspace_id = str(Path(str(runtime_cwd)).resolve()) if runtime_cwd else ""
    values = {
        "owner_agent_id": owner,
        "memory_namespace": namespace,
        "project_id": getattr(agent, "memory_project_id", None)
        or getattr(agent, "project_id", None)
        or "",
        "workspace_id": workspace_id,
        "repo_id": getattr(agent, "memory_repo_id", None)
        or getattr(agent, "repo_id", None)
        or "",
        "goal_id": getattr(agent, "memory_goal_id", None)
        or getattr(agent, "goal_id", None)
        or "",
        "task_id": getattr(agent, "memory_task_id", None)
        or getattr(agent, "task_id", None)
        or "",
        "session_id": getattr(agent, "current_session_id", None) or "",
        "sensitivity": getattr(agent, "memory_sensitivity", None) or "",
    }
    result: dict[str, Any] = {
        key: str(value) for key, value in values.items() if str(value or "").strip()
    }
    agent_config_id = (
        getattr(agent, "agent_config_id", None)
        or getattr(agent, "main_agent_id", None)
        or getattr(agent, "runtime_agent_id", None)
    )
    agents = getattr(getattr(config, "agent_registry", None), "agents", {}) or {}
    agent_config = agents.get(agent_config_id) if isinstance(agents, dict) else None
    memory_policy = getattr(agent_config, "memory", None)
    if memory_policy is not None and hasattr(memory_policy, "to_dict"):
        policy = memory_policy.to_dict()
        if policy:
            result["memory_policy"] = policy
    return result


def bind_memory_scope_to_agent(
    agent: Any,
    *,
    owner_agent_id: str,
    memory_namespace: str | None = None,
    project_id: str | None = None,
    workspace_id: str | None = None,
    repo_id: str | None = None,
    goal_id: str | None = None,
    task_id: str | None = None,
    taskflow_id: str | None = None,
    issue_id: str | None = None,
) -> None:
    """Attach stable memory scope attributes to an in-process core agent."""

    setattr(agent, "memory_owner_agent_id", str(owner_agent_id or ""))
    setattr(agent, "memory_namespace", str(memory_namespace or owner_agent_id or ""))
    for attr, value in (
        ("memory_project_id", project_id),
        ("memory_workspace_id", workspace_id),
        ("memory_repo_id", repo_id),
        ("memory_goal_id", goal_id),
        ("memory_task_id", task_id),
        ("memory_taskflow_id", taskflow_id),
        ("memory_issue_id", issue_id),
    ):
        if value is not None:
            setattr(agent, attr, str(value))


def bind_main_chat_memory_scope_to_agent(agent: Any, *, peer_info: Any) -> bool:
    """Bind the user-facing main chat agent to account-scoped memory.

    Returns True when an authenticated account identity was available. Other
    agent types should keep using bind_memory_scope_to_agent directly.
    """

    meta = getattr(peer_info, "meta", None)
    principal = meta.get("auth_principal") if isinstance(meta, dict) else None
    if not isinstance(principal, dict):
        return False
    user_id = str(principal.get("user_id") or "").strip()
    if not user_id:
        return False
    bind_memory_scope_to_agent(
        agent,
        owner_agent_id=f"{ACCOUNT_MEMORY_OWNER_PREFIX}{user_id}",
        memory_namespace=MAIN_CHAT_MEMORY_NAMESPACE,
        project_id=GLOBAL_MEMORY_PROJECT_ID,
        workspace_id="",
        repo_id="",
    )
    setattr(agent, "memory_disable_workspace_fallback", True)
    setattr(agent, "memory_scope_kind", "main_chat_account")
    setattr(agent, "memory_account_user_id", user_id)
    username = str(principal.get("username") or "").strip()
    if username:
        setattr(agent, "memory_account_username", username)
    device_id = str(principal.get("device_id") or "").strip()
    if device_id:
        setattr(agent, "memory_account_device_id", device_id)
    return True

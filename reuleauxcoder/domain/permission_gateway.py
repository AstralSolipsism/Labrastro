"""Unified runtime permission gateway for Agent tool and task decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from reuleauxcoder.domain.agent_runtime.models import AgentConfig
from reuleauxcoder.domain.approval_engine import (
    ApprovalPolicyEngine,
    ToolApprovalContext,
    ToolSource,
)
from reuleauxcoder.domain.config.models import ApprovalConfig, ApprovalRuleConfig
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.extensions.tools.policies import DEFAULT_TOOL_POLICIES, ToolPolicy


class PermissionAction(str, Enum):
    """Canonical runtime permission outcomes."""

    ALLOW = "allow"
    WARN = "warn"
    REQUIRE_APPROVAL = "require_approval"
    BLOCKED_REVIEW = "blocked_review"
    DENY = "deny"


@dataclass(slots=True)
class PermissionSubject:
    """Actor and runtime context for one permission decision."""

    agent_id: str = ""
    role: str = ""
    visibility: str = "user"
    trigger_source: str = "manual"
    interactive: bool = False
    runtime_profile_id: str = ""
    session_id: str | None = None
    task_id: str | None = None
    workspace_root: str | None = None


@dataclass(slots=True)
class PermissionTarget:
    """Resource being invoked by an Agent."""

    kind: str
    name: str = ""
    tool_source: str = "unknown"
    registry_path: str = ""
    component_id: str = ""
    mcp_server: str | None = None
    mcp_tool: str | None = None
    target_agent_id: str | None = None


@dataclass(slots=True)
class PermissionRequest:
    """All policy inputs required to decide one runtime action."""

    subject: PermissionSubject
    target: PermissionTarget
    action: str = "execute"
    tool_call: ToolCall | None = None
    effective_capabilities: dict[str, Any] = field(default_factory=dict)
    approval: ApprovalConfig | None = None
    runtime_profile: dict[str, Any] = field(default_factory=dict)
    agent_config: AgentConfig | None = None
    target_agent_config: AgentConfig | None = None
    enforce_effective_capabilities: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PermissionDecision:
    """Resolved permission decision with explainable provenance."""

    action: PermissionAction
    authorized: bool
    reason: str = ""
    warning: str = ""
    capability_matched: str = ""
    policy_matched: str = ""
    approval_action: str = ""
    approval_rule: ApprovalRuleConfig | None = None
    audit: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.action in {
            PermissionAction.ALLOW,
            PermissionAction.WARN,
            PermissionAction.REQUIRE_APPROVAL,
        }

    @property
    def requires_approval(self) -> bool:
        return self.action == PermissionAction.REQUIRE_APPROVAL

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "action": self.action.value,
            "authorized": self.authorized,
        }
        if self.reason:
            result["reason"] = self.reason
        if self.warning:
            result["warning"] = self.warning
        if self.capability_matched:
            result["capability_matched"] = self.capability_matched
        if self.policy_matched:
            result["policy_matched"] = self.policy_matched
        if self.approval_action:
            result["approval_action"] = self.approval_action
        if self.audit:
            result["audit"] = dict(self.audit)
        return result


class PermissionGateway:
    """Single authority for runtime permission decisions."""

    def __init__(
        self,
        *,
        hard_policies: tuple[ToolPolicy, ...] | None = None,
    ) -> None:
        self.hard_policies = hard_policies or DEFAULT_TOOL_POLICIES

    def evaluate(self, request: PermissionRequest) -> PermissionDecision:
        """Resolve a tool/resource invocation decision."""

        warnings: list[str] = []
        hard_decision = self._evaluate_hard_policies(request)
        if hard_decision is not None:
            if hard_decision.action == PermissionAction.WARN:
                warnings.append(hard_decision.warning or hard_decision.reason)
            elif hard_decision.action != PermissionAction.ALLOW:
                return hard_decision

        agent_decision = self._evaluate_agent_boundary(request)
        if agent_decision.action != PermissionAction.ALLOW:
            return agent_decision

        mode_decision = self._evaluate_mode_policy(request)
        if mode_decision is not None:
            return mode_decision

        capability_matched = ""
        if request.enforce_effective_capabilities:
            capability_matched = self._capability_match(request)
            if not capability_matched:
                return PermissionDecision(
                    action=PermissionAction.DENY,
                    authorized=False,
                    reason=(
                        f"{request.target.kind} '{request.target.name}' is not "
                        "authorized by this Agent's effective_capabilities"
                    ),
                    audit=self._audit(request),
                )

        policy_decision = self._evaluate_execution_policy(
            request,
            capability_matched=capability_matched,
            warnings=warnings,
        )
        if policy_decision is not None:
            return policy_decision

        approval_decision = self._evaluate_approval(
            request,
            capability_matched=capability_matched,
            warnings=warnings,
        )
        if approval_decision is not None:
            return approval_decision

        runtime_decision = self._evaluate_runtime_profile_default(
            request,
            capability_matched=capability_matched,
            warnings=warnings,
        )
        if runtime_decision is not None:
            return runtime_decision

        if warnings:
            return PermissionDecision(
                action=PermissionAction.WARN,
                authorized=True,
                warning="; ".join(item for item in warnings if item),
                capability_matched=capability_matched,
                audit=self._audit(request),
            )
        return PermissionDecision(
            action=PermissionAction.ALLOW,
            authorized=True,
            capability_matched=capability_matched,
            audit=self._audit(request),
        )

    def evaluate_agent_invocation(
        self,
        agent_config: AgentConfig,
        *,
        source: str,
        interactive: bool,
    ) -> PermissionDecision:
        """Resolve whether a configured Agent may be invoked by a source."""

        subject = PermissionSubject(
            agent_id=agent_config.id,
            visibility=agent_config.visibility,
            trigger_source=source,
            interactive=interactive,
        )
        return self._evaluate_agent_config(agent_config, subject)

    def _evaluate_hard_policies(
        self, request: PermissionRequest
    ) -> PermissionDecision | None:
        tool_call = request.tool_call
        if tool_call is None:
            return None
        for policy in self.hard_policies:
            decision = policy.evaluate(tool_call)
            if decision is None:
                continue
            if not decision.allowed:
                return PermissionDecision(
                    action=PermissionAction.DENY,
                    authorized=False,
                    reason=decision.reason or "blocked by hard tool policy",
                    policy_matched="system_hard_deny",
                    audit=self._audit(request),
                )
            if decision.requires_approval:
                return self._approval_or_background_block(
                    request,
                    reason=decision.reason or "requires approval by hard tool policy",
                    policy_matched="system_hard_policy",
                )
            if decision.warning:
                return PermissionDecision(
                    action=PermissionAction.WARN,
                    authorized=True,
                    warning=decision.warning,
                    policy_matched="system_hard_policy",
                    audit=self._audit(request),
                )
        return None

    def _evaluate_agent_boundary(
        self, request: PermissionRequest
    ) -> PermissionDecision:
        if request.agent_config is None:
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)
        return self._evaluate_agent_config(request.agent_config, request.subject)

    def _evaluate_mode_policy(
        self, request: PermissionRequest
    ) -> PermissionDecision | None:
        mode = str(request.metadata.get("active_mode") or "").strip()
        if not mode:
            return None
        mode_tools = _string_set(request.metadata.get("mode_tools"))
        if not mode_tools or "*" in mode_tools or request.target.name in mode_tools:
            return None
        suggested = _string_set(request.metadata.get("suggested_modes"))
        suggestion_text = ""
        if suggested:
            suggestion_text = (
                " Ask user to switch mode first: "
                + ", ".join(f"/mode switch {name}" for name in sorted(suggested))
            )
        return PermissionDecision(
            action=PermissionAction.DENY,
            authorized=False,
            reason=(
                f"Tool '{request.target.name}' is not available in current mode "
                f"'{mode}'.{suggestion_text}"
            ),
            policy_matched="mode.tool_whitelist",
            audit=self._audit(
                request,
                policy={
                    "mode": mode,
                    "tools": sorted(mode_tools),
                    "suggested_modes": sorted(suggested),
                },
            ),
        )

    def _evaluate_agent_config(
        self,
        agent_config: AgentConfig,
        subject: PermissionSubject,
    ) -> PermissionDecision:
        source = _source_flow(subject.trigger_source)
        if agent_config.visibility != "user":
            if not agent_config.allows_system_flow(source):
                return PermissionDecision(
                    action=PermissionAction.DENY,
                    authorized=False,
                    reason=(
                        f"agent '{agent_config.id}' is restricted to system flow "
                        f"{agent_config.system_flow_only}; source '{source}' is not allowed"
                    ),
                    policy_matched="agent.system_flow_only",
                    audit={
                        "agent_id": agent_config.id,
                        "source": source,
                        "interactive": subject.interactive,
                    },
                )
            return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

        if source == "taskflow" and not agent_config.can_run_taskflow:
            return PermissionDecision(
                action=PermissionAction.DENY,
                authorized=False,
                reason=f"agent '{agent_config.id}' is not taskflow eligible",
                policy_matched="agent.taskflow_eligible",
                audit={"agent_id": agent_config.id, "source": source},
            )
        if source == "delegation" and not agent_config.can_delegate:
            return PermissionDecision(
                action=PermissionAction.DENY,
                authorized=False,
                reason=f"agent '{agent_config.id}' is not delegable",
                policy_matched="agent.delegable",
                audit={"agent_id": agent_config.id, "source": source},
            )
        return PermissionDecision(action=PermissionAction.ALLOW, authorized=True)

    def _capability_match(self, request: PermissionRequest) -> str:
        effective = request.effective_capabilities or {}
        target = request.target
        candidates = _target_candidates(target)
        tools = _string_set(effective.get("tools"))
        builtin_tools = _string_set(effective.get("builtin_tools"))
        if target.tool_source in {"builtin", "builtin_tool"} or target.kind in {
            "builtin",
            "builtin_tool",
            "tool",
        }:
            for candidate in candidates:
                if candidate in tools:
                    return candidate
            if target.name in builtin_tools:
                return f"builtin_tool:{target.name}"
            for value in tools:
                if _tool_name_from_registry_path(value) == target.name:
                    return value
            return ""

        if target.tool_source == "mcp" or target.kind in {"mcp", "mcp_tool"}:
            servers = _string_set(effective.get("mcp_servers"))
            mcp_tools = _string_set(effective.get("mcp_tools"))
            if target.mcp_server and target.mcp_server in servers:
                return f"mcp:{target.mcp_server}"
            if target.name in mcp_tools:
                return f"mcp_tool:{target.name}"
            if target.mcp_tool and target.mcp_tool in mcp_tools:
                return f"mcp_tool:{target.mcp_tool}"
            for candidate in candidates:
                if candidate in tools:
                    return candidate
            return ""

        if (
            target.kind == "environment_requirement"
            or target.tool_source == "environment_requirement"
        ):
            requirement_match = _executable_requirement_match(
                effective.get("environment_requirements"),
                target.name,
            )
            if requirement_match:
                return requirement_match
            for candidate in candidates:
                if candidate in tools:
                    return candidate
            return ""

        if target.kind == "skill":
            skills = _string_set(effective.get("skills"))
            return f"skill:{target.name}" if target.name in skills else ""

        for candidate in candidates:
            if candidate in tools:
                return candidate
        return ""

    def _evaluate_execution_policy(
        self,
        request: PermissionRequest,
        *,
        capability_matched: str,
        warnings: list[str],
    ) -> PermissionDecision | None:
        policy = self._matching_execution_policy(request)
        if policy is None:
            return None
        policy_value = str(policy.get("policy") or "").strip().lower()
        policy_label = f"execution_policy:{policy_value or 'inherit'}"
        if policy_value == "deny":
            return PermissionDecision(
                action=PermissionAction.DENY,
                authorized=False,
                reason=f"{request.target.name} denied by execution policy",
                capability_matched=capability_matched,
                policy_matched=policy_label,
                audit=self._audit(request, policy=policy),
            )
        if policy_value == "allow":
            return PermissionDecision(
                action=PermissionAction.ALLOW,
                authorized=True,
                capability_matched=capability_matched,
                policy_matched=policy_label,
                audit=self._audit(request, policy=policy),
            )
        if policy_value == "require_user":
            return self._approval_or_background_block(
                request,
                reason=f"{request.target.name} requires user review by execution policy",
                capability_matched=capability_matched,
                policy_matched=policy_label,
                policy=policy,
            )
        if policy_value == "escalate":
            warnings.append(f"{request.target.name} matched escalate execution policy")
            return None
        return None

    def _evaluate_approval(
        self,
        request: PermissionRequest,
        *,
        capability_matched: str,
        warnings: list[str],
    ) -> PermissionDecision | None:
        if request.approval is None:
            return None
        match = ApprovalPolicyEngine(request.approval).evaluate(
            ToolApprovalContext(
                tool_call=request.tool_call
                or ToolCall(id="permission-preview", name=request.target.name, arguments={}),
                tool_name=request.target.name,
                tool_source=_approval_tool_source(request.target),
                mcp_server=request.target.mcp_server,
                profile=request.subject.runtime_profile_id or None,
            )
        )
        if match.action == "deny":
            return PermissionDecision(
                action=PermissionAction.DENY,
                authorized=False,
                reason=f"{request.target.name} denied by approval policy",
                capability_matched=capability_matched,
                policy_matched="approval_policy:deny",
                approval_action=match.action,
                approval_rule=match.rule,
                audit=self._audit(request),
            )
        if match.action == "warn":
            warnings.append(f"{request.target.name} matched warning approval policy")
            return PermissionDecision(
                action=PermissionAction.WARN,
                authorized=True,
                warning="; ".join(item for item in warnings if item),
                capability_matched=capability_matched,
                policy_matched="approval_policy:warn",
                approval_action=match.action,
                approval_rule=match.rule,
                audit=self._audit(request),
            )
        if match.action == "require_approval":
            return self._approval_or_background_block(
                request,
                reason=f"{request.target.name} requires approval by policy",
                capability_matched=capability_matched,
                policy_matched="approval_policy:require_approval",
            )
        if match.action == "allow":
            if warnings:
                return PermissionDecision(
                    action=PermissionAction.WARN,
                    authorized=True,
                    warning="; ".join(item for item in warnings if item),
                    capability_matched=capability_matched,
                    policy_matched="approval_policy:allow",
                    approval_action=match.action,
                    approval_rule=match.rule,
                    audit=self._audit(request),
                )
            return PermissionDecision(
                action=PermissionAction.ALLOW,
                authorized=True,
                capability_matched=capability_matched,
                policy_matched="approval_policy:allow",
                approval_action=match.action,
                approval_rule=match.rule,
                audit=self._audit(request),
            )
        return None

    def _evaluate_runtime_profile_default(
        self,
        request: PermissionRequest,
        *,
        capability_matched: str,
        warnings: list[str],
    ) -> PermissionDecision | None:
        mode = str(request.runtime_profile.get("approval_mode") or "").strip().lower()
        if mode in {"none", "auto", "autonomous", "full-auto"}:
            return None
        if mode in {"full", "manual", "strict"}:
            return self._approval_or_background_block(
                request,
                reason=f"{request.target.name} requires approval by runtime profile",
                capability_matched=capability_matched,
                policy_matched=f"runtime_profile:{mode}",
            )
        return None

    def _matching_execution_policy(
        self, request: PermissionRequest
    ) -> dict[str, Any] | None:
        policies = request.effective_capabilities.get("execution_policies", [])
        if not isinstance(policies, list):
            return None
        candidates = _target_candidates(request.target)
        for policy in policies:
            if not isinstance(policy, dict):
                continue
            target = str(policy.get("target") or "").strip()
            if target and target in candidates:
                return policy
        return None

    def _approval_or_background_block(
        self,
        request: PermissionRequest,
        *,
        reason: str,
        capability_matched: str = "",
        policy_matched: str = "",
        policy: dict[str, Any] | None = None,
    ) -> PermissionDecision:
        if request.subject.interactive:
            return PermissionDecision(
                action=PermissionAction.REQUIRE_APPROVAL,
                authorized=True,
                reason=reason,
                capability_matched=capability_matched,
                policy_matched=policy_matched,
                audit=self._audit(request, policy=policy),
            )
        return PermissionDecision(
            action=PermissionAction.BLOCKED_REVIEW,
            authorized=False,
            reason=reason,
            capability_matched=capability_matched,
            policy_matched=policy_matched,
            audit=self._audit(request, policy=policy),
        )

    @staticmethod
    def _audit(
        request: PermissionRequest,
        *,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        audit = {
            "agent_id": request.subject.agent_id,
            "source": request.subject.trigger_source,
            "interactive": request.subject.interactive,
            "target_kind": request.target.kind,
            "target_name": request.target.name,
            "tool_source": request.target.tool_source,
            "runtime_profile_id": request.subject.runtime_profile_id,
        }
        if request.subject.session_id:
            audit["session_id"] = request.subject.session_id
        if request.subject.task_id:
            audit["task_id"] = request.subject.task_id
        if request.target.mcp_server:
            audit["mcp_server"] = request.target.mcp_server
        if policy:
            audit["execution_policy"] = dict(policy)
        return audit


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _tool_name_from_registry_path(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if ":" not in text:
        return text
    prefix, suffix = text.split(":", 1)
    if prefix in {"builtin", "tool", "builtin_tool"}:
        return suffix.strip()
    return text


def _source_flow(source: str) -> str:
    value = str(source or "").strip()
    if value == "capability-ingest":
        return "capability_ingest"
    return value or "manual"


def _approval_tool_source(target: PermissionTarget) -> ToolSource:
    source = str(target.tool_source or "").strip()
    if source in {"builtin_tool", "builtin"}:
        return "builtin"
    if source == "mcp":
        return "mcp"
    if target.kind in {"mcp", "mcp_tool"}:
        return "mcp"
    if target.kind in {"builtin", "builtin_tool", "tool"}:
        return "builtin"
    return "unknown"


def _target_candidates(target: PermissionTarget) -> set[str]:
    values = {
        str(target.name or "").strip(),
        str(target.registry_path or "").strip(),
        str(target.component_id or "").strip(),
    }
    name = str(target.name or "").strip()
    kind = str(target.kind or "").strip()
    if name:
        if target.tool_source in {"builtin", "builtin_tool"} or kind in {
            "builtin",
            "builtin_tool",
            "tool",
        }:
            values.update({f"builtin:{name}", f"builtin_tool:{name}"})
        if target.tool_source == "mcp" or kind in {"mcp", "mcp_tool"}:
            values.add(f"mcp_tool:{name}")
        if kind == "environment_requirement" or target.tool_source == "environment_requirement":
            values.add(f"envreq:executable:{name}")
        if kind == "skill":
            values.add(f"skill:{name}")
    if target.mcp_server:
        values.add(f"mcp:{target.mcp_server}")
    if target.mcp_tool:
        values.add(f"mcp_tool:{target.mcp_tool}")
    return {value for value in values if value}


def _executable_requirement_match(value: object, name: str) -> str:
    target_name = str(name or "").strip()
    if not target_name or not isinstance(value, list):
        return ""
    for item in value:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").strip() != "executable":
            continue
        item_name = str(item.get("name") or "").strip()
        command = str(item.get("command") or "").strip()
        if target_name in {item_name, command}:
            return str(item.get("id") or f"envreq:executable:{item_name}")
    return ""


__all__ = [
    "PermissionAction",
    "PermissionDecision",
    "PermissionGateway",
    "PermissionRequest",
    "PermissionSubject",
    "PermissionTarget",
]

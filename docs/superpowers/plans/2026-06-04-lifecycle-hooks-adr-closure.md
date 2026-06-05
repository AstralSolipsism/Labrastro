# Lifecycle Hooks ADR Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring ADR 0006 lifecycle hook contracts into runtime, tests, and user-visible server contracts so protocol fields do not drift from execution semantics.

**Architecture:** Keep lifecycle declarations, dispatch policy, runtime adapters, permission gateway, AgentRun runtime context, and SessionRun projection as separate layers. Gate decisions must be consumed by the layer that owns the side effect: prompt gates by `Agent`, tool gates by `ToolExecutor` plus `PermissionGateway`, handler side effects by adapter permission gates.

**Tech Stack:** Python domain code, pytest regression tests, Labrastro remote admin protocol, existing lifecycle hook registry and AgentRun services.

---

### Task 1: PreToolUse ask must use approval/review semantics

**Files:**
- Modify: `reuleauxcoder/domain/agent/tool_execution.py`
- Test: `tests/domain/agent/test_tool_execution.py`

- [ ] **Step 1: Write failing tests**

Add tests proving interactive `PreToolUse decision="ask"` routes to `approval_provider`, executes only after approval, and background runs return blocked review without prompting.

Run: `.\.venv\Scripts\python -m pytest tests/domain/agent/test_tool_execution.py::test_tool_executor_routes_pre_tool_lifecycle_ask_through_approval_provider tests/domain/agent/test_tool_execution.py::test_tool_executor_blocks_background_pre_tool_lifecycle_ask_as_review -q`

Expected before implementation: FAIL because ask is returned as `lifecycle_pre_tool_denied`.

- [ ] **Step 2: Implement minimal runtime path**

Make `_apply_pre_tool_lifecycle()` return a structured result instead of a string-only error. `ask` should request approval using the final transformed tool call; interactive approval allows execution, denial returns approval-denied diagnostics, background returns blocked-review diagnostics.

- [ ] **Step 3: Verify**

Run: `.\.venv\Scripts\python -m pytest tests/domain/agent/test_tool_execution.py -q`

Expected: PASS.

### Task 2: Handler permissions must be real runtime constraints

**Files:**
- Modify: `reuleauxcoder/domain/hooks/lifecycle.py`
- Test: `tests/domain/hooks/test_lifecycle.py`

- [ ] **Step 1: Write failing tests**

Add tests proving a trusted prompt adapter with non-empty `permissions` is blocked when the agent permission context denies lifecycle prompt execution, and allowed only when that permission passes.

Run: `.\.venv\Scripts\python -m pytest tests/domain/hooks/test_lifecycle.py::test_prompt_lifecycle_runtime_adapter_respects_declared_permissions -q`

Expected before implementation: FAIL because prompt adapter calls the LLM directly.

- [ ] **Step 2: Implement permission gate**

Give `PromptLifecycleHookRuntimeAdapter` optional `agent` context, bind it in `bind_lifecycle_runtime_adapters_to_agent()`, and call a shared lifecycle permission helper before model execution. Use a stable tool name such as `lifecycle_prompt`.

- [ ] **Step 3: Verify**

Run: `.\.venv\Scripts\python -m pytest tests/domain/hooks/test_lifecycle.py -q`

Expected: PASS.

### Task 3: Output schema must not silently reinterpret control fields

**Files:**
- Modify: `reuleauxcoder/domain/hooks/lifecycle.py`
- Test: `tests/domain/hooks/test_lifecycle.py`

- [ ] **Step 1: Write failing tests**

Add tests proving `continue_flow` accepts only JSON booleans and rejects strings like `"false"`.

Run: `.\.venv\Scripts\python -m pytest tests/domain/hooks/test_lifecycle.py::test_lifecycle_hook_output_rejects_non_boolean_continue_flow -q`

Expected before implementation: FAIL because `bool("false")` is true.

- [ ] **Step 2: Implement strict parsing**

Replace `bool(data.get("continue_flow", True))` with explicit bool validation in `LifecycleHookOutput.from_dict()`.

- [ ] **Step 3: Verify**

Run: `.\.venv\Scripts\python -m pytest tests/domain/hooks/test_lifecycle.py -q`

Expected: PASS.

### Task 4: Event catalog and wired external events must be explicit

**Files:**
- Modify: `reuleauxcoder/domain/hooks/lifecycle.py`
- Test: `tests/domain/hooks/test_lifecycle.py`

- [ ] **Step 1: Write failing tests**

Add tests proving dashboard/runtime metadata separates ADR catalog events from externally wired events and that unsupported external events are visibly `external_event_unwired`.

Run: `.\.venv\Scripts\python -m pytest tests/domain/hooks/test_lifecycle.py::test_lifecycle_event_catalog_reports_external_wiring_status -q`

Expected before implementation: FAIL because the boundary is only implicit through config validation.

- [ ] **Step 2: Implement explicit status helper**

Expose `lifecycle_event_catalog_items()` or equivalent metadata containing `event`, `in_adr_catalog`, `external_config_supported`, and `runtime_status`.

- [ ] **Step 3: Verify**

Run: `.\.venv\Scripts\python -m pytest tests/domain/hooks/test_lifecycle.py -q`

Expected: PASS.

### Task 5: Stop and StopFailure contract must match documented runtime behavior

**Files:**
- Modify: `reuleauxcoder/domain/agent/agent.py`
- Test: `tests/domain/agent/test_agent_lifecycle.py`

- [ ] **Step 1: Write failing tests**

Add tests proving Stop hook terminal decisions do not silently claim continuation semantics. If `decision=defer`, `deny`, or `continue_flow=false` appears on Stop, the SessionRun emits a lifecycle diagnostic explaining terminal control is ignored for Stop.

Run: `.\.venv\Scripts\python -m pytest tests/domain/agent/test_agent_lifecycle.py::test_agent_chat_records_stop_terminal_control_as_ignored_diagnostic -q`

Expected before implementation: FAIL because Stop only collects message/artifacts without explicit diagnostic.

- [ ] **Step 2: Implement diagnostics**

Annotate Stop/StopFailure outputs with ignored control-field diagnostics, keeping them observation-only until ADR defines a bounded continue-turn mechanism.

- [ ] **Step 3: Verify**

Run: `.\.venv\Scripts\python -m pytest tests/domain/agent/test_agent_lifecycle.py -q`

Expected: PASS.

### Task 6: Trust management and visibility must be testable as one contract

**Files:**
- Modify: `labrastro_server/services/admin/service.py`
- Modify: `labrastro_server/interfaces/http/remote/protocol/registry.py`
- Test: `tests/labrastro_server/services/test_admin_service.py`
- Test: `tests/domain/session/test_document.py`

- [ ] **Step 1: Write failing tests**

Add tests proving hook dashboard views contain manageable actions for `trusted`, `disabled`, and `blocked`, and that lifecycle hook SessionRun parts expose user-safe title/message while raw technical data stays in payload.

Run: `.\.venv\Scripts\python -m pytest tests/labrastro_server/services/test_admin_service.py::test_lifecycle_hook_dashboard_exposes_management_actions tests/domain/session/test_document.py::test_lifecycle_hook_event_uses_user_safe_presentation -q`

Expected before implementation: FAIL if management actions/presentation fields are missing.

- [ ] **Step 2: Implement server-visible contract**

Add `management_actions` to hook dashboard items for non-system hooks. Keep raw command/prompt/json details under `technical` or payload, not default presentation fields.

- [ ] **Step 3: Verify**

Run: `.\.venv\Scripts\python -m pytest tests/labrastro_server/services/test_admin_service.py tests/domain/session/test_document.py -q`

Expected: PASS.

### Task 7: Regression suite

**Files:**
- Existing focused lifecycle, permission, agent, admin, session tests.

- [ ] **Step 1: Run focused suite**

Run:
`.\.venv\Scripts\python -m pytest tests/domain/hooks/test_lifecycle.py tests/domain/agent/test_tool_execution.py tests/domain/agent/test_agent_lifecycle.py tests/domain/test_permission_gateway.py tests/domain/session/test_document.py tests/labrastro_server/services/test_admin_service.py -q`

Expected: PASS.

- [ ] **Step 2: Run full suite if focused suite is green**

Run: `.\.venv\Scripts\python -m pytest -q`

Expected: PASS or report exact failures.

# Lifecycle Gate Semantics Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close lifecycle gate semantics end to end so dispatcher short-circuiting, runtime consumers, permission decisions, tool execution, history, audit, and tests all use one terminal-decision contract.

**Architecture:** Introduce one shared lifecycle gate policy module instead of letting dispatcher, ToolExecutor, Agent, and PermissionGateway each interpret decisions independently. Gate terminal decisions are `decision="deny"`, `decision="defer"`, and `continue_flow=false`; terminal outputs stop later hooks and must stop the gated action. `decision="ask"` remains a non-terminal request for approval/review and must be consumed by the existing approval or permission boundary.

**Tech Stack:** Python dataclasses, pytest, existing lifecycle hook dispatcher, ToolExecutor, Agent, PermissionGateway, AgentLoop.

---

### Task 1: Shared Gate Policy

**Files:**
- Create: `reuleauxcoder/domain/hooks/lifecycle_policy.py`
- Modify: `reuleauxcoder/domain/hooks/lifecycle.py`
- Test: `tests/domain/hooks/test_lifecycle.py`

- [ ] **Step 1: Write failing policy tests**

Add tests proving:
- `deny`, `defer`, and `continue_flow=false` are terminal gate outputs.
- `ask` is not terminal for dispatcher short-circuiting.
- Terminal outputs do not apply `updated_input` to the next hook context.

- [ ] **Step 2: Run policy tests and verify RED where behavior is not shared**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\domain\hooks\test_lifecycle.py -q
```

- [ ] **Step 3: Implement shared policy**

Create `lifecycle_policy.py` with reusable helpers for event gate detection, normalized decisions, terminal detection, and blocking-message extraction.

- [ ] **Step 4: Wire dispatcher to shared policy**

Replace local dispatcher-only helpers in `lifecycle.py` with imports from the shared policy module.

### Task 2: Gate Consumer Matrix

**Files:**
- Modify: `reuleauxcoder/domain/agent/tool_execution.py`
- Modify: `reuleauxcoder/domain/permission_gateway.py`
- Test: `tests/domain/agent/test_tool_execution.py`
- Test: `tests/domain/test_permission_gateway.py`

- [ ] **Step 1: Write failing consumer tests**

Add tests proving:
- `PreToolUse decision="defer"` blocks execution and emits no tool side effect.
- `PreToolUse continue_flow=false` blocks execution and emits no tool side effect.
- `PermissionRequest decision="defer"` resolves to a non-authorized permission decision.
- `PermissionRequest continue_flow=false` resolves to a non-authorized permission decision.

- [ ] **Step 2: Run consumer tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\domain\agent\test_tool_execution.py tests\domain\test_permission_gateway.py -q
```

- [ ] **Step 3: Implement consumer policy use**

Update `ToolExecutor._apply_pre_tool_lifecycle()` and `PermissionGateway._evaluate_lifecycle_outputs()` to use the shared terminal/blocking helpers instead of local partial checks.

### Task 3: End-To-End Drift Guard

**Files:**
- Modify: `tests/domain/agent/test_loop.py`
- Modify: `tests/domain/agent/test_tool_execution.py`
- Modify: `tests/domain/hooks/test_lifecycle.py`

- [ ] **Step 1: Ensure existing effective tool-call tests cover history, event, permission, and batch payloads**

Confirm tests cover:
- `PreToolUse updated_input.tool_call` affects actual execution.
- permission is evaluated against the transformed tool.
- tool start/end events use the transformed tool.
- PostToolBatch uses transformed calls.
- AgentLoop history stores transformed calls before tool results.

- [ ] **Step 2: Add only missing regression coverage**

Add tests only for uncovered cells in the gate matrix; do not duplicate already covered behavior.

### Task 4: Verification

**Files:**
- No production changes unless tests reveal a missing consumer.

- [ ] **Step 1: Run focused lifecycle runtime tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\domain\hooks\test_lifecycle.py tests\domain\agent\test_tool_execution.py tests\domain\test_permission_gateway.py tests\domain\agent\test_loop.py -q
```

- [ ] **Step 2: Run full suite**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

- [ ] **Step 3: Run diff hygiene check**

Run:

```powershell
git diff --check
```

- [ ] **Step 4: Close goal only after verification**

Mark the goal complete only if the full suite passes and no actionable gate-semantics drift remains from the matrix.

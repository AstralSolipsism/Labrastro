# Lifecycle AgentRun Runtime Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair lifecycle hook and AgentRun runtime adapter drift so the unified protocol also preserves AgentRun control flow, runtime boundaries, child-run projection, cancellation, cwd, and budget semantics.

**Architecture:** Keep the public protocol shape, but move execution semantics into explicit runtime helpers and dispatcher policy. Gate events run sequentially and stop on terminal outputs; runtime-bound adapters read one canonical boundary helper instead of separate legacy attrs.

**Tech Stack:** Python dataclasses, pytest, existing `LifecycleHookDispatcher`, `ToolExecutor`, `ReuleauxCoderExecutorBackend`, AgentRun control-plane services.

---

### Task 1: Lifecycle Gate Semantics

**Files:**
- Modify: `reuleauxcoder/domain/hooks/lifecycle.py`
- Modify: `reuleauxcoder/domain/agent/agent.py`
- Modify: `reuleauxcoder/domain/agent/tool_execution.py`
- Test: `tests/domain/hooks/test_lifecycle.py`
- Test: `tests/domain/agent/test_tool_execution.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:
- `LifecycleHookDispatcher.dispatch()` stops after a `UserPromptSubmit`, `PermissionRequest`, or `PreToolUse` output with `decision="deny"`, `decision="defer"`, or `continue_flow=False`.
- A second hook with a visible side effect is not called after the first hook blocks.
- `updated_input` from hook A is visible to hook B for `UserPromptSubmit` and `PreToolUse`.

- [ ] **Step 2: Verify RED**

Run:

```powershell
pytest tests/domain/hooks/test_lifecycle.py tests/domain/agent/test_tool_execution.py -q
```

Expected: the new short-circuit and sequential transform tests fail because the current dispatcher fan-outs to all matching trusted hooks before consumers inspect outputs.

- [ ] **Step 3: Implement minimal dispatcher policy**

Add a small event-policy helper in `lifecycle.py`:

```python
_LIFECYCLE_GATE_EVENTS = {"UserPromptSubmit", "PermissionRequest", "PreToolUse"}

def _lifecycle_output_is_terminal(output: LifecycleHookOutput) -> bool:
    decision = str(getattr(output, "decision", "") or "none")
    return decision in {"deny", "defer"} or getattr(output, "continue_flow", True) is False
```

Use that policy inside `LifecycleHookDispatcher.dispatch()` so gate events return immediately after terminal output.

- [ ] **Step 4: Implement sequential input application**

Apply `updated_input` to the context payload before the next hook runs for gate events. Preserve existing downstream consumers as compatibility, but make dispatch-time context the authoritative value for subsequent hooks.

- [ ] **Step 5: Verify GREEN**

Run the same pytest command and confirm all touched tests pass.

### Task 2: Runtime Boundary Helpers

**Files:**
- Create: `reuleauxcoder/domain/agent/runtime_boundary.py`
- Modify: `reuleauxcoder/domain/hooks/lifecycle.py`
- Modify: `reuleauxcoder/domain/agent/agent.py`
- Modify: `reuleauxcoder/domain/agent/tool_execution.py`
- Modify: `reuleauxcoder/domain/agent/loop.py`
- Modify: `reuleauxcoder/extensions/tools/builtin/agent.py`
- Modify: `reuleauxcoder/extensions/tools/builtin/shell.py` only if initialization helper cannot stay in `ToolExecutor`
- Test: `tests/labrastro_server/services/agent_runtime/test_executor_backend.py`
- Test: `tests/domain/hooks/test_lifecycle.py`
- Test: `tests/extensions/tools/test_agent_tool.py`
- Test: `tests/domain/agent/test_tool_execution.py`
- Test: `tests/domain/agent/test_loop.py`

- [ ] **Step 1: Write failing tests**

Add tests proving executor-bound agents with only `runtime_task_id` and `runtime_workspace_root` still:
- produce lifecycle `agent_run_id`;
- submit lifecycle delegated AgentRuns with parent/delegated ids;
- run command hooks in the AgentRun workdir;
- delegate agent tool runs with correct parent ids and workdir;
- initialize first shell command cwd from AgentRun workdir;
- render AgentLoop runtime context from AgentRun workdir.

- [ ] **Step 2: Verify RED**

Run:

```powershell
pytest tests/labrastro_server/services/agent_runtime/test_executor_backend.py tests/domain/hooks/test_lifecycle.py tests/extensions/tools/test_agent_tool.py tests/domain/agent/test_tool_execution.py tests/domain/agent/test_loop.py -q
```

Expected: new tests fail because current code reads mixed legacy attrs.

- [ ] **Step 3: Implement helper module**

Create canonical helper functions:

```python
def runtime_agent_run_id(agent: Any) -> str:
    return str(
        getattr(agent, "runtime_agent_run_id", "")
        or getattr(agent, "runtime_task_id", "")
        or getattr(agent, "runtime_agent_id", "")
        or ""
    )

def runtime_workspace_root(agent: Any) -> str:
    return str(
        getattr(agent, "runtime_workspace_root", "")
        or getattr(agent, "runtime_working_directory", "")
        or ""
    )

def runtime_working_directory(agent: Any) -> str:
    return str(
        getattr(agent, "runtime_working_directory", "")
        or getattr(agent, "runtime_workspace_root", "")
        or ""
    )
```

- [ ] **Step 4: Replace direct attr reads**

Use the helpers in lifecycle context creation, lifecycle adapters, delegate agent tool, tool executor shell slot key, shell cwd initialization, and AgentLoop runtime context.

- [ ] **Step 5: Verify GREEN**

Run the RED command and confirm it passes.

### Task 3: Executor Binding And Active Agent Lifetime

**Files:**
- Modify: `labrastro_server/services/agent_runtime/executor_backend.py`
- Test: `tests/labrastro_server/services/agent_runtime/test_executor_backend.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:
- `_bind_permission_context()` sets `runtime_agent_run_id`, `runtime_task_id`, `runtime_workspace_root`, and `runtime_working_directory`.
- `cancel(task_id)` returns `False` after a synchronous run has completed.
- running agents remain cancellable while `_agent_chat()` is blocked.

- [ ] **Step 2: Verify RED**

Run:

```powershell
pytest tests/labrastro_server/services/agent_runtime/test_executor_backend.py -q
```

Expected: binding and completed-run cancel tests fail.

- [ ] **Step 3: Implement binding and cleanup**

Set both canonical and compatibility attrs during executor binding, and remove the agent from `_active_agents` in `_run_agent()` after the run reaches a terminal result.

- [ ] **Step 4: Verify GREEN**

Run the same pytest command.

### Task 4: AgentRun Budget Contract

**Files:**
- Modify: `labrastro_server/services/agent_runtime/executor_backend.py`
- Modify: `labrastro_server/services/agent_runtime/control_plane.py`
- Modify: `labrastro_server/services/agent_runtime/postgres_store.py`
- Modify: `reuleauxcoder/domain/agent/tool_execution.py`
- Test: `tests/labrastro_server/services/agent_runtime/test_control_plane.py`
- Test: `tests/labrastro_server/services/agent_runtime/test_postgres_runtime_store.py`
- Test: `tests/domain/agent/test_tool_execution.py`

- [ ] **Step 1: Write failing tests**

Add tests proving:
- `AgentRunRequest.budget` is present on `ExecutorRunRequest`.
- `ExecutorRunRequest.to_dict()` round-trips budget.
- `max_tool_calls` blocks tool execution after the configured limit.

- [ ] **Step 2: Verify RED**

Run:

```powershell
pytest tests/labrastro_server/services/agent_runtime/test_control_plane.py tests/labrastro_server/services/agent_runtime/test_postgres_runtime_store.py tests/domain/agent/test_tool_execution.py -q
```

Expected: budget propagation and enforcement tests fail.

- [ ] **Step 3: Implement minimal budget propagation**

Add `budget: dict[str, Any]` to `ExecutorRunRequest`, populate it from task metadata in control-plane and Postgres claims, and round-trip it in `to_dict()`.

- [ ] **Step 4: Implement max_tool_calls enforcement**

Track per-run tool calls on the agent or ToolExecutor. When `max_tool_calls` is exhausted, return a tool error before permission approval or execution.

- [ ] **Step 5: Verify GREEN**

Run the RED command again.

### Task 5: Parent Projection And Cancellation Integration

**Files:**
- Modify: `reuleauxcoder/domain/hooks/lifecycle.py`
- Modify: `reuleauxcoder/extensions/tools/builtin/agent.py`
- Modify: `labrastro_server/services/agent_runtime/control_plane.py` only if projection needs store-side compatibility
- Test: `tests/domain/hooks/test_lifecycle.py`
- Test: `tests/extensions/tools/test_agent_tool.py`
- Test: `tests/labrastro_server/services/agent_runtime/test_control_plane.py`
- Test: `tests/labrastro_server/services/agent_runtime/test_postgres_runtime_store.py`

- [ ] **Step 1: Write failing integration tests**

Add tests proving delegated child runs created from lifecycle adapter and DelegateAgentTool attach to the parent, project `delegated_run_completed`, and are found by cancellation cascade.

- [ ] **Step 2: Verify RED**

Run:

```powershell
pytest tests/domain/hooks/test_lifecycle.py tests/extensions/tools/test_agent_tool.py tests/labrastro_server/services/agent_runtime/test_control_plane.py tests/labrastro_server/services/agent_runtime/test_postgres_runtime_store.py -q
```

Expected: parent id propagation fails before runtime boundary fixes.

- [ ] **Step 3: Implement compatibility**

Use canonical runtime ids everywhere child AgentRun requests are created. Keep existing metadata keys for UI/audit compatibility.

- [ ] **Step 4: Verify GREEN**

Run the RED command again.

### Task 6: Final Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused backend tests**

```powershell
pytest tests/domain/hooks/test_lifecycle.py tests/domain/agent/test_tool_execution.py tests/domain/agent/test_loop.py tests/extensions/tools/test_agent_tool.py tests/labrastro_server/services/agent_runtime/test_executor_backend.py tests/labrastro_server/services/agent_runtime/test_control_plane.py tests/labrastro_server/services/agent_runtime/test_postgres_runtime_store.py -q
```

- [ ] **Step 2: Run impacted frontend tests if backend event contract changes**

```powershell
npm test -- ChatView.context-events runtimeState sessionRunTranscriptReducer
```

- [ ] **Step 3: Review GitNexus impact after edits**

```powershell
gitnexus analyze .
gitnexus impact -r Labrastro LifecycleHookDispatcher --depth 2 --include-tests
gitnexus impact -r Labrastro AgentRunRequest --depth 2 --include-tests
```

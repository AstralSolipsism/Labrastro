# SessionRun Source Identity Resolution Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Host-side SessionRun control operations derive `source` identity from one unified target-branch resolver, so selected-branch UI operations and branch-local lifecycle work cannot borrow the wrong active run identity.

**Architecture:** Keep the 2026-06-17 and 2026-06-18 documents authoritative. Add one Host source identity resolver that proves `sessionRunId + branchBindingId + agentRunId` before any non-start operation begins. Split operation acceptance/effects into two explicit scopes: `selected-visible` for current UI commands and `branch-local` for branch-owned pending-next-turn continuation.

**Tech Stack:** TypeScript, VS Code extension Host, Vitest, existing Webview message gate, no backend protocol change. Existing branch binding / branch summary payloads must carry `agent_run_id`; if a test exposes a missing propagation path, repair that propagation instead of adding a new protocol shape or disabling branch-local continuation.

---

## Why This Exists

The current repair introduced a correct operation model, but one boundary is still not closed:

- `continueSessionRun()` still builds operation `source` from `this.sessionRunCoordinator.activeRun`.
- `applySessionRunEventsBatch()` can call `continueSessionRun()` for `pendingNextTurnForBranch(sessionRunId, streamBranchBindingId)`.
- If the user switched from branch A to branch B while branch A finished, branch A's pending next turn is continued using branch B's active source identity.
- The backend may start branch A, then Host rejects or mishandles the response because the operation source was built from branch B.

In plain language: the user sent the next message on branch A, then looked at branch B. When branch A finishes, the system tries to keep branch A going, but it uses branch B as the proof of identity. That can leave pending input, running state, and stream state out of sync.

This is not a reason to add another local `if`. The fix is to make source identity a first-class model.

## Authoritative Inputs

- `2026-06-17-sessionrun-agentrun-execution-convergence.md`
  - `pending_next_turn` is branch-local.
  - It belongs to the branch where it was created.
  - Branch switching must not carry it to another branch.
  - Selected branch transcript must not receive sibling branch output.

- `2026-06-18-sessionrun-async-correlation-architecture-repair.md`
  - Host async requests must create typed operations with exact source and target identity.
  - Awaited responses cannot patch active UI state unless the operation coordinator accepts them.
  - Webview operation errors are command failures, not selected-run terminal failures.

- `2026-06-18-sessionrun-operation-model-execution.md`
  - Operation result acceptance requires operation id/kind and source/target proof.
  - Run-level messages with `sessionRunId` require a current active run, except accepted start/bootstrap restore paths.

## Decisions Already Resolved

These are decided by the existing documents and current implementation direction. Do not reopen them during execution unless code evidence contradicts them.

1. Keep branch-local pending-next-turn auto-continue.
   - Do not change behavior to "pause pending next turn when the user switches away".
   - The correction is identity resolution, not a UX policy reversal.

2. Do not make branch-local auto-continue a global Webview pending operation.
   - A hidden branch continuing in the background must not occupy ChatView's single visible `pendingOperation`.
   - It must not block or alter the selected branch's input state.

3. Keep selected visible commands selected-only.
   - Direct `chat.send`, `steer`, `recover`, `cancel`, `branch.create`, and `branch.select` are selected-branch operations unless the existing document explicitly says otherwise.
   - Approval and user-input replies can remain explicit branch-bound replies, but they are not part of this source identity repair unless a test proves drift.

4. Do not implement hide/close/delete lifecycle APIs here.
   - Those have destructive resource semantics and remain deferred.
   - This plan only repairs source identity and operation acceptance for existing control paths.

5. Do not add backend compatibility or migration scaffolding.
   - Development-phase rule: one architecture, one model.
   - If Host cannot prove a branch's `agentRunId` from current state, fail closed and keep the branch-local pending next turn queued.

6. Branch-local auto-continue failure does not introduce a new Webview protocol.
   - Do not add `sessionRun.branchOperation.error` or any equivalent new message.
   - Do not emit `sessionRun.operation.error` for branch-local auto-continue failures that were never represented as a Webview pending operation.
   - Keep the pending next turn on its original branch, post the existing pending-next-turns snapshot for that branch, and leave the user able to retry or edit when they return to that branch.
   - Never mark the selected visible run terminal, never switch branches, and never append the failure to the selected transcript.

7. Missing sibling branch `agentRunId` is a propagation bug, not a product-route decision.
   - Backend `SessionRunBinding` already requires `agent_run_id`, and branch summaries are expected to expose it.
   - If execution finds a missing `agent_run_id` in Host-visible `branches`, repair the existing status/events/branch-summary propagation path.
   - Do not choose "disable non-selected branch auto-continue" as an implementation shortcut.

8. Documentation precedence for this repair is fixed.
   - This document narrows execution for source identity repair.
   - 2026-06-18 operation/async-correlation documents remain the authority for async guard semantics.
   - 2026-06-17 remains the authority for branch-local pending-next-turn behavior and lifecycle deferral.
   - If a local implementation seems to require hide/close/delete branch lifecycle work, do not implement it in this plan.

## Non-Decision Guardrails

Execution should not stop for architecture decisions if the issue is covered above. Stop only for a hard implementation blocker that cannot be resolved by the existing codebase, tests, or the precedence rules in this document. Do not modify the 2026-06-17 or 2026-06-18 authority documents while implementing this plan.

## Target Model

### Source Scopes

Add one explicit source scope type:

```ts
export type SessionRunOperationSourceScope =
  | "selected-visible"
  | "branch-local"
```

Meaning:

- `selected-visible`: operation is owned by the current visible ChatView branch. Success may patch selected run state and may emit visible operation pending/result/error messages.
- `branch-local`: operation is owned by a specific branch binding, but that branch may not be selected now. Success must not switch the selected branch, must not patch selected transcript, and must not create a global Webview pending operation.

### Resolved Source Identity

Add one resolved identity shape:

```ts
export interface ResolvedSessionRunSourceIdentity {
  source: SessionRunBranchIdentity
  targetBranchBindingId: string
  selectedBranch: boolean
  scope: SessionRunOperationSourceScope
  emitWebviewOperation: boolean
  canPatchSelectedRun: boolean
  sessionId?: string
}
```

Rules:

- `source.sessionRunId` must be the active `SessionRun` id.
- `source.branchBindingId` must be the target branch binding.
- `source.agentRunId` must come from the selected active run when selected, or from the active run's branch summaries when branch-local.
- `source.activeRunRevision` remains the selected-run identity revision at operation begin time, but branch-local acceptance must not reject merely because the user selected a sibling branch later.

### Resolver Contract

Create one resolver responsible for all Host source derivation:

```ts
export function resolveSessionRunSourceIdentity(input: {
  activeRun: ActiveSessionRun | undefined
  activeRunRevision: number
  sessionRunId?: string
  branchBindingId?: string
  scope: SessionRunOperationSourceScope
}): SessionRunSourceIdentityResolution
```

Return type:

```ts
export type SessionRunSourceIdentityResolution =
  | { ok: true; value: ResolvedSessionRunSourceIdentity }
  | {
      ok: false
      sessionRunId?: string
      sourceBranchBindingId?: string
      targetBranchBindingId: string
      message: string
    }
```

Resolution rules:

1. No active run means failure.
2. Missing target branch falls back to active run branch or `"main"`.
3. `selected-visible` requires the target branch to equal the active selected branch.
4. `branch-local` requires the target branch to belong to the active session:
   - If target equals active selected branch, use active run `agentRunId`.
   - Otherwise find the target in `activeRun.branches`.
   - Match by `branch_binding_id`, `branchBindingId`, `binding_id`, or `bindingId`.
   - Read agent id from `agent_run_id` or `agentRunId`.
5. Missing `agentRunId` is failure. Do not begin an operation without a proven source agent.

## Required File Changes

- Create: `Labrastro-vscode-extension/src/coordinators/SessionRunSourceIdentityResolver.ts`
  - Owns parsing and resolving Host source identity.
  - No network calls.
  - No Webview state.

- Create: `Labrastro-vscode-extension/src/coordinators/SessionRunSourceIdentityResolver.test.ts`
  - Focused resolver tests.

- Modify: `Labrastro-vscode-extension/src/coordinators/SessionRunOperationCoordinator.ts`
  - Add `sourceScope`.
  - Acceptance must distinguish selected-visible from branch-local.

- Modify: `Labrastro-vscode-extension/src/coordinators/SessionRunOperationCoordinator.test.ts`
  - Add acceptance tests for branch-local scope.

- Modify: `Labrastro-vscode-extension/src/LabrastroController.ts`
  - Replace local source construction in `continueSessionRun`, `steerAgentRun`, `recoverSessionRun`, and `cancelSessionRun` with resolver calls.
  - `continueSessionRun()` must support `sourceScope?: SessionRunOperationSourceScope`.
  - `applySessionRunEventsBatch()` must call auto-continue with `sourceScope: "branch-local"`.
  - Branch-local continue success must not switch selected branch.

- Modify: `Labrastro-vscode-extension/src/coordinators/SessionRunCoordinator.ts`
  - Thread `sourceScope` only where needed for pending-next-turn auto-continue.

- Modify: `Labrastro-vscode-extension/src/LabrastroController.session-run-correlation.test.ts`
  - Add branch-local auto-continue regression tests.

- Do not modify: `Labrastro-vscode-extension/src/protocol/messages.ts`
  - This repair must not add a branch-local failure protocol message.

## Implementation Tasks

### Task 1: Add Source Identity Resolver

**Files:**
- Create: `Labrastro-vscode-extension/src/coordinators/SessionRunSourceIdentityResolver.ts`
- Create: `Labrastro-vscode-extension/src/coordinators/SessionRunSourceIdentityResolver.test.ts`

- [ ] **Step 1: Write resolver tests**

Add tests covering selected branch, sibling branch, missing agent id, wrong session, and selected-visible target mismatch.

```ts
import { describe, expect, it } from "vitest"
import {
  resolveSessionRunSourceIdentity,
} from "./SessionRunSourceIdentityResolver"
import type { ActiveSessionRun } from "./SessionRunCoordinator"

const activeRun = (overrides: Partial<ActiveSessionRun> = {}): ActiveSessionRun => ({
  sessionRunId: "run-current",
  sessionId: "session-current",
  cursor: 12,
  status: "running",
  agentRunId: "agent-main",
  branchBindingId: "main",
  startedAt: "2026-06-18T00:00:00.000Z",
  reconnectAttempts: 0,
  branches: [
    { branch_binding_id: "main", agent_run_id: "agent-main", selected: true },
    { branch_binding_id: "branch-a", agent_run_id: "agent-branch-a", selected: false },
  ],
  ...overrides,
})

describe("resolveSessionRunSourceIdentity", () => {
  it("resolves selected visible source from active run", () => {
    const result = resolveSessionRunSourceIdentity({
      activeRun: activeRun(),
      activeRunRevision: 7,
      branchBindingId: "main",
      scope: "selected-visible",
    })
    expect(result).toEqual({
      ok: true,
      value: expect.objectContaining({
        targetBranchBindingId: "main",
        selectedBranch: true,
        scope: "selected-visible",
        emitWebviewOperation: true,
        canPatchSelectedRun: true,
        source: {
          sessionRunId: "run-current",
          branchBindingId: "main",
          agentRunId: "agent-main",
          activeRunRevision: 7,
        },
      }),
    })
  })

  it("resolves branch-local source from branch summaries without selecting it", () => {
    const result = resolveSessionRunSourceIdentity({
      activeRun: activeRun({ branchBindingId: "branch-b", agentRunId: "agent-branch-b" }),
      activeRunRevision: 9,
      sessionRunId: "run-current",
      branchBindingId: "branch-a",
      scope: "branch-local",
    })
    expect(result).toEqual({
      ok: true,
      value: expect.objectContaining({
        targetBranchBindingId: "branch-a",
        selectedBranch: false,
        scope: "branch-local",
        emitWebviewOperation: false,
        canPatchSelectedRun: false,
        source: {
          sessionRunId: "run-current",
          branchBindingId: "branch-a",
          agentRunId: "agent-branch-a",
          activeRunRevision: 9,
        },
      }),
    })
  })

  it("rejects selected-visible source when the target is a sibling branch", () => {
    const result = resolveSessionRunSourceIdentity({
      activeRun: activeRun(),
      activeRunRevision: 1,
      branchBindingId: "branch-a",
      scope: "selected-visible",
    })
    expect(result).toEqual(expect.objectContaining({
      ok: false,
      targetBranchBindingId: "branch-a",
      sourceBranchBindingId: "main",
    }))
  })

  it("rejects branch-local source when the branch agent id is unavailable", () => {
    const result = resolveSessionRunSourceIdentity({
      activeRun: activeRun({ branches: [{ branch_binding_id: "branch-a" }] }),
      activeRunRevision: 1,
      branchBindingId: "branch-a",
      scope: "branch-local",
    })
    expect(result).toEqual(expect.objectContaining({
      ok: false,
      targetBranchBindingId: "branch-a",
    }))
  })

  it("rejects branch-local source for another session run", () => {
    const result = resolveSessionRunSourceIdentity({
      activeRun: activeRun(),
      activeRunRevision: 1,
      sessionRunId: "run-other",
      branchBindingId: "branch-a",
      scope: "branch-local",
    })
    expect(result).toEqual(expect.objectContaining({
      ok: false,
      sessionRunId: "run-other",
      targetBranchBindingId: "branch-a",
    }))
  })
})
```

- [ ] **Step 2: Run resolver tests and verify failure**

Run:

```powershell
npm run test -- src/coordinators/SessionRunSourceIdentityResolver.test.ts
```

Expected: fail because `SessionRunSourceIdentityResolver.ts` does not exist.

- [ ] **Step 3: Implement resolver**

Create `SessionRunSourceIdentityResolver.ts` with the public types and the resolver function. Keep all branch-summary parsing here.

```ts
import type { ActiveSessionRun } from "./SessionRunCoordinator"
import type { SessionRunBranchIdentity } from "./SessionRunOperationCoordinator"

export type SessionRunOperationSourceScope = "selected-visible" | "branch-local"

export interface ResolvedSessionRunSourceIdentity {
  source: SessionRunBranchIdentity
  targetBranchBindingId: string
  selectedBranch: boolean
  scope: SessionRunOperationSourceScope
  emitWebviewOperation: boolean
  canPatchSelectedRun: boolean
  sessionId?: string
}

export type SessionRunSourceIdentityResolution =
  | { ok: true; value: ResolvedSessionRunSourceIdentity }
  | {
      ok: false
      sessionRunId?: string
      sourceBranchBindingId?: string
      targetBranchBindingId: string
      message: string
    }

export function resolveSessionRunSourceIdentity(input: {
  activeRun: ActiveSessionRun | undefined
  activeRunRevision: number
  sessionRunId?: string
  branchBindingId?: string
  scope: SessionRunOperationSourceScope
}): SessionRunSourceIdentityResolution {
  const activeRun = input.activeRun
  const activeBranchBindingId = activeRun?.branchBindingId || "main"
  const targetBranchBindingId = input.branchBindingId || activeBranchBindingId
  const requestedSessionRunId = input.sessionRunId || activeRun?.sessionRunId || ""
  if (!activeRun?.sessionRunId || !requestedSessionRunId) {
    return {
      ok: false,
      sessionRunId: requestedSessionRunId || undefined,
      targetBranchBindingId,
      message: "没有可操作的会话运行。",
    }
  }
  if (activeRun.sessionRunId !== requestedSessionRunId) {
    return {
      ok: false,
      sessionRunId: requestedSessionRunId,
      sourceBranchBindingId: activeBranchBindingId,
      targetBranchBindingId,
      message: "目标会话运行已不是当前会话。",
    }
  }
  const selectedBranch = activeBranchBindingId === targetBranchBindingId
  if (input.scope === "selected-visible" && !selectedBranch) {
    return {
      ok: false,
      sessionRunId: requestedSessionRunId,
      sourceBranchBindingId: activeBranchBindingId,
      targetBranchBindingId,
      message: "当前可见分支与操作目标分支不一致。",
    }
  }
  const agentRunId = selectedBranch
    ? activeRun.agentRunId || ""
    : branchAgentRunId(activeRun.branches, targetBranchBindingId)
  if (!agentRunId) {
    return {
      ok: false,
      sessionRunId: requestedSessionRunId,
      sourceBranchBindingId: activeBranchBindingId,
      targetBranchBindingId,
      message: "目标分支没有可证明的 AgentRun mainline。",
    }
  }
  return {
    ok: true,
    value: {
      source: {
        sessionRunId: requestedSessionRunId,
        branchBindingId: targetBranchBindingId,
        agentRunId,
        activeRunRevision: input.activeRunRevision,
      },
      targetBranchBindingId,
      selectedBranch,
      scope: input.scope,
      emitWebviewOperation: input.scope === "selected-visible",
      canPatchSelectedRun: selectedBranch,
      ...(activeRun.sessionId ? { sessionId: activeRun.sessionId } : {}),
    },
  }
}

function branchAgentRunId(branches: Record<string, unknown>[] | undefined, branchBindingId: string): string {
  const branch = (branches || []).find((item) => branchBindingKey(item) === branchBindingId)
  return branch ? stringValue(branch.agent_run_id) || stringValue(branch.agentRunId) || "" : ""
}

function branchBindingKey(value: Record<string, unknown>): string {
  return (
    stringValue(value.branch_binding_id) ||
    stringValue(value.branchBindingId) ||
    stringValue(value.binding_id) ||
    stringValue(value.bindingId) ||
    ""
  )
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined
}
```

- [ ] **Step 4: Run resolver tests and verify pass**

Run:

```powershell
npm run test -- src/coordinators/SessionRunSourceIdentityResolver.test.ts
```

Expected: pass.

### Task 2: Make Operation Acceptance Scope-Aware

**Files:**
- Modify: `Labrastro-vscode-extension/src/coordinators/SessionRunOperationCoordinator.ts`
- Modify: `Labrastro-vscode-extension/src/coordinators/SessionRunOperationCoordinator.test.ts`

- [ ] **Step 1: Add failing operation coordinator tests**

Add tests proving branch-local operations survive selected-branch changes inside the same SessionRun, but reject another SessionRun.

```ts
it("accepts branch-local continue success after the selected branch changes within the same SessionRun", () => {
  const coordinator = new SessionRunOperationCoordinator()
  coordinator.begin({
    operationId: "op-branch-local",
    operationKind: "continue",
    sourceScope: "branch-local",
    source: {
      sessionRunId: "run-current",
      branchBindingId: "branch-a",
      agentRunId: "agent-branch-a",
      activeRunRevision: 1,
    },
    targetBranchBindingId: "branch-a",
  })

  expect(coordinator.acceptsControlSuccess({
    operationId: "op-branch-local",
    operationKind: "continue",
    activeRun: {
      sessionRunId: "run-current",
      branchBindingId: "branch-b",
      agentRunId: "agent-branch-b",
    },
    activeRunRevision: 2,
    responseSessionRunId: "run-current",
    responseBranchBindingId: "branch-a",
    responseAgentRunId: "agent-branch-a",
  })).toBe(true)
})

it("rejects branch-local continue success after the active SessionRun changes", () => {
  const coordinator = new SessionRunOperationCoordinator()
  coordinator.begin({
    operationId: "op-branch-local-other-run",
    operationKind: "continue",
    sourceScope: "branch-local",
    source: {
      sessionRunId: "run-current",
      branchBindingId: "branch-a",
      agentRunId: "agent-branch-a",
      activeRunRevision: 1,
    },
    targetBranchBindingId: "branch-a",
  })

  expect(coordinator.acceptsControlSuccess({
    operationId: "op-branch-local-other-run",
    operationKind: "continue",
    activeRun: {
      sessionRunId: "run-other",
      branchBindingId: "main",
      agentRunId: "agent-other",
    },
    activeRunRevision: 2,
    responseSessionRunId: "run-current",
    responseBranchBindingId: "branch-a",
    responseAgentRunId: "agent-branch-a",
  })).toBe(false)
})
```

- [ ] **Step 2: Run operation coordinator tests and verify failure**

Run:

```powershell
npm run test -- src/coordinators/SessionRunOperationCoordinator.test.ts
```

Expected: fail because `sourceScope` is not supported.

- [ ] **Step 3: Implement scoped operation acceptance**

Required changes:

```ts
import type { SessionRunOperationSourceScope } from "./SessionRunSourceIdentityResolver"
```

Add `sourceScope` to non-start operations:

```ts
sourceScope: SessionRunOperationSourceScope
```

Set default in `begin()`:

```ts
sourceScope: operation.sourceScope || "selected-visible"
```

Change selected source acceptance:

```ts
private sourceStillCurrent(
  activeRun: ActiveSessionRunIdentity | undefined,
  activeRunRevision: number,
  operation: Extract<SessionRunOperation, { source: SessionRunBranchIdentity }>,
): boolean {
  if (operation.sourceScope === "branch-local") {
    return activeRun?.sessionRunId === operation.source.sessionRunId
  }
  return (
    operation.activeRunRevision === activeRunRevision &&
    this.activeRunMatchesSource(activeRun, operation.source)
  )
}
```

Use `sourceStillCurrent()` inside `acceptsBranchCreateSuccess()`, `acceptsBranchSelectSuccess()`, and `acceptsControlSuccess()`. Keep branch create/select callers on default `selected-visible`.

Change `acceptsFailure()` so branch-local failures do not require selected branch revision to remain unchanged:

```ts
if (operation.operationKind !== "start" && operation.sourceScope === "branch-local") {
  const accepted = input.activeSessionRunId === operation.source.sessionRunId
  this.operation = undefined
  return accepted
}
```

If this requires adding `activeSessionRunId?: string` to `acceptsFailure()` input, update every caller explicitly.

- [ ] **Step 4: Run operation coordinator tests and verify pass**

Run:

```powershell
npm run test -- src/coordinators/SessionRunOperationCoordinator.test.ts
```

Expected: pass.

### Task 3: Route Controller Control Operations Through Resolver

**Files:**
- Modify: `Labrastro-vscode-extension/src/LabrastroController.ts`
- Modify: `Labrastro-vscode-extension/src/coordinators/SessionRunCoordinator.ts`

- [ ] **Step 1: Extend `continueSessionRun` option shape**

Add:

```ts
sourceScope?: SessionRunOperationSourceScope
```

Use default:

```ts
const sourceScope = options.sourceScope || "selected-visible"
```

- [ ] **Step 2: Replace `continueSessionRun()` source construction**

Remove this pattern:

```ts
const activeRun = this.sessionRunCoordinator.activeRun
const sessionRunId = activeRun?.sessionRunId || ""
const sourceAgentRunId = activeRun?.agentRunId || ""
const branchBindingId = options.branchBindingId || activeRun?.branchBindingId || "main"
```

Replace with resolver:

```ts
const sourceResolution = resolveSessionRunSourceIdentity({
  activeRun: this.sessionRunCoordinator.activeRun,
  activeRunRevision: this.sessionRunCoordinator.activeRunRevision,
  branchBindingId: options.branchBindingId,
  scope: sourceScope,
})
if (!sourceResolution.ok) {
  this.reportSessionRunOperationPreflightFailure(post, {
    operationId,
    operationKind,
    ...(sourceResolution.sessionRunId ? { sessionRunId: sourceResolution.sessionRunId } : {}),
    ...(sourceResolution.sourceBranchBindingId ? { sourceBranchBindingId: sourceResolution.sourceBranchBindingId } : {}),
    targetBranchBindingId: sourceResolution.targetBranchBindingId,
    message: sourceResolution.message,
  })
  return
}
const resolvedSource = sourceResolution.value
const { source, targetBranchBindingId: branchBindingId } = resolvedSource
const sessionRunId = source.sessionRunId
```

Begin operation with:

```ts
this.sessionRunOperationCoordinator.begin({
  operationId,
  operationKind,
  sourceScope: resolvedSource.scope,
  source,
  targetBranchBindingId: branchBindingId,
})
```

Emit Webview pending only for visible operations:

```ts
if (resolvedSource.emitWebviewOperation) {
  this.emitSessionRunOperationPending(post, {
    operationId,
    operationKind,
    sessionRunId,
    branchBindingId,
    targetBranchBindingId: branchBindingId,
  })
}
```

- [ ] **Step 3: Make continue success effects scope-aware**

Selected-visible success keeps current visible behavior.

Branch-local success must:

```ts
this.sessionRunCoordinator.removePendingNextTurnForBranch(sessionRunId, branchBindingId, {
  clientRequestId: options.clientRequestId,
  text,
})
this.sessionRunCoordinator.postPendingNextTurnsSnapshot(post, sessionRunId, branchBindingId)
```

Then only patch selected run and start selected stream when the target is selected at success time:

```ts
const targetSelectedNow = this.activeSessionRunMatches({ sessionRunId, branchBindingId })
if (targetSelectedNow) {
  this.sessionRunCoordinator.patchActiveRun({
    status: "running",
    cursor: this.sessionRunCoordinator.activeRun?.cursor ?? 0,
    branchBindingId,
    ...(agentRunId ? { agentRunId } : {}),
    ...(activationId ? { activationId } : {}),
    reconnectAttempts: 0,
    lastStreamAt: new Date().toISOString(),
  })
  this.ensureSessionRunEventStream(sessionRunId, resolvedSource.sessionId || "", post, branchBindingId)
}
```

Do not switch to `branchBindingId` when `targetSelectedNow` is false.

- [ ] **Step 4: Make branch-local continue failure fail closed without visible operation error**

In the `continueSessionRun()` catch block, branch-local failures must keep the queued next turn on its branch and avoid visible operation error messages:

```ts
if (resolvedSource.scope === "branch-local") {
  this.sessionRunOperationCoordinator.acceptsFailure({
    operationId,
    operationKind,
    activeRunRevision: this.sessionRunCoordinator.activeRunRevision,
    activeSessionRunId: this.sessionRunCoordinator.activeSessionRunId,
  })
  this.sessionRunCoordinator.postPendingNextTurnsSnapshot(post, sessionRunId, branchBindingId)
  await this.postConnectionStateIfAuthRequired(error, post)
  return
}
```

Required behavior:

- Do not call `removePendingNextTurnForBranch()`.
- Do not emit `sessionRun.operation.error`.
- Do not emit `sessionRun.error`.
- Do not patch selected run terminal state.
- Do not switch `selectedBranchBindingId` or `activeRun.branchBindingId`.

- [ ] **Step 5: Pass branch-local scope from pending-next-turn auto-continue**

In `applySessionRunEventsBatch()`:

```ts
void this.continueSessionRun(pendingNextTurn.text, post, {
  sourceScope: "branch-local",
  branchBindingId: streamBranchBindingId,
  clientRequestId: pendingNextTurn.clientRequestId,
  locale: pendingNextTurn.locale,
  mentions: pendingNextTurn.mentions,
})
```

Update `SessionRunCoordinatorOptions.continueSessionRun` type to accept the new `sourceScope`.

- [ ] **Step 6: Route selected-visible control operations through resolver**

Refactor `steerAgentRun`, `recoverSessionRun`, and `cancelSessionRun` to call the resolver with `scope: "selected-visible"` instead of hand-checking source branch and agent id.

Acceptance criteria:

- The preflight messages remain operation errors, not `sessionRun.error`.
- Selected-only operations still reject sibling branch targets.
- No operation is begun with missing `source.agentRunId`.

### Task 4: Add Controller Regression Tests

**Files:**
- Modify: `Labrastro-vscode-extension/src/LabrastroController.session-run-correlation.test.ts`

- [ ] **Step 1: Add non-selected auto-continue test**

Add a regression proving branch A can auto-continue while branch B remains selected.

```ts
it("continues a branch-local pending next turn without switching the selected branch", async () => {
  const controller = new LabrastroController(context())
  const continueSessionRun = vi.fn(async () => ({
    session_run_id: "run-current",
    branch_binding_id: "branch-a",
    agent_run_id: "agent-branch-a",
    activation_id: "activation-branch-a-2",
  }))
  const ensureSessionRunEventStream = vi.fn()
  ;(controller as unknown as { client: { continueSessionRun: typeof continueSessionRun } }).client = { continueSessionRun }
  ;(controller as unknown as {
    ensureSessionRunEventStream: typeof ensureSessionRunEventStream
  }).ensureSessionRunEventStream = ensureSessionRunEventStream
  setActiveRun(controller, {
    sessionRunId: "run-current",
    sessionId: "session-current",
    branchBindingId: "branch-b",
    agentRunId: "agent-branch-b",
    status: "idle",
    branches: [
      { branch_binding_id: "branch-a", agent_run_id: "agent-branch-a", selected: false },
      { branch_binding_id: "branch-b", agent_run_id: "agent-branch-b", selected: true },
    ],
    pendingNextTurnsByBranch: {
      "run-current:branch-a": [{
        text: "queued on A",
        sessionRunId: "run-current",
        branchBindingId: "branch-a",
        clientRequestId: "queued-a",
        queuedAt: "2026-06-18T00:00:00.000Z",
      }],
    },
  })
  const post = vi.fn()

  await (controller as unknown as {
    continueSessionRun: (
      text: string,
      post: (message: Record<string, unknown>) => void,
      options?: { branchBindingId?: string; clientRequestId?: string; sourceScope?: "branch-local" },
    ) => Promise<void>
  }).continueSessionRun("queued on A", post, {
    sourceScope: "branch-local",
    branchBindingId: "branch-a",
    clientRequestId: "queued-a",
  })

  expect(continueSessionRun).toHaveBeenCalledWith(expect.objectContaining({
    sessionRunId: "run-current",
    branchBindingId: "branch-a",
    prompt: "queued on A",
    clientRequestId: "queued-a",
  }))
  expect(sessionRunCoordinator(controller).activeRun?.branchBindingId).toBe("branch-b")
  expect(sessionRunCoordinator(controller).activeRun?.agentRunId).toBe("agent-branch-b")
  expect(sessionRunCoordinator(controller).pendingNextTurnForBranch("run-current", "branch-a")).toBeUndefined()
  expect(ensureSessionRunEventStream).not.toHaveBeenCalled()
  expect(post).not.toHaveBeenCalledWith(expect.objectContaining({
    type: "sessionRun.operation.pending",
    operationKind: "continue",
  }))
})
```

- [ ] **Step 2: Add fail-closed test for missing sibling agent id**

```ts
it("does not start branch-local continue when sibling branch identity lacks agentRunId", async () => {
  const controller = new LabrastroController(context())
  const continueSessionRun = vi.fn()
  ;(controller as unknown as { client: { continueSessionRun: typeof continueSessionRun } }).client = { continueSessionRun }
  setActiveRun(controller, {
    sessionRunId: "run-current",
    sessionId: "session-current",
    branchBindingId: "branch-b",
    agentRunId: "agent-branch-b",
    status: "idle",
    branches: [{ branch_binding_id: "branch-a", selected: false }],
  })
  const post = vi.fn()

  await (controller as unknown as {
    continueSessionRun: (
      text: string,
      post: (message: Record<string, unknown>) => void,
      options?: { branchBindingId?: string; sourceScope?: "branch-local" },
    ) => Promise<void>
  }).continueSessionRun("queued on A", post, {
    sourceScope: "branch-local",
    branchBindingId: "branch-a",
  })

  expect(continueSessionRun).not.toHaveBeenCalled()
  expect(sessionRunCoordinator(controller).pendingNextTurnForBranch("run-current", "branch-a")?.text).toBe("queued on A")
  expect(post).toHaveBeenCalledWith(expect.objectContaining({
    type: "sessionRun.pendingNextTurns",
    sessionRunId: "run-current",
    branchBindingId: "branch-a",
  }))
  expect(post).not.toHaveBeenCalledWith(expect.objectContaining({
    type: "sessionRun.operation.error",
    operationKind: "continue",
    branchBindingId: "branch-a",
  }))
  expect(post).not.toHaveBeenCalledWith(expect.objectContaining({ type: "sessionRun.error" }))
})
```

- [ ] **Step 3: Add branch-local remote failure test**

```ts
it("keeps a branch-local pending next turn queued when auto-continue fails", async () => {
  const controller = new LabrastroController(context())
  const continueSessionRun = vi.fn(async () => {
    throw new Error("continue failed")
  })
  ;(controller as unknown as { client: { continueSessionRun: typeof continueSessionRun } }).client = { continueSessionRun }
  setActiveRun(controller, {
    sessionRunId: "run-current",
    sessionId: "session-current",
    branchBindingId: "branch-b",
    agentRunId: "agent-branch-b",
    status: "idle",
    branches: [
      { branch_binding_id: "branch-a", agent_run_id: "agent-branch-a", selected: false },
      { branch_binding_id: "branch-b", agent_run_id: "agent-branch-b", selected: true },
    ],
    pendingNextTurnsByBranch: {
      "run-current:branch-a": [{
        text: "queued on A",
        sessionRunId: "run-current",
        branchBindingId: "branch-a",
        clientRequestId: "queued-a",
        queuedAt: "2026-06-18T00:00:00.000Z",
      }],
    },
  })
  const post = vi.fn()

  await (controller as unknown as {
    continueSessionRun: (
      text: string,
      post: (message: Record<string, unknown>) => void,
      options?: { branchBindingId?: string; clientRequestId?: string; sourceScope?: "branch-local" },
    ) => Promise<void>
  }).continueSessionRun("queued on A", post, {
    sourceScope: "branch-local",
    branchBindingId: "branch-a",
    clientRequestId: "queued-a",
  })

  expect(sessionRunCoordinator(controller).activeRun?.branchBindingId).toBe("branch-b")
  expect(sessionRunCoordinator(controller).pendingNextTurnForBranch("run-current", "branch-a")?.text).toBe("queued on A")
  expect(post).toHaveBeenCalledWith(expect.objectContaining({
    type: "sessionRun.pendingNextTurns",
    sessionRunId: "run-current",
    branchBindingId: "branch-a",
  }))
  expect(post).not.toHaveBeenCalledWith(expect.objectContaining({
    type: "sessionRun.operation.error",
    operationKind: "continue",
    branchBindingId: "branch-a",
  }))
  expect(post).not.toHaveBeenCalledWith(expect.objectContaining({ type: "sessionRun.error" }))
})
```

- [ ] **Step 4: Run controller correlation tests and verify failure before implementation, pass after implementation**

Run:

```powershell
npm run test -- src/LabrastroController.session-run-correlation.test.ts
```

Expected before implementation: fail. Expected after Task 3: pass.

### Task 5: Drift Scans And Verification

**Files:**
- No new files unless a test forces a targeted change.

- [ ] **Step 1: Scan for direct source construction**

Run:

```powershell
rg -n "sourceAgentRunId|sourceBranchBindingId|activeRun\\?\\.agentRunId|activeRun\\?\\.branchBindingId" src/LabrastroController.ts src/coordinators
```

Expected:

- Direct active-run reads may remain for selected state display and non-operation paths.
- No control operation should build `SessionRunBranchIdentity` by hand outside `SessionRunSourceIdentityResolver`.

- [ ] **Step 2: Scan for accidental Webview global pending on branch-local continue**

Run:

```powershell
rg -n "sourceScope: \"branch-local\"|emitSessionRunOperationPending\\(" src/LabrastroController.ts
```

Expected:

- `sourceScope: "branch-local"` appears only where pending-next-turn auto-continue is invoked or tested.
- `emitSessionRunOperationPending()` is guarded by `resolvedSource.emitWebviewOperation` for `continue`.

- [ ] **Step 3: Run targeted tests**

Run:

```powershell
npm run test -- src/coordinators/SessionRunSourceIdentityResolver.test.ts src/coordinators/SessionRunOperationCoordinator.test.ts src/LabrastroController.session-run-correlation.test.ts src/LabrastroController.chat-stream.test.ts
```

Expected: all pass.

- [ ] **Step 4: Run typecheck**

Run:

```powershell
npm run typecheck
```

Expected: pass.

- [ ] **Step 5: Final review checklist**

Confirm each statement with code evidence:

- Every non-start Host operation has a source identity before `begin()`.
- `continueSessionRun()` no longer borrows selected branch `agentRunId` for branch-local auto-continue.
- Branch-local auto-continue does not switch selected branch.
- Branch-local auto-continue does not create a global Webview pending operation.
- Selected-visible operations still reject sibling branch targets.
- Operation errors remain operation-only and do not mark selected run terminal.
- No branch hide/close/delete lifecycle behavior was introduced.

## Completion Definition

This plan is complete only when:

1. The resolver is the single owned place for Host control-operation source identity derivation.
2. Operation coordinator acceptance explicitly models `selected-visible` and `branch-local`.
3. The concrete branch-local pending-next-turn bug has a regression test.
4. All targeted tests and typecheck pass.
5. A final review compares the implementation against 2026-06-17, 2026-06-18 async correlation, and this document, with code evidence for every boundary.

## Execution Warning

Do not implement this as nested conditionals inside `continueSessionRun()` or `applySessionRunEventsBatch()`. The intended design is:

1. Resolve source identity once.
2. Begin operation from the resolved identity.
3. Accept response using the operation's scope.
4. Apply visible or branch-local effects through one scope decision.

If the implementation starts accumulating branch-specific `if` ladders in controller handlers, stop and refactor back to the model above.

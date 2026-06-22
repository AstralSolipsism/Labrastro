# SessionRun Async Correlation Architecture Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current scattered async guards with one owned SessionRun operation lifecycle, one Webview message gate, and one backend control resolver so stale responses cannot drift into the visible ChatView and projection recovery errors are explicit.

**Architecture:** Host async requests create typed operations with operation ids and exact source/target identity; no awaited response can patch active UI state unless the operation coordinator accepts it. Webview applies SessionRun messages through one message gate that understands current branch, pending operation, and projection-only sibling summaries. Backend control routes resolve SessionRun control targets through one typed resolver result instead of route-local fallback checks.

**Tech Stack:** VS Code extension TypeScript, Solid ChatView, Host/Webview protocol messages, Python remote HTTP routes, pytest, Vitest, GitNexus.

---

## Read First

This document is the active execution guide for the 2026-06-18 SessionRun async drift repair. If chat history, temporary review notes, or the partially applied patch conflicts with this document, follow this document.

Development-period rule: do not add redundant compatibility, migration fallback, or parallel legacy behavior. A correct fix must make the architecture unified, the logic complete, and the boundaries explicit.

This plan covers a repair of the current uncommitted changes. Treat the current patch as diagnostic evidence, not as the desired final shape.

## Confirmed Facts

- The original P1 issue is real: a message carrying `sessionRunId` must not apply when there is no current `activeSessionRunId()`. Otherwise an old `sessionRun.done`, `sessionRun.cancelled`, `sessionRun.error`, or failed reply can still mutate the currently visible UI after the active run has been cleared.
- The current patch tightens `messageTargetsCurrentRun()`, but the logic remains scattered across `LabrastroController.ts`, `ChatView.tsx`, and `routes/chat.py`.
- The current patch added `pendingBranchSelectionId` in `ChatView.tsx`. It clears only on `sessionRun.branch.selected` success. A failed branch selection can leave a stale pending target that later allows an old selected response to apply.
- The current patch added Host checks through `activeSessionRunContextMatches()`. That helper still allows a response with source `agentRunId` to pass when the current active run has no `agentRunId`, because the comparison only runs when both sides are present.
- The backend helper `_persisted_binding_for_missing_session_run_projection()` catches broad exceptions and converts binding-store failure into absence. That can return `session_run_not_found` when the real problem is runtime store unavailability.
- Current tests include source-shape assertions such as checking for guard strings. Those tests can preserve the patch shape instead of proving the behavior.
- Current Webview branch selection messages do not carry a request or operation id. A later plan that expects Webview to match branch-select results by operation id is incomplete unless `chatMessages.selectBranch()`, `SessionRunCoordinator`, Host messages, and ChatView pending operation state all share the same id.
- Current Host active-run checks compare current identity but do not defend against ABA drift: the active branch can change away and later back to the same `sessionRunId` and `branchBindingId`, allowing an old response to appear current by identity alone.
- Current `sessionRun.error` is overloaded. Some errors mean "the selected run failed"; other errors mean "a user command such as branch select failed". A unified gate cannot be correct while both meanings share one terminal-looking message type.
- Current backend route helpers split session projection lookup from branch binding lookup. A resolver that only answers "does the SessionRun projection exist" is still incomplete because continue/events/status/recover/cancel/reply/select must resolve a concrete branch binding target with a route-specific policy.
- The 2026-06-17 convergence plan is still the higher-level direction: `SessionRun` is projection/interaction, `AgentRun` is execution. This document only repairs the async correlation and projection resolver gaps exposed by the recent review.

## Plain Explanation

### Why the same kind of bug keeps coming back

The system currently answers stale async responses by asking many local questions:

- Does this message have a `sessionRunId`?
- Does this message have a `branchBindingId`?
- Is there an active run right now?
- Is this branch pending selection?
- Does the response source branch still look close enough to the current branch?

Those questions are scattered in different functions. Each new message type or failure path has to remember the same rule again. That is why one fix closes `sessionRun.done` drift, then another path such as branch-selection failure reopens the same class of drift.

The correct question is one level higher:

> Which operation owns this response, and is that operation still current?

Once this is the rule, success, failure, stream recovery, branch selection, and first-run establishment all use the same lifecycle.

### Impact of the current defects

- Stale terminal message impact: an old run can mark the visible run done, cancelled, or failed. The user sees the wrong final state and can lose the ability to continue the real selected branch.
- Stale branch selection impact: a branch switch that failed or was superseded can still change the selected branch later. The transcript, pending approvals, queued prompt, and stream cursor can point at a branch the user did not select.
- Weak Host `agentRunId` correlation impact: when the active run state is temporarily incomplete, a stale branch-create or branch-select response can pass because the guard treats missing current identity as acceptable.
- Backend broad-exception fallback impact: store failure can be reported as missing session. That hides infrastructure failures, makes recovery misleading, and can cause clients to treat a recoverable control-plane outage as a deleted run.
- Source-string tests impact: they prevent cleanup of the bad shape. A future implementation can be behaviorally correct but fail because it removed the exact helper text the test expects.

## Non-Negotiable Decisions

- No further patch-layer guard additions as the main fix. Small edits are allowed only when they are part of replacing the scattered guard model.
- Host owns async operation lifecycle. `LabrastroController` must not call `patchActiveRun()`, emit `sessionRun.session`, emit `sessionRun.branch.started`, emit `sessionRun.branch.selected`, or start a stream after `await` unless a SessionRun operation coordinator accepts the response.
- Every visible async SessionRun operation has one `operationId`. For user-originated operations, Webview generates it before sending the request and Host echoes the same id. For Host-originated visible operations, Host must first emit `sessionRun.operation.pending` carrying the id before emitting the result.
- `operationId` is a Host/Webview correlation field, not an HTTP idempotency field. `clientRequestId` remains backend idempotency or activation routing metadata. They may share the same generated string for one user action, but they must stay separate fields and separate concepts.
- Host-to-Webview operation results include `operationId` and `operationKind` for `sessionRun.session`, `sessionRun.branch.started`, and `sessionRun.branch.selected`.
- Operation failures use `sessionRun.operation.error`, not terminal `sessionRun.error`. `sessionRun.operation.error` clears the matching pending operation and shows a notice; it must not call terminal run cleanup for the selected run.
- Operation success and operation failure both close the pending operation. There is no success-only cleanup path.
- A message with `sessionRunId` and no active run is stale unless it is a current `start` operation response accepted by operation id. Legacy branch fallback is only for messages without `sessionRunId` and without operation semantics.
- Branch create with auto-select is an operation with an explicit target branch. Webview must not infer validity from a loose pending branch id.
- Branch select failure leaves the selected branch unchanged, clears the pending operation, and shows an operation-scoped error in the current UI. It must not switch to the target branch.
- Branch create/select success must validate both source and target. The source must still match the captured active run revision and identity; the response target branch must equal the operation target branch.
- Selected transcript writes are selected-branch-only. Sibling branch updates are projection summaries and cannot write transcript text, terminal state, or active input target.
- Backend SessionRun control routes use a typed resolver that returns a concrete session and branch binding target, not just a SessionRun projection. Store exceptions produce explicit unavailable errors; they are not collapsed into not found.
- Branch lifecycle commands remain separated: select/switch, hide, stop active branch run, close binding, and destructive delete/resources cleanup. This repair does not mark hide/close/delete complete.

## File Responsibility Map

Extension Host:

- Create: `../Labrastro-vscode-extension/src/coordinators/SessionRunOperationCoordinator.ts`
  - Owns current pending SessionRun operation.
  - Accepts or rejects async success/failure responses.
  - Invalidates operations when the active run revision changes outside that operation.
- Modify: `../Labrastro-vscode-extension/src/LabrastroController.ts`
  - Starts operations before awaiting `startSessionRun`, `branchAgentRun`, and `selectSessionRunBranch`.
  - Applies results only through the operation coordinator.
  - Emits operation ids in Host-to-Webview messages.
  - Emits `sessionRun.operation.error` for operation failures.
  - Removes `activeSessionRunContextMatches()` and `sessionRunStartResponseStillCurrent()`.
- Modify: `../Labrastro-vscode-extension/src/coordinators/SessionRunCoordinator.ts`
  - Exposes an active-run revision that increments on every `setActiveRun()` mutation.
  - Passes Webview-originated `operationId` to `startSessionRun`, `branchSessionRun`, and `selectSessionRunBranch`.
- Modify: `../Labrastro-vscode-extension/webview-ui/src/chat/chatMessages.ts`
  - Adds `operationId` to `chat.send`, `sessionRun.branch`, and `sessionRun.branch.select` messages where they start visible SessionRun operations.
- Modify: `../Labrastro-vscode-extension/src/protocol/messages.ts`
  - Keeps message type registration in sync with operation-scoped fields.
  - Adds `sessionRun.operation.pending` and `sessionRun.operation.error`.
  - No `operation_id` compatibility alias is introduced for Host/Webview messages.
- Test: `../Labrastro-vscode-extension/src/LabrastroController.session-run-correlation.test.ts`
  - Replaces source-shape checks with behavior tests for success, stale success, failure cleanup, and active-run invalidation.

Webview:

- Create: `../Labrastro-vscode-extension/webview-ui/src/chat/sessionRunMessageGate.ts`
  - Holds pure gate functions for Host-to-Webview SessionRun messages.
  - Distinguishes current-branch messages, operation-result messages, branch summaries, and first-run establishment.
- Modify: `../Labrastro-vscode-extension/webview-ui/src/components/ChatView.tsx`
  - Removes `pendingBranchSelectionId`.
  - Generates an `operationId` before sending visible start, branch create, and branch select operations.
  - Calls the message gate before applying any SessionRun message.
  - Clears operation state on success and failure.
- Test: `../Labrastro-vscode-extension/webview-ui/src/components/ChatView.context-events.test.ts`
  - Tests behavior by dispatching messages and reading resulting UI/state effects.
  - Removes tests that require a particular inline guard string.

Backend:

- Create: `labrastro_server/interfaces/http/remote/session_run_control.py`
  - Defines typed SessionRun control resolution outcomes.
  - Resolves token, in-memory projection, route-specific branch binding target, peer ownership, and store unavailability.
- Modify: `labrastro_server/interfaces/http/remote/routes/chat.py`
  - Delegates control resolution to the resolver.
  - Converts resolver outcomes into HTTP status and error payloads in one place.
  - Removes `_persisted_binding_for_missing_session_run_projection()`.
  - Removes route-local selected/branch binding lookup from the SessionRun control path.
- Test: `tests/labrastro_server/http/test_remote_service.py`
  - Covers persisted binding with missing projection.
  - Covers binding-store exception as service unavailable.
  - Covers peer mismatch as forbidden.

Documentation:

- Modify: `docs/superpowers/plans/2026-06-17-sessionrun-agentrun-execution-convergence.md`
  - Keep branch lifecycle correction unchecked until the separate lifecycle API exists.
  - Add a cross-reference to this document for async correlation.

## Required GitNexus Checks Before Code Edits

Run these before editing the related symbols. Report HIGH or CRITICAL risk before applying code edits.

```powershell
Push-Location ..\Labrastro-vscode-extension
node .gitnexus/run.cjs impact --repo Labrastro-vscode-extension --file src/LabrastroController.ts LabrastroController
node .gitnexus/run.cjs impact --repo Labrastro-vscode-extension --file webview-ui/src/components/ChatView.tsx ChatView
node .gitnexus/run.cjs impact --repo Labrastro-vscode-extension --file src/protocol/messages.ts isHostToWebviewMessage
Pop-Location

Push-Location .\Labrastro
node .gitnexus/run.cjs impact --repo Labrastro --file labrastro_server/interfaces/http/remote/routes/chat.py RemoteChatRoutes
Pop-Location
```

## Target Host Operation Model

Use this as the contract for the new Host coordinator.

```ts
export type SessionRunOperationKind = "start" | "branch.create" | "branch.select"

export interface SessionRunBranchIdentity {
  sessionRunId: string
  branchBindingId: string
  agentRunId: string
  activeRunRevision: number
}

export type SessionRunOperation =
  | {
      kind: "start"
      operationId: string
      activeRunRevision: number
      activeSessionRunId?: string
      requestedSessionId?: string
      draftSessionId?: string
      startedAt: number
    }
  | {
      kind: "branch.create"
      operationId: string
      source: SessionRunBranchIdentity
      targetBranchBindingId: string
      startedAt: number
    }
  | {
      kind: "branch.select"
      operationId: string
      source: SessionRunBranchIdentity
      targetBranchBindingId: string
      startedAt: number
    }
```

Acceptance rules:

- `start` success may establish active run only when the operation id is current, the active-run revision still equals the captured start revision, and either there is no active run or the active run already has the same `sessionRunId`.
- `branch.create` success may patch active run only when the operation id is current, the active-run revision still equals `source.activeRunRevision`, and the active run still matches `source.sessionRunId`, `source.branchBindingId`, and `source.agentRunId`.
- `branch.create` success must reject a response whose target branch binding id differs from `targetBranchBindingId`.
- `branch.select` success may switch selected branch only when the operation id is current, the active-run revision still equals `source.activeRunRevision`, and the active run still matches `source.sessionRunId`, `source.branchBindingId`, and `source.agentRunId`.
- `branch.select` success must reject a status response whose selected branch binding id differs from `targetBranchBindingId`.
- Failure may emit `sessionRun.operation.error` only when the operation id is current and the active-run revision still matches the captured operation revision. It must complete the operation whether the error is visible or suppressed.
- Any active-run revision change outside the operation invalidates the operation even if the visible identity later changes back to the same session and branch.
- A stale success or stale failure must still clear the Host-side current operation if and only if it is the same operation id. It must not clear a newer operation.

## Target Webview Message Gate

Use this as the contract for Webview message routing.

```ts
export type SessionRunMessageScope =
  | "operation-pending"
  | "establish-run"
  | "operation-result"
  | "operation-error"
  | "current-branch"
  | "branch-summary"
  | "legacy-current-branch"

export interface PendingSessionRunOperationView {
  operationId: string
  kind: "start" | "branch.create" | "branch.select"
  createdAt: number
  sessionRunId?: string
  sourceBranchBindingId?: string
  targetBranchBindingId?: string
}

export interface SessionRunMessageGateContext {
  activeSessionRunId: string
  selectedBranchBindingId: string
  pendingOperation?: PendingSessionRunOperationView
}
```

Gate rules:

- `operation-pending`: sets or replaces the pending operation only when no active pending operation exists, or when the incoming operation is explicitly superseding the current pending operation with `supersedesOperationId`.
- `operation-result`: requires `operationId` and matching pending operation. If `sessionRunId` is present, it must match the pending operation or the active run allowed by that operation kind.
- `operation-error`: requires `operationId` and matching pending operation. It clears the pending operation and may append an error notice. It must not call selected-run terminal cleanup unless the failed operation is the current `start` operation and no active run has been established.
- `current-branch`: requires current active run when `sessionRunId` is present. If `branchBindingId` is present, it must equal selected branch.
- `branch-summary`: requires current active run when `sessionRunId` is present. It can update summaries but cannot write transcript, terminal status, active session id, or selected branch.
- `establish-run`: only applies to current `start` operation. It can set `activeSessionRunId`.
- `legacy-current-branch`: allowed only for messages without `sessionRunId`, without `operationId`, and with no new protocol path available. It falls back to selected branch only.

Allowed side effects:

| Message | Required Scope | Allowed State Changes |
| --- | --- | --- |
| `sessionRun.operation.pending` | `operation-pending` | set pending operation only |
| `sessionRun.session` | `establish-run` | set active session/run ids, selected branch, runtime state, pending cancel routing |
| `sessionRun.branch.started` | `operation-result` | select target branch, mark running, clear branch compose |
| `sessionRun.branch.selected` | `operation-result` | select target branch, replace selected projection, clear pending operation |
| `sessionRun.operation.error` | `operation-error` | clear pending operation, append notice, clear start-only local working state when no run exists |
| `sessionRun.branches` | `branch-summary` | update `branchSummaries` only |
| `sessionRun.events` / `sessionRun.stream` | `current-branch` | write selected transcript events only |
| `sessionRun.done` / `sessionRun.cancelled` / `sessionRun.error` | `current-branch` | apply terminal state to selected branch only |
| approval reply errors / user-input reply errors | `current-branch` | update matching pending item and optionally append visible notice |

No ChatView handler may bypass the gate for `sessionRun.operation.pending`, `sessionRun.session`, `sessionRun.branch.started`, `sessionRun.branch.selected`, `sessionRun.operation.error`, `sessionRun.branches`, `sessionRun.events`, `sessionRun.stream`, `sessionRun.done`, `sessionRun.cancelled`, `sessionRun.error`, approval reply errors, or user-input reply errors.

## Target Backend Resolver

Use this as the contract for the backend resolver module.

```python
from dataclasses import dataclass
from enum import Enum
from typing import Any


class SessionRunControlResolutionKind(str, Enum):
    FOUND = "found"
    INVALID_TOKEN = "invalid_token"
    PROJECTION_UNAVAILABLE = "projection_unavailable"
    MISSING = "missing"
    FORBIDDEN = "forbidden"
    BRANCH_BINDING_REQUIRED = "branch_binding_required"
    BRANCH_BINDING_NOT_FOUND = "branch_binding_not_found"
    BINDING_STORE_UNAVAILABLE = "binding_store_unavailable"


@dataclass(frozen=True)
class SessionRunControlPolicy:
    require_branch_binding_id: bool
    allow_selected_branch_default: bool
    requested_branch_binding_id: str = ""


@dataclass(frozen=True)
class SessionRunControlResolution:
    kind: SessionRunControlResolutionKind
    peer_id: str = ""
    session: Any | None = None
    binding: Any | None = None
    branch_binding_id: str = ""
    error: Exception | None = None
```

Resolver rules:

- Invalid peer token returns `INVALID_TOKEN`.
- The resolver receives a `SessionRunControlPolicy` per route. It decides whether a missing request branch binding can default to the selected branch.
- In-memory projection found returns `FOUND` only after resolving a concrete branch binding target and verifying peer ownership.
- In-memory projection found plus required missing branch binding id returns `BRANCH_BINDING_REQUIRED`.
- In-memory projection found plus requested branch binding not found returns `BRANCH_BINDING_NOT_FOUND`.
- In-memory projection missing plus persisted matching binding returns `PROJECTION_UNAVAILABLE`; no route may operate without the projection.
- In-memory projection missing plus no binding returns `MISSING`.
- Persisted binding found for another peer returns `FORBIDDEN`.
- Binding lookup exception returns `BINDING_STORE_UNAVAILABLE`.
- A route never catches resolver store exceptions and converts them to not found.
- `_selected_session_run_binding()` and `_session_run_binding_for_branch()` must either be removed from the SessionRun control path or converted into private pure helpers used only by the resolver. They must not send HTTP errors directly after this refactor.

HTTP mapping:

- `INVALID_TOKEN` -> `401 invalid_peer_token`
- `PROJECTION_UNAVAILABLE` -> `409 session_run_projection_unavailable`
- `MISSING` -> `404 session_run_not_found`
- `FORBIDDEN` -> `403 session_run_binding_peer_mismatch`
- `BRANCH_BINDING_REQUIRED` -> `400 branch_binding_id_required`
- `BRANCH_BINDING_NOT_FOUND` -> `404 session_run_branch_binding_not_found`
- `BINDING_STORE_UNAVAILABLE` -> `503 session_run_binding_store_unavailable`

Route policies:

| Route | `require_branch_binding_id` | `allow_selected_branch_default` |
| --- | ---: | ---: |
| `/remote/session-runs/continue` | yes | no |
| `/remote/session-runs/events` | no | yes |
| `/remote/session-runs/status` | no | yes |
| `/remote/session-runs/recover` | yes | no |
| `/remote/session-runs/cancel` | yes | no |
| `/remote/session-runs/user-input/reply` | yes | no |
| `/remote/session-runs/approval/reply` | yes | no |
| `/remote/session-runs/branches/select` | yes | no |

`/remote/session-runs/branches/select` keeps `branch_binding_id` as a structurally required protocol field. A missing field may fail as `invalid_session_run_branch_select_request` before resolver policy runs. The resolver still owns peer and binding lookup once the request is structurally valid.

## Execution Tasks

### Task 1: Replace Review Findings With Behavior Tests

**Files:**
- Modify: `../Labrastro-vscode-extension/src/LabrastroController.session-run-correlation.test.ts`
- Modify: `../Labrastro-vscode-extension/webview-ui/src/components/ChatView.context-events.test.ts`
- Modify: `tests/labrastro_server/http/test_remote_service.py`

- [ ] **Step 1: Add Host failure-cleanup regression tests**

Add tests with these names:

```ts
it("suppresses stale branch selection failure after active branch changes", async () => {
  const operationId = "op-select-a"
  // Begin selecting branch-a from main, change the active branch to branch-b before rejection,
  // reject the select promise, and assert no sessionRun.operation.error is emitted.
})

it("rejects branch create response when current active run lost the source agentRunId", async () => {
  const operationId = "op-create-a"
  // Begin creating branch-a from source agent-current, clear activeRun.agentRunId before success,
  // resolve the branch promise, and assert no sessionRun.branch.started is emitted.
})

it("rejects branch selection success after active run changes away and back to the same branch", async () => {
  const operationId = "op-select-aba"
  // Begin selecting branch-a from main at activeRunRevision N, switch to branch-b,
  // switch back to main at revision N+2, resolve the old select promise,
  // and assert the selected branch remains main.
})

it("rejects branch create success when the response branch does not equal the operation target", async () => {
  const operationId = "op-create-target"
  // Begin creating branch-a, resolve with branch-b, and assert no active run patch or stream start occurs.
})
```

Run:

```powershell
Push-Location ..\Labrastro-vscode-extension
npx vitest run src/LabrastroController.session-run-correlation.test.ts
Pop-Location
```

Expected: the new tests fail against the current patch.

- [ ] **Step 2: Add Webview pending-operation failure tests**

Add tests with these names:

```ts
it("clears pending branch selection when the matching operation fails", async () => {
  const operationId = "op-select-a"
  // Dispatch branch select request with operationId, then matching sessionRun.operation.error.
  // Dispatch an old sessionRun.branch.selected for the same target and operationId.
  // Assert selected branch remains the original branch and no terminal run error state is applied.
})

it("rejects operation result when operationId does not match the pending operation", async () => {
  const pendingOperationId = "op-current"
  const staleOperationId = "op-stale"
  // Dispatch a pending branch select with pendingOperationId, then dispatch
  // sessionRun.branch.selected with staleOperationId. Assert no selected branch change,
  // no transcript replacement, and no running state change.
})

it("treats operation errors as command failures rather than selected-run terminal failures", async () => {
  const operationId = "op-select-error"
  // Dispatch a matching sessionRun.operation.error for branch select.
  // Assert the pending operation clears, an error notice is visible, and sessionRunStatus is unchanged.
})
```

Run:

```powershell
Push-Location ..\Labrastro-vscode-extension
npx vitest run webview-ui/src/components/ChatView.context-events.test.ts
Pop-Location
```

Expected: the new tests fail against the current patch.

- [ ] **Step 3: Add backend store-failure tests**

Add a test with this name:

```python
def test_session_run_control_routes_report_binding_store_unavailable_when_binding_lookup_fails(self) -> None:
    # Arrange a runtime control plane whose binding lookup raises RuntimeError.
    # Remove the in-memory SessionRun projection.
    # Assert continue/events/status/recover/cancel/user-input reply return 503
    # with error "session_run_binding_store_unavailable".
```

Add a second resolver-policy test:

```python
def test_session_run_control_resolver_enforces_route_branch_binding_policy(self) -> None:
    # Start a bound SessionRun with a selected main branch.
    # Assert events/status without branch_binding_id use the selected branch and return 200.
    # Assert continue/recover/cancel/user-input reply/approval reply without
    # branch_binding_id return 400 with error "branch_binding_id_required".
    # Assert branch select without branch_binding_id remains a parser-level
    # invalid_session_run_branch_select_request error.
```

Run:

```powershell
Push-Location .\Labrastro
uv run python -m pytest tests/labrastro_server/http/test_remote_service.py -q
Pop-Location
```

Expected: the new test fails against the current patch.

### Task 2: Add Host Operation Coordinator

**Files:**
- Create: `../Labrastro-vscode-extension/src/coordinators/SessionRunOperationCoordinator.ts`
- Modify: `../Labrastro-vscode-extension/src/LabrastroController.ts`
- Modify: `../Labrastro-vscode-extension/src/coordinators/SessionRunCoordinator.ts`
- Test: `../Labrastro-vscode-extension/src/LabrastroController.session-run-correlation.test.ts`

- [ ] **Step 1: Create the coordinator module**

Create `src/coordinators/SessionRunOperationCoordinator.ts` with the operation model from "Target Host Operation Model" and these exported methods:

```ts
export class SessionRunOperationCoordinator {
  begin(operation: SessionRunOperation): SessionRunOperation
  currentOperationId(): string
  current(): SessionRunOperation | undefined
  complete(operationId: string): void
  invalidate(): void
  acceptsStartSuccess(operationId: string, sessionRunId: string, activeRun: { sessionRunId?: string } | undefined, activeRunRevision: number): boolean
  acceptsBranchCreateSuccess(operationId: string, responseBranchBindingId: string, activeRun: { sessionRunId?: string; branchBindingId?: string; agentRunId?: string } | undefined, activeRunRevision: number): boolean
  acceptsBranchSelectSuccess(operationId: string, responseBranchBindingId: string, activeRun: { sessionRunId?: string; branchBindingId?: string; agentRunId?: string } | undefined, activeRunRevision: number): boolean
  acceptsFailure(operationId: string, activeRunRevision: number): boolean
}
```

Required behavior:

- `complete()` clears only the matching current operation.
- `acceptsFailure()` returns true only for the current operation id and matching active-run revision, then clears that operation.
- Branch success methods require exact `source.sessionRunId`, `source.branchBindingId`, `source.agentRunId`, `source.activeRunRevision`, and target branch equality.
- A success or failure for an older operation id must not clear a newer operation.
- A success or failure for the same operation id but changed active-run revision must clear that operation and return false.

- [ ] **Step 2: Run coordinator unit tests**

Add focused tests in `src/coordinators/SessionRunOperationCoordinator.test.ts`.

Run:

```powershell
Push-Location ..\Labrastro-vscode-extension
npx vitest run src/coordinators/SessionRunOperationCoordinator.test.ts src/LabrastroController.session-run-correlation.test.ts
Pop-Location
```

Expected: coordinator tests pass; integration tests still fail until controller refactor is complete.

### Task 3: Refactor Host Async Entry Points Through The Coordinator

**Files:**
- Modify: `../Labrastro-vscode-extension/src/LabrastroController.ts`
- Modify: `../Labrastro-vscode-extension/src/coordinators/SessionRunCoordinator.ts`
- Modify: `../Labrastro-vscode-extension/webview-ui/src/chat/chatMessages.ts`
- Modify: `../Labrastro-vscode-extension/src/protocol/messages.ts`
- Test: `../Labrastro-vscode-extension/src/LabrastroController.session-run-correlation.test.ts`
- Test: `../Labrastro-vscode-extension/src/coordinators/SessionRunCoordinator.test.ts`
- Test: `../Labrastro-vscode-extension/webview-ui/src/chat/chatState.test.ts`
- Test: `../Labrastro-vscode-extension/src/protocol/messages.test.ts`

- [ ] **Step 1: Thread Webview operation ids into Host handlers**

Update message payloads and coordinator option signatures:

```ts
export interface ChatBranchInput {
  operationId: string
  baseSessionItemId: string
  prompt: string
  branchBindingId?: string
  sourceLabel?: string
  sourceMessageId?: string
  sourceNodeId?: string
  composeMode?: "edit" | "fork"
}

export interface ChatBranchSelectInput {
  operationId: string
  branchBindingId: string
}
```

`chatMessages.branch()` and `chatMessages.selectBranch()` must post `operationId`. `SessionRunCoordinator.handleMessage()` must pass that id to `branchSessionRun()` and `selectSessionRunBranch()`. For `chat.send`, ChatView must provide `operationId`; `SessionRunCoordinator` passes it to `startSessionRun()` only when the send starts a new SessionRun.

- [ ] **Step 2: Add active-run revision**

Add a revision getter to `SessionRunCoordinator`:

```ts
get activeRunRevision(): number
```

Increment the revision inside `setActiveRun()` every time it is called, including clear. `patchActiveRun()` must increment through `setActiveRun()`. Operation creation captures this revision before awaiting any remote call.

- [ ] **Step 3: Replace start response guard**

In `startSessionRun()`, create a `start` operation before awaiting the concrete `this.client.startSessionRun(text, sessionId, options)` request. After the await, call `acceptsStartSuccess(operationId, sessionRunId, this.sessionRunCoordinator.activeRun, this.sessionRunCoordinator.activeRunRevision)` before `setActiveRun()`, emitting `sessionRun.session`, or consuming stream.

When `startSessionRun()` is called without a Webview-originated `operationId`, Host must generate one and emit this pending message before awaiting the remote call:

```ts
{
  type: "sessionRun.operation.pending",
  operationId,
  operationKind: "start",
}
```

The emitted `sessionRun.session` message must include:

```ts
{
  type: "sessionRun.session",
  operationId,
  operationKind: "start",
  sessionRunId,
  sessionId,
  branchBindingId: branchBindingId || "main",
  branch_binding_id: branchBindingId || "main",
}
```

- [ ] **Step 4: Replace branch create response guard**

In `branchSessionRun()`, create a `branch.create` operation with exact source `sessionRunId`, `sourceBranchBindingId`, `sourceAgentRunId`, and `activeRunRevision`. After awaiting the concrete `this.client.branchAgentRun(request)` call, call `acceptsBranchCreateSuccess(operationId, responseBranchBindingId, this.sessionRunCoordinator.activeRun, this.sessionRunCoordinator.activeRunRevision)` before `patchActiveRun()`, emitting `sessionRun.branch.started`, or starting stream.

The emitted `sessionRun.branch.started` message must include `operationId` and `operationKind: "branch.create"`.

- [ ] **Step 5: Replace branch select response guard**

In `selectSessionRunBranch()`, create a `branch.select` operation with exact source identity, source `activeRunRevision`, and target `branchBindingId`. After awaiting the concrete `this.client.selectSessionRunBranch(request)` call, call `acceptsBranchSelectSuccess(operationId, responseBranchBindingId, this.sessionRunCoordinator.activeRun, this.sessionRunCoordinator.activeRunRevision)` before `patchActiveRun()`, emitting `sessionRun.branch.selected`, fetching events, or starting stream.

The emitted `sessionRun.branch.selected` message must include `operationId` and `operationKind: "branch.select"`.

- [ ] **Step 6: Replace error handling**

For each operation catch block, call `acceptsFailure(operationId, this.sessionRunCoordinator.activeRunRevision)`. Emit `sessionRun.operation.error` only when the failure is accepted.

The emitted operation error must include:

```ts
{
  type: "sessionRun.operation.error",
  operationId,
  operationKind,
  sessionRunId,
  branchBindingId: targetOrSourceBranchBindingId,
  branch_binding_id: targetOrSourceBranchBindingId,
  message: chatErrorMessage(error),
}
```

- [ ] **Step 7: Remove patch helpers**

Remove these helpers from `LabrastroController.ts`:

```ts
activeSessionRunContextMatches
sessionRunStartResponseStillCurrent
```

Run:

```powershell
Push-Location ..\Labrastro-vscode-extension
npx vitest run src/LabrastroController.session-run-correlation.test.ts src/coordinators/SessionRunCoordinator.test.ts webview-ui/src/chat/chatState.test.ts src/protocol/messages.test.ts
Pop-Location
```

Expected: Host correlation and protocol tests pass.

### Task 4: Add Webview Message Gate And Remove Pending Branch Id Guessing

**Files:**
- Create: `../Labrastro-vscode-extension/webview-ui/src/chat/sessionRunMessageGate.ts`
- Modify: `../Labrastro-vscode-extension/webview-ui/src/components/ChatView.tsx`
- Test: `../Labrastro-vscode-extension/webview-ui/src/components/ChatView.context-events.test.ts`

- [ ] **Step 1: Create the pure gate module**

Create `webview-ui/src/chat/sessionRunMessageGate.ts` with the model from "Target Webview Message Gate" and pure functions:

```ts
export function shouldApplyOperationResult(
  context: SessionRunMessageGateContext,
  message: { operationId?: unknown; sessionRunId?: unknown; branchBindingId?: unknown; operationKind?: unknown },
): boolean

export function shouldApplyOperationPending(
  context: SessionRunMessageGateContext,
  message: { operationId?: unknown; operationKind?: unknown; supersedesOperationId?: unknown },
): boolean

export function shouldApplyOperationError(
  context: SessionRunMessageGateContext,
  message: { operationId?: unknown; sessionRunId?: unknown; branchBindingId?: unknown; operationKind?: unknown },
): boolean

export function shouldApplyCurrentBranchMessage(
  context: SessionRunMessageGateContext,
  message: { sessionRunId?: unknown; branchBindingId?: unknown },
): boolean

export function shouldApplyBranchSummaryMessage(
  context: SessionRunMessageGateContext,
  message: { sessionRunId?: unknown },
): boolean
```

Required behavior:

- `shouldApplyOperationPending()` rejects a second pending operation unless it names the current pending operation in `supersedesOperationId`.
- `shouldApplyOperationResult()` requires exact `operationId` match and matching operation kind. For `branch.create` and `branch.select`, message branch binding must equal `pendingOperation.targetBranchBindingId`.
- `shouldApplyOperationError()` requires exact `operationId` match and matching operation kind. For branch operations, message branch binding must equal the pending target or source branch recorded by the operation.
- `shouldApplyCurrentBranchMessage()` rejects any message with `sessionRunId` when no active session run exists.
- `shouldApplyCurrentBranchMessage()` rejects branch-scoped messages whose branch does not equal selected branch.
- `shouldApplyBranchSummaryMessage()` never authorizes transcript or terminal status writes.

- [ ] **Step 2: Replace ChatView pending selection state**

Remove:

```ts
const [pendingBranchSelectionId, setPendingBranchSelectionId] = createSignal("")
messageTargetsCurrentRunOrPendingBranchSelection
```

Add pending operation state shaped like:

```ts
const [pendingSessionRunOperation, setPendingSessionRunOperation] =
  createSignal<PendingSessionRunOperationView | undefined>()
```

Start, branch create, and branch select requests set pending operation from the Webview-generated `operationId` before posting the Webview-to-Host message. Success and `sessionRun.operation.error` clear it.

- [ ] **Step 3: Gate all SessionRun message handlers**

Route each handler through the gate:

- `sessionRun.operation.pending`: `operation-pending`
- `sessionRun.session`: `operation-result` / `establish-run`
- `sessionRun.branch.started`: `operation-result`
- `sessionRun.branch.selected`: `operation-result`
- `sessionRun.branches`: `branch-summary`
- `sessionRun.events` and `sessionRun.stream`: `current-branch`
- `sessionRun.operation.error`: `operation-error`
- `sessionRun.done`, `sessionRun.cancelled`, `sessionRun.error`: `current-branch`
- approval and user-input reply errors: `current-branch`

No handler should call a local ad hoc session/branch check after this refactor.

- [ ] **Step 4: Replace source-string tests**

Remove assertions that require exact source fragments such as:

```ts
expect(source).toContain("const messageTargetsCurrentRun =")
expect(source).toContain("if (!messageTargetsCurrentRun(sessionRunId, branchBindingId)) return")
```

Replace them with dispatch-and-observe tests:

```ts
it("ignores terminal messages for a run when no active SessionRun exists", async () => {
  // Dispatch sessionRun.done with sessionRunId after activeSessionRunId is cleared.
  // Assert no finish state is applied.
})
```

Run:

```powershell
Push-Location ..\Labrastro-vscode-extension
npx vitest run webview-ui/src/components/ChatView.context-events.test.ts
Pop-Location
```

Expected: Webview context-event tests pass without source-shape assertions.

### Task 5: Replace Backend Route Fallback With Typed Resolver

**Files:**
- Create: `labrastro_server/interfaces/http/remote/session_run_control.py`
- Modify: `labrastro_server/interfaces/http/remote/routes/chat.py`
- Test: `tests/labrastro_server/http/test_remote_service.py`

- [ ] **Step 1: Create resolver result types**

Create `session_run_control.py` with the enum and dataclass from "Target Backend Resolver".

- [ ] **Step 2: Move missing-projection binding lookup into resolver**

The resolver must call `list_session_run_bindings(session_run_id=session_run_id)` or `find_session_run_binding(session_run_id=session_run_id)` exactly once per resolution path. Any exception is captured in `BINDING_STORE_UNAVAILABLE`.

- [ ] **Step 3: Map resolver outcomes in routes**

Replace `_get_session_run_control()` with a function that converts resolver results into route responses. Remove `_persisted_binding_for_missing_session_run_projection()`.

The same mapping must be used by:

- `/remote/session-runs/continue`
- `/remote/session-runs/events`
- `/remote/session-runs/status`
- `/remote/session-runs/recover`
- `/remote/session-runs/cancel`
- `/remote/session-runs/user-input/reply`
- `/remote/session-runs/approval/reply`
- `/remote/session-runs/branches/select`

- [ ] **Step 4: Run backend tests**

Run:

```powershell
Push-Location .\Labrastro
uv run python -m pytest tests/labrastro_server/http/test_remote_service.py::TestRemoteRelayHTTPService::test_session_run_control_routes_report_unavailable_projection_when_binding_persists tests/labrastro_server/http/test_remote_service.py -q
Pop-Location
```

Expected: missing projection with persisted binding returns `409 session_run_projection_unavailable`; binding store failure returns `503 session_run_binding_store_unavailable`.

### Task 6: Lock Branch Lifecycle Scope

**Files:**
- Modify: `docs/superpowers/plans/2026-06-17-sessionrun-agentrun-execution-convergence.md`
- Test: source scan only

- [ ] **Step 1: Keep lifecycle checklist honest**

Keep these checklist items unchecked until public SessionRun lifecycle APIs exist:

- Branch switch/hide/stop/delete are separate operations.
- Branch lifecycle backend commands separate selection, hiding, stopping, binding close, and resource cleanup.

- [ ] **Step 2: Add guard text**

Add one sentence near the branch lifecycle correction:

```markdown
The async correlation repair only protects select/switch and branch-scoped stream/event routing; it does not implement hide, close binding, or destructive delete/resource cleanup.
```

- [ ] **Step 3: Scan for false completion wording**

Run:

```powershell
Push-Location .\Labrastro
rg -n "branch lifecycle.*complete|close branch|delete branch" docs labrastro_server tests
Pop-Location
```

Expected: no documentation says the full lifecycle API is complete.

### Task 7: Final Verification

**Files:**
- All files touched by Tasks 1-6

- [ ] **Step 1: Extension tests**

Run:

```powershell
Push-Location ..\Labrastro-vscode-extension
npm run typecheck
npx vitest run src/LabrastroController.session-run-correlation.test.ts src/LabrastroController.chat-stream.test.ts webview-ui/src/components/ChatView.context-events.test.ts webview-ui/src/chat/approval-state.test.ts webview-ui/src/chat/user-input-state.test.ts src/protocol/messages.test.ts
Pop-Location
```

Expected: typecheck and listed Vitest suites pass.

- [ ] **Step 2: Backend tests**

Run:

```powershell
Push-Location .\Labrastro
uv run python -m pytest tests/labrastro_server/http/test_remote_service.py tests/labrastro_server/http/test_protocol.py tests/labrastro_server/services/agent_runtime/test_contract_scan.py -q
Pop-Location
```

Expected: listed pytest suites pass.

- [ ] **Step 3: Source scans**

Run:

```powershell
Push-Location ..\Labrastro-vscode-extension
rg -n "activeSessionRunContextMatches|sessionRunStartResponseStillCurrent|pendingBranchSelectionId|messageTargetsCurrentRunOrPendingBranchSelection" src webview-ui/src
rg -n "type: \"sessionRun.error\".*operationId|operation_id|operation error" src webview-ui/src
Pop-Location

Push-Location .\Labrastro
rg -n "_persisted_binding_for_missing_session_run_projection|_selected_session_run_binding|_session_run_binding_for_branch|except Exception:\\s*$" labrastro_server/interfaces/http/remote/routes/chat.py
Pop-Location
```

Expected: removed patch helpers do not appear. Operation errors do not use terminal `sessionRun.error`. Route-local SessionRun control binding helpers do not remain in `routes/chat.py`. Broad `except Exception` in `routes/chat.py` is either gone from the SessionRun control path or justified outside this repair scope.

- [ ] **Step 4: Git hygiene**

Run:

```powershell
Push-Location ..\Labrastro-vscode-extension
git diff --check
node .gitnexus/run.cjs detect-changes --repo Labrastro-vscode-extension
Pop-Location

Push-Location .\Labrastro
git diff --check
node .gitnexus/run.cjs detect-changes --repo Labrastro
Pop-Location
```

Expected: diff check passes and GitNexus affected scope matches the files named in this plan.

## Completion Criteria

The repair is complete only when all statements below are true:

- No current SessionRun operation is represented by a branch id alone.
- Every visible user-originated start/branch-create/branch-select operation has a Webview-generated `operationId` before the request is posted.
- Host operation success and failure share the same acceptance and cleanup path.
- Host operation acceptance checks active-run revision, not only current ids, so ABA branch changes cannot revive stale responses.
- Webview has one gate for SessionRun Host messages.
- A message with `sessionRunId` cannot apply when no active run exists, except a current accepted start operation result.
- Branch select failure clears pending operation and cannot authorize a later stale selected response.
- Operation failure uses `sessionRun.operation.error` and does not trigger selected-run terminal cleanup.
- Branch create/select Host responses require exact source `sessionRunId`, source branch binding id, source `agentRunId`, source active-run revision, and target branch binding id.
- Backend resolver returns a concrete branch binding target for every SessionRun control route that mutates or reads branch-scoped state.
- Backend store lookup failure is visible as service unavailable, not not found.
- Tests prove behavior by dispatching operations/messages, not by asserting local guard source strings.
- The 2026-06-17 plan remains honest about branch lifecycle work that is not implemented by this repair.

## Closed Scope Boundary

This repair must not implement the full branch hide/close/delete lifecycle API. It only prevents false completion claims and keeps the lifecycle checklist unchecked. Implementing lifecycle commands is a separate backend/frontend product surface with destructive-resource semantics, and mixing it into async correlation repair increases drift risk.

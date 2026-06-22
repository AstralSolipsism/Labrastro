# SessionRun Runtime Scope Boundary Closure Contract

> Status: pre-goal review contract. Do not execute implementation from this
> document until the user has reviewed the document, resolved semantic forks,
> and explicitly asks to set an active goal. Any later active goal must assume
> this file already exists before execution begins.

## Purpose

This document exists to close the boundary gap left after the 2026-06-20
Pi-agent runtime model rebuild work. It is not a new architecture direction and
does not replace the core model from the 2026-06-20 document. It turns the
remaining risk into a smaller, harder execution contract:

- every SessionRun UI-mutating path must be accounted for;
- every success, failure, fallback, reconnect, and restore path must use the
  same scoped runtime ownership model;
- every old patch-model authority must be removed or proven to be a stateless
  adapter;
- no local guard patch may be accepted as a closure.

This document is intentionally not divided into separately completable work
slices. Boundary inventory, repair, same-class search, verification, review, and
evidence are one indivisible closure. Completing only one part never means the
goal is complete.

## Authority Relationship

The authority stack for future execution is:

1. This document controls the current boundary-closure execution.
2. `2026-06-20-sessionrun-pi-agent-runtime-model-rebuild.md` remains the model
   reference for "tree + runtime scope + visible projection".
3. `2026-06-17-*` and `2026-06-18-*` documents remain historical evidence and
   problem records only.

If this document conflicts with the 2026-06-20 architecture principles, the
contract is insufficient for execution and must be amended before code changes
continue. If the conflict is only that the 2026-06-20 execution evidence claimed
completion but current code still has a boundary hole, current code and this
closure contract take precedence.

## Contract Application Rule

This contract has no known unresolved product choice.

Implementation details already determined by this document, the 2026-06-20
model, or existing Labrastro product semantics must be executed directly. Write
those decisions directly into this contract.

If future execution discovers a product behavior that is not covered by this
contract, that behavior is outside the current closure. Stop before code changes
for that behavior and amend the corresponding contract section for review. Do
not keep a separate unresolved-item list.

## Root Cause Statement

The repeated defects are not caused by the Pi-inspired target model being wrong.
They are caused by incomplete execution of that model:

- some success paths were moved under scoped runtime ownership;
- some exceptional, fallback, bootstrap, reconnect, or restore paths still kept
  old patch-model authority;
- reviews found the next bypass only after the previous symptom was fixed;
- prior goals allowed "fix the currently reported issue" to substitute for
  "clear the same-class boundary family".

The current known example is `activeRunPayloadWithServerStatus()`: the success
path checks current active-run identity, while the non-404 failure fallback can
return stale payload and allow bootstrap resume or stream restart for an old
run. The correct repair is not another handler-local special case. The repair
must place status-refresh fallback under the same scoped runtime ownership rule
as status success, resume, and stream start.

## Non-Negotiable Model Contract

- `SessionBranchTree + BranchRuntimeScope + VisibleSessionProjection` is the
  target ownership model.
- `selectedBranchBindingId` is a visible projection pointer only.
- `activeSessionRunId` is a visible projection field only.
- A global pending operation slot is not lifecycle authority.
- Start is the only SessionRun lifecycle path allowed to create the initial
  missing runtime scope.
- Every later SessionRun lifecycle, control, stream, restore, or UI-mutating
  message must carry enough proof to resolve a concrete `BranchRuntimeScope`.
- Host is the only boundary that converts backend/raw async responses into
  scoped Host/Webview SessionRun messages.
- Webview may verify known scopes and apply reducer effects, but must not
  reconstruct async ownership from visible UI state.
- Status and events are UI-driving for this architecture. Even if an endpoint is
  read-shaped, any response that can affect SessionRun UI must be scope-resolved.
- Missing branch/run/agent proof is a fail-closed condition unless this document
  explicitly names the path as start-created initial scope.
- This closure does not implement destructive branch lifecycle semantics such as
  hiding branches, closing bindings, deleting branch resources, or resource
  cleanup. Those operations require their own product/resource contract before
  implementation.
- This closure uses the existing branch/runtime proof model. Do not introduce a
  backend protocol shape change unless this contract is amended first. Missing
  proof must fail closed rather than being patched with a compatibility field.

## Scope Terms

- A known scope is a `BranchRuntimeScope` already present in the scoped runtime
  model.
- Scope creation is allowed only through entries explicitly named in this
  document: start creates the initial main scope; branch.create creates a
  provisional target scope; bootstrap restore may restore a strongly proven
  startup scope; branch summaries may ensure metadata-only sibling scopes.
- Stream, events, terminal lifecycle messages, operation errors, interaction
  replies, and status fallback must not create a scope from selected UI state.
- A sibling scope is any known scope in the same SessionRun that is not the
  currently selected visible scope.
- Unknown or proof-mismatched scopes fail closed unless the current entry is one
  of the explicit scope-creation entries above.

## Resolved Product Decisions

### Bootstrap Resume

Decision: keep `bootstrap resume`, but only as a startup restore entry with
strong proof.

`bootstrap resume` is allowed because the product should recover a running
SessionRun after Webview reload or VS Code restore. It is not a general fallback
path, not a selected-UI-state inference path, and not a way to trust stale
`activeRunPayload` when status refresh fails.

Required behavior:

- the message must carry `bootstrapRestore: true`;
- the payload must carry concrete `sessionRunId` and `branchBindingId`;
- Host must reject the restore when the current active scope conflicts with the
  payload;
- status-refresh non-404 failure may return the payload only when the current
  active scope still exactly matches that payload;
- if the payload no longer matches, Host must not emit `sessionRun.resume` and
  must not start or restart the stream;
- Webview may apply the restore only through scoped restore/reducer effects.

### Status Refresh Failure During Restore

Decision: allow degraded restore on non-404 status refresh failure only when the
current scope still exactly matches the restore payload.

This preserves the reload/restore experience during transient backend or network
failure, while still blocking stale-run drift.

Required behavior:

- `session_run_not_found` / 404 means the run is gone and must not be restored;
- non-404 status failure may use the local payload only after scoped identity is
  rechecked;
- the recheck must compare the current active scope with the payload's
  `sessionRunId` and `branchBindingId`;
- if the payload carries `agentRunId`, it must also match the current scope;
- if the recheck fails, Host must not emit `sessionRun.resume` and must not
  start or restart a stream;
- this degraded path must be tested separately from the successful status path.

### Stream Restart After Degraded Restore

Decision: allow immediate stream restart after degraded restore only for the
same verified scope.

This preserves the expected "reload and keep receiving output" experience. The
stream restart is not a separate ownership shortcut. It is allowed only because
the restore payload already matched the current scope, and every stream message
must still pass scoped runtime reduction before it can affect visible UI.

Required behavior:

- degraded restore may start or restart the stream only for the exact
  `sessionRunId + branchBindingId` that passed restore recheck;
- the stream key must be derived from that verified scope, not from selected UI
  state;
- stream events, live deltas, terminal messages, reconnect messages, and errors
  must still pass their own scoped reducer checks;
- a restarted stream for a known sibling scope may update that sibling scope,
  and may affect visible UI only when the target scope is selected;
- a restarted stream for an unknown or proof-mismatched scope must fail closed;
- tests must cover a degraded restore stream restart after branch switch and
  prove that stale stream output cannot mutate the selected transcript.

### User Notice For Degraded Restore

Decision: degraded restore is silent by default. It must not append a user-visible
notice merely because status refresh failed with a non-404 error.

This avoids noisy reload behavior during transient backend or network failures.
The user-visible state should come from subsequent scoped reconnect, stream,
status, or runtime error handling. Degraded restore itself is an ownership-safe
bootstrap behavior, not a selected-run failure.

Required behavior:

- degraded restore must not append a transcript notice;
- degraded restore must not mark the selected run as error;
- degraded restore may record internal diagnostics or scoped non-visible
  metadata, but those records must not drive visible UI by themselves;
- subsequent reconnect, stream, status, or runtime error messages may update the
  UI only after resolving to the selected scope;
- tests must prove status-refresh degraded restore does not append a selected
  transcript notice or terminal error.

### Sibling Branch Stream Updates

Decision: allow a sibling branch stream to update its own `BranchRuntimeScope`,
but never allow it to write the currently selected transcript unless that sibling
scope is selected.

Labrastro intentionally supports sibling branches running in the background. A
non-selected branch stream is therefore not automatically stale. It is stale only
for the currently visible projection. The model must separate "update the target
scope" from "produce visible transcript effects".

Required behavior:

- a stream message for a known sibling scope may update that sibling scope's
  runtime status, transcript projection, queue, terminal state, update marker,
  and last-event timestamp;
- the same stream message may produce visible transcript/live-delta effects only
  when the target scope is the selected visible scope;
- sibling terminal messages must not finish, cancel, or error the selected
  branch;
- switching back to a sibling branch may show the latest scoped state already
  accumulated for that branch;
- tests must prove that sibling stream updates are retained for that scope while
  the selected transcript remains unchanged.

### Sibling Branch Visible Update Markers

Decision: sibling branch background updates may refresh branch summary metadata,
but must not write the selected transcript or selected-branch notice surface.

This keeps background branch execution visible without interrupting the user's
current branch. A background branch can show that it is running, has updates, has
finished, or has failed, but that information belongs to branch metadata and
branch list presentation.

Required behavior:

- sibling updates may update branch summary/status fields such as `has_updates`,
  runtime status, terminal status, and last-event time;
- sibling updates must not append notices to the selected transcript;
- sibling errors must not mark the selected branch as failed;
- selected branch notices may be emitted only when the target scope is selected;
- tests must prove that a sibling error/update changes branch metadata without
  adding a current transcript notice or terminal state.

### Old Helper Adapter Retention

Decision: old helper names may remain only as proven stateless adapters.

Keeping a name such as `activeSessionRunMatches`,
`SessionRunOperationCoordinator`, `SessionRunSourceIdentityResolver`, or
`sessionRunMessageGate` is acceptable only when the helper no longer owns async
authority. Name retention is not model retention.

Required behavior:

- every retained old helper must delegate to `SessionRuntimeStore`, scoped
  runtime reducer logic, or another model-owned proof source;
- every retained old helper must be listed in final evidence with its call
  sites, delegated owner, and proof that it is stateless;
- a retained helper must not read selected UI state as proof;
- a retained helper must not read active UI state as proof;
- a retained helper must not settle operations, start streams, patch active run,
  emit SessionRun UI messages, replace transcript, finish runs, or perform
  rollback/restore;
- any helper that violates these rules must be deleted or moved into the scoped
  runtime model.

### Branch Summary Scope Metadata

Decision: branch summaries may create or update sibling scope metadata, but must
not create transcript content, switch the visible projection, or produce visible
terminal effects.

Branch summaries are SessionRun-level branch projection data. They can tell the
model that a branch exists and what its current summary status is. They are not
stream events, transcript authority, or selected-branch UI authority.

Required behavior:

- a branch summary with concrete `branchBindingId` may ensure or update a
  sibling `BranchRuntimeScope` metadata record;
- a branch summary without concrete `branchBindingId` must fail closed for scope
  creation;
- if a branch summary carries `agentRunId`, it may complete runtime identity
  metadata for that branch;
- if a branch summary lacks `agentRunId`, it must not be used as control
  operation source identity;
- branch summaries may update metadata such as status, `has_updates`, branch
  title, last-event time, and sibling ordering;
- branch summaries must not create transcript entries, apply live deltas, finish
  the selected run, append selected-branch notices, or switch selected branch;
- tests must prove that branch summary updates can reveal a sibling branch while
  leaving selected transcript and selected terminal state unchanged.

### Missing AgentRun Identity

Decision: missing `agentRunId` is a proof gap, not a reason to relax scoped
ownership.

The 2026-06-20 model treats missing sibling `agentRunId` as a propagation bug.
Execution must fix propagation or fail closed for operations that require runtime
identity. It must not use missing `agentRunId` to accept stale responses by
session/branch id alone.

Required behavior:

- when a scoped operation source requires runtime identity, missing `agentRunId`
  must fail closed or be repaired at the propagation source;
- when current scope and message both carry `agentRunId`, they must match;
- when a branch summary lacks `agentRunId`, it may create metadata only and must
  not authorize control operations;
- missing `agentRunId` must not cause fallback to selected branch, active run id,
  or `"main"`;
- tests must cover stale response rejection when branch identity matches but
  runtime identity is missing or mismatched.

### Branch-Scoped Interaction Replies

Decision: approval and user-input replies update the target branch scope, and
selected-branch notices are allowed only when that target scope is selected.

Approval and user-input replies are branch-scoped interaction results. They must
not be dropped merely because the user switched branches before the reply
arrived. They also must not interrupt or mutate another currently selected
branch.

Required behavior:

- reply messages must carry concrete `sessionRunId`, `branchBindingId`, and the
  approval or input id;
- a reply for a known sibling scope may update that sibling scope's pending
  approval/input state and related branch metadata;
- a reply for a non-selected sibling scope must not append selected transcript
  notices;
- a reply error must not mark another selected branch as failed;
- selected notices or visible interaction updates are allowed only when the
  target scope is selected;
- tests must cover reply success and reply error after switching away from the
  source branch.

### Operation Message Targeting With Missing Host Scope Proof

Decision: Host-provided scope proof is preferred. If Host scope proof is missing,
Webview may target only an already-existing scoped operation with exact
`operationId + operationKind`; otherwise the message must fail closed.

This rule exists for early operation failures such as preflight errors where the
Host may know the operation id/kind but may not have produced full run/branch
proof. It must not become a general selected-branch fallback.

Required behavior:

- explicit Host `sessionRunId + branchBindingId` proof takes precedence when
  present;
- when Host scope proof is missing, the Webview may look up an existing operation
  by exact `operationId + operationKind` inside the scoped runtime model;
- that lookup may settle only that existing operation's scoped
  pending/result/error effect;
- this lookup must not create a new scope;
- this lookup must not switch visible projection;
- this lookup must not infer scope from `selectedBranchBindingId`,
  `activeSessionRunId`, or the current transcript;
- if no matching scoped operation exists, the Host message must fail closed;
- tests must cover preflight operation error settlement without Host scope proof
  and a mismatched/missing operation id being rejected.

### Branch Create Optimistic Rollback

Decision: branch-create failure may roll back only the failed scoped operation.
It may change the visible projection only when the failed optimistic target scope
is still selected.

This follows directly from the scoped runtime model. Rollback is not allowed to
become another async drift path.

Required behavior:

- branch-create optimistic state belongs to the target scope and operation;
- operation failure settles that scoped operation;
- if the optimistic target scope is still selected, the reducer may restore the
  previous visible projection through a scoped rollback effect;
- if the user has already selected another scope, the failure must not pull the
  visible projection back to the source branch;
- cleanup or failure marking for the provisional target scope must happen only
  inside the scoped model;
- tests must cover branch-create failure after the user switches away from the
  optimistic target scope.

### Non-Bootstrap Resume

Decision: non-bootstrap resume is not a scope creation path. It may restore only
an already known current scope or an operation-targeted scope.

This follows from the rule that start is the only missing initial scope creator.
Bootstrap restore has its own strong-proof startup rule. Ordinary resume/recover
results cannot invent ownership from visible UI state.

Required behavior:

- non-bootstrap resume with operation proof must target the operation's scoped
  owner;
- non-bootstrap resume without operation proof must target an already known
  current scope by concrete `sessionRunId + branchBindingId`;
- non-bootstrap resume must not create a new scope from selected UI state;
- non-bootstrap resume must not use `activeSessionRunId` or
  `selectedBranchBindingId` as fallback proof;
- tests must cover stale non-bootstrap resume after branch switch and missing
  scope proof rejection.

### Operation Error Versus Runtime Error

Decision: `sessionRun.operation.error` is operation-scoped. Runtime terminal
failure belongs to scoped runtime lifecycle messages such as `sessionRun.error`.

This prevents a failed command, branch operation, or preflight check from being
treated as "the selected run crashed" unless the scoped reducer explicitly
receives a runtime terminal error for the selected scope.

Required behavior:

- `sessionRun.operation.error` may settle pending/result/error state for the
  targeted operation;
- `sessionRun.operation.error` may produce scoped rollback/restore or operation
  notices;
- `sessionRun.operation.error` must not mark a selected run terminal merely
  because it arrived while that run is selected;
- `sessionRun.error` may mark runtime error only after resolving to a concrete
  branch runtime scope, and visible terminal effect is allowed only when that
  scope is selected;
- tests must cover operation error not setting selected runtime status to error.

### Projection Error Scope

Decision: `sessionRun.projection.error` is a scoped projection notice, not a
runtime terminal event.

Projection recovery can fail without meaning that the underlying AgentRun or
SessionRun branch has failed. The UI must not convert projection recovery failure
into selected-run terminal cleanup.

Required behavior:

- projection error must carry concrete `sessionRunId + branchBindingId` proof;
- projection error may append a selected notice only when the target scope is
  selected;
- projection error for a sibling scope may update sibling metadata, but must not
  append a selected transcript notice;
- projection error must not finish, cancel, or error the selected branch;
- tests must cover projection error for a sibling scope and projection error for
  the selected scope.

## Forbidden Repair Shapes

Do not accept any repair that does one of these:

- adds another route-local, handler-local, or component-local ownership `if`;
- falls back from precise proof to `selectedBranchBindingId`;
- falls back from precise proof to `activeSessionRunId`;
- fabricates `"main"` as proof after a scope should already exist;
- lets status/read responses drive UI through selected-branch fallback;
- lets catch/fallback behavior bypass the success-path ownership rule;
- directly patches active run, emits visible SessionRun messages, starts streams,
  replaces transcript, finishes run, or restores optimistic UI without scoped
  runtime authority;
- treats a source-string assertion as the only correctness proof;
- fixes only the single reviewed line without scanning the same boundary family.

## Boundary Matrix

Every row in this matrix must be closed before execution can be called complete.
For each row, implementation must prove the same rule on success, failure,
catch/fallback, stale response, active-run switch, branch switch, and ABA
switch-back where the scenario applies.

| Entry | Required owner | Scope proof | Missing-proof rule | UI authority | Required closure evidence |
| --- | --- | --- | --- | --- | --- |
| start | scoped runtime start operation | operation id plus start-created initial main scope | only start may create missing scope | establishes first visible projection through scoped result | stale start, failed start, active-run revision/version conflict |
| bootstrap resume | Host restore boundary plus scoped runtime store | `bootstrapRestore`, session run id, branch binding, runtime scope proof | reject if active run/scope already conflicts or proof missing | restore visible projection only through scoped restore effect | stale bootstrap payload, missing branch proof, status refresh failure |
| non-bootstrap resume | scoped runtime reducer/effect | operation proof or already known concrete scope proof | fail closed; never create scope from visible state | restore running projection only for selected scope | stale resume after branch switch and missing proof |
| status refresh success | scoped runtime store | session run id plus branch binding | fail closed | update only target scope; visible effect only if selected | status for sibling branch, stale active run |
| status refresh failure | scoped runtime store | same proof as status success, checked before fallback | fail closed unless current scope still matches | degraded restore and stream restart only for exact matching scope | non-404 failure after active run switch |
| events | branch runtime scope | session run id plus branch binding | fail closed | append events only through selected-scope visible event effect | sibling events do not mutate selected transcript |
| stream | branch runtime scope | session run id plus branch binding | fail closed | live deltas only through selected-scope visible event effect | delayed old stream batch after branch switch |
| reconnecting | branch runtime scope | session run id plus branch binding | fail closed | mark reconnecting only for matching selected scope | stale reconnect cannot mark selected UI |
| reconnected | branch runtime scope | session run id plus branch binding | fail closed | mark running only for matching selected scope | stale reconnect recovery ignored |
| done | branch runtime scope | session run id plus branch binding | fail closed | terminal effect only for selected matching scope | sibling done does not finish selected transcript |
| cancelled | branch runtime scope or scoped cancel operation | operation proof or concrete current scope proof | fail closed | terminal/cancel effect only for matching selected scope | stale cancel result and stream cancel both covered |
| error | branch runtime scope or scoped operation error | operation proof or concrete current scope proof | fail closed | operation error is operation-only; runtime error terminalizes only matching selected scope | stale error cannot finish current UI |
| projection error | branch runtime scope | session run id plus branch binding | fail closed | notice only for selected matching scope | projection error never terminal-cleans selected run |
| continue | scoped operation or branch-local queue | source scope plus operation/queue proof | fail closed | target scope queue/runtime only | branch-local pending next turn after visible branch switch |
| pending next turn | branch runtime scope | session run id plus branch binding | fail closed | queue visible only if selected | sibling queue snapshot does not replace selected queue |
| recover | scoped operation | source scope plus operation proof | fail closed | operation result/effect only | stale recover after branch switch |
| cancel | scoped operation | source scope plus operation proof | fail closed | operation result/effect only | stale cancel failure does not clear current run |
| steer | scoped operation | source scope plus operation proof | fail closed | operation result/effect only | stale steer failure does not clear current run |
| branch.create | scoped operation with provisional target scope | source scope plus target branch proof | fail closed | optimistic projection and rollback are scoped effects | rollback only if failed operation scope is still visible |
| branch.select | scoped operation | source scope plus target branch proof | fail closed | visible projection switches only after scoped acceptance | ABA switch-back rejects stale selection |
| operation.pending | scoped operation reducer | Host source/target proof, or exact existing scoped operation by operation id/kind | fail closed without proof or existing scoped operation | registers operation in scope only | no global pending operation authority |
| operation.result | scoped operation reducer | Host source/target proof, or exact existing scoped operation by operation id/kind | fail closed without proof or existing scoped operation | reducer/effect only | stale result cannot patch active run |
| operation.error | scoped operation reducer | Host source/target proof, or exact existing scoped operation by operation id/kind | fail closed without proof or existing scoped operation | scoped notice/restore/rollback only | no handler-local branch special case |
| approval reply | branch interaction scope | session run id, branch binding, approval id | fail closed | update matching scoped approval; visible notice only if selected | sibling approval reply updates its scope without current notice |
| user input reply | branch interaction scope | session run id, branch binding, input id | fail closed | update matching scoped input; visible notice only if selected | sibling input reply updates its scope without current notice |
| branch summaries | SessionRun-level projection with scoped branch records | session run id plus branch records with binding proof | no transcript or visible terminal mutation | metadata summary only; may ensure/update metadata scope | stale summary cannot change selected transcript |

## Old Authority Disposal Rules

The following names may exist only if they are proven stateless adapters, or as
code being removed in the same execution. They must not own mutable state,
accept/reject async messages, settle operations, or emit UI effects:

- `SessionRunOperationCoordinator`
- `SessionRunSourceIdentityResolver`
- `sessionRunMessageGate`
- `activeSessionRunMatches`
- ChatView handler-local SessionRun guards
- any pending-operation view helper that stores lifecycle authority outside a
  `BranchRuntimeScope`

An adapter is acceptable only when all of these are true:

- no private mutable operation state;
- no selected UI state read for ownership;
- no active UI state read for ownership;
- no operation settlement;
- no stream start;
- no transcript replacement;
- no terminal cleanup;
- no rollback/restore side effect.

If a helper cannot satisfy this adapter contract, it must be deleted or moved
into the scoped runtime model. Keeping a parallel ownership model is not allowed.

## Review Protocol

Every implementation pass must be reviewed with `code-review-expert`. The
review must answer these questions explicitly:

- Did this pass remove or reduce old patch-model authority?
- Did this pass add any new ownership guard outside scoped runtime model?
- Did this pass close the same-class family, or only the reported symptom?
- Are success, failure, catch/fallback, stale response, branch switch, active-run
  switch, and ABA switch-back covered where applicable?
- Are source-string scans only negative drift guards rather than the sole proof?
- Does any remaining adapter violate the adapter contract above?

If a review finds a same-class issue, execution must return to the boundary
matrix and inspect all related rows. It must not fix only the reviewed line.

## Required Verification

Future execution cannot claim completion without current evidence for all of
these:

```powershell
Push-Location .\Labrastro-vscode-extension
npx vitest run src/sessionRuntime/SessionRuntimeReducer.test.ts src/sessionRuntime/SessionRuntimeStore.test.ts src/coordinators/SessionRunCoordinator.test.ts src/LabrastroController.session-run-correlation.test.ts src/LabrastroController.chat-stream.test.ts src/protocol/messages.test.ts webview-ui/src/chat/sessionRuntimeReducer.test.ts webview-ui/src/components/ChatView.context-events.test.ts webview-ui/src/chat/chatMessages.test.ts webview-ui/src/context/server.test.ts webview-ui/src/settings/useSettingsController.test.tsx
npm run typecheck
git diff --check
node .gitnexus/run.cjs status
node .gitnexus/run.cjs detect-changes --repo Labrastro-vscode-extension --scope all
Pop-Location

Push-Location .\Labrastro
.\.venv\Scripts\python.exe -m pytest tests/labrastro_server/services/agent_runtime/test_session_branch_tree.py tests/labrastro_server/services/agent_runtime/test_session_branch_runtime.py tests/labrastro_server/http/test_remote_service.py tests/labrastro_server/http/test_protocol.py tests/labrastro_server/services/agent_runtime/test_contract_scan.py -q
git diff --check
Pop-Location
```

The commands above are the baseline. Future execution must add touched-file
tests when the repair changes files not covered by this baseline. The added
coverage must include:

- Host correlation/runtime tests for stale resume, status refresh failure,
  stream/reconnect after branch switch, branch create/select, continue/recover,
  cancel/steer, operation errors, and projection errors.
- Webview reducer/context tests for scoped event visibility, branch-create
  optimistic rollback, sibling terminal events, projection error notices, queues,
  approvals, and inputs.
- Backend tests if control-route, status/events, binding, projection, or runtime
  scope code is touched.

## Completion Gate

Completion requires every item below to be true at the same time:

- the boundary matrix has no unexamined row;
- the known stale status-refresh fallback issue is fixed through scoped runtime
  ownership, not local fallback patching;
- no SessionRun UI-mutating path uses `selectedBranchBindingId` or
  `activeSessionRunId` as async ownership proof;
- no global pending operation slot is lifecycle authority;
- every old helper is deleted or proven to be a stateless adapter;
- catch/fallback paths obey the same ownership rule as success paths;
- sibling scope terminal/stream/queue/approval/input updates cannot mutate the
  selected transcript;
- operation rollback/restore is scoped reducer/effect behavior;
- all required verification commands pass;
- `code-review-expert` reports no P0/P1/P2 correctness, architecture, or
  boundary-closure finding;
- the final response cites concrete code paths, tests, scans, and GitNexus
  impact evidence.

## Pre-Goal Review Checklist

Before this document is used to set an active goal, review it for:

- ambiguous words that allow incomplete completion;
- hidden staged-completion language;
- any instruction to generate or recreate this file during execution;
- any fallback that permits selected UI state as proof;
- any boundary row that lacks failure/fallback closure;
- any review rule that could allow one-line symptom fixing;
- any unresolved conflict with the 2026-06-20 model.

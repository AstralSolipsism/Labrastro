# SessionRun Operation Model Execution Record

This is a working execution record for the active goal. It does not replace the
2026-06-17 convergence plan or the 2026-06-18 async-correlation repair plan.
If those authority documents conflict in a way not already decided by the
active goal, execution must stop for user decision.

## Decided Model

### start

- Source: active run identity snapshot at begin time, captured as
  `activeRunRevision` plus optional previous `activeSessionRunId`.
- Target: a new active SessionRun returned by the backend.
- Begin condition: visible start request creates one current operation before
  awaiting remote start.
- Success result: canonical `sessionRunId`, `sessionId`, `branchBindingId`,
  optional `agentRunId`, optional `activationId`.
- Acceptance: operation id and kind match; active run revision still matches;
  if an old active run existed at begin time, that same old run is still active.
  The returned new `sessionRunId` is not required to equal the old active run.
- Host side effect: replace active run and start event stream for the returned
  branch.
- Webview side effect: accepted `sessionRun.session` establishes active run.
- Failure side effect: operation-level error only. It must not clear or mark an
  existing visible run terminal.

### branch.create

- Source: current `sessionRunId`, source branch binding id, source `agentRunId`,
  and `activeRunRevision`.
- Target: requested target branch binding id.
- Begin condition: source active run has an `agentRunId`; branch create derives
  a new AgentRun mainline from that source AgentRun.
- Success result: canonical response branch binding id and target agent run id.
- Acceptance: operation id/kind match; revision matches; current active run
  still matches source `sessionRunId`, source branch, and source `agentRunId`;
  response branch equals target.
- Host side effect: select target branch, patch target agent run/activation, and
  start target branch stream.
- Webview side effect: accepted `sessionRun.branch.started` selects target
  branch and marks branch operation running.
- Failure side effect: operation-level error only. It must not mark selected run
  terminal.

### branch.select

- Source: current `sessionRunId`, source branch binding id, and
  `activeRunRevision`.
- Target: requested branch binding id.
- Begin condition: active run and target branch exist in the request context.
  Source `agentRunId` is not required because `ActiveSessionRun.agentRunId` is
  optional and branch select switches an existing SessionRun branch binding.
- Success result: canonical selected branch binding id, optional target
  `agentRunId`, optional `activationId`, selected branch projection/status.
- Acceptance: operation id/kind match; revision matches; current active run
  still matches source `sessionRunId` and source branch; response branch equals
  target.
- Host side effect: switch selected branch, apply target projection, fetch target
  events, and start target stream.
- Webview side effect: accepted `sessionRun.branch.selected` switches selected
  branch and replaces selected projection.
- Failure side effect: operation-level error only. It must not switch branch or
  mark selected run terminal.

### operation.error

- Scope: operation-level failure, not selected-run terminal state.
- Acceptance: exact pending/current operation id and kind.
- Host side effect: emit `sessionRun.operation.error` only for the matching
  accepted failure.
- Webview side effect: clear matching pending operation and append an operation
  notice. It may clear operation-owned local draft/working state only when no
  existing visible run is being preserved.
- Forbidden side effect: no `finishSessionRun("error")`, no selected run
  terminal status, no clearing existing `activeSessionRunId`.

### Run-level visible messages

- Scope: selected active SessionRun and selected branch.
- Messages: `sessionRun.events`, `sessionRun.stream`, `sessionRun.done`,
  `sessionRun.cancelled`, `sessionRun.error`, approval reply failures, user
  input reply failures, pending-next-turn messages, reconnect messages.
- Acceptance: if message carries `sessionRunId`, current `activeSessionRunId`
  must exist and equal it. If it carries `branchBindingId`, it must equal the
  selected branch. Legacy branch fallback is only for messages without
  `sessionRunId` and without operation semantics.
- Side effect: only accepted run-level messages may change selected transcript or
  selected run terminal state.

### Backend SessionRun control

- Projection present: resolve peer token, in-memory projection, requested branch
  binding policy, peer ownership, and store errors through one resolver.
- Projection missing: apply the same requested branch policy against persisted
  binding data. A request for a concrete branch must inspect that branch, not any
  sibling/main binding.
- Outcomes:
  - requested branch persisted and same peer: `session_run_projection_unavailable`
  - requested branch missing: `session_run_branch_binding_not_found`
  - requested branch belongs to another peer: `session_run_binding_peer_mismatch`
  - binding store throws: `session_run_binding_store_unavailable`

## Red Test Matrix

- Webview operation error does not mark existing visible run as error.
- Webview operation error for branch.create does not mark selected run terminal.
- Host start failure does not clear or mark an existing active run.
- Host branch.create failure does not mark selected run terminal.
- branch.select success is accepted when the active run has no source
  `agentRunId` but source session, source branch, revision, and target match.
- branch.create still rejects when source `agentRunId` is missing.
- branch.select rejects stale revision, changed source branch, and mismatched
  response target.
- Backend projection-missing resolver returns precise errors for target persisted,
  target missing, target other peer, and store failure.

## Completion Evidence Required

- Implementation references for every model row above.
- Tests proving every red-test row.
- Scans proving no internal `operation_id` / `operation_kind` aliases, no
  operation error terminal cleanup, no create/select source guard mix-up, no
  backend branch-policy bypass, and no Controller path bypassing normalization
  and operation coordinator.

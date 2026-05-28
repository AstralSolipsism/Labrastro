# ADR 0004: AgentRun Execution Architecture Boundary

## Status

Proposed.

## Context

ReuleauxCoder uses a server-side AgentRun control plane and distributed execution
workers. The current implementation already has runtime profiles, execution
locations, local peer workers, server sandbox workers, per-run worktrees, and
executor-specific adapters for ReuleauxCoder, Codex, Claude, and Gemini.

The architecture is correct in direction but several defaults are ambiguous:

- user Agents can fall back to local workspace execution when no runtime profile
  is set;
- Taskflow dispatch does not enforce server-capable profiles;
- capability package generation is still biased toward a local runtime profile;
- local peers can over-declare execution features;
- executor availability is broader than actual installed CLI capability;
- model request origin is not explicit enough for configuration, UI, and audit.

The project is still in development. This ADR intentionally favors clear
architecture over backward compatibility.

## Product Rules

- The server is the only control plane for Agent config, Taskflow dispatch,
  AgentRun lifecycle, capability package generation, permission policy, audit,
  and runtime state.
- Execution workers may run on the server, in a server-managed sandbox, or on a
  local peer. Worker identity and claim scope must be explicit.
- Default execution is server-first:
  - user-defined Agents default to server execution;
  - Taskflow Agents default to server execution;
  - capability package generation defaults to server execution;
  - environment configuration runs on the local peer because it manages local
    capability dependencies.
- Local Codex, Claude, and Gemini CLIs are explicit advanced executors. They are
  valid integration points, but they are not the default Agent execution path.
- Model request origin must be explicit:
  - server executors make model requests from the server or server-managed
    worker environment;
  - local CLI executors make model requests through the user's local CLI,
    account, credentials, and configuration.
- Worktrees and sandboxes belong to an AgentRun/task, not to an Agent.
- Runtime slots must be separated by resource type instead of represented by one
  global concurrency number.

## Decisions

### Execution Model

Keep these concepts separate:

```text
executor            = reuleauxcoder | codex | claude | gemini | fake
execution_location  = local_workspace | daemon_worktree | remote_server
worker_kind         = local_peer | server_worker | sandbox_worker
model_request_origin = server | server_worker_cli | local_cli
```

`execution_location` describes workspace/runtime semantics.
`worker_kind` describes which worker identity may claim the run.
`model_request_origin` describes where provider traffic originates.

### Runtime Profile Defaults

- `environment_local` remains local:
  - `execution_location=local_workspace`
  - `worker_kind=local_peer`
  - `model_request_origin` is not applicable unless it invokes an LLM executor.
- capability package generation must use a server profile by default, such as
  `capability_packager_remote`.
- user Agent defaults must resolve to a server-capable profile.
- Taskflow-eligible Agents must resolve to a server-capable profile unless a
  future explicit local Taskflow opt-in is added.
- Missing `runtime_profile` must not silently fall back to `local_workspace`.

### AgentRun Submission

AgentRun submission must fail fast when the requested Agent/runtime combination
violates product rules:

- user Agent without a resolvable runtime profile is invalid;
- Taskflow Agent with a local-only profile is invalid unless explicitly allowed;
- capability package generation must use a server-capable profile;
- environment configuration must use the local environment profile;
- system/internal Agents may only run through their declared system flows.

Recommended error shape:

```json
{
  "error": "invalid_agent_runtime_profile",
  "message": "Taskflow agent requires a server-capable runtime profile"
}
```

### Worker Claim Routing

Worker claim matching must use explicit worker identity and actual executor
capability.

- Local VSIX peers may claim only `worker_kind=local_peer` and
  `execution_location=local_workspace` runs whose workspace root matches.
- Server workers may claim server-owned `remote_server` runs.
- Sandbox workers may claim sandbox-managed runs.
- A local peer must not claim a `remote_server` run by declaring a loose feature
  such as `agent_runs.remote_server`.
- Go workers must report only actually available executors. Codex, Claude, and
  Gemini should be claimable only when the corresponding CLI is installed and
  usable.

### Model Request Origin

Resolved AgentRun requests and runtime events must expose model request origin.

- Server ReuleauxCoder execution uses `model_request_origin=server`.
- Local Codex/Claude/Gemini execution uses `model_request_origin=local_cli`.
- Server-managed Codex/Claude/Gemini execution uses `server_worker_cli` or an
  equivalent server-owned origin.

Configuration UI and audit surfaces must not imply that local CLI execution is a
server-originated model request.

### Worktree Lifecycle

- AgentRun worktrees are created per run/task.
- Agents do not own fixed worktrees.
- Taskflow runs, capability package generation, and server-side AgentRuns may use
  per-run worktrees or sandboxes.
- Local environment configuration uses the user's current workspace and does not
  create daemon worktrees.

### Runtime Slots

Separate scheduling/resource counters are required for:

```text
server_agent_run_slots
server_sandbox_slots
local_peer_agent_run_slots
model_request_slots
```

This ADR does not require a complete slot-management UI. It requires the backend
model and scheduler semantics to stop treating all runtime pressure as one
undifferentiated concurrency pool.

## Implementation Path

1. Add domain fields for `worker_kind` and `model_request_origin` to runtime
   profile resolution and AgentRun request snapshots.
2. Change built-in runtime profiles so capability package generation and user
   Agent defaults are server-capable, while environment configuration stays
   local.
3. Remove silent `local_workspace` fallback for missing profile resolution.
4. Add submit-time validation for user Agents, Taskflow Agents, system flows,
   capability package generation, and environment configuration.
5. Update worker registration and claim matching to require explicit
   `worker_kind` and matching execution location.
6. Update the Go worker to advertise only installed and usable CLI executors.
7. Add model request origin to AgentRun event/audit data.
8. Keep per-run worktree behavior and document it in code comments or runtime
   docs where claim/prepare logic lives.
9. Update frontend Agent settings copy and controls after backend semantics are
   fixed:
   - default user Agent execution is server-side;
   - local CLI execution is an advanced explicit option;
   - local CLI copy states that provider requests are made by the local CLI;
   - system/internal Agents are not edited as normal user Agents.

## Test Plan

- Backend tests:
  - user Agent without a runtime profile no longer falls back to local
    workspace;
  - user Agent default runtime resolves to a server-capable profile;
  - Taskflow Agent with a local-only profile is rejected unless explicitly
    allowed;
  - capability package generation resolves to a server-capable runtime profile;
  - environment configurator resolves to the local environment profile;
  - system/internal Agent cannot run outside its declared `system_flow_only`;
  - local peer worker cannot claim a `remote_server` AgentRun;
  - server worker can claim a server-owned `remote_server` AgentRun;
  - sandbox worker can claim sandbox-managed AgentRuns;
  - local peer worker can claim only matching `local_workspace` runs;
  - workspace-root mismatch prevents local peer claim;
  - Codex executor is not advertised or claimable when the Codex CLI is missing;
  - Claude executor is not advertised or claimable when the Claude CLI is
    missing;
  - Gemini executor is not advertised or claimable when the Gemini CLI is
    missing;
  - local CLI executor resolution includes `model_request_origin=local_cli`;
  - server executor resolution includes `model_request_origin=server` or the
    chosen server-owned equivalent;
  - per-run worktree creation still happens for server/daemon worktree runs;
  - local environment configuration does not create daemon worktrees.
- Frontend tests:
  - user Agent creation defaults to server execution;
  - local Codex/Claude/Gemini execution is shown only as an explicit advanced
    option;
  - local CLI execution copy explains that model/provider requests are made by
    the local CLI;
  - system/internal Agents are displayed separately and are not editable as
    normal user Agents;
  - Taskflow configuration rejects or clearly blocks ambiguous local-only
    profiles;
  - capability package generation does not default to local execution.

## Acceptance Criteria

- No code path silently resolves a missing user Agent runtime profile to
  `local_workspace`.
- User-defined Agents default to a server-capable runtime profile.
- Taskflow dispatch is guarded by backend validation, not only frontend UI.
- Taskflow Agents cannot accidentally run on a local peer.
- Capability package generation defaults to server execution.
- Environment configuration still defaults to local peer execution.
- `worker_kind` is represented in runtime profile resolution or equivalent
  claim metadata.
- Worker claim matching uses explicit worker identity and execution location.
- Ordinary VSIX local peers cannot claim `remote_server` AgentRuns.
- Server workers can claim server-owned AgentRuns.
- Sandbox workers can claim sandbox-managed AgentRuns.
- Go AgentRun workers advertise only actually available CLI executors.
- Installed-state metadata and claimable-executor metadata do not contradict
  each other.
- Resolved AgentRun data exposes `model_request_origin`.
- Local CLI executor runs are auditable as local-origin model requests.
- Server executor runs are auditable as server-origin model requests.
- Worktree lifecycle remains per AgentRun/task, not per Agent.
- Runtime slot terminology distinguishes server AgentRun slots, server sandbox
  slots, local peer AgentRun slots, and model request slots.
- Frontend copy does not imply that local CLI execution is server-hosted model
  traffic.
- System/internal Agents are not presented as normal editable user Agents.
- Documentation states that AgentRun is a server-controlled distributed
  execution mechanism, and local CLIs are explicit advanced execution nodes.

# Capability Package Ecosystem Architecture Matrix

> **For agentic workers:** This document is the control document for capability package ecosystem redesign. Do not implement isolated fixes in the old capability package path unless the task is explicitly mapped to this matrix. Implementation may be staged, but every change must move toward this architecture and update the matrix evidence before being treated as complete.

**Status:** Frozen architecture baseline with reopened drift-convergence closure recorded. The pre-freeze checklist was accepted on 2026-06-11, but code review and follow-up GitNexus review found that several implementation paths still used local state logic instead of the unified architecture. The 13-task evidence log and the earlier C-09 through C-18 closure rows are therefore baseline evidence only; current completion evidence is the full convergence set C-01 through C-22 and gates G-13 through G-34 below.

**Goal:** Rebuild capability package installation around a unified, auditable ecosystem architecture where a GitHub link can become an installed, verified, and activatable capability without relying on free-form LLM config generation.

**Architecture:** Labrastro is the central control plane. It stores upstream snapshots, normalized manifests, dependency graphs, install plans, activation state, credentials metadata, audit events, and update candidates. Server and local peer executors use the same typed `InstallPlan` protocol but execute only the actions assigned to their target.

**Tech Stack:** Python backend, AgentRun/SessionRun, Postgres persistence, config-backed capability resources, VS Code local peer, MCP runtime, Skill discovery, pytest, frontend Vitest.

---

## 1. Purpose

The user experience contract is simple: when a user gives Labrastro a GitHub repository link for a capability, they expect Labrastro to install the capability, not merely draft a configuration.

The current path is not acceptable as the target architecture:

```text
LLM free-form draft patch
-> backend assembly
-> strict late validation
-> approval
-> config and SKILL.md materialization
```

That path fails structurally because the LLM can invent schema values such as `envreq:python-pkg:*`, the backend rejects the whole draft, and no install decision is ever shown to the user. Strict validation is necessary, but generation must be constrained before validation, not repaired only after failure.

This document defines the replacement architecture and the execution matrix needed to implement it without drifting into local patches.

## 2. Non-Goals

- Do not preserve compatibility with invalid capability package draft shapes produced by the old packager.
- Do not let LLM output directly become config, shell commands, secret values, hook trust, or installed state.
- Do not implement a second private versioning system separate from upstream repository versions.
- Do not treat `enabled=true`, config presence, or a written file as proof that a capability is usable.
- Do not make local peer behavior a special one-off path. Server and peer execution must share the same plan and result schema.
- Do not expose full source snapshots to runtime agents by default.
- Do not make users review every low-level item. User decisions must be package-level by default, with component-level overrides only when needed.

## 3. Product Contract

| User intent | System behavior |
| --- | --- |
| User provides a GitHub link | Labrastro fetches an upstream snapshot, extracts capability candidates, normalizes a manifest, and prepares an installable package. |
| User installs a package | Labrastro registers the package, stores the snapshot, materializes canonical artifacts, creates target install plans, and runs safe checks/install actions. Installation does not imply activation. |
| User activates a package | Labrastro adds valid package components to the active runtime capability surface. Valid hooks follow parent activation. MCP processes enter desired active state and report actual runtime health separately. |
| Environment dependency is missing | Labrastro attempts safe automatic installation when a typed action exists. If user action is inherently required, the UI presents the action and re-checks after completion. |
| A dependency cannot be safely classified | Only dependent components are blocked or degraded. The package is not discarded. |
| Update is available | Labrastro fetches a candidate snapshot, computes deterministic manifest diff, explains impact, and requires approval before activation. |

## 4. Current Failure Baseline

The 2026-06-10 Waza install session established the baseline failure:

```text
agent_run_id: task-60f93265c7cf4bdaa05eba1a19dc20b0
source: https://github.com/tw93/Waza
agent_run_status: completed
source_errors: 0
source_documents: 15
source_files: 43
source_skill_files: 8
failure: draft_invalid
validation_message: invalid environment requirement kind in id: python-pkg
```

The root cause was not GitHub fetch failure and not an install command failure. The model produced environment requirements such as:

```json
{
  "id": "envreq:python-pkg:readability-lxml",
  "kind": "python_package",
  "install": "pip install --user readability-lxml html2text"
}
```

The backend authoritative enum accepts only:

```text
executable
runtime
sdk
service
env_var
credential
path
project_file
container
```

The old system treated this as a whole-draft failure. The new system must convert unknown values into unmapped findings and block only dependent components.

## 5. Core Principles

| ID | Principle | Required behavior |
| --- | --- | --- |
| P-01 | Backend schema is authoritative | LLM cannot create enum values, component types, trust states, action types, or status values. |
| P-02 | LLM output is advisory | LLM proposes component candidates, dependency edges, runtime target reasoning, and impact explanations. Backend normalizes and validates. |
| P-03 | Installation and activation are separate | Installed resources are not active until activation is requested and accepted. |
| P-04 | State is multi-axis | Package/component state must distinguish install, activation, runtime, check, credential, update, and mapping state. |
| P-05 | Server is the global fact source | Server stores package manifest, source snapshot, expected target state, dependency graph, and aggregated status. Peer-owned facts must be reported by the peer. |
| P-06 | Skill is an artifact, not a process | Skill content is centrally stored as a package artifact bundle. Its dependencies and delivery targets determine runtime availability. |
| P-07 | Source snapshot is complete, runtime exposure is closed | Store full upstream snapshot for traceability. Runtime agents see only generated `exposed_paths`. |
| P-08 | System tools are shared, package-manager dependencies are isolated | `git`, `gh`, `bash`, `python3`, `node`, `jq` may be shared checks. Python/npm package installs default to package-local isolated environments. |
| P-09 | Credentials are scoped and actor-aware | Default credential binding is per user. Workspace/global credentials require explicit admin configuration and authorization. |
| P-10 | Updates follow upstream versions | User-visible versions are upstream release/tag/manifest versions. Labrastro snapshot ids are internal traceability, not product versions. |
| P-11 | LLM proposes target placement; backend owns executable placement | LLM may reason that a component should run on `server`, `local_peer`, or `both`, but backend normalizers must accept, rewrite, or reject that proposal before any plan executes. |
| P-12 | State transitions have one owner | Package install, activation, target facts, updates, rollback, materialized resource enablement, and hook executability must flow through a shared state machine/projection boundary, not route-local or UI-local boolean checks. |
| P-13 | Runtime facts have stable compound identity | Server, local peer, package, plan, action, component, and content/version identity must be part of target fact reconciliation. A short local id such as `action_id` is never enough to prove install/check state. |
| P-14 | Package ownership gates package-managed resources | Package-managed components, materialized skills, MCP resources, environment requirements, and hooks must derive availability from their owning package chain. A child resource cannot become active merely because it has its own `enabled=true`. |

## 6. Domain Model

### 6.1 Package Source Snapshot

The package source snapshot is a complete read-only copy of the upstream source at an exact ref.

Required fields:

```text
package_id
source_type
source_url
source_ref
commit_sha
upstream_version
snapshot_id
snapshot_path
created_at
content_hash
provenance
```

Version display rules:

| Upstream evidence | User-visible version |
| --- | --- |
| GitHub release/tag | Release/tag version |
| `VERSION`, `package.json`, marketplace manifest | Declared version |
| No declared version | `branch@commit` |

Internal rollback and audit use `snapshot_id` and `commit_sha`, not a Labrastro-invented user-facing version.

### 6.2 Capability Manifest

The normalized manifest is backend-owned and deterministic.

Required sections:

```text
package
components
dependency_edges
environment_requirements
credential_requirements
install_plans
activation_rules
update_metadata
unmapped_findings
exposed_file_closures
```

The manifest may be derived from LLM suggestions, source evidence, and deterministic scanners, but only backend normalizers may produce final ids, enum values, action types, and status axes.

### 6.3 Component Types

Authoritative component types:

```text
skill
mcp_server
environment_requirement
credential_requirement
prompt_fragment
hook
```

Unknown component-like findings are stored under:

```text
unmapped_findings.unsupported_component_candidates
```

They cannot enter `components`.

### 6.4 Skill Artifact Bundle

Skill installation must not reduce a skill to a lone `SKILL.md`.

The package canonical store keeps the snapshot and registers skill entries:

```text
capability-packages/<package_id>/<upstream_version-or-commit>/source/
  skills/<skill_name>/SKILL.md
  skills/<skill_name>/references/**
  skills/<skill_name>/scripts/**
  rules/**
  docs/**
  assets/**
  README.md
```

Each skill component records:

```text
component_id
package_id
entry_path
skill_dir
package_root
source_hash
exposed_paths
delivery_targets
dependency_refs
```

Skill names must be namespaced:

```text
<package_id>:<skill_name>
```

or an equivalent collision-proof canonical id. Plain names such as `read` must not collide across packages.

### 6.5 Exposed File Closure

The full snapshot is stored, but runtime agents receive only a controlled closure.

Default included paths:

```text
entry SKILL.md
entry skill_dir/**
relative paths explicitly referenced by entry SKILL.md
package-level allowlisted shared dirs: rules/**, references/**, docs/**, assets/**
README.md when referenced or needed for package context
```

Default denied paths:

```text
.git/**
.github/**
node_modules/**
dist/**
build/**
coverage/**
cache/**
secret-like paths: *secret*, *.env, *token*, *key*
CI files unless explicitly referenced by the skill
```

LLM may propose additional paths, but backend exposure rules decide whether they enter `exposed_paths`. Rejected proposals become technical diagnostics, not runtime-visible files.

### 6.6 Dependency Graph

Dependencies are graph edges, not duplicated prose in components.

Edge shape:

```text
from_component_id
to_requirement_id
target
evidence_refs
confidence
status
```

Allowed edge statuses:

```text
verified_by_source
inferred
unmapped
invalid
```

LLM may propose dependency edges. Backend must verify target ids, evidence, enum legality, and command safety. Invalid edges become `unmapped_dependency` and cannot activate a resource.

## 7. State Model

The following state axes are authoritative. New states require updating this document, backend enums, frontend display, tests, and migration notes.

### 7.1 State Axes

```text
install_state:
  not_installed | registered | materialized | installed | blocked | failed

activation_state:
  inactive | active | degraded | blocked

runtime_state:
  not_applicable | stopped | starting | running | connected | failed

check_state:
  unknown | pending | passed | missing | failed | stale

credential_state:
  not_required | missing | bound | verified | failed

update_state:
  not_checked | current | update_available | candidate_ready | updating | rollback_available | failed

mapping_state:
  mapped | unmapped | mapping_required | invalid
```

### 7.2 State Meaning

| State axis | Meaning |
| --- | --- |
| `install_state` | Whether a package or component has been registered, materialized, and installed on required targets. |
| `activation_state` | Whether the user wants the package/component in the active capability surface. |
| `runtime_state` | Runtime process reality for MCP servers and other service-like resources. |
| `check_state` | Result of executable/environment verification actions. |
| `credential_state` | Whether required credentials are bound and verified. |
| `update_state` | Upstream update and rollback status. |
| `mapping_state` | Whether LLM/source findings are mapped to backend schema. |

### 7.3 Materialized vs Installed

`materialized` and `installed` must remain separate.

```text
materialized:
  Labrastro wrote normalized bundle/config/action plan into controlled storage.

installed:
  The target executor completed target-side installation actions.
```

For pure server-side skill artifacts, `materialized` may satisfy the install requirement, but the state axis must still preserve the distinction.

### 7.4 Skill State

Skill state is two-layered:

```text
artifact_state:
  state of the canonical server-side skill bundle

delivery_state_by_target:
  server_agent and local_peer_agent visibility/sync status
```

Example:

```text
skill:waza/read
  artifact.install_state: materialized
  delivery.server_agent.install_state: installed
  delivery.local_peer_agent.install_state: installed
  activation_state: active
```

### 7.5 MCP State

MCP activation intent is separate from process health:

```text
mcp:browser
  install_state: installed
  activation_state: active
  runtime_state: starting | connected | failed | stopped
```

`activation_state=active` means Labrastro should try to run/connect the MCP server. It does not prove the MCP process is connected.

### 7.6 Hook State

Hooks follow parent activation.

```text
hook.install_state: installed
hook.activation_state: active when parent package/component is active
hook.activation_reason: parent_component_active | parent_package_active
```

Hooks are not individually reviewed by default. If a hook is invalid or unmapped, it must not enter the activatable manifest. It blocks or degrades only the dependent component.

### 7.7 State Ownership and Propagation Invariants

The state axes are not just display labels. They define execution ownership.

There must be exactly one backend-owned state transition boundary for capability package lifecycle changes. A route, UI helper, executor, normalizer, or compatibility adapter may request a transition, but must not independently decide final package/component/resource state.

Required invariants:

| Invariant | Required behavior |
| --- | --- |
| Install does not activate | Accepting or applying an install writes package records and artifacts, but package-managed skills, MCP resources, environment requirements, components, and hooks remain unavailable until package activation allows them. |
| Activation propagates downward | Enabling a package updates or projects desired availability for package-owned components and materialized skill/MCP resources. |
| Shared components derive from active owners | A component referenced by multiple packages is desired-active only when at least one owning package is active and the component is not blocked by its own checks/credentials/mapping. |
| Hooks follow owner chain | A component hook must consider package ownership. It is not enough to inspect the component's local `enabled/status`. |
| Target facts do not overwrite desired state | Desired install actions and reported target results are separate records. Missing/stale peer facts must not be converted into verified state. |
| Update/rollback is a state machine | `check`, `prepare`, `apply`, and `rollback` must use explicit transition helpers. A non-empty diff object, stale rollback field, or route-local boolean cannot decide user-visible update state. |

Implementation consequence: if a code path writes `enabled`, `state`, `rollback`, `update_candidate`, target check/install status, or hook executable state directly, the task must either move that write behind the shared state/projection boundary or document why it is only a compatibility serializer.

## 8. LLM Boundary and Normalization

### 8.1 Allowed LLM Responsibilities

LLM may:

- Summarize source purpose.
- Propose components and their source evidence.
- Propose dependency edges.
- Propose target placement as `server`, `local_peer`, or `both`, with source evidence and reasoning.
- Propose exposed path candidates.
- Explain deterministic manifest diffs.
- Produce user-facing impact summaries.

Backend normalizers own final executable target placement. A placement proposal without source evidence becomes an open finding, not an executable plan.

### 8.2 Forbidden LLM Authority

LLM must not:

- Invent enum values.
- Produce final component ids.
- Produce final config.
- Produce final install actions.
- Produce shell commands that are executed directly.
- Store or transform secret values.
- Mark anything installed, verified, active, trusted, or connected.
- Decide final update diffs.

### 8.3 Manifest Candidate vs Open Findings

LLM output must be split:

```text
manifest_candidate:
  items that can map to backend schema

open_findings:
  unclassified_requirements
  unmapped_install_instructions
  ambiguous_runtime_targets
  unsupported_component_candidates
  schema_repair_notes
```

Unknown enum values are evidence, not manifest. Example:

```text
observed: "pip install --user readability-lxml html2text"
suggested_kind: "python_package"
mapping_state: mapping_required
```

Backend may map this to a supported isolated runtime:

```text
id: envreq:runtime:waza-read-python-env
kind: runtime
isolation: python_venv
packages:
  - readability-lxml
  - html2text
```

### 8.4 Prefix and Kind Invariants

Backend normalization must reject or remap mismatched ids before manifest persistence.

| Prefix / field | Required normalized kind | If mismatched |
| --- | --- | --- |
| `envreq:*` | `environment_requirement` | Move to `open_findings.unclassified_requirements` or reject the candidate. |
| `skill:*` | `skill` | Move to `open_findings.unsupported_component_candidates`. |
| `mcp:*` / `mcp_server:*` | `mcp_server` | Move to `open_findings.unsupported_component_candidates`. |
| `credreq:*` | `credential_requirement` | Move to credential requirements only after secret-free validation. |

The LLM may mention strings that look like ids. Only backend normalizers may decide whether those strings become manifest ids. This section exists to prevent the specific drift where `envreq:executable:*` could pass through as a non-environment component.

## 9. Environment and Isolation

### 9.1 Shared System Requirements

Shared system requirements are tools expected to exist as platform capabilities.

Initial shared allowlist:

```text
git
gh
bash
sh
python3
node
npm
pnpm
yarn
docker
jq
curl
wget
rg
```

Shared means "check a system capability and optionally install through a controlled action", not "silently mutate the global system".

### 9.2 Shared Capability Registry

The allowlist above is only the initial seed. Long-term shared capabilities must be represented by a backend-owned registry, not by LLM guesses or scattered string checks.

Registry item shape:

```text
id
display_name
executable_names
version_check_action
install_action_policy
platforms
credential_interaction
conflict_policy
evidence_required
```

Rules:

- Shared registry entries may be referenced by many packages through dependency graph edges.
- Registry entries do not mean the system can always install the tool automatically.
- `install_action_policy` must distinguish safe typed install, user/admin system action, GUI authorization, credential binding, and manual command review.
- If no registry entry or typed action exists, the finding becomes `mapping_required` internally and a concrete user/admin action externally.

### 9.3 Isolated Requirements

Package-manager dependencies default to isolation:

| Evidence pattern | Normalized action |
| --- | --- |
| `pip install ...` | package-local Python venv |
| `python3 -c "import X"` | check package-local or declared Python runtime |
| `npm install ...` | package-local Node env |
| `npm install -g ...` | do not execute globally; map to isolated Node runtime when possible |
| `npx -y <pkg>` | isolated/cacheable Node component runtime |
| `cargo install ...` | mapping-required until a typed cargo action exists |
| `go install ...` | mapping-required until a typed Go action exists |
| `curl ... | bash` | manual command review required |
| `apt/brew/winget/choco install ...` | system-level user/admin action with check verification |

### 9.4 Conflict Handling

Conflicts are resolved by isolation first.

| Conflict | Required behavior |
| --- | --- |
| Two packages require `gh` | One shared `envreq:executable:gh` with multiple `required_by` edges. |
| Two packages require different Python packages | Separate package-local venvs. |
| Two packages require incompatible Node versions | Separate package-local Node envs if supported; otherwise block dependent components. |
| Two MCP servers claim same global name | Namespaced component ids and explicit display names. No global overwrite. |
| Two skills share plain name | Namespaced skill ids; UI may show display labels with source package. |

Unknown conflicts do not destroy the package. They set affected components to `activation_state=blocked` or package `activation_state=degraded`.

## 10. Install Plan Protocol

Server and local peer use the same install protocol.

### 10.1 InstallPlan Shape

```text
install_plan_id
package_id
snapshot_id
target: server | local_peer
actions[]
expected_results[]
created_at
created_by
```

### 10.2 Action Catalog

Initial action catalog:

```text
write_skill_bundle
write_skill_delivery_projection
write_mcp_config
create_python_venv
install_python_packages
create_node_env
install_node_packages
check_executable
check_python_imports
check_node_package
bind_credential_requirement
start_mcp_server
check_mcp_server
```

LLM commands are evidence. Executors run only typed actions generated by backend normalizers.

### 10.3 Manual Steps

Manual steps are not "waiting for developer support." They are user/admin actions that inherently require human participation or elevated authority.

Allowed manual categories:

```text
credential_auth_required
credential_secret_required
gui_authorization_required
system_package_install_required
manual_command_review_required
license_acceptance_required
path_selection_required
```

Manual completion never marks success. It triggers a re-check action. Verified state comes only from checks.

## 11. Credentials and Multi-Tenant Rules

### 11.1 Credential Requirements

Capability packages declare credential requirements, not credential values.

```text
credential_requirement:
  id
  provider
  kind: api_key | oauth | token | ssh_key | app_installation
  placement: server | local_peer | both
  allowed_scopes
  required_by
```

### 11.2 Binding Scopes

Binding scopes:

```text
user
workspace
server_global
```

Resolution order:

```text
user -> workspace -> server_global
```

Defaults:

- Default scope is `user`.
- `workspace` and `server_global` require explicit admin configuration and authorization.
- A package may forbid global credentials.

### 11.3 Credential Actor

Server-side credential use must declare actor:

```text
credential_actor:
  user_delegated | service_account
```

Audit events must include:

```text
capability_id
component_id
credential_requirement_id
credential_binding_scope
credential_actor
target
principal_user_id
secret_ref_id
```

Secret values must never enter LLM prompts, config files, normal event logs, or SessionRun public projection.

## 12. Install, Activation, and Update Flows

### 12.1 Install Flow

```text
GitHub link
-> fetch upstream snapshot
-> deterministic source inventory
-> LLM advisory classification
-> backend manifest normalization
-> dependency graph validation
-> action plan generation
-> install request confirmation only when risk/manual steps require it
-> materialize canonical store
-> execute server/local_peer install plans
-> check results
-> installed or degraded installed state
```

Install does not activate the package.

### 12.2 Activation Flow

```text
user activates package
-> validate install/check/credential state
-> activate valid components
-> hooks follow parent activation
-> MCP enters desired active state
-> runtime health updates asynchronously
-> package active or degraded
```

Default activation granularity is package-level. Component-level disable is an override.

### 12.3 Update Flow

```text
check upstream
-> detect release/tag/version/commit candidate
-> fetch candidate full snapshot
-> rebuild normalized manifest
-> deterministic manifest diff
-> LLM impact explanation
-> user approval
-> install candidate snapshot
-> verify target-side requirements
-> switch active_snapshot_id
-> keep rollback snapshot
```

Rules:

- User-visible version follows upstream.
- Main-only repositories are locked to the install commit.
- Labrastro may check for new main commits but must not auto-activate them.
- Manifest diff is backend-computed, not LLM-computed.

## 13. UI Contract

The UI must show clear human states, not raw protocol fields.

Required package summary:

```text
Package name and upstream version
Source repository and commit
Install state
Activation state
Update state
Degraded/blocked component count
Server target status
Local peer target status
Credential requirements
Manual steps needing user action
```

Required component summary:

```text
component name
component type
owner package
install_state
activation_state
runtime_state when applicable
check_state when applicable
credential_state when applicable
required_by / depends_on
target placement
last_error
```

The UI must not force users through large decision lists. It must default to package-level install/activation, surfacing only:

- blocking issues,
- degraded components,
- manual steps,
- credential binding choices,
- high-risk command reviews,
- update impact.

The UI must not expose internal terms such as `unsupported`, `mapping_required`, `unmapped enum`, or "waiting for developer support" as primary user messages. It must show:

- affected package/components,
- responsible actor: system, user, workspace admin, or server admin,
- concrete next action,
- whether Labrastro will retry automatically or needs the user to click re-check,
- which target is affected: server, local peer, or both.

User-facing labels must translate internal state axes into product language. For example, `materialized` should render as "prepared" or "written to controlled storage", not as a raw enum.

## 14. GitNexus Impact Review

GitNexus was updated to `1.6.7` and both Labrastro repositories were re-indexed on 2026-06-11 before freezing this matrix:

| Repository | Indexed commit | Scope |
| --- | --- | --- |
| `D:\AboutDEV\Labrastro\Labrastro` | `ed09357` | Server/domain/config/runtime/admin/API tests. |
| `D:\AboutDEV\Labrastro\Labrastro-vscode-extension` | `834480a` | VS Code local peer, remote client, ChatView, settings UI, frontend tests. |

The GitNexus refresh required an ASCII temp directory for LadybugDB FTS repair on this Windows machine:

```text
TEMP=D:\AboutDEV\Labrastro\.gitnexus-temp
TMP=D:\AboutDEV\Labrastro\.gitnexus-temp
gitnexus analyze --repair-fts .
```

During implementation, the actual worktree paths were also indexed under temporary aliases so `detect-changes` covered the edited files rather than the original clean repo paths:

| Worktree alias | Path | Index result |
| --- | --- | --- |
| `Labrastro-capability-package-ecosystem` | `D:\AboutDEV\Labrastro\.worktrees\capability-package-ecosystem` | `16,572 nodes | 36,885 edges | 640 clusters | 300 flows` |
| `Labrastro-vscode-extension-capability-package-ecosystem` | `D:\AboutDEV\Labrastro\.worktrees\capability-package-ecosystem-extension` | `5,151 nodes | 16,129 edges | 325 clusters | 300 flows` |

Original aliases `Labrastro` and `Labrastro-vscode-extension` remained registered to the clean source directories and returned `No changes detected`, so final impact evidence uses the worktree aliases above.

### 14.1 Server-Side Impact Boundary

| Area | Current symbol/file boundary | Required caution |
| --- | --- | --- |
| Draft accept/materialization | `labrastro_server/services/capability_packages.py`, `CapabilityPackageInstaller.install_draft` | This is the current hot path that turns draft directly into config and single-file skill materialization. It must be split behind compatibility adapters, not patched in place as a final architecture. |
| Draft/config models | `reuleauxcoder/domain/agent_runtime/models.py`, `CapabilityPackageDraft`, `CapabilityPackageConfig`, `resolve_capability_refs` | Existing `enabled/status` and component lists must be mapped into the new multi-axis state model without breaking current config reads. |
| Environment requirements | `reuleauxcoder/domain/config/models.py`, `EnvironmentRequirementConfig`; `reuleauxcoder/domain/environment_requirements.py` | Existing enum normalization accepts only authoritative environment kinds. New package-manager evidence must become isolated runtime requirements or open findings, not new enum values. |
| Admin API | `labrastro_server/services/admin/service.py`, `accept_capability_package_draft`, `delete_capability_package`, `enable_capability_package` | The current API conflates install and enable. New endpoints may be added, but old endpoints need a migration path and must not silently mean activation. |
| Environment/MCP projection | `labrastro_server/interfaces/http/remote/service.py`, environment manifest paths, MCP manifest/report paths | Server and peer facts must remain distinct. Do not let server config imply peer installation, verification, or runtime connection. |
| Lifecycle hooks | `reuleauxcoder/domain/hooks/lifecycle.py` and hook validation/tests | Hooks should follow parent activation, but invalid/unmapped hooks must still be rejected before entering the activatable manifest. Existing lifecycle runtime semantics must not regress. |
| Tests | `tests/labrastro_server/services/test_capability_packages.py`, `tests/domain/test_config_models.py`, `tests/domain/hooks/test_lifecycle.py`, `tests/labrastro_server/http/test_remote_service.py` | Existing tests prove current behavior; new tests should be added before behavior changes and old tests should be updated only when the matrix explains the semantic change. |

### 14.2 VS Code Extension Impact Boundary

| Area | Current symbol/file boundary | Required caution |
| --- | --- | --- |
| Capability package chat cards | `webview-ui/src/components/chat/SessionTurn.tsx`, `CapabilityPackageDraftReviewPart`, `CapabilityPackageInstallDecisionPart` | Replace raw draft/install approval framing with package-level install intent plus explicit risk/manual-step review. Do not surface huge decision lists. |
| Capability package view model | `webview-ui/src/settings/capabilityPackageView.ts` | Add projection for state axes and target status; keep current runtime footprint helpers until backend projection replaces them. |
| Capability settings UI | `webview-ui/src/settings/tabs/CapabilitiesTab.tsx` | Split install state, activation state, runtime state, check state, credential state, and update state. Existing `enabled/status` labels are not sufficient. |
| Remote client/protocol | `src/LabrastroRemoteClient.ts`, `src/protocol/messages.ts`, `src/LabrastroController.ts` | Add install-plan, activation, manual-step, peer-result, and credential-binding messages without breaking existing ingest-session start messages during migration. |
| Local peer state | `src/LabrastroRemoteClient.ts` peer preparation and environment manifest flow | Peer-owned install/check/runtime facts must be produced by peer execution results, not inferred from server settings. |
| Frontend tests | `webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx`, `webview-ui/src/components/chat/SessionTurn.test.ts`, `src/LabrastroController.admin.test.ts`, `src/LabrastroRemoteClient.test.ts` | Update assertions away from raw "pending review" hook workflows where activation now controls valid hooks, while keeping explicit warnings that packages contain hooks. |

### 14.3 Do-Not-Cross Boundaries

- Do not rewrite AgentRun as part of this capability-package redesign. AgentRun remains the execution/audit substrate.
- Do not use AgentRun event replay as the durable source of package truth after normalization. Store source snapshots, manifests, plans, and results in capability-package structures.
- Do not make server config the source of peer installation truth.
- Do not expose full snapshots to runtime agents to avoid solving missing path bugs.
- Do not make `enabled` carry both installed and active meanings during migration.
- Do not delete existing config compatibility until migration tests prove old packages can be read into the new projection.

### 14.4 Active Drift-Convergence GitNexus Baseline

Follow-up review after the first post-review convergence pass found that the remaining risk is not one isolated bug. The repeated pattern is that the architecture introduced new state/protocol fields, but old runtime and UI entry points still made local decisions from legacy booleans or partial projections.

The current active goal is therefore: **make capability-package invariants structural**. A path is not considered converged until it uses the shared state/projection, manifest diff, protocol normalizer, credential resolver, target fact, materialization helper, canonical install action identity, and authenticated local-peer runner boundary instead of route-local, peer-local, or UI-local logic.

Fresh GitNexus worktree evidence for this active convergence pass:

| Repository alias | Command | Result | Boundary found |
| --- | --- | --- | --- |
| `Labrastro-capability-package-ecosystem` | `gitnexus analyze .` | `16,591 nodes | 36,962 edges | 641 clusters | 300 flows` | Server index was rebuilt after an interrupted incremental run. |
| `Labrastro-capability-package-ecosystem` | `gitnexus detect-changes --repo Labrastro-capability-package-ecosystem --scope all` | `17 files, 102 symbols, 27 affected processes, critical` | Capability package admin/update/remote/config paths are critical impact surfaces. |
| `Labrastro-vscode-extension-capability-package-ecosystem` | `gitnexus analyze .` | `5,151 nodes | 16,129 edges | 325 clusters | 300 flows` | Extension worktree index is current. |
| `Labrastro-vscode-extension-capability-package-ecosystem` | `gitnexus detect-changes --repo Labrastro-vscode-extension-capability-package-ecosystem --scope all` | `14 files, 45 symbols, 9 affected processes, high` | Remote client/protocol, `CapabilitiesTab`, state view, and install-decision cards are high impact surfaces. |

Targeted GitNexus context queries established these concrete drift boundaries:

| Invariant | GitNexus symbol/process boundary | Required convergence |
| --- | --- | --- |
| Activation truth | `resolve_capability_refs` is called by `build_agent_run_snapshot` and `resolve_agent_effective_capability_scope`; `_package_managed_requirement_available` is called by `_build_environment_manifest`. | Both must stop treating `package.enabled` as runtime activation truth and use package activation projection. |
| Update diff truth | `manifest_diff` is called by `build_update_candidate`; `_sync_capability_components_from_package_manifest` is called by apply/rollback update routes. | Diff must compare stable normalized manifest content, and manifest sync must use shared component convergence. |
| UI state truth | `installedCapabilityPackages` feeds `capabilityPackageStateView`; `capabilityPackageStateView` feeds capability settings labels and tests. | Frontend must consume backend real `state`, top-level credential projection, and target-scoped facts without inventing local status. |

### 14.5 GitNexus Execution Protocol

These rules are mandatory for the remaining convergence work:

1. Before each task, run GitNexus on both worktrees with explicit aliases:

```powershell
gitnexus analyze .
gitnexus detect-changes --repo Labrastro-capability-package-ecosystem --scope all
gitnexus detect-changes --repo Labrastro-vscode-extension-capability-package-ecosystem --scope all
```

2. Before changing a core invariant, query GitNexus for definition points, call sites, affected processes, and related tests. Minimum required queries are:

```powershell
gitnexus query --repo Labrastro-capability-package-ecosystem "<invariant>" --context "<current task>" --goal "<what may drift>" --limit 8
gitnexus context --repo Labrastro-capability-package-ecosystem <symbol> --file <path>
gitnexus impact --repo Labrastro-capability-package-ecosystem <symbol> --file <path> --include-tests
```

Use the extension alias for frontend/protocol symbols.

3. After editing any shared helper, protocol model, state projection, admin/remote service, or frontend view model, re-run GitNexus `analyze` and `detect-changes` for the edited worktree before marking the task complete.

4. Every convergence evidence row must include the chain:

```text
GitNexus impact boundary -> files changed -> tests/grep that prove the invariant
```

5. If GitNexus cannot run, record the command, error, and temporary fallback. Do not silently replace it with `rg`; use `rg` only as a documented fallback and keep GitNexus recovery as an open task.

6. Final closure requires both GitNexus and grep evidence that there are no runtime bypasses of package activation projection, no scattered materialization logic, no frontend-invented state axes, no protocol alias parsing split across entry points, and no local-peer install execution path that runs from `peerConnected` alone or re-runs `installed/passed` actions.

## 15. Implementation Matrix

### 15.1 Backend Domain and Persistence

| ID | Work item | Files / areas | Acceptance |
| --- | --- | --- | --- |
| B-01 | Add authoritative state enums | `reuleauxcoder/domain/...`, config models, admin views | All state axes in this document have typed constants and tests rejecting unknown values. |
| B-02 | Add source snapshot model | persistence + capability services | Snapshot records include upstream version, commit, snapshot id, content hash, and path. |
| B-03 | Add normalized manifest model | capability package domain/service split | Manifest contains components, dependency graph, install plans, exposed closures, unmapped findings. |
| B-04 | Split LLM advisory output from manifest | `labrastro_server/services/capability_package_ingest.py`, `capability_packages.py` | LLM unknown enum produces unmapped finding, not whole package failure. |
| B-05 | Add dependency graph validation | capability package normalizer | Invalid dependency edges block only affected components. |
| B-06 | Replace single `SKILL.md` materialization | `CapabilityPackageInstaller`, Skill discovery | Full snapshot is stored; skill registrations point to entry paths and controlled closures. |
| B-07 | Add install plan action catalog | new focused module under capability packages | Shell commands are generated only from typed actions. |
| B-08 | Add credential requirement and binding refs | auth/credential service, admin API | User/workspace/global scopes resolve in order and audit actor identity. |
| B-09 | Add update candidate flow | capability package service | Candidate snapshot and deterministic manifest diff are available without activation. |
| B-10 | Add shared capability registry | environment/capability package domain | Shared tools such as `gh` and `git` are checked through registry entries with version checks, platform policy, and install-action policy. |
| B-11 | Add compatibility projection for old package config | `CapabilityPackageConfig`, admin service | Existing `enabled/status` packages load into new state axes without treating config write as activation or peer verification. |

### 15.2 Server Executor

| ID | Work item | Files / areas | Acceptance |
| --- | --- | --- | --- |
| S-01 | Execute server `InstallPlan` actions | server runtime service | `write_skill_bundle`, `check_executable`, Python venv, Node env, MCP config actions produce result records. |
| S-02 | Isolate package-manager dependencies | runtime home / package directories | Python/npm installs never default to global/user package locations. |
| S-03 | Start and check server MCP | MCP manager | `activation_state=active` starts desired MCP; `runtime_state` reflects process truth. |
| S-04 | Report server target facts | admin/status APIs | Server target status is aggregated into package/component status. |
| S-05 | Persist install/check action results | capability package result store | Result records include action id, target, status, version/hash when available, stderr summary, and timestamp. |

### 15.3 Local Peer Executor

| ID | Work item | Files / areas | Acceptance |
| --- | --- | --- | --- |
| P-01 | Define peer install protocol | remote protocol + VS Code extension | Peer accepts same `InstallPlan` shape and returns same result schema. |
| P-02 | Execute peer skill delivery | VS Code peer runtime | Peer can sync controlled skill closures without becoming the global fact source. |
| P-03 | Execute peer env checks/install actions | VS Code peer runtime | Peer reports check/install facts with target, timestamp, version/hash. |
| P-04 | Execute peer MCP lifecycle | VS Code peer runtime | Peer MCP activation and runtime health are separate states. |
| P-05 | Bind peer credentials | VS Code SecretStorage / OS keychain | User-scoped local secrets are never sent to LLM or server logs. |
| P-06 | Reconcile peer results with server desired state | remote protocol + server aggregation | Server shows stale/missing peer facts as such; it does not convert desired state into verified state. |

### 15.4 Frontend

| ID | Work item | Files / areas | Acceptance |
| --- | --- | --- | --- |
| F-01 | Package status view | VS Code webview settings/capability UI | Shows install, activation, update, degraded, target, and credential states separately. |
| F-02 | Install/activation flow UI | ChatView + Settings | Install and activation are separate user actions; hooks follow activation. |
| F-03 | Manual step UI | Settings/ChatView | Manual completion triggers re-check, not success. |
| F-04 | Dependency graph and degraded UI | capability package view | A blocked requirement shows affected components, not whole-package failure. |
| F-05 | Update candidate UI | settings/capability UI | Shows upstream version, manifest diff, impact summary, and activation approval. |
| F-06 | Replace internal-state wording | `SessionTurn`, `CapabilitiesTab`, i18n | UI does not show `unsupported`, `mapping_required`, or "waiting for developer support" as primary messages. |
| F-07 | Show hook notice without hook-by-hook approval | `SessionTurn`, `CapabilitiesTab` | Package install/activation surfaces that hooks exist; valid hooks follow activation and invalid hooks are shown as blockers. |

### 15.5 Tests and Evidence

| ID | Work item | Required tests |
| --- | --- | --- |
| T-01 | Waza regression | `python-pkg` becomes unmapped/runtime-isolated finding; 8 skills remain installable. |
| T-02 | Full skill bundle | Skill with `references/`, `scripts/`, and shared `rules/` keeps valid relative paths. |
| T-03 | Dependency conflicts | Shared `gh` is checked once and referenced by multiple skills. Python package requirements are isolated. |
| T-04 | Install vs activate | Installed package remains inactive until activation. Hooks become active only through parent activation. |
| T-05 | MCP runtime truth | Active MCP can be `failed` or `connected`; UI does not conflate activation with connection. |
| T-06 | Credentials | User credential wins over workspace/global; global requires admin authorization; secrets never appear in public projection. |
| T-07 | Update | Main-only repository installs as `main@commit`; new commit becomes candidate, not active. |
| T-08 | Peer target facts | Server cannot mark local peer installed/verified without peer result. |
| T-09 | Multi-skill repository layout | Fixture with root `SKILL.md`, nested skills, shared `scripts/`, shared `references/`, shared root files, namespaced ids, and controlled closure assertions. |
| T-10 | Shared capability registry | Two packages requiring `gh` share one registry-backed check and keep separate dependent edges. |
| T-11 | UI wording boundary | Frontend tests prove internal states render as action-oriented messages, not raw unsupported/waiting-for-developer wording. |
| T-12 | Legacy config migration | Old `enabled/status` package config reads into new state axes and remains disabled/inactive correctly. |

### 15.6 Evidence Log

| Date | Task | Matrix rows | Evidence |
| --- | --- | --- | --- |
| 2026-06-11 | Task 1: state axes and legacy projection | B-01, B-11, T-12 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\domain\test_capability_package_domain.py tests\domain\test_config_models.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `43 passed in 0.87s`; changed files: `reuleauxcoder/domain/capability_packages.py`, `reuleauxcoder/domain/agent_runtime/models.py`, `tests/domain/test_capability_package_domain.py`. |
| 2026-06-11 | Task 2: source snapshot and normalized manifest models | B-02, B-03 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\domain\test_capability_package_domain.py tests\domain\test_config_models.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `45 passed in 0.95s`; changed files: `reuleauxcoder/domain/capability_packages.py`, `tests/domain/test_capability_package_domain.py`. |
| 2026-06-11 | Task 3: LLM advisory normalizer and open findings | B-04, G-09, T-01 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_normalizer.py tests\labrastro_server\services\test_capability_package_ingest_fields.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `13 passed in 0.90s`; Waza-style `python_package` and `envreq:python-pkg:*` become `unmapped_findings.unclassified_requirements`; changed files: `labrastro_server/services/capability_package_normalizer.py`, `labrastro_server/services/capability_package_ingest.py`, `tests/labrastro_server/services/test_capability_package_normalizer.py`, `tests/labrastro_server/services/test_capability_package_ingest_fields.py`. |
| 2026-06-11 | Task 4: full skill artifact bundle closures | B-02, B-06, T-02, T-09 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_artifacts.py tests\labrastro_server\services\test_capability_packages.py::test_package_installer_materializes_skill_to_canonical_server_path tests\labrastro_server\services\test_capability_packages.py::test_package_installer_keeps_shared_skill_path_stable_when_owner_changes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `4 passed in 2.91s`; artifact closure preserves nested skill files and shared explicit paths while denying `.env`, `token`, and `node_modules`; changed files: `labrastro_server/services/capability_package_artifacts.py`, `labrastro_server/services/capability_packages.py`, `tests/labrastro_server/services/test_capability_package_artifacts.py`. |
| 2026-06-11 | Task 5: dependency graph and shared registry | B-05, B-10, T-03, T-10 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_dependencies.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `3 passed in 0.86s`; shared `gh` maps to one `shared:executable:gh` registry node while retaining per-skill dependency edges; invalid dependency edges block only dependent components; changed files: `labrastro_server/services/capability_package_dependencies.py`, `reuleauxcoder/domain/capability_packages.py`, `tests/labrastro_server/services/test_capability_package_dependencies.py`. |
| 2026-06-11 | Task 6: typed InstallPlan and server executor records | B-07, S-01, S-02, S-05 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_install_plan.py tests\labrastro_server\services\test_capability_package_executor.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `5 passed in 0.10s`; `shell` action is rejected, server executable checks record only server target facts, and Python package runtime paths are package-local under `runtime_root`; changed files: `labrastro_server/services/capability_package_install_plan.py`, `labrastro_server/services/capability_package_executor.py`, `tests/labrastro_server/services/test_capability_package_install_plan.py`, `tests/labrastro_server/services/test_capability_package_executor.py`. |
| 2026-06-11 | Task 7: accept/install/activation split | B-11, S-04, F-02, T-04, G-10 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_admin_service.py::test_accept_capability_package_installs_without_activation tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_admin_capability_package_accept_returns_separate_state_axes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `2 passed in 4.50s`; accepted packages now return `state`, keep `status=installed`, and default `enabled=False` so activation is inactive until explicit enable/activation; changed files: `labrastro_server/services/capability_packages.py`, `reuleauxcoder/domain/agent_runtime/models.py`, `tests/labrastro_server/services/test_admin_service.py`, `tests/labrastro_server/http/test_remote_service.py`. |
| 2026-06-11 | Task 8: local peer install protocol and peer-owned facts | P-01, P-02, P-03, P-04, P-06, T-08 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_capability_package_peer_install_plan_waits_for_peer_result --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `1 passed in 4.19s`; `npx vitest run src/LabrastroRemoteClient.test.ts src/protocol/messages.test.ts` in `D:\AboutDEV\Labrastro\.worktrees\capability-package-ecosystem-extension` -> PASS, `2 passed`, `52 passed` in `4.10s`; server now exposes peer-token `capabilityPackage.installPlan` and `capabilityPackage.installResult`, keeps desired action separate from `peer_result`, and does not infer local peer installed from server config. |
| 2026-06-11 | Task 9: credentials and multi-tenant bindings | B-08, P-05, T-06 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_credentials.py tests\labrastro_server\services\test_admin_service.py::test_server_settings_projects_capability_package_credential_bindings tests\labrastro_server\services\test_admin_service.py::test_accept_capability_package_preserves_credential_requirements --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `9 passed in 1.31s`; `npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/useSettingsController.test.tsx` -> PASS, `2 passed`, `56 passed` in `1.22s`; user credential wins over workspace/global, server-global requires admin authorization, accepted packages preserve `credential_requirements`, and frontend scope labels do not retain secret values. |
| 2026-06-11 | Task 10: activation, hooks, and MCP runtime truth | S-03, F-02, T-04, T-05 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\domain\hooks\test_lifecycle.py tests\labrastro_server\services\test_capability_packages.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `264 passed in 5.98s`; lifecycle dashboard exposes `owner_activation_state`, inactive package hooks remain visible but do not enter runtime, activated package hooks can execute with available adapters, and explicit `runtime_state=failed` remains separate from `activation_state=active`. |
| 2026-06-11 | Task 11: upstream update candidate flow | B-09, F-05, T-07, G-08 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\services\test_admin_service.py::test_prepare_capability_package_update_records_candidate_without_activation tests\labrastro_server\services\test_admin_service.py::test_apply_capability_package_update_candidate_does_not_auto_activate tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_admin_capability_package_update_candidate_routes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `8 passed in 4.17s`; update candidates use upstream version display such as `main@commit`, backend-computed manifest diffs, rollback snapshot references, and no-auto-activation semantics. |
| 2026-06-11 | Task 12: frontend projection and copy boundaries | F-01, F-03, F-04, F-06, F-07, T-11, G-12 | `npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/components/chat/SessionTurn.test.ts` -> PASS, `3 passed`, `48 passed` in `4.00s`; `npm run typecheck` -> PASS; UI renders install/activation/server/local/credential/update labels, avoids raw `mapping_required`, and install confirmation cards show hooks/manual command/re-check semantics without per-hook approval controls. |
| 2026-06-11 | Task 13: end-to-end regression matrix closure | G-01 through G-12, T-01 through T-12 | `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_ingest_fields.py tests\labrastro_server\services\test_capability_package_normalizer.py tests\labrastro_server\services\test_capability_package_artifacts.py tests\labrastro_server\services\test_capability_package_dependencies.py tests\labrastro_server\services\test_capability_package_install_plan.py tests\labrastro_server\services\test_capability_package_executor.py tests\labrastro_server\services\test_capability_package_credentials.py tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\services\test_capability_packages.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `147 passed in 3.33s`; `npx vitest run src/LabrastroRemoteClient.test.ts src/LabrastroController.admin.test.ts webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/components/chat/SessionTurn.test.ts` -> PASS, `5 passed`, `117 passed` in `4.39s`; `npm run typecheck` -> PASS. Waza regression fixture covers 8 skills plus `readability-lxml`/`html2text` open finding and dependent-only blocking. GitNexus worktree `detect-changes` reported server `15 files, 89 symbols, 31 affected processes, critical` concentrated in capability package/admin/remote/config flows and extension `12 files, 41 symbols, 9 affected processes, high` concentrated in remote client/protocol/CapabilitiesTab/SessionTurn. |
| 2026-06-11 | Post-review convergence closure | C-01 through C-08, G-13 through G-20 | Failing-before evidence: accepted package components were still `enabled=True`; component hooks under inactive package owners reported `owner_activation_state=active`; peer status used short `action_id` keys and failed duplicate-action collision expectations; no-diff update checks returned `update_available=True`; rollback retained consumed `rollback` metadata; normalizer admitted prefix/kind mismatches such as `envreq:executable:gh` with `kind=skill`. Passing-after evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_admin_service.py::test_accept_capability_package_installs_without_activation tests\labrastro_server\services\test_admin_service.py::test_disable_capability_package_keeps_shared_component_active_for_other_owner tests\labrastro_server\services\test_admin_service.py::test_check_capability_package_update_does_not_report_no_diff_candidate --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `3 passed in 1.45s`; `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\domain\hooks\test_lifecycle.py::test_lifecycle_registry_gates_capability_package_hooks_by_activation_state tests\domain\hooks\test_lifecycle.py::test_lifecycle_registry_gates_package_component_hooks_by_owner_activation --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `2 passed in 1.00s`; `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_capability_package_peer_install_plan_waits_for_peer_result tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_capability_package_peer_results_do_not_collide_on_action_id tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_admin_capability_package_update_candidate_routes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `3 passed in 9.46s`; capability package service regression command from Task 13 now -> PASS, `149 passed in 3.50s`; extension affected tests -> PASS, `5 passed`, `117 passed in 4.67s`; `npm run typecheck` -> PASS; GitNexus worktree impact: server `15 files, 101 symbols, 42 affected processes, critical`; extension `12 files, 41 symbols, 9 affected processes, high`. |
| 2026-06-11 | Active drift convergence: runtime activation and resource helper | C-09, C-10, G-21, G-22 | GitNexus recovery/evidence: initial worktree alias was absent from registry and incremental repair hit `lbug.wal without lbug.shadow`; `gitnexus analyze -f --index-only --name Labrastro-capability-package-ecosystem .` rebuilt the worktree index, then `gitnexus detect-changes --repo Labrastro-capability-package-ecosystem --scope all` reported `22 files, 119 symbols, 18 affected processes, critical`. GitNexus query for `_sync_capability_components_from_package_manifest delete enable apply rollback materialize_component remove_materialized_component` showed delete/rollback processes entering the shared helper and linked the apply/rollback resource tests. C-09 failing-before tests proved inactive packages leaked through fallback catalog and environment/runtime surfaces; passing command `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\services\config\test_loader.py::test_config_validate_projects_capability_package_hook_activation_state tests\services\config\test_agent_runtime_config_loader.py::test_agent_effective_capability_scope_excludes_disabled_package_runtime_resources tests\domain\agent_runtime\test_runtime_models.py::test_resolve_capability_refs_uses_activation_state_not_enabled_only tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_environment_manifest_endpoint_returns_structured_manifest tests\domain\hooks\test_lifecycle.py::test_lifecycle_registry_gates_capability_package_hooks_by_activation_state tests\domain\hooks\test_lifecycle.py::test_lifecycle_registry_gates_package_component_hooks_by_owner_activation --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `6 passed in 4.41s`. C-10 failing-before test proved update apply removed `capability_components` but left stale materialized skill resources; passing command `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_admin_service.py tests\services\config\test_agent_runtime_config_loader.py::test_accept_and_delete_capability_package_manages_shared_components tests\services\config\test_agent_runtime_config_loader.py::test_admin_rejects_builtin_capability_package_disable_and_delete tests\services\config\test_loader.py::test_config_validate_projects_capability_package_hook_activation_state tests\services\config\test_agent_runtime_config_loader.py::test_agent_effective_capability_scope_excludes_disabled_package_runtime_resources tests\domain\agent_runtime\test_runtime_models.py::test_resolve_capability_refs_uses_activation_state_not_enabled_only tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_environment_manifest_endpoint_returns_structured_manifest --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `64 passed in 4.35s`. Grep shows remaining `package.enabled` reads only in admin enable write/sync path, not runtime activation decisions; `_sync_capability_components_from_package_manifest` is called by accept, delete, enable/disable, apply update, and rollback. |
| 2026-06-11 | Active drift convergence: config/SKILL file transaction policy | C-11, G-23 | C-11 policy is now explicit for capability package paths: build desired config and queued SKILL file operations, commit config, apply file operations, then on file operation failure restore pre-operation file snapshots and roll config back to `previous_data`. Failing-before evidence: `test_accept_capability_package_rolls_back_config_when_skill_file_write_fails` left installed package config after a blocked SKILL write. Passing command: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_admin_service.py tests\services\config\test_agent_runtime_config_loader.py::test_admin_accept_does_not_write_skill_file_when_config_commit_fails tests\services\config\test_agent_runtime_config_loader.py::test_admin_delete_does_not_remove_skill_file_when_config_commit_fails tests\services\config\test_agent_runtime_config_loader.py::test_accept_and_delete_capability_package_manages_shared_components --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `62 passed in 1.95s`. GitNexus refresh after the transaction helper reported `22 files, 127 symbols, 18 affected processes, critical`, still concentrated in capability package/admin flows. |
| 2026-06-11 | Active drift convergence: full stable manifest diff | C-12, G-24 | Failing-before evidence: `test_manifest_diff_detects_same_count_non_component_section_changes` raised `KeyError: changed_sections`, proving same-count dependency/environment/credential/install plan/activation rule/file closure/package/update metadata changes were invisible to `manifest_diff_has_changes`. Passing command: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\services\test_admin_service.py::test_prepare_capability_package_update_records_candidate_without_activation tests\labrastro_server\services\test_admin_service.py::test_check_capability_package_update_does_not_report_no_diff_candidate tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_admin_capability_package_update_candidate_routes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `10 passed in 4.66s`. `manifest_diff` now preserves legacy component/delta fields and adds stable `changed_sections` over package, components, dependency edges, environment requirements, credential requirements, install plans, activation rules, exposed file closures, and update metadata. |
| 2026-06-11 | Active drift convergence: protocol/action/credential/source aliases | C-13, G-25 | GitNexus targeted queries for `InstallAction CapabilityPackageInstallResultRecord action_id id _peer_install_action _build_peer_install_plan`, `CapabilityCredentialRequirement CapabilityCredentialBinding requirement_id secret_ref credential_ref_id allowed_scopes`, and `candidate_snapshot candidate_manifest source_snapshot build_update_candidate detect_upstream_version` identified the protocol/domain boundaries: install action parsing, install result records, credential binding resolution, and admin update candidate payloads. Failing-before evidence: `InstallAction.from_dict` accepted only `id`, credential bindings accepted only `requirement_id`/`secret_ref_id`, and admin update payload handling read only `candidate_snapshot`/`candidate_manifest`. Passing command: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_install_plan.py tests\labrastro_server\services\test_capability_package_credentials.py tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_capability_package_peer_install_plan_waits_for_peer_result tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_capability_package_peer_results_do_not_collide_on_action_id tests\labrastro_server\services\test_admin_service.py::test_prepare_capability_package_update_records_candidate_without_activation tests\labrastro_server\services\test_admin_service.py::test_prepare_capability_package_update_accepts_alias_payload tests\labrastro_server\services\test_admin_service.py::test_check_capability_package_update_does_not_report_no_diff_candidate --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `26 passed in 6.75s`. A regression discovered during C-13 proved normalized empty manifest sections were being treated as meaningful diffs; `test_manifest_diff_treats_missing_and_empty_sections_as_equivalent` now guards that `missing` and `empty` sections compare equal. Grep for route-local alias parsing now leaves only `registry_by_requirement.get(requirement_id)`, which is dictionary lookup by normalized variable, not protocol parsing. Post-edit GitNexus recovery: server incremental analyze exited non-zero after `+201 importer(s) added to writable set`; `gitnexus analyze -f --index-only --name Labrastro-capability-package-ecosystem .` rebuilt the index successfully. Latest detect-changes: server `22 files, 128 symbols, 18 affected processes, critical`; extension `14 files, 45 symbols, 9 affected processes, high`. |
| 2026-06-11 | Active drift convergence: frontend backend fact alignment | C-14, G-26 | GitNexus query for `installedCapabilityPackages capabilityPackageView CapabilitiesTab runtime_state check_state target_facts credential_requirements credential_state` identified frontend boundaries in `capabilityPackageValue`, `dashboardItemToRecord`, `installedCapabilityPackages`, `capabilityPackageStateView`, and `targetStatusLabel`. Failing-before evidence: `capabilityPackageStateView`/`targetStatusLabel` copied one package-level `runtime_state`/`check_state` into both server and local-peer labels. Passing evidence: `npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts --testNamePattern "target labels|package state axes"` -> PASS, `2 passed`; `npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx` -> PASS, `28 passed`; affected extension command `npx vitest run src/LabrastroRemoteClient.test.ts src/LabrastroController.admin.test.ts webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/components/chat/SessionTurn.test.ts` -> PASS, `5 passed`, `118 passed`; `npm run typecheck` -> PASS. `targetStatusLabel` now reads `target_facts`/`targetFacts`/target-named objects and shows missing target facts instead of duplicating package aggregate runtime/check state. `CapabilitiesTab` fixture uses real server settings shape with package-level `credential_requirements` and `target_facts`; rendered output keeps credential scope labels and proves `本地端：运行中，检查通过` is not fabricated from the package-level state. Latest GitNexus detect-changes: server `22 files, 128 symbols, 18 affected processes, critical`; extension `14 files, 48 symbols, 9 affected processes, high`. |
| 2026-06-11 | Active drift convergence: final audit closure | C-09 through C-14, G-21 through G-26 | Grep audit: `package.enabled` appears only in admin enable write path `labrastro_server/services/admin/service.py:2190`; route-local alias parsing grep for `action_id`, `candidate_snapshot`, `candidate_manifest`, `secret_ref`, and `credential_ref_id` returned no hits; materialize/remove calls are limited to `CapabilityPackageInstaller`, tests, and `_sync_capability_components_from_package_manifest`; frontend target label grep shows `targetStatusLabel` reads through `targetStateValue`. Server regression: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_ingest_fields.py tests\labrastro_server\services\test_capability_package_normalizer.py tests\labrastro_server\services\test_capability_package_artifacts.py tests\labrastro_server\services\test_capability_package_dependencies.py tests\labrastro_server\services\test_capability_package_install_plan.py tests\labrastro_server\services\test_capability_package_executor.py tests\labrastro_server\services\test_capability_package_credentials.py tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\services\test_capability_packages.py tests\labrastro_server\services\test_admin_service.py tests\services\config\test_agent_runtime_config_loader.py::test_agent_effective_capability_scope_excludes_disabled_package_runtime_resources tests\services\config\test_loader.py::test_config_validate_projects_capability_package_hook_activation_state tests\domain\agent_runtime\test_runtime_models.py::test_resolve_capability_refs_uses_activation_state_not_enabled_only tests\domain\hooks\test_lifecycle.py::test_lifecycle_registry_gates_capability_package_hooks_by_activation_state tests\domain\hooks\test_lifecycle.py::test_lifecycle_registry_gates_package_component_hooks_by_owner_activation tests\domain\test_capability_package_domain.py --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `226 passed in 4.25s`; HTTP focused protocol tests -> PASS, `5 passed in 14.28s`; extension affected tests -> PASS, `118 passed in 4.67s`; `npm run typecheck` -> PASS. Final GitNexus refresh succeeded on both worktrees; detect-changes reports server `22 files, 128 symbols, 18 affected processes, critical` and extension `14 files, 48 symbols, 9 affected processes, high`, matching the documented capability package/admin/remote/config and CapabilitiesTab/SessionTurn impact boundaries. |

**Correction after follow-up GitNexus audit:** The C-14 row and the "final audit closure" row above are superseded by the 2026-06-11 follow-up audit. That audit found three same-class drifts: update helpers read raw `enabled` as activation truth, rollback accepted empty consumed metadata, and `CapabilitiesTab` fed only nested `state` into the frontend state view while backend credential/target facts were package-level. These drifts are now closed by C-15 through C-18 and G-27 through G-30. Keep the earlier C-14/final-audit rows as historical baseline only; use the C-15 through C-18 evidence below as the current closure.

**Correction after local-peer install audit:** A later review found the same drift class in the install execution contract: service and extension paths were both using partial local interpretations of install identity and readiness. The server compared peer results with an action key that missed `params.component_id`; the extension local runner executed actions again even when `peer_status` already said `installed/passed`; and the controller started the local runner from `peerConnected` without requiring authenticated ready state. These drifts are closed by C-19 through C-22 and G-31 through G-34. Older closure rows remain historical baseline only.

Current reclosure evidence:

| Date | Evidence group | Rows / gates | Evidence |
| --- | --- | --- | --- |
| 2026-06-11 | Active drift convergence reclosure | C-15 through C-18, G-27 through G-30 | Baseline GitNexus queries covered server activation drift (`apply_update_candidate rollback_update_candidate capability_package_is_active capability_package_state_projection enabled`), server rollback drift (`rollback_capability_package_update rollback_update_candidate rollback_not_available update_state`), and extension frontend fact drift (`CapabilitiesTab installedCapabilityPackages capabilityPackageStateView credential_state target_facts useSettingsController`). Impact review covered `apply_update_candidate`, `rollback_update_candidate`, `rollback_capability_package_update`, and `installedCapabilityPackages`. Passing server evidence: `D:\AboutDEV\Labrastro\Labrastro\.venv\Scripts\python.exe -m pytest -q tests\labrastro_server\services\test_capability_package_updates.py tests\labrastro_server\services\test_admin_service.py::test_rollback_capability_package_update_rejects_empty_rollback_metadata tests\labrastro_server\services\test_admin_service.py::test_rollback_capability_package_update_rejects_consumed_rollback tests\labrastro_server\http\test_remote_service.py::TestRemoteRelayHTTPService::test_admin_capability_package_update_candidate_routes --basetemp .pytest-tmp -o cache_dir=.pytest-cache-codex` -> PASS, `14 passed in 4.97s`. Passing extension evidence: `npx vitest run webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/tabs/CapabilitiesTab.test.tsx webview-ui/src/settings/useSettingsController.test.tsx` -> PASS, `67 passed in 3.71s`; `npm run typecheck` -> PASS. Grep evidence: update lifecycle no longer contains `current.get("enabled")`; rollback availability is guarded by `rollback_update_available`; `CapabilitiesTab` now calls `capabilityPackageStatePayload(item)`. Final GitNexus refresh succeeded on both worktrees; final detect-changes reports server `22 files, 130 symbols, 10 affected processes, high` and extension `14 files, 50 symbols, 10 affected processes, high`. |
| 2026-06-11 | Local-peer install execution reclosure | C-19 through C-22, G-31 through G-34 | Baseline GitNexus refresh succeeded on both worktrees: server `16,655 nodes | 37,117 edges | 649 clusters | 300 flows`; extension `5,195 nodes | 16,268 edges | 328 clusters | 300 flows`. Targeted GitNexus queries covered server install/action/result identity (`_peer_install_action _peer_action_key _peer_result_key _peer_result_is_stale component_id params expected_content_hash content_hash`) and extension runner gating/idempotency (`actionAlreadyInstalled installActionKey peer_status install_state check_state updateCapabilityPackageLocalPeerRunner authenticated status ready peerConnected`). Failing-before evidence: server key comparison dropped `params.component_id`; extension skip test timed out because `install_python_packages` still ran; repeated `runOnce` submitted the same install result twice; unauthenticated `peerConnected` started the runner. Passing-after evidence: server targeted tests -> PASS, `2 passed`; extension targeted tests -> PASS, `3 passed`; focused server regression -> PASS, `21 passed`; broad server capability-package regression -> PASS, `232 passed`; affected extension tests -> PASS, `126 passed`; `npm run typecheck` -> PASS. Grep evidence: service action/result keys converge through `_install_action_identity`; route-local `params.component_id` and hand-built `|` keys have no residual matches; extension runner skip logic is centralized in `actionAlreadyInstalled`/`installActionKey`; controller lifecycle is keyed by `capabilityPackageLocalPeerRunnerKey`. Post-fix GitNexus refresh reports server `16,661 nodes | 37,149 edges | 647 clusters | 300 flows` and extension `5,199 nodes | 16,294 edges | 322 clusters | 300 flows`; detect-changes reports server `22 files, 133 symbols, 10 affected processes, high` and extension `16 files, 61 symbols, 22 affected processes, critical`, concentrated in capability-package remote/admin paths and extension local-peer/controller/frontend capability paths. |

### 15.7 Post-Review Convergence Matrix

Code review after Task 13 found a repeated pattern: the architecture was documented, but several implementation paths still used local state decisions. Follow-up GitNexus review extended that finding beyond the first fourteen convergence rows and then into local-peer install execution. These rows supersede any earlier "complete" interpretation until all C rows and G-13 through G-34 are green.

| ID | Work item | Files / areas | Required acceptance |
| --- | --- | --- | --- |
| C-01 | Introduce shared package lifecycle state machine/projection boundary | `reuleauxcoder/domain/capability_packages.py`, `labrastro_server/services/capability_packages.py`, `labrastro_server/services/admin/service.py` | All package lifecycle mutations call shared transition/projection helpers. Direct writes to `enabled`, `state`, `rollback`, or `update_candidate` outside serializers are removed or justified. |
| C-02 | Make install inactive all the way down | `CapabilityPackageInstaller.install_draft`, `materialize_component`, skill/MCP/env requirement materialization, admin enable route | Accept/install writes artifacts and config but package-managed skills, MCP resources, components, and hooks are not active/executable until activation. Tests must prove accepted package resources do not leak into active runtime. |
| C-03 | Propagate activation through package ownership | package enable/disable route, component/materialized resource projection, shared component handling | Enabling a package activates only valid owned resources; disabling a package deactivates resources that have no other active owner; shared components remain desired-active only if an active owner still needs them. |
| C-04 | Gate hooks through owner chain | `reuleauxcoder/domain/hooks/lifecycle.py`, package/component hook tests | Package-level and component-level hooks both consult activation state through package ownership. A component hook from an inactive package is visible but not executable. |
| C-05 | Replace peer result single-key store with target fact identity | `labrastro_server/interfaces/http/remote/routes/capability_packages.py`, remote protocol tests, extension client tests if shape changes | Peer results are keyed and reconciled by `peer_id`, `target`, `package_id`, `plan_id`, `action_id`, `component_id`, and content/version hash where available. Collision tests with duplicate `action_id` across packages must pass. |
| C-06 | Convert update flow into explicit lifecycle transitions | `labrastro_server/services/capability_package_updates.py`, admin update routes/tests | `update_available` is true only when diff fields are non-empty/non-zero. Successful rollback clears or replaces rollback metadata consistently. Repeated rollback after `update_state=current` is rejected or represented as a deliberate redo transition. |
| C-07 | Harden manifest id/kind normalization | `labrastro_server/services/capability_package_normalizer.py`, manifest/domain tests | `envreq:*` cannot enter `components` as `skill`, `mcp_server`, or any non-environment kind. Prefix/kind mismatch tests for `envreq:*`, `skill:*`, `mcp:*`, and `credreq:*` pass. |
| C-08 | Close regression and matrix evidence | server regression set, extension regression set, GitNexus impact check, this matrix | Evidence log includes focused failing-before/passing-after tests for C-01 through C-07, server/extension regression commands, and GitNexus impact review. |
| C-09 | Replace remaining `package.enabled` runtime activation bypasses | `reuleauxcoder/domain/agent_runtime/models.py`, `labrastro_server/interfaces/http/remote/service.py`, related config/runtime tests | `resolve_capability_refs`, environment manifest filtering, and any runtime scope calculation use `capability_package_is_active` or a shared owner projection. Grep/GitNexus prove no runtime path decides active capability from `package.enabled` alone. |
| C-10 | Promote component/resource convergence into one helper | `labrastro_server/services/admin/service.py`, `labrastro_server/services/capability_packages.py`, update apply/rollback tests | Install, delete, enable/disable, apply update, and rollback update all call the same helper to recalculate owner-derived enabled state and materialize/remove resources. Shared-component removal from one active owner cannot leave stale active resources behind. |
| C-11 | Make config commit and skill-file operations transactional by policy | `RemoteAdminConfigManager`, `CapabilityPackageInstaller`, admin failure tests | All paths use one documented ordering and failure strategy for config save/reload and SKILL file write/delete. Tests prove commit failure cannot leave files in a state that contradicts persisted config. |
| C-12 | Replace partial manifest diff with stable full-manifest diff | `labrastro_server/services/capability_package_updates.py`, manifest tests | Same-count changes to dependencies, environment requirements, credentials, install plans, activation rules, exposed file closures, and package/update metadata are detected. No-diff manifests stay no-diff. |
| C-13 | Centralize protocol/action/credential field alias normalization | install plan/result protocol, manifest/source snapshot normalizers, credential service | `id`/`action_id`, `id`/`requirement_id`, package/source aliases, and credential binding fields round-trip through typed normalizers. Entry points no longer hand-roll conflicting alias rules. |
| C-14 | Align frontend state with backend target and credential facts | `webview-ui/src/settings/capabilityPackageView.ts`, `CapabilitiesTab.tsx`, controller/view tests | Frontend fixtures use real server settings payload shape. Credential state does not disappear because it is top-level. Server/local-peer labels read target-scoped facts, not duplicated top-level runtime/check state. |
| C-15 | Replace update helper raw activation truth | `labrastro_server/services/capability_package_updates.py`, `reuleauxcoder/domain/capability_packages.py`, update/admin/http tests | `apply_update_candidate` and `rollback_update_candidate` must decide prior activation through `capability_package_is_active` or a shared transition helper, never `current.get("enabled")`. Grep and GitNexus must prove no update lifecycle path uses raw `enabled` as activation truth. |
| C-16 | Make rollback availability a shared transition guard | `labrastro_server/services/capability_package_updates.py`, `labrastro_server/services/admin/service.py`, update/admin/http tests | Rollback is available only when `state.update_state == rollback_available` and rollback metadata contains a valid rollback snapshot or manifest. Empty consumed rollback metadata returns `rollback_not_available`; a second rollback after `update_state=current` must fail or enter an explicit redo transition. |
| C-17 | Unify frontend package state payload projection | `webview-ui/src/settings/capabilityPackageView.ts`, `webview-ui/src/settings/tabs/CapabilitiesTab.tsx`, `webview-ui/src/settings/useSettingsController.tsx`, frontend tests | A single helper builds the frontend state payload from backend package facts: nested `state` plus package-level `credential_state`, `target_facts`, and aliases. CapabilitiesTab fixtures must keep credential and target facts at package level only, so tests fail if UI reads only `item.state`. |
| C-18 | Rebuild evidence closure and invalidate stale completion claims | matrix, implementation plan, GitNexus audit output, server/extension regression commands | Documentation must no longer claim final closure until C-15 through C-17 are fixed. Final evidence must include targeted GitNexus query/impact for the three drift classes, refreshed indexes, final `detect-changes`, grep audits, and affected server/extension tests. |
| C-19 | Centralize install action identity across plan/status/result/stale checks | `labrastro_server/interfaces/http/remote/routes/capability_packages.py`, remote protocol tests | Service code must derive `package_id`, `plan_id`, `action_id`, `component_id`, and expected content/hash aliases through one helper for install actions and peer results. `params.component_id` and content-hash aliases must not create separate keys or stale-check behavior. |
| C-20 | Make local peer runner idempotent against canonical peer status | `src/CapabilityPackageLocalPeerRunner.ts`, runner tests | The local runner must skip actions whose canonical key is `install_state=installed` and `check_state` is empty or `passed`. Repeated polling or `runOnce` must not rerun `install_python_packages` or resubmit identical install results unless content/status changed. |
| C-21 | Gate local peer runner by authenticated ready generation | `src/LabrastroController.ts`, controller admin tests | Runner start requires `authenticated=true`, `status=ready`, `peerConnected=true`, and a stable host/account/device/peer generation key. Logout, unauthenticated connection, host change, account change, device change, or peer change must stop the previous runner before any new run. |
| C-22 | Rebuild local-peer execution evidence closure | matrix, implementation plan, GitNexus audit output, server/extension regression commands | Final evidence must include failing-before and passing-after tests for C-19 through C-21, GitNexus query/detect output on both worktrees, grep audits for residual hand-built keys and `peerConnected`-only runner start, broad capability-package server regression, affected extension tests, typecheck, and `git diff --check`. |

## 16. Execution Order

The implementation is staged, but not as a "minimal viable loop." Each stage must introduce the target architecture boundary it owns.

```text
Stage 1: domain model and state axes
Stage 2: source snapshot and canonical skill bundle
Stage 3: normalized manifest and unmapped findings
Stage 4: dependency graph and isolation rules
Stage 5: typed InstallPlan and server executor
Stage 6: local peer executor protocol
Stage 7: credentials and multi-tenant bindings
Stage 8: activation/runtime projection
Stage 9: update candidate and manifest diff
Stage 10: frontend management surfaces
Stage 11: regression matrix closure
Stage 12: post-review convergence state machine and propagation closure
Stage 13: active drift-convergence closure with GitNexus protocol
Stage 14: reopened drift correction and evidence reclosure
Stage 15: local-peer install execution contract reclosure
```

No stage may add a compatibility shortcut that contradicts later stages.

## 17. Prohibited Shortcuts

- Do not execute LLM-provided shell commands directly.
- Do not install Python/npm dependencies globally by default.
- Do not write only `SKILL.md` for package-managed skills.
- Do not use un-namespaced skill ids as package-managed ids.
- Do not treat config write as installed or verified.
- Do not infer local peer state from server config.
- Do not expose full snapshots to runtime agents.
- Do not make upstream update activation automatic.
- Do not store actual secrets in capability package config.
- Do not hide package install failures inside raw logs only.
- Do not mark a row complete without tests or runtime evidence.
- Do not display internal `unsupported` or `mapping_required` states as product-facing dead ends.
- Do not let package-managed child resources become active from their own local `enabled=true` while the parent package is inactive.
- Do not key target facts only by `action_id`.
- Do not treat a non-empty object as evidence that a diff has meaningful changes.
- Do not leave stale rollback metadata behind while reporting `update_state=current`.
- Do not allow id prefixes such as `envreq:*` to pass through with mismatched component kinds.
- Do not directly use `package.enabled` as runtime activation truth outside compatibility serialization.
- Do not implement target status as duplicated labels derived from one package-level runtime/check field.
- Do not make manifest diff ignore install plans, activation rules, file closures, credentials, or same-count content changes.
- Do not parse `id`, `action_id`, `requirement_id`, or source snapshot aliases differently in different protocol entry points.
- Do not put package-level backend facts inside nested frontend `state` fixtures just to make UI tests pass.
- Do not claim final audit closure when GitNexus or grep evidence still contradicts the claim.
- Do not start the local peer install runner from `peerConnected` alone.
- Do not re-run `installed/passed` local-peer install actions during polling when the canonical action identity and expected content hash are unchanged.

## 18. Acceptance Gate

This architecture is not complete until all rows below are green.

| Gate | Requirement | Evidence |
| --- | --- | --- |
| G-01 | Waza can be installed without whole-package failure from `python-pkg` findings. | `test_waza_like_multi_skill_repo_keeps_skills_when_python_packages_are_unmapped` in Task 13 server run keeps 8 skills and stores `readability-lxml`/`html2text` as an open finding instead of rejecting the package. |
| G-02 | Package-managed skills preserve relative references/scripts/rules. | `test_waza_like_repo_keeps_eight_skill_file_closures_controlled` and artifact closure regression in Task 13 server run. |
| G-03 | Unknown dependency blocks only dependent components. | `test_waza_unresolved_python_packages_block_only_dependent_skills` in Task 13 server run. |
| G-04 | Install and activation are visibly separate. | Task 7 backend/http tests plus Task 12 frontend state labels. |
| G-05 | Server and peer states are independently sourced. | Task 8 server/extension protocol tests for install plan/result and stale peer result handling. |
| G-06 | MCP activation and runtime health are separate. | Task 10 lifecycle/runtime tests preserve `activation_state=active` with `runtime_state=failed`. |
| G-07 | Credentials support user/workspace/global scope and actor audit. | Task 9 credential service and frontend projection tests prove scoped resolution and no secret leakage. |
| G-08 | Update follows upstream version and snapshot tracking. | Task 11 update candidate tests cover `main@commit`, deterministic diff, rollback snapshot, and no auto-activation. |
| G-09 | LLM cannot extend schema. | Task 3 and Task 13 normalizer tests convert `python_package` and `envreq:python-pkg:*` to unmapped findings. |
| G-10 | No old path can write free-form LLM draft directly into config. | Task 3 draft field allowlist/normalizer tests plus Task 7 accept/install tests keep install inactive and validated. |
| G-11 | GitNexus-identified server and extension impact boundaries are covered. | Worktree `detect-changes` in Task 13: server impact concentrated in capability package/admin/remote/config flows; extension impact concentrated in remote client/protocol/CapabilitiesTab/SessionTurn. |
| G-12 | UI never exposes internal mapping dead ends as product state. | Task 12 frontend tests assert no raw `mapping_required`, no "等待开发者", and manual-step labels show concrete user actions. |
| G-13 | Package lifecycle writes use the shared state machine/projection boundary. | `package_managed_component_enabled` now projects component/resource availability from package owners; install, delete, enable/disable, and manifest sync paths use owner projection before materializing child resources. Focused admin tests pass. |
| G-14 | Installing a package does not activate package-managed child resources. | `test_accept_capability_package_installs_without_activation` proves accepted package component and materialized skill remain disabled until `enable_capability_package(... enabled=True)`. |
| G-15 | Activation and deactivation propagate through ownership, including shared components. | `test_disable_capability_package_keeps_shared_component_active_for_other_owner` proves disabling one owner keeps a shared component active for another active owner, then disables it when all owners are inactive. |
| G-16 | Component hooks cannot bypass inactive package ownership. | `test_lifecycle_registry_gates_package_component_hooks_by_owner_activation` proves component hooks under inactive package owners are visible but non-executable with `owner_activation:inactive`. |
| G-17 | Local peer facts cannot collide across packages/plans/actions. | Peer status keys now use `package_id|plan_id|action_id|component_id`; `test_capability_package_peer_results_do_not_collide_on_action_id` proves duplicate `action_id` across packages does not leak install state. |
| G-18 | Update check, apply, and rollback have deterministic state transitions. | `test_check_capability_package_update_does_not_report_no_diff_candidate` proves no-diff candidates are not updates; `test_rollback_update_candidate_clears_consumed_rollback_metadata` proves rollback metadata is consumed. |
| G-19 | Manifest normalization enforces prefix/kind invariants. | `test_component_id_prefix_must_match_backend_kind` proves `envreq:*`, `skill:*`, and `mcp_server:*` mismatches enter unmapped findings instead of manifest components. |
| G-20 | Post-review convergence is evidenced in matrix and impact scan. | Evidence log records failing-before causes, passing focused tests, server service regression `149 passed`, HTTP peer/update `3 passed`, extension affected tests `117 passed`, typecheck PASS, and GitNexus worktree impact for server/extension. |
| G-21 | Runtime activation truth is centralized. | GitNexus context for `resolve_capability_refs` and `_package_managed_requirement_available` is covered by tests proving inactive/blocked package owners do not enter runtime overlay or environment manifest. Grep proves no runtime active decision reads only `package.enabled`. |
| G-22 | Component/resource convergence is centralized. | Update apply/rollback/delete/enable tests prove owner removal and shared inactive owners cannot leave stale enabled materialized resources. GitNexus shows all relevant admin paths call the shared convergence helper. |
| G-23 | Config/file transaction policy is consistent. | Failure tests prove config commit/reload failure and SKILL file operation failure do not leave contradictory persisted config and filesystem state. |
| G-24 | Manifest diff is full and stable. | Tests cover same-count content changes and changes to install plans, activation rules, exposed file closures, dependencies, credentials, and package/update metadata. |
| G-25 | Protocol and credential aliases are normalized once. | C-13 tests prove `id`/`action_id`, `id`/`requirement_id`, `secret_ref`/`credential_ref_id`, source snapshot aliases, and update payload aliases round-trip through typed models/normalizers; grep shows no route-local alias parsing remains for these protocol fields. |
| G-26 | Frontend state uses backend facts. | Historical C-14 evidence is superseded by the follow-up audit, and C-17/G-29 reclose the gap: `CapabilitiesTab` now builds its state payload through `capabilityPackageStatePayload(item)`, which merges nested `state` with package-level `credential_state`, `target_facts`, and aliases. |
| G-27 | Update lifecycle activation truth is centralized. | C-15 tests prove apply and rollback preserve prior activation only when shared projection says the package was active and activation approval is present. Grep/GitNexus prove `capability_package_updates.py` does not read raw `enabled` as activation truth. |
| G-28 | Rollback availability is explicit and repeat-safe. | C-16 tests prove empty `{}` rollback metadata and second rollback attempts return `rollback_not_available`, while valid rollback remains guarded by explicit `state.update_state == rollback_available` plus rollback snapshot/manifest metadata. |
| G-29 | Frontend package state payload has one owner. | C-17 tests keep `credential_state` and `target_facts` at package top level only and still render credential and server/local-peer labels correctly through the shared frontend helper. `useSettingsController` does not expose the same state payload, so it is not given a second divergent view-model path. |
| G-30 | Evidence closure matches current code. | C-18 updates this matrix and the implementation plan with fresh GitNexus query/impact/detect output, grep audits, and affected regression commands. Stale "final audit" rows remain historical baseline only and cannot override current C-15 through C-18 evidence. |
| G-31 | Install action identity is canonical. | C-19 tests prove a peer result using `params.component_id` matches the server action key and stale/hash comparison uses the same identity helper for action and result. Grep proves there is no remaining route-local `params.component_id` key construction or hand-built `package|plan|action|component` path outside the helper. |
| G-32 | Local peer action execution is idempotent. | C-20 tests prove already `installed/passed` actions are skipped and repeated `runOnce` after server peer status update does not call `install_python_packages` or submit duplicate results. |
| G-33 | Local peer runner authorization is explicit. | C-21 tests prove unauthenticated `peerConnected` does not start the runner, authenticated ready state does start it, and account/generation changes stop the old runner before a new one starts. |
| G-34 | Local-peer execution closure matches current code. | C-22 evidence includes GitNexus before/after query/detect, grep residual audit, targeted red/green tests, broad server capability-package regression, affected extension tests, typecheck, and `git diff --check`. |

## 19. Immediate Next Steps

1. Treat the previous 13-task implementation as baseline evidence; closure must cite the full C-01 through C-22 evidence set.
2. Keep every future code change mapped to a C row and a G gate before implementation.
3. Update this matrix after each C row with the real failing-before and passing-after evidence.
4. Treat any missing or stale G-13 through G-34 evidence as blocking completion, even if older G-01 through G-12 evidence still passes.
5. Re-run GitNexus `analyze`, GitNexus `detect-changes`, targeted tests, regression sets, grep audits, and all matrix acceptance gates before claiming any new completion state.

# Taskflow Engineering Method Projection Design

Version: 2026-06-06

## 1. Positioning

This document settles how Taskflow should present and implement software
engineering methods such as PRD, SBE, BDD, DDD, SDD, TDD, ADR, Tech Spec, API
Spec, and Runbook.

The core decision is:

```text
Taskflow is not a document generator and not a method template gallery.
Taskflow is a goal compiler.

Engineering methods enter Taskflow as recipes, gates, state writes, and
artifact projectors.
```

The user should not experience Taskflow as:

```text
Please fill PRD.
Please fill DDD.
Please write Gherkin.
Please choose TDD.
```

The user should experience Taskflow as:

```text
I understood the project shape.
I identified the few decisions that affect this change.
Here are my recommended answers and why.
Confirm these points, then I can compile executable work.
```

## 2. Existing Baseline

Current Taskflow already has the right lower-level objects:

- `ProjectState`: long-lived project memory, terms, decisions, constraints,
  WorkItems, TraceLinks, projections.
- `TaskflowState`: one Goal's clarification and compile state.
- `PlanCompiler`: compiles TaskflowState and ProjectState into traceable
  WorkItems and links.
- `ComplexityAssessmentService`: chooses risk level and required artifacts.
- `ProjectMemoryService`: lets users patch long-lived project memory.
- `ProjectorPreviewService`: read-only preview for future artifact export.
- `TaskflowView`: visible workspace with Discovery, Project Memory, Compiler
  Review, Dispatch/Runtime, Trace, Projectors.

The gap is above those objects:

```text
Code/docs/tests
  -> project engineering projection
  -> method recipe selection
  -> context-aware question planning
  -> artifact projections
```

Current implementation has storage and control-plane pieces, but not the
complete engineering-method projection pipeline.

## 3. Final Chain

### 3.0 Workspace Background Understanding

Project understanding should not wait until the user starts asking for a
Taskflow.

Project understanding can require substantial code/doc/test/config scanning,
summarization, indexing, token usage, and stale detection. If this work runs
synchronously at Taskflow start, the product experience will feel slow and
unpredictable.

Therefore, project understanding is a workspace-level background capability:

```text
Workspace opened / repo switched / files changed
  -> Background Project Understanding Runtime
  -> Evidence Collector
  -> ProjectProjectionV1
  -> Projection Cache
  -> Project Memory Patch Proposal
```

Taskflow consumes the latest snapshot:

```text
User starts Taskflow
  -> load latest ProjectProjectionV1 snapshot
  -> check staleness
  -> optionally request a goal-scoped incremental refresh
  -> enter Method Router and Question Planner
```

Boundary:

```text
Project Understanding is the workspace background layer.
Taskflow is the goal compiler layer.
They connect through ProjectProjectionV1 snapshots.
```

For an existing project:

```text
Workspace background understanding
  -> ProjectProjectionV1 snapshot
  -> User goal
  -> Method Router
  -> Question Planner
  -> TaskflowState
  -> Brief / PlanCompiler
  -> WorkItem + TestObligation + TraceLink
  -> Artifact Projectors
  -> Dispatch Contract
  -> AgentRun runtime
```

For a new project:

```text
User goal
  -> Method Router
  -> Question Planner
  -> TaskflowState
  -> Brief / PlanCompiler
  -> WorkItem + TestObligation + TraceLink
  -> Artifact Projectors
  -> Dispatch Contract
```

The difference is that existing projects depend on the workspace background
Project Projection. New projects can route directly from the user goal to
method selection and questioning.

## 4. Architecture

### 4.0 Background Project Understanding Runtime

Purpose:

```text
Maintain project understanding silently and incrementally after the workspace
is loaded.
```

This is not a Taskflow submodule. It is a workspace-level service.

Triggers:

- workspace opened
- repository switched
- branch switched
- config changed
- important code files changed
- tests/docs/migrations/workflows changed
- user manually refreshes project understanding

Outputs:

```text
ProjectProjectionV1 snapshot
ProjectEvidenceRecord index
staleness report
confidence summary
Project Memory patch proposals
```

Requirements:

- run in the background
- do not block user input
- support incremental refresh
- support cancel and resume
- obey token and time budgets
- expose stale status
- never auto-write ProjectState

Suggested states:

```text
idle
scanning
summarizing
ready
stale
failed
disabled
```

Taskflow API shape:

```text
get_latest_projection(project_id, workspace_id)
get_projection_status(project_id, workspace_id)
request_incremental_refresh(project_id, goal_hint)
```

### 4.0.1 Refresh Trigger Policy

Project understanding updates use:

```text
incremental invalidation + layered refresh
```

Do not rescan the whole project after every code change.

Recommended flow:

```text
File changed
  -> classify impact
  -> mark affected projection sections stale
  -> run cheap structural scan immediately
  -> queue expensive semantic refresh in the background
  -> generate a new ProjectProjectionV1 snapshot
  -> if durable memory should change, emit a Project Memory patch proposal
```

Trigger categories:

| Trigger | Behavior |
|---|---|
| workspace opened / repo switched / branch switched | start background baseline scan |
| file save / git change / watcher event | debounce and run incremental update |
| user starts Taskflow | load existing snapshot; if stale, request goal-scoped refresh |
| user manually refreshes understanding | allow forced rescan or projection rebuild |

Change impact levels:

| Impact | Examples | Behavior |
|---|---|---|
| low | README, comments, small style changes | update evidence hash; usually no full projection recompute |
| medium | component, service, tests, config changes | update related module/interface/test sections |
| high | routes, APIs, schemas, migrations, workflows, runtime config | mark interface/data/runtime/delivery projection stale |
| critical | ProjectState schema, TaskflowState schema, AgentRun boundary, dispatch contract | mark architecture_principles / module_boundaries / system_map stale |

### 4.0.2 Section-Level Staleness

Do not use only one global flag:

```text
project_projection_stale = true
```

Track freshness per section:

```text
system_map: fresh
module_boundaries: stale
domain_language: fresh
architecture_principles: stale
interface_surfaces: stale
data_surfaces: fresh
runtime_surfaces: stale
test_surfaces: partial
delivery_ops: fresh
ui_conventions: fresh
risk_hotspots: partial
```

Suggested metadata:

```text
ProjectionSectionStatus
- section_id
- status: fresh | partial | stale | unknown
- last_refreshed_at
- source_hash
- changed_paths
- stale_reason
- confidence
```

This lets Taskflow keep using fresh sections while avoiding strong
recommendations from stale inference.

### 4.0.3 Two-Level Refresh

Background refresh has two levels:

```text
Level 1: cheap scan
  file paths, hashes, exports, routes, manifests, tests, migrations, workflows.
  no LLM call; fast.

Level 2: semantic refresh
  module responsibility, architecture principles, domain language, risk inference.
  token-using; queued; budgeted; cancellable.
```

Scheduling rules:

```text
cheap scan may run often;
semantic refresh must debounce, merge changes, and rate-limit;
when the user is typing or AgentRun is active, semantic refresh lowers priority;
when the user opens Taskflow or Project Understanding, relevant refresh gets priority.
```

### 4.0.4 Taskflow Stale Behavior

Taskflow degrades based on snapshot staleness:

```text
snapshot fresh:
  use directly for Method Router and Question Planner.

snapshot partially stale:
  continue using it;
  show "based on partially stale project understanding" on question cards;
  request goal-scoped refresh.

snapshot severely stale:
  do not block user input;
  do not make strong architecture recommendations;
  ask only low-risk clarification questions;
  wait for background refresh before design recommendations.

snapshot missing:
  Taskflow can start;
  show "project understanding is still being built";
  use only the user goal and existing ProjectMemory.
```

This guarantees:

```text
project understanding does not block the user;
stale understanding does not mislead the user;
Taskflow does not own full-project scanning;
when background understanding completes, question recommendations can refresh.
```

### 4.1 Evidence Collector

Purpose:

```text
Read project sources and create traceable evidence.
```

Inputs:

- code files
- tests
- docs
- package manifests
- routes
- database migrations
- CI workflows
- runtime config
- existing ProjectState memory

Outputs:

```text
ProjectEvidenceRecord
- id
- source_type
- source_path
- symbol_or_section
- statement
- confidence
- extracted_at
- content_hash
```

Current `RepoStaticAnalyzer` can seed this layer, but it only produces
complexity evidence. It should be extended or wrapped by a broader collector.

### 4.2 Project Projection

Purpose:

```text
Turn raw evidence into a project engineering projection.
```

Proposed object:

```text
ProjectProjectionV1
- projection_id
- project_id
- schema_version
- source_snapshot_hash
- generated_at
- confidence
- stale_reason
- evidence_refs
- sections
```

Sections:

```text
system_map
module_boundaries
domain_language
architecture_principles
interface_surfaces
data_surfaces
runtime_surfaces
test_surfaces
delivery_ops
ui_conventions
risk_hotspots
```

Each section contains findings:

```text
ProjectionFinding
- id
- kind
- statement
- evidence_refs
- source_paths
- confidence
- scope
- method_tags
- confirmation_state
```

Important distinction:

```text
Observed Fact
  A code/doc/test fact directly supported by evidence.

Inferred Principle
  A likely project rule inferred from several facts.

Confirmed Project Memory
  A fact or principle accepted by the user and stored in ProjectState.
```

Only confirmed project memory should become durable project truth. Projection
findings remain reviewable and stale-able.

Project Projection lifecycle is owned by the Background Project Understanding
Runtime, not by a single Taskflow session.

### 4.3 Engineering Method Registry

Purpose:

```text
Represent PRD, BDD, DDD, SDD, TDD, ADR, Tech Spec, API Spec, and Runbook as
activation recipes rather than top-level state modules.
```

Proposed object:

```text
EngineeringMethodRecipe
- id
- label
- intent
- trigger_conditions
- required_state_sections
- question_packs
- readiness_gates
- artifact_projectors
- compiler_rules
- default_visibility
```

Examples:

```text
prd-guided
bdd-sbe-guided
ddd-guided
sdd-openspec
sdd-speckit
tdd-obligation
adr-guided
tech-spec-guided
api-spec-guided
runbook-guided
```

### 4.3.1 Guided / Full / Automation Layers

The initial recipes should not use `lite` as the capability label.

`lite` sounds like a reduced methodology. That is not the intended model.
Taskflow should provide full engineering-method capability, but expose it in
layers so ordinary users are not forced to operate a full method framework.

Use three capability layers:

```text
guided
```

User-visible low-friction guidance.
It asks only the questions needed for the current goal and presents brief
cards, decision options, examples, and readiness gates.

```text
full
```

Complete method artifact generation.
For example, full PRD, full Tech Spec, full ADR, or full DDD modeling notes.

The LLM can generate these full artifacts, but only from confirmed state,
project evidence, and explicit user decisions.

```text
automation
```

Executable validation and integration.
For example, BDD automation, Gherkin/Cucumber, OpenAPI checks, CI gates,
test-runner integration, or runbook verification.

This means:

```text
The model can write a complete PRD / DDD / Tech Spec.
The user should not have to fill a PRD / DDD / Tech Spec form manually.
Taskflow must still retain structured source state, traceability, and review gates.
```

Do not let the LLM generate a complete-looking PRD / DDD / Tech Spec directly
from a prompt without evidence and confirmation flow.

Recommended naming:

```text
prd-guided
prd-full

bdd-sbe-guided
bdd-contract
bdd-automation

ddd-guided
ddd-modeling
ddd-full-workshop

tdd-obligation
tdd-plan
tdd-automation

tech-spec-guided
tech-spec-full

runbook-guided
runbook-full
```

Default behavior:

```text
guided is the default interaction layer.
full is the complete artifact generation layer.
automation is the execution and verification integration layer.
```

The Method Router can upgrade by complexity, risk, user intent, and project
readiness:

```text
User explicitly requests full PRD -> enable prd-full
Interface contract is high-risk -> enable api-spec-guided, then api-spec-full if needed
Acceptance needs automation -> upgrade bdd-sbe-guided to bdd-automation
Domain concepts conflict -> upgrade ddd-guided to ddd-modeling
```

### 4.4 Method Router

Purpose:

```text
Choose which recipes apply to the current goal.
```

Inputs:

- user goal
- ProjectProjectionV1
- ComplexityEstimate
- existing ProjectState
- current TaskflowState

Outputs:

```text
SelectedMethodPlan
- selected_recipe_ids
- skipped_recipe_ids
- rationale
- required_questions
- required_artifacts
- compile_gates
- dispatch_gates
```

Default policy:

```text
L0: scope + acceptance only
L1: prd-guided + acceptance + tdd-obligation
L2: prd-guided + bdd-sbe-guided + tech-spec-guided
L3: prd-guided + bdd-sbe-guided + adr-guided + tech-spec-guided + api/runbook if needed
L4: governance/roadmap mode, with explicit multi-phase planning
```

### 4.5 Question Planner

Purpose:

```text
Turn selected recipes and project projection into a small set of high-value
questions.
```

Outputs write into existing TaskflowState:

- `OpenQuestion`
- `Assumption`
- `DecisionRecord`
- `RuleRecord`
- `ScenarioRecord`
- `AcceptanceExample`
- `WorkItemCandidate`

Each question must include:

```text
question
why_needed
default_suggestion
options
risk_if_unknown
blocks_compile
blocks_dispatch
source_refs
field_bindings
method_tags
```

User-facing rule:

```text
Ask only questions that change scope, acceptance, architecture, tests,
delivery risk, or dispatch safety.
```

### 4.6 Artifact Projectors

Purpose:

```text
Generate documents and external specs from state without making documents the
source of truth.
```

Projectors:

```text
PrdLiteProjector
BddExamplesProjector
GherkinProjector
DddLiteProjector
AdrProjector
TechSpecProjector
ApiSpecProjector
RunbookProjector
OpenSpecProjector
SpecKitProjector
NativeBriefProjector
```

Projector rule:

```text
Projectors are output-only. They must not rewrite ProjectState or
TaskflowState.
```

Artifacts should carry:

```text
artifact_id
artifact_type
schema_version
source_taskflow_id
source_brief_version
source_state_version
source_refs
content_hash
```

### 4.7 Test Obligation Layer

Purpose:

```text
Make TDD visible as validation obligations, not as a test framework takeover.
```

Proposed object:

```text
TestObligationV1
- id
- work_item_id
- source_acceptance_refs
- test_level
- suggested_test_files
- suggested_commands
- observable_output
- required_before_dispatch
- required_before_done
```

This can initially live in WorkItem metadata or artifact projection, then move
to a first-class model once validated.

## 5. Method Mapping

| Method / Artifact | Role in Taskflow | State Writes | User View |
|---|---|---|---|
| PRD guided/full | Product intent and scope; generate full PRD when needed | goal, stakeholders, scope, success criteria, rules | brief card + PRD preview, not a form |
| SBE | Rules and examples | rules, examples, acceptance_examples | example cards |
| BDD | Shared behavior understanding | scenarios, observable outputs, acceptance refs | behavior questions and scenario preview |
| Gherkin | Optional executable expression | artifact projection only | advanced preview |
| DDD guided/modeling | Project/domain understanding; expand into full modeling when needed | bounded_context_refs, ubiquitous_language, domain_model_delta | terminology and boundary cards |
| SDD | Spec-driven artifact export | projector output | OpenSpec/Spec Kit preview |
| TDD | Validation duty | test_suggestions, TestObligation, DoD | test obligation card |
| ADR | Durable decision explanation | local_decisions, project decisions | decision card and ADR preview |
| RFC / Design Doc | Compare solution options | solution_options, local_decisions, risks | tradeoff card |
| Tech Spec | Implementation guidance | interfaces, risks, implementation_notes | technical plan preview |
| HLD / LLD | Architecture/detail split | architecture principles, module boundaries, implementation notes | architecture preview |
| API Spec | Interface contract | InterfaceSpec | API contract preview |
| Runbook | Delivery and operations | rollout_plan, ops risks, rollback/monitoring | delivery readiness card |

## 6. User Experience

### 6.1 Product Feel

The user should see a guided engineering workspace, not a raw schema editor.

The primary surfaces should be:

```text
Project Understanding
Decisions To Confirm
Plan Brief
Readiness
Artifacts
Dispatch
Trace
```

Avoid exposing method names as first-class navigation unless the user opens
details. The main UI should not say:

```text
DDD module
BDD module
TDD module
PRD module
```

It should say:

```text
Project terms
Behavior examples
Validation
Architecture decisions
Delivery notes
```

### 6.2 First Interaction

When the user starts a Taskflow on an existing project:

```text
I loaded the background project-understanding snapshot and will check whether
it needs a goal-scoped refresh.
```

Then Taskflow should present:

```text
I found:
- this touches TaskflowView and workspace projection
- dispatch data currently comes from TaskRun metadata
- existing design says Taskflow is the compiler, AgentRun is runtime

I need you to confirm 2 decisions:
1. Should the draft be user-facing or audit-only?
2. Should dispatch be blocked if the draft is incomplete?
```

The visible experience is:

```text
less form filling
more reasoned confirmation
low question count
clear default recommendation
traceable evidence
visible progress
```

### 6.3 Review Card Shape

Every important card should have:

```text
Title
Recommendation
Why this matters
Evidence
Tradeoffs
Actions: Accept / Edit / Discuss / Skip
Impact: scope / architecture / tests / dispatch
```

Cards are the product surface where software engineering methods become
usable. The user does not need to know whether a card came from BDD, DDD, or
ADR. The card can show method tags only in details.

### 6.4 Artifact Preview

Artifacts are secondary projections:

```text
PRD guided / full
Behavior examples
ADR
Tech Spec
API Spec
Runbook
OpenSpec
Spec Kit
```

Default state:

```text
preview only
read only
generated from confirmed state
not editable as truth source
```

If users edit an artifact, the edit should become a proposed ProjectState or
TaskflowState patch, not direct document mutation.

## 7. Implementation Plan

### Phase 0: Contract Cleanup

Deliverables:

- add this design to docs
- define `EngineeringMethodRecipe` contract
- define `ProjectProjectionV1` contract
- define artifact projector contract
- document method-to-state mapping

No runtime behavior change is required.

### Phase 1: Project Projection V1

Deliverables:

- `BackgroundProjectUnderstandingRuntime`
- `ProjectProjectionService`
- `ProjectEvidenceRecord`
- `ProjectProjectionV1`
- repo/doc/test/config evidence collection
- projection stale detection
- projection cache
- silent startup after workspace load
- projection view in Taskflow workspace

Acceptance:

```text
Given an opened workspace,
the system silently generates a project-understanding snapshot before the user
starts a Taskflow.

Given an existing repo and goal,
Taskflow can load the snapshot and show project facts, inferred principles,
source refs, and confidence before asking questions.
```

### Phase 2: Method Router and Question Planner

Deliverables:

- `EngineeringMethodRegistry`
- `MethodRouter`
- `QuestionPlanner`
- recipe-driven question packs
- selected method plan in workspace DTO

Acceptance:

```text
Given a goal touching API and data migration,
Taskflow selects prd-guided, bdd-sbe-guided, tech-spec-guided,
api-spec-guided, runbook-guided, and tdd-obligation.

The user sees 1-5 decision cards, each with evidence and a recommendation.
```

### Phase 3: Artifact Projectors

Deliverables:

- native brief projector
- PRD guided / full projector
- ADR projector
- BDD examples/Gherkin projector
- Tech Spec projector
- OpenSpec / Spec Kit preview projector

Acceptance:

```text
Artifacts are generated from confirmed TaskflowState and ProjectState.
Artifact edits never directly rewrite the source state.
```

### Phase 4: Test Obligation and Readiness

Deliverables:

- `TestObligationV1`
- test obligation cards
- readiness gates tied to test obligations
- trace links from acceptance examples to WorkItems and tests

Acceptance:

```text
Every dispatchable WorkItem has acceptance refs and a visible validation duty.
```

### Phase 5: UX Finalization

Deliverables:

- TaskflowView section rename and information hierarchy
- Project Understanding panel
- Decision Cards panel
- Plan Brief panel
- Artifact Preview panel
- Readiness/Dispatch panel

Acceptance:

```text
The user can complete a real existing-project Taskflow without reading raw
JSON, internal state names, or full PRD/DDD/BDD forms.
```

## 8. UI Information Architecture

Recommended Taskflow workspace layout:

```text
Header
  Goal
  Status
  Confidence
  Readiness
  Runs

Project Understanding
  recognized modules
  project principles
  terminology
  affected surfaces
  evidence refs

Decisions
  questions
  assumptions
  recommendations
  tradeoffs

Plan
  brief
  scope
  WorkItems
  acceptance
  test obligations

Artifacts
  PRD guided / full
  behavior examples
  ADR
  Tech Spec
  API Spec
  Runbook
  OpenSpec / Spec Kit

Dispatch
  dispatch contract
  readiness gates
  TaskRuns / AgentRuns

Trace
  goal -> decision -> example -> WorkItem -> TaskRun -> artifact
```

This is the visible product shape. It should feel like a workbench for
confirming engineering decisions, not a document wizard.

## 9. Hard Boundaries

1. Taskflow remains the design compiler.
2. AgentRun remains the runtime orchestrator.
3. Project Understanding is a workspace background layer, not a synchronous Taskflow step.
4. Project Projection remains reviewable inference, not durable truth.
5. Confirmed Project Memory is durable truth.
6. Engineering methods are recipes and projectors, not top-level state modules.
7. Documents are generated views, not the source of truth.
8. Projectors never mutate state directly.
9. Gherkin is optional output, not the user's required input language.
10. TDD is represented as validation obligations, not as a test runner.
11. DDD starts as bounded context, language, and decision memory, not as a full ontology.

## 10. Initial Native Recipes

### 10.1 `prd-guided` / `prd-full`

Triggers:

- user-facing feature
- product value unclear
- business scope involved

Writes:

- goal statement
- stakeholders
- scope in/out
- success criteria
- business rules

Projector:

- `prd-guided`: brief / scope / success criteria preview
- `prd-full`: complete PRD preview generated from confirmed state

### 10.2 `bdd-sbe-guided` / `bdd-contract` / `bdd-automation`

Triggers:

- business behavior
- state transitions
- role differences
- acceptance ambiguity

Writes:

- rules
- examples
- scenarios
- acceptance examples
- open questions

Projector:

- `bdd-sbe-guided`: example table
- `bdd-contract`: behavior contract preview
- optional Gherkin
- `bdd-automation`: Cucumber / CI / executable acceptance integration

### 10.3 `ddd-guided` / `ddd-modeling` / `ddd-full-workshop`

Triggers:

- term ambiguity
- domain concept conflict
- multiple bounded contexts
- complex lifecycle

Writes:

- ubiquitous language
- bounded context refs
- domain model delta
- project decisions

Projector:

- `ddd-guided`: domain glossary / boundary preview
- `ddd-modeling`: bounded context / aggregate / lifecycle model preview
- `ddd-full-workshop`: event storming / context map / full modeling output

### 10.4 `sdd-openspec`

Triggers:

- user wants repo-native spec workflow
- project already uses OpenSpec-style files
- team wants spec delta review

Writes:

- no direct source writes

Projector:

- proposal/design/tasks/spec delta preview

### 10.5 `tdd-obligation`

Triggers:

- WorkItem is dispatchable
- acceptance examples exist
- risky behavior needs proof

Writes:

- test suggestions
- test obligations
- validation gates

Projector:

- test plan / validation checklist

### 10.6 `adr-guided` / `adr-full`

Triggers:

- meaningful solution choice
- future maintainers may challenge the decision
- rejected options matter

Writes:

- DecisionRecord
- rationale
- rejected options
- TraceLinks

Projector:

- `adr-guided`: decision card / rationale preview
- `adr-full`: complete ADR preview

## 11. Success Criteria

This area is ready when all of the following are true:

1. Workspace load starts project understanding silently before user questions.
2. A goal on an existing project loads a ProjectProjectionV1 snapshot before questioning.
3. Questions cite source evidence and explain why they matter.
4. The system selects methods through recipes, not hardcoded prompt vibes.
5. BDD/DDD/SDD/TDD/PRD artifacts are visible as projections, not raw forms.
6. WorkItems carry acceptance refs and validation obligations.
7. Dispatch is blocked by readiness gates, not by document completeness.
8. Users can accept/edit/reopen key decisions without editing JSON.
9. Artifacts can be regenerated from state.
10. Project memory updates require explicit confirmation.
11. Runtime execution remains owned by AgentRun.

## 12. Final Decision

Build this as a projection and recipe layer:

```text
BackgroundProjectUnderstandingRuntime
ProjectProjectionV1
EngineeringMethodRecipe
MethodRouter
QuestionPlanner
ArtifactProjector
TestObligation
```

Do not build it as:

```text
BDD module
DDD module
PRD module
TDD module
document template center
workflow graph engine
```

The final user-facing feel should be:

```text
The system starts understanding my project when I open the workspace,
Taskflow consumes that project-understanding snapshot,
asks only the engineering questions that matter,
shows the reason and evidence,
compiles my answers into executable work,
and can generate PRD/ADR/spec/test/runbook views when useful.
```

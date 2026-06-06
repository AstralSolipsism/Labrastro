# Taskflow 工程方法投影设计

版本：2026-06-06

## 1. 定位

这份文档用于定稿 Taskflow 如何承接这些软件工程方法和规范文档：

```text
PRD
SBE
BDD
DDD
SDD
TDD
ADR
Tech Spec
API Spec
Runbook
```

核心结论：

```text
Taskflow 不是文档生成器。
Taskflow 不是方法论模板库。
Taskflow 是 Goal Compiler，也就是目标编译器。

软件工程方法不应该变成一堆顶层模块，
而应该以 recipe、gate、状态写入、artifact projector 的形式进入系统。
```

用户不应该看到这种体验：

```text
请填写 PRD。
请填写 DDD。
请编写 Gherkin。
请选择是否使用 TDD。
```

用户应该看到这种体验：

```text
我已经理解了当前项目的结构和约束。
我识别出这次改造真正会影响的几个设计点。
下面有几个需要确认的关键决策。
我会给出推荐答案、原因、证据和风险。
你确认后，我再把它编译成可执行工作项。
```

换句话说：

```text
方法论在系统内部生效，
但用户面对的是可理解的工程决策，
不是方法论术语和文档表单。
```

## 2. 当前已有基础

当前 Taskflow 已经有一些正确的底层对象：

| 能力 | 当前对象 | 状态 |
|---|---|---|
| 长期项目状态 | `ProjectState` | 已有，保存术语、决策、约束、WorkItem、TraceLink、Projections |
| 单次目标编译状态 | `TaskflowState` | 已有，保存当前 Goal 的澄清、决策、编译、派发准备 |
| 计划编译 | `PlanCompiler` | 已有，把 TaskflowState + ProjectState 编译成 WorkItem 和 trace |
| 复杂度评估 | `ComplexityAssessmentService` | 已有，能选择风险等级和 required artifacts |
| 项目记忆治理 | `ProjectMemoryService` | 已有，可 patch 术语、决策、约束、WorkItem、TraceLink |
| 工件预览 | `ProjectorPreviewService` | 已有雏形，但目前只是只读元数据预览 |
| 前端工作台 | `TaskflowView` | 已有 Discovery、Project Memory、Compiler Review、Dispatch/Runtime、Trace、Projectors |

当前缺口不在底层对象，而在上层链路：

```text
代码 / 文档 / 测试
  -> 项目工程投影
  -> 方法论 recipe 选择
  -> 基于项目上下文的追问规划
  -> 文档 / 规格 / 测试义务投影
```

也就是说：

```text
现在已经有状态容器和控制面，
但还没有完整的“工程方法投影层”。
```

## 3. 完整链路

### 3.0 工作区后台项目理解

项目理解不应该等用户开始提问后才执行。

原因：

```text
项目理解可能需要读取大量代码、文档、测试、配置；
可能消耗大量 token；
可能需要多轮扫描、摘要、索引、过期检测；
如果绑在用户提问时同步执行，会直接破坏 Taskflow 的交互体验。
```

因此，项目理解必须解耦为工作区级后台能力：

```text
工作区加载 / 仓库切换 / 文件变更
  -> Background Project Understanding Runtime
  -> Evidence Collector
  -> ProjectProjectionV1
  -> Projection Cache
  -> Project Memory Patch Proposal
```

Taskflow 本身只消费这个后台能力产出的快照：

```text
用户发起 Taskflow
  -> 读取最新 ProjectProjectionV1 snapshot
  -> 判断 snapshot 是否 stale
  -> 如有必要只触发目标相关的增量刷新
  -> 进入 Method Router 和 Question Planner
```

也就是说：

```text
Project Understanding 是工作区后台层。
Taskflow 是目标编译层。
二者通过 ProjectProjectionV1 snapshot 连接。
```

### 3.1 已有项目

对于已有项目，完整链路应该是：

```text
工作区加载后的后台项目理解
  -> ProjectProjectionV1 snapshot
  -> 用户目标
  -> Method Router
  -> Question Planner
  -> TaskflowState
  -> Brief / PlanCompiler
  -> WorkItem + TestObligation + TraceLink
  -> Artifact Projectors
  -> Dispatch Contract
  -> AgentRun runtime
```

中文解释：

```text
工作区后台先持续形成项目工程投影，
用户发起目标时 Taskflow 读取最新投影快照，
再判断这次需求应该启用哪些工程方法，
再生成精准问题，
再写入 TaskflowState，
再编译成可执行工作项，
最后才进入 AgentRun 执行。
```

### 3.2 从零项目

对于从零开始的项目，链路可以简化：

```text
用户目标
  -> Method Router
  -> Question Planner
  -> TaskflowState
  -> Brief / PlanCompiler
  -> WorkItem + TestObligation + TraceLink
  -> Artifact Projectors
  -> Dispatch Contract
```

差异在于：

```text
已有项目必须依赖工作区后台 Project Projection。
从零项目可以直接从用户目标进入方法选择和追问。
```

## 4. 架构组件

### 4.0 Background Project Understanding Runtime

职责：

```text
在工作区加载后，持续、静默、增量地维护项目理解。
```

它不是 Taskflow 的子模块，而是工作区级服务。

触发时机：

```text
工作区打开
仓库切换
分支切换
配置文件变化
重要代码文件变化
测试 / 文档 / migration / workflow 变化
用户手动刷新项目理解
```

输出：

```text
ProjectProjectionV1 snapshot
ProjectEvidenceRecord index
staleness report
confidence summary
Project Memory patch proposals
```

关键要求：

```text
后台执行，不阻塞用户输入；
增量刷新，不每次全量扫描；
可取消、可恢复；
有 token / 时间预算；
有 stale 状态；
有用户可见的“项目理解状态”；
不会自动改写 ProjectState。
```

建议状态：

```text
idle
scanning
summarizing
ready
stale
failed
disabled
```

Taskflow 调用方式：

```text
get_latest_projection(project_id, workspace_id)
get_projection_status(project_id, workspace_id)
request_incremental_refresh(project_id, goal_hint)
```

### 4.0.1 Refresh Trigger Policy

项目理解更新采用：

```text
增量失效 + 分层刷新
```

不要在每次代码变化时全量重扫。

推荐流程：

```text
文件变化
  -> 判断影响范围
  -> 标记相关 projection section stale
  -> 低成本结构扫描立即更新
  -> 高成本语义总结排后台队列
  -> 生成新的 ProjectProjectionV1 snapshot
  -> 如需写入长期记忆，只生成 Project Memory patch proposal
```

触发分为四类：

| 触发 | 行为 |
|---|---|
| 工作区打开 / repo 切换 / branch 切换 | 启动后台 baseline scan |
| 文件保存 / git change / watcher event | debounce 后做增量更新 |
| 用户发起 Taskflow | 读取已有 snapshot；若 stale，只触发 goal-scoped refresh |
| 用户手动刷新项目理解 | 允许强制重扫或重建 projection |

文件变化影响等级：

| 变化等级 | 示例 | 行为 |
|---|---|---|
| 低影响 | README、普通注释、样式微调 | 更新 evidence hash，通常不重算整体投影 |
| 中影响 | 组件、服务、测试、配置变化 | 更新相关 module/interface/test section |
| 高影响 | route、API、schema、migration、workflow、runtime config 变化 | 标记 interface/data/runtime/delivery projection stale |
| 关键影响 | ProjectState schema、TaskflowState schema、AgentRun boundary、dispatch contract 变化 | 标记 architecture_principles / module_boundaries / system_map stale |

### 4.0.2 Section-Level Staleness

不要只有一个全局字段：

```text
project_projection_stale = true
```

应该按 section 维护新鲜度：

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

建议 section stale metadata：

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

这样 Taskflow 可以继续使用未过期 section，同时避免把过期推断当成可靠建议。

### 4.0.3 Two-Level Refresh

后台刷新分两层：

```text
Level 1: cheap scan
  文件路径、hash、exports、routes、manifest、tests、migrations、workflow。
  不调用大模型，快速完成。

Level 2: semantic refresh
  模块职责、架构原则、领域语言、风险推断。
  需要 token，进入后台队列，限额执行，可取消。
```

调度规则：

```text
cheap scan 可以频繁运行；
semantic refresh 必须 debounce、合并变更、限流；
用户正在输入或运行 AgentRun 时，semantic refresh 降低优先级；
用户显式打开 Taskflow 或 Project Understanding 面板时，提高相关 refresh 优先级。
```

### 4.0.4 Taskflow Stale Behavior

Taskflow 使用 project snapshot 时，按 stale 程度降级：

```text
snapshot fresh:
  直接用于 Method Router 和 Question Planner。

snapshot partially stale:
  继续可用；
  问题卡片显示“基于部分过期项目理解”；
  同时触发 goal-scoped refresh。

snapshot severely stale:
  不阻塞用户输入；
  不给强架构推荐；
  只问低风险澄清问题；
  等后台刷新后再给设计建议。

snapshot missing:
  Taskflow 可启动；
  但必须提示“项目理解仍在建立中”；
  只能使用用户目标和现有 ProjectMemory。
```

这能保证：

```text
项目理解不会阻塞用户；
过期理解不会误导用户；
Taskflow 不需要承担全量扫描职责；
后台理解完成后可以自动刷新问题建议。
```

### 4.1 Evidence Collector

职责：

```text
读取项目来源，生成可追溯证据。
```

输入：

- 代码文件
- 测试文件
- 文档
- package manifest
- route / API
- 数据库 migration
- CI workflow
- runtime config
- 已有 ProjectState memory

输出：

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

当前 `RepoStaticAnalyzer` 可以作为起点，但它现在主要产出复杂度证据，还不能完整表达项目工程理念。

### 4.2 Project Projection

职责：

```text
把原始证据转成项目工程投影。
```

建议新增对象：

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

建议包含这些 section：

```text
system_map              系统地图
module_boundaries       模块边界
domain_language         领域语言
architecture_principles 架构原则
interface_surfaces      接口面
data_surfaces           数据面
runtime_surfaces        运行面
test_surfaces           测试面
delivery_ops            交付 / 运维
ui_conventions          UI 约定
risk_hotspots           风险热点
```

每条 finding 建议是：

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

这里必须区分三种东西：

```text
Observed Fact
  代码、文档、测试中直接能证明的事实。

Inferred Principle
  系统根据多个事实推断出的项目原则。

Confirmed Project Memory
  用户确认后写入 ProjectState 的长期项目记忆。
```

重要边界：

```text
Project Projection 是可审阅推断，不是长期真相。
Project Memory 才是被用户确认后的长期真相。
```

Project Projection 的生命周期由 Background Project Understanding Runtime
管理，而不是由单个 Taskflow 会话管理。

### 4.3 Engineering Method Registry

职责：

```text
把 PRD、BDD、DDD、SDD、TDD、ADR、Tech Spec、API Spec、Runbook
表达成可激活的工程 recipe。
```

建议对象：

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

初始 recipe 可以包括：

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

### 4.3.1 Guided / Full / Automation 分层

工程方法不是只做简化版。完整方法论能力必须保留。

但完整方法论不应该作为默认前门压给用户。

推荐分三层：

```text
guided
  用户可见的低负担引导形态。
  用简单问题、推荐答案、决策卡片帮助用户补齐目标、范围、规则、风险。

full
  大模型基于已确认状态、项目证据和用户回答，生成完整方法论文档或设计内容。
  例如完整 PRD、完整 Tech Spec、完整 ADR、完整 DDD 建模说明。

automation
  进一步接入自动化执行、测试框架、CI、Gherkin/Cucumber、OpenAPI 校验等。
```

这不是能力阉割，而是渐进式能力激活：

```text
用户入口保持简单；
系统内部保留完整方法论；
输出层按需展开完整文档；
自动化层只在足够确定时启用。
```

完整内容可以由大模型生成，但必须满足三个前提：

```text
基于结构化 TaskflowState；
基于 ProjectProjectionV1 / ProjectState 证据；
基于用户确认过的关键决策。
```

不能让大模型直接凭 prompt 生成一份看似完整、但没有证据和确认链路的 PRD / DDD / Tech Spec。

推荐命名：

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

其中：

```text
guided 是默认交互层；
full 是完整工件生成层；
automation 是执行/验证接入层。
```

Method Router 根据复杂度、风险、用户请求和 readiness 决定是否升级：

```text
用户明确要求完整 PRD -> 启用 prd-full
接口契约高风险 -> 启用 api-spec-guided，必要时 api-spec-full
验收需要自动化 -> 从 bdd-sbe-guided 升级到 bdd-automation
领域概念冲突严重 -> 从 ddd-guided 升级到 ddd-modeling
```

### 4.4 Method Router

职责：

```text
根据当前目标和项目投影，选择应该启用哪些工程方法。
```

输入：

- 用户目标
- ProjectProjectionV1
- ComplexityEstimate
- 现有 ProjectState
- 当前 TaskflowState

输出：

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

默认策略：

| 复杂度 | 默认方法 |
|---|---|
| L0 | scope + acceptance |
| L1 | prd-guided + acceptance + tdd-obligation |
| L2 | prd-guided + bdd-sbe-guided + tech-spec-guided |
| L3 | prd-guided + bdd-sbe-guided + adr-guided + tech-spec-guided，必要时升级 api/runbook |
| L4 | governance / roadmap 模式，需要显式多阶段规划 |

### 4.5 Question Planner

职责：

```text
把选中的 recipe 和项目投影转成少量高价值问题。
```

Question Planner 的输出写入现有 TaskflowState：

- `OpenQuestion`
- `Assumption`
- `DecisionRecord`
- `RuleRecord`
- `ScenarioRecord`
- `AcceptanceExample`
- `WorkItemCandidate`

每个问题必须带：

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

问题生成规则：

```text
只问会改变范围、验收、架构、测试、交付风险、派发安全的问题。
```

不能问这种问题：

```text
请补充更多需求。
你希望系统怎么做？
是否需要 DDD？
是否需要 BDD？
```

应该问这种问题：

```text
我看到当前 Taskflow 的 dispatch 数据来自 TaskRun.metadata。
这次改造要让 dispatch contract draft 对用户可见。

你希望这个 draft 是：
A. 仅作为调试 / 审计信息
B. 作为用户确认前的正式检查项
C. 作为阻塞 dispatch 的必填契约

推荐：B。
原因：它能让用户看到执行入口，但不会在字段还未稳定前过度阻塞流程。
```

### 4.6 Artifact Projectors

职责：

```text
从状态生成文档和外部规格，但不让文档成为真相源。
```

建议 projector：

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

硬规则：

```text
Projector 只能输出视图。
Projector 不能反向重写 ProjectState 或 TaskflowState。
```

Artifact 应该带：

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

职责：

```text
把 TDD 表达成验证义务，而不是把 Taskflow 变成测试框架。
```

建议对象：

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

初期可以先放在 WorkItem metadata 或 artifact projection 中，等跑通真实流程后再提升成一等模型。

## 5. 方法论映射

| 方法 / 文档 | 在 Taskflow 中的职责 | 写入状态 | 用户看到什么 |
|---|---|---|---|
| PRD guided/full | 产品意图和范围；必要时生成完整 PRD | goal、stakeholders、scope、success criteria、rules | brief 卡片 + PRD 预览，而不是 PRD 表单 |
| SBE | 规则和例子 | rules、examples、acceptance_examples | 示例卡片 |
| BDD | 行为共识 | scenarios、observable outputs、acceptance refs | 行为问题和场景预览 |
| Gherkin | 可执行验收表达 | 只作为 artifact projection | 高级预览 |
| DDD guided/modeling | 项目 / 领域理解；必要时展开完整建模 | bounded_context_refs、ubiquitous_language、domain_model_delta | 术语和边界卡片 |
| SDD | spec-driven artifact 导出 | projector output | OpenSpec / Spec Kit 预览 |
| TDD | 验证义务 | test_suggestions、TestObligation、DoD | 测试义务卡片 |
| ADR | 长期决策解释 | local_decisions、project decisions | 决策卡 + ADR 预览 |
| RFC / Design Doc | 方案比较 | solution_options、local_decisions、risks | tradeoff 卡片 |
| Tech Spec | 实现指导 | interfaces、risks、implementation_notes | 技术方案预览 |
| HLD / LLD | 架构 / 细节拆分 | architecture principles、module boundaries、implementation notes | 架构预览 |
| API Spec | 接口契约 | InterfaceSpec | API 契约预览 |
| Runbook | 交付和运维 | rollout_plan、ops risks、rollback、monitoring | 交付准备卡片 |

## 6. 用户体验定稿

### 6.1 产品观感

用户看到的不是方法论面板，而是工程工作台。

推荐主界面：

```text
项目理解
待确认决策
计划 Brief
执行准备度
工件预览
派发
追踪
```

不要把这些作为主导航：

```text
DDD
BDD
TDD
PRD
```

主导航应该是：

```text
Project Understanding
Decisions
Plan
Artifacts
Dispatch
Trace
```

中文 UI 可表达为：

```text
项目理解
决策确认
计划
工件
派发
追踪
```

### 6.2 真实交互示例

用户输入：

```text
我想让 TaskflowView 中 dispatch contract draft 可见。
```

Taskflow 不应该直接问：

```text
请填写 PRD。
是否使用 BDD？
是否需要 Tech Spec？
```

Taskflow 应该先说：

```text
我已读取后台项目理解快照，并会检查它是否需要增量刷新。
```

然后展示：

```text
我识别到：
- UI 入口在 TaskflowView
- workspace DTO 来自 WorkspaceProjectionService
- dispatch/runtime 数据来自 TaskflowRuntimeProjectionService
- 当前 dispatch 数据大量存在 TaskRun.metadata
- 既有设计边界是：Taskflow 是设计编译器，AgentRun 是运行编排器

我需要你确认两个设计点：
1. 这个 draft 是给用户确认用，还是只给调试 / 审计用？
2. 如果 draft 不完整，是否应该阻塞 dispatch？
```

用户观感应该是：

```text
系统理解了项目；
系统没有让我填表；
系统只问真正影响设计的点；
每个问题都有推荐答案；
每个推荐答案都有证据；
确认后能继续向可执行工作项推进。
```

### 6.3 Review Card 形态

每个关键卡片应该包含：

```text
标题
推荐答案
为什么重要
证据
取舍
操作：接受 / 编辑 / 讨论 / 跳过
影响：范围 / 架构 / 测试 / 派发
```

方法论标签可以放在详情里，例如：

```text
method_tags: ["bdd-sbe-guided", "tech-spec-guided", "tdd-obligation"]
```

但不要让用户必须理解这些标签。

### 6.4 Artifact Preview 形态

工件是次级视图：

```text
PRD guided / full
行为示例
ADR
Tech Spec
API Spec
Runbook
OpenSpec
Spec Kit
```

默认状态：

```text
只读预览
由已确认状态生成
不是事实源
可以重新生成
```

如果用户编辑 artifact，编辑结果应该变成一个状态 patch proposal，而不是直接改文档当真相。

## 7. 建设阶段

### Phase 0：契约清理

产物：

- 本设计文档
- `EngineeringMethodRecipe` 契约
- `ProjectProjectionV1` 契约
- artifact projector 契约
- 方法论到状态字段的映射表

这一阶段不需要改运行行为。

### Phase 1：Project Projection V1

产物：

- `BackgroundProjectUnderstandingRuntime`
- `ProjectProjectionService`
- `ProjectEvidenceRecord`
- `ProjectProjectionV1`
- 代码 / 文档 / 测试 / 配置证据采集
- projection stale 检测
- projection cache
- workspace load 后静默启动
- Taskflow workspace 中的项目理解视图

验收：

```text
给定一个已打开工作区，
系统会在用户发起 Taskflow 前静默生成项目理解快照。

给定一个已有 repo 和 goal，
Taskflow 能读取快照，并在提问前展示项目事实、推断原则、来源引用和置信度。
```

### Phase 2：Method Router + Question Planner

产物：

- `EngineeringMethodRegistry`
- `MethodRouter`
- `QuestionPlanner`
- recipe-driven question packs
- workspace DTO 中展示 selected method plan

验收：

```text
给定一个涉及 API 和数据 migration 的目标，
Taskflow 能自动选择 prd-guided、bdd-sbe-guided、tech-spec-guided、
api-spec-guided、runbook-guided、tdd-obligation。

用户看到 1-5 个决策卡片，每个卡片都有证据和推荐答案。
```

### Phase 3：Artifact Projectors

产物：

- native brief projector
- PRD guided / full projector
- ADR projector
- BDD examples / Gherkin projector
- Tech Spec projector
- OpenSpec / Spec Kit preview projector

验收：

```text
工件全部由已确认的 TaskflowState 和 ProjectState 生成。
工件编辑不能直接重写真相源。
```

### Phase 4：Test Obligation + Readiness

产物：

- `TestObligationV1`
- 测试义务卡片
- 绑定测试义务的 readiness gates
- acceptance examples -> WorkItems -> tests 的 TraceLink

验收：

```text
每个可派发 WorkItem 都有 acceptance refs 和可见验证义务。
```

### Phase 5：UX 定稿

产物：

- TaskflowView 信息架构调整
- Project Understanding panel
- Decision Cards panel
- Plan Brief panel
- Artifact Preview panel
- Readiness / Dispatch panel

验收：

```text
用户可以完成一次真实已有项目 Taskflow，
过程中不需要阅读 raw JSON、内部状态名、完整 PRD/DDD/BDD 表单。
```

## 8. UI 信息架构

推荐 Taskflow workspace 结构：

```text
Header
  Goal
  Status
  Confidence
  Readiness
  Runs

项目理解
  识别到的模块
  项目原则
  术语
  受影响区域
  证据引用

决策确认
  问题
  假设
  推荐答案
  取舍

计划
  brief
  scope
  WorkItems
  acceptance
  test obligations

工件
  PRD guided / full
  behavior examples
  ADR
  Tech Spec
  API Spec
  Runbook
  OpenSpec / Spec Kit

派发
  dispatch contract
  readiness gates
  TaskRuns / AgentRuns

追踪
  goal -> decision -> example -> WorkItem -> TaskRun -> artifact
```

这个界面应该像一个工程决策工作台，而不是文档向导。

## 9. 硬边界

1. Taskflow 仍然是设计编译器。
2. AgentRun 仍然是运行编排器。
3. Project Understanding 是工作区后台层，不是 Taskflow 会话内同步步骤。
4. Project Projection 是可审阅推断，不是长期真相。
5. Confirmed Project Memory 才是长期真相。
6. 工程方法是 recipe 和 projector，不是顶层状态模块。
7. 文档是生成视图，不是真相源。
8. Projector 不能直接修改状态。
9. Gherkin 是可选输出，不是用户必填语言。
10. TDD 表达成验证义务，不是测试框架。
11. DDD 先从 bounded context、统一语言、决策记忆开始，不做全量 ontology。

## 10. 初始内置 Recipe

### 10.1 `prd-guided` / `prd-full`

触发条件：

- 用户可见功能
- 产品价值不清晰
- 涉及业务范围

写入：

- goal statement
- stakeholders
- scope in/out
- success criteria
- business rules

投影：

- `prd-guided`: brief / scope / success criteria preview
- `prd-full`: 完整 PRD preview，由已确认状态生成

### 10.2 `bdd-sbe-guided` / `bdd-contract` / `bdd-automation`

触发条件：

- 涉及业务行为
- 存在状态流转
- 不同角色结果不同
- 验收标准容易争议

写入：

- rules
- examples
- scenarios
- acceptance examples
- open questions

投影：

- `bdd-sbe-guided`: example table
- `bdd-contract`: acceptance contract / optional Gherkin
- `bdd-automation`: Cucumber / CI / executable acceptance integration

### 10.3 `ddd-guided` / `ddd-modeling` / `ddd-full-workshop`

触发条件：

- 术语含义不清
- 领域概念冲突
- 多个 bounded context
- 实体生命周期复杂

写入：

- ubiquitous language
- bounded context refs
- domain model delta
- project decisions

投影：

- `ddd-guided`: domain glossary / boundary preview
- `ddd-modeling`: entity / value object / aggregate / lifecycle notes
- `ddd-full-workshop`: event storming / context map / full modeling output

### 10.4 `sdd-openspec`

触发条件：

- 用户希望 repo-native spec workflow
- 项目已经使用 OpenSpec 风格文件
- 团队希望审阅 spec delta

写入：

- 不直接写入真相源

投影：

- proposal / design / tasks / spec delta preview

### 10.5 `tdd-obligation`

触发条件：

- WorkItem 可派发
- 有 acceptance examples
- 高风险行为需要证明

写入：

- test suggestions
- test obligations
- validation gates

投影：

- test plan
- validation checklist

### 10.6 `adr-guided` / `adr-full`

触发条件：

- 存在有意义的方案选择
- 未来维护者可能质疑这个选择
- 被拒绝方案也很重要

写入：

- DecisionRecord
- rationale
- rejected options
- TraceLinks

投影：

- `adr-guided`: decision card / rationale preview
- `adr-full`: complete ADR preview

## 11. 成功标准

这部分算建设到位，需要满足：

1. 工作区加载后会静默启动项目理解，而不是等用户提问后才开始。
2. 已有项目的 Taskflow 会读取 ProjectProjectionV1 快照，再开始追问。
3. 每个问题都有来源证据和 why_needed。
4. 方法选择来自 recipe，而不是纯 prompt 感觉。
5. BDD / DDD / SDD / TDD / PRD 都作为投影和工作流能力出现，而不是表单。
6. WorkItem 带 acceptance refs 和 validation obligations。
7. dispatch 被 readiness gates 控制，而不是被“文档是否完整”控制。
8. 用户能 accept / edit / reopen 关键决策，不需要编辑 JSON。
9. artifact 可以从状态重新生成。
10. Project Memory 更新必须显式确认。
11. runtime 执行仍然属于 AgentRun。

## 12. 最终定稿

这部分应该建设成：

```text
BackgroundProjectUnderstandingRuntime
ProjectProjectionV1
EngineeringMethodRecipe
MethodRouter
QuestionPlanner
ArtifactProjector
TestObligation
```

不应该建设成：

```text
BDD module
DDD module
PRD module
TDD module
文档模板中心
workflow graph engine
```

最终用户观感应该是：

```text
系统在我打开工作区后就开始理解项目，
Taskflow 读取已有项目理解，
只问真正重要的工程问题，
展示每个问题的原因和证据，
把我的回答编译成可执行工作项，
并在需要时生成 PRD / ADR / Spec / Test / Runbook 等视图。
```

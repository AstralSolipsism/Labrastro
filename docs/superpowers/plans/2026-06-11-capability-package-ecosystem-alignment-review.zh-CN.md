# 能力包生态架构对齐审查文档

> 本文用于在会话压缩后审查当前矩阵文档是否仍然对齐用户预期。它不是新的架构方案，而是对 `2026-06-11-capability-package-ecosystem-architecture-matrix.md` 的中文对照、漂移检查和冻结前修正清单。

**状态：** Draft for review

**被审查文档：** `docs/superpowers/plans/2026-06-11-capability-package-ecosystem-architecture-matrix.md`

**审查结论：** 当前矩阵文档的主目标没有发生实质漂移。它已经从“修 Waza 一次失败”扩展为“能力包安装生态的统一架构”，这符合前文要求的“构架统一优先”。但冻结前需要修正若干表达和边界，尤其是：不要让 UI 暴露“等待支持”语义，不要让 `mapping_required` 被理解为等待开发者，不要让“package install decision”看起来像用户要审查大量细项，并且要把 LLM 的“判断运行侧”与后端的“最终归一化/执行权”写得更清楚。

---

## 1. 对照依据

本文对照以下已明确的前文共识：

1. 用户给出 GitHub 链接，产品预期是安装能力包，而不是只生成配置草稿。
2. Labrastro 是中心化自托管系统：有些能力可以纯服务端运行，有些必须在用户本地端运行，有些需要两侧配合。
3. 具体安装/运行在哪一侧，需要 agent/LLM 参与判断，但最终状态和执行不能由 LLM 自由发挥。
4. 能力状态必须区分：已登记、已准备/已物化、已安装、已激活。
5. 环境状态必须区分：服务端/本地端的已检查、缺失、已配置、失败、过期等。
6. Skills 的 `SKILL.md` 可以集中由服务端管理；真正需要本地安装的通常是 skills 依赖、MCP 进程、凭据或本地工具。
7. 一个 skills 仓库可能有多个 `SKILL.md`，且包含 `scripts/`、`references/`、共享根目录文件等，不能只复制单个 `SKILL.md`。
8. 多个 skills 可能依赖同一个能力，例如 `gh` CLI；依赖要建图，不能复制成互相冲突的散装配置。
9. 要保存完整仓库快照，但运行时只暴露受控文件闭包。
10. 更新策略必须跟随 GitHub/upstream 版本，不应在 Labrastro 内另造用户可见版本体系。
11. LLM 不能扩展枚举，只能提交未分类项或候选项。
12. 允许部分安装并在前端合理展示，但不能让用户被大量选择淹没。
13. 不同能力之间的依赖冲突必须有环境隔离策略。
14. 共享系统级能力可以存在，但判断依据必须可审计，不能假装能全覆盖。
15. 安装能力包不等于启用能力；启用能力后，其有效 hook 应默认跟随启用，不要让用户逐个审查 hook。
16. “还不支持”“等待支持”不应作为用户体验路径；凭据、GUI 授权、系统权限、命令审查等应表达为明确的用户/管理员动作。
17. 用户习惯是把“需要安装 GitHub CLI”也交给 agent 处理；系统应尽量自动处理可控安装，不能只告诉用户自己装。
18. 凭据必须支持多租户：默认按用户隔离，也允许经过管理员配置的 workspace/server-global 共享凭据。
19. 当前阶段是开发期，目标不是最小可用闭环，而是先形成统一架构、执行矩阵、边界、验收标准，再生成可执行 goal。

## 2. 总体目标对齐

| 预期目标 | 矩阵文档覆盖位置 | 对齐判定 | 说明 |
| --- | --- | --- | --- |
| GitHub 链接应进入安装流程 | `Product Contract`、`Install Flow` | 对齐 | 文档明确 GitHub link -> snapshot -> manifest -> install plans -> check results。 |
| 构架统一优先，不做最小闭环 | `Goal`、`Implementation Matrix`、`Execution Order` | 对齐 | 文档写明 staged 不是 MVP，每一阶段必须引入目标架构边界。 |
| 修复旧 Waza 失败不是局部补丁 | `Current Failure Baseline`、`Acceptance Gate G-01` | 对齐 | 文档把 `python-pkg` 失败抽象为 LLM 枚举越权和后端归一化问题。 |
| 不再让 LLM 自由写 config/skill | `Core Principles P-01/P-02`、`LLM Boundary` | 对齐 | LLM 只能建议，后端生成最终 manifest/config/action。 |
| 服务端和本地端必须统一协议 | `Architecture`、`InstallPlan`、`Local Peer Executor` | 对齐 | 使用同一 `InstallPlan` 协议，按 target 分配执行。 |

**结论：** 主目标没有偏离。当前矩阵已经从“失败排错”转向“能力包生态架构重建”，这是前文要求的方向。

## 3. 关键共识逐条对照

### 3.1 GitHub 链接就是安装意图

| 项 | 当前矩阵 | 判定 |
| --- | --- | --- |
| 用户给链接后的默认预期 | `The user experience contract is simple... they expect Labrastro to install the capability` | 对齐 |
| 安装不等于启用 | `Installation does not imply activation` | 对齐 |
| 不应让用户审查大量细项 | `UI must not force users through large decision lists` | 对齐 |

**需要修正文案：** `package install decision` 容易被读成“用户还要对安装本身做复杂审批”。建议冻结前改成：

```text
install request
-> risk/manual-step review only when required
```

也就是：用户给 GitHub 链接并点击安装，本身已经表达安装意图；系统只在高风险、凭据、GUI 授权、系统权限、命令审查、许可证等场景打断。

### 3.2 服务端、本地端、两侧运行

| 项 | 当前矩阵 | 判定 |
| --- | --- | --- |
| 中心化服务端是事实源 | `Server is the global fact source` | 对齐 |
| local peer 不是所有能力都需要 | `target: server | local_peer`，并由 component placement 决定 | 基本对齐 |
| 服务端不能伪造本地端已安装 | `Peer target facts`、`Server cannot mark local peer...` | 对齐 |
| LLM 判断安装/运行侧 | `LLM may explain why ... server/local_peer/both` | 需澄清 |

**漂移风险：** 前文说“具体需要在哪一侧安装、运行，就是 LLM 要来做判断的事情”。矩阵文档为了安全写成 LLM 只能“explain why”。这不是方向错误，但措辞可能过度收缩。

推荐冻结写法：

```text
LLM may propose target placement with evidence.
Backend normalizer accepts, rewrites, or rejects the proposed placement.
Final executable target placement is backend-owned.
```

中文语义：LLM 负责做“带证据的目标侧判断建议”，后端负责把判断收敛成可执行的最终 target。

### 3.3 能力状态和环境状态

| 项 | 当前矩阵 | 判定 |
| --- | --- | --- |
| 已登记 | `registered` | 对齐 |
| 已准备/已物化 | `materialized` | 对齐 |
| 已安装 | `installed` | 对齐 |
| 已激活 | `activation_state: active` | 对齐 |
| 环境检查 | `check_state` | 对齐 |
| 服务端/本地端分别检查 | `target` + server/local peer result | 对齐 |

**需要补强：** `materialized` 是偏工程词。前端不应直接显示“materialized”，应该显示“已准备”或“已写入受控存储”。内部枚举可以保留，UI 文案要人话化。

### 3.4 Skills 集中管理与依赖拆分

| 项 | 当前矩阵 | 判定 |
| --- | --- | --- |
| `SKILL.md` 可集中管理 | `Skill is an artifact, not a process` | 对齐 |
| 不强制每个 skill 都有 local peer | `delivery_targets` | 对齐 |
| 本地需要安装的是依赖/进程/凭据 | `dependency_refs`、`target placement`、`InstallPlan` | 对齐 |
| 多个 skill 共享同一依赖 | `Dependency Graph`、`required_by` | 对齐 |

**结论：** 这一块没有漂移。矩阵选择的是更清晰的拆分：skill 文档/源代码包集中管理，依赖和运行能力按 target 下发或检查。

### 3.5 多 Skill 仓库、相对路径、文件闭包

| 项 | 当前矩阵 | 判定 |
| --- | --- | --- |
| 保存完整仓库快照 | `Package Source Snapshot` | 对齐 |
| 运行时只暴露受控闭包 | `Exposed File Closure` | 对齐 |
| `scripts/`、`references/`、共享根目录 | `Skill Artifact Bundle` 和默认 include | 对齐 |
| 多 skill 命名冲突 | namespaced skill ids | 对齐 |

**需要补强：** 当前 include 规则提到 `rules/**, references/**, docs/**, assets/**`，但多 skill 仓库常见根目录结构可能更复杂。建议后续实现计划中加入一个 fixture：

```text
repo/
  SKILL.md
  references/
  scripts/
  skill-a/SKILL.md
  skill-a/references/
  skill-b/SKILL.md
  shared/
```

验收要证明每个 skill 的相对路径解析不会串到别的 skill，也不会丢掉共享依赖。

### 3.6 更新策略

| 项 | 当前矩阵 | 判定 |
| --- | --- | --- |
| 用户可见版本跟随 GitHub/upstream | `Updates follow upstream versions` | 对齐 |
| Labrastro snapshot 仅内部追踪 | `snapshot_id` internal traceability | 对齐 |
| main 分支无 release 时锁 commit | `main@commit` | 对齐 |
| 更新不自动激活 | `must not auto-activate` | 对齐 |

**结论：** 更新策略对齐，没有明显漂移。

### 3.7 LLM 边界

| 项 | 当前矩阵 | 判定 |
| --- | --- | --- |
| LLM 不能扩展枚举 | `Forbidden LLM Authority` | 对齐 |
| 未分类项进入 open findings | `open_findings` | 对齐 |
| 后端最终归一化 | `backend normalizers` | 对齐 |
| 不能直接执行 LLM shell | `Do not execute LLM-provided shell commands directly` | 对齐 |

**风险点：** `unsupported_component_candidates` 这个内部名可以保留，但前端不能显示“还不支持”。用户体验上应显示：

```text
发现未分类安装证据
影响：以下组件暂不能自动安装
下一步：系统需要人工确认命令 / 绑定凭据 / 选择路径 / 获取管理员授权
```

而不是：

```text
等待开发者支持
不支持
高级模式
```

### 3.8 部分安装、冲突和隔离

| 项 | 当前矩阵 | 判定 |
| --- | --- | --- |
| 允许部分安装 | `installed or degraded installed state` | 对齐 |
| 不因单个未知依赖丢弃整个包 | `block only dependent components` | 对齐 |
| Python/npm 隔离 | package-local venv / Node env | 对齐 |
| 共享 `gh` 等系统工具 | shared allowlist | 基本对齐 |
| 冲突处理 | `Conflict Handling` | 对齐 |

**需要补强：** 共享系统能力不能只靠固定 allowlist。应扩展为“共享能力注册表”：

```text
shared_capability_registry:
  id
  executable_names
  version_check
  install_action_policy
  platforms
  credential_interaction
  conflict_policy
  evidence_required
```

这样 `gh`、`git`、`node` 不是靠 LLM 猜，而是靠可检查的注册项。

### 3.9 Hook 行为

| 项 | 当前矩阵 | 判定 |
| --- | --- | --- |
| 安装不启用 hook | install/activation split | 对齐 |
| 启用能力后 hook 默认跟随启用 | `Hooks follow parent activation` | 对齐 |
| 不让用户逐个审查 hook | `not individually reviewed by default` | 对齐 |
| 无效 hook 不进入 active manifest | `invalid or unmapped... must not enter` | 对齐 |

**结论：** 这一点已按用户纠正后的语义写入，没有漂移。

### 3.10 凭据和多租户

| 项 | 当前矩阵 | 判定 |
| --- | --- | --- |
| 默认按用户隔离 | `Default scope is user` | 对齐 |
| workspace/global 共享需管理员配置 | `explicit admin configuration` | 对齐 |
| 服务端凭据 actor 可审计 | `user_delegated | service_account` | 对齐 |
| secrets 不进 LLM/config/log | 明确写入 | 对齐 |

**需要补强：** 凭据 UI 需要明确同一个能力在不同登录账户下使用不同绑定；server-global 凭据必须显示“由谁提供、给谁可用、以谁身份调用”。

## 4. 当前矩阵的主要缺口

### 4.1 文档语言与可审查性

当前矩阵是英文，虽然结构完整，但不利于用户直接审查。需要保留英文技术名词，但主控审查文档应有中文版本或中文对照。

本文件就是第一层中文对照。冻结前建议再做一版中文主控矩阵，或者把英文矩阵改为中英混排。

### 4.2 `mapping_required` 的用户语义

内部 `mapping_required` 是合理状态，但它不能变成前端“等待支持”。用户已经明确反对这种不可控体验。

建议加一条 UI 规则：

```text
Internal mapping_state names must not be exposed directly.
User-facing copy must describe the required action, responsible actor, and re-check path.
```

### 4.3 自动安装 vs 手动动作的边界

当前矩阵把 manual steps 列得比较清楚，但还需要强调：

```text
If a typed safe action exists, Labrastro should execute it through the target executor.
Manual steps are only for credentials, GUI authorization, system authority, license acceptance, path selection, or high-risk command review.
```

否则实现时仍可能退化成“提示用户自己安装 GitHub CLI”。

### 4.4 共享系统能力的依据

当前 allowlist 是初始列表，不足以作为长期依据。需要在实现矩阵中补一行：

```text
B-10 Add shared capability registry
```

否则不同能力共享 `gh`、`docker`、`node` 时会变成散装判断。

### 4.5 执行矩阵还不是最终任务拆分

当前 `Implementation Matrix` 已经足够作为架构控制文档，但还不是可直接执行的任务清单。下一步应从它派生实施计划，拆成：

```text
stage
task id
files
data model changes
API changes
frontend projection
tests
evidence
rollback / migration concern
```

这一步完成后，才适合创建新的强指导性 goal。

## 5. 是否发生目标漂移

| 维度 | 判定 | 原因 |
| --- | --- | --- |
| 从安装失败扩大到生态架构 | 未漂移 | 这是用户明确要求的“构架统一优先”。 |
| 从修枚举错误扩大到 LLM 边界 | 未漂移 | `python-pkg` 错误本质就是 LLM 越权和后端归一化失败。 |
| 从 skills 安装扩大到 MCP/凭据/环境 | 未漂移 | 用户明确提出中心化自托管、本地端安装、多租户凭据、MCP/skills 区分。 |
| 从局部实现扩大到执行矩阵 | 未漂移 | 用户明确拒绝“最小可用闭环”，要求矩阵执行文档。 |
| 从 LLM 判断 target 到后端最终 target | 需澄清 | 安全边界合理，但需要写成“LLM 提议，后端归一化”而不是削弱 LLM 判断价值。 |
| 使用 `unsupported`/`mapping_required` 等词 | 有 UX 漂移风险 | 内部可用，前端不能表达为等待开发者或不支持。 |

**总判定：** 方向没有漂移，但当前矩阵仍有若干“实现时容易漂移”的措辞风险，需要在冻结前修订。

## 6. 建议对原矩阵做的修订

### 6.1 修改 `Allowed LLM Responsibilities`

建议把：

```text
Explain why a component should run on server, local_peer, or both.
```

改为：

```text
Propose target placement as server, local_peer, or both, with source evidence and reasoning.
Backend normalizer owns final executable target placement.
```

### 6.2 修改安装流程措辞

建议把：

```text
package install decision
```

改为：

```text
install request confirmation when needed for risk/manual steps
```

避免用户误解成安装前还要审查大量条目。

### 6.3 增加 UI 禁止语

建议在 `UI Contract` 增加：

```text
The UI must not expose internal terms such as unsupported, mapping_required, unmapped enum, or waiting for developer support as the primary user message.
It must show the affected components, required actor, concrete next action, and re-check behavior.
```

### 6.4 增加共享能力注册表

建议在 `Domain Model` 或 `Environment and Isolation` 增加：

```text
shared_capability_registry
```

并在实施矩阵增加对应工作项。

### 6.5 增加多 Skill 仓库 fixture

建议在 `Tests and Evidence` 增加：

```text
T-09 Multi-skill repository layout
```

覆盖根目录 `SKILL.md`、子目录多个 `SKILL.md`、共享 `scripts/`、共享 `references/`、相对路径引用和命名空间冲突。

## 7. 冻结前检查清单

冻结原矩阵前，至少确认以下问题：

1. 是否同意把 LLM target 判断表述为“LLM 提议，后端归一化最终 target”？
2. 是否同意前端禁止出现“等待开发者支持/不支持/高级模式”作为主要用户状态？
3. 是否同意把共享系统能力从 allowlist 升级为注册表？
4. 是否同意把 `materialized` 的用户文案固定为“已准备”或“已写入受控存储”？
5. 是否同意下一步先修订矩阵，再派生执行计划和新 goal？

## 8. 对后续 agent 的指令

后续 agent 不应直接从旧的 chat 摘要继续执行实现。必须按以下顺序推进：

1. 先读取英文矩阵文档和本文。
2. 按本文第 6 节修订英文矩阵。
3. 修订后再生成实施计划。
4. 实施计划确认后，才创建新的 goal。
5. 执行时每个任务必须回填矩阵行、测试证据和用户可见状态语义。

如果后续会话再次被压缩，本文优先级高于临时聊天摘要，低于用户后续明确修改意见。

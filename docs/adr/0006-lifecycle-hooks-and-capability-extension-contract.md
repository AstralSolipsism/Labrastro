# ADR 0006: 生命周期 Hooks 与能力扩展契约

## Status

Accepted.

## Context

本项目仍处于开发阶段。Hooks、能力包、Skill、MCP、记忆、AgentRun
和 SessionRun 之间不承担旧数据和旧接口兼容负担。当前目标是一次性收敛
架构方向，避免继续在旧的内部 hook 文档上修补。

工作区根目录历史文档 `../docs/hook-system.md` 来自项目来源
ReuleauxCoder，描述的
是内部 Python AOP 风格 hook：在后端进程内注册 Python 类，通过
`HookRegistry` 在模型请求前后、工具执行前后介入。当前 Labrastro 已经做
了大量服务端控制面、远端 peer、能力包、SessionRun 可见投影和权限网关改
造，该旧文档不再作为目标架构依据。

新的 hooks 架构参考 Claude Code 的产品级事件模型：hooks 不是后端开发者
手写 Python 类的扩展口，而是用户、项目、能力包、Skill、MCP、管理员策略
都能声明的生命周期协议。系统在会话、用户输入、工具、权限、子任务、压缩、
环境变化等关键节点发出稳定事件；hook 收到结构化上下文后，可以放行、阻止、
补充上下文、请求确认、改写受支持输入、记录诊断或触发后台动作。

本文档按全量目标架构定稿。实现可以拆阶段提交，但每个阶段都必须朝同一套
公开生命周期协议推进，不允许引入临时最小版本架构、双轨 schema 或后续需
要兼容迁移的过渡接口。

Labrastro 不能照搬 Claude Code 的本地 CLI 假设。本项目的核心边界是：

- 服务端是 Agent 配置、能力包、权限、审计、SessionRun、AgentRun 和模型
  请求的控制面。
- VS Code extension 是用户本地端和可选 peer，负责本地资源、用户交互和
  ChatView/Settings 展示。
- 能力可能只在服务端运行、只在本地 peer 运行，或两端都需要配置。
- Settings 发起的交互式能力流程必须绑定 SessionRun/ChatView 可见状态；
  普通会话、能力包安装、MCP 交互和 Skill 调用不能各自生成隐藏流程。
  Taskflow 等长期后台 AgentRun 通过 Taskflow runtime projection 和 AgentRun
  audit 承载运行细节，并保留回到发起会话、任务和工作项的追踪关系。

## Product Rules

- Hooks 的公开契约是生命周期事件协议，不是 Python 类接口。
- `HookRegistry` 保留为内部实现层；能力包、Skill、MCP 和用户配置不得直
  接依赖 Python hook 类或装饰器。
- 所有公开 hook 声明必须有来源、运行位置、处理器类型、权限需求、可见
  摘要和审计信息。
- 所有 hook 执行结果必须进入统一可审计运行通道。交互会话内的事实进入
  SessionRun/ChatView；Taskflow 和其他长期后台 AgentRun 的事实进入对应
  runtime projection 与 AgentRun audit，并通过 TraceLink、source metadata
  或父运行关系保留追踪。
- Settings 可以发起能力安装、配置和验证流程，但发起后必须绑定一个
  SessionRun，让 ChatView 能看到交互式流程的工具调用、审批、草案、安装、
  验证和终态。
- 普通工具、MCP 工具、Skill 触发、能力包安装工具必须共用同一套工具生命
  周期事件，不允许新增能力包专用隐藏事件链。
- 权限裁决只属于统一权限网关。Hook 可以提供建议、理由、补充上下文或阻
  止建议，但不能绕过权限网关直接授予工具执行权。
- Memory Provider 不是 hook。生命周期事件只触发 `MemoryRuntime`，
  `MemoryRuntime` 再按策略调用 provider。
- MCP server 可以作为普通工具来源，也可以通过 adapter 成为 memory
  provider 或 hook handler，但这些身份必须分开声明。
- 开发阶段不做冗余迁移、兼容 alias 或旧 schema 兜底。旧概念直接移除或
  改名，以新契约为准。

## Core Concepts

### 生命周期事件

生命周期事件是系统公开给 hooks 的稳定节点。事件名、触发时机、输入字段和
允许输出必须由 schema 固定。事件只表达“系统运行到哪里了”，不表达具体
实现方式。

### Hook 声明

Hook 声明来自用户配置、项目配置、本地配置、管理员策略、能力包、Skill、
MCP server 或系统内置定义。声明内容包括：

```text
source          来源
event           监听的生命周期事件
matcher         事件过滤条件
handler_type    处理器类型
placement       server | peer | both
permissions     需要的权限
display_name    用户可读名称
summary         用户可读说明
technical       技术详情
```

### Hook 处理器

处理器是 hook 被触发后实际执行的动作。目标处理器类型为：

```text
command       在声明位置执行命令
http          调用 HTTP endpoint
mcp_tool      调用 MCP 工具
prompt        使用一次模型判断生成结构化结果
agent         派生受控 agent 做多步检查
internal      系统内置 adapter，仅核心代码可用
```

`internal` 只服务本项目核心能力，例如记忆注入、工具输出归档、LSP 诊断等。
能力包作者不能要求用户写 Python 类。

所有处理器都必须通过统一 runtime adapter 总线装载和执行。公开 schema 中
允许声明的 handler type，不能只停留在配置解析或 Settings 展示层；它必须有
对应 adapter、权限边界、失败语义、审计事件和验收测试。

### 运行位置

所有 hook 必须声明运行位置：

```text
server  服务端或服务端 worker/container 中执行
peer    用户本地 VS Code peer 中执行
both    两侧都有动作，必须拆成可审计的 server/peer 子执行
```

`local` 不是规范值。服务端和 peer 是本项目唯一的运行位置边界。

### 来源

Hook 来源必须进入相应 UI 投影和审计：

```text
system_builtin       系统内置
admin_managed        管理员托管策略
user_config          用户级配置
project_config       项目可提交配置
local_project_config 本地项目配置
capability_package   能力包
skill                Skill frontmatter 或 Skill 所属能力包
mcp_server           MCP server 声明
session              当前会话临时声明
```

UI 展示时应使用用户可读名称和说明；内部 id、路径、命令和原始 JSON 进入技
术详情。

### 信任状态

非系统内置 hook 必须有信任状态：

```text
pending_review  已发现，待用户或管理员审查
trusted         已信任，可执行
disabled        已禁用，不执行
blocked         被策略阻止
```

能力包安装成功不等于其 hooks 自动可信。安装后如果能力包携带 hooks，必须
在草案和安装确认中展示：来源、运行位置、会执行什么、需要哪些权限。

## Lifecycle Events

本节列出 lifecycle 的目标事件目录。外部配置可声明事件必须以运行时已接入
为准，不能因为事件出现在目标目录里就允许能力包、Skill 或 MCP manifest
声明。当前外部配置可声明事件只有：

```text
UserPromptSubmit
PermissionRequest
PreToolUse
PostToolUse
PostToolUseFailure
PostToolBatch
Stop
StopFailure
```

`SessionStart`、`SessionEnd` 和其他尚未接入外部配置运行线的事件仍保留为
核心 runtime/internal adapter 的目标事件；在它们获得真实触发入口、输出语
义、审计投影和测试前，validator 必须拒绝外部声明。

### 会话事件

| 事件 | 触发时机 | 主要用途 |
| --- | --- | --- |
| `SessionStart` | 新会话、恢复会话、清空后继续、压缩后继续 | 绑定上下文、检查能力状态、准备记忆范围 |
| `SessionEnd` | 会话结束、切换、登出、清空 | flush、清理、写入总结、释放资源 |

### 用户输入事件

| 事件 | 触发时机 | 主要用途 |
| --- | --- | --- |
| `UserPromptSubmit` | 用户输入进入模型前 | 识别意图、补充上下文、阻止高风险请求、从链接启动能力安装 |
| `UserPromptExpansion` | 用户命令或 Skill 指令展开前 | 校验命令、补充上下文、阻止不可用指令 |

用户在 ChatView 中发“安装这个 Skill/能力/MCP 链接”，必须从
`UserPromptSubmit` 进入统一会话流程。Settings 只是另一个发起入口，不是
独立后台流程。

### 工具事件

| 事件 | 触发时机 | 主要用途 |
| --- | --- | --- |
| `PreToolUse` | 工具参数生成后、执行前 | 校验、补充上下文、改写受支持输入、建议阻止 |
| `PermissionRequest` | 即将请求权限审批时 | 由 hook 补充审批理由、建议允许或拒绝 |
| `PermissionDenied` | 权限被自动拒绝后 | 给模型可恢复反馈，说明如何改正 |
| `PostToolUse` | 工具成功或有结果后 | 检查结果、补充上下文、捕获记忆、归档输出 |
| `PostToolUseFailure` | 工具失败后 | 归一错误、提供恢复建议、触发诊断 |
| `PostToolBatch` | 一批并行工具结束后 | 聚合诊断、统一收尾 |

能力包安装必须表现为普通工具过程：

1. 生成能力包草案。
2. 通过 SessionRun 展示结构化草案。
3. 需要安装时进入统一权限/审批。
4. 执行安装工具。
5. 验证能力、MCP、Skill、环境需求。
6. 写入终态事件。

### 子任务和 AgentRun 事件

| 事件 | 触发时机 | 主要用途 |
| --- | --- | --- |
| `SubagentStart` | 派生子 agent 前后 | 审计子任务来源、绑定上下文 |
| `SubagentStop` | 子 agent 完成 | 检查结果、决定是否需要继续 |
| `TaskCreated` | 后台任务创建 | 审计任务来源、校验任务配置 |
| `TaskCompleted` | 后台任务完成 | 投影结果、触发收尾 |
| `Stop` | 一轮回复正常结束 | 验证质量、写入终态提示和产物引用 |
| `StopFailure` | 一轮因错误结束 | 记录失败、生成恢复信息 |

AgentRun 原始事件不能直接进入用户正文。所有可见过程必须先投影成 canonical
SessionRun 事件。

### 压缩事件

| 事件 | 触发时机 | 主要用途 |
| --- | --- | --- |
| `PreCompact` | 上下文压缩前 | 保存必须保留的长期事实，必要时阻止压缩 |
| `PostCompact` | 压缩完成后 | 记录新摘要、刷新记忆索引 |

压缩事件是长会话、Taskflow、能力安装会话和记忆系统的共同边界。

### 环境和配置事件

| 事件 | 触发时机 | 主要用途 |
| --- | --- | --- |
| `ConfigChange` | 配置变更 | 重新校验能力、hooks、provider 和权限 |
| `CwdChanged` | 工作目录变化 | 刷新项目上下文、环境状态 |
| `FileChanged` | 被监听文件变化 | 触发验证、刷新上下文或重新索引 |
| `WorktreeCreate` | 创建隔离工作区前后 | 替换或扩展默认 worktree 行为 |
| `WorktreeRemove` | 移除隔离工作区 | 清理资源 |

这类事件必须声明发生在 server 还是 peer。服务端容器路径、用户本机路径和
AgentRun worktree 不能混为一个路径空间。

### MCP 交互事件

| 事件 | 触发时机 | 主要用途 |
| --- | --- | --- |
| `Elicitation` | MCP 工具运行中请求用户输入 | 进入统一用户确认/输入流程 |
| `ElicitationResult` | 用户完成 MCP 输入后 | 审计输入并返回 MCP server |

MCP 不允许自建独立弹窗审批链。MCP 的用户输入和确认必须进入统一
SessionRun/权限/审计流程。

### 通知事件

| 事件 | 触发时机 | 主要用途 |
| --- | --- | --- |
| `Notification` | 系统需要用户注意 | 桌面/VS Code 提示、等待输入、权限提示 |

通知事件只能提醒，不应改变主控制流。

## Hook Input and Output

### 通用输入

每个事件都必须包含：

```text
event_name
session_run_id
agent_run_id
turn_id
source
placement
origin
locale
timestamp
metadata
```

工具相关事件还必须包含工具名、工具来源、输入摘要、权限上下文和执行位置。
用户输入事件必须包含用户输入文本和当前会话摘要。MCP 事件必须包含 MCP
server id、工具名和请求输入 schema。

### 工具 Matcher 标准字段

工具生命周期事件的 matcher 和顶层 payload 只能使用同一组标准字段：

```text
tool_names      工具名列表
tool_call_ids   工具调用 id 列表
tool_sources    工具来源列表
mcp_servers     MCP server id 列表
```

单工具事件也必须使用列表结构，列表长度通常为 1；批量工具事件使用同一结构，
列表长度可以大于 1。这样 `PreToolUse`、`PermissionRequest`、`PostToolUse`、
`PostToolUseFailure` 和 `PostToolBatch` 不需要维护两套字段形状。

matcher 语义固定为：

```text
实际值是标量，期望值是标量：相等才命中
实际值是标量，期望值是列表：实际值在列表中才命中
实际值是列表，期望值是标量：列表包含期望值才命中
实际值是列表，期望值是列表：两组列表有交集才命中
实际值是空列表：不命中非空期望值
```

`tool_name`、`tool_call_id`、`tool_source`、`mcp_server` 不属于 lifecycle
顶层 matcher 或顶层 payload 字段。它们可以出现在普通 ChatView transcript、
权限请求对象、provider diagnostics 或嵌套技术详情中，但不能作为 lifecycle
协议的可匹配字段。配置声明中出现这些旧单数字段必须直接失败，不能静默转
换成新字段，也不能作为兼容 alias 保留。

工具 lifecycle payload 顶层也必须遵守同一边界。单工具、批量工具和权限事
件的顶层工具事实只允许使用 `tool_names`、`tool_call_ids`、
`tool_sources`、`mcp_servers`。原始 `tool`、`tool_call`、`tool_calls`、
`result`、`error`、`subject`、`target`、权限上下文和执行结果只能进入
`technical` 技术详情容器。`technical` 不参与 matcher，不能被后续实现拿来
恢复第二套匹配语义。

`event_name`、`placement`、`trigger_source`、`session_run_id`、
`agent_run_id`、`turn_id`、`timestamp` 是 lifecycle context 的权威字段，
只能由 context 构造参数决定。调用方 payload 里即使带有同名字段，也不能覆
盖权威字段。

### 通用输出

Hook 输出允许表达：

```text
continue_flow      是否继续主流程
decision           allow | deny | ask | defer | none
reason             给模型或审计看的结构化原因
user_message       给用户看的本地化消息 key 或安全文本
additional_context 给模型的额外上下文
updated_input      受支持事件的替换输入
diagnostics        审计诊断
artifacts          产物引用
```

每个事件必须完整实现自己的输出语义。运行时不能把已声明支持的字段降级成
“只记录诊断”。如果某个字段对某个事件没有语义，必须在 schema 中明确不支
持，并在 validator 中拒绝或诊断；如果 schema 声明支持，dispatcher、adapter
和调用链必须有确定行为。

| 事件 | 必须消费字段 |
| --- | --- |
| `UserPromptSubmit` | `continue_flow`, `decision`, `reason`, `user_message`, `additional_context`, `updated_input`, `diagnostics` |
| `PermissionRequest` | `continue_flow`, `decision`, `reason`, `user_message`, `diagnostics` |
| `PreToolUse` | `continue_flow`, `decision`, `reason`, `user_message`, `updated_input`, `diagnostics` |
| `PostToolUse` | `updated_input`, `diagnostics` |
| `PostToolUseFailure` | `diagnostics` |
| `PostToolBatch` | `diagnostics` |
| `Stop` | `reason`, `user_message`, `diagnostics`, `artifacts` |
| `StopFailure` | `reason`, `user_message`, `diagnostics`, `artifacts` |

`PostToolBatch` 是批次后诊断事件，不消费 `continue_flow`、`decision`、
`updated_input`、`additional_context` 或 `artifacts`。这些字段如果出现在
`PostToolBatch` 输出中，runtime 必须产生明确 diagnostic。

`Stop` 和 `StopFailure` 是终态事件，消费 `reason`、`user_message`、
`diagnostics` 和 `artifacts`。它们不消费 `continue_flow`、`decision`、
`updated_input` 或 `additional_context`，也不能靠这些字段要求继续一轮或改写
模型上下文。需要恢复提示、失败说明、质量检查结论或总结引用时，必须使用终
态事件自己的 `user_message`、`reason`、`diagnostics` 和 `artifacts` 字段并
配套验收测试。

`updated_input` 只能用于明确支持的事件和工具。替换输入必须保留完整对象，不
允许只返回局部 patch 造成歧义。

工具调用的 provider/model 关联 id 不属于可改写输入。`PreToolUse` 和内部
HookRegistry transform 可以改写最终工具名和参数，但必须保留原始
`tool_call_id`，后续审批、工具结束事件和模型 tool result 都使用同一个原始
id。

### 错误语义

- 安全和权限相关 hook 失败按 fail-closed 处理。
- 观察、记录和通知类 hook 失败按 fail-open 处理，但必须写入诊断。
- 记忆注入默认 fail-open，除非 memory runtime policy 明确设置为
  fail-closed。
- Hook 输出必须有大小限制。超限内容写 artifact，只在事件中保留摘要和引用。

## Permission Boundary

权限网关是唯一最终裁决点。

Hook 可以：

- 补充权限请求说明；
- 建议允许或拒绝；
- 要求转为用户确认；
- 给模型提供失败后的可恢复建议；
- 标记输入被改写后的新风险。

Hook 不可以：

- 绕过权限网关直接执行工具；
- 绕过用户确认安装能力、MCP 或 Skill；
- 自行写入 Agent 授权；
- 在未审查状态下自动信任能力包 hooks；
- 用记忆 provider 或 MCP 工具暗中执行权限外动作。

`PreToolUse` 不再承担最终权限判断。工具请求先经过硬安全、Agent 边界、mode
和 effective_capabilities 等先验边界筛选；通过先验筛选的候选请求才进入
`PermissionRequest` lifecycle。权限网关负责合并 hook 建议、策略规则、Agent
能力范围、用户审批和管理员限制，并给出最终裁决。

## Runtime Visibility

SessionRun 是交互会话的用户观察、交互和历史恢复承载。后台运行事实使用
对应 runtime projection 和 AgentRun audit 承载；Taskflow 长期执行必须通过
TraceLink、source metadata、`parent_run_id`、`parent_session_id` 或同等引用
与发起会话、任务和工作项保持可追踪。

所有 hook 相关事实分三层：

```text
raw execution      原始命令输出、HTTP 响应、MCP 结果、诊断
semantic event     结构化生命周期事件和结果
presentation       ChatView、Settings 或 Taskflow runtime view 展示
```

ChatView 只消费 canonical SessionRun transcript。Settings 不能维护独立的能
力安装进度状态作为事实源。Settings 发起动作后，应显示同一个 SessionRun
状态或跳转到对应会话。

TaskflowView 消费 Taskflow runtime projection。它展示 TaskRun、AgentRun、
events、artifacts 和 liveness，并提供回到相关 ChatView 入口；ChatView 不承
载长期后台运行的原始事件流。

交互式 SessionRun 主时间线只展示：

- 当前正在做什么；
- 调用了什么工具；
- 哪个 hook 或能力要求确认；
- 能力包草案；
- 安装和验证结果；
- 可理解错误和恢复建议；
- 最终完成、失败、取消或中断状态。

内部路径、原始命令、完整 prompt、证据、stdout、traceback、hook 原始 JSON
进入技术详情或审计入口。

## Capability Package, Skill, and MCP Contract

能力包可以贡献：

```text
skills
mcp_servers
environment_requirements
prompt_fragments
credentials
memory_provider_adapters
memory_source_connectors
hooks
```

能力包 hooks 必须声明：

- 用户可读名称和说明；
- 来源能力包；
- 运行位置；
- 监听事件；
- matcher；
- 处理器类型；
- 权限需求；
- 需要的环境条件；
- 是否默认启用；
- 风险等级；
- 技术详情。

Skill 可以声明 hooks，但 Skill 本身不等于 hook。Skill 是模型可用的行为说明
和资源；hook 是生命周期动作。Skill hooks 必须作为 Skill frontmatter 或所属
能力包 manifest 的结构化字段进入同一注册表。

MCP server 可以声明 hooks 或作为 hook handler 被调用，但 MCP server 的工具
暴露、用户输入、权限请求、运行位置和凭据必须继续遵守 MCP 子系统契约。

单独安装 MCP 时，用户主流程应支持粘贴标准 MCP JSON；系统解析后补齐显示
名称、说明、运行位置、依赖和权限。高级字段进入技术详情。

## Memory Boundary

Memory Provider 的定位必须收敛为 MemoryRuntime 背后的读写适配器。

Memory Provider 不是：

- hook；
- 数据库概念；
- UI 功能入口；
- 自动注入策略；
- 权限裁决层。

MemoryRuntime 是记忆使用的唯一编排层。生命周期事件触发 MemoryRuntime，
MemoryRuntime 再根据 Agent、会话、用户、项目、provider 能力和策略决定：

- 是否读取记忆；
- 读取哪些 provider；
- 如何合并和排序；
- 如何控制 token；
- 是否包装外部不可信内容；
- 是否捕获工具结果；
- 是否捕获会话总结；
- 失败时 fail-open 还是 fail-closed；
- 写入哪些 SessionRun 诊断事件。

Provider 只负责：

```text
health
provide
capture
remember
forget
capabilities
```

如果能力包安装了 memory provider adapter，也只是在 provider registry 中增
加一个可用适配器；自动注入和捕获仍由 MemoryRuntime 和生命周期事件统一
控制。

MCP 记忆服务有两种身份：

1. 普通 MCP 工具，由模型显式调用。
2. Memory provider adapter，由 MemoryRuntime 自动调用。

这两种身份必须分开声明、分开授权、分开展示。

## Internal Runtime Adapter

内部 Python `HookRegistry` 继续存在，但只作为实现层。

目标执行链：

```text
hook declarations
  -> validation and trust review
  -> normalized lifecycle hook registry
  -> runtime adapters
  -> existing HookRegistry or event-specific runner
  -> SessionRun semantic events
```

现有内置 Python hooks 的目标归属：

| 当前能力 | 新定位 |
| --- | --- |
| 项目上下文注入 | 内置 `UserPromptSubmit` 或模型请求准备 adapter |
| 记忆上下文注入 | 内置 MemoryRuntime adapter |
| 工具输出截断 | 内置 `PostToolUse` adapter |
| 工具结果记忆捕获 | 内置 MemoryRuntime adapter |
| 会话保存记忆捕获 | 内置 MemoryRuntime adapter |
| LSP 诊断注入 | peer/server 位置明确的内置上下文 adapter |
| LSP 编辑后诊断 | 内置 `PostToolUse` adapter |

能力包、Skill 和 MCP 不能要求直接注册 Python 类。需要系统内置代码时，必须
先有公开 hook 声明，再由受信任的 internal adapter 执行。

## UI and Admin Surfaces

### Hooks 管理页

Settings 需要提供 hooks 管理视图，展示：

- hook 名称；
- 说明；
- 来源；
- 运行位置；
- 监听事件；
- 启用状态；
- 信任状态；
- 权限需求；
- 最近执行结果；
- 风险和凭据；
- 技术详情。

默认视图不展示内部 id、路径、完整命令、prompt 和原始 JSON；这些进入技术
详情。

### 能力管理页

能力管理页展示能力包、Skill、MCP、memory adapter 和 hooks 的关系：

- 这个能力是什么；
- 能做什么；
- 来自哪里；
- 在服务端还是本机运行；
- 是否需要用户本地安装；
- 是否携带 hooks；
- hooks 是否已信任；
- 是否可被 Agent 使用。

单独注册 Skill/MCP 不自动授予 Agent 使用权限。Agent 仍通过
`capability_refs` 获得能力包授权，或通过明确的用户管理路径授予单项资源。

### ChatView

ChatView 展示生命周期过程，不展示配置编辑器细节。用户看到的是：

- 系统识别到安装请求；
- 正在读取来源；
- 正在生成草案；
- 需要确认安装；
- 正在安装哪些组件；
- 哪些 hook 或本地配置需要信任；
- 验证结果；
- 最终可用状态。

## Configuration and Manifest Shape

目标 manifest 形态：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "display_name": "识别能力安装链接",
        "summary": "当用户发送 Skill、MCP 或能力包链接时启动安装会话",
        "matcher": "*",
        "placement": "server",
        "source": "capability_package",
        "trust": "pending_review",
        "hooks": [
          {
            "type": "internal",
            "handler": "capability_ingest_router",
            "permissions": ["capability.ingest.start"]
          }
        ]
      }
    ]
  }
}
```

目标能力包贡献形态：

```json
{
  "contributions": {
    "hooks": [
      {
        "event": "PostToolUse",
        "display_name": "记录部署结果",
        "summary": "部署工具完成后把结果写入项目上下文和审计",
        "matcher": "mcp__deploy__*",
        "placement": "server",
        "handler_type": "mcp_tool",
        "handler_ref": "mcp__audit__record",
        "permissions": ["audit.write"],
        "risk_level": "medium"
      }
    ]
  }
}
```

具体字段名可以在实现计划中转为 Python/TypeScript schema，但语义不得改变。

## Regression Boundaries

这轮改造的风险边界必须先用现有测试固定下来。后续实现生命周期 hooks 时，
不得为了通过新 schema 而削弱这些现有合同。

### P0 不变量

以下条目是生命周期 hooks 架构的交付前置条件，不是后续增强项。任一条没有
被代码和测试证明时，本轮 hooks 改造不得视为完成：

- 配置声明必须进入真实 Agent 运行链路。生产创建的 Agent 必须从当前
  `Config` 构建 lifecycle registry 和 dispatcher；测试不能只靠手动注入
  dispatcher 证明行为。
- 信任状态必须闭环。非系统 hooks 可以默认 `pending_review`，但必须存在
  `trusted`、`disabled`、`blocked` 的服务端状态变更入口、前端操作入口和
  运行时过滤验证；`pending_review` 不得被执行，也不得成为无流转死状态。
- `UserPromptSubmit` 必须完整处理 hook 输出语义。`updated_input`、
  `additional_context`、`decision=deny`、`decision=ask`、
  `continue_flow=false` 都必须有确定行为；拒绝必须阻断本轮，确认必须进入
  统一审批，继续执行必须留下 SessionRun 或审计事实。
- 会话事实必须统一。`UserPromptSubmit` 改写后，SessionRun 的用户消息、
  标题、`taskText`、ChatView 展示和模型实际输入必须使用同一个最终
  prompt；不得先写 `session_run_start` 再改写模型输入。
- 权限网关仍是唯一最终裁决点。Hook 可以建议、补充上下文或要求确认，但
  不能直接授予工具、能力、MCP 或 Skill 执行权。
- Settings 发起能力安装等交互式流程必须绑定 ChatView 可观察的同一个
  SessionRun；Settings 不允许维护独立安装进度事实源。
- Taskflow 等长期后台 AgentRun 必须写入 Taskflow runtime projection 和
  AgentRun audit；ChatView 只展示发起关系、关键状态、需用户处理的阻塞和
  最终结果。
- 普通会话行为不得被能力安装流程污染。工具渲染、审批、取消、中断和完成
  终态必须保持现有语义。

### 禁止交付条件

实现中出现以下任一情况时必须阻塞合入：

- hooks 只在配置、dashboard 或 Settings 中可见，普通 ChatView 会话不会触发。
- hooks 只在测试里通过手动 `lifecycle_dispatcher` 注入触发，默认 Agent 创建
  路径没有装配。
- 安装后的 hooks 统一写成 `pending_review`，但没有任何 UI/API 可以信任、禁用
  或阻止。
- `UserPromptSubmit` 返回拒绝、要求确认或 `continue_flow=false` 后，本轮仍
  静默进入模型。
- SessionRun 记录的用户输入与 LLM 实际收到的用户输入不同。
- 能力包安装引入独立后台状态源，绕开普通工具生命周期、审批或 SessionRun
  transcript。

### 术语边界

Hook 声明中的 `placement` 只使用：

```text
server
peer
both
```

能力运行足迹中的 `runtime_footprint.runs_on` 是用户展示和环境检查字段，可
继续使用：

```text
server
local_peer
both
agent_only
```

两者不能混用。`peer` 是 hook 执行位置；`local_peer` 是能力运行足迹里
“用户本地端需要安装或配置”的展示目标。解析标准 MCP JSON 时，可以把用户
输入中的 `peer` 归一到运行足迹的 `local_peer`，但公开 hook schema 不接受
`local`、`local_peer` 或其他同义词作为 `placement`。

`both` 不能实现为“等待 peer runtime 前整体不可执行”。它表示 server 和
peer 两侧都有动作：server 侧按 server runtime 独立判断，peer 侧按 peer
runtime 独立判断。Dashboard 必须展示两侧状态，不能把 peer 不可用折叠成
server 不可执行。

### Handler Runtime Adapter 总线

公开 schema 中出现的每一种 `handler_type` 都必须进入统一
`LifecycleHookRuntimeAdapterRegistry`。这个 registry 是 runtime adapter 的
唯一事实源，负责：

- 装载 adapter；
- 声明 adapter 支持的 `handler_type`、`placement`、事件和输出字段；
- 判断 dashboard 中的 `executable`、`placement_runtime` 和
  `unavailable_reason`；
- 为 `LifecycleHookDispatcher` 提供真实执行入口；
- 统一超时、失败、权限、审计和用户可见 runtime 投影。

`LifecycleHookDispatcher` 不能直接依赖临时 `dict[str, handler]` 作为生产合
同；dashboard 也不能使用另一套 handler 可用性判断。UI 显示“可执行”和运行
时真正会执行必须来自同一个 adapter registry。

公开 handler type 的完整推进目标如下：

| handler type | runtime adapter 合同 |
| --- | --- |
| `internal` | 调用系统内置 adapter，只能由核心代码、系统内置或管理员托管策略使用。 |
| `prompt` | 通过受控模型请求生成结构化 `LifecycleHookOutput`，可用于 `UserPromptSubmit` 改写、阻断、补充上下文和审批建议。 |
| `command` | 在声明位置执行命令，必须经过权限网关、运行位置校验、超时、输出截断和审计。 |
| `http` | 调用声明的 HTTP endpoint，必须经过权限网关、超时、响应大小限制、错误归一和审计。 |
| `mcp_tool` | 通过 MCP/tool gateway 调用工具，必须复用普通工具权限、审批、tool result 闭合，并按 source 投影到交互式 SessionRun 或后台 runtime audit。 |
| `agent` | 通过受控 AgentRun 或子 agent 调度执行，必须绑定上下文、预算、取消、中断和终态事件。 |

没有 adapter 的 handler type 不能被称为已完成，也不能在验收中被当作“可执行
生态”。完整推进要求所有公开 handler type 都有 adapter、装载路径、失败诊断、
权限边界和回归测试；如果某个底层服务尚未接入，ADR 必须把它列为阻塞项，
不能把它降级为“可声明但不可执行”的已交付能力。

### 必须保留的现有合同

- 普通会话工具、MCP 工具、Skill 调用和能力包安装共享工具生命周期，但普
  通会话现有工具渲染、审批和终态不能被能力包流程改坏。
- 权限网关仍是唯一最终裁决点；hook 或审批建议不能绕过 Agent 能力范围、
  execution policy、后台/交互边界和系统硬限制。
- Settings 发起交互式能力包流程后必须创建并绑定同一个 SessionRun；
  ChatView 能看到 session、workflow 摘要、审批、草案、安装结果和终态。
- 完成、失败、取消或中断后的 SessionRun 不能继续暴露 active thinking、
  pending approval 或 running workflow 作为当前状态。
- Memory hook adapter 只能调用 MemoryRuntime；provider 不能直接承载生命
  周期策略，也不能绕过 runtime 的 scope、budget、fail-open/fail-closed 和
  工具表面授权。
- 标准 MCP JSON 粘贴安装必须保留 command、args、env key 和运行位置展示；
  server/peer/both 的含义不能被 UI 或配置模型折叠成一个“已安装”状态。

### 当前回归测试门槛

后端：

```powershell
.\.venv\Scripts\python -m pytest tests/domain/test_permission_gateway.py tests/domain/hooks/test_memory_context_hook.py tests/domain/test_config_models.py tests/labrastro_server/services/test_capability_packages.py::test_capability_package_session_run_requests_install_approval_and_installs tests/labrastro_server/services/test_capability_packages.py::test_capability_package_session_cancel_during_install_approval_does_not_append_install_terminal_events tests/labrastro_server/services/test_capability_packages.py::test_capability_package_session_revision_feedback_revises_pending_draft tests/labrastro_server/http/test_remote_service.py::TestRemoteRelayHTTPService::test_session_run_done_resolves_registered_pending_approval tests/labrastro_server/http/test_remote_service.py::TestRemoteRelayHTTPService::test_session_run_cancel_resolves_registered_pending_approval tests/labrastro_server/http/test_remote_service.py::TestRemoteRelayHTTPService::test_approval_reply_routes_to_matching_session_run_only -q
```

其中关键边界包括：

- `test_mcp_tool_user_review_policy_blocks_background_runs`
- `test_execution_policy_deny_overrides_approval_allow`
- `test_approval_require_approval_never_waits_in_background`
- `test_memory_context_hook_delegates_policy_to_runtime`
- `test_runtime_merges_registered_provider_fragments_and_applies_budget`
- `test_runtime_memory_tools_use_configured_tool_provider`
- `test_peer_mcp_server_config_roundtrip`
- `test_mcp_server_config_accepts_both_placement`
- `test_capability_package_session_run_requests_install_approval_and_installs`
- `test_capability_package_session_cancel_during_install_approval_does_not_append_install_terminal_events`
- `test_session_run_done_resolves_registered_pending_approval`

前端：

```powershell
npx vitest run src/LabrastroController.admin.test.ts src/coordinators/SessionRunCoordinator.test.ts src/coordinators/SessionRunCoordinator.semantic.test.ts webview-ui/src/components/chat/transcript-presentation.test.ts webview-ui/src/settings/capabilityPackageView.test.ts webview-ui/src/settings/useSettingsController.test.tsx
```

其中关键边界包括：

- Settings 发起能力包安装时向 Settings 和 Sidebar/ChatView 共享
  `sessionRun.session`。
- `SessionRunCoordinator` 只持有 SessionRun active state，不恢复旧 chat id
  字段。
- transcript presentation 在能力包 workflow 已完成时，即使之前有 running
  step，也不能继续展示运行中。
- 标准 MCP JSON 默认展示服务端运行；显式 peer 运行足迹展示为本地端需要
  安装或配置。
- Settings 能从同一 SessionRun 事件投影能力包安装状态，不保留独立轮询草
  案事实源。

## Implementation Path

1. 新增 lifecycle hook domain models、schema 和 validator。
2. 新增 hook source、placement、trust、handler type、event name 的权威常量。
3. 新增公开 hook registry，存储声明式 hooks，不暴露 Python 类。
4. 新增 `LifecycleHookRuntimeAdapterRegistry`，作为 handler runtime adapter
   的唯一事实源。
5. 将 dashboard/executable 判断、placement runtime 判断和 dispatcher 调度
   统一改为读取 adapter registry，删除生产链路中的裸 handler dict 事实源。
6. 将现有内置 Python hooks 迁移为 `internal` adapter。
7. 实现 `prompt` adapter，通过受控模型请求生成结构化 `LifecycleHookOutput`。
8. 实现 `command` adapter，通过权限网关、运行位置、超时、输出截断和审计
   执行命令。
9. 实现 `http` adapter，通过权限网关、超时、响应大小限制、错误归一和审计
   调用 HTTP endpoint。
10. 实现 `mcp_tool` adapter，通过 MCP/tool gateway 复用普通工具权限、审批、
    tool result 闭合，并按 source 投影到交互式 SessionRun 或后台 runtime
    audit。
11. 实现 `agent` adapter，通过受控 AgentRun 或子 agent 调度，绑定上下文、
    预算、取消、中断和终态事件。
12. 建立 lifecycle event dispatcher，把用户输入、工具、权限、子任务、压缩、
   SessionRun、MCP elicitation、环境变化接入统一事件。
13. 将权限请求改为先经过先验安全/能力边界筛选，再让候选请求生成
   `PermissionRequest` 生命周期事件，最后交给权限网关汇总裁决。
14. 将能力包、Skill、MCP manifest 中的 hooks 纳入声明解析、校验、信任审查
   和 Settings 展示。
15. 将能力包安装流程改为普通工具生命周期和 SessionRun 草案事件，不保留旧
   轮询草案或隐藏后台状态作为事实源。
16. 将 MemoryRuntime 作为记忆 hook adapter 的唯一入口，禁止 provider 自行
   注册生命周期动作。
17. 增加 ChatView/Settings 对同一 SessionRun 的绑定展示。
18. 删除或废弃与目标架构冲突的旧 hook 文档、旧测试和旧配置路径。

## Test Plan

- Domain tests:
  - 所有事件名、来源、运行位置、处理器类型、信任状态均由权威常量校验。
  - 非法 placement、未知事件、未知 handler type 被拒绝。
  - 能力包 hooks 缺少显示名称、说明、运行位置或权限摘要时被拒绝。
  - `local` placement 被拒绝。
  - `LifecycleHookRuntimeAdapterRegistry` 是 adapter 可用性的唯一事实源。
  - dashboard `executable`、`placement_runtime`、`unavailable_reason` 与
    dispatcher 使用同一 adapter registry。
  - 所有公开 handler type 都有 adapter 合同测试：`internal`、`prompt`、
    `command`、`http`、`mcp_tool`、`agent`。
- Permission tests:
  - 通过先验安全/能力边界的候选工具审批前产生 `PermissionRequest` 生命周期事件。
  - 硬拒绝、Agent 边界、mode 边界和 effective_capabilities 拒绝不产生
    `PermissionRequest` lifecycle dispatch。
  - hook 建议不能绕过权限网关。
  - 多个 hook 决策合并时拒绝优先于允许。
  - 未信任 hook 不参与权限放行。
  - `command`、`http`、`mcp_tool`、`agent` adapter 都不能绕过权限网关、Agent
    能力范围、execution policy、后台/交互边界和系统硬限制。
- SessionRun tests:
  - 默认 Agent 创建链路从 Config 装配 lifecycle dispatcher；trusted
    `UserPromptSubmit` 能在真实 `agent.chat()` 路径触发。
  - `UserPromptSubmit` 改写输入后，SessionRun userMessage/title/taskText 与
    LLM messages 使用同一个最终 prompt。
  - `UserPromptSubmit` 返回 `deny`、`ask` 或 `continue_flow=false` 时不会继续
    静默调用模型。
  - Settings 发起能力安装后 ChatView 可恢复同一 SessionRun。
  - 能力包草案通过结构化 SessionRun event 展示。
  - 交互式 SessionRun 内的 hook 执行、审批、安装、验证、失败和终态均写入
    canonical transcript。
  - 原始命令、stdout、prompt 和证据不进入 assistant 正文。
- Taskflow runtime tests:
  - Taskflow 发起的长期 AgentRun 写入 Taskflow runtime projection 和
    AgentRun audit，不要求原始事件进入 ChatView transcript。
  - TaskRun 与 AgentRun 通过 TraceLink、source metadata 或等价引用保持可
    追踪。
  - 后台 source 遇到交互审批需求时转为 `blocked_review`、`blocked` 或
    `needs_attention`，不能进入长期等待 ChatView 审批的状态。
- Runtime adapter tests:
  - `internal` adapter 只能执行系统内置和管理员托管声明，能力包不能伪造
    internal handler。
  - `prompt` adapter 可改写输入、阻断输入、追加上下文，并保证 SessionRun、
    ChatView 和 LLM messages 使用同一个最终 prompt。
  - `command` adapter 覆盖命令白名单/权限、placement、timeout、stdout/stderr
    截断、退出码、失败诊断和取消。
  - `http` adapter 覆盖 URL/方法/headers/body 限制、timeout、响应大小限制、
    失败诊断和敏感信息脱敏。
  - `mcp_tool` adapter 覆盖 MCP server/tool 解析、普通工具审批复用、tool
    result 闭合、失败诊断和交互式 SessionRun 投影。
  - `agent` adapter 覆盖子 AgentRun 创建、上下文绑定、预算、取消、中断、
    失败终态、父运行追踪和按 source 选择的 runtime 投影。
  - trusted 才执行；`pending_review`、`disabled`、`blocked` 均不执行。
  - server/peer/both placement 按两侧 runtime 独立判断和独立投影。
- Capability tests:
  - 能力包携带 hooks 后进入 hook registry 且状态为 pending review。
  - hook trust 可从 `pending_review` 改为 `trusted`、`disabled`、`blocked`；
    registry 只执行 `trusted`。
  - Skill frontmatter hooks 被解析为声明式 hooks。
  - 标准 MCP JSON 安装后能展示运行位置、依赖、权限和 hooks 风险。
  - 单独 MCP/Skill 注册不自动授予 Agent capability_refs。
- Memory tests:
  - lifecycle hooks 只调用 MemoryRuntime，不直接调用 provider。
  - provider 不能自行注册公开 lifecycle hook。
  - MCP memory tool 和 memory provider adapter 身份分离。
  - provider 失败按 runtime policy 写诊断。
- Frontend tests:
  - hooks 管理页显示来源、运行位置、事件、信任状态和权限摘要。
  - 能力管理页显示能力是否携带 hooks、是否需要服务端/本地配置。
  - 技术详情折叠后才显示原始命令、路径、prompt 和 JSON。
  - ChatView 展示能力安装全流程。

## Acceptance Criteria

- `../docs/hook-system.md` 不再被实现计划、测试或代码注释作为目标架构依据。
- 公开 hooks 契约是声明式生命周期协议。
- 能力包、Skill、MCP、用户配置和管理员策略使用同一 hook schema。
- 每个 hook 都有来源、运行位置、处理器类型、信任状态、权限摘要和用户可读
  说明。
- `HookRegistry` 只作为内部执行 adapter，不作为能力包或用户扩展接口。
- 所有公开 handler type 都通过 `LifecycleHookRuntimeAdapterRegistry` 装载、
  判定和执行：`internal`、`prompt`、`command`、`http`、`mcp_tool`、`agent`。
- Settings/dashboard 的 `executable` 与真实运行时执行能力一致。
- 任一公开 handler type 只有 schema 和 UI、没有 adapter、权限边界和测试时，
  lifecycle hooks 改造不得视为完成。
- Settings 发起能力包等交互式流程后 ChatView 可观察完整 SessionRun。
- Taskflow 等长期后台 AgentRun 通过 Taskflow runtime projection 和 AgentRun
  audit 可观察，ChatView 保留入口、关键状态、需用户处理的阻塞和终态，不承
  载长期原始事件流。
- 能力包安装不再有独立隐藏流程；它复用工具生命周期、权限事件和
  SessionRun 草案事件。
- 权限裁决仍由统一权限网关完成。
- MemoryRuntime 是记忆注入和捕获唯一编排层。
- Memory Provider 不拥有生命周期策略，不直接注册公开 hooks。
- MCP elicitation 进入统一用户输入/审批/审计流程。
- 服务端路径、本地 peer 路径、AgentRun worktree 路径不混用。
- 未信任 hook 不执行高风险动作。
- 用户可以在 UI 中理解一个 hook 来自哪里、做什么、在哪运行、是否可信。
- 技术细节可追溯，但不污染主时间线。

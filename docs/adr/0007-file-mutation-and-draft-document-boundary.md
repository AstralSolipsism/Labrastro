# ADR 0007: Agent 文件变更与长文档草稿边界

## Status

Accepted.

## Context

Labrastro 当前继承了 ReuleauxCoder 的文件工具模型：

```text
write_file(file_path, content)        # 创建或完整覆盖文件
edit_file(file_path, old_string, new_string)  # 精确字符串替换
```

这组工具按文件系统动作区分“新增/覆盖”和“修改”，但不适合作为 Labrastro
长期的 Agent 文件变更架构。原因是它把这些语义混在同一条工具调用链里：

```text
模型生成正文
工具参数传输
权限审批
文件变更
UI 可见过程
审计回放
```

当模型写长 Markdown、架构文档或报告时，`write_file.content` 会把完整正文放进
工具参数。工具参数没有生成完之前，运行时不能真正执行写文件；UI 只能看到
`tool_call_delta/preparing`，看不到正文。Labrastro 还会把工具参数流投影为
SessionRun 事件，这会放大高频参数碎片造成的 UI、存储和取消语义问题。

项目仍处于开发期。本 ADR 不保留旧文件编辑工具作为模型可见兼容层；目标是一次性
收敛文件变更协议，避免 `write_file`、`edit_file`、`apply_patch`、文档 artifact
平级共存导致模型漂移。

本 ADR 已按 Codex 的文件变更架构定稿。Codex 的可参考边界是：

| 层级 | Codex 对标 | Labrastro 决策 |
| --- | --- | --- |
| 模型可见文件变更入口 | `apply_patch` freeform / grammar | 只暴露 `apply_patch` |
| 工具参数实时解析 | `StreamingPatchParser.push_delta()` | 增量解析 patch 并输出 file change 状态 |
| 文件变更运行时对象 | `FileChange` | 引入 `fileChange` 等价事件模型 |
| patch 实时通知 | `item/fileChange/patchUpdated` | 引入 `file_change_patch_updated` |
| patch 生命周期 | `inProgress / completed / failed / declined` | 映射为 Labrastro snake_case 状态 |
| 整轮 diff | `turn/diff/updated` | 引入 `turn_diff_updated` |
| UI 行数摘要 | unified diff 计算 `+N/-N` | 前端从 diff 计算行数 |
| 旧文本输出协议 | `outputDelta` deprecated | 不新增旧式 raw output 协议 |

`draft_document_begin` 是 Labrastro 的产品扩展，不是 Codex 已有文件变更协议。它只用于
长文档正文可见生成，必须服从同一原则：模型只声明目标，runtime 管状态、取消和提交。

## Product Rules

- 模型可见的文件变更入口只有 `apply_patch`。
- 模型可见的长文档生成入口只有 `draft_document_begin`。
- `write_file` 和 `edit_file` 作为命名能力彻底删除，不保留 runtime 同名 helper。
- 文件系统实际写入只允许通过运行时内部服务完成，模型不能直接传完整大正文给写盘原语。
- `shell` 不是文件编辑协议；不得用 shell 的 echo、heredoc、`Set-Content`、
  `Out-File`、脚本片段等方式绕过 `apply_patch` 或 draft document commit。
- 工具参数流不是正文流。正文必须通过 `assistant_delta` 或 draft document 通道可见。
- `tool_call_delta` 是 live 状态，不是 durable transcript event。
- 文件变更 UI 必须使用 `fileChange` / diff / `+N/-N`，不展示原始工具 JSON。
- 开发期不做旧工具 alias、兼容迁移或双轨 schema。

## Core Boundary

目标架构按运行语义分层，不按“新增/修改”或“长短”分层。

```text
文件变更协议       apply_patch
长正文产物协议     draft_document
内部落盘能力       FileMutationService / filesystem adapter，不保留 write_file/edit_file 命名
实时正文展示       assistant_delta
文件变更展示       fileChange + patchUpdated + turnDiff
工具运行输出       tool_call_stream
工具准备状态       tool_call_delta live state
```

### 文件变更协议

`apply_patch` 表示“如何改变文件系统中的文本文件”。

它覆盖：

```text
新增小文件
修改已有文件
删除文件
重命名文件
多处上下文修改
```

它不覆盖：

```text
生成长正文
隐藏传输完整文件内容
不经 diff 的大文件覆盖
```

### 长正文产物协议

`draft_document_begin` 表示“接下来模型要生成一份用户可阅读的文档草稿，并在完成后由
运行时提交到目标文件”。

它覆盖：

```text
架构文档
设计说明
报告
长 Markdown
用户需要实时看到和审阅的正文
```

它不覆盖：

```text
代码 patch
局部文件修改
工具参数传输
直接写盘
```

长度阈值只作为运行时保护，不是架构边界。默认建议：

```text
apply_patch.patch <= 64 KiB
单个 Add File 内容 <= 32 KiB
draft_document 正文不使用工具参数传输，按 assistant stream 捕获
```

这些阈值未来可以按模型能力调整，但语义边界不能改变：

```text
改文件 -> apply_patch
生成长正文 -> draft_document
写盘 -> workspace owner internal commit
```

## Workspace Mutation Ownership Boundary

文件变更的最终所有权不属于 LLM，也不属于远端会话编排层。所有权属于实际承载
workspace 文件系统的执行端。

```text
LLM                         只声明意图：apply_patch / draft_document_begin
AgentLoop                   只编排：stream、draft、approval、event
SessionRun service          只持久化：event、approval、artifact、trace
Workspace mutation owner    唯一可 preview / plan / commit 的文件系统 owner
UI                          只展示：fileChange、diff、draft、approval
```

`Workspace mutation owner` 的判定规则：

```text
local_workspace / server-owned workspace    owner = server/local process
remote_peer workspace                       owner = remote peer process
agent_run worktree                          owner = agent-run runtime worktree process
```

硬约束：

- Server 不得用 peer 上报的 `cwd` / `workspace_root` 字符串直接构造本机
  `FileMutationService` 并读写文件。
- 远端 workspace 的 preview、path resolution、old state snapshot、diff、approval-bound
  state validation、commit 必须在 remote peer process 内完成。
- `AgentLoop` 和 `DocumentDraftRuntime` 不得直接假设自己拥有文件系统；它们只能调用
  注入的 `WorkspaceMutationBackend`。
- `FileMutationService` 是 owner 内部实现，不是跨进程边界。跨进程边界必须传
  `MutationPlan` / `MutationPlanRef`，不能传本机 `Path` 语义。
- 文件变更 event 可以由 server 统一发布，但 event 的 `changes`、`diff`、old state、
  result 必须来自 workspace owner。

### `MutationPlan`

`MutationPlan` 是 preview、approval 和 execute 的唯一绑定对象。不得继续用单个
`resolved_path` 表达一次文件变更。

```text
MutationPlan
  plan_id
  tool_name: apply_patch | draft_document_commit
  workspace_id
  execution_target: local_workspace | remote_peer | agent_run_worktree
  path_space
  operations[]
    kind: add | update | delete | move | document_commit
    path
    move_path?
    real_path_fingerprint
    old_exists
    old_sha256
    old_size
  changes[]
    path
    kind
    move_path?
    diff
  combined_diff
  grammar_version
  plan_hash
```

规则：

- preview 生成 `MutationPlan`。
- approval 显示 `MutationPlan.changes` / `combined_diff`。
- approval 通过后 execute 必须携带 `plan_id` 或完整 `MutationPlanRef`。
- execute 必须重新读取每个 operation 的 old state，并校验 `plan_hash` 与 per-file
  old state。
- 多文件 patch 必须作为一个 plan 被批准和执行；禁止 preview 支持多文件而 execute
  只支持单文件。
- 单文件旧 `ToolMutationPreviewState(resolved_path, old_sha256, old_exists, old_size)`
  只能作为迁移前状态，不得作为完成态。
- plan 失败不得写入任何文件；如果底层文件系统无法提供原子提交，必须实现 staging
  或 rollback。

### Draft Document Commit Boundary

`draft_document_begin` 的正文通过 assistant stream 捕获，但写盘仍必须归属 workspace
owner。

```text
draft_document_begin
  -> assistant_delta captures visible body
  -> draft runtime requests owner.preview_document_commit
  -> owner returns MutationPlan(tool_name=draft_document_commit)
  -> approval binds to plan_id / plan_hash
  -> owner.commit_plan(plan_id)
  -> fileChange completed
```

远端模式下，draft commit 的正文可以通过 server-to-peer 内部协议发送给 owner 生成
plan；这不是模型工具参数，也不能进入 LLM tool schema。禁止把 peer path 当成本机
path 直接调用 `FileMutationService.commit_document`。

### Parser and Owner Parity

Python 与 Go 可以各自实现执行端，但不得各自定义语义。

- patch grammar 必须有一套 golden fixtures。
- Python `FileMutationService` 与 Go remote runner 必须通过同一批 grammar、path、
  diff、error、transaction fixture。
- `Add File`、`Update File`、`Delete File`、`Move to`、空 hunk、坏前缀、重复上下文、
  symlink escape、multi-file plan 的行为必须一致。
- 任一执行端不支持某个合法 plan 时，preview 阶段必须拒绝，不能等 approval 后失败。

## Model-Visible Tools

### `apply_patch`

模型可见的唯一文件变更工具。

目标形态对标 Codex：优先使用 freeform / grammar tool，模型直接输出 patch 文本，
不包 JSON。

如果当前 provider transport 只能使用 JSON schema，`{"patch": "..."}`
只允许作为传输适配层形状。公开合同是 patch grammar，不是 JSON 字段形状。

```json
{
  "patch": "*** Begin Patch\n*** Update File: docs/example.md\n@@\n-old\n+new\n*** End Patch\n"
}
```

当 provider 支持 freeform/custom tool 时，必须复用同一 patch grammar，不能新增
第二套编辑协议。

JSON schema transport 必须实现专门的 `PatchArgumentStreamDecoder`：

- 输入是 provider 的 tool argument delta。
- 输出是连续 patch 文本 delta，而不是完整 JSON 参数对象。
- decoder 只能识别 `patch` 字段，不能新增 `content`、`old_string`、`new_string`
  等第二套编辑字段。
- decoder 必须在 `patch` 字符串尚未完整结束时向 patch parser 推送可解析片段。
- decoder 失败时产生 `tool_call_protocol_error` 和 `file_change_completed(status=failed)`。
- 禁止等待完整 `{"patch": "..."}` 参数结束后才创建 fileChange；否则实时 diff、
  审批预览和 liveness 边界都无法成立。

Patch grammar 目标形态：

```text
*** Begin Patch
*** Add File: path/to/file.md
+new line
*** Update File: path/to/file.py
@@
-old line
+new line
*** Delete File: obsolete.txt
*** End Patch
```

规则：

- 文件路径必须是相对 workspace root 的路径。
- 禁止绝对路径、`..` 越界、目录删除和二进制写入。
- `Update File` 必须有足够上下文定位。
- parser 必须先验证，再进入权限审批。
- 审批预览使用结构化 diff，不展示原始工具 JSON。
- 执行结果必须包含变更文件列表、diff 摘要、失败原因和应用耗时。
- 运行时必须在 tool argument delta 阶段增量解析 patch，输出 `file_change_patch_updated`。
- UI 必须从 diff 计算 `+N/-N`，不得从工具参数文本推断。
- `ApplyPatchTool` 只负责模型工具入口，内部实现必须调用
  `FileMutationService.apply_text_patch`；不得复用 taskflow/project memory 等
  现有 unrelated `apply_patch` 方法。

### `draft_document_begin`

模型可见的唯一长文档生成工具。它只声明目标，不接收正文内容。

参数：

```json
{
  "target_path": "docs/architecture.md",
  "title": "Architecture",
  "format": "markdown"
}
```

规则：

- `target_path` 必须是相对 workspace root 的文本文件路径。
- `format` 初始只允许 `markdown`。
- 工具返回 `draft_id` 后，下一段 assistant 正文流被运行时绑定到该 draft。
- UI 通过现有 `assistant_delta` 实时展示正文。
- assistant 正文完成后，运行时生成目标文件 diff，并进入 draft commit 阶段。
- draft commit 是文件变更，必须使用 fileChange diff 和审批链路。
- 用户批准后，运行时内部 commit 到 `target_path`。
- commit 产生 durable event：`document_draft_committed` 和对应
  `file_change_completed(status=completed)`。
- 用户拒绝、取消或审批超时后，必须产生 `document_draft_cancelled` 或
  `document_draft_failed`，不得写入目标文件。
- 取消、中断或 provider stream 失败时不得写入目标文件；必须产生
  `document_draft_cancelled` 或 `document_draft_failed`。

`draft_document_begin` 不是通用 artifact 工具。`artifact` 是内部存储和回放基座；
模型侧只使用 `draft_document_begin` 这个受控文档草稿协议。

## Internal Services

### `FileMutationService`

新增内部服务，负责所有文件系统文本变更。模型不可见。

职责：

- 解析并应用 patch。
- 提交 draft document 到文件。
- 创建父目录。
- 校验 workspace 边界。
- 读取旧内容并生成 diff。
- 统一处理换行、编码、错误和审计 metadata。

禁止职责：

- 不生成模型正文。
- 不决定工具选择。
- 不直接处理 provider stream。
- 不作为模型 tool 注册。
- 不复用 `ProjectMemoryService.apply_patch` 或任何 taskflow memory patch 逻辑。
- 不暴露 `write_file`、`edit_file`、`replace_in_file` 等旧工具命名。

### `DocumentDraftRuntime`

新增运行时组件，负责 draft document 的生命周期。

状态：

```text
idle
declared
streaming
committing
committed
cancelled
failed
```

职责：

- 接收 `draft_document_begin` 的目标声明。
- 绑定后续 assistant stream。
- 缓存完整正文到 draft buffer。
- 在 assistant message 完成时生成目标 diff 和 commit request。
- commit request 必须创建 fileChange item，并通过 fileChange approval 决定是否写盘。
- 审批通过后调用 `FileMutationService.commit_document`。
- 在 SessionRun 中投影 draft 状态。
- 在取消或中断时阻止写盘。

禁止职责：

- 不解析 patch。
- 不提供任意 artifact 写入工具。
- 不让模型直接调用 commit。

## Removed Model-Visible Tools

### `write_file` / `edit_file`

从模型可见 builtin tools、remote peer features、prompt、capability catalog、
approval policy、前端文案和测试 fixture 的公共能力列表中移除。

不保留 `write_file` / `edit_file` 作为命名能力。底层仍需要写入文本、替换文本、
创建目录和生成 diff，但这些能力必须迁移并重命名为：

```text
FileMutationService.apply_text_patch
FileMutationService.commit_document
filesystem adapter text write/read methods
```

禁止保留以下形态：

```text
write_file(...)
edit_file(...)
builtin_tool:write_file
builtin_tool:edit_file
remote feature: write_file
remote feature: edit_file
approval policy: write_file/edit_file
prompt: use write_file/edit_file
```

小范围替换、新增文件、删除文件、重命名文件全部通过 `apply_patch` 表达。

## Event Architecture

SessionRun 事件必须按用途分层，并对标 Codex 的 `fileChange` 协议。

### File Change Events

文件变更事件是文件修改 UI 和审批的唯一输入源。

| Event | Durable | 用途 | Payload 要点 |
| --- | --- | --- | --- |
| `file_change_started` | yes | 创建 fileChange item | `item_id`, `changes`, `status=in_progress` |
| `file_change_patch_updated` | live/coalesced | patch 参数流增量解析结果 | `item_id`, `changes`, `updated_at` |
| `file_change_approval_requested` | yes | 请求用户批准文件变更 | `approval_id`, `item_id`, `reason` |
| `file_change_approval_resolved` | yes | 记录批准、拒绝或取消 | `approval_id`, `item_id`, `decision` |
| `file_change_completed` | yes | 文件变更终态 | `item_id`, `changes`, `status`, `error?` |
| `turn_diff_updated` | coalesced | 整轮聚合 diff | `unified_diff` |

文件变更状态枚举固定为：

```text
in_progress
completed
failed
declined
cancelled
```

前端、后端、持久化和测试必须使用同一组 snake_case 状态值。不得混用
`inProgress`、`done`、`success`、`rejected` 等临时命名。

`changes` 的结构固定为：

```json
{
  "path": "src/example.ts",
  "kind": "add|update|delete|move",
  "diff": "@@\n-old\n+new\n",
  "move_path": "src/new-name.ts"
}
```

前端只能从 `changes[].diff` 计算 `+N/-N`。审批、回放和最终摘要都使用同一组
`fileChange` 数据。

文件变更不得使用通用 `approval_request` 作为 UI 或回放数据源。通用
`approval_request` 只服务非文件工具；如果底层需要兼容旧 approval envelope，
也必须由 runtime 映射为 `file_change_approval_requested` 后再进入前端和
SessionRun 回放。

### Durable Events

必须持久化、可回放：

```text
session_run_start
assistant_message
file_change_started
file_change_approval_requested
file_change_approval_resolved
file_change_completed
tool_call_protocol_error
approval_request
approval_resolved
document_draft_started
document_draft_commit_requested
document_draft_committed
document_draft_failed
document_draft_cancelled
session_run_end
session_run_failed
session_run_cancelled
```

### Coalesced Stream Events

可以持久化，但必须合并：

```text
assistant_delta
reasoning_delta
tool_call_stream
turn_diff_updated
```

### Live-Only State

不作为长期 transcript event 持久化：

```text
tool_call_delta
```

`tool_call_delta` 只表达“工具参数正在准备”。它必须按 `tool_call_id` 或
`session_run_id + index` 覆盖更新，最多节流为 500ms 一次 UI 状态推送。

状态字段：

```json
{
  "tool_name": "apply_patch",
  "status": "preparing",
  "received_chars": 1200,
  "preview_head": "*** Begin Patch...",
  "preview_tail": "...*** End Patch",
  "updated_at": 1234567890
}
```

长正文不得通过 `tool_call_delta` 展示。

`tool_call_delta` 不能作为文件变更 UI 的数据源。对 `apply_patch` 来说，
运行时必须把 patch delta 解析成 `file_change_patch_updated`。

### Artifact Envelope Boundary

大 payload artifact 化只能转移重内容，不能替换事件 envelope。

事件 payload 中必须保留以下字段，不能只剩 `artifact_ref`：

```text
event_id
session_id
session_run_id
type
item_id
tool_call_id
approval_id
draft_id
status
created_at
updated_at
```

artifact 只能承载大字段：

```text
diff
content
raw_args
tool_output
diagnostics_blob
```

`file_change_*`、`document_draft_*`、`approval_*` 事件的身份、状态、审批关系和
回放顺序必须直接存在于 durable event payload。前端不得为理解事件生命周期而先
反查 artifact。

## Provider Stream Liveness Boundary

Provider stream 是有生命周期的输入流，不是无限等待的后台任务。

必须实现以下边界：

| Boundary | 默认值 | 触发后行为 |
| --- | --- | --- |
| total stream wall time | 600s | 中断 provider stream，生成 failed/interrupted event |
| no-event idle time | 120s | 中断 provider stream，生成 failed/interrupted event |
| tool argument chars | 128 KiB | 中断当前工具调用，要求模型改用正确协议 |
| apply_patch patch chars | 64 KiB | 拒绝 patch，要求拆分或改用 draft document |
| draft document target declaration | 4 KiB | 拒绝声明，禁止正文进入参数 |

边界语义：

- 如果 stream 已经进入 `apply_patch` 参数流，超限后产生 `file_change_completed`
  且 `status=failed`。
- 如果 stream 已经进入 draft document 正文流，超限或中断后产生
  `document_draft_failed`，不得写入目标文件。
- 如果 stream 只有普通 assistant 正文，超限后产生 `session_run_interrupted`，
  已可见正文保留为 assistant stream。
- 所有中断都必须通知前端，不允许只写 diagnostics 文件。
- `provider.timeout_sec` 不能被解释为整轮 stream 的唯一超时保护；运行时必须有
  自己的 wall time 和 idle time supervisor。
- diagnostics 必须记录 `session_id`、`session_run_id`、provider、model、phase、
  duration、chunk_count、last_event_at、partial_kind、tool_name、received_chars。
- `StreamSupervisor` 是共享 provider 边界，必须覆盖 OpenAI Chat、OpenAI Responses、
  Anthropic Messages、labrastro_server adapter、agent_runs route 和 model_bridge。
  不允许只在 DeepSeek 或单个 provider adapter 内实现局部超时。

这条边界直接覆盖 2026-06-11 线上日志暴露的问题：DeepSeek SSE 在 `write_file`
工具参数流阶段持续约 2 小时 52 分，最终以 incomplete chunked read 断开。

## Shell Boundary

`shell` 保留为运行命令、测试、构建、git 查询和环境检查工具。

禁止用于手工编辑项目文本文件：

```text
cat > file
cat <<EOF > file
echo ... > file
printf ... > file
tee file
Set-Content
Out-File
Add-Content
python -c "...open(..., 'w')..."
node -e "...writeFileSync..."
```

允许的文件变化：

- 构建工具生成的输出。
- 测试运行产生的缓存或报告。
- 包管理器按用户请求更新 lockfile。
- git 操作按用户请求更新工作区。

这些允许项不是手工编辑协议。源码、文档和配置文本变更仍必须走
`apply_patch` 或 `draft_document` commit。

拦截责任属于 approval/preflight command classifier，而不是 UI 提示文案。
classifier 必须识别 PowerShell、bash、Python、Node 常见写文件形态，并在执行前
拒绝。拒绝结果必须包含恢复建议：使用 `apply_patch` 或 `draft_document_begin`。

## Implementation Landing Points

### Backend Tools

- Modify: `reuleauxcoder/extensions/tools/registry.py`
  - 保持 builtin 自动导入机制。
  - 确保 `write_file`、`edit_file` 不再注册为模型工具。
  - 确保旧工具文件删除或迁移后不会被自动导入。

- Create: `reuleauxcoder/extensions/tools/builtin/apply_patch.py`
  - 注册 `ApplyPatchTool`。
  - 参数只接受 `patch`。
  - 调用 `FileMutationService.apply_text_patch`。
  - 不调用 taskflow/project memory 的任何 `apply_patch` 方法。

- Create: `reuleauxcoder/extensions/tools/builtin/draft_document.py`
  - 注册 `DraftDocumentBeginTool`。
  - 参数只接受 `target_path`、`title`、`format`。
  - 不接受 `content`。

- Create: `reuleauxcoder/domain/files/file_mutation_service.py`
  - 内部文本文件变更服务。
  - 对外方法只允许 `apply_text_patch`、`commit_document` 和必要的私有 helper。

- Create: `reuleauxcoder/domain/files/patch_argument_stream_decoder.py`
  - 将 JSON schema transport 的 tool argument delta 解码为 patch 文本 delta。
  - 只允许 `patch` 字段，不接受正文型字段。
  - 在完整 JSON 尚未结束时向 patch parser 推送可解析文本。

- Create: `reuleauxcoder/domain/agent/document_draft.py`
  - draft lifecycle 状态、buffer、commit 决策。

- Remove or de-register:
  - `reuleauxcoder/extensions/tools/builtin/write.py`
  - `reuleauxcoder/extensions/tools/builtin/edit.py`
  - `reuleauxcoder-agent/internal/runner/runner.go` feature list 中的 `write_file`
    和 `edit_file`。
  - `reuleauxcoder-agent/internal/tools` 中对外暴露的旧工具执行分支。
  - remote protocol/contract 中暴露旧工具能力的类型、fixture 和 mock。

### Prompt and Capability Catalog

- Modify: `reuleauxcoder/services/prompt/builder.py`
  - 删除 “edit_file for small changes / write_file for new files”。
  - 新增 “file changes use apply_patch only”。
  - 新增 “long documents use draft_document_begin, then assistant markdown stream”。
  - 新增 “do not write files through shell”。

- Modify: `reuleauxcoder/domain/config/models.py`
  - 删除 `builtin_tool:write_file` 和 `builtin_tool:edit_file`。
  - 新增 `builtin_tool:apply_patch`。
  - 新增 `builtin_tool:draft_document_begin`。
  - remote peer feature 不再声明 `write_file` 或 `edit_file`。

- Modify: `reuleauxcoder/services/config/loader.py`
  - 默认 approval policy 从旧工具迁移为 `apply_patch` 与 `draft_document_begin`。
  - 不保留旧工具 policy alias。

- Modify: `config.yaml.example`
  - 默认 approval rules 不再列出 `write_file` 或 `edit_file`。
  - 示例注释不再出现旧工具名，除非位于历史 ADR 或 prohibited-state 测试说明。

### Remote Relay and SessionRun

- Modify: `reuleauxcoder/domain/agent/loop.py`
  - 将 draft runtime 接入 assistant stream 与 message completion。
  - 在 provider stream 取消时停止 draft commit。
  - 将 apply_patch argument delta 接入 `PatchArgumentStreamDecoder` 和 patch parser。
  - draft 正文完成后只发起 commit request；审批通过前不得写盘。

- Modify: `reuleauxcoder/services/providers/tool_call_delta.py`
  - 输出 live state 所需字段。
  - 不再把每个 delta 当作 durable event payload。
  - 对 `apply_patch` 只提供准备状态，文件变更内容由 patch parser 产出。

- Modify: `reuleauxcoder/services/providers/stream_supervisor.py`
  - 增加 total wall time、idle time、tool argument chars 边界。
  - 超限时返回结构化 interrupted/failed 信息。
  - 该逻辑由共享 supervisor 提供，所有 provider adapter 通过同一路径生效。

- Modify: `reuleauxcoder/interfaces/entrypoint/remote_relay.py`
  - `TOOL_CALL_DELTA` 不再直接 `append_event("tool_call_delta")`。
  - 改为 live state update 或节流状态事件。
  - 产生 `file_change_started`、`file_change_patch_updated`、
    `file_change_approval_requested`、`file_change_approval_resolved`、
    `file_change_completed`。
  - 文件变更不得使用通用 approval UI 数据源。
  - draft document 产生 started/commit_requested/committed/failed/cancelled durable events。

- Modify: `labrastro_server/interfaces/http/remote/service.py`
  - 显式维护 durable/coalesced/live-only 分类。
  - live-only 事件不得进入 trace persistence。
  - cancel/done 后拒绝普通 stream/delta 追加，只允许 terminal events。
  - 大 payload artifact 化不得破坏审批、fileChange、draft 事件的必需字段。
  - artifact 化后 envelope 字段仍留在 event payload。

### Frontend

- Modify: `Labrastro-vscode-extension/webview-ui/src/components/ChatView.tsx`
  - 继续用 `assistant_delta` 实时显示 draft 正文。
  - 新增 draft document 状态卡。
  - `tool_call_delta` 只显示轻量准备状态，不显示正文。
  - 文件变更显示使用 fileChange item。

- Modify: `Labrastro-vscode-extension/webview-ui/src/chat/sessionRunTranscriptReducer.ts`
  - 支持 `file_change_started`、`file_change_patch_updated`、
    `file_change_approval_requested`、`file_change_approval_resolved`、
    `file_change_completed`、`turn_diff_updated`。
  - 支持 `document_draft_started`、`document_draft_commit_requested`、
    `document_draft_committed`、`document_draft_failed`、`document_draft_cancelled`。
  - 不把 `tool_call_delta` 当作可回放正文事实。

- Modify: `Labrastro-vscode-extension/webview-ui/src/components/chat/SessionTurn.tsx`
  - 渲染 fileChange 状态、路径、diff、`+N/-N`。
  - 渲染 draft status。
  - commit 后展示目标路径、写入行数、diff 摘要。

- Modify: `Labrastro-vscode-extension/webview-ui/src/chat/transcript-presentation.ts`
  - 删除 `MODIFY_TOOLS`、labels、summary 中的 `write_file`、`edit_file`、
    `write_to_file`、`replace_in_file` 同类旧编辑词典。
  - 只保留 fileChange 和 `apply_patch` 语义。

- Modify: `Labrastro-vscode-extension/webview-ui/src/lib/approval-details.ts`
  - 文件变更审批标题和详情来自 fileChange diff。
  - 不再通过 `tool_name === "edit_file"` 或工具参数判断写入/修改。

- Modify: `Labrastro-vscode-extension/webview-ui/src/components/chat/TaskTimeline.tsx`
  - timeline 不再按旧工具名识别文件变更。
  - 文件变更节点来自 fileChange item。

- Modify tests and fixtures:
  - `approval-state.test.ts`
  - `settings/utils.test.ts`
  - `mock-data.ts`
  - 旧工具名 fixture 只能保留在 prohibited-state 测试中。

## Coding Agent Task Matrix

后续 coding agent 必须按矩阵执行。每个任务完成前必须同时满足 Code、Prompt、
Protocol、UI、Tests 中对应验收，不得只做其中一层。

| Task | Scope | Required changes | Required tests |
| --- | --- | --- | --- |
| T0 | Workspace mutation ownership | 新增 `WorkspaceMutationBackend` / `MutationPlan` / owner 判定；禁止 server 用 peer path 直接读写；preview、approval、execute 绑定 plan | local、remote peer、agent_run worktree 三种 path space 均有 owner 测试；远端 draft commit 不写 server filesystem |
| T1 | Tool inventory cleanup | 删除 `write_file` / `edit_file` 注册、schema、remote feature、approval policy、prompt、`config.yaml.example` 文案 | 工具枚举、prompt、capability catalog、runner feature list 均不含旧工具 |
| T2 | FileMutationService | 作为 owner 内部 planner/executor，新增 workspace realpath 校验、`MutationPlan` 生成、diff summary、staging/rollback | Add/Update/Delete/Move、多文件、越界、symlink escape、上下文不匹配、失败不写盘 |
| T3 | ApplyPatch tool | 新增模型可见 `apply_patch`，接入 patch grammar 和 owner-backed `MutationPlan` | tool schema 只暴露 `apply_patch`，执行结果含 changes/diff/status；preview/approval/execute 使用同一 plan |
| T4 | Patch streaming | 新增 `PatchArgumentStreamDecoder`，把 provider tool argument delta 增量解析为 patch/fileChange changes | partial JSON patch 产生 `file_change_patch_updated`，坏 JSON/坏 patch 产生 failed |
| T5 | FileChange events | 引入 `file_change_started/patch_updated/approval_requested/approval_resolved/completed`、`turn_diff_updated`、snake_case status | SessionRun 回放、审批、终态、turn diff 均可重建；文件变更不走通用 approval UI |
| T6 | Draft document | 新增 `draft_document_begin` 和 DocumentDraftRuntime，assistant stream 绑定 draft，完成后请求 owner 生成 document commit plan 并审批 | 正文实时可见，审批前不写盘；local/remote/agent_run owner 批准才提交，拒绝/取消/中断不写入 |
| T7 | Stream liveness | StreamSupervisor 增加 wall time、idle time、argument chars 边界，并覆盖所有 provider adapter | OpenAI Chat/Responses、Anthropic、labrastro_server adapter、agent_runs/model_bridge 均可终止并通知 UI |
| T8 | Shell boundary | approval/preflight command classifier 禁止 shell 手写源码/文档/config 文件 | echo/heredoc/Set-Content/Out-File/python write/node write 被执行前拒绝 |
| T9 | Frontend fileChange | reducer、ChatView、SessionTurn、presentation、approval details、timeline 渲染 fileChange、diff、`+N/-N`、status | patchUpdated 更新同一 item，completed 固化状态，旧工具词典不再驱动 UI |
| T10 | Frontend draft | reducer 和 UI 渲染 draft started/commit_requested/committed/failed/cancelled | draft 正文走 assistant_delta，历史回放无正文参数碎片 |
| T11 | Artifact safety | 大 payload artifact 化保留 event envelope，live-only 不进 durable trace | approval/fileChange/draft 身份、状态、关联字段完整；tool_call_delta 不持久化 |
| T12 | Test and fixture migration | 删除或改写旧 `write_file/edit_file` 测试、settings、mock、snapshot、fixture | repo-wide allowlist 检查：旧工具名只允许出现在历史 ADR 和 prohibited-state 测试 |
| T13 | End-to-end parity | 覆盖代码修改、小文件新增、长文档、取消、provider 断流 | 无 `write_file/edit_file` 调用，UI 行为对标 Codex fileChange |

## Implementation Order

1. T0：先建立 workspace mutation owner、`MutationPlan`、plan approval binding。
2. T1：删除旧工具模型可见入口和所有提示、catalog、policy 引用。
3. T2 + T3：建立 owner-backed 唯一文件变更执行路径。
4. T4 + T5：建立 Codex 对标的 fileChange 流式事件与审批链路。
5. T7：加入 provider stream 生命周期边界，防止长时间等待。
6. T6：建立长文档 draft 协议，并通过 owner commit plan 写盘。
7. T8：关闭 shell 文件编辑逃逸路径。
8. T9 + T10 + T11：前端和事件持久化按新协议对齐。
9. T12：清理旧测试、旧 fixture 和旧快照，建立 allowlist。
10. T13：跑端到端验收，确认无旧工具、无长正文参数流、无无限等待。

## Test Plan

### Backend Domain Tests

- `WorkspaceMutationBackend` 能按 execution target 选择 local、remote peer、agent_run
  worktree owner。
- server 在 remote peer 会话中不会用 peer path 直接构造本机 `FileMutationService`
  读写文件。
- `MutationPlan` 包含 `plan_id`、`workspace_id`、`execution_target`、`path_space`、
  `operations[]`、`changes[]`、`combined_diff`、`plan_hash`。
- approval 通过后 execute 校验 `plan_hash` 和每个 operation 的 old state。
- multi-file patch preview、approval、execute 使用同一个 `MutationPlan`。
- preview 支持的合法 patch，execute 不得因 plan 结构缺失而拒绝。
- Python 与 Go patch grammar golden fixtures 完全一致。
- Python 与 Go 对 malformed Add File 行都拒绝，不能静默丢弃。
- Python 与 Go 都拒绝 symlink workspace escape。
- Python 与 Go 多文件 patch 失败时不留下部分写入。
- `iter_tool_classes()` 不返回 `write_file` 或 `edit_file`。
- `iter_tool_classes()` 返回 `apply_patch` 和 `draft_document_begin`。
- `apply_patch` 拒绝绝对路径。
- `apply_patch` 拒绝 `..` 越界路径。
- `apply_patch` 拒绝空 patch。
- `apply_patch` 支持 Add File。
- `apply_patch` 支持 Update File。
- `apply_patch` 支持 Delete File。
- `apply_patch` 支持 Move to。
- `apply_patch` 在上下文不匹配时不修改文件。
- `apply_patch` 失败时返回可给模型恢复的错误。
- `PatchArgumentStreamDecoder` 能从未完成的 JSON `patch` 字段中流式产出 patch delta。
- `PatchArgumentStreamDecoder` 拒绝 `content`、`old_string`、`new_string`。
- `FileMutationService` 生成 diff 摘要。
- `FileMutationService` 只暴露 `apply_text_patch` 和 `commit_document` 公共写盘入口。
- `draft_document_begin` 不接受 `content` 参数。
- draft streaming 完成后生成 commit diff 和审批请求。
- draft commit 审批通过后通过 workspace owner 写入目标文件。
- draft commit 审批拒绝后不写入目标文件。
- draft 取消后不写入目标文件。
- draft provider interrupted 后不写入目标文件。

### SessionRun Tests

- `tool_call_delta` 不进入 trace persistence。
- `assistant_delta` 仍按 coalesced stream 持久化。
- `tool_call_stream` 仍按 coalesced stream 持久化。
- `file_change_started` 是 durable event。
- `file_change_patch_updated` 更新同一 fileChange item。
- `file_change_approval_requested` 和 `file_change_approval_resolved` 是文件变更唯一审批源。
- `file_change_completed` 是 durable event。
- fileChange status 只允许 `in_progress`、`completed`、`failed`、`declined`、
  `cancelled`。
- `turn_diff_updated` 提供整轮 unified diff。
- `document_draft_started` 是 durable event。
- `document_draft_commit_requested` 是 durable event。
- `document_draft_committed` 是 durable event。
- `approval_request` 不驱动文件变更 UI 或回放。
- artifact 化后 `event_id`、`session_run_id`、`type`、`item_id`、`approval_id`、
  `draft_id`、`status` 仍保留在 event payload。
- cancelled/done session 不再接受普通 `assistant_delta`、`reasoning_delta`、
  `tool_call_delta`。
- cancel request 会触发 provider stream abort 或等价中断。
- provider stream 超过 wall time、idle time 或 argument chars 边界时产生可见失败事件。
- OpenAI Chat、OpenAI Responses、Anthropic、labrastro_server adapter、
  agent_runs/model_bridge 都走同一 liveness supervisor。

### Prompt and Config Tests

- system prompt 不包含 `write_file`。
- system prompt 不包含 `edit_file`。
- system prompt 包含 `apply_patch` 文件变更规则。
- system prompt 包含 `draft_document_begin` 长文档规则。
- 默认 capability catalog 不包含旧工具。
- 默认 approval policy 不引用旧工具。
- `config.yaml.example` 不包含旧工具 approval 规则或示例注释。
- remote runner feature list 不包含 `write_file` 或 `edit_file`。
- 旧工具名只允许出现在历史 ADR、迁移说明或 prohibited-state 测试 allowlist。

### Frontend Tests

- `assistant_delta` 仍实时追加 Markdown 正文。
- `file_change_started` 显示路径、状态和 diff。
- `file_change_patch_updated` 更新同一 fileChange item。
- `file_change_completed` 显示 completed/failed/declined。
- 前端从 diff 计算并显示 `+N/-N`。
- `transcript-presentation.ts` 不再用旧工具名判断文件修改。
- `approval-details.ts` 不再用 `tool_name` 或工具参数判断写入/修改。
- `TaskTimeline.tsx` 不再用旧工具名判断文件变更。
- `document_draft_started` 显示目标文件和生成中状态。
- `document_draft_commit_requested` 显示待审批 diff。
- `document_draft_committed` 显示已写入、路径和 diff 摘要。
- `document_draft_failed` 显示失败原因。
- `document_draft_cancelled` 显示取消且不显示已写入。
- `tool_call_delta` 多次更新同一 preparing card，而不是创建大量历史条目。
- 回放历史不显示几千条工具参数碎片。

### End-to-End Scenarios

- 修改已有源码文件：
  - 模型只调用 `apply_patch`。
  - UI 显示 fileChange、diff 和 `+N/-N`。
  - 文件内容正确。
  - 没有 `write_file/edit_file` 工具调用。

- 新增小文档：
  - 模型调用 `apply_patch Add File`。
  - 文件创建成功。
  - 审批显示 fileChange diff。

- 生成 100 KiB Markdown 架构文档：
  - 模型先调用 `draft_document_begin`。
  - 正文通过 `assistant_delta` 实时可见。
  - 完成后 runtime 请求 workspace owner 生成 commit diff 和审批请求。
  - 审批通过后 workspace owner 写入目标文件。
  - `tool_call_delta` 数量为 0 或小于节流上限。
  - trace 不含正文参数碎片。

- 远端 peer 生成长 Markdown 文档：
  - server 不在本机创建目标文件。
  - peer workspace 中创建目标文件。
  - draft commit approval 绑定 `MutationPlan`。
  - fileChange diff 来自 peer owner。

- 多文件 patch：
  - preview 显示所有文件 diff。
  - approval 绑定同一个 `MutationPlan`。
  - execute 校验所有文件 old state。
  - 成功时全部写入。
  - 任一文件失败时全部不写入或回滚。

- provider 通过 JSON schema 流式输出 `apply_patch`：
  - `PatchArgumentStreamDecoder` 在 JSON 参数未结束时产出 patch delta。
  - UI 实时显示 fileChange diff。
  - provider 断流时产生 failed 终态，不留下无限 preparing。

- provider 在工具参数流中不结束：
  - runtime 在 liveness 边界内中断。
  - SessionRun 产生可见 failed/interrupted 事件。
  - UI 不无限显示 preparing。
  - diagnostics 记录 phase、duration、partial_kind、received_chars。

- 用户取消长文档生成：
  - provider stream 被中断。
  - 目标文件不写入。
  - SessionRun 进入 cancelled。
  - 取消后没有继续追加普通 delta。

- shell 绕写尝试：
  - `echo > file`、heredoc、`Set-Content` 等被 policy 拒绝。
  - 模型收到恢复建议：使用 `apply_patch` 或 `draft_document_begin`。

- 旧测试和 fixture 清理：
  - repo-wide allowlist 外不存在 `write_file` / `edit_file` 旧工具名。
  - settings、mock、snapshot 不再把旧工具列为可用文件编辑能力。

## Acceptance Criteria

- 模型可见工具列表只有一套文件变更协议：`apply_patch`。
- 模型可见长文档协议只有 `draft_document_begin`。
- `write_file` 和 `edit_file` 不出现在工具 schema、prompt、capability catalog、
  approval policy、remote peer feature 或前端工具文案中。
- 代码中不保留 `write_file` / `edit_file` 作为 runtime 命名能力。
- `ApplyPatchTool` 调用 `FileMutationService.apply_text_patch`，不复用 unrelated
  `ProjectMemoryService.apply_patch`。
- `FileMutationService` 只作为 workspace owner 内部实现使用；server 不得用 remote
  peer path 直接实例化它。
- preview、approval、execute 绑定同一个 `MutationPlan`，不能只绑定单个
  `resolved_path`。
- multi-file patch 作为一个 plan 被批准和执行。
- Python 与 Go remote runner 对 patch grammar、workspace path、diff、error、
  transaction 语义保持 fixture 级一致。
- 文件写入失败不留下部分变更。
- JSON schema transport 下，`PatchArgumentStreamDecoder` 能在完整参数结束前产出
  patch delta。
- 文件新增、修改、删除、重命名全部通过 `apply_patch` 跑通。
- 文件变更 UI 使用 fileChange 事件、diff 和 `+N/-N`。
- 文件变更审批、回放和终态只使用 fileChange 数据源。
- fileChange status 只使用 `in_progress`、`completed`、`failed`、`declined`、
  `cancelled`。
- 长 Markdown 文档生成通过 `draft_document_begin` + `assistant_delta` 跑通。
- 长正文不会进入工具参数。
- 用户可以实时看到长文档正文。
- draft 完成后由 runtime 请求 workspace owner 生成 commit diff，审批通过后由 owner
  写入目标文件。
- remote peer draft 完成后由 peer owner 生成 commit diff，审批通过后写入 peer
  workspace。
- draft 取消、中断、失败时不会写入目标文件。
- draft commit 审批拒绝时不会写入目标文件。
- artifact 化不移除 durable event envelope 的身份、状态和关联字段。
- `tool_call_delta` 不作为 durable transcript event 持久化。
- `tool_call_delta` 最多作为 live state 节流更新。
- 回放历史不包含高频工具参数碎片。
- provider stream 不会在工具参数阶段无限等待；超限必须产生可见终态。
- liveness 边界通过共享 `StreamSupervisor` 覆盖所有 provider adapter。
- 取消后 provider stream 被切断，或等价地停止继续追加普通事件。
- `shell` 无法作为手工文件编辑逃逸路径。
- shell 文件写入拦截发生在 approval/preflight command classifier。
- 所有文件落盘只发生在 `apply_patch` 和 draft commit 两条路径。
- 前端、后端、prompt、capability catalog 和测试使用同一套术语。
- 旧工具名只允许出现在历史 ADR、迁移说明或 prohibited-state 测试 allowlist。

## Prohibited Completion States

以下任一情况出现时，本 ADR 不得视为完成：

- `write_file`、`edit_file` 与 `apply_patch` 平级暴露给模型。
- remote peer 仍上报 `write_file` 或 `edit_file` feature。
- 代码保留 `write_file` / `edit_file` 作为 runtime 命名能力。
- `ApplyPatchTool` 复用 taskflow/project memory 的 unrelated `apply_patch` 方法。
- server 在 remote peer 会话中用 peer path 直接读写本机文件系统。
- draft commit 在 remote peer 会话中绕过 remote peer owner 直接调用 server-side
  `FileMutationService.commit_document`。
- preview 支持 multi-file patch，但 execute 因 expected state 只能表达单文件而拒绝。
- approval 只绑定 `resolved_path`，没有绑定 `MutationPlan` / `plan_hash`。
- Python 与 Go patch parser 对同一 patch 产生不同结果。
- malformed Add File 行在任一执行端被静默忽略。
- symlink 指向 workspace 外时仍允许写入。
- multi-file patch 失败后留下部分已写文件。
- JSON schema transport 等完整工具参数结束后才创建 fileChange。
- `PatchArgumentStreamDecoder` 接受 `content`、`old_string` 或 `new_string`。
- `draft_document_begin` 接受 `content` 参数。
- 长文档仍通过工具参数传输正文。
- draft 正文完成后绕过 diff/approval 直接写盘。
- draft commit 被拒绝后仍写入目标文件。
- 文件变更 UI 依赖 raw tool JSON，而不是 fileChange diff。
- 文件变更审批仍由通用 `approval_request` 驱动前端。
- fileChange status 混用 `inProgress`、`done`、`success`、`rejected` 等非约定值。
- shell 可以手写源码或文档文件。
- shell 写文件拦截只存在于 prompt 文案，没有 approval/preflight classifier。
- `tool_call_delta` 继续逐条进入 trace persistence。
- artifact 化后 durable event 只剩 `artifact_ref`，丢失身份、状态或审批关联字段。
- 前端显示 draft 正文依赖 `arguments_preview`。
- prompt 同时指导 `edit_file` 和 `apply_patch`。
- capability catalog 仍声明旧文件编辑工具。
- config、settings、mock、snapshot fixture 仍把旧工具作为可用文件编辑能力。
- provider stream 在工具参数阶段无 wall time / idle time / argument size 边界。
- liveness 边界只在单个 provider adapter 内局部实现。
- 取消后 session 已是 cancelled/done，但 provider delta 仍继续进入普通历史。

## Notes for Implementers

- 不要先加新工具再保留旧工具等待迁移；开发期直接收敛。
- 不要把 `artifact` 做成泛用模型工具；先实现受控的 `draft_document`。
- 不要把阈值当架构边界；阈值只是保护线。
- 不要把 UI preview 当正文流；正文流只有 assistant text stream。
- 不要把低层文件写入 helper 注册成 model tool。
- 不要保留 `write_file` / `edit_file` 作为内部命名；底层能力必须重命名进
  `FileMutationService` 或 filesystem adapter。
- 不要让通用 approval、artifact 或 tool delta 成为文件变更的事实来源；文件变更
  的事实来源只有 fileChange。

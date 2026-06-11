# ADR 0007: Agent 文件变更与长文档草稿边界

## Status

Proposed.

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

## Product Rules

- 模型可见的文件变更入口只有 `apply_patch`。
- 模型可见的长文档生成入口只有 `draft_document_begin`。
- `write_file` 和 `edit_file` 不再作为模型可见工具存在。
- 文件系统实际写入只允许通过运行时内部服务完成，模型不能直接传完整大正文给写盘原语。
- `shell` 不是文件编辑协议；不得用 shell 的 echo、heredoc、`Set-Content`、
  `Out-File`、脚本片段等方式绕过 `apply_patch` 或 draft document commit。
- 工具参数流不是正文流。正文必须通过 `assistant_delta` 或 draft document 通道可见。
- `tool_call_delta` 是 live 状态，不是 durable transcript event。
- 开发期不做旧工具 alias、兼容迁移或双轨 schema。

## Core Boundary

目标架构按运行语义分层，不按“新增/修改”或“长短”分层。

```text
文件变更协议       apply_patch
长正文产物协议     draft_document
内部落盘能力       FileMutationService / filesystem adapter
实时正文展示       assistant_delta
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
写盘 -> runtime internal commit
```

## Model-Visible Tools

### `apply_patch`

模型可见的唯一文件变更工具。

当前 Python tool transport 可以先使用 JSON schema：

```json
{
  "patch": "*** Begin Patch\n*** Update File: docs/example.md\n@@\n-old\n+new\n*** End Patch\n"
}
```

公开合同是 patch grammar，不是 JSON 字段形状。若未来 provider 支持 freeform/custom
tool，可复用同一 patch grammar，但不能新增第二套编辑协议。

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
- assistant 正文完成后，运行时内部 commit 到 `target_path`。
- commit 产生 durable event：`document_draft_committed`。
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
- 在 assistant message 完成时调用 `FileMutationService.commit_document`。
- 在 SessionRun 中投影 draft 状态。
- 在取消或中断时阻止写盘。

禁止职责：

- 不解析 patch。
- 不提供任意 artifact 写入工具。
- 不让模型直接调用 commit。

## Removed Model-Visible Tools

### `write_file`

从模型可见 builtin tools 中移除。

可保留的代码只能作为内部 helper，但必须满足：

- 没有 `@register_tool`。
- 不出现在模型 tool schema。
- 不出现在 capability catalog 的模型可见工具列表。
- 不出现在 prompt 规则里。
- 只能被 `FileMutationService` 或测试 fixture 调用。

### `edit_file`

从模型可见 builtin tools 中移除。

不保留同级替代工具。小范围替换通过 `apply_patch` 表达。

如果实现时需要临时复用旧逻辑，只能作为内部函数迁移到 patch apply 路径，不允许保留
`edit_file(old_string, new_string)` 的模型入口或提示规则。

## Event Architecture

SessionRun 事件必须按用途分层。

### Durable Events

必须持久化、可回放：

```text
session_run_start
assistant_message
tool_call_start
tool_call_end
tool_call_protocol_error
approval_request
approval_resolved
document_draft_started
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

## Implementation Landing Points

### Backend Tools

- Modify: `reuleauxcoder/extensions/tools/registry.py`
  - 保持 builtin 自动导入机制。
  - 确保 `write_file`、`edit_file` 不再注册为模型工具。

- Create: `reuleauxcoder/extensions/tools/builtin/apply_patch.py`
  - 注册 `ApplyPatchTool`。
  - 参数只接受 `patch`。
  - 调用 `FileMutationService.apply_patch`。

- Create: `reuleauxcoder/extensions/tools/builtin/draft_document.py`
  - 注册 `DraftDocumentBeginTool`。
  - 参数只接受 `target_path`、`title`、`format`。
  - 不接受 `content`。

- Create: `reuleauxcoder/domain/files/file_mutation_service.py`
  - 内部文本文件变更服务。

- Create: `reuleauxcoder/domain/agent/document_draft.py`
  - draft lifecycle 状态、buffer、commit 决策。

- Remove or de-register:
  - `reuleauxcoder/extensions/tools/builtin/write.py`
  - `reuleauxcoder/extensions/tools/builtin/edit.py`

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

- Modify: `reuleauxcoder/services/config/loader.py`
  - 默认 approval policy 从旧工具迁移为 `apply_patch` 与 `draft_document_begin`。
  - 不保留旧工具 policy alias。

### Remote Relay and SessionRun

- Modify: `reuleauxcoder/domain/agent/loop.py`
  - 将 draft runtime 接入 assistant stream 与 message completion。
  - 在 provider stream 取消时停止 draft commit。

- Modify: `reuleauxcoder/services/providers/tool_call_delta.py`
  - 输出 live state 所需字段。
  - 不再把每个 delta 当作 durable event payload。

- Modify: `reuleauxcoder/interfaces/entrypoint/remote_relay.py`
  - `TOOL_CALL_DELTA` 不再直接 `append_event("tool_call_delta")`。
  - 改为 live state update 或节流状态事件。
  - draft document 产生 started/committed/failed/cancelled durable events。

- Modify: `labrastro_server/interfaces/http/remote/service.py`
  - 显式维护 durable/coalesced/live-only 分类。
  - live-only 事件不得进入 trace persistence。
  - cancel/done 后拒绝普通 stream/delta 追加，只允许 terminal events。

### Frontend

- Modify: `Labrastro-vscode-extension/webview-ui/src/components/ChatView.tsx`
  - 继续用 `assistant_delta` 实时显示 draft 正文。
  - 新增 draft document 状态卡。
  - `tool_call_delta` 只显示轻量准备状态，不显示正文。

- Modify: `Labrastro-vscode-extension/webview-ui/src/chat/sessionRunTranscriptReducer.ts`
  - 支持 `document_draft_started`、`document_draft_committed`、
    `document_draft_failed`、`document_draft_cancelled`。
  - 不把 `tool_call_delta` 当作可回放正文事实。

- Modify: `Labrastro-vscode-extension/webview-ui/src/components/chat/SessionTurn.tsx`
  - 渲染 draft status。
  - commit 后展示目标路径、写入行数、diff 摘要。

## Implementation Order

1. 增加测试，证明模型工具 schema 中不允许出现 `write_file` 和 `edit_file`。
2. 增加测试，证明 prompt 规则中不再指导使用旧工具。
3. 实现 `FileMutationService` 的 workspace path 校验、diff、文本写入。
4. 实现 patch parser 和 `apply_patch` 工具。
5. 将新增、修改、删除文件测试全部改为 `apply_patch`。
6. 从 builtin registry 和 capability catalog 移除 `write_file`、`edit_file`。
7. 实现 `draft_document_begin` 工具和 `DocumentDraftRuntime`。
8. 接入 assistant stream 捕获和 turn completion commit。
9. 将 `tool_call_delta` 改为 live-only 状态并从 trace persistence 排除。
10. 加强 cancel：关闭 provider stream，取消后禁止普通 delta append。
11. 前端接入 draft events 和 live tool preparing state。
12. 增加 shell 文件写入禁用策略。
13. 删除旧工具相关审批预览、测试和文案。
14. 跑完整后端、前端和端到端验收。

## Test Plan

### Backend Domain Tests

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
- `FileMutationService` 生成 diff 摘要。
- `draft_document_begin` 不接受 `content` 参数。
- draft streaming 完成后写入目标文件。
- draft 取消后不写入目标文件。
- draft provider interrupted 后不写入目标文件。

### SessionRun Tests

- `tool_call_delta` 不进入 trace persistence。
- `assistant_delta` 仍按 coalesced stream 持久化。
- `tool_call_stream` 仍按 coalesced stream 持久化。
- `document_draft_started` 是 durable event。
- `document_draft_committed` 是 durable event。
- cancelled/done session 不再接受普通 `assistant_delta`、`reasoning_delta`、
  `tool_call_delta`。
- cancel request 会触发 provider stream abort 或等价中断。

### Prompt and Config Tests

- system prompt 不包含 `write_file`。
- system prompt 不包含 `edit_file`。
- system prompt 包含 `apply_patch` 文件变更规则。
- system prompt 包含 `draft_document_begin` 长文档规则。
- 默认 capability catalog 不包含旧工具。
- 默认 approval policy 不引用旧工具。

### Frontend Tests

- `assistant_delta` 仍实时追加 Markdown 正文。
- `document_draft_started` 显示目标文件和生成中状态。
- `document_draft_committed` 显示已写入、路径和 diff 摘要。
- `document_draft_failed` 显示失败原因。
- `document_draft_cancelled` 显示取消且不显示已写入。
- `tool_call_delta` 多次更新同一 preparing card，而不是创建大量历史条目。
- 回放历史不显示几千条工具参数碎片。

### End-to-End Scenarios

- 修改已有源码文件：
  - 模型只调用 `apply_patch`。
  - UI 显示 patch 工具执行。
  - 文件内容正确。
  - 没有 `write_file/edit_file` 工具调用。

- 新增小文档：
  - 模型调用 `apply_patch Add File`。
  - 文件创建成功。
  - 审批显示 diff。

- 生成 100 KiB Markdown 架构文档：
  - 模型先调用 `draft_document_begin`。
  - 正文通过 `assistant_delta` 实时可见。
  - 完成后 runtime 写入目标文件。
  - `tool_call_delta` 数量为 0 或小于节流上限。
  - trace 不含正文参数碎片。

- 用户取消长文档生成：
  - provider stream 被中断。
  - 目标文件不写入。
  - SessionRun 进入 cancelled。
  - 取消后没有继续追加普通 delta。

- shell 绕写尝试：
  - `echo > file`、heredoc、`Set-Content` 等被 policy 拒绝。
  - 模型收到恢复建议：使用 `apply_patch` 或 `draft_document_begin`。

## Acceptance Criteria

- 模型可见工具列表只有一套文件变更协议：`apply_patch`。
- 模型可见长文档协议只有 `draft_document_begin`。
- `write_file` 和 `edit_file` 不出现在工具 schema、prompt、capability catalog、
  approval policy 或前端工具文案中。
- 文件新增、修改、删除、重命名全部通过 `apply_patch` 跑通。
- 长 Markdown 文档生成通过 `draft_document_begin` + `assistant_delta` 跑通。
- 长正文不会进入工具参数。
- 用户可以实时看到长文档正文。
- draft 完成后由 runtime 内部写入目标文件。
- draft 取消、中断、失败时不会写入目标文件。
- `tool_call_delta` 不作为 durable transcript event 持久化。
- `tool_call_delta` 最多作为 live state 节流更新。
- 回放历史不包含高频工具参数碎片。
- 取消后 provider stream 被切断，或等价地停止继续追加普通事件。
- `shell` 无法作为手工文件编辑逃逸路径。
- 所有文件落盘只发生在 `apply_patch` 和 draft commit 两条路径。
- 前端、后端、prompt、capability catalog 和测试使用同一套术语。

## Prohibited Completion States

以下任一情况出现时，本 ADR 不得视为完成：

- `write_file`、`edit_file` 与 `apply_patch` 平级暴露给模型。
- `draft_document_begin` 接受 `content` 参数。
- 长文档仍通过工具参数传输正文。
- shell 可以手写源码或文档文件。
- `tool_call_delta` 继续逐条进入 trace persistence。
- 前端显示 draft 正文依赖 `arguments_preview`。
- prompt 同时指导 `edit_file` 和 `apply_patch`。
- capability catalog 仍声明旧文件编辑工具。
- 取消后 session 已是 cancelled/done，但 provider delta 仍继续进入普通历史。

## Notes for Implementers

- 不要先加新工具再保留旧工具等待迁移；开发期直接收敛。
- 不要把 `artifact` 做成泛用模型工具；先实现受控的 `draft_document`。
- 不要把阈值当架构边界；阈值只是保护线。
- 不要把 UI preview 当正文流；正文流只有 assistant text stream。
- 不要把低层文件写入 helper 注册成 model tool。

# Agent 工具合同与长文档链路纠偏指导

日期：2026-06-12

## 结论

本轮问题不是单个 `apply_patch` 解析器 bug，也不是单纯前端渲染慢。根因是“模型可见工具合同、运行时预校验、审批预览、远端执行、前端状态展示”没有被同一份可测试合同绑定住。

最明显的故障表现在最后一轮会话里：

- 模型把 `apply_patch` 写成了不存在的语法：`*** File:`、`*** Action:`。
- 模型又把标准 unified diff 放进 `*** Begin Patch`，但当前 parser 只接受 `*** Add File:` / `*** Update File:` / `*** Delete File:`。
- 第三次尝试长文档 patch 时，工具参数流在 `*** End Patch` 前中断。
- 文档草稿已经显示“已生成 7998 个字符”，但 VS Code 预览停在 1700 字左右，说明正文展示、进度事件、快照和中断状态之间没有给用户一个一致解释。
- 用户看到 `reasoning` 在文档半句未完成时继续出现，这从模型流角度可能发生，但从产品语义上必须被明确标注为“模型仍在思考/流被中断”，不能让用户理解成“文档生成还在顺畅推进”。

因此，纠偏方向不是继续增加临时兼容或让 parser 接受更多随意格式，而是把工具合同写死：

```text
模型可见合同
  -> 工具参数流状态
  -> 语义预校验
  -> 预览和审批
  -> 执行端校验
  -> 前端展示
  -> 中断恢复
```

每一层必须使用同一个工具语义、同一个状态机、同一组错误码和同一组回归样例。

## 当前链路事实

### 1. 模型拿不到完整 patch 语法

当前系统提示只在工具列表中渲染工具名和一句描述：

- `reuleauxcoder/services/prompt/builder.py`
  - `_tools_block()` 只输出 `name + description`。
  - `_rules_block()` 只说“文件变更用 `apply_patch`”，没有给出 grammar。

`apply_patch` 工具本身也只给了简短参数描述：

- `reuleauxcoder/extensions/tools/builtin/apply_patch.py`
  - `patch.description = "Patch text using the *** Begin Patch grammar."`
  - `preflight_validate()` 只检查非空、64 KiB、二进制字符，不解析语法。

但真正 parser 的要求很严格：

- `reuleauxcoder/domain/files/file_mutation_service.py`
  - 第一行必须是 `*** Begin Patch`。
  - 最后一行必须是 `*** End Patch`。
  - 只接受 `*** Add File: path`、`*** Update File: path`、`*** Delete File: path`、可选 `*** Move to: path`。
  - Add File 正文每行必须以 `+` 开头。
  - Update File 需要 `@@` hunk，hunk 行必须以空格、`-`、`+` 开头。

Go peer 也复制了同一套语法：

- `reuleauxcoder-agent/internal/tools/execute.go`
  - `parsePatchOperations()` 同样只接受 `Add File / Update File / Delete File / Move to`。

这说明语法不是没有实现，而是“实现没有被模型可见合同、预校验和自修复链路消费”。

### 2. 当前先做 JSON schema 修复，不做 patch DSL 修复

`reuleauxcoder/domain/agent/tool_arguments.py` 的修复层只处理 JSON 层：

- 参数必须是对象。
- 字段类型要匹配 JSON schema。
- 可以修复部分 provider 的字符串、数组、null 等问题。

但 `{"patch": "<无效 patch DSL>"}` 在 JSON schema 层是合法的，所以会继续进入后续流程。最后失败发生在 mutation backend 或 Go peer：

```text
Error [REMOTE_TOOL_ERROR]: unexpected patch line: *** File: ...
Error [REMOTE_TOOL_ERROR]: patch must end with *** End Patch
```

这就是“先校验、再修复”看起来存在，但实际没有覆盖最关键语义层的原因。

### 3. `apply_patch` 的文件变更状态会过早进入 UI

`reuleauxcoder/domain/agent/loop.py` 的 `_emit_apply_patch_stream_delta()` 在工具参数还只是 delta 的时候就发：

```text
file_change_started
file_change_patch_updated
```

它用 `PatchArgumentStreamDecoder` 从 JSON 字符串里抠出 `patch` 字段增量，但这个 decoder 只保证 JSON 参数流结构，不保证 patch grammar 已经合法。

结果是：

- 用户可能看到“文件变更开始了”。
- UI 可能有 patch preview。
- 但这个 patch 实际上还不是一个可执行变更。
- 一旦流中断或语法错误，用户体验就是“它好像在改文件，但其实什么也没做成”。

正确合同应该是：

```text
tool_call_delta/preparing    只表示工具参数正在生成
patch_parse_valid            才能进入 file_change_started
preview_ok                   才能进入 approval_request
execute_ok/failed            才能进入 file_change_completed
```

### 4. 审批预览和错误展示没有强区分

`reuleauxcoder/interfaces/shared/approval_preview.py` 对本地 `apply_patch` 预览失败时返回 `None`，不会把 parser 错误变成用户可理解的审批前错误。

远端审批链路已经更接近正确方向：

- `remote_relay.py` 会向 peer 请求 preview。
- preview 成功后 approval payload 带 diff。
- 允许后 `remember_approved_preview()` 缓存 expected state。
- Go peer execute 时校验 old state / plan hash。

但还有两个合同风险：

- preview 失败应该阻止 approval request，不能让用户批准一个“没有 diff 的写文件请求”。
- preview 失败信息要给模型和用户两种版本：
  - 给模型：精确修复语法的 retry hint。
  - 给用户：说明“工具参数无效，尚未进入文件修改审批”。

### 5. `expected_state` 空状态问题已按正确方向修复，但应纳入合同

当前 `labrastro_server/interfaces/http/remote/protocol/tools.py` 中：

```python
and not self.operations
```

说明 `operations=[]` 已经按空状态处理；`old_exists is None` 才算空，因此 `old_exists=False` 仍然会被视为有效状态。

这类问题仍然要纳入纠偏合同：任何 expected state 如果没有可校验内容，就不得被发送或缓存；任何“文件不存在”状态必须仍然可校验。

### 6. 长文档正文通道已经从 ChatView 拆出，但中断语义还没闭合

当前长文档链路：

- `draft_document_begin` 只声明新 Markdown 草稿目标，不传正文。
- `AgentLoop._on_token()` 在 draft active 时把普通输出 token 追加到 `DocumentDraftRuntime`。
- `DocumentDraftLiveStream` 产生：
  - `document_draft_preview_chunk`
  - `document_draft_progress`
  - `document_draft_snapshot`
- `remote_relay` 对 `document_draft_preview_chunk` 调用 `append_live_event()`，并设置 `draft_content_emitted`。
- `session_run_end.response_rendered = assistant_content_emitted or draft_content_emitted`，避免把完整正文再塞回 ChatView。
- VS Code extension 的 `DraftDocumentProvider` 用 `labrastro-draft:` 虚拟文档和 `markdown.showPreviewToSide` 打开渲染预览。

这个方向是对的，但还有未闭合点：

- `document_draft_preview_chunk` 是 live-only 事件，主 session projection 只保留状态，不持久完整正文。
- 如果 provider stream 中断，`AgentLoop` 当前会 `_flush_active_draft("interrupted")` 后 `draft_runtime.cancel_active("provider stream interrupted")`。
- 用户看到的预览停住后，只能通过“中断提示”理解状态，但当前 UI 没有把“正文通道停住”和“reasoning 仍在流”分开解释。

因此，长文档链路不是“不能做”，而是需要补齐恢复和停滞可见性：

```text
正文通道有新增内容 -> 文档预览持续更新
正文通道无新增但 reasoning 有新增 -> 显示模型仍在思考，不显示文档进度增长
provider 中断 -> draft 保留最后快照，状态为 interrupted/recoverable，不直接表现为已完成或继续生成
用户继续 -> 基于 draft checkpoint 续写，而不是从空状态重来或转向 apply_patch
```

### 7. 服务端到前端不是完全无缓冲实时，但当前卡顿不能只归因于前端

当前已存在几层节流：

- `DocumentDraftLiveStream`
  - preview chunk 默认 0.1 秒或 2048 字符 flush。
  - progress 默认 1 秒或 2048 字符 flush。
  - snapshot 默认 5 秒或 16 KiB flush。
- `_RemoteSessionRun`
  - `assistant_delta`、`reasoning_delta`、`tool_call_stream` 走 0.04 秒合并。
  - `document_draft_preview_chunk` 是 live-only。
- VS Code extension
  - `LabrastroController` 把 `assistant_delta`、`document_draft_preview_chunk`、`reasoning_delta`、`tool_call_delta`、`tool_call_stream` 作为 live 批次转给 webview。

这些阈值理论上不会导致“一分钟只更新 3 个字符”。如果出现这种现象，更可能是：

- 上游 provider 在这段时间没有输出正文 token。
- provider 输出的是 reasoning token，不是 output text token。
- 流已经进入中断/恢复，但 UI 没把“正文停止”和“reasoning 继续”拆开显示。
- draft 预览事件没有被前端消费、cursor 跳过、或 live-only buffer 没有按预期送达。

所以性能纠偏不能只改节流参数，必须加可观测性和状态分类。

### 8. provider 流中断是被统一建模了，但草稿恢复没有产品级闭环

`reuleauxcoder/services/providers/stream_supervisor.py` 当前默认：

```text
wall_time_sec = 600
idle_time_sec = 120
```

它把中断按 partial kind 分类：

```text
text_interrupted
reasoning_interrupted
tool_call_delta_interrupted
usage_after_output_interrupted
```

这说明底层不是完全不知道中断原因。但上层现在仍有缺口：

- partial kind 是 reasoning 时，UI 容易让用户误以为“文档还在生成”。
- partial kind 是 tool_call_delta 时，未完成工具参数没有形成一个可修复的工具错误结果。
- active draft 遇到 provider interruption 当前会 cancel，而不是保留为 recoverable draft checkpoint。

### 9. ChatView 仍有原始流程卡片泄漏风险

前端已经有 presentation 分层：

- reducer 存 canonical event。
- `transcript-presentation.ts` 做用户可见分组。
- `SessionTurn.tsx` 渲染 process group、reasoning panel、file change、draft、tool 等。

但用户反馈的 `PreToolUse`、`PostToolUse`、原始参数卡片说明仍有两类风险：

- 生命周期事件仍被当成普通过程卡暴露给普通用户。
- 调试字段和 raw audit 能力没有被折叠到足够深的“开发者详情”层。

这属于同一类合同问题：事件是审计事实，不等于用户可见语言。

## 同类问题清单

| 编号 | 问题类别 | 当前表现 | 是否同类 | 根因 |
| --- | --- | --- | --- | --- |
| C1 | 工具语法不可见 | `apply_patch` 真实 grammar 只在 parser/tests 中，prompt 只说一句话 | 是 | 模型可见合同缺失 |
| C2 | JSON 校验和语义校验分层断裂 | JSON 合法但 patch DSL 无效时仍进入审批/执行链路 | 是 | preflight 没调用语义 parser |
| C3 | 运行中状态过早升级 | 参数 delta 阶段就发 `file_change_started` | 是 | `preparing` 和 `file_change` 状态机混用 |
| C4 | preview 失败不够产品化 | 用户看不到“这还不是可审批 diff”，模型只得到普通 Error | 是 | 错误码和修复提示没有统一 |
| C5 | Python/Go parser 双实现漂移风险 | 两边目前语义接近，但没有共享 fixture 合同 | 是 | 跨语言合同靠人工同步 |
| C6 | expected state 空状态 | 当前已修，但必须防回归 | 是 | “存在字段”和“可校验状态”曾混淆 |
| C7 | 长文档中断不可恢复 | draft 中断后 cancel，预览停在半句 | 是 | draft checkpoint / recoverable 状态缺失 |
| C8 | reasoning 和正文通道混淆 | 文档半句未完时 reasoning 仍显示，用户以为流程乱跳 | 是 | UI 没标出“正文停滞、模型思考、流中断”的差异 |
| C9 | 工具失败自修复弱 | patch 语法错后模型连续尝试错误格式 | 是 | 错误返回没有强示例和机器可读修复码 |
| C10 | ChatView 调试信息泄漏 | PreToolUse/PostToolUse/raw args 进入用户主视图 | 是 | 审计事实和用户展示没有硬边界 |

## 统一目标架构

### 核心原则

1. 模型只看一个写文件入口：`apply_patch`。
2. 长 Markdown 正文不走工具参数：`draft_document_begin` 只声明草稿，正文走 draft preview 通道。
3. `draft_document_commit` 是 runtime 内部发布动作，不是模型可自由选择的第二套写文件协议。
4. `shell` 永远不是文件编辑协议。
5. 工具参数流、文件变更流、长文档正文流、reasoning 流必须是四条不同语义的通道。
6. 任何进入审批的文件变更必须已经 preview 成功，有明确 diff 和可执行 owner。
7. 任何执行端都必须复用同一合同样例，不允许 Python/Go/前端各自猜语义。

### 统一状态机

文件变更：

```text
tool_preparing
  -> tool_argument_valid
  -> mutation_previewing
  -> mutation_preview_failed | mutation_preview_ready
  -> approval_requested
  -> approval_denied | approval_approved
  -> execute_started
  -> execute_failed | execute_completed
```

长文档：

```text
draft_declared
  -> draft_streaming
  -> draft_body_stalled
  -> draft_interrupted_recoverable
  -> draft_streaming
  -> draft_preview_ready
  -> draft_commit_approval_requested
  -> draft_commit_denied | draft_committed | draft_commit_failed
```

用户可见规则：

- `tool_preparing` 只显示“正在准备工具参数”，不显示文件 diff。
- `mutation_preview_failed` 显示“工具参数无效，未进入文件审批”。
- `mutation_preview_ready` 才显示文件变更卡和 diff。
- `draft_body_stalled` 显示“文档正文暂未继续输出”，不能把 reasoning 当成文档进度。
- `draft_interrupted_recoverable` 显示“文档草稿已保留，可继续生成”。

## 修改矩阵

### A. 工具合同层

| 项目 | 当前落点 | 修改要求 | 验收 |
| --- | --- | --- | --- |
| `apply_patch` grammar | `file_mutation_service.py`、`execute.go` | 抽出一份明确 grammar 文档和 fixture；Python/Go parser 必须跑同一组合法/非法样例 | `*** File:`、unified diff、缺失 `End Patch` 都有稳定错误码 |
| 工具描述 | `apply_patch.py`、`prompt/builder.py` | 系统提示必须渲染完整最小 grammar、Add/Update/Delete 示例和禁止格式 | prompt 快照测试包含 `*** Add File:`、`*** Update File:`、`Add File lines must start with +` |
| 长文档工具描述 | `draft_document.py`、ADR | 明确 `draft_document_begin` 只声明新 Markdown 草稿，不写盘，不修改已有文件 | 模型提示不会把 draft begin 理解成文件已保存 |

### B. 语义预校验层

| 项目 | 当前落点 | 修改要求 | 验收 |
| --- | --- | --- | --- |
| JSON 参数校验 | `tool_arguments.py` | 保留现有 JSON schema 修复 | 现有测试不退化 |
| DSL 参数校验 | `ApplyPatchTool.preflight_validate()` | 调用 patch parser 的“只解析不读写”入口，返回结构化语法错误 | 无效 patch 在 approval 前失败 |
| 修复提示 | `tool_execution.py` | preflight/preview error 进入统一 retry message，包含正确 patch 示例 | 模型收到错误后下一次能按正确 grammar 重试 |

### C. 文件变更事件层

| 项目 | 当前落点 | 修改要求 | 验收 |
| --- | --- | --- | --- |
| 参数流展示 | `AgentLoop._emit_apply_patch_stream_delta()` | delta 阶段只发 `tool_call_delta/preparing` 或新增 `patch_argument_preparing`；不发 `file_change_started` | 半截 patch 不再生成文件变更卡 |
| preview 成功后展示 | `ToolExecutor._emit_apply_patch_file_change_started()` | 只在 parser + preview 成功后发 `file_change_started` | 文件变更卡一定有 changes 或明确 preview 状态 |
| preview 失败 | `ToolExecutor`、`approval_preview.py` | 发 `tool_call_protocol_error` 或 `mutation_preview_failed`，不发 approval request | 用户不会审批无 diff 请求 |

### D. 远端 peer 合同层

| 项目 | 当前落点 | 修改要求 | 验收 |
| --- | --- | --- | --- |
| Python/Go parser 一致 | `file_mutation_service.py`、`execute.go` | 建立共享 fixture 文件，Python pytest 和 Go test 都读取 | 两端错误码、合法 patch 结果一致 |
| expected state | `ToolMutationPreviewState`、`remote_backend.py`、Go execute | 保持空状态不发送，`old_exists=False` 可校验；补回归测试防漂移 | `from_dict({"operations": []}) is None`，`old_exists=False` 非空 |
| preview 绑定 execute | `remote_backend.py`、Go peer | 保持远端 preview expected_state -> execute 校验；不要扩展 wire shape 除非有 ADR | stale preview 仍失败 |

### E. 长文档正文层

| 项目 | 当前落点 | 修改要求 | 验收 |
| --- | --- | --- | --- |
| 正文流 | `AgentLoop._on_token()`、`DocumentDraftLiveStream` | 保持正文 token 进入 `document_draft_preview_chunk`，不进入 ChatView assistant 正文 | 长文档不刷 ChatView 主消息 |
| 停滞状态 | `DocumentDraftLiveStream`、`remote_relay`、前端 | 记录 last body chunk 时间；reasoning 有更新但正文无更新时进入 `draft_body_stalled` UI 状态 | 用户能看出不是文档在继续生成 |
| 中断恢复 | `AgentLoop`、`DocumentDraftRuntime` | provider interruption 不直接丢弃 active draft；保留 snapshot/checkpoint 和 recoverable 状态 | 断流后可继续从半句位置续写 |
| 发布 | `DocumentDraftRuntime.commit_active()` | 只在正文完成后 preview + approval；已有目标失败；失败不覆盖文件 | 预览完成前不出现落盘审批 |

### F. 前端展示层

| 项目 | 当前落点 | 修改要求 | 验收 |
| --- | --- | --- | --- |
| draft preview | `DraftDocumentProvider.ts` | 继续用 `markdown.showPreviewToSide`；消费 `document_draft_preview_chunk`、snapshot、finish | 预览内容和 progress/snapshot 对齐 |
| ChatView draft card | `sessionRunTranscriptReducer.ts`、`SessionTurn.tsx` | 只显示状态、目标、字符数、打开预览按钮；不展示正文和 JSON | ChatView 不再出现长正文或 raw draft payload |
| reasoning 卡 | `transcript-presentation.ts`、`SessionTurn.tsx` | reasoning 与 draft status 分离；正文停滞时不让 reasoning 卡制造“还在生成文档”的错觉 | 文档停住时有明确状态说明 |
| raw lifecycle | `sessionRunTranscriptReducer.ts`、`SessionTurn.tsx` | PreToolUse/PostToolUse 等默认折叠到开发者详情，不进入主过程语言 | 普通用户不看到钩子英文名作为主卡标题 |

### G. 可观测性层

| 项目 | 当前落点 | 修改要求 | 验收 |
| --- | --- | --- | --- |
| provider 输出分类 | `stream_supervisor.py`、provider adapter | 记录最近 60 秒 output/reasoning/tool_delta 数量和时间 | 可判断“慢”是上游无正文、服务端缓冲、还是前端未消费 |
| draft 事件延迟 | `DocumentDraftLiveStream`、`_RemoteSessionRun`、extension | 记录 body token -> preview chunk -> extension apply 的时间差 | 能定位预览卡住发生在哪一段 |
| 工具语法失败 | `ToolExecutor`、remote peer | 按错误码统计 patch syntax fail | 能看到模型是否仍在生成旧语法 |

## 禁止项

开发期以架构统一为第一优先级，禁止用以下方式“临时缓解”：

- 不允许同时支持 `*** File:`、unified diff、`old_string/new_string` 等多套 patch 语法，除非重新写 ADR 并替换全部合同。
- 不允许恢复 `write_file` / `edit_file` 作为模型可见工具。
- 不允许让 `shell` 写文件绕过审批和 diff。
- 不允许 preview 失败后仍发 approval request。
- 不允许把 `document_draft_commit` 扩展成修改已有文件的第二套协议。
- 不允许把 reasoning token 当作文档正文进度。
- 不允许为了兼容历史事件继续暴露普通用户看不懂的 raw hook 卡片。

## 执行顺序

### 第 1 步：建立工具合同基线

产物：

- 新增 `apply_patch` grammar contract 文档或 fixture。
- Python/Go 共享合法和非法样例。
- prompt 快照测试。

最小必须覆盖样例：

```text
合法 Add File
合法 Update File
合法 Delete File
合法 Move
非法：*** File:
非法：*** Action:
非法：--- /dev/null unified diff
非法：缺少 *** End Patch
非法：Add File 正文缺少 +
非法：Update File 没有 @@
```

### 第 2 步：语义预校验前移

产物：

- `ApplyPatchTool.preflight_validate()` 调 parser。
- parser 返回结构化错误码和修复提示。
- `ToolExecutor` 对 preflight semantic error 发模型可读 retry hint。

验收：

```text
模型提交无效 patch
-> 不发 approval_request
-> 不发 file_change_started
-> 返回带正确示例的工具错误
```

### 第 3 步：重排文件变更事件生命周期

产物：

- `tool_call_delta` 仍实时显示“准备中”。
- 只有 preview 成功才创建 `file_change`。
- preview 失败有独立状态。

验收：

```text
半截 patch 参数流
-> UI 只显示工具准备中
-> 流中断后显示工具参数中断
-> 不出现文件修改卡
```

### 第 4 步：远端 peer 合同回归

产物：

- Python/Go parser 共享 fixtures。
- expected state 空状态测试保留。
- stale preview 测试保留。

验收：

```text
preview 后文件被改
-> Go execute 返回 stale preview
-> 文件不变
```

### 第 5 步：长文档 draft 恢复闭环

产物：

- active draft 中断后保留 checkpoint。
- UI 区分 `streaming`、`body_stalled`、`interrupted_recoverable`、`committing`。
- 继续生成时从最后 draft content 续写。

验收：

```text
文档正文输出到半句后 provider 中断
-> VS Code preview 保留半句
-> ChatView 显示草稿中断可继续
-> 继续后不改用 apply_patch 长参数
```

### 第 6 步：前端展示收敛

产物：

- 普通用户主链路只显示：思考、探索、文件变更、文档草稿、审批、结果。
- raw hook、raw args、raw event refs 默认进入开发者详情。
- approval diff 成功审批后继续自动关闭。

验收：

```text
普通聊天任务
-> 不出现 PreToolUse/PostToolUse 主标题
-> 不出现大段原始 JSON 参数
-> 文件审批只在 diff 可用时出现
```

### 第 7 步：慢链路定位指标

产物：

- provider output/reasoning/tool_delta 分段计数。
- draft preview chunk 延迟指标。
- extension apply event 延迟日志。

验收：

```text
用户报告“卡住一分钟”
-> 能判断是 provider 无正文、provider 只吐 reasoning、server 未 flush、extension 未 apply，还是 markdown preview 未刷新
```

## 回归测试清单

### Python

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests\extensions\tools\test_apply_patch_tool.py `
  tests\domain\agent\test_tool_execution.py `
  tests\domain\agent\test_document_draft.py `
  tests\domain\session\test_document.py `
  tests\labrastro_server\http\test_protocol.py `
  tests\extensions\tools\test_remote_backend_dispatch.py `
  --basetemp .pytest-tmp-tool-contract `
  -o cache_dir=.pytest-cache-tool-contract
```

必须新增或确认：

- 无效 patch grammar 在 preflight 阶段失败。
- preview 失败不请求 approval。
- 半截 tool arg 中断不创建 file_change。
- `operations=[]` 不生成 expected_state。
- `old_exists=False` 保持可校验。
- draft interruption 保留 checkpoint。

### Go

```powershell
go test ./internal/tools ./internal/protocol
```

必须新增或确认：

- Go parser 与 Python fixture 一致。
- invalid patch 返回稳定错误码。
- expected state stale preview 仍生效。
- `draft_document_commit` 仍 create-only。

### VS Code extension

```powershell
npm run typecheck:extension
npm run typecheck:webview
npx vitest run `
  src/DraftDocumentProvider.test.ts `
  src/ApprovalDocumentProvider.test.ts `
  src/coordinators/SessionRunCoordinator.test.ts `
  src/LabrastroController.chat-stream.test.ts `
  webview-ui/src/chat/sessionRunTranscriptReducer.test.ts `
  webview-ui/src/components/chat/SessionTurn.test.ts
```

必须新增或确认：

- `document_draft_preview_chunk` 更新 virtual markdown。
- draft stalled/interrupted 状态不会变成 assistant 正文。
- invalid patch 不显示 file change diff。
- approval 成功关闭对应 diff tab，失败不关闭。
- raw lifecycle 默认不进入主过程卡标题。

## 运营排查命令

遇到“慢、卡住、半句中断”时，先按下面顺序看证据：

```powershell
rg -n "document_draft_preview_chunk|document_draft_progress|document_draft_snapshot|provider_stream_interrupted|reasoning_delta|assistant_delta|tool_call_delta" <session-log-or-db-export>
```

判断：

```text
有 document_draft_preview_chunk 持续增加
  -> 前端或 markdown preview 刷新问题

没有 document_draft_preview_chunk，但 reasoning_delta 持续增加
  -> 上游模型在 reasoning，不是在输出文档正文

没有任何 output/reasoning/tool_delta
  -> provider 或网络流中断/空闲

有 tool_call_delta 但没有 tool_call_end
  -> 工具参数流中断，不能当作已执行工具

有 file_change_started 但 preview 不成功
  -> 违反本纠偏合同，必须修事件生命周期
```

## 最终验收标准

本轮系统性修复完成后，必须同时满足：

1. 模型生成 `*** File:` 或 unified diff 时，不会出现审批，也不会出现文件变更卡；模型会收到明确正确语法示例。
2. 模型生成合法 `apply_patch` 时，用户只审批真实 diff；批准后远端 execute 校验 expected state。
3. 长 Markdown 文档不会通过 `apply_patch` 长参数传输。
4. 文档预览卡住时，UI 能区分“上游没正文”“正在 reasoning”“流中断”“前端未刷新”。
5. provider 中断不会让 draft 悄悄丢失；必须保留可继续 checkpoint。
6. ChatView 主流程没有普通用户看不懂的 hook 英文名和原始 JSON。
7. Python、Go、extension 三边都有防漂移测试。

## 维护准则

后续任何文件变更、长文档、审批、流式事件改动，都必须先回答四个问题：

```text
1. 这是模型可见合同，还是 runtime 内部能力？
2. 这是工具参数流，正文流，文件变更流，还是 reasoning 流？
3. 用户看到的是可审批事实，还是只是一段准备中的参数？
4. Python、Go、前端是否已经有同一组 fixture 或快照测试？
```

只要有一个问题回答不清楚，就不能继续实现。否则同类漂移会再次出现。

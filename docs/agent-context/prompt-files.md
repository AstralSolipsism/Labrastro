# Prompt File Ownership

本仓库的提示词入口分工如下：

| 文件 | 读者 | 职责 |
| --- | --- | --- |
| `AGENTS.md` | Codex | 后端开发宪法，主事实源 |
| `AGENT.md` | ReuleauxCoder runtime | 与 `AGENTS.md` 保持一致的后端开发宪法 |
| `CLAUDE.md` | Claude | Claude 入口适配，指向 `AGENTS.md` |
| `docs/agent-context/backend-runtime-map.md` | 所有 agent | 原 `AGENT.md` 项目百科和架构地图 |

规则：

- `AGENTS.md` 是开发规则的主事实源。
- `AGENT.md` 为 runtime 注入保留，语义必须与 `AGENTS.md` 一致。
- `CLAUDE.md` 不维护独立开发规则。
- 架构百科放在 `docs/agent-context/backend-runtime-map.md`，不要塞回入口提示词。

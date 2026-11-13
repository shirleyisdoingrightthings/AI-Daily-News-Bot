# AI Daily News Bot — Claude Code 专属上下文

> 完整操作手册见 [`AGENTS.md`](./AGENTS.md)（通用，适用于所有 AI 工具）。  
> 本文件只记录 Claude Code 专属的额外指令。

---

## 必读顺序

1. 先读 `AGENTS.md` — 获取完整架构、约定、禁区
2. 再读本文件 — 获取 Claude 专属行为指令

---

## Claude 专属指令

### 自动加载行为
Claude Code 打开此目录时，自动加载本文件。请同时主动读取：
- `AGENTS.md`（完整上下文）
- `logs/run.log`（最近运行状态）
- `changelog.md`（当前已知问题，如存在）

### 运行约定（Option B）
- 当前定时调度与开机唤醒补跑均由 Antigravity Sidecar 负责；`launchd` 仅保留轻量化健康检查与 Level 1 重发兜底（不再拉起无头 CLI）。

### 对话风格
- 回复使用中文
- 代码改动前先说明改动范围和理由
- 不确定时主动说明，不要猜测后直接修改


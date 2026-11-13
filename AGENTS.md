# AI Daily News Bot — AI 操作手册

> **通用文件**：适用于 Antigravity、Cursor、GitHub Copilot 等 AI 工具。

---

## 工作流概述

这是一个 **AI 产业日报系统**。每天 15:00 由 Antigravity Sidecar 定时任务触发：`daily_report.py --mode fetch` 抓取资讯（best-effort 抓正文全文，失败回退 RSS 摘要）→ AI 按 `prompt.md` 写稿落盘 `logs/report_draft.txt` → `daily_report.py --mode send` 推送飞书并同步归档 Notion。脚本本身零第三方大模型 API、零 token 成本。

**每日统一出当日客观日报（已永久取消周日《AI 产业周回顾》）**：全局唯一周趋势回顾收敛至周日 `github-hn-bot`。

### 数据流

```text
RSS 源 ──▶ daily_report.py --mode fetch
            ├─ build_ai_context()（24h 窗口 + 7 天 sent_urls 去重）
            ├─ 并发 best-effort 抓正文全文（失败回退 RSS 摘要）
            └─ 输出 logs/last_context.txt + stdout（=== FETCH_OK ===）
                  ▼
            AI 按 prompt.md 写稿（最多入选 12 条）→ logs/report_draft.txt
                  ▼
            daily_report.py --mode send ──▶ 飞书（AI 产业日报）+ Notion 看板归档
```

### 自动化调度（方案 B）

```text
15:00  Antigravity Sidecar（ai-daily-afternoon，Part A）
         claude_report.sh fetch → 写稿 → claude_report.sh send → run.log [OK/FAIL]

16:00  launchd（com.shirley.ai-chain-health）→ health_check.sh
         ├─ [OK / 无有效新闻]  记录连续正常天数 / 正常终止
         ├─ [无记录]           发告警提示；开机唤醒补跑由 Antigravity Sidecar 负责（不再拉起无头 CLI）
         └─ [FAIL]             若 drafts_fresh 则 Level 1 重跑 send；否则发告警通知人工介入
```

---

## 文件结构与职责

| 文件 | 职责 |
|---|---|
| `daily_report.py` | 主脚本：`--mode fetch` / `--mode send`（清洗 HTML、推送飞书、归档 Notion、写日志） |
| `claude_report.sh` | 命令行封装，委托 `../shared/report_base.sh` 从 plist 加载环境变量后执行 `daily_report.py` |
| `prompt.md` | 当日日报写稿规范（唯一权威源） |
| `../shared/bot_utils.py` | 共享工具库：代理自愈（`127.0.0.1:1082`）、RSS 抓取、正文提取、跨天去重、分页 |
| `../shared/notion_archive.py` | Notion「每日情报归档与决策看板」同步模块 |
| `../shared/scheduled-tasks/ai-daily-afternoon/SKILL.md` | 15:00 定时任务指令版本化备份 |
| `health_check.sh` / `auto_repair.sh` | 轻量化健康检查与 Level 1 重发兜底（委托 `../shared/`） |
| `logs/report_draft.txt` | 当日写好的纯正文（`write_file Overwrite: true` 写入） |
| `logs/last_context.txt` | `fetch` 完整素材落盘 |
| `logs/fetch_meta.json` | `fetch` 边车指标（供 `send` 回填） |
| `logs/run.log` / `logs/run.jsonl` | 单行人类可读日志 & 结构化指标日志 |
| `logs/sent_urls.json` | 7 天跨天去重档案 |
| `logs/.zero_streak.json` | 各源连续零产天数（`fetch` 单点写入，连续 3 天告警） |
| `pending_messages.json` | 发送前持久化备份（防消息丢失） |
| `com.shirley.ai-daily-news-bot.plist.example` | `~/Library/LaunchAgents/` 环境变量模板（含 `1082` 代理端口与 `FEISHU_WEBHOOK`） |

---

## 关键约定（修改前必读）

### 日志格式（不得改动）
```text
YYYY-MM-DD HH:MM  [OK/FAIL/WARN]  消息内容
```
`health_check.sh` 依赖 `[FAIL]` 字符串匹配与 `NO_NEWS_PATTERN="无有效新闻"`，改动格式会导致健康检查失效。

### 稿件格式（HTML 中间格式 → 飞书卡片 markdown）
- 只能使用 `<b>` 和 `<a href="...">` 两种标签，禁止 Markdown `**`；
- `bot_utils.html_to_lark_md` 会将其翻译为飞书 markdown（`<a href><b>标题</b></a>` → `**[标题](url)**`）；
- 单条消息预算约 `20KB`，超长时由 `bot_utils.paginate_feishu` 按段落切分并加页码。

### 分源零产监控与时效
- 默认收录 **24 小时内**新闻（`DEFAULT_WINDOW_H = 24`），RSS 抓取最多重试 2 次（退避 3 → 6s），飞书推送最多重试 3 次；
- `logs/.zero_streak.json` 由 `fetch` 单点写入，连续 3 天零产触发 `=== SOURCE_ALERT ===`。

### 代理
- 统一走 `http://127.0.0.1:1082`（由 `~/Library/LaunchAgents/com.shirley.ai-daily-news-bot.plist` 配置）；
- 推送飞书使用 `trust_env=False` 直连；Notion 归档优先走代理并保留直连兜底。

---

## 调试入口

```bash
tail -5 logs/run.log
tail -3 logs/run.jsonl | python3 -m json.tool
bash claude_report.sh fetch
bash claude_report.sh send
bash health_check.sh
launchctl list | grep shirley
```

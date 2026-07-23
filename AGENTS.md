# AI Daily News Bot — AI 操作手册

> **通用文件**：适用于 Claude Code、Cursor、GitHub Copilot、Codex 等任何 AI 工具。  
> Claude Code 专属上下文见 `CLAUDE.md`（在此基础上做了精简，避免冗余）。

---

## 工作流概述

这是一个 **AI 产业日报系统**。每早由本地 Claude 定时任务触发：`daily_report.py --mode fetch` 抓取资讯（best-effort 抓正文全文，失败回退 RSS 摘要）→ Claude 按 `prompt.md` 写稿 → `daily_report.py --mode send` 推送 Telegram。脚本本身只做抓取与发送，零第三方大模型 API、零 token 成本。

### 数据流

```
[数据源]                          [抓取 / 写稿]                     [输出]
RSS × 10 源 ──▶ daily_report.py --mode fetch
（The Verge / TechCrunch /         │  build_ai_context()
 VentureBeat / Wired /             │  ├─ 并发 best-effort 抓正文全文
 MIT Tech Review / Engadget /      │  └─ 抓不到 → 回退 RSS 摘要
 IEEE / Ars Technica /             ▼
 The Decoder）              Claude 按 prompt.md 写稿 → logs/report_draft.txt
                                   ▼
                            daily_report.py --mode send ──▶ Telegram（AI 产业日报）
```

### 自动化调度

```
08:30  Claude 定时任务（唯一写稿入口）
         claude_report.sh fetch → Claude 写稿 → claude_report.sh send
                       │
                  run.log [OK/FAIL]

09:45  launchd → health_check.sh
                       │
           ┌── [OK] ───┼── .ok_streak +1
           │                streak ≥ 3 → 删除 changelog 中 [x] 条目
           │
           ├── [无记录] ── claude_catchup.sh 无头补跑（自动版 Run Now）
           │                claude CLI 重走 fetch → 写稿 → send；同一天只补跑一次
           │
           └── [FAIL] ─── changelog 新增 [ ] 条目
                       └─▶ auto_repair.sh（后台触发；先查当日稿件，缺稿直接转无头补跑）
                                 │
                         瞬时错误？
                         ├─ Yes → 等 30s → 重跑 send
                         │         ├─ 成功 → changelog [x]
                         │         └─ 失败 → 升级
                         └─ No  → claude CLI 分析修复 → 重跑 send
                                   ├─ 成功 → changelog [x]
                                   └─ 失败 → claude_catchup.sh 无头补跑
                                             ├─ 成功 → changelog [x]
                                             └─ 失败 → macOS 通知 → 人工介入
```

---

## 文件结构与职责

| 文件 | 职责 | 修改频率 |
|------|------|---------|
| `daily_report.py` | 主脚本：`--mode fetch`（抓取+抓正文）/ `send`（清洗+推送），零第三方大模型 API | 偶尔 |
| `claude_report.sh` | 供 Claude 定时任务调用的 fetch/send 封装（从 plist 加载环境变量） | 极少 |
| `prompt.md` | 写稿规范（唯一权威源，Claude 依此写稿） | 偶尔 |
| `~/bots/shared/bot_utils.py` | 共享工具库（两个 Bot 共用）：sanitize_html / with_retry / fetch_rss / parse_entry_date / already_ran_today / fetch_article_text（抓正文） | 偶尔 |
| `health_check.sh` | 检查 run.log，触发 auto_repair | 极少 |
| `auto_repair.sh` | 两级自动修复代理（委托 `~/bots/shared/auto_repair_base.sh`；重跑走 claude_report.sh send，先做当日稿件新鲜度检查） | 极少 |
| `claude_catchup.sh` | 无头补跑薄包装（委托 `~/bots/shared/headless_catchup_base.sh`）：当天未出稿或自愈失败时由 claude CLI 完整重走流程；同一天只补跑一次（logs/.catchup_ran 戳记） | 极少 |
| `logs/report_draft.txt` | 当日 Claude 写好的稿子（send 读取后推送） | 每日写入 |
| `logs/fetch_meta.json` | fetch 边车：日志摘要 + 指标（send 回填，供体检监控） | 每日写入 |
| `logs/run.log` | 单行摘要日志（人类可读） | 每日写入 |
| `logs/run.jsonl` | 结构化指标（程序可读） | 每日写入 |
| `logs/sent_urls.json` | 跨天去重档案：已推送链接 → 日期（保留 7 天，send 成功后写入） | 每日写入 |
| `logs/.zero_streak.json` | 各源连续零产天数（health_check 维护，达 3 天才告警） | 每日写入 |
| `logs/launchd.log` | （历史）旧 09:15 launchd 兜底的 stdout/stderr，兜底已移除，不再写入 | 不再写入 |
| `logs/health_check.log` | health_check 运行日志 | 每日写入 |
| `logs/headless_catchup.log` | 无头补跑运行日志 | 触发时写入 |
| `changelog.md` | 问题追踪，与 health_check 联动 | 按需 |
| `pending_messages.json` | Telegram 发送缓存（降级保护） | 临时 |
| `com.shirley.ai-daily-news-bot.plist.example` | 环境变量 plist 模板（正式配置在 `~/Library/LaunchAgents/`，是端口/密钥的唯一权威源，`claude_report.sh` 从中读环境变量；不含调度，09:15 launchd 兜底已于 2026-07 移除，失败兜底由 health_check + auto_repair 承担） | 极少 |
| `com.shirley.ai-daily-news-bot-health.plist` | health_check launchd 配置（09:45 触发） | 极少 |

---

## 关键约定（修改前必读）

### 数据源与 API

| 类别 | 来源 | 条数 | 备注 |
|-----|------|------|------|
| 主流科技（5 条） | The Verge (AI) / TechCrunch / VentureBeat (AI) | 各 5 条 | 免费 RSS |
| 主流科技（4 条） | Engadget | 4 条 | 免费 RSS |
| 深度/垂直（3 条） | The Verge (Reviews) / Wired / MIT Tech Review / IEEE Spectrum / Ars Technica / The Decoder | 各 3 条 | 免费 RSS |
| 正文抓取 | 各新闻源文章页 | - | best-effort 抓正文（JSON-LD `articleBody` / `<p>` 启发式），失败回退 RSS 摘要，零依赖 |

### 日志格式（不得改动）
```
YYYY-MM-DD HH:MM  [OK/FAIL/WARN]  消息内容
```
`health_check.sh` 依赖 `[FAIL]` 字符串匹配，改动格式会导致健康检查失效。

### Telegram 输出格式
- 所有 AI 输出必须是 **HTML 格式**，禁止 Markdown
- 只能使用 `<b>` 和 `<a href="...">` 两种标签
- 单条消息上限 4096 字符；超长由 `bot_utils.paginate_telegram` 按段落边界切分，
  **每条顶部加 `<b>（n/N）</b>` 页码**（单条不加）。页码在 sanitize 之后拼接，切分点不会腰斩条目

### 分源零产监控与源淘汰
- 每次 fetch 把各源 `{fetched, kept}` 写入 JSONL 的 `rss_source_stats`；`rss_zero_sources` 列出**过滤后一条都没剩**的源
- 判定口径是"过滤后零产"而非"RSS 拉到 0 条"——源可能天天拉得到却条条过期/重复/已播，旧口径发现不了
- `logs/.zero_streak.json` 累计各源连续零产天数，**由 fetch 单点写入**（`update_zero_streak`），health_check 只读不写，避免两处各加一次把天数翻倍
- 连续 **3 天**零产 → fetch 的 stdout 输出 `=== SOURCE_ALERT ===` 块（routine 会在日报汇报里单列「RSS 源健康」），同时写入 metrics 的 `rss_stale_sources`，health_check 据此发 macOS 通知
- 收到告警即可把该源从脚本的 `RSS_SOURCES` 移除或更换；源一旦恢复产出，计数自动清零并移出档案

### 新闻时效
- AI Daily News Bot 收录 **24 小时内**新闻（`timedelta(days=1)`）

### 代理
- 固定走 `127.0.0.1:YOUR_PORT` (本地代理端口)
- 端口在 `~/Library/LaunchAgents/com.shirley.ai-daily-news-bot.plist` 的 `HTTP_PROXY`/`HTTPS_PROXY` 里配置（唯一权威源）；改完即生效，`claude_report.sh` 每次运行时直接读文件，无需重载 launchd
- `requests` 通过 `SESSION` 显式配置，`feedparser` 通过 `HTTP_PROXY` 环境变量

### 重试策略
- Telegram：最多 3 次，指数退避（5 → 10 → 20s）
- RSS 抓取（fetch_rss）：最多 2 次，退避 3 → 6s
- 正文抓取（fetch_article_text）：best-effort、单次、失败即回退 RSS 摘要，不重试

### 消息缓存降级
- send 模式发送前把稿子写入 `pending_messages.json`；代理不可用时也缓存
- Telegram 发送成功后删除该文件
- 缓存用于避免内容丢失（可人工恢复），当前 Claude 流程不做自动重发

---

## 修改禁区

| 禁止操作 | 原因 |
|---------|------|
| 修改 `run.log` 的 `[OK]/[FAIL]/[WARN]` 格式 | health_check.sh 依赖字符串匹配 |
| 删除 `flush_pending()` 调用 | 会导致失败消息永久丢失 |
| 修改 PROMPT 中的 HTML 输出格式 | Telegram 不支持 Markdown |
| 将 `timedelta(days=1)` 改小 | 会漏掉重要新闻 |
| 修改 `with_retry` 的 exceptions 参数 | 会影响重试覆盖范围 |
| 替换核心 RSS 源 | 确保数据抓取的广度与质量 |
| 修改 `daily_report.py` 的 HTML 清洗逻辑 | 防止 Telegram 消息推送由于标签不规范而失败 |

---

## 调试入口

```bash
# 查看最近运行状态
tail -5 logs/run.log

# 查看结构化指标（含耗时）
tail -3 logs/run.jsonl | python3 -m json.tool

# 查看当前问题清单
cat changelog.md

# 手动抓取 / 发送（Claude 定时任务用同一封装）
bash claude_report.sh fetch     # 抓取 + 抓正文，输出写稿素材
bash claude_report.sh send      # 读取 logs/report_draft.txt 并推送

# 手动运行健康检查
bash health_check.sh

# 查看 launchd 任务状态
launchctl list | grep shirley
```

---

## AI 工具使用说明

### 如果你是 Claude Code
自动加载 `CLAUDE.md`（内含额外的 Claude 专属指令）。本文件提供完整上下文。

### 如果你是 Cursor / GitHub Copilot / 其他工具
直接阅读本文件（`AGENTS.md`）即可获得完整上下文。
如果工具支持自定义规则文件，将本文件路径加入即可：
- Cursor：将内容复制到 `.cursorrules`
- GitHub Copilot：将内容复制到 `.github/copilot-instructions.md`

### Auto-Repair 代理行为规范
当 `auto_repair.sh` 调用 Claude CLI 时，Claude 应当：
1. 只修复**最小范围**的问题
2. 修复后必须输出 `FIX: <一行说明>` 或 `CANNOT_FIX: <原因>`
3. 不得触碰修改禁区中的任何内容
4. 如果不确定根因，选择 `CANNOT_FIX` 而不是盲目修改


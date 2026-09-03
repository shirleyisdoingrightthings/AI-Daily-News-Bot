# AI Daily News Bot

每天早上由 Claude 自动写稿：抓取 9 家全球顶级 AI 媒体的当日报道，去重、评分、提炼，生成一份结构化《AI 产业日报》推送到飞书。

---

## 核心特性

**数据来源 — 10 个 feed，覆盖主流、算力硬件、半导体深度到机器人产业**

| 类型 | 媒体 | 闸门 |
|------|------|------|
| AI 垂直频道 | The Verge (AI) · TechCrunch (AI) · Wired (AI) · The Decoder · Ars Technica (AI) | 只过低价值闸门 |
| 半导体/算力深度 | SemiAnalysis (newsletter) | 只过低价值闸门，窗口 120h |
| 机器人/具身智能 | The Robot Report · TechCrunch (机器人，窗口 48h) | 只过低价值闸门 |
| 泛科技全站源 | Engadget · Tom's Hardware | 过 AI 相关性闸门 + 低价值闸门 |

不收厂商官方博客（OpenAI / Google / DeepMind 等）——发文稀疏、软文占比高，重要官宣当天必被上述垂直源覆盖。

每日抓取约 55 条原始内容，经"跨天去重 → 时效窗 → 相关性闸门 → 低价值闸门"四层过滤后进入写稿素材。时效窗默认 24h，个别源单独放宽（SemiAnalysis 120h、TechCrunch 机器人 48h）。

> 泛科技源里大量条目与 AI 无关（游戏机 demo、Steam 功能更新、影视并购），实测约
> 30% 的抓取额度被这类内容占掉。故对它们单独加一道 `is_ai_relevant` 闸门，而 AI
> 垂直频道不过闸——避免误伤标题里不含关键词的正当选题。
>
> 机器人两个源是 2026-09-03 加的。此前 14 天存档里 `#机器人` 标签只用过 8 次，
> 而 Figure、Unitree/宇树、波士顿动力、世界模型、具身智能**一次都没出现过**——抓到的
> 全是泛科技媒体顺带写的消费级机器人，机器人产业本身是空白。The Robot Report 日更、
> 产业向，但它是 RoboBusiness 大会的主办方媒体，feed 里常年混着会议引流稿，靠低价值
> 闸门拦。**IEEE Spectrum 机器人试过没加**：质量最高但周更成簇，实测最新一条已是 133
> 小时前，给到 120h 窗口仍然零产，当日报源只会天天触发零产告警。
>
> Tom's Hardware 是 2026-09-03 加的算力硬件补充源。它没有 AI 分类 feed（分类页的
> `/feed` 是 301 到 HTML，`/feeds/tag/ai.xml` 全是 404），只能用全站 `feeds.xml`；
> 而 `is_ai_relevant` 的关键词表里有 `nvidia` / `gpu`，显卡促销和装机评测会直接骗过
> 闸门。故对泛科技源再加一道 `_PROMO_RE` 标题闸门拦掉 "Save $300…"、"… review:"
> 这类条目。实测最新 14 条里保留 4 条，全部是数据中心 / HBM / AI 安全选题。

**取材 — 优先抓正文全文，而不是只吃 RSS 摘要**

- 选中的新闻会 best-effort 抓取文章正文全文（JSON-LD `articleBody` → `<p>` 启发式，纯标准库零依赖）
- 抓取失败 / 被反爬拦截 / 正文过短时，自动回退到 RSS 摘要，绝不因此漏发
- 正文信息量远大于摘要，让 The Details 能写出摘要里没有的具体数字与细节

**内容处理 — 去噪，不是聚合**

- **跨源去重**：同一事件多家报道时自动合并、保留最权威来源
- **跨天去重**：`send` 成功后归档稿件里实际用到的链接（保留 7 天），下次抓取自动排除，同一条新闻不会隔天再上一次
- **价值评分**（3 / 4 / 5 分制）：5 星事件配完整 Details，3 星只占一格，过滤信息噪声
- **自动分类**：模型发布 / 产品动态 / 公司动向 / AI 政策 / 基础设施 / 机器人

**报告结构 — 30 秒能看完，也能深读**

- **速览**：开头固定 5 条，每条带星级 + 至少一个具体数字，与正文前 5 条一一对应——只看速览就能掌握当天要点
- **产业动态**：每条为「加粗超链接标题（点标题直达原文）+ 📄 The Details（逐条具体事实，句句自足）」
- **超长自动分页**：超过飞书 webhook 单条 20KB 请求体上限时按段落边界切分，每条顶部标 `（n/N）` 页码，条目不会被腰斩；切完还会二次均衡，避免出现只有两行的尾页
- **周日出周回顾**：北京时间周日不发当日日报，改发《AI 产业周回顾》——以本周每天已推送稿件的存档为素材，跨天合并同一线索、按一周尺度重新评分（只留 4 分以上）、按主题分组，另附「本周数字」。周日仍照常抓最近 24h 并入回顾，周末的新闻不会漏掉

**稳定性 — 出了问题自己修**

- 每日体检：11:00 检查当天是否成功出稿，异常自动记 changelog 并触发自愈
- 两级自愈：瞬时故障等 30 秒重跑；持续故障调用 Claude CLI 诊断修复
- 无头补跑：当天根本没出稿（机器睡眠错过 10:00）或自愈无效时，claude CLI 自动完整重走一遍流程（自动版 Run Now）
- 消息缓存：发送失败 / 代理不可用时，把稿子缓存到 pending_messages.json，避免内容丢失
- 源淘汰监测：统计每个 RSS 源"过滤后还剩几条"，连续 3 天零产即告警建议移除（详见下文）

---

## 系统架构与工作流

```
[数据源]                          [抓取 / 写稿]                     [输出]
RSS × 10 源 ─▶ daily_report.py --mode fetch
（8 个 AI/半导体/机器人垂直源 +    │  build_ai_context()
  2 个泛科技全站源）               │  ├─ URL 去重（单次运行内）
                                   │  ├─ 跨天去重（排除 logs/sent_urls.json）
                                   │  ├─ 时效窗（默认 24h，按源可调）
                                   │  ├─ AI 相关性闸门（仅泛科技源）
                                   │  ├─ 低价值闸门（全源：促销/评测/会议）
                                   │  ├─ 并发 best-effort 抓正文全文
                                   │  └─ 抓不到 → 回退 RSS 摘要
                                   ▼
                            Claude 按 prompt.md 写稿
                                   │  → logs/report_draft.txt
                                   ▼
                            daily_report.py --mode send
                                   │  清洗 HTML → 转飞书卡片 markdown
                                   │  超 20KB → 按段落分页 + (n/N) 页码
                                   │  推送成功 → 归档链接供跨天去重
                                   ▼
                               飞书（AI 产业日报）

【自动化调度】
10:00  Claude 定时任务（唯一写稿入口）
         └─ claude_report.sh fetch → Claude 写稿 → claude_report.sh send → run.log [OK/FAIL]

11:00  launchd ──▶ health_check.sh
                        │
                  [OK] ──┼── .ok_streak +1（连续 3 次后清理已解决的 changelog 条目）
                        │
                  [无记录] ── claude_catchup.sh 无头补跑（自动版 Run Now：
                        │       claude CLI 完整重走 fetch → 写稿 → send，同一天只补跑一次）
                        │
                  [FAIL] ── changelog 新增条目
                        └──▶ auto_repair.sh（前台运行；缺当日稿件时直接转无头补跑）
                                  ├─ Level 1：等 30s 重跑 send（瞬时网络错误）
                                  ├─ Level 2：claude CLI 诊断修复 → 重跑 send
                                  └─ 最终兜底：claude_catchup.sh 无头补跑
                                              ├─ 成功 → changelog 标记 [x]
                                              └─ 失败 → macOS 通知，需人工介入
```

> 写稿由本地 Claude 定时任务完成（抓取 → 按 `prompt.md` 写稿 → 推送）。`daily_report.py` 本身只负责抓取（`fetch`）与发送（`send`）两件事，全程零第三方大模型 API、零 token 成本。

---

## RSS 源健康与淘汰

一个 RSS 源可能天天拉得到条目、却条条被过滤（过期 / 重复 / 已播 / 与 AI 无关），
对日报的实际贡献长期为零。旧口径只统计"RSS 拉到 0 条"，发现不了这种空转。

现在的判定口径是**过滤后零产**：

| 环节 | 行为 |
|---|---|
| `fetch` | 记录每个源的 `{fetched, kept}` 到 `run.jsonl` 的 `rss_source_stats` |
| `fetch` | 过滤后 `kept == 0` 的源计入 `logs/.zero_streak.json`，连续天数 +1；有产出则清零并移出档案 |
| `fetch` | 连续 **3 天**零产 → stdout 输出 `=== SOURCE_ALERT ===` 块，并写入 metrics 的 `rss_stale_sources` |
| 10:00 routine | 读到 SOURCE_ALERT 后，在日报汇报末尾单列「RSS 源健康」，说明哪个源连续几天没贡献、可以移除或更换 |
| 11:00 health_check | 读 metrics 发 macOS 通知；单日零产只记 INFO 不打扰 |

连续天数由 `fetch` **单点写入**，health_check 只读不写——两处各加一次会让天数翻倍。

收到告警后，把该源从 `daily_report.py` 的 `RSS_SOURCES` 里删掉或换成新源即可。

---

## 文件结构

```
~/Desktop/bots/shared/bot_utils.py     # 外部共享工具库（含抓正文 fetch_article_text，三个 bot 共用）
~/Desktop/bots/shared/auto_repair_base.sh         # 共享修复逻辑（三个 bot 共用）
~/Desktop/bots/shared/headless_catchup_base.sh    # 共享无头补跑逻辑（自动版 Run Now，三个 bot 共用）

AI Daily News Bot/
├── daily_report.py                    # 主脚本：--mode fetch（抓取+抓正文）/ send（清洗+推送）
├── claude_report.sh                   # 供 Claude 定时任务调用的 fetch/send 封装（从 plist 加载环境变量）
├── prompt.md                          # 当日日报的写稿规范（唯一权威源，Claude 依此写稿）
├── prompt_weekly.md                   # 周日《AI 产业周回顾》的写稿规范
├── health_check.sh                    # 健康检查（失败时触发 auto_repair）
├── auto_repair.sh                     # 薄包装：设置参数后委托 ~/Desktop/bots/shared/auto_repair_base.sh
├── claude_catchup.sh                  # 薄包装：无头补跑（委托 ~/Desktop/bots/shared/headless_catchup_base.sh）
├── logs/                              # 所有日志与产物集中存放（运行时生成）
│   ├── report_draft.txt              # 当日 Claude 写好的稿子（send 读取后推送；日报与周回顾共用）
│   ├── archive/YYYY-MM-DD.txt        # 已推送稿件按日存档（保留 14 天），周回顾的素材来源
│   ├── fetch_meta.json               # fetch 边车：日志摘要 + 指标（send 回填，供体检监控）
│   ├── run.log                        # 单行摘要日志（人类可读）
│   ├── run.jsonl                      # 结构化指标日志（程序可读，含分源 fetched/kept 统计）
│   ├── sent_urls.json                 # 跨天去重档案：已推送链接 → 日期（保留 7 天）
│   ├── .zero_streak.json              # 各源连续零产天数（fetch 单点写入，达 3 天告警）
│   ├── health_check.log              # health_check 运行日志
│   └── .ok_streak                     # 连续成功计数
├── changelog.md                       # 问题追踪，与 health_check 联动
├── pending_messages.json              # 推送缓存（仅飞书推送失败时存在）
├── AGENTS.md                          # 通用 AI 操作手册（适用于任意 AI 工具）
├── CLAUDE.md                          # Claude Code 专属上下文（引用 AGENTS.md）
├── com.shirley.ai-daily-news-bot.plist.example       # 环境变量 plist 模板（正式配置在 ~/Library/LaunchAgents/，是端口/密钥的唯一权威源；不含调度，09:15 launchd 兜底已于 2026-07 移除）
├── com.shirley.ai-daily-news-bot-health.plist        # health_check launchd 配置（11:00 触发）
├── requirements.txt                   # Python 依赖清单
└── README.md                          # 本文件（人类阅读）
```

> `logs/` 下的文件均为运行时自动生成，不预置。`pending_messages.json` 仅在飞书推送失败时存在。

---

## 环境变量

所有变量写在**唯一权威配置源** `~/Library/LaunchAgents/com.shirley.ai-daily-news-bot.plist` 中，`claude_report.sh` 从这里读取并自动注入，无需配置 shell profile。仓库内只保留 `.plist.example` 模板（不含密钥）。改端口/密钥请直接编辑 LaunchAgents 里那份，改完即生效（`claude_report.sh` 每次运行时直接读文件，无需重载 launchd）。

> 该 plist 已不承担任何调度职责：09:15 的 launchd 兜底于 2026-07 移除（它因缺 `--mode` 参数且 launchd 环境下 `import bot_utils` 失败，从未成功运行过），失败兜底由 11:00 的 health_check + auto_repair 承担。plist 仅作为环境变量配置源保留。

| 变量 | 说明 | 来源 |
|------|------|------|
| `FEISHU_WEBHOOK` | 飞书自定义机器人 webhook 地址 | plist（需手动填入） |
| `FEISHU_SECRET` | 机器人签名密钥（未开签名校验则留空） | plist（需手动填入） |
| `HTTPS_PROXY` / `HTTP_PROXY` | 本地代理地址，**仅抓取阶段使用**（推送飞书是直连） | plist（已配置，127.0.0.1:YOUR_PORT）|

---

## 快速开始

**手动抓取 / 发送（测试）**
```bash
cd ~/Desktop/bots/AI\ Daily\ News\ Bot
bash claude_report.sh fetch     # 抓取 + 抓正文，把写稿素材打到 stdout
# （由 Claude 依 prompt.md 写稿并存入 logs/report_draft.txt）
bash claude_report.sh send      # 读取 report_draft.txt，清洗 HTML 后推送飞书
```

**验证调度状态**
```bash
launchctl list | grep shirley
tail -5 logs/run.log
```

---

## 依赖安装

```bash
pip3 install requests feedparser
```

---

## 调试

```bash
tail -5 logs/run.log                          # 最近运行状态
tail -3 logs/run.jsonl | python3 -m json.tool # 结构化指标
cat changelog.md                              # 当前问题清单
bash health_check.sh                          # 手动触发健康检查
```

详细操作规范见 [`AGENTS.md`](./AGENTS.md)。

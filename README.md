# AI Daily News Bot

每天早上由 Claude 写稿：抓取 10 个 feed 的当日报道，去重、评分、提炼，生成一份
《AI 产业日报》推送到飞书。全程零第三方大模型 API——脚本只负责抓取与发送，稿子由
本地 Claude 定时任务按 `prompt.md` 写。

它是飞书「AI 日报」机器人的第一块（广度层），另两块是 X 热点（深度层）与
AI Builder（人物层），三块推到同一个 webhook。

## 数据来源

| 类型 | 媒体 | 闸门 |
|---|---|---|
| AI 垂直频道 | The Verge (AI) · TechCrunch (AI) · Wired (AI) · The Decoder · Ars Technica (AI) | 只过低价值闸门 |
| 半导体/算力 | SemiAnalysis (newsletter，窗口 120h) | 只过低价值闸门 |
| 机器人/具身智能 | The Robot Report · TechCrunch (机器人，窗口 48h) | 只过低价值闸门 |
| 泛科技全站源 | Engadget · Tom's Hardware | AI 相关性闸门 + 低价值闸门 |

每日抓约 55 条，经"跨天去重 → 时效窗 → 相关性闸门 → 低价值闸门"四层过滤后进入素材。
时效窗默认 24h，个别源单独放宽。不收厂商官方博客（发文稀疏、软文多，重要官宣当天
必被垂直源覆盖）。

每个源为什么在这里、为什么用这个 feed 地址而不是另一个、试过哪些没加，都写在
`daily_report.py` 的 `RSS_SOURCES` 注释里，改源之前先读那段。

## 主要行为

- 取材：选中的新闻会 best-effort 抓正文全文（JSON-LD → `<p>` 启发式，零依赖），
  失败或过短时回退 RSS 摘要，不因此漏发。正文比摘要信息量大得多。
- 去重：跨源合并同一事件；跨天靠 `logs/sent_urls.json`（留 7 天），同一条新闻不会隔天再上。
- 评分：3/4/5 分制，5 星配完整 Details，3 星只占一格。规则见 `prompt.md`。
- 分页：超过飞书 webhook 单条 20KB 上限时按段落切分并标 `(n/N)` 页码，条目不会被腰斩。
- 周日出周回顾：不发当日日报，改用本周存档（`logs/archive/`）跨天合并线索、
  按一周尺度重新评分，规范见 `prompt_weekly.md`。

## 工作流

```
RSS × 10 源 ─▶ daily_report.py --mode fetch
                 │  ├─ URL 去重（单次运行内）
                 │  ├─ 跨天去重（排除 logs/sent_urls.json）
                 │  ├─ 时效窗（默认 24h，按源可调）
                 │  ├─ AI 相关性闸门（仅泛科技源）
                 │  ├─ 低价值闸门（全源：促销/评测/会议）
                 │  └─ 并发抓正文，抓不到回退摘要
                 ▼
          Claude 按 prompt.md 写稿 → logs/report_draft.txt
                 ▼
          daily_report.py --mode send
                 │  清洗 HTML → 飞书卡片 markdown → 超长分页
                 │  推送成功 → 归档链接供跨天去重
                 ▼
            飞书「AI 产业日报」

10:00  Claude 定时任务（唯一写稿入口）
11:00  launchd → health_check.sh
         [OK]    .ok_streak +1，连续 3 次清理 changelog
         [无记录] claude_catchup.sh 无头补跑
         [FAIL]  记 changelog → auto_repair.sh
                   Level 1 等 30s 重跑 → Level 2 claude CLI 诊断 → 兜底无头补跑
```

## 源健康与淘汰

判定口径是过滤后零产，而不是"RSS 拉到 0 条"——一个源可能天天拉得到、却条条被过滤，
对日报的实际贡献长期为零，旧口径发现不了。

`fetch` 记录每个源的 `{fetched, kept}`，`kept == 0` 计入 `logs/.zero_streak.json`，
连续 3 天即输出 `=== SOURCE_ALERT ===`，10:00 的 routine 会在汇报里单列。
连续天数由 `fetch` 单点写入，health_check 只读不写（两处各加会让天数翻倍）。

收到告警后，把该源从 `RSS_SOURCES` 删掉或换掉即可。

## 文件结构

```
daily_report.py        主脚本：--mode fetch（抓取+抓正文）/ send（清洗+推送）
claude_report.sh       供定时任务调用的封装，从 plist 加载环境变量
prompt.md              当日日报的写稿规范（唯一权威源）
prompt_weekly.md       周日《AI 产业周回顾》的写稿规范
health_check.sh        体检薄封装，逻辑在 ../shared/health_check_base.sh
auto_repair.sh         自愈薄封装，逻辑在 ../shared/auto_repair_base.sh
claude_catchup.sh      无头补跑薄封装，逻辑在 ../shared/headless_catchup_base.sh
changelog.md           问题追踪，与 health_check 联动
logs/                  运行时生成，不预置
  report_draft.txt       当日稿子（send 读取后推送）
  archive/YYYY-MM-DD.txt 已推送稿件存档（留 14 天），周回顾的素材
  run.log / run.jsonl    单行摘要 / 结构化指标
  sent_urls.json         跨天去重档案（留 7 天）
  .zero_streak.json      各源连续零产天数
  last_context.txt       最近一次 fetch 的完整输出
```

## 环境变量

写在 `~/Library/LaunchAgents/com.shirley.ai-daily-news-bot.plist`，`claude_report.sh`
每次运行时直接读文件，改完即生效，不用重载 launchd。仓库里只留 `.plist.example` 模板。
该 plist 不承担调度，只作为配置源。

| 变量 | 说明 |
|---|---|
| `FEISHU_WEBHOOK` | 飞书机器人 webhook（AI 三块共用同一个） |
| `FEISHU_ALERT_WEBHOOK` | 运维告警走的监测机器人，不进日报群 |
| `FEISHU_SECRET` | 签名密钥，未开签名校验则留空 |
| `HTTPS_PROXY` / `HTTP_PROXY` | 本地代理，仅抓取阶段用（推送飞书是直连） |

## 用法

```bash
bash claude_report.sh fetch     # 抓取，把素材打到 stdout
bash claude_report.sh send      # 读 logs/report_draft.txt 推送飞书
```

调试：

```bash
tail -5 logs/run.log                          # 最近运行状态
tail -3 logs/run.jsonl | python3 -m json.tool # 结构化指标
bash health_check.sh                          # 手动体检
```

依赖：`pip3 install requests feedparser`

详细操作规范见 [`AGENTS.md`](./AGENTS.md)；换机与排障见 [TROUBLESHOOTING.md](https://github.com/shirleyisdoingrightthings/bot-ops/blob/main/TROUBLESHOOTING.md)。

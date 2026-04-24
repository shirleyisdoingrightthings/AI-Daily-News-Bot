# AI Daily News Bot

每日自动从 10 大全球 AI 媒体源抓取资讯，通过 DeepSeek 进行分析、评分与去重，生成结构化日报并推送到 Telegram。

---

## 核心特性与数据源

- 📰 **多维媒体矩阵**：
  *   **主流科技**：The Verge, Wired, TechCrunch, Engadget
  *   **学术深度**：MIT Technology Review, IEEE Spectrum, Ars Technica
  *   **垂直领域**：VentureBeat (AI), The Decoder
- 🛠 **自动化流程**：
  *   **内容去重**：自动合并多源同质报道，保留最权威来源。
  *   **价值评分**：基于 3/4/5 分制进行价值过滤，确保高密度信息输出。
  *   **自动分类**：涵盖模型、产品、公司、政策、基础设施及机器人等领域。
- 📈 **输出规范**：
  *   **简讯预览**：首部提供 30 秒快速扫读摘要。
  *   **深度叙事**：分析事件背景、竞争逻辑及行业关联。
- 🛡 **稳定性保障**：
  *   **错误重试**：网络或 API 波动时自动触发指数退避重试。
  *   **消息缓存**：发送失败时自动持久化，下次运行优先补发。

---

## Demo 预览
 
 <details>
 <summary>点击展开查看 Bot 推送到 Telegram 的长图预览</summary>
 <br>
 
 ![AI Daily News Bot 运行效果图](./assets/full_demo.png)
 
 </details>
 
 ---

## 系统架构与工作流

```
[数据源]                      [处理]           [输出]
RSS × 10 源 ──▶  build_ai_context()
（The Verge / TechCrunch /      │
 VentureBeat / Wired /          ▼
 MIT Tech Review /        generate_report()
 Engadget / IEEE /         (DeepSeek × 1)  ──▶  Telegram（AI 产业日报，段落分块发送）
 Ars Technica /
 The Decoder）

【自动化调度】
08:00  launchd ──▶ daily_report.py ──▶ run.log [OK/FAIL]
08:30  launchd ──▶ health_check.sh
                        │
                  [OK] ──┴── .ok_streak +1（连续 3 次后清理已解决的 changelog 条目）
                        │
                  [FAIL] ── changelog 新增条目
                        └──▶ auto_repair.sh（后台运行）
                                  ├─ Level 1：等 30s 直接重跑（瞬时网络错误）
                                  └─ Level 2：claude CLI 诊断修复 → 重跑
                                              ├─ 成功 → changelog 标记 [x]
                                              └─ 失败 → macOS 通知，需人工介入
```

---

## 文件结构

```
~/Desktop/bot_ops/shared/bot_utils.py      # 外部共享工具库（与 Crypto Daily Bot 共用）
~/Desktop/bot_ops/auto_repair_base.sh     # 外部共享修复逻辑（与 Crypto Daily Bot 共用）

AI Daily News Bot/
├── daily_report.py                    # 主脚本（抓取 → 分析 → 推送）
├── health_check.sh                     # 健康检查（失败时触发 auto_repair）
├── auto_repair.sh                      # 薄包装：设置参数后委托 bot_ops/auto_repair_base.sh
├── run.log                             # 单行摘要日志（人类可读，脚本运行后生成）
├── run.jsonl                           # 结构化指标日志（程序可读，运行成功后生成）
├── changelog.md                        # 问题追踪，与 health_check 联动
├── pending_messages.json               # Telegram 缓存（仅 Telegram 失败时存在）
├── AGENTS.md                           # 通用 AI 操作手册（适用于任意 AI 工具）
├── CLAUDE.md                           # Claude Code 专属上下文（引用 AGENTS.md）
├── com.shirley.ai-daily-news-bot.plist      # launchd 主脚本配置（08:00 触发）
├── com.shirley.ai-daily-news-bot-health.plist  # launchd 健康检查配置（08:30 触发）
└── README.md                           # 本文件（人类阅读）
```

> `run.jsonl` 和 `pending_messages.json` 是运行时自动生成的，不会预置在文件夹中。  
> `__pycache__/` 是 Python 自动创建的字节码缓存目录，可安全忽略，建议加入 `.gitignore`。

---

## 环境变量

所有变量已写入 `com.shirley.ai-daily-news-bot.plist`，launchd 会自动注入，无需手动配置 shell profile。

| 变量 | 说明 | 来源 |
|------|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key | plist（需手动填入） |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | plist（需手动填入） |
| `TELEGRAM_CHAT_ID` | 目标 Chat ID | plist（已配置）|
| `HTTPS_PROXY` | 代理地址 | plist（已配置，127.0.0.1:YOUR_PORT）|

---

## 快速开始

**手动运行（测试）**
```bash
cd ~/Desktop/AI\ Daily\ News\ Bot
/opt/homebrew/bin/python3.11 daily_report.py
```

**激活自动调度**

1. 将样板文件拷贝为正式配置文件：
```bash
cp com.shirley.ai-daily-news-bot.plist.example com.shirley.ai-daily-news-bot.plist
```
2. **重要**：编辑 `com.shirley.ai-daily-news-bot.plist`，填入你的 API Key、路径和代理端口。
3. 加载任务：
```bash
cp *.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.shirley.ai-daily-news-bot.plist
```

**验证调度状态**
```bash
launchctl list | grep shirley
tail -5 run.log
```

---

## 依赖安装

```bash
pip3.11 install requests feedparser openai
```

---

## 调试

```bash
tail -5 run.log                          # 最近运行状态
tail -3 run.jsonl | python3 -m json.tool # 结构化指标
cat changelog.md                         # 当前问题清单
bash health_check.sh                     # 手动触发健康检查
```

详细操作规范见 [`AGENTS.md`](./AGENTS.md)。

#!/bin/bash
# claude_catchup.sh — AI Daily News Bot
# 当天未成功出稿时的无头补跑（自动版 Run Now）
# 由 health_check.sh（MISSING 分支）或 auto_repair 最终兜底触发
# 补跑逻辑统一维护在同级 shared/headless_catchup_base.sh（路径由 $DIR 相对推导）

DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="AI Daily News Bot"
PLIST="$HOME/Library/LaunchAgents/com.shirley.ai-daily-news-bot.plist"
WRITE_SPEC="严格按 prompt.md 写稿，写入 logs/report_draft.txt（若 fetch 输出的是 === WEEKLY_OK === 而非 === FETCH_OK ===，说明今天是周日、要出的是《AI 产业周回顾》，改按 prompt_weekly.md 写稿，素材取 ARCHIVE 与 CONTEXT 两段，产物文件不变）"

source "$DIR/../shared/headless_catchup_base.sh"

#!/bin/bash
# claude_catchup.sh — AI Daily News Bot
# 当天未成功出稿时的无头补跑（自动版 Run Now）
# 由 health_check.sh（MISSING 分支）或 auto_repair 最终兜底触发
# 补跑逻辑统一维护在 ~/bots/shared/headless_catchup_base.sh

DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="AI Daily News Bot"
PLIST="$HOME/Library/LaunchAgents/com.shirley.ai-daily-news-bot.plist"
WRITE_SPEC="严格按 prompt.md 写稿，写入 logs/report_draft.txt"

source "$HOME/bots/shared/headless_catchup_base.sh"

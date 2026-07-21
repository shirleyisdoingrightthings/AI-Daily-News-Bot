#!/bin/bash
# auto_repair.sh — AI Daily News Bot
# 检测到 [FAIL] 时由 health_check.sh 触发
# 修复逻辑统一维护在 ~/bots/shared/auto_repair_base.sh
# （2026-07-20 起从 ~/Desktop/bot_ops/ 迁入，旧文件不再引用）

DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_NAME="AI Daily News Bot"
SCRIPT="$DIR/daily_report.py"
ERROR="${1:-unknown error}"
DRAFTS="logs/report_draft.txt"

source "$HOME/bots/shared/auto_repair_base.sh"

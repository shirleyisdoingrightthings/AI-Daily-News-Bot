# AI Daily News Bot

Automated daily AI industry news digest and Sunday weekly review, drafted by an AI agent and delivered to Feishu.

## Workflow

```text
RSS Feeds (6 AI & Robotics Sources)
  │
  ▼
1. Fetch & Filter (daily_report.py --mode fetch)
  ├─ Deduplicate within run & across 7-day history (sent_urls.json)
  ├─ Filter by 24h freshness window & AI relevance gates
  └─ Concurrently extract full article bodies (fallback to RSS summary)
  │
  ▼
2. Draft Report (Scheduled Agent at 15:00 CST)
  ├─ Mon–Sat (FETCH_OK): Draft daily briefing with 5-item executive summary & scored details (prompt.md)
  └─ Sunday (WEEKLY_OK): Merge 6-day archives, re-score on a weekly scale, and group by theme (prompt_weekly.md)
  │
  ▼
3. Sanitize & Deliver (daily_report.py --mode send)
  ├─ Sanitize HTML (<b> and <a href> whitelist) & convert to Feishu card Markdown
  ├─ Auto-paginate messages exceeding 20KB with (n/N) headers
  └─ Push to Feishu webhook & archive sent URLs and daily text
  │
  ▼
4. Health Check & Self-Healing (16:00 CST via launchd)
  └─ Verify daily run status; trigger headless catchup or auto-repair if missing or failed
```

## Operation

1. **Fetch Materials**: Run `bash claude_report.sh fetch` to pull and filter the latest news into `logs/last_context.txt`.
2. **Generate Draft**: The agent reads `logs/last_context.txt` and follows `prompt.md` (or `prompt_weekly.md` on Sundays) to write the formatted report to `logs/report_draft.txt`.
3. **Send to Feishu**: Run `bash claude_report.sh send` to validate, paginate, and push the report to the Feishu group.

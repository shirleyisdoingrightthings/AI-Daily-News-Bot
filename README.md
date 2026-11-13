# AI Daily News Bot — README

Automated daily AI industry news digest drafted by an AI agent and delivered to Feishu & Notion.

## Workflow

```text
RSS Feeds (AI & Robotics Sources)
  │
  ▼
1. Fetch & Filter (daily_report.py --mode fetch)
  ├─ Deduplicate within run & across 7-day history (sent_urls.json)
  ├─ Filter by 24h freshness window & AI relevance gates
  └─ Concurrently extract full article bodies (fallback to RSS summary)
  │
  ▼
2. Draft Report (Scheduled Antigravity Sidecar at 15:00 CST)
  └─ Daily (FETCH_OK, incl. Sundays): Draft briefing with 5-item summary & up to 12 scored details (prompt.md)
  │
  ▼
3. Sanitize & Deliver (daily_report.py --mode send)
  ├─ Sanitize HTML (<b> and <a href> whitelist) & convert to Feishu card Markdown
  ├─ Auto-paginate messages exceeding 20KB with (n/N) headers
  └─ Push to Feishu webhook, archive to Notion, and update sent_urls.json
  │
  ▼
4. Lightweight Health Check (16:00 CST via launchd ai-chain-health, Option B)
  └─ Verify run status & retry Level 1 pending send if drafts_fresh (no headless CLI catchup)
```

## Operation

1. **Fetch Materials**: Run `bash claude_report.sh fetch` to write context into `logs/last_context.txt`.
2. **Generate Draft**: Read `logs/last_context.txt` and follow `prompt.md` using `write_file` to save `logs/report_draft.txt`.
3. **Send to Feishu & Notion**: Run `bash claude_report.sh send`.

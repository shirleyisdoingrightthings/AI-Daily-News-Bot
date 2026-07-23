#!/usr/bin/python3
"""
AI 产业日报
从多个 RSS 源抓取新闻（best-effort 抓正文全文，失败回退 RSS 摘要），
由 Claude 写稿后发送到 Telegram。本脚本只负责抓取与发送，不含写稿用的第三方大模型 API。

两种运行模式（--mode，均零 API 成本）：
- fetch：抓取 + 抓正文 → 把新闻 context 打到 stdout（供 Claude routine 读取写稿）
- send ：读取 Claude 写好的稿子文件 → 清洗 HTML → 发送 Telegram + 写日志

写稿规范存放于同目录 prompt.md，由 Claude routine 读取。
"""

import os
import sys
import time
import json
import argparse
import traceback
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 共享工具库
sys.path.insert(0, str(Path.home() / "bots" / "shared"))
from bot_utils import (sanitize_html, with_retry, fetch_rss, parse_entry_date,
                       already_ran_today, fetch_article_text,
                       url_key, load_sent_urls, record_sent_urls, extract_hrefs,
                       is_ai_relevant, paginate_telegram, update_zero_streak)

LOG_FILE    = Path(__file__).parent / "logs" / "run.log"
JSONL_FILE  = Path(__file__).parent / "logs" / "run.jsonl"
LOG_FILE.parent.mkdir(exist_ok=True)
CACHE_FILE  = Path(__file__).parent / "pending_messages.json"
# Claude routine 把写好的稿子存到这里，再用 --mode send 发送
DRAFT_FILE  = Path(__file__).parent / "logs" / "report_draft.txt"
# fetch 模式写出、send 模式读回的边车：承载 OK 日志摘要与 health_check 所需 metrics
FETCH_META  = Path(__file__).parent / "logs" / "fetch_meta.json"
# 跨天去重档案：send 成功后记录稿件里实际用到的链接，fetch 时据此排除
SENT_URLS   = Path(__file__).parent / "logs" / "sent_urls.json"
# RSS 源连续零产计数（fetch 阶段唯一写入，health_check 只读）
ZERO_STREAK = Path(__file__).parent / "logs" / ".zero_streak.json"
# 连续零产多少天就判定该源可以移除
ZERO_STREAK_THRESHOLD = 3

# ===== P0: 显式代理配置 =====
_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
SESSION = requests.Session()
SESSION.proxies = {"http": _PROXY, "https": _PROXY}
# feedparser 内部使用 urllib，通过环境变量注入代理
os.environ.setdefault("HTTP_PROXY",  _PROXY or "")
os.environ.setdefault("HTTPS_PROXY", _PROXY or "")


# ===== P1: 结构化日志 =====
def write_log(status: str, message: str, metrics: dict = None) -> None:
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"{ts}  [{status}]  {message}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    print(line, end="")
    if metrics:
        record = {"ts": ts, "status": status, "msg": message, **metrics}
        with open(JSONL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ===== 配置（优先读取环境变量）=====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "your_telegram_bot_token")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "your_telegram_chat_id")

# (feed_url, limit, is_general)
# is_general=True 表示这是泛科技源而非 AI 垂直源，条目要过 is_ai_relevant 闸门。
# 垂直源不过闸，避免误伤标题里不含关键词的正当 AI 选题。
RSS_SOURCES = [
    ("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",    5, False),
    ("https://techcrunch.com/category/artificial-intelligence/feed/",        5, False),
    ("https://venturebeat.com/category/ai/feed/",                            5, False),
    ("https://www.wired.com/feed/tag/ai/latest/rss",                         3, False),
    ("https://www.technologyreview.com/topic/artificial-intelligence/feed/", 3, False),
    ("https://spectrum.ieee.org/feeds/topic/robotics.rss",                   3, False),
    ("https://the-decoder.com/feed/",                                        3, False),
    # 泛科技源：抓取额度放宽，靠相关性闸门收敛（原额度会被非 AI 条目吃掉）
    ("https://feeds.arstechnica.com/arstechnica/technology-lab",             3, True),
    ("https://www.engadget.com/rss.xml",                                     8, True),
]

# ===== P2: 消息缓存（降级策略）=====
def save_pending(messages: list) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": datetime.now().isoformat(), "messages": messages}, f, ensure_ascii=False)


def flush_pending() -> bool:
    """启动时检查并重发上次未发送的缓存消息"""
    if not CACHE_FILE.exists():
        return False
    try:
        data    = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        pending = data.get("messages", [])
        if not pending:
            CACHE_FILE.unlink(missing_ok=True)
            return False
        print(f"[CACHE] 发现 {len(pending)} 条待发消息（来自 {data.get('ts','?')}），优先重发...")
        for msg in pending:
            send_telegram(msg)
        CACHE_FILE.unlink(missing_ok=True)
        print("[CACHE] 缓存消息重发成功")
        return True
    except Exception as e:
        print(f"[WARN] 缓存重发失败: {e}", file=sys.stderr)
        return False


# ===== 整理新闻数据 =====
def build_ai_context(all_entries: list) -> tuple:
    """整理素材，返回 (context, kept_per_source, drop_stats)。

    过滤顺序：标题/URL 缺失 → 单次运行内 URL 去重 → 跨天已播去重 →
    24h 时间窗 → 泛科技源的 AI 相关性闸门。"""
    now        = datetime.now(timezone.utc)
    time_limit = now - timedelta(days=1)
    seen_urls: set = set()
    sent_before    = load_sent_urls(SENT_URLS)
    lines: list = []

    kept_per_source: dict = {}
    drops = {"dup": 0, "already_sent": 0, "stale": 0, "off_topic": 0}

    picked: list = []   # (title, url, url_lower, snippet)
    for entry in all_entries:
        title = getattr(entry, "title", None)
        if not title:
            continue
        original_url = getattr(entry, "link", "") or getattr(entry, "id", "")
        url_lower    = original_url.lower()
        if not url_lower or url_lower in seen_urls:
            drops["dup"] += 1
            continue
        seen_urls.add(url_lower)
        # 跨天去重：前几天已经播出去的条目不再重复入选
        if url_key(original_url) in sent_before:
            drops["already_sent"] += 1
            continue
        pub_date = parse_entry_date(entry)
        if not pub_date or pub_date < time_limit:
            drops["stale"] += 1
            continue
        snippet = getattr(entry, "summary", "") or ""
        # 泛科技源过 AI 相关性闸门，垂直源直接放行
        if getattr(entry, "__general", False) and not is_ai_relevant(title, snippet):
            drops["off_topic"] += 1
            continue
        src = getattr(entry, "__src", "?")
        kept_per_source[src] = kept_per_source.get(src, 0) + 1
        picked.append((title, original_url, url_lower, snippet))

    # best-effort 并发抓正文全文；抓到用正文，失败/被墙/过短则回退 RSS 摘要。
    # 全程零 API、纯 HTTP，抓不到不影响出稿。
    def _material(item):
        title, url, url_lower, snippet = item
        body = fetch_article_text(url)          # "" 表示失败/过短
        text = body if body else snippet[:500]
        src  = "正文" if body else "摘要"
        return f"[原始英文标题] {title}\n[链接] {url}\n[来源域名] {url_lower}\n[正文/摘要（{src}）] {text}\n----"

    if picked:
        with ThreadPoolExecutor(max_workers=8) as ex:
            lines = list(ex.map(_material, picked))

    return "\n".join(lines), kept_per_source, drops


# ===== P0: 发送 Telegram（单块重试 + 整体分块）=====
@with_retry(max_retries=3, base_delay=5, exceptions=(requests.RequestException,))
def _send_one(chunk: str) -> None:
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = SESSION.post(
        api_url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not resp.ok:
        raise requests.RequestException(f"Telegram 返回错误: {resp.text}")


def send_telegram(text: str) -> None:
    # 切分 + 页码统一在 bot_utils.paginate_telegram 里做（两个 bot 共用同一实现）。
    # 调用方已完成 sanitize_html，故页码的 <b> 标签不会被转义。
    chunks = paginate_telegram(text)
    for chunk in chunks:
        _send_one(chunk)


# ===== 抓取阶段（fetch 模式用）=====
def _proxy_ok() -> bool:
    """代理预检：无代理直接放行，有代理则快速验证可达。"""
    if not _PROXY:
        return True
    try:
        SESSION.get("https://www.gstatic.com/generate_204", timeout=5)
        return True
    except Exception:
        return False


def fetch_news() -> tuple:
    """抓 RSS + 整理去重，返回 (ai_context, rss_fetched, entry_count, zero_sources, source_stats)。
    进度打到 stderr，让 fetch 模式的 stdout 只保留干净的 context。"""
    print("📡 抓取 RSS 源...", file=sys.stderr)
    all_entries = []
    fetched_counts: dict = {}
    for feed_url, limit, is_general in RSS_SOURCES:
        entries = fetch_rss(feed_url, limit)
        domain  = feed_url.split("/")[2]
        # 给条目打上来源标记，供后续统计"过滤后每个源还剩几条"与相关性闸门判定
        for e in entries:
            e["__src"]     = domain
            e["__general"] = is_general
        all_entries.extend(entries)
        fetched_counts[domain] = fetched_counts.get(domain, 0) + len(entries)
        print(f"  ✓ {len(entries)} 条  {feed_url}", file=sys.stderr)

    print(f"\n📰 共抓取 {len(all_entries)} 条，整理过滤中...", file=sys.stderr)
    ai_context, kept_per_source, drops = build_ai_context(all_entries)
    # 条目数按标记计数，不再数 "----"：正文里出现连字符串会把计数撑爆
    entry_count = ai_context.count("[原始英文标题]") if ai_context else 0

    # 零产源 = 过滤后一条都没剩的源（而非"RSS 拉到 0 条"）。
    # 一个源可能天天拉得到、却条条过期，旧口径永远发现不了。
    source_stats = {d: {"fetched": n, "kept": kept_per_source.get(d, 0)}
                    for d, n in fetched_counts.items()}
    zero_sources = [d for d, s in source_stats.items() if s["kept"] == 0]

    # 连续零产追踪：本处是 .zero_streak.json 的唯一写入方，health_check 只读不写
    stale_sources = update_zero_streak(ZERO_STREAK, zero_sources, list(source_stats),
                                       threshold=ZERO_STREAK_THRESHOLD)

    print(f"   过滤明细：重复 {drops['dup']} · 已播过 {drops['already_sent']} · "
          f"超 24h {drops['stale']} · 非 AI {drops['off_topic']} → 保留 {entry_count}",
          file=sys.stderr)
    streak_now = _load_streak()
    for d, s in sorted(source_stats.items(), key=lambda kv: -kv[1]["kept"]):
        n = streak_now.get(d, 0)
        flag = f"  ⚠️ 零产（连续 {n} 天）" if s["kept"] == 0 else ""
        print(f"   {d:26s} 抓{s['fetched']:>2} → 留{s['kept']:>2}{flag}", file=sys.stderr)

    return ai_context, len(all_entries), entry_count, zero_sources, source_stats, stale_sources


def _load_streak() -> dict:
    """只读地取一份当前连续零产计数，供 stderr 明细展示。"""
    try:
        return json.loads(ZERO_STREAK.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ===== 模式 1：fetch — 抓取并输出 context（零 API 成本，供 Claude 写稿）=====
def run_fetch() -> int:
    # 防重复：今天已成功则让 routine 停手（FORCE_RUN=1 可绕过）
    if already_ran_today(LOG_FILE):
        print("=== SKIP_ALREADY_RAN ===")
        return 0

    if not _proxy_ok():
        print(f"=== SKIP_PROXY === {_PROXY}")
        return 0

    ai_context, rss_fetched, entry_count, zero_sources, source_stats, stale_sources = fetch_news()

    if not ai_context:
        print("=== NO_NEWS ===")
        write_log("WARN", "过去24小时无有效新闻，未发送")
        return 0

    # 写边车：OK 日志摘要 + metrics，供 send 模式回填（保持 health_check 监控存活）
    FETCH_META.write_text(
        json.dumps(
            {"log_summary": f"抓取{rss_fetched}条 → 保留{entry_count}条",
             "metrics": {"rss_fetched": rss_fetched, "rss_kept": entry_count,
                         "rss_zero_sources": zero_sources,
                         "rss_source_stats": source_stats,
                         "rss_stale_sources": stale_sources}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    today = datetime.now().strftime("%Y-%m-%d")
    # stdout 只输出结构化标记 + context，供 Claude routine 稳定解析
    print("=== FETCH_OK ===")
    print(f"今天日期：{today}")
    print(f"保留 {entry_count} 条有效新闻（共抓取 {rss_fetched} 条）")
    if zero_sources:
        print(f"零产源：{', '.join(zero_sources)}")
    # 连续零产达阈值 → 结构化告警块，供 routine 在日报汇报里转述给用户
    if stale_sources:
        print("=== SOURCE_ALERT ===")
        for d, n in stale_sources.items():
            print(f"{d} 已连续 {n} 天零产，建议从 RSS_SOURCES 移除或更换")
        print("=== SOURCE_ALERT_END ===")
    print("=== CONTEXT_BEGIN ===")
    print(ai_context)
    print("=== CONTEXT_END ===")
    return 0


# ===== 模式 2：send — 读取 Claude 写好的稿子并发送（零 API 成本）=====
def run_send(draft_path: Path) -> int:
    t0 = time.time()

    if already_ran_today(LOG_FILE):
        print("今天已成功运行过，跳过发送。如需强制请设置 FORCE_RUN=1。", file=sys.stderr)
        return 0

    if not draft_path.exists():
        write_log("FAIL", f"稿子文件不存在：{draft_path}")
        return 1
    report = draft_path.read_text(encoding="utf-8").strip()
    if not report:
        write_log("FAIL", f"稿子文件为空：{draft_path}")
        return 1

    # HTML 白名单清洗，防止非法标签导致 Telegram 发送失败
    report = sanitize_html(report)

    # 代理不可用时不丢内容：缓存下来，等代理恢复后补发
    if not _proxy_ok():
        save_pending([report])
        write_log("WARN", f"代理不可用（{_PROXY}），稿子已缓存未发送")
        return 0

    # P2: 先持久化缓存，防止 Telegram 失败时内容丢失
    save_pending([report])
    print("📨 发送到 Telegram...", file=sys.stderr)
    send_telegram(report)
    print("  ✓ 发送成功", file=sys.stderr)
    CACHE_FILE.unlink(missing_ok=True)

    # 归档本次真正播出去的链接，供后续 fetch 跨天去重。
    # 记在发送成功之后：发失败的那批不该被标成"已播"。
    hrefs = extract_hrefs(report)
    if hrefs:
        total = record_sent_urls(SENT_URLS, hrefs)
        print(f"  ✓ 已归档 {len(hrefs)} 条链接用于跨天去重（档案共 {total} 条）",
              file=sys.stderr)

    # OK 日志：从 fetch 边车取摘要与 metrics，保持 health_check 监控存活
    try:
        meta = json.loads(FETCH_META.read_text(encoding="utf-8"))
    except Exception:
        meta = {"log_summary": "", "metrics": {}}
    duration = round(time.time() - t0, 1)
    write_log(
        "OK",
        f"Claude写稿 → {meta.get('log_summary', '')} → Telegram发送成功（{len(report)}字）",
        metrics={**meta.get("metrics", {}), "ai_calls": 0, "duration_s": duration,
                 "report_chars": len(report), "source": "claude"},
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI 产业日报")
    parser.add_argument(
        "--mode", choices=["fetch", "send"], required=True,
        help="fetch=抓取并输出 context（供 Claude 写稿，零 API）/ send=发送 Claude 写好的稿子",
    )
    parser.add_argument(
        "--file", type=Path, default=DRAFT_FILE,
        help="send 模式读取的稿子文件（默认 logs/report_draft.txt）",
    )
    parsed = parser.parse_args()

    try:
        if parsed.mode == "fetch":
            sys.exit(run_fetch())
        else:
            sys.exit(run_send(parsed.file))
    except Exception:
        err = traceback.format_exc().strip().splitlines()[-1]
        write_log("FAIL", err)
        raise

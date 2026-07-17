#!/opt/homebrew/bin/python3.11
"""
AI 产业日报
从多个 RSS 源抓取新闻，生成日报，发送到 Telegram。

三种运行模式（--mode，默认 full 以保持向后兼容）：
- full ：抓取 → DeepSeek 写稿 → 发送（无头兜底，launchd 使用，需 DEEPSEEK_API_KEY）
- fetch：抓取 → 把新闻 context 打到 stdout（零 API 成本，供 Claude routine 读取写稿）
- send ：读取稿子文件 → 发送 Telegram + 写日志（零 API 成本，供 Claude routine 发稿）

写稿规范统一存放于同目录 prompt.md，full 模式与 Claude routine 共用同一份。
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
from openai import OpenAI

# 共享工具库
sys.path.insert(0, str(Path.home() / "Desktop" / "bot_ops" / "shared"))
from bot_utils import (sanitize_html, with_retry, fetch_rss, parse_entry_date,
                       already_ran_today, fetch_article_text)

LOG_FILE    = Path(__file__).parent / "logs" / "run.log"
JSONL_FILE  = Path(__file__).parent / "logs" / "run.jsonl"
LOG_FILE.parent.mkdir(exist_ok=True)
CACHE_FILE  = Path(__file__).parent / "pending_messages.json"
PROMPT_FILE = Path(__file__).parent / "prompt.md"
# Claude routine 把写好的稿子存到这里，再用 --mode send 发送
DRAFT_FILE  = Path(__file__).parent / "logs" / "report_draft.txt"
# fetch 模式写出、send 模式读回的边车：承载 OK 日志摘要与 health_check 所需 metrics
FETCH_META  = Path(__file__).parent / "logs" / "fetch_meta.json"

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
DEEPSEEK_API_KEY   = os.getenv("DEEPSEEK_API_KEY",   "your_deepseek_api_key")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "your_telegram_bot_token")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "your_telegram_chat_id")

RSS_SOURCES = [
    ("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 5),
    ("https://techcrunch.com/category/artificial-intelligence/feed/",     5),
    ("https://venturebeat.com/category/ai/feed/",                          5),
    ("https://www.wired.com/feed/tag/ai/latest/rss",                       3),
    ("https://www.technologyreview.com/feed/",                             3),
    ("https://www.engadget.com/rss.xml",                                   4),
    ("https://spectrum.ieee.org/feeds/topic/robotics.rss",                 3),
    ("https://www.theverge.com/rss/reviews/index.xml",                     3),
    ("https://feeds.arstechnica.com/arstechnica/technology-lab",           3),
    ("https://the-decoder.com/feed/",                                      3),
]

def load_system_prompt() -> str:
    """写稿规范单一权威源：full 模式与 Claude routine 共用 prompt.md。"""
    return PROMPT_FILE.read_text(encoding="utf-8")

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
def build_ai_context(all_entries: list) -> str:
    now        = datetime.now(timezone.utc)
    time_limit = now - timedelta(days=1)
    seen_urls: set = set()
    lines: list = []

    picked: list = []   # (title, url, url_lower, snippet)
    for entry in all_entries:
        title = getattr(entry, "title", None)
        if not title:
            continue
        original_url = getattr(entry, "link", "") or getattr(entry, "id", "")
        url_lower    = original_url.lower()
        if not url_lower or url_lower in seen_urls:
            continue
        seen_urls.add(url_lower)
        pub_date = parse_entry_date(entry)
        if not pub_date or pub_date < time_limit:
            continue
        snippet = getattr(entry, "summary", "") or ""
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

    return "\n".join(lines)


# ===== P0: DeepSeek 生成日报（超时 + 重试）=====
@with_retry(max_retries=2, base_delay=10, exceptions=(Exception,))
def generate_report(ai_context: str) -> str:
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
        timeout=60.0,
        max_retries=0,
    )
    today   = datetime.now().strftime("%Y-%m-%d")
    user_msg = f"今天日期：{today}\n\n{ai_context}"
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": load_system_prompt()},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.3,
    )
    report = response.choices[0].message.content
    return sanitize_html(report)


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
    MAX_LEN = 4096
    if len(text) <= MAX_LEN:
        _send_one(text)
        return

    # Split at paragraph boundaries so news items are never cut mid-content.
    # Each chunk accumulates paragraphs until the next one would exceed the limit.
    paragraphs = text.split('\n\n')
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        needed = len(para) + (2 if current else 0)  # 2 for the '\n\n' joining separator
        if current_len + needed > MAX_LEN and current:
            chunks.append('\n\n'.join(current))
            current = [para]
            current_len = len(para)
        else:
            current.append(para)
            current_len += needed

    if current:
        chunks.append('\n\n'.join(current))

    for chunk in chunks:
        _send_one(chunk)


# ===== 抓取阶段（fetch / full 共用）=====
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
    """抓 RSS + 整理去重，返回 (ai_context, rss_fetched, entry_count, zero_sources)。
    进度打到 stderr，让 fetch 模式的 stdout 只保留干净的 context。"""
    print("📡 抓取 RSS 源...", file=sys.stderr)
    all_entries = []
    source_counts: dict = {}
    for feed_url, limit in RSS_SOURCES:
        entries = fetch_rss(feed_url, limit)
        all_entries.extend(entries)
        source_counts[feed_url.split("/")[2]] = len(entries)
        print(f"  ✓ {len(entries)} 条  {feed_url}", file=sys.stderr)
    zero_sources = [d for d, c in source_counts.items() if c == 0]

    print(f"\n📰 共抓取 {len(all_entries)} 条，整理过滤中...", file=sys.stderr)
    ai_context  = build_ai_context(all_entries)
    entry_count = ai_context.count("----") if ai_context else 0
    return ai_context, len(all_entries), entry_count, zero_sources


# ===== 模式 1：fetch — 抓取并输出 context（零 API 成本，供 Claude 写稿）=====
def run_fetch() -> int:
    # 防重复：今天已成功则让 routine 停手（FORCE_RUN=1 可绕过）
    if already_ran_today(LOG_FILE):
        print("=== SKIP_ALREADY_RAN ===")
        return 0

    if not _proxy_ok():
        print(f"=== SKIP_PROXY === {_PROXY}")
        return 0

    ai_context, rss_fetched, entry_count, zero_sources = fetch_news()

    if not ai_context:
        print("=== NO_NEWS ===")
        write_log("WARN", "过去24小时无有效新闻，未发送")
        return 0

    # 写边车：OK 日志摘要 + metrics，供 send 模式回填（保持 health_check 监控存活）
    FETCH_META.write_text(
        json.dumps(
            {"log_summary": f"抓取{rss_fetched}条 → 保留{entry_count}条",
             "metrics": {"rss_fetched": rss_fetched, "rss_kept": entry_count,
                         "rss_zero_sources": zero_sources}},
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
        print(f"零结果源：{', '.join(zero_sources)}")
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

    # 与 DeepSeek 路径同一套 HTML 白名单清洗，防止非法标签导致发送失败
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


# ===== 模式 3：full — 抓取 → DeepSeek 写稿 → 发送（无头兜底，向后兼容）=====
def run_full() -> None:
    t0 = time.time()

    # 防重复推送：今天已有 [OK] 记录则跳过（FORCE_RUN=1 可绕过）
    if already_ran_today(LOG_FILE):
        print("今天已成功运行过，跳过。如需强制执行请设置 FORCE_RUN=1。")
        return

    # P2: 优先重发上次未发送的缓存消息
    if flush_pending():
        duration = round(time.time() - t0, 1)
        write_log("OK", "缓存重发完成（上次发送中断）", metrics={"duration_s": duration, "ai_calls": 0})
        return

    # 代理预检：失败立即退出
    if not _proxy_ok():
        write_log("WARN", f"代理不可用（{_PROXY}），跳过本次运行")
        return

    ai_context, rss_fetched, entry_count, zero_sources = fetch_news()

    if not ai_context:
        print("⚠️  过去 24 小时内无有效新闻，退出。")
        write_log("WARN", "过去24小时无有效新闻，未发送")
        return

    print(f"  ✓ 保留 {entry_count} 条有效新闻")

    print("\n🤖 调用 DeepSeek 生成日报...")
    report = generate_report(ai_context)
    print("  ✓ 日报生成完毕")

    # P2: 先持久化缓存，防止 Telegram 失败时内容丢失
    save_pending([report])

    print("\n📨 发送到 Telegram...")
    send_telegram(report)
    print("  ✓ 发送成功\n")

    # 发送成功后清除缓存
    CACHE_FILE.unlink(missing_ok=True)

    duration = round(time.time() - t0, 1)
    write_log(
        "OK",
        f"抓取{rss_fetched}条 → 保留{entry_count}条 → Telegram发送成功",
        metrics={
            "rss_fetched": rss_fetched, "rss_kept": entry_count,
            "rss_zero_sources": zero_sources,
            "ai_calls": 1, "duration_s": duration,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI 产业日报")
    parser.add_argument(
        "--mode", choices=["full", "fetch", "send"], default="full",
        help="full=DeepSeek全流程(默认,向后兼容) / fetch=只抓取输出context / send=只发送稿子",
    )
    parser.add_argument(
        "--file", type=Path, default=DRAFT_FILE,
        help="send 模式读取的稿子文件（默认 logs/report_draft.txt）",
    )
    parsed = parser.parse_args()

    try:
        if parsed.mode == "fetch":
            sys.exit(run_fetch())
        elif parsed.mode == "send":
            sys.exit(run_send(parsed.file))
        else:
            run_full()
    except Exception:
        err = traceback.format_exc().strip().splitlines()[-1]
        write_log("FAIL", err)
        raise

#!/usr/bin/python3
"""
AI 产业日报
从多个 RSS 源抓取新闻（best-effort 抓正文全文，失败回退 RSS 摘要），
由 Claude 写稿后推送到飞书。本脚本只负责抓取与推送，不含写稿用的第三方大模型 API。

两种运行模式（--mode，均零 API 成本）：
- fetch：抓取 + 抓正文 → 把新闻 context 打到 stdout（供 Claude routine 读取写稿）
- send ：读取 Claude 写好的稿子文件 → 清洗 HTML → 推送飞书 + 写日志

北京时间周日 fetch 不出当日日报，改出《AI 产业周回顾》：素材是本周每天已推送稿件的
存档（logs/archive/）+ 最近 24 小时新增，stdout 标记为 === WEEKLY_OK ===。
两种稿子共用同一个 send 流程与 run.log 格式，health_check / auto_repair 无需改动。

写稿规范存放于同目录 prompt.md（日报）与 prompt_weekly.md（周回顾），由 Claude routine 读取。
"""

import os
import sys
import time
import json
import argparse
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 共享工具库
# 从脚本自身位置推导共享层（bot 目录的同级 shared/），
# 这样整个 bots 文件夹搬到任何位置都不用改路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from bot_utils import (sanitize_html, fetch_rss, parse_entry_date,
                       already_ran_today, fetch_article_text,
                       url_key, load_sent_urls, record_sent_urls, extract_hrefs,
                       is_ai_relevant, send_feishu, update_zero_streak,
                       resolve_proxy)

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
# 当日稿件存档目录：send 成功后按日期归档，供周日的「本周回顾」取材
ARCHIVE_DIR = Path(__file__).parent / "logs" / "archive"
# 存档保留天数（周回顾只回看 6 天，多留几天方便人工排查）
ARCHIVE_KEEP_DAYS = 14
# 周回顾回看天数：6 = 周一到周六，不含今天（周日），也就不会把上周日的回顾稿卷进来
WEEKLY_LOOKBACK_DAYS = 6
# 存档少于这个份数就不出回顾，退回当日日报（刚上线的头几天、长时间没开机时）
WEEKLY_MIN_ARCHIVES = 3
# 默认时效窗口（小时）。个别发文节奏慢的源可在 RSS_SOURCES 里单独放宽，
# 上限受 sent_urls 的 7 天保留期约束——超过 7 天的窗口会让旧条目重新入选。
DEFAULT_WINDOW_H = 24

# ===== P0: 显式代理配置 =====
_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
# 抓取一律走 bot_utils 里的 requests（trust_env 默认开），feedparser 内部用 urllib，
# 两者都只认环境变量，所以代理在这里注入即可。
# 本脚本不再持有自己的 Session：改用飞书后推送不经代理，由 bot_utils 的专用直连
# Session 负责（见 bot_utils 第 9 节）。
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
# 飞书自定义机器人：webhook 地址在群「设置 → 群机器人 → 添加机器人 → 自定义机器人」
# 里取得。若在那里勾了「签名校验」，把密钥一并放进 FEISHU_SECRET；没勾就留空。
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")
FEISHU_SECRET  = os.getenv("FEISHU_SECRET",  "")

# (feed_url, limit, is_general[, window_h])
# is_general=True 表示这是泛科技源而非 AI 垂直源，条目要过 is_ai_relevant 闸门。
# 垂直源不过闸，避免误伤标题里不含关键词的正当 AI 选题。
# window_h 可选，缺省用 DEFAULT_WINDOW_H；只给发文慢的源单独放宽。
RSS_SOURCES = [
    ("https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",    5, False),
    ("https://techcrunch.com/category/artificial-intelligence/feed/",        5, False),
    ("https://www.wired.com/feed/tag/ai/latest/rss",                         3, False),
    # 2026-08-07：MIT Tech Review 放宽到 72h 后仍连续 3 天零产（抓得到，但条条被
    # 去重/时效/相关性挡下），换成 SemiAnalysis。
    # 注意用 newsletter 子域的 feed：主站 semianalysis.com/feed/ 最新一篇停在
    # 2025-09-16，已随内容迁移变成死源，抓了也永远零产。
    # SemiAnalysis 是每周 2-3 篇的算力/半导体深度稿节奏，24h 窗口必然天天零产，
    # 故给 120h（仍在 sent_urls 7 天保留期内，同一篇不会重复入选）。
    ("https://newsletter.semianalysis.com/feed",                             3, False, 120),
    ("https://the-decoder.com/feed/",                                        3, False),
    ("https://arstechnica.com/ai/feed/",                                     4, False),
    # 2026-08-06：移除全部厂商官方博客（openai.com/blog、blog.google/technology/ai，
    # 更早还有 deepmind.google）。这类源发文稀疏、软文占比高，长期零产；真正重要的
    # 官宣一定会被上面的垂直媒体源当天覆盖，留着只是白占抓取额度。不要再加回来。
    # 泛科技源：抓取额度放宽，靠相关性闸门收敛（原额度会被非 AI 条目吃掉）
    ("https://www.engadget.com/rss.xml",                                     8, True),
]

# ===== P2: 消息缓存（降级策略）=====
# 飞书推送失败时把稿件存到 pending_messages.json，避免内容丢失。
# 注意：**不做自动重发**——重发要判断"这稿子还是今天的吗"，跨天重发旧稿比丢一次
# 更糟。当天补救由 health_check → claude_catchup 重走完整流程负责，缓存只作为
# 人工恢复的兜底副本。（2026-08 删除了定义后从未被调用的 flush_pending。）
def save_pending(messages: list) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": datetime.now().isoformat(), "messages": messages}, f, ensure_ascii=False)


# ===== 稿件存档（周日「本周回顾」的素材来源）=====
def archive_draft(report: str) -> None:
    """把刚推送成功的稿子按日期归档，并清掉过期存档。

    周回顾必须靠存档，不能靠周日重新抓：RSS feed 只保留最近几十条，周一的新闻
    到周日早已滚出 feed；且这些链接都在 sent_urls 的跨天去重档案里，重抓也会被挡。
    存档里的稿子已经筛过、写好、带链接，是唯一稳妥的素材来源。
    归档只是锦上添花，失败绝不能影响已经成功的推送，故整体 try 住。"""
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (ARCHIVE_DIR / f"{today}.txt").write_text(report, encoding="utf-8")
        cutoff = datetime.now() - timedelta(days=ARCHIVE_KEEP_DAYS)
        for f in ARCHIVE_DIR.glob("*.txt"):
            try:
                if datetime.strptime(f.stem, "%Y-%m-%d") < cutoff:
                    f.unlink()
            except ValueError:
                continue        # 文件名不是日期格式，不归本函数管，留着
        print(f"  ✓ 稿件已归档 logs/archive/{today}.txt", file=sys.stderr)
    except Exception as e:
        print(f"  ⚠️ 稿件归档失败（不影响本次推送）：{e}", file=sys.stderr)


def load_week_archives(days: int = WEEKLY_LOOKBACK_DAYS) -> list:
    """读最近 days 天（不含今天）的存档，按日期从旧到新返回 [(日期, 稿件正文)]。

    缺哪天跳哪天——某天没出稿不该让整个周回顾停摆。"""
    today = datetime.now().date()
    out = []
    for i in range(days, 0, -1):
        day  = today - timedelta(days=i)
        path = ARCHIVE_DIR / f"{day.isoformat()}.txt"
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if text:
            out.append((day.isoformat(), text))
    return out


# ===== 整理新闻数据 =====
def build_ai_context(all_entries: list) -> tuple:
    """整理素材，返回 (context, kept_per_source, drop_stats)。

    过滤顺序：标题/URL 缺失 → 单次运行内 URL 去重 → 跨天已播去重 →
    时间窗（按源，见 RSS_SOURCES 的 window_h）→ 泛科技源的 AI 相关性闸门。"""
    now = datetime.now(timezone.utc)
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
        # 时间窗按源取，缺省 24h；放宽的源靠上面的跨天去重兜住重复
        window_h = getattr(entry, "__window_h", DEFAULT_WINDOW_H)
        pub_date = parse_entry_date(entry)
        if not pub_date or pub_date < now - timedelta(hours=window_h):
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


# ===== P0: 推送飞书 =====
# HTML → 富文本 post 的转换、20KB 分页、失败重试统一在 bot_utils.send_feishu 里做
# （三个 bot 共用同一实现）。
def send_report(text: str) -> int:
    """推送稿件，返回实际发出的消息条数。调用方须已完成 sanitize_html。"""
    return send_feishu(text, FEISHU_WEBHOOK, FEISHU_SECRET)


# ===== 抓取阶段（fetch 模式用）=====
def _proxy_ok() -> bool:
    """代理预检 + 端口自愈。

    配置端口不通时会探测候选端口（见 bot_utils.PROXY_CANDIDATES），命中则
    就地切换本进程的 SESSION 与环境变量——换代理软件导致端口变化时不再
    静默停摆。无代理配置直接放行（视为直连）。"""
    global _PROXY
    resolved, switched = resolve_proxy(_PROXY)
    if resolved is None:
        return not _PROXY          # 本来就没配代理 → 直连放行；配了但全不通 → 失败
    if switched:
        _PROXY = resolved
        # 抓取侧走 requests/feedparser，两者都读环境变量（用赋值而非 setdefault）
        os.environ["HTTP_PROXY"] = resolved
        os.environ["HTTPS_PROXY"] = resolved
    return True


def fetch_news() -> tuple:
    """抓 RSS + 整理去重，返回 (ai_context, rss_fetched, entry_count, zero_sources, source_stats)。
    进度打到 stderr，让 fetch 模式的 stdout 只保留干净的 context。"""
    print("📡 抓取 RSS 源...", file=sys.stderr)
    all_entries = []
    fetched_counts: dict = {}
    for src in RSS_SOURCES:
        feed_url, limit, is_general = src[:3]
        window_h = src[3] if len(src) > 3 else DEFAULT_WINDOW_H
        entries = fetch_rss(feed_url, limit)
        domain  = feed_url.split("/")[2]
        # 给条目打上来源标记，供后续统计"过滤后每个源还剩几条"与相关性闸门判定
        for e in entries:
            e["__src"]      = domain
            e["__general"]  = is_general
            e["__window_h"] = window_h
        all_entries.extend(entries)
        fetched_counts[domain] = fetched_counts.get(domain, 0) + len(entries)
        note = "" if window_h == DEFAULT_WINDOW_H else f"（窗口 {window_h}h）"
        print(f"  ✓ {len(entries)} 条  {feed_url}{note}", file=sys.stderr)

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
          f"超时效窗 {drops['stale']} · 非 AI {drops['off_topic']} → 保留 {entry_count}",
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

    # 周日改出《AI 产业周回顾》：素材以本周已播稿件的存档为主。
    # 但仍照常抓最近 24h——周六 10:00 到周日 10:00 这段新闻不在任何一天的存档里，
    # 不抓就永远没人播；抓来并进回顾，send 后照常写 sent_urls 完成去重闭环。
    # FORCE_WEEKLY=1 可在非周日强制预览回顾流程。
    is_sunday = datetime.now().weekday() == 6 or os.getenv("FORCE_WEEKLY") == "1"
    archives   = load_week_archives() if is_sunday else []
    weekly     = len(archives) >= WEEKLY_MIN_ARCHIVES
    if is_sunday and not weekly:
        print(f"ℹ️ 今天是周日，但只有 {len(archives)} 份稿件存档"
              f"（需 ≥{WEEKLY_MIN_ARCHIVES} 份），本次退回当日日报", file=sys.stderr)

    ai_context, rss_fetched, entry_count, zero_sources, source_stats, stale_sources = fetch_news()

    # 日报没素材就不发；周回顾的主素材是存档，24h 无新增照样成立
    if not ai_context and not weekly:
        print("=== NO_NEWS ===")
        write_log("WARN", "过去24小时无有效新闻，未发送")
        return 0

    # 写边车：OK 日志摘要 + metrics，供 send 模式回填（保持 health_check 监控存活）
    log_summary = (f"周回顾{len(archives)}天存档 + 新增{entry_count}条"
                   if weekly else f"抓取{rss_fetched}条 → 保留{entry_count}条")
    FETCH_META.write_text(
        json.dumps(
            {"log_summary": log_summary,
             "kind": "weekly" if weekly else "daily",
             "metrics": {"rss_fetched": rss_fetched, "rss_kept": entry_count,
                         "rss_zero_sources": zero_sources,
                         "rss_source_stats": source_stats,
                         "rss_stale_sources": stale_sources,
                         "report_kind": "weekly" if weekly else "daily",
                         "weekly_archive_days": len(archives)}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    today = datetime.now().strftime("%Y-%m-%d")
    # stdout 只输出结构化标记 + context，供 Claude routine 稳定解析
    print("=== WEEKLY_OK ===" if weekly else "=== FETCH_OK ===")
    print(f"今天日期：{today}")
    if weekly:
        print(f"本周回顾覆盖 {len(archives)} 天存档（{archives[0][0]} ~ {archives[-1][0]}），"
              f"另有最近 24 小时新增 {entry_count} 条")
    else:
        print(f"保留 {entry_count} 条有效新闻（共抓取 {rss_fetched} 条）")
    if zero_sources:
        print(f"零产源：{', '.join(zero_sources)}")
    # 连续零产达阈值 → 结构化告警块，供 routine 在日报汇报里转述给用户
    if stale_sources:
        print("=== SOURCE_ALERT ===")
        for d, n in stale_sources.items():
            print(f"{d} 已连续 {n} 天零产，建议从 RSS_SOURCES 移除或更换")
        print("=== SOURCE_ALERT_END ===")
    # 周回顾的主素材：本周每天已播出去的完整稿件，按日期从旧到新
    if weekly:
        print("=== ARCHIVE_BEGIN ===")
        for day, text in archives:
            print(f"===== {day} 日报 =====")
            print(text)
            print()
        print("=== ARCHIVE_END ===")
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

    # HTML 白名单清洗：这里的 HTML 只是内部中间格式，send_report 会把它翻译成
    # 飞书的富文本结构；清洗保证正文里的裸 < > & 不会被后面的标签解析吃掉。
    report = sanitize_html(report)

    # P2: 先持久化缓存，防止推送失败时内容丢失
    # 这里不再做代理预检：飞书直连可达，推送阶段本来就不需要翻墙代理。
    # （Telegram 时代代理一挂当天就整个不播，现在只有抓取阶段依赖它。）
    save_pending([report])
    print("📨 推送到飞书...", file=sys.stderr)
    sent = send_report(report)
    print(f"  ✓ 推送成功（{sent} 条）", file=sys.stderr)
    CACHE_FILE.unlink(missing_ok=True)

    # 归档本次真正播出去的链接，供后续 fetch 跨天去重。
    # 记在发送成功之后：发失败的那批不该被标成"已播"。
    hrefs = extract_hrefs(report)
    if hrefs:
        total = record_sent_urls(SENT_URLS, hrefs)
        print(f"  ✓ 已归档 {len(hrefs)} 条链接用于跨天去重（档案共 {total} 条）",
              file=sys.stderr)

    # 存下当天稿件，供周日的《AI 产业周回顾》取材（周回顾稿本身也存，
    # 但回看只取 6 天即周一到周六，不会把上周日的回顾稿卷进下一份回顾）
    archive_draft(report)

    # OK 日志：从 fetch 边车取摘要与 metrics，保持 health_check 监控存活
    try:
        meta = json.loads(FETCH_META.read_text(encoding="utf-8"))
    except Exception:
        meta = {"log_summary": "", "metrics": {}}
    duration = round(time.time() - t0, 1)
    write_log(
        "OK",
        f"Claude写稿 → {meta.get('log_summary', '')} → 飞书推送成功（{sent} 条 / {len(report)}字）",
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

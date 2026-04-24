#!/opt/homebrew/bin/python3.11
"""
AI 产业日报
从多个 RSS 源抓取新闻，用 DeepSeek 生成日报，发送到 Telegram。
"""

import os
import sys
import time
import json
import socket
import calendar
import traceback
import functools
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from openai import OpenAI

LOG_FILE   = Path(__file__).parent / "run.log"
JSONL_FILE = Path(__file__).parent / "run.jsonl"
CACHE_FILE = Path(__file__).parent / "pending_messages.json"

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
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "7788909584")

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

SYSTEM_PROMPT = """\
角色：你是一位 AI 产业资深编辑，长期跟踪全球 AI 模型、算力基础设施、机器人、产业政策和商业动态。面向中文读者输出内容，要求专业、克制、有信息密度，避免空话。

任务：基于输入的新闻数据，生成一份《AI 产业日报》。

━━━━━━━━━━━━━━━━
数据说明：
每条新闻包含以下字段：
- 原始英文标题：必须原样保留，不得翻译
- 链接：原文 URL，用于超链接
- 来源域名：媒体来源
- 摘要：新闻内容摘要

注意：数据中可能包含重复报道，可能包含中英文内容
━━━━━━━━━━━━━━━━
【处理规则】

1. 去重与合并（必须执行）
- 标题语义相似 + 主体一致 → 视为同一事件，合并为一条
- 优先保留信息最完整、最权威的来源

2. 时效过滤
- 今日日报只收录过去 24 小时内的新闻
- 如果某条新闻的核心事件明显是昨天或更早已广泛报道的，降低其优先级或不收录

3. 信息筛选
- 仅保留评分 >= 3分 的新闻

4. 评分标准
5分：头部公司重大动作、基础模型发布、国家级政策、公司战略级转型
4分：影响开发者生态或商业模式、性能/成本显著提升、平台级产品更新、人形机器人重大进展
3分：公司融资、合作、产品发布、市场扩展、机器人产品评测

5. 排序：按评分从高到低，同分下 AI模型 > AI基础设施 > AI政策 > AI公司 > AI产品 > 机器人

6. 标签（每条选1-2个）：#AI模型 #AI产品 #AI公司 #AI政策 #AI基础设施 #机器人

7. 语言：中文撰写，保留关键英文产品名
━━━━━━━━━━━━━━━━
【写作要求】

The Details 要求：
- 250字以内，说清楚"发生了什么、谁做的、关键数字/细节"
- 不加主观评价，只陈述事实

Why it matters 禁止：
- "这标志着……""这推动了……""这意味着AI行业……"
- 任何以"这"字开头后跟宽泛影响的句子

Why it matters 必须包含以下其中一种（100字以内）：
- 对哪类公司或开发者是具体的利好或利空
- 会触发哪个具体的连锁反应
- 和近期另一事件的关联及叠加影响
- 某个数字背后隐含的竞争逻辑
━━━━━━━━━━━━━━━━
【输出格式】

📋 AI 产业日报 · [今天日期]

⚡ 30秒简讯速览
· [一句话，最重要的事件]
· [一句话，第二重要的事件]
· [一句话，第三重要的事件]
（共 3–5 条，每条独立一行，只陈述事实，不加分析，读者扫一眼即可判断今天发生了什么）

📌 产业动态

[星级] [标签]
[中文标题，一句话说清事件]
📄 The Details：[250字以内，陈述事实、关键数字、涉及主体]
💡 Why it matters：[100字以内，具体分析]
🔗 <a href="原文链接URL">英文原始标题 · 媒体名</a>

星级：5分=⭐⭐⭐⭐⭐ 4分=⭐⭐⭐⭐ 3分=⭐⭐⭐

排版：星级和标签占第一行，中文标题另起一行，每条之间空行分隔，不加横线
风格：不用感叹号，不用"重磅""颠覆"等夸张词

    ⚠️ 字数限制：整份日报（含所有内容）必须控制在 4096 字符以内。如条目过多，优先保留高评分条目，裁减低分条目，宁可少而精。\
"""

def sanitize_html(text: str) -> str:
    """清理 HTML 标签，仅保留 b 和 a，并修复未闭合标签，转义非法字符"""
    import re
    # 1. 保护合法标签 <b> </b> <a href="..."> </a>
    # 将其暂时替换为特殊占位符
    text = re.sub(r'<b>', '[[B_OPEN]]', text)
    text = re.sub(r'</b>', '[[B_CLOSE]]', text)
    # 提取 a 标签
    a_tags = []
    def save_a(m):
        a_tags.append(m.group(0))
        return f"[[A_TAG_{len(a_tags)-1}]]"
    text = re.sub(r'<a\s+href="[^"]+">.*?</a>', save_a, text)

    # 2. 转义所有剩余的 < > & (Telegram 要求)
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # 3. 还原合法标签
    text = text.replace('[[B_OPEN]]', '<b>').replace('[[B_CLOSE]]', '</b>')
    for i, tag in enumerate(a_tags):
        text = text.replace(f"[[A_TAG_{i}]]", tag)

    # 4. 最终检查：确保标签闭合（简单计数补全）
    if text.count('<b>') > text.count('</b>'):
        text += '</b>' * (text.count('<b>') - text.count('</b>'))
    if text.count('<a ') > text.count('</a>'):
        text += '</a>' * (text.count('<a ') - text.count('</a>'))
    
    return text


# ===== P0: 指数退避重试装饰器 =====
def with_retry(max_retries=3, base_delay=5.0, exceptions=(Exception,)):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_retries:
                        raise
                    delay = base_delay * (2 ** attempt)
                    print(f"[RETRY] {func.__name__} 第{attempt+1}次失败: {e}，{delay:.0f}s 后重试",
                          file=sys.stderr)
                    time.sleep(delay)
        return wrapper
    return decorator


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
            _send_one(msg)
        CACHE_FILE.unlink(missing_ok=True)
        print("[CACHE] 缓存消息重发成功")
        return True
    except Exception as e:
        print(f"[WARN] 缓存重发失败: {e}", file=sys.stderr)
        return False


# ===== P1: 抓取 RSS（socket 超时 + 重试兜底）=====
def fetch_rss(url: str, limit: int, retries: int = 2, delay: float = 3.0) -> list:
    UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(10)
    try:
        for attempt in range(retries + 1):
            try:
                feed = feedparser.parse(url, request_headers={"User-Agent": UA})
                if feed.entries:
                    return feed.entries[:limit]
                if attempt < retries:
                    time.sleep(delay)
            except Exception as e:
                if attempt < retries:
                    time.sleep(delay)
                else:
                    print(f"[WARN] 抓取失败（已重试 {retries} 次）{url}: {e}", file=sys.stderr)
    finally:
        socket.setdefaulttimeout(old_timeout)
    return []


def parse_entry_date(entry) -> Optional[datetime]:
    for field in ("published_parsed", "updated_parsed"):
        t = getattr(entry, field, None)
        if t:
            try:
                return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
            except Exception:
                pass
    return None


# ===== 整理新闻数据 =====
def build_ai_context(all_entries: list) -> str:
    now        = datetime.now(timezone.utc)
    time_limit = now - timedelta(days=1)
    seen_urls: set = set()
    lines: list = []

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
        lines.append(
            f"[原始英文标题] {title}\n[链接] {original_url}\n"
            f"[来源域名] {url_lower}\n[摘要] {snippet[:200]}\n----"
        )

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
            {"role": "system", "content": SYSTEM_PROMPT},
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
    chunks = [text[i : i + MAX_LEN] for i in range(0, len(text), MAX_LEN)]
    for chunk in chunks:
        _send_one(chunk)


# ===== 主流程 =====
def main() -> None:
    t0 = time.time()

    # P2: 优先重发上次未发送的缓存消息
    flush_pending()

    print("📡 抓取 RSS 源...")
    all_entries = []
    for feed_url, limit in RSS_SOURCES:
        entries = fetch_rss(feed_url, limit)
        all_entries.extend(entries)
        print(f"  ✓ {len(entries)} 条  {feed_url}")

    print(f"\n📰 共抓取 {len(all_entries)} 条，整理过滤中...")
    ai_context = build_ai_context(all_entries)

    if not ai_context:
        print("⚠️  过去 24 小时内无有效新闻，退出。")
        write_log("WARN", "过去24小时无有效新闻，未发送")
        return

    entry_count = ai_context.count("----")
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
        f"抓取{len(all_entries)}条 → 保留{entry_count}条 → Telegram发送成功",
        metrics={
            "rss_fetched": len(all_entries), "rss_kept": entry_count,
            "ai_calls": 1, "duration_s": duration,
        },
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err = traceback.format_exc().strip().splitlines()[-1]
        write_log("FAIL", err)
        raise

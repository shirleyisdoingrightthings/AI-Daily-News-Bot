#!/opt/homebrew/bin/python3.11
"""
AI 产业日报
从多个 RSS 源抓取新闻，用 DeepSeek 生成日报，发送到 Telegram。
"""

import os
import sys
import time
import json
import traceback
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from openai import OpenAI

# 共享工具库
sys.path.insert(0, str(Path.home() / "Desktop" / "bot_ops" / "shared"))
from bot_utils import sanitize_html, with_retry, fetch_rss, parse_entry_date, already_ran_today

LOG_FILE   = Path(__file__).parent / "logs" / "run.log"
JSONL_FILE = Path(__file__).parent / "logs" / "run.jsonl"
LOG_FILE.parent.mkdir(exist_ok=True)
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
角色：你是一位 AI 产业资深编辑，长期跟踪全球 AI 模型、算力基础设施、机器人、产业政策和商业动态。面向中文读者输出内容，要求专业、克制、有信息密度，不带 AI 总结腔。

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

2. 时效说明
- 输入数据已预过滤为 24 小时内，无需再做时效判断，直接按评分筛选

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

The Details 格式与规则：
- 使用 bullet list，格式：· [主谓结构的核心事实句]：补充细节与量化数据
- 5分/4分事件写 4-5 条，3分事件写 3 条；每条必须有具体数字、人名或量化数据
- **小标题必须是完整的主谓结构句**，读者只看小标题就能获得这条 Detail 的核心信息，无需再看冒号后面的内容也能知道发生了什么
  - ✅ 正确示例：「OpenAI 本轮估值升至 $3000 亿，较上轮翻倍」「微软削减 2 GW 数据中心合同」「TML 由前 OpenAI 研究员创立，已完成 $4 亿融资」
  - ❌ 错误示例：「背景」「投资规模」「竞争格局」「行业现象」「政策边界」
- 判断标准：只读小标题，读者是否已经知道"谁做了什么"或"发生了什么数字级别的事"？不知道就必须重写。
- 严格职能边界：只写客观事实，以下内容禁止出现在 Details，必须后置到 Why it matters：
  · "这意味着……""说明……""暗示……"等推断性表述
  · "大概率""可能""预计"等概率性判断（原文直接引用当事方预测除外）
  · 对动机或未来走向的解读
- 严禁模糊代词：不写"该公司""这项技术"，必须写具体名称（如 OpenAI、Claude 3.5）

Why it matters 规则（4分/5分事件 80-120字，3分事件 80字内）：
- 第一句直接切入因果判断，不复述 Details 内容
- 必须说明具体受影响对象（某类公司、开发者或用户），禁止写"整个行业将会……"
- 因果链必须说到"机制"层面，禁止三级跳到宏大结论
- 必须包含以下方向之一：
  · 对哪类公司或开发者是具体的利好或利空
  · 会触发哪个具体的连锁反应
  · 与本期另一条新闻的关联及叠加影响
  · 某个数字背后隐含的竞争逻辑

语言规范（去除 AI 味，写完每条必须自查）：
- 词汇黑名单（出现即删）：这标志着、这意味着、深度、赋能、颠覆、重磅、范式转移、根本性转变、历史性一刻、新时代、整个行业、不仅……更……
- 不用排比句式，不用感叹号，不用夸张词
- 每个判断有具体对象，不泛指
- Details 每条有主语和动词，不写名词堆砌
- 破折号（——）非必要不使用，能用句号或逗号断开的不用

⚠️ HTML标签限制：只能使用 <b>文字</b> 和 <a href="URL">文字</a> 两种标签，禁止使用任何其他 HTML 标签（如 <i>、<br>、<code> 等），否则会导致消息发送失败。
━━━━━━━━━━━━━━━━
【输出格式】

📋 AI 产业日报 · [今天日期]

⚡ 30秒简讯速览
· [一句话，最重要的事件，不超过30字]
· [一句话，第二重要的事件，不超过30字]
· [一句话，第三重要的事件，不超过30字]
（共 3–5 条，每条不超过 30 字，只陈述事实，不加分析；顺序与正文产业动态一致）

📌 产业动态

[星级] [标签]
<b>[中文标题，一句话说清事件，不超过20字]</b>
📄 The Details：
· [核心事实或数据，不是分类词]：具体事实和数据
· [核心事实或数据，不是分类词]：具体事实和数据
· [核心事实或数据，不是分类词]：具体事实和数据
💡 Why it matters：[直接从因果判断开始，不复述Details]
🔗 <a href="原文链接URL">英文原始标题 · 媒体名</a>

星级：5分=⭐⭐⭐⭐⭐ 4分=⭐⭐⭐⭐ 3分=⭐⭐⭐

排版：星级和标签占第一行，中文标题另起一行，每条之间空行分隔，不加横线\
"""

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


# ===== 主流程 =====
def main() -> None:
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

    # 代理预检：快速验证代理可用，失败立即退出
    if _PROXY:
        try:
            SESSION.get("https://www.google.com", timeout=5)
        except Exception:
            write_log("WARN", f"代理不可用（{_PROXY}），跳过本次运行")
            return

    print("📡 抓取 RSS 源...")
    all_entries = []
    source_counts: dict = {}
    for feed_url, limit in RSS_SOURCES:
        entries = fetch_rss(feed_url, limit)
        all_entries.extend(entries)
        source_counts[feed_url.split("/")[2]] = len(entries)
        print(f"  ✓ {len(entries)} 条  {feed_url}")
    zero_sources = [d for d, c in source_counts.items() if c == 0]

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
            "rss_zero_sources": zero_sources,
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

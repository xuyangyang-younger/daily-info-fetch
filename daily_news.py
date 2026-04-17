#!/usr/bin/env python3
"""
Daily News Digest — 每日国际新闻中文摘要推送
Fetches news from HN API, TechCrunch RSS, 36kr RSS,
summarises via Hermes LLM, sends to WeChat via Hermes send_weixin_direct.
"""

import json
import os
import re
import sys
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# Add Hermes to Python path for send_weixin_direct
sys.path.insert(0, "/root/.hermes/hermes-agent")

# ── Logging ────────────────────────────────────────────────────────
LOG_DIR = os.path.expanduser("~/daily-news-digest/logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "daily_news.log")),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────
HERMES_API_URL = os.getenv("HERMES_API_URL", "http://localhost:8000/v1/chat/completions")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "REDACTED_HERMES_KEY")

WEIXIN_TOKEN = os.getenv("WEIXIN_TOKEN", "b28d654c09d9@im.bot:REDACTED_WEIXIN_TOKEN")
WEIXIN_CHAT_ID = os.getenv("WEIXIN_CHAT_ID", "o9cq80wJs4pU5Dbx0YV-5anUXC04@im.wechat")
WEIXIN_ACCOUNT_ID = os.getenv("WEIXIN_ACCOUNT_ID", "b28d654c09d9@im.bot")

FETCH_TIMEOUT = 10
BJT = timezone(timedelta(hours=8))


# ═══════════════════════════════════════════════════════════════════
#  NEWS FETCHING
# ═══════════════════════════════════════════════════════════════════

def fetch_hn_news(max_items=15):
    """Fetch top stories from Hacker News Firebase API."""
    try:
        r = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            timeout=FETCH_TIMEOUT,
        )
        story_ids = r.json()[:max_items]
        stories = []
        for sid in story_ids:
            try:
                item = requests.get(
                    "https://hacker-news.firebaseio.com/v0/item/{}.json".format(sid),
                    timeout=FETCH_TIMEOUT,
                ).json()
                if item and item.get("title"):
                    stories.append({
                        "title": item["title"],
                        "url": item.get("url", "https://news.ycombinator.com/item?id={}".format(sid)),
                        "score": item.get("score", 0),
                        "source": "Hacker News",
                    })
            except Exception:
                continue
        log.info("HN: fetched %d stories", len(stories))
        return stories
    except Exception as e:
        log.error("HN fetch failed: %s", e)
        return []


def fetch_rss(url, source_name, max_items=10):
    """Fetch news from an RSS feed using xml.etree."""
    try:
        r = requests.get(url, timeout=FETCH_TIMEOUT, headers={
            "User-Agent": "Mozilla/5.0 (compatible; DailyNewsBot/1.0)"
        })
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        stories = []
        for item in items[:max_items]:
            title = (
                item.findtext("title")
                or item.findtext("atom:title", namespaces=ns)
                or ""
            ).strip()
            link = (item.findtext("link") or "").strip()
            if link.startswith("<![CDATA["):
                link = link[9:-3] if link.endswith("]]>") else link[9:]
            desc = (
                item.findtext("description")
                or item.findtext("atom:summary", namespaces=ns)
                or ""
            ).strip()
            desc = re.sub(r"<[^>]+>", "", desc).strip()[:300]
            if title:
                stories.append({
                    "title": title,
                    "url": link,
                    "description": desc,
                    "source": source_name,
                })
        log.info("%s: fetched %d stories", source_name, len(stories))
        return stories
    except Exception as e:
        log.error("%s RSS fetch failed: %s", source_name, e)
        return []


def fetch_all_news():
    """Fetch from all news sources and return combined list."""
    hn = fetch_hn_news(15)
    tc = fetch_rss("https://techcrunch.com/feed/", "TechCrunch", 10)
    kr = fetch_rss("https://36kr.com/feed", "36Kr", 10)

    finance_kw = re.compile(
        r"stock|market|economy|bank|fed|rate|inflation|GDP|trade|tariff|"
        r"crypto|bitcoin|invest|earning|revenue|IPO|金融|股|融资|经济",
        re.IGNORECASE,
    )
    ai_kw = re.compile(
        r"\bAI\b|artificial.intelligence|LLM|GPT|Claude|Gemini|neural|"
        r"machine.learning|deep.learning|transformer|AGI|OpenAI|Anthropic|"
        r"大模型|人工智能|智能|算力|芯片|推理",
        re.IGNORECASE,
    )

    all_news = []
    for item in hn + tc + kr:
        text = item["title"] + " " + item.get("description", "")
        if finance_kw.search(text):
            item["category"] = "finance"
        elif ai_kw.search(text):
            item["category"] = "ai"
        else:
            item["category"] = "tech"
        all_news.append(item)

    return all_news


# ═══════════════════════════════════════════════════════════════════
#  LLM SUMMARIZATION via Hermes API
# ═══════════════════════════════════════════════════════════════════

def generate_summary(news_items):
    """Call Hermes API to generate a Chinese news digest."""
    now = datetime.now(BJT)
    period = "早间" if now.hour < 12 else "晚间"
    date_str = now.strftime("%Y-%m-%d")

    news_text = ""
    for cat_name, cat_key in [("Finance", "finance"), ("AI", "ai"), ("Tech", "tech")]:
        cat_items = [n for n in news_items if n.get("category") == cat_key]
        if cat_items:
            news_text += "\n## {} News\n".format(cat_name)
            for i, n in enumerate(cat_items, 1):
                news_text += "{}. [{}] {}\n".format(i, n["source"], n["title"])
                if n.get("description"):
                    news_text += "   {}\n".format(n["description"][:200])

    prompt = (
        "你是一个专业的新闻编辑。请根据以下原始新闻数据，生成一份精选中文新闻摘要。\n\n"
        "当前时段: {period}\n日期: {date}\n\n"
        "原始新闻数据:\n{news}\n\n"
        "请严格按以下纯文本格式输出（不要用任何 Markdown 标记，不要用 ** 加粗）:\n\n"
        "📰 {period}新闻速递 | {date}\n\n"
        "━━━ 💰 金融市场 ━━━\n\n"
        "1️⃣ 【标题不超过15字】\n2-3句中文摘要，概括核心信息和影响。\n\n"
        "2️⃣ 【标题】\n摘要内容。\n\n"
        "━━━ 🤖 AI 前沿 ━━━\n\n"
        "3️⃣ 【标题】\n摘要。\n\n"
        "━━━ 🔬 科技动态 ━━━\n\n"
        "5️⃣ 【标题】\n摘要。\n\n"
        "📎 新闻来源: Hacker News, TechCrunch, 36Kr\n\n"
        "要求:\n"
        "- 总共精选 5-8 条最有价值的新闻\n"
        "- 每个板块至少 1-2 条，AI 板块优先多选\n"
        "- 每条摘要 2-3 句话（50-100字）\n"
        "- 标题不超过15字\n"
        "- 全文控制在 2000 字符以内\n"
        "- 使用简洁中文，不要翻译腔\n"
        "- 保留关键数据（数字、百分比）\n"
        "- 绝对不要使用 Markdown 语法（不要 ** # 等符号）\n"
        "- 确保内容基于提供的新闻数据，不要编造\n"
        "- 直接输出新闻摘要，不要输出任何前言或解释"
    ).format(period=period, date=date_str, news=news_text)

    try:
        r = requests.post(
            HERMES_API_URL,
            headers={
                "Authorization": "Bearer " + HERMES_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "model": "hermes-agent",
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        log.info("LLM summary generated: %d chars", len(content))
        return content
    except Exception as e:
        log.error("LLM summarization failed: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════
#  WECHAT SENDING via Hermes send_weixin_direct
# ═══════════════════════════════════════════════════════════════════

def send_to_wechat(text):
    """Send a text message to WeChat using Hermes's native iLink adapter."""
    import asyncio
    from gateway.platforms.weixin import send_weixin_direct

    async def _send():
        return await send_weixin_direct(
            extra={
                "account_id": WEIXIN_ACCOUNT_ID,
                "base_url": "https://ilinkai.weixin.qq.com",
                "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
            },
            token=WEIXIN_TOKEN,
            chat_id=WEIXIN_CHAT_ID,
            message=text,
        )

    try:
        result = asyncio.run(_send())
        log.info("WeChat send result: %s", result)
        return result.get("success", False)
    except Exception as e:
        log.error("WeChat send failed: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    log.info("=" * 50)
    log.info("Daily News Digest starting...")
    now = datetime.now(BJT)
    log.info("Time: %s BJT", now.strftime("%Y-%m-%d %H:%M:%S"))

    # 1. Fetch news
    log.info("Step 1: Fetching news...")
    news = fetch_all_news()
    if not news:
        log.error("No news fetched from any source. Aborting.")
        sys.exit(1)
    log.info("Total news items: %d", len(news))
    by_cat = {}
    for n in news:
        by_cat.setdefault(n.get("category", "tech"), []).append(n)
    for cat, items in by_cat.items():
        log.info("  %s: %d items", cat, len(items))

    # 2. Generate summary
    log.info("Step 2: Generating Chinese summary via LLM...")
    summary = generate_summary(news)
    if not summary:
        log.error("LLM summary generation failed. Aborting.")
        sys.exit(1)
    log.info("Summary length: %d chars", len(summary))
    log.info("--- Summary preview ---")
    log.info(summary[:500])
    log.info("--- End preview ---")

    # 3. Send to WeChat
    log.info("Step 3: Sending to WeChat...")
    ok = send_to_wechat(summary)

    if ok:
        log.info("Daily news digest sent successfully!")
    else:
        log.error("Failed to send news digest to WeChat")
        sys.exit(1)

    log.info("=" * 50)


if __name__ == "__main__":
    main()

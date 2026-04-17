#!/usr/bin/env python3
"""
Daily News Digest — 每日新闻多源抓取 + 权重排序 + 中文摘要推送

Pipeline:
  1. 并行抓取 12+ 信息源（HN API / RSS / GitHub Trending HTML）
  2. 自动分类（finance / ai / tech / world）
  3. 打分 & 交叉验证（源权重 × 新鲜度 × 热度 + 交叉源奖励 + 关键词加分）
  4. 去重合并（同一事件的不同源合并，附交叉源列表）
  5. 每板块 top-N 送入 Hermes LLM 生成中文速递
  6. 通过 Hermes send_weixin_direct 推送微信
"""

import json
import os
import re
import sys
import math
import logging
import requests
import xml.etree.ElementTree as ET
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

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
UA = "Mozilla/5.0 (compatible; DailyNewsBot/2.0)"

# ═══════════════════════════════════════════════════════════════════
#  SOURCE REGISTRY
#  weight: 源权重 (0.7 ~ 1.5)
#  cat:    默认板块归属 (仍会被关键词分类覆盖)
#  type:   hn_api / rss / gh_trending
# ═══════════════════════════════════════════════════════════════════
SOURCES = [
    # ─ Finance ─────────────────────────────────────
    {"name": "WSJ Markets",       "type": "rss",   "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",                "weight": 1.2, "cat": "finance", "max": 8},
    {"name": "WSJ Business",      "type": "rss",   "url": "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",              "weight": 1.2, "cat": "finance", "max": 8},
    {"name": "CNBC Top",          "type": "rss",   "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",         "weight": 1.1, "cat": "finance", "max": 10},
    {"name": "CNBC Finance",      "type": "rss",   "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html",          "weight": 1.1, "cat": "finance", "max": 10},
    {"name": "MarketWatch",       "type": "rss",   "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",    "weight": 1.0, "cat": "finance", "max": 8},
    {"name": "Yahoo Finance",     "type": "rss",   "url": "https://finance.yahoo.com/news/rssindex",                       "weight": 0.9, "cat": "finance", "max": 10},
    {"name": "Investing.com",     "type": "rss",   "url": "https://www.investing.com/rss/news.rss",                        "weight": 0.9, "cat": "finance", "max": 8},
    {"name": "华尔街见闻",         "type": "rss",   "url": "https://dedicated.wallstreetcn.com/rss.xml",                    "weight": 1.1, "cat": "finance", "max": 15},

    # ─ AI (一手源权重最高) ──────────────────────────
    {"name": "OpenAI Blog",       "type": "rss",   "url": "https://openai.com/blog/rss.xml",                               "weight": 1.5, "cat": "ai",      "max": 5},
    {"name": "Google AI Blog",    "type": "rss",   "url": "https://blog.google/technology/ai/rss/",                        "weight": 1.4, "cat": "ai",      "max": 5},
    {"name": "DeepMind",          "type": "rss",   "url": "https://deepmind.google/blog/rss.xml",                          "weight": 1.4, "cat": "ai",      "max": 5},
    {"name": "MIT Tech Review AI","type": "rss",   "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed", "weight": 1.2, "cat": "ai", "max": 5},
    {"name": "arXiv cs.AI",       "type": "rss",   "url": "http://export.arxiv.org/rss/cs.AI",                             "weight": 1.1, "cat": "ai",      "max": 6},
    {"name": "arXiv cs.LG",       "type": "rss",   "url": "http://export.arxiv.org/rss/cs.LG",                             "weight": 1.0, "cat": "ai",      "max": 4},
    {"name": "arXiv cs.CL",       "type": "rss",   "url": "http://export.arxiv.org/rss/cs.CL",                             "weight": 1.0, "cat": "ai",      "max": 4},
    {"name": "量子位",             "type": "rss",   "url": "https://www.qbitai.com/feed",                                   "weight": 1.0, "cat": "ai",      "max": 8},

    # ─ Tech ────────────────────────────────────────
    {"name": "Hacker News",       "type": "hn_api","url": "",                                                              "weight": 1.0, "cat": "tech",    "max": 20},
    {"name": "TechCrunch",        "type": "rss",   "url": "https://techcrunch.com/feed/",                                  "weight": 1.0, "cat": "tech",    "max": 10},
    {"name": "The Verge",         "type": "rss",   "url": "https://www.theverge.com/rss/index.xml",                        "weight": 1.0, "cat": "tech",    "max": 10},
    {"name": "Ars Technica",      "type": "rss",   "url": "https://feeds.arstechnica.com/arstechnica/index",               "weight": 1.0, "cat": "tech",    "max": 10},
    {"name": "Wired",             "type": "rss",   "url": "https://www.wired.com/feed/rss",                                "weight": 1.0, "cat": "tech",    "max": 10},
    {"name": "BBC Tech",          "type": "rss",   "url": "https://feeds.bbci.co.uk/news/technology/rss.xml",              "weight": 1.1, "cat": "tech",    "max": 8},
    {"name": "InfoQ",             "type": "rss",   "url": "https://feed.infoq.com/",                                       "weight": 0.9, "cat": "tech",    "max": 8},
    {"name": "36Kr",              "type": "rss",   "url": "https://36kr.com/feed",                                         "weight": 1.0, "cat": "tech",    "max": 10},
    {"name": "爱范儿",             "type": "rss",   "url": "https://www.ifanr.com/feed",                                    "weight": 0.9, "cat": "tech",    "max": 8},
    {"name": "少数派",             "type": "rss",   "url": "https://sspai.com/feed",                                        "weight": 0.8, "cat": "tech",    "max": 6},
    {"name": "GitHub Trending",   "type": "gh_trending", "url": "https://github.com/trending?since=daily",                 "weight": 0.9, "cat": "tech",    "max": 8},

    # ─ World (综合时政，作为背景板块) ───────────────
    {"name": "BBC World",         "type": "rss",   "url": "https://feeds.bbci.co.uk/news/world/rss.xml",                   "weight": 1.2, "cat": "world",   "max": 8},
    {"name": "NYT HomePage",      "type": "rss",   "url": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",     "weight": 1.2, "cat": "world",   "max": 8},
]


# ═══════════════════════════════════════════════════════════════════
#  FETCHERS
# ═══════════════════════════════════════════════════════════════════

def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_date(s):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            continue
    return None


def fetch_hn(src):
    try:
        ids = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            timeout=FETCH_TIMEOUT,
        ).json()[: src["max"]]
        stories = []
        # Parallel fetch HN items
        def _get(sid):
            return requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                timeout=FETCH_TIMEOUT,
            ).json()

        with ThreadPoolExecutor(max_workers=10) as ex:
            for item in ex.map(_get, ids):
                if not item or not item.get("title"):
                    continue
                stories.append({
                    "title": item["title"],
                    "url": item.get("url") or f"https://news.ycombinator.com/item?id={item['id']}",
                    "description": "",
                    "source": src["name"],
                    "source_weight": src["weight"],
                    "default_cat": src["cat"],
                    "published": datetime.fromtimestamp(item.get("time", 0), tz=timezone.utc) if item.get("time") else None,
                    "hn_score": item.get("score", 0),
                    "hn_comments": item.get("descendants", 0),
                })
        return stories
    except Exception as e:
        log.warning("[%s] fetch failed: %s", src["name"], e)
        return []


def fetch_rss(src):
    try:
        r = requests.get(src["url"], timeout=FETCH_TIMEOUT, headers={"User-Agent": UA})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}
        items = root.findall(".//item")
        is_atom = False
        if not items:
            items = root.findall(".//atom:entry", ns)
            is_atom = True

        stories = []
        for item in items[: src["max"]]:
            if is_atom:
                title = item.findtext("atom:title", "", ns)
                link_el = item.find("atom:link", ns)
                link = link_el.get("href") if link_el is not None else ""
                desc = item.findtext("atom:summary", "", ns) or item.findtext("atom:content", "", ns)
                pub = item.findtext("atom:updated", "", ns) or item.findtext("atom:published", "", ns)
            else:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                desc = item.findtext("description", "") or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded", "")
                pub = item.findtext("pubDate", "") or item.findtext("dc:date", "", ns)

            title = _strip_html(title)[:200]
            link = (link or "").strip()
            desc = _strip_html(desc)[:400]
            if not title:
                continue
            stories.append({
                "title": title,
                "url": link,
                "description": desc,
                "source": src["name"],
                "source_weight": src["weight"],
                "default_cat": src["cat"],
                "published": _parse_date(pub),
                "hn_score": 0,
                "hn_comments": 0,
            })
        return stories
    except Exception as e:
        log.warning("[%s] fetch failed: %s", src["name"], e)
        return []


def fetch_gh_trending(src):
    """Scrape github.com/trending HTML (no official API)."""
    try:
        r = requests.get(src["url"], timeout=FETCH_TIMEOUT, headers={"User-Agent": UA})
        r.raise_for_status()
        html_text = r.text
        # Each repo is in <article class="Box-row">
        articles = re.findall(r'<article class="Box-row">(.*?)</article>', html_text, re.DOTALL)
        stories = []
        for art in articles[: src["max"]]:
            m = re.search(r'<a[^>]+href="/([^"]+)"[^>]*>(.*?)</a>', art, re.DOTALL)
            if not m:
                continue
            repo = m.group(1).strip()
            desc_m = re.search(r'<p[^>]*class="col-9[^"]*"[^>]*>(.*?)</p>', art, re.DOTALL)
            desc = _strip_html(desc_m.group(1)) if desc_m else ""
            stars_m = re.search(r'(\d[\d,]*)\s*stars today', art)
            stars = stars_m.group(1) if stars_m else ""
            title = f"🔥 {repo}" + (f" (+{stars} ⭐今日)" if stars else "")
            stories.append({
                "title": title,
                "url": f"https://github.com/{repo}",
                "description": desc[:300],
                "source": src["name"],
                "source_weight": src["weight"],
                "default_cat": src["cat"],
                "published": datetime.now(timezone.utc),  # trending = very fresh
                "hn_score": 0,
                "hn_comments": 0,
            })
        return stories
    except Exception as e:
        log.warning("[%s] fetch failed: %s", src["name"], e)
        return []


FETCHERS = {"hn_api": fetch_hn, "rss": fetch_rss, "gh_trending": fetch_gh_trending}


def fetch_all():
    all_stories = []
    stats = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(FETCHERS[s["type"]], s): s for s in SOURCES}
        for fut in as_completed(futures):
            src = futures[fut]
            items = fut.result() or []
            stats[src["name"]] = len(items)
            all_stories.extend(items)
    log.info("Fetch summary: %s", {k: v for k, v in sorted(stats.items(), key=lambda x: -x[1])})
    log.info("Total raw items: %d", len(all_stories))
    return all_stories


# ═══════════════════════════════════════════════════════════════════
#  CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════

FINANCE_KW = re.compile(
    r"\b(stock|market|econom|bank|fed|rate|inflation|gdp|trade|tariff|crypto|bitcoin|ethereum|invest|earning|revenue|ipo|yield|bond|treasury|dollar|oil|gold)\b"
    r"|金融|股|融资|经济|美联储|降息|加息|关税|通胀|利率|央行|汇率|黄金|原油|A股|港股|美股",
    re.IGNORECASE,
)
AI_KW = re.compile(
    r"\bAI\b|artificial.intelligence|\bLLM\b|\bGPT\b|Claude|Gemini|neural|"
    r"machine.learning|deep.learning|transformer|\bAGI\b|OpenAI|Anthropic|DeepMind|Mistral|Llama|"
    r"大模型|人工智能|智能体|算力|推理|多模态|扩散模型",
    re.IGNORECASE,
)


def classify(item):
    text = (item["title"] + " " + (item.get("description") or ""))
    if AI_KW.search(text):
        return "ai"
    if FINANCE_KW.search(text):
        return "finance"
    return item.get("default_cat", "tech")


# ═══════════════════════════════════════════════════════════════════
#  SCORING & DEDUP
# ═══════════════════════════════════════════════════════════════════

CATEGORY_BOOST = {"ai": 1.2, "finance": 1.1, "tech": 1.0, "world": 0.9}

HOT_KW = re.compile(
    r"OpenAI|Anthropic|NVIDIA|英伟达|Apple|苹果|Google|Microsoft|Meta|Tesla|"
    r"Fed|美联储|降息|加息|关税|tariff|launch|release|发布|breach|leak|outage|"
    r"GPT-?\d|Claude|Gemini|breakthrough|record|acquisition|收购|IPO",
    re.IGNORECASE,
)

STOPWORDS = {"the","a","an","of","to","in","on","for","and","or","with","is","are","be","by","as","at","from","that","this","it","its","new","has","have","will","was"}


def _tokens_en(s):
    toks = re.findall(r"[a-zA-Z][a-zA-Z0-9]{2,}", s.lower())
    return {t for t in toks if t not in STOPWORDS}


def _bigrams_zh(s):
    chars = re.findall(r"[\u4e00-\u9fff]", s)
    return {"".join(chars[i:i+2]) for i in range(len(chars)-1)}


def similarity(a, b):
    """Hybrid title similarity: English Jaccard ∪ Chinese bigram Jaccard."""
    ea, eb = _tokens_en(a), _tokens_en(b)
    za, zb = _bigrams_zh(a), _bigrams_zh(b)
    ua, ub = ea | za, eb | zb
    if not ua or not ub:
        return 0.0
    return len(ua & ub) / len(ua | ub)


def base_score(item):
    # 1. source weight
    w = item["source_weight"]
    # 2. freshness (24h half-life, capped)
    if item.get("published"):
        hours = max(0, (datetime.now(timezone.utc) - item["published"]).total_seconds() / 3600)
        freshness = math.exp(-hours / 24)
    else:
        freshness = 0.5  # unknown date → mild penalty
    # 3. engagement (HN only for now)
    eng = 1.0
    if item.get("hn_score", 0) > 0:
        eng = 1.0 + math.log10(1 + item["hn_score"] + 2 * item.get("hn_comments", 0)) / 2
    # 4. keyword boost
    text = item["title"] + " " + item.get("description", "")
    kw_bonus = 0.3 if HOT_KW.search(text) else 0.0
    return w * freshness * eng + kw_bonus


def dedup_and_score(items):
    """Cluster similar items by title similarity. Merge sources, sum bonus."""
    clusters = []
    for it in items:
        best_i, best_sim = -1, 0.0
        for i, c in enumerate(clusters):
            sim = similarity(it["title"], c["title"])
            if sim > best_sim:
                best_sim, best_i = sim, i
        if best_sim >= 0.5 and best_i >= 0:
            # merge into existing cluster
            c = clusters[best_i]
            c["sources"].append({"name": it["source"], "url": it["url"]})
            # keep highest-weighted representative
            if it["source_weight"] > c["source_weight"]:
                c["title"] = it["title"]
                c["url"] = it["url"]
                c["description"] = it.get("description") or c["description"]
                c["source_weight"] = it["source_weight"]
            elif not c["description"] and it.get("description"):
                c["description"] = it["description"]
            # use freshest date
            if it.get("published") and (not c.get("published") or it["published"] > c["published"]):
                c["published"] = it["published"]
            # accumulate HN engagement
            c["hn_score"] = max(c["hn_score"], it.get("hn_score", 0))
            c["hn_comments"] = max(c["hn_comments"], it.get("hn_comments", 0))
        else:
            clusters.append({
                "title": it["title"],
                "url": it["url"],
                "description": it.get("description", ""),
                "source_weight": it["source_weight"],
                "default_cat": it["default_cat"],
                "published": it.get("published"),
                "hn_score": it.get("hn_score", 0),
                "hn_comments": it.get("hn_comments", 0),
                "sources": [{"name": it["source"], "url": it["url"]}],
            })

    # score each cluster
    for c in clusters:
        # classify based on merged text
        c["category"] = classify({"title": c["title"], "description": c["description"], "default_cat": c["default_cat"]})
        base = base_score({
            "source_weight": c["source_weight"],
            "published": c["published"],
            "hn_score": c["hn_score"],
            "hn_comments": c["hn_comments"],
            "title": c["title"],
            "description": c["description"],
        })
        n_sources = len({s["name"] for s in c["sources"]})
        cross_bonus = 0.6 * (n_sources - 1)  # ⭐ 关键：交叉验证奖励
        cat_mul = CATEGORY_BOOST.get(c["category"], 1.0)
        c["score"] = (base + cross_bonus) * cat_mul
        c["n_sources"] = n_sources

    clusters.sort(key=lambda x: -x["score"])
    return clusters


# ═══════════════════════════════════════════════════════════════════
#  LLM SUMMARIZATION
# ═══════════════════════════════════════════════════════════════════

def build_digest_input(clusters, per_cat=(("finance", 4), ("ai", 5), ("tech", 4), ("world", 2))):
    """Pick top-N per category and format for LLM."""
    selected = []
    parts = []
    for cat_key, n in per_cat:
        cat_items = [c for c in clusters if c["category"] == cat_key][:n]
        if not cat_items:
            continue
        cat_name = {"finance": "Finance", "ai": "AI", "tech": "Tech", "world": "World"}[cat_key]
        parts.append(f"\n## {cat_name} News\n")
        for i, c in enumerate(cat_items, 1):
            srcs = " · ".join(sorted({s["name"] for s in c["sources"]}))
            parts.append(
                f"{i}. 【score={c['score']:.2f} · {c['n_sources']}源】{c['title']}\n"
                f"   来源: {srcs}\n"
                f"   {c['description'][:250]}\n"
            )
            selected.append(c)
    return "".join(parts), selected


def generate_summary(clusters):
    now = datetime.now(BJT)
    period = "早间" if now.hour < 12 else "晚间"
    date_str = now.strftime("%Y-%m-%d")

    news_text, selected = build_digest_input(clusters)
    if not selected:
        log.error("No items selected for digest")
        return None, []

    # Build a source-list hint string per item so LLM can embed it verbatim
    src_hint_lines = []
    for i, c in enumerate(selected, 1):
        srcs = " · ".join(sorted({s["name"] for s in c["sources"]}))
        tag = "🔥多源" if c["n_sources"] >= 3 else ("⭐交叉" if c["n_sources"] == 2 else "")
        src_hint_lines.append(f"{i}. {tag} 📎{srcs}")
    src_hint = "\n".join(src_hint_lines)

    prompt = (
        "你是一个专业的新闻编辑。根据下列已按权重排序+交叉验证的新闻素材，生成中文速递。\n\n"
        f"时段: {period}\n日期: {date_str}\n\n"
        f"素材（已按 score 降序）:\n{news_text}\n\n"
        "每条素材已标注其来源列表。请在每条摘要后**原样保留来源行**。\n\n"
        "输出严格遵循此纯文本格式（不得使用 Markdown，不要 ** # 等符号）:\n\n"
        f"📰 {period}新闻速递 | {date_str}\n\n"
        "━━━ 💰 金融市场 ━━━\n\n"
        "1️⃣ 【不超过15字的标题】\n"
        "2-3句中文摘要，概括核心信息和影响。保留关键数字。\n"
        "📎 来源1 · 来源2 · 来源3\n\n"
        "2️⃣ 【标题】\n摘要。\n📎 来源列表\n\n"
        "━━━ 🤖 AI 前沿 ━━━\n\n"
        "（同上格式）\n\n"
        "━━━ 🔬 科技动态 ━━━\n\n"
        "（同上格式）\n\n"
        "━━━ 🌍 世界要闻 ━━━（可选，如无则省略）\n\n"
        f"📊 本期覆盖多源交叉验证 | 共 {len(selected)} 条精选\n\n"
        "严格要求:\n"
        "- 从素材中挑选 5-9 条最有价值的新闻（优先高 score 和多源交叉的）\n"
        "- 金融 1-3 条 / AI 2-4 条 / 科技 1-3 条 / 世界 0-2 条\n"
        "- 每条摘要 2-3 句话（50-120 字），标题不超过 15 字\n"
        "- 📎 来源行必须原样保留素材中给出的来源列表\n"
        "- 全文控制在 2500 字符以内\n"
        "- 使用简洁中文，避免翻译腔，保留关键数据\n"
        "- 不要使用 Markdown 语法，不要编造原文之外的信息\n"
        "- 直接输出速递，不要前言或解释\n\n"
        "补充提示（每条对应的来源标签）:\n"
        f"{src_hint}\n"
    )

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
            timeout=180,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        log.info("LLM summary generated: %d chars", len(content))
        return content, selected
    except Exception as e:
        log.error("LLM summarization failed: %s", e)
        return None, selected


# ═══════════════════════════════════════════════════════════════════
#  WECHAT SENDING
# ═══════════════════════════════════════════════════════════════════

def send_to_wechat(text):
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
    log.info("=" * 60)
    log.info("Daily News Digest v2 starting...")
    now = datetime.now(BJT)
    log.info("Time: %s BJT", now.strftime("%Y-%m-%d %H:%M:%S"))

    # 1. Fetch
    log.info("Step 1/4: Fetching from %d sources (parallel)...", len(SOURCES))
    raw = fetch_all()
    if not raw:
        log.error("No items from any source. Aborting.")
        sys.exit(1)

    # 2. Classify
    log.info("Step 2/4: Classifying & scoring...")
    for it in raw:
        it["_cat"] = classify(it)

    # 3. Dedup + score
    clusters = dedup_and_score(raw)
    by_cat = {}
    for c in clusters:
        by_cat.setdefault(c["category"], []).append(c)
    log.info("After dedup: %d clusters (from %d raw items)", len(clusters), len(raw))
    for cat, items in by_cat.items():
        top = items[:3]
        log.info("  %s: %d items | top: %s",
                 cat, len(items),
                 " / ".join(f"{t['title'][:30]}(s={t['score']:.2f},n={t['n_sources']})" for t in top))

    # 4. Summarize
    log.info("Step 3/4: LLM summarization...")
    summary, selected = generate_summary(clusters)
    if not summary:
        log.error("Summary generation failed. Aborting.")
        sys.exit(1)
    log.info("Summary %d chars | selected %d items", len(summary), len(selected))
    log.info("--- preview ---\n%s\n--- end ---", summary[:600])

    # 5. Send
    log.info("Step 4/4: Sending to WeChat...")
    ok = send_to_wechat(summary)
    if ok:
        log.info("✅ Digest sent successfully")
    else:
        log.error("❌ Failed to send digest")
        sys.exit(1)
    log.info("=" * 60)


if __name__ == "__main__":
    main()

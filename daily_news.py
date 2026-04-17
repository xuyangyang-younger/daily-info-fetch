#!/usr/bin/env python3
"""
Daily News Digest — 每日新闻多源抓取 + 权重排序 + 中文摘要推送

Pipeline:
  1. 并行抓取 29 个信息源（HN Algolia / RSS / GitHub Trending HTML），带 wall-clock 预算
  2. 自动分类（finance / ai / tech / world）
  3. 倒排索引去重 + URL 归一化；跨会话过滤最近 36h 已发条目
  4. 打分（源权重 × 新鲜度 × 热度[HN 有 cap] + family 交叉奖励 + 关键词）
  5. 每板块 top-N 送入 Hermes LLM 生成中文速递（硬截断 3500 字符）
  6. 通过 Hermes send_weixin_direct 推送微信，解析 iLink ret 检测静默失败
"""

import json
import os
import re
import sys
import math
import logging
import logging.handlers
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import xml.etree.ElementTree as ET
import html
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# Hermes venv import path (server-side only; harmless if missing locally)
_HERMES_AGENT_PATH = os.getenv("HERMES_AGENT_PATH", "/root/.hermes/hermes-agent")
if os.path.isdir(_HERMES_AGENT_PATH):
    sys.path.insert(0, _HERMES_AGENT_PATH)

# ── Logging ────────────────────────────────────────────────────────
LOG_DIR = os.path.expanduser(os.getenv("DAILY_NEWS_LOG_DIR", "~/daily-news-digest/logs"))
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, "daily_news.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────
HERMES_API_URL = os.getenv("HERMES_API_URL", "http://localhost:8000/v1/chat/completions")
HERMES_API_KEY = os.getenv("HERMES_API_KEY")

WEIXIN_TOKEN = os.getenv("WEIXIN_TOKEN")
WEIXIN_CHAT_ID = os.getenv("WEIXIN_CHAT_ID")
WEIXIN_ACCOUNT_ID = os.getenv("WEIXIN_ACCOUNT_ID")

FETCH_TIMEOUT = int(os.getenv("DAILY_NEWS_FETCH_TIMEOUT", "10"))
FETCH_WALL_BUDGET = int(os.getenv("DAILY_NEWS_FETCH_BUDGET", "20"))
BJT = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (compatible; DailyNewsBot/2.0)"
STATE_DIR = os.path.expanduser(os.getenv("DAILY_NEWS_STATE_DIR", "~/daily-news-digest/state"))

# ── Shared HTTP Session ────────────────────────────────────────────
# Connection pooling + automatic retry with exponential backoff.
# All fetchers MUST go through HTTP (not requests.*) to benefit.
def _build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": UA})
    return s


HTTP = _build_session()


def _require_env(name: str, value):
    if not value:
        log.error("Missing required env var: %s (see .env.example)", name)
        sys.exit(2)

# ═══════════════════════════════════════════════════════════════════
#  SOURCE REGISTRY
#  weight: 源权重 (0.7 ~ 1.5)
#  cat:    默认板块归属 (仍会被关键词分类覆盖)
#  type:   hn_api / rss / gh_trending
# ═══════════════════════════════════════════════════════════════════
# `family` 用于跨源交叉验证奖励：同 family 的多条不算"独立交叉源"
# （例如 WSJ Markets + WSJ Business 属 family=wsj → 算 1 个交叉源）
SOURCES = [
    # ─ Finance ─────────────────────────────────────
    {"name": "WSJ Markets",       "family": "wsj",     "type": "rss",   "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",                "weight": 1.2, "cat": "finance", "max": 8},
    {"name": "WSJ Business",      "family": "wsj",     "type": "rss",   "url": "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",              "weight": 1.2, "cat": "finance", "max": 8},
    {"name": "CNBC Top",          "family": "cnbc",    "type": "rss",   "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",         "weight": 1.1, "cat": "finance", "max": 10},
    {"name": "CNBC Finance",      "family": "cnbc",    "type": "rss",   "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html",          "weight": 1.1, "cat": "finance", "max": 10},
    {"name": "MarketWatch",       "family": "marketwatch", "type": "rss", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories", "weight": 1.0, "cat": "finance", "max": 8},
    {"name": "Investing CN",      "family": "investing", "type": "rss", "url": "https://cn.investing.com/rss/news_285.rss",                     "weight": 0.9, "cat": "finance", "max": 8},
    {"name": "华尔街见闻",         "family": "wallstreetcn", "type": "rss", "url": "https://dedicated.wallstreetcn.com/rss.xml",                "weight": 1.1, "cat": "finance", "max": 15},

    # ─ AI (一手源权重最高) ──────────────────────────
    {"name": "OpenAI Blog",       "family": "openai",  "type": "rss",   "url": "https://openai.com/blog/rss.xml",                               "weight": 1.5, "cat": "ai",      "max": 5},
    {"name": "Google AI Blog",    "family": "google",  "type": "rss",   "url": "https://blog.google/technology/ai/rss/",                        "weight": 1.4, "cat": "ai",      "max": 5},
    {"name": "DeepMind",          "family": "google",  "type": "rss",   "url": "https://deepmind.google/blog/rss.xml",                          "weight": 1.4, "cat": "ai",      "max": 5},
    {"name": "MIT Tech Review AI","family": "mitreview","type": "rss",  "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed", "weight": 1.2, "cat": "ai", "max": 5},
    {"name": "VentureBeat AI",    "family": "venturebeat", "type": "rss", "url": "https://venturebeat.com/category/ai/feed/",                   "weight": 1.1, "cat": "ai",      "max": 7},
    {"name": "Simon Willison",    "family": "simonw",  "type": "rss",   "url": "https://simonwillison.net/atom/everything/",                    "weight": 1.2, "cat": "ai",      "max": 6},
    {"name": "arXiv cs.AI",       "family": "arxiv",   "type": "rss",   "url": "http://export.arxiv.org/rss/cs.AI",                             "weight": 1.1, "cat": "ai",      "max": 6},
    {"name": "arXiv cs.LG",       "family": "arxiv",   "type": "rss",   "url": "http://export.arxiv.org/rss/cs.LG",                             "weight": 1.0, "cat": "ai",      "max": 4},
    {"name": "arXiv cs.CL",       "family": "arxiv",   "type": "rss",   "url": "http://export.arxiv.org/rss/cs.CL",                             "weight": 1.0, "cat": "ai",      "max": 4},
    {"name": "量子位",             "family": "qbitai",  "type": "rss",   "url": "https://www.qbitai.com/feed",                                   "weight": 1.0, "cat": "ai",      "max": 8},

    # ─ Tech ────────────────────────────────────────
    {"name": "Hacker News",       "family": "hn",      "type": "hn_api","url": "",                                                              "weight": 1.0, "cat": "tech",    "max": 20},
    {"name": "TechCrunch",        "family": "techcrunch", "type": "rss", "url": "https://techcrunch.com/feed/",                                "weight": 1.0, "cat": "tech",    "max": 10},
    {"name": "The Verge",         "family": "verge",   "type": "rss",   "url": "https://www.theverge.com/rss/index.xml",                        "weight": 1.0, "cat": "tech",    "max": 10},
    {"name": "Ars Technica",      "family": "ars",     "type": "rss",   "url": "https://feeds.arstechnica.com/arstechnica/index",               "weight": 1.0, "cat": "tech",    "max": 10},
    {"name": "Wired",             "family": "wired",   "type": "rss",   "url": "https://www.wired.com/feed/rss",                                "weight": 1.0, "cat": "tech",    "max": 10},
    {"name": "VentureBeat",       "family": "venturebeat", "type": "rss", "url": "https://venturebeat.com/feed/",                               "weight": 1.0, "cat": "tech",    "max": 7},
    {"name": "Stratechery",       "family": "stratechery", "type": "rss", "url": "https://stratechery.com/feed/",                               "weight": 1.2, "cat": "tech",    "max": 5},
    {"name": "InfoQ",             "family": "infoq",   "type": "rss",   "url": "https://feed.infoq.com/",                                       "weight": 0.9, "cat": "tech",    "max": 8},
    {"name": "Hacker Noon",       "family": "hackernoon", "type": "rss", "url": "https://hackernoon.com/feed",                                  "weight": 0.8, "cat": "tech",    "max": 8},
    {"name": "TechNode",          "family": "technode","type": "rss",   "url": "https://technode.com/feed/",                                    "weight": 0.9, "cat": "tech",    "max": 8},
    {"name": "36Kr",              "family": "36kr",    "type": "rss",   "url": "https://36kr.com/feed",                                         "weight": 1.0, "cat": "tech",    "max": 10},
    {"name": "爱范儿",             "family": "ifanr",   "type": "rss",   "url": "https://www.ifanr.com/feed",                                    "weight": 0.9, "cat": "tech",    "max": 8},
    {"name": "钛媒体",             "family": "tmtpost", "type": "rss",   "url": "https://www.tmtpost.com/rss.xml",                               "weight": 0.9, "cat": "tech",    "max": 8},
    {"name": "雷锋网",             "family": "leiphone","type": "rss",   "url": "https://www.leiphone.com/feed",                                 "weight": 0.9, "cat": "tech",    "max": 8},
    {"name": "Solidot",           "family": "solidot", "type": "rss",   "url": "https://www.solidot.org/index.rss",                             "weight": 0.9, "cat": "tech",    "max": 10},
    {"name": "少数派",             "family": "sspai",   "type": "rss",   "url": "https://sspai.com/feed",                                        "weight": 0.8, "cat": "tech",    "max": 6},
    {"name": "GitHub Trending",   "family": "github",  "type": "gh_trending", "url": "https://github.com/trending?since=daily",                 "weight": 0.9, "cat": "tech",    "max": 8},
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
    """HN via Algolia front-page API — 一次请求拿全 top N，避免 1+N 次 Firebase 往返。"""
    try:
        r = HTTP.get(
            "https://hn.algolia.com/api/v1/search",
            params={"tags": "front_page", "hitsPerPage": src["max"]},
            timeout=FETCH_TIMEOUT,
        )
        r.raise_for_status()
        hits = r.json().get("hits", [])
        stories = []
        for item in hits:
            title = item.get("title") or item.get("story_title")
            if not title:
                continue
            obj_id = item.get("objectID") or item.get("story_id") or ""
            url = item.get("url") or item.get("story_url") or f"https://news.ycombinator.com/item?id={obj_id}"
            pub = None
            ts = item.get("created_at_i")
            if ts:
                pub = datetime.fromtimestamp(ts, tz=timezone.utc)
            stories.append({
                "title": title,
                "url": url,
                "description": "",
                "source": src["name"],
                "source_family": src["family"],
                "source_weight": src["weight"],
                "default_cat": src["cat"],
                "published": pub,
                "hn_score": item.get("points", 0) or 0,
                "hn_comments": item.get("num_comments", 0) or 0,
            })
        return stories
    except Exception as e:
        log.warning("[%s] fetch failed: %s", src["name"], e)
        return []


def fetch_rss(src):
    try:
        r = HTTP.get(src["url"], timeout=FETCH_TIMEOUT)
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
                "source_family": src["family"],
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
        r = HTTP.get(src["url"], timeout=FETCH_TIMEOUT)
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
                "source_family": src["family"],
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
    """并行抓取所有源；整体 wall-clock 预算 FETCH_WALL_BUDGET 秒，超时的源丢弃。"""
    all_stories = []
    stats = {}
    timed_out = []
    # IO-bound: 每源一线程，max_workers=len(SOURCES) 几乎零额外代价
    with ThreadPoolExecutor(max_workers=max(len(SOURCES), 1)) as ex:
        futures = {ex.submit(FETCHERS[s["type"]], s): s for s in SOURCES}
        try:
            for fut in as_completed(futures, timeout=FETCH_WALL_BUDGET):
                src = futures[fut]
                items = fut.result() or []
                stats[src["name"]] = len(items)
                all_stories.extend(items)
        except FutureTimeout:
            pass
        # 处理未完成的 future：记录慢源，取消（网络层 cancel 不可靠，但计数准确）
        for fut, src in futures.items():
            if not fut.done():
                timed_out.append(src["name"])
                fut.cancel()

    if timed_out:
        log.warning("Fetch timed out (%ds budget) for sources: %s",
                    FETCH_WALL_BUDGET, ", ".join(timed_out))
    log.info("Fetch summary: %s", {k: v for k, v in sorted(stats.items(), key=lambda x: -x[1])})
    log.info("Total raw items: %d (completed %d/%d sources)",
             len(all_stories), len(stats), len(SOURCES))
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
WORLD_KW = re.compile(
    r"\b(election|president|parliament|senate|war|conflict|strike|protest|sanction|"
    r"treaty|UN|EU|NATO|Ukraine|Russia|Israel|Gaza|Iran|Korea|Taiwan)\b"
    r"|大选|总统|战争|冲突|制裁|条约|联合国|欧盟|北约|乌克兰|俄罗斯|以色列|加沙|伊朗|朝鲜|台海",
    re.IGNORECASE,
)


def classify(item):
    text = (item["title"] + " " + (item.get("description") or ""))
    if AI_KW.search(text):
        return "ai"
    if FINANCE_KW.search(text):
        return "finance"
    if WORLD_KW.search(text):
        return "world"
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

# dedup 门槛
SIM_THRESHOLD = 0.55           # Jaccard 相似度合并阈值
MIN_SHARED_TOKENS = 2          # 必须至少 N 个共同 token 才可能合并（抗单词误命中）
HN_ENG_CAP = 2.0               # HN engagement 乘数上限（避免 HN 屠榜）


def _tokens_en(s):
    toks = re.findall(r"[a-zA-Z][a-zA-Z0-9]{2,}", s.lower())
    return {t for t in toks if t not in STOPWORDS}


def _bigrams_zh(s):
    chars = re.findall(r"[\u4e00-\u9fff]", s)
    return {"".join(chars[i:i+2]) for i in range(len(chars)-1)}


def _title_tokens(title: str):
    """返回标题的合并 token 集合（英文 token ∪ 中文 bigram），用于倒排索引 & 相似度。"""
    return _tokens_en(title) | _bigrams_zh(title)


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = a & b
    if not inter:
        return 0.0
    return len(inter) / len(a | b)


_TRACK_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref|spm$)", re.IGNORECASE)


def normalize_url(url: str) -> str:
    """归一化 URL 用于精确去重：小写 host、去除追踪参数、去 fragment、去末尾斜杠。"""
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host.startswith("m."):
            host = host[2:]
        netloc = host
        if parts.port:
            netloc = f"{host}:{parts.port}"
        query = urlencode([
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACK_PARAMS.match(k)
        ])
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower() or "https", netloc, path, query, ""))
    except Exception:
        return url.strip()


# 相似度保留旧名称以防外部引用（未来移除兼容层可考虑直接用 _jaccard）
def similarity(a, b):
    return _jaccard(_title_tokens(a), _title_tokens(b))


def base_score(item):
    # 1. source weight
    w = item["source_weight"]
    # 2. freshness (24h half-life, capped)
    if item.get("published"):
        hours = max(0, (datetime.now(timezone.utc) - item["published"]).total_seconds() / 3600)
        freshness = math.exp(-hours / 24)
    else:
        freshness = 0.5  # unknown date → mild penalty
    # 3. engagement (HN only) — 加 cap 防止屠榜
    eng = 1.0
    if item.get("hn_score", 0) > 0:
        eng = min(HN_ENG_CAP, 1.0 + math.log10(1 + item["hn_score"] + 2 * item.get("hn_comments", 0)) / 2)
    # 4. keyword boost
    text = item["title"] + " " + item.get("description", "")
    kw_bonus = 0.3 if HOT_KW.search(text) else 0.0
    return w * freshness * eng + kw_bonus


def _precompute_tokens(items):
    """为每个 item 预计算 title_tokens 和 normalized_url（后续 dedup 直接复用）。"""
    for it in items:
        it["_tokens"] = _title_tokens(it["title"])
        it["_nurl"] = normalize_url(it.get("url", ""))


def dedup_and_score(items):
    """
    倒排索引 + 两阶段合并:
      1) normalized_url 精确命中 → 直接合并（同文章在不同源的同一 link）
      2) 标题 Jaccard ≥ SIM_THRESHOLD 且共享 token ≥ MIN_SHARED_TOKENS → 聚类合并
    代表标题一经确立不再漂移（仅在更高权重源出现时替换描述，不替换 title）。
    """
    _precompute_tokens(items)

    clusters = []
    token_index: dict = {}   # token -> list[cluster idx]
    url_index: dict = {}     # normalized_url -> cluster idx

    def _new_cluster(it) -> int:
        c = {
            "title": it["title"],                    # 代表标题：首次命中后固定
            "url": it["url"],
            "description": it.get("description", ""),
            "source_weight": it["source_weight"],
            "default_cat": it["default_cat"],
            "published": it.get("published"),
            "hn_score": it.get("hn_score", 0),
            "hn_comments": it.get("hn_comments", 0),
            "sources": [{"name": it["source"], "family": it.get("source_family", it["source"]), "url": it["url"]}],
            "_tokens": set(it["_tokens"]),
        }
        clusters.append(c)
        idx = len(clusters) - 1
        for tok in it["_tokens"]:
            token_index.setdefault(tok, []).append(idx)
        if it["_nurl"]:
            url_index[it["_nurl"]] = idx
        return idx

    def _merge_into(idx: int, it):
        c = clusters[idx]
        c["sources"].append({"name": it["source"], "family": it.get("source_family", it["source"]), "url": it["url"]})
        # 代表 title 不变；仅当描述更饱满时替换，并在更高权重源出现时用其描述
        if it["source_weight"] > c["source_weight"]:
            if it.get("description"):
                c["description"] = it["description"]
            c["source_weight"] = it["source_weight"]
        elif not c["description"] and it.get("description"):
            c["description"] = it["description"]
        if it.get("published") and (not c.get("published") or it["published"] > c["published"]):
            c["published"] = it["published"]
        c["hn_score"] = max(c["hn_score"], it.get("hn_score", 0))
        c["hn_comments"] = max(c["hn_comments"], it.get("hn_comments", 0))
        # 倒排索引增量更新（并入的 token 也要可被后续条目检索到）
        for tok in it["_tokens"] - c["_tokens"]:
            token_index.setdefault(tok, []).append(idx)
        c["_tokens"] |= it["_tokens"]

    for it in items:
        # Stage 1: URL 精确命中
        if it["_nurl"] and it["_nurl"] in url_index:
            _merge_into(url_index[it["_nurl"]], it)
            continue

        # Stage 2: 倒排索引取候选 → 相似度比对
        toks = it["_tokens"]
        if not toks:
            _new_cluster(it)
            continue
        candidate_counts: dict = {}
        for tok in toks:
            for ci in token_index.get(tok, ()):
                candidate_counts[ci] = candidate_counts.get(ci, 0) + 1

        best_i, best_sim = -1, 0.0
        for ci, shared in candidate_counts.items():
            if shared < MIN_SHARED_TOKENS:
                continue
            sim = _jaccard(toks, clusters[ci]["_tokens"])
            if sim > best_sim:
                best_sim, best_i = sim, ci

        if best_i >= 0 and best_sim >= SIM_THRESHOLD:
            _merge_into(best_i, it)
            if it["_nurl"]:
                url_index.setdefault(it["_nurl"], best_i)
        else:
            _new_cluster(it)

    # 打分
    for c in clusters:
        c["category"] = classify({"title": c["title"], "description": c["description"], "default_cat": c["default_cat"]})
        base = base_score({
            "source_weight": c["source_weight"],
            "published": c["published"],
            "hn_score": c["hn_score"],
            "hn_comments": c["hn_comments"],
            "title": c["title"],
            "description": c["description"],
        })
        # cross-source bonus: 按 family 去重（WSJ Markets+Business 算同一家）
        families = {s.get("family", s["name"]) for s in c["sources"]}
        n_families = len(families)
        n_sources = len({s["name"] for s in c["sources"]})
        cross_bonus = 0.6 * (n_families - 1)
        cat_mul = CATEGORY_BOOST.get(c["category"], 1.0)
        c["score"] = (base + cross_bonus) * cat_mul
        c["n_sources"] = n_sources
        c["n_families"] = n_families
        c.pop("_tokens", None)

    clusters.sort(key=lambda x: -x["score"])
    return clusters


# ═══════════════════════════════════════════════════════════════════
#  LLM SUMMARIZATION
# ═══════════════════════════════════════════════════════════════════

def build_digest_input(clusters, per_cat=(("finance", 7), ("ai", 8), ("tech", 7))):
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
        f"📊 本期覆盖多源交叉验证 | 共 {len(selected)} 条精选\n\n"
        "严格要求:\n"
        "- 从素材中挑选 6-10 条最有价值的新闻（优先高 score 和多源交叉的）\n"
        "- 金融 2-3 条 / AI 2-4 条 / 科技 2-3 条（若某板块素材不足可减少）\n"
        "- 每条摘要 2-3 句话（60-120 字），标题不超过 15 字\n"
        "- 📎 来源行必须原样保留素材中给出的来源列表\n"
        "- 全文控制在 3500 字符以内\n"
        "- 使用简洁中文，避免翻译腔，保留关键数据\n"
        "- 不要使用 Markdown 语法，不要编造原文之外的信息\n"
        "- 直接输出速递，不要前言或解释\n\n"
        "补充提示（每条对应的来源标签）:\n"
        f"{src_hint}\n"
    )

    try:
        r = HTTP.post(
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
        # 硬截断保护：微信侧长消息可能被 iLink 静默截断
        if len(content) > 3500:
            log.warning("LLM output %d chars exceeds 3500 limit; truncating", len(content))
            content = content[:3497] + "..."
        log.info("LLM summary generated: %d chars", len(content))
        return content, selected
    except Exception as e:
        log.error("LLM summarization failed: %s", e)
        return None, selected


# ═══════════════════════════════════════════════════════════════════
#  CROSS-SESSION DEDUP (persisted "already-sent" state)
# ═══════════════════════════════════════════════════════════════════

SENT_STATE_PATH = os.path.join(STATE_DIR, "sent_titles.json")
SENT_RETENTION_HOURS = 36
SENT_SIM_THRESHOLD = 0.6       # 跨会话去重更严格（避免误杀）


def _title_hash(title: str) -> str:
    import hashlib
    return hashlib.sha1(title.strip().lower().encode("utf-8")).hexdigest()[:16]


def load_sent_history():
    """Return list of {hash, title, tokens, ts_iso}. 丢弃超过保留窗口的条目。"""
    try:
        with open(SENT_STATE_PATH, "r", encoding="utf-8") as f:
            records = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=SENT_RETENTION_HOURS)
    fresh = []
    for rec in records:
        try:
            ts = datetime.fromisoformat(rec["ts"])
            if ts >= cutoff:
                rec["_tokens"] = set(rec.get("tokens", []))
                fresh.append(rec)
        except Exception:
            continue
    return fresh


def filter_recently_sent(clusters, history):
    """过滤与最近 36h 已发条目相似度 ≥ SENT_SIM_THRESHOLD 的 cluster。"""
    if not history:
        return clusters, 0
    kept = []
    skipped = 0
    for c in clusters:
        toks = _title_tokens(c["title"])
        hit = False
        for rec in history:
            if _jaccard(toks, rec["_tokens"]) >= SENT_SIM_THRESHOLD:
                hit = True
                break
        if hit:
            skipped += 1
        else:
            kept.append(c)
    return kept, skipped


def save_sent_history(selected, history):
    """把本次 selected 合并到历史，写回磁盘。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    records = [
        {"hash": r["hash"], "title": r["title"], "tokens": list(r["_tokens"]), "ts": r["ts"]}
        for r in history
    ]
    seen_hashes = {r["hash"] for r in records}
    for c in selected:
        h = _title_hash(c["title"])
        if h in seen_hashes:
            continue
        records.append({
            "hash": h,
            "title": c["title"],
            "tokens": list(_title_tokens(c["title"])),
            "ts": now_iso,
        })
    try:
        with open(SENT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        log.info("Sent history persisted: %d records → %s", len(records), SENT_STATE_PATH)
    except Exception as e:
        log.warning("Failed to persist sent history: %s", e)


# ═══════════════════════════════════════════════════════════════════
#  WECHAT SENDING
# ═══════════════════════════════════════════════════════════════════

def _scan_ret_code(obj):
    """递归查找响应中的 iLink ret 字段（HTTP 200 也可能 ret != 0 即静默失败）。"""
    if isinstance(obj, dict):
        if "ret" in obj and isinstance(obj["ret"], (int, float)):
            return int(obj["ret"])
        for v in obj.values():
            code = _scan_ret_code(v)
            if code is not None:
                return code
    elif isinstance(obj, list):
        for v in obj:
            code = _scan_ret_code(v)
            if code is not None:
                return code
    return None


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
        if not (isinstance(result, dict) and result.get("success")):
            return False
        ret_code = _scan_ret_code(result)
        if ret_code is not None and ret_code != 0:
            # 常见：-2 = context token 过期，消息静默丢弃。见 README "context token 刷新"章节
            log.error("iLink returned ret=%d (context token may be expired; "
                      "发条消息给 bot 账号可刷新 token)", ret_code)
            return False
        return True
    except Exception as e:
        log.error("WeChat send failed: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

def parse_args():
    import argparse
    p = argparse.ArgumentParser(description="Daily news digest fetcher")
    p.add_argument("--stdout", action="store_true",
                   help="仅生成摘要并打印到 stdout，跳过微信发送（供 Hermes agent 调用）")
    p.add_argument("--no-history", action="store_true",
                   help="禁用跨会话去重（调试用）")
    return p.parse_args()


def main():
    args = parse_args()
    log.info("=" * 60)
    log.info("Daily News Digest v2 starting (stdout=%s)", args.stdout)
    now = datetime.now(BJT)
    log.info("Time: %s BJT", now.strftime("%Y-%m-%d %H:%M:%S"))

    # 0. 必需凭据校验（--stdout 模式下可跳过微信相关）
    _require_env("HERMES_API_KEY", HERMES_API_KEY)
    if not args.stdout:
        _require_env("WEIXIN_TOKEN", WEIXIN_TOKEN)
        _require_env("WEIXIN_CHAT_ID", WEIXIN_CHAT_ID)
        _require_env("WEIXIN_ACCOUNT_ID", WEIXIN_ACCOUNT_ID)

    # 1. Fetch
    log.info("Step 1/4: Fetching from %d sources (parallel, budget=%ds)...",
             len(SOURCES), FETCH_WALL_BUDGET)
    raw = fetch_all()
    if not raw:
        log.error("No items from any source. Aborting.")
        sys.exit(1)

    # 2. Dedup + score
    log.info("Step 2/4: Classifying, dedup & scoring...")
    clusters = dedup_and_score(raw)

    # 2.5. 跨会话去重：过滤最近 36h 已发送的相似条目
    history = [] if args.no_history else load_sent_history()
    if history:
        clusters, skipped = filter_recently_sent(clusters, history)
        if skipped:
            log.info("Cross-session dedup: skipped %d recently-sent clusters", skipped)

    by_cat = {}
    for c in clusters:
        by_cat.setdefault(c["category"], []).append(c)
    log.info("After dedup: %d clusters (from %d raw items)", len(clusters), len(raw))
    for cat, items in by_cat.items():
        top = items[:3]
        log.info("  %s: %d items | top: %s",
                 cat, len(items),
                 " / ".join(f"{t['title'][:30]}(s={t['score']:.2f},n={t['n_sources']})" for t in top))

    # 3. Summarize
    log.info("Step 3/4: LLM summarization...")
    summary, selected = generate_summary(clusters)
    if not summary:
        log.error("Summary generation failed. Aborting.")
        sys.exit(1)
    log.info("Summary %d chars | selected %d items", len(summary), len(selected))
    log.info("--- preview ---\n%s\n--- end ---", summary[:600])

    # 4. Deliver
    if args.stdout:
        # 供 Hermes agent 读取后自行投递（见 README 中 cron job prompt）
        sys.stdout.write(summary)
        sys.stdout.flush()
        save_sent_history(selected, history)
        log.info("=" * 60)
        return

    log.info("Step 4/4: Sending to WeChat...")
    ok = send_to_wechat(summary)
    if ok:
        save_sent_history(selected, history)
        log.info("✅ Digest sent successfully")
    else:
        log.error("❌ Failed to send digest")
        sys.exit(1)
    log.info("=" * 60)


if __name__ == "__main__":
    main()

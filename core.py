from __future__ import annotations

import os
import re
import sys
import json
import time
import base64
import queue
import random
import sqlite3
import asyncio
import logging
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import requests
import feedparser

from openai import OpenAI

from telethon import TelegramClient
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# =========================================================
# APP PATH
# =========================================================

APP_NAME = "Mantle Social Publisher"
PROJECT_OWNER_WALLET = "0x152B5F1E58ACD5036D8d2027D3B793e81103E644"
PROJECT_DEMO_WALLETS = {PROJECT_OWNER_WALLET.lower()}
CREDIT_TOKEN_SYMBOL = "MFC"
CONTENT_LANGUAGES = ["English", "Vietnamese", "Indonesian", "Thai", "Chinese", "Korean", "Japanese", "Spanish", "French", "Portuguese", "Hindi"]

def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent

BASE_DIR = base_dir()
# Railway: mount a Volume at /data so sessions, SQLite DB, browser profiles, and config survive redeploys.
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", "/data" if Path("/data").exists() else str(BASE_DIR / "runtime")))
SESSION_DIR = RUNTIME_DIR / "sessions"
PROFILE_DIR = RUNTIME_DIR / "browser_profiles"
DB_PATH = RUNTIME_DIR / "app.db"
CONFIG_PATH = RUNTIME_DIR / "config.json"

for p in [RUNTIME_DIR, SESSION_DIR, PROFILE_DIR]:
    p.mkdir(parents=True, exist_ok=True)


# =========================================================
# RSS FEEDS
# =========================================================

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/reuters/worldNews",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
]

CATEGORY_KEYWORDS = {
    "Crypto": [
        "bitcoin", "btc", "crypto", "ethereum", "eth", "solana", "xrp",
        "binance", "coinbase", "stablecoin", "token", "defi", "blockchain",
        "etf", "spot bitcoin", "memecoin"
    ],
    "Stock": [
        "stock", "stocks", "nasdaq", "dow", "s&p", "market", "shares",
        "equity", "earnings", "wall street", "ipo"
    ],
    "Fed/CPI": [
        "fed", "federal reserve", "interest rate", "rate cut", "rate hike",
        "inflation", "cpi", "ppi", "jobs report", "treasury yields"
    ],
    "Gold/Oil": [
        "gold", "oil", "crude", "brent", "wti", "opec", "commodity",
        "commodities", "safe haven"
    ],
    "China": [
        "china", "hong kong", "beijing", "yuan", "pboc", "chinese",
        "property crisis", "asia stocks"
    ],
    "War/Geopolitics": [
        "war", "conflict", "russia", "ukraine", "middle east", "iran",
        "israel", "tariff", "sanction", "geopolitical"
    ],
    "All": [
        "bitcoin", "crypto", "ethereum", "inflation", "interest rate",
        "fed", "ecb", "cpi", "oil", "gold", "crude", "opec", "stock",
        "nasdaq", "dow", "s&p", "war", "conflict", "russia", "ukraine",
        "middle east", "china", "market", "economy"
    ]
}


# =========================================================
# LOG TO UI
# =========================================================

class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            msg = self.format(record)
            self.q.put(msg)
        except Exception:
            pass


log_queue = queue.Queue()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(APP_NAME)
logger.setLevel(logging.INFO)

gui_handler = QueueLogHandler(log_queue)
gui_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(gui_handler)


# =========================================================
# CONFIG
# =========================================================

@dataclass
class AppConfig:
    openai_api_key: str = ""
    wp_url: str = ""
    wp_jwt: str = ""
    crypto_panic: str = ""

    selected_categories: List[str] = field(default_factory=lambda: ["Crypto"])
    custom_topic_filter: str = ""
    min_score: int = 7
    post_interval_seconds: int = 17280
    posts_per_day: int = 5
    recent_hours: int = 6

    create_image: bool = True
    wp_publish_status: str = "publish"
    content_language: str = "English"

    # Cost controls
    # Default setup is optimized for low API cost: heuristic scoring, one text call per article, and low-quality image generation only for high-score news.
    ai_text_model: str = "gpt-5-nano"
    enable_ai_scoring: bool = False
    image_policy: str = "every_post"  # off | high_score_only | every_post
    image_min_score: int = 9
    image_model: str = "gpt-image-2"
    image_quality: str = "low"
    image_size: str = "1536x1024"

    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    telegram_session_name: str = "forward_session"
    telegram_phone: str = ""

    telegram_source_channel: str = "dttdsignal"
    telegram_target_channels: str = (
        "@tradingsignal12221\n"
        "@KriptoHaberleri2025\n"
        "@backfeecrypto\n"
        "@cryptodautu\n"
    )
    enable_telegram_forward: bool = False
    enable_telegram_social_post: bool = True

    x_auth_token: str = ""
    x_ct0: str = ""
    enable_x_post: bool = False

    facebook_cookie_json: str = ""
    facebook_target_url: str = ""
    enable_facebook_post: bool = False

    # Web3 subscription settings
    project_owner_wallet: str = PROJECT_OWNER_WALLET
    mantle_rpc_url: str = "https://rpc.mantle.xyz"
    mantlescan_api_url: str = "https://api.etherscan.io/v2/api"
    mantlescan_api_key: str = ""
    monthly_mnt_amount: float = 50.0
    monthly_credit_amount: int = 100
    credit_token_address: str = ""
    credit_token_symbol: str = CREDIT_TOKEN_SYMBOL

    enable_block_scam: bool = False
    enable_block_scam_ai: bool = True
    block_scam_ai_model: str = "gpt-5-nano"
    block_scam_ai_threshold: int = 7
    block_scam_keywords: str = (
        "vip\n"
        "inbox\n"
        "lien he\n"
        "liên hệ\n"
        "team futures\n"
        "copytrade\n"
        "signal\n"
        "mở team\n"
        "nhóm trade\n"
        "ib\n"
        "pm\n"
        "t.me/\n"
        "joinchat\n"
    )
    block_scam_target_chats: str = ""


def apply_server_subscription_settings(cfg: AppConfig) -> AppConfig:
    """Apply server-owned subscription settings from environment variables.

    These values define the payment gate for the entire deployment and are not
    editable from the dashboard. They can be configured in Railway Variables.
    """
    cfg.project_owner_wallet = PROJECT_OWNER_WALLET
    cfg.mantle_rpc_url = os.getenv("MANTLE_RPC_URL", cfg.mantle_rpc_url).strip() or "https://rpc.mantle.xyz"
    cfg.mantlescan_api_url = os.getenv("EXPLORER_API_V2_URL", os.getenv("ETHERSCAN_API_URL", cfg.mantlescan_api_url)).strip() or "https://api.etherscan.io/v2/api"
    cfg.mantlescan_api_key = os.getenv("ETHERSCAN_API_KEY", os.getenv("MANTLESCAN_API_KEY", cfg.mantlescan_api_key)).strip()
    cfg.credit_token_address = os.getenv("CREDIT_TOKEN_ADDRESS", cfg.credit_token_address).strip()
    cfg.credit_token_symbol = os.getenv("CREDIT_TOKEN_SYMBOL", cfg.credit_token_symbol or CREDIT_TOKEN_SYMBOL).strip() or CREDIT_TOKEN_SYMBOL
    try:
        cfg.monthly_mnt_amount = float(os.getenv("MONTHLY_MNT_AMOUNT", str(cfg.monthly_mnt_amount)).strip())
    except Exception:
        cfg.monthly_mnt_amount = 50.0
    try:
        cfg.monthly_credit_amount = int(os.getenv("MONTHLY_CREDIT_AMOUNT", str(cfg.monthly_credit_amount)).strip())
    except Exception:
        cfg.monthly_credit_amount = 100
    return cfg


class ConfigStore:
    @staticmethod
    def load() -> AppConfig:
        if not CONFIG_PATH.exists():
            return apply_server_subscription_settings(AppConfig())

        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = AppConfig(**data)
            return apply_server_subscription_settings(cfg)
        except Exception as e:
            logger.error(f"Config read error: {e}")
            return apply_server_subscription_settings(AppConfig())

    @staticmethod
    def save(cfg: AppConfig):
        apply_server_subscription_settings(cfg)
        CONFIG_PATH.write_text(
            json.dumps(cfg.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )


# =========================================================
# DATABASE
# =========================================================

class AppDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.init()

    def init(self):
        with self.lock:
            c = self.conn.cursor()
            c.execute("""
            CREATE TABLE IF NOT EXISTS seen_news (
                uid TEXT PRIMARY KEY,
                created_at TEXT
            )
            """)
            c.execute("""
            CREATE TABLE IF NOT EXISTS sent_messages (
                message_id INTEGER PRIMARY KEY,
                created_at TEXT
            )
            """)
            self.conn.commit()

    def is_seen_news(self, uid: str) -> bool:
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT 1 FROM seen_news WHERE uid=?", (uid,))
            return c.fetchone() is not None

    def mark_seen_news(self, uid: str):
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO seen_news(uid, created_at) VALUES (?, ?)",
                (uid, datetime.now(timezone.utc).isoformat())
            )
            self.conn.commit()

    def is_sent_message(self, msg_id: int) -> bool:
        with self.lock:
            c = self.conn.cursor()
            c.execute("SELECT 1 FROM sent_messages WHERE message_id=?", (msg_id,))
            return c.fetchone() is not None

    def mark_sent_message(self, msg_id: int):
        with self.lock:
            c = self.conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO sent_messages(message_id, created_at) VALUES (?, ?)",
                (msg_id, datetime.now(timezone.utc).isoformat())
            )
            self.conn.commit()


db = AppDB(DB_PATH)


# =========================================================
# HELPERS
# =========================================================

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text

def clean_ai_output(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:html|python|text)?", "", text, flags=re.I).strip()
    text = re.sub(r"```$", "", text).strip()
    return text

def clean_title(text: str) -> str:
    text = clean_ai_output(text)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if lines:
        text = lines[0]
    text = re.sub(r"^\s*[\-\*\d\.\)\:]+", "", text).strip()
    text = text.strip('"“”')
    return text[:180]

def normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )
    for ch in ["\u200b", "\u200c", "\u200d", "\ufeff", "\xa0"]:
        text = text.replace(ch, "")
    return text

def parse_lines(text: str) -> List[str]:
    return [x.strip() for x in (text or "").splitlines() if x.strip()]

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# =========================================================
# NEWS / WORDPRESS SERVICE
# =========================================================

class NewsWordPressService:
    def __init__(self, cfg_getter):
        self.cfg_getter = cfg_getter
        self.last_post_time = 0

    def get_openai(self) -> OpenAI:
        cfg = self.cfg_getter()
        key = cfg.openai_api_key or os.getenv("OPENAI_API_KEY", "")
        return OpenAI(api_key=key)

    def wp_headers(self) -> Dict[str, str]:
        cfg = self.cfg_getter()
        return {
            "Authorization": f"Bearer {cfg.wp_jwt or os.getenv('WP_JWT', '')}",
            "Content-Type": "application/json"
        }

    def get_full_content(self, news: Dict[str, Any]) -> str:
        if news.get("content"):
            try:
                return news["content"][0].get("value") or ""
            except Exception:
                pass
        return news.get("summary") or news.get("description") or news.get("title") or ""

    def is_recent(self, news: Dict[str, Any]) -> bool:
        cfg = self.cfg_getter()
        try:
            if news.get("published_parsed"):
                pub_time = datetime(*news["published_parsed"][:6], tzinfo=timezone.utc)
            elif news.get("created_at"):
                pub_time = datetime.fromisoformat(news["created_at"].replace("Z", "+00:00"))
            else:
                return True

            return (now_utc() - pub_time).total_seconds() < cfg.recent_hours * 3600
        except Exception:
            return True

    def selected_keywords(self) -> List[str]:
        cfg = self.cfg_getter()
        keys = []
        selected = cfg.selected_categories or ["All"]

        for cat in selected:
            keys.extend(CATEGORY_KEYWORDS.get(cat, []))

        if cfg.custom_topic_filter:
            extra = re.split(r"[,\n]+", cfg.custom_topic_filter)
            keys.extend([x.strip().lower() for x in extra if x.strip()])

        return list(dict.fromkeys(keys))

    def output_language(self) -> str:
        cfg = self.cfg_getter()
        lang = (getattr(cfg, "content_language", "English") or "English").strip()
        return lang if lang in CONTENT_LANGUAGES else "English"

    def language_rule(self) -> str:
        lang = self.output_language()
        return (
            f"Write all generated content in {lang}. The WordPress title, WordPress article, "
            f"and every social post must use the same selected language: {lang}. "
            "Do not switch to another language unless it is part of a proper noun, brand, ticker, or quoted source."
        )

    def matches_user_choice(self, news: Dict[str, Any]) -> bool:
        cfg = self.cfg_getter()
        if "All" in cfg.selected_categories:
            return True

        title = news.get("title", "")
        content = self.get_full_content(news)
        text = f"{title} {content}".lower()

        keys = self.selected_keywords()
        if not keys:
            return True

        return any(k.lower() in text for k in keys)

    def fetch_news_from_cryptopanic(self) -> List[Dict[str, Any]]:
        cfg = self.cfg_getter()
        token = cfg.crypto_panic or os.getenv("CRYPTO_PANIC", "")
        if not token:
            return []

        try:
            r = requests.get(
                "https://cryptopanic.com/api/v1/posts/",
                params={"auth_token": token},
                timeout=20
            )
            data = r.json()
            results = data.get("results") or []
            news_list = []
            for item in results[:20]:
                news_list.append({
                    "title": item.get("title"),
                    "summary": item.get("title"),
                    "link": item.get("url"),
                    "id": item.get("id") or item.get("url"),
                    "created_at": item.get("created_at")
                })
            return news_list
        except Exception as e:
            logger.warning(f"CryptoPanic error: {e}")
            return []

    def fetch_news_from_rss(self) -> List[Dict[str, Any]]:
        all_news = []

        for feed_url in RSS_FEEDS:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:20]:
                    item = dict(entry)
                    item["_feed_url"] = feed_url
                    all_news.append(item)
            except Exception as e:
                logger.warning(f"RSS error {feed_url}: {e}")

        random.shuffle(all_news)
        return all_news

    def fetch_candidates(self) -> List[Dict[str, Any]]:
        news_list = []

        news_list.extend(self.fetch_news_from_cryptopanic())
        news_list.extend(self.fetch_news_from_rss())

        candidates = []
        for news in news_list:
            title = news.get("title", "")
            uid = str(news.get("id") or news.get("link") or title)

            if not title:
                continue

            if db.is_seen_news(uid):
                continue

            if not self.is_recent(news):
                logger.info(f"⛔ Old news: {title}")
                continue

            if not self.matches_user_choice(news):
                logger.info(f"⛔ Does not match selected filters: {title}")
                continue

            candidates.append(news)

        return candidates[:12]

    def score_news_heuristic(self, news: Dict[str, Any]) -> int:
        """Cheap local scoring used by default to avoid spending API calls on every RSS item."""
        title = news.get("title", "") or ""
        content = strip_html(self.get_full_content(news))
        text = normalize_text(f"{title} {content}")

        score = 4
        strong_terms = [
            "bitcoin", "btc", "ethereum", "eth", "fed", "federal reserve", "cpi", "inflation",
            "etf", "rate cut", "rate hike", "sec", "binance", "coinbase", "hack", "lawsuit",
            "war", "tariff", "sanction", "oil", "gold", "nasdaq", "dow", "s&p"
        ]
        hot_terms = ["surge", "plunge", "crash", "rally", "record", "approval", "reject", "ban", "breakout", "liquidation"]

        score += min(3, sum(1 for k in strong_terms if normalize_text(k) in text))
        score += min(2, sum(1 for k in hot_terms if normalize_text(k) in text))
        if len(title) >= 45:
            score += 1
        if any(k in text for k in ["breaking", "urgent", "exclusive"]):
            score += 1

        return max(0, min(10, score))

    def score_news(self, news: Dict[str, Any]) -> int:
        cfg = self.cfg_getter()
        if not getattr(cfg, "enable_ai_scoring", False):
            return self.score_news_heuristic(news)

        client = self.get_openai()
        model = getattr(cfg, "ai_text_model", "gpt-5-nano") or "gpt-5-nano"
        prompt = f"""
Rate the market impact of the news below from 0 to 10.
Return only one integer.

TITLE:
{news.get("title")}

CONTENT:
{strip_html(self.get_full_content(news))[:2500]}
"""
        try:
            res = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=16,
            )
            raw = (res.choices[0].message.content or "").strip()
            m = re.search(r"\d+", raw)
            return int(m.group(0)) if m else self.score_news_heuristic(news)
        except Exception as e:
            logger.warning(f"Score error, falling back to local scoring: {e}")
            return self.score_news_heuristic(news)

    def _article_quality_ok(self, article_html: str) -> bool:
        """Keep WordPress articles from becoming short summaries."""
        plain = strip_html(article_html)
        word_count = len(re.findall(r"\w+", plain))
        h2_count = len(re.findall(r"<h2[\s>].*?</h2>|<h2>", article_html or "", flags=re.I | re.S))
        paragraphs = len(re.findall(r"<p[\s>].*?</p>", article_html or "", flags=re.I | re.S))
        return word_count >= 750 and h2_count >= 4 and paragraphs >= 8

    def _repair_article_if_needed(self, news: Dict[str, Any], title: str, article_html: str) -> str:
        """Retry once with a stronger prompt/model when the low-cost model returns a thin article."""
        if self._article_quality_ok(article_html):
            return article_html

        logger.warning("Article quality check failed; regenerating a fuller WordPress article.")
        cfg = self.cfg_getter()
        client = self.get_openai()
        fallback_model = "gpt-5-mini" if getattr(cfg, "ai_text_model", "gpt-5-nano") == "gpt-5-nano" else getattr(cfg, "ai_text_model", "gpt-5-mini")
        source = strip_html(self.get_full_content(news))[:7000]

        prompt = f"""
You are a senior financial journalist and WordPress editor.

{self.language_rule()}

Write a COMPLETE WordPress article for the headline below.

STRICT OUTPUT RULES:
- Return HTML only. No markdown. No JSON. No code block.
- Do NOT use <h1>; WordPress already uses the post title as H1.
- Start with 2 strong introductory <p> paragraphs.
- Use at least 5 meaningful <h2> sections.
- Use <h3> only when it helps explain a sub-topic.
- Each <h2> section must have 2-3 substantial paragraphs.
- Write like a real financial news article, not a summary.
- Do not use bullet points unless absolutely necessary.
- Do not fabricate numbers, quotes, dates, or facts not present in the source.
- If the source is short, explain the context, market reaction, investor implications, and what to watch next without inventing data.
- Target length: 900-1,300 words.

TITLE:
{title}

SOURCE:
{source}
"""
        try:
            res = client.chat.completions.create(
                model=fallback_model,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=4200,
            )
            repaired = clean_ai_output(res.choices[0].message.content or "")
            if self._article_quality_ok(repaired):
                return repaired
            logger.warning("Article repair still looks thin; using the best available article.")
            return repaired or article_html
        except Exception as e:
            logger.error(f"Article repair error: {e}")
            return article_html

    def make_content_bundle(self, news: Dict[str, Any], link_placeholder: str = "{{LINK}}") -> Dict[str, str]:
        """Generate title, WordPress article, and social summary in one API call to reduce cost."""
        cfg = self.cfg_getter()
        client = self.get_openai()
        model = getattr(cfg, "ai_text_model", "gpt-5-nano") or "gpt-5-nano"
        content = strip_html(self.get_full_content(news))[:5000]

        prompt = f"""
You are a senior financial news editor and WordPress journalist.

{self.language_rule()}

Create a JSON object with exactly these keys:
- "title": one natural financial-news headline.
- "article_html": a complete WordPress article in HTML.
- "social_summary": a concise social post, 500-900 characters, with one market-impact sentence and this link placeholder at the end: {link_placeholder}

ARTICLE_HTML REQUIREMENTS:
- Do NOT use <h1>; WordPress already uses the post title as H1.
- Start with 2 strong introductory <p> paragraphs.
- Use at least 5 meaningful <h2> sections.
- Use <h3> only when it helps explain a sub-topic.
- Each major section must include substantial paragraphs, not one-line summaries.
- Write like a real financial article with context, market impact, investor implications, and what to watch next.
- Do not use bullets unless truly necessary.
- Do not add outside facts, fake quotes, fake dates, or fabricated numbers.
- Target length: 900-1,300 words.

SOURCE TITLE:
{news.get("title")}

SOURCE CONTENT:
{content}
"""
        try:
            res = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_completion_tokens=4200,
            )
            raw = clean_ai_output(res.choices[0].message.content or "{}")
            data = json.loads(raw)
            title = clean_title(str(data.get("title") or news.get("title") or "Market Update"))
            article = clean_ai_output(str(data.get("article_html") or ""))
            summary = clean_ai_output(str(data.get("social_summary") or ""))
            if not article:
                article = f"<p>{content[:700]}</p><h2>Market Context</h2><p>{content[700:1400]}</p>"
            article = self._repair_article_if_needed(news, title, article)
            if not summary:
                summary = f"{title}\n\n{strip_html(article)[:650]}...\n\n{link_placeholder}"
            return {"title": title, "article": article, "summary": summary}
        except Exception as e:
            logger.error(f"Content bundle error, falling back to separate generation: {e}")
            title = self.make_title(news)
            article = self.write_article(news)
            summary = self.summarize_for_social(title, article, link_placeholder)
            return {"title": title, "article": article, "summary": summary}

    def make_title(self, news: Dict[str, Any]) -> str:
        cfg = self.cfg_getter()
        client = self.get_openai()
        model = getattr(cfg, "ai_text_model", "gpt-5-nano") or "gpt-5-nano"

        prompt = f"""
You are a financial news editor.

{self.language_rule()}

Rewrite the original headline into exactly ONE natural financial-news headline.
Return only one headline. Do not add outside facts.

ORIGINAL HEADLINE:
{news.get("title")}
"""
        res = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=120,
        )
        return clean_title(res.choices[0].message.content)

    def write_article(self, news: Dict[str, Any]) -> str:
        cfg = self.cfg_getter()
        client = self.get_openai()
        model = getattr(cfg, "ai_text_model", "gpt-5-nano") or "gpt-5-nano"
        content = strip_html(self.get_full_content(news))[:5000]

        prompt = f"""
You are a financial journalist.

{self.language_rule()}

Write a complete natural financial-news article with HTML H2/H3 structure.
Do not use H1 because WordPress already uses the post title as H1.
Start with 2 introductory paragraphs, use at least 5 <h2> sections, and make every section substantial.
Write coherent paragraphs, not bullets. Do not add outside facts or fabricate numbers. Target length: 900-1,300 words.

SOURCE DATA:
{news.get("title")}

{content}
"""
        res = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=4200,
        )
        return self._repair_article_if_needed(news, news.get("title", "Market Update"), clean_ai_output(res.choices[0].message.content))

    def should_generate_image(self, score: int) -> bool:
        cfg = self.cfg_getter()
        policy = (getattr(cfg, "image_policy", "high_score_only") or "high_score_only").strip()
        if not getattr(cfg, "create_image", True):
            return False
        if policy == "off":
            return False
        if policy == "high_score_only":
            return score >= int(getattr(cfg, "image_min_score", 9) or 9)
        return True

    def create_image(self, title: str, score: int = 10) -> Optional[bytes]:
        cfg = self.cfg_getter()
        if not self.should_generate_image(score):
            logger.info("🎨 Featured image skipped by cost policy.")
            return None

        client = self.get_openai()
        model = getattr(cfg, "image_model", "gpt-image-2") or "gpt-image-2"
        quality = getattr(cfg, "image_quality", "low") or "low"
        size = getattr(cfg, "image_size", "1536x1024") or "1536x1024"

        prompt = f"""
Create a clear, high-contrast editorial featured image for a financial news article.

Headline:
{title}

Visual direction:
- 16:9 news-thumbnail composition.
- Abstract financial markets, crypto market charts, candlestick motion, macro finance symbols, data grids, risk/volatility mood.
- Strong foreground subject, clean depth, premium fintech editorial style.
- No real people, no faces, no hands.
- No readable text, no logos, no watermark, no brand names.
- Must not be blank, plain, or mostly empty.
"""
        try:
            res = client.images.generate(
                model=model,
                prompt=prompt,
                size=size,
                quality=quality,
                n=1,
            )
            img = res.data[0]
            if getattr(img, "url", None):
                data = requests.get(img.url, timeout=60).content
                logger.info(f"✅ Image generated: {len(data)} bytes")
                return data
            if getattr(img, "b64_json", None):
                data = base64.b64decode(img.b64_json)
                logger.info(f"✅ Image generated: {len(data)} bytes")
                return data
        except Exception as e:
            logger.error(f"🔥 Image generation error: {e}")
        return None

    def upload_image(self, img: Optional[bytes]) -> Optional[int]:
        if not img:
            return None

        cfg = self.cfg_getter()
        wp_url = cfg.wp_url or os.getenv("WP_URL", "")
        wp_jwt = cfg.wp_jwt or os.getenv("WP_JWT", "")

        if not wp_url or not wp_jwt:
            logger.warning("Missing WP_URL or WP_JWT, skipping image upload.")
            return None

        media_url = wp_url.rsplit("/posts", 1)[0] + "/media"

        try:
            r = requests.post(
                media_url,
                headers={"Authorization": f"Bearer {wp_jwt}"},
                files={"file": ("featured.png", img, "image/png")},
                timeout=90
            )

            if r.status_code in (200, 201):
                media_id = r.json().get("id")
                logger.info(f"✅ Featured image uploaded: media_id={media_id}")
                return media_id

            logger.error(f"Image upload error: {r.text}")
            return None

        except Exception as e:
            logger.error(f"Image upload exception: {e}")
            return None

    def post_wp(self, title: str, content: str, img_id: Optional[int]) -> Optional[Dict[str, Any]]:
        cfg = self.cfg_getter()
        wp_url = cfg.wp_url or os.getenv("WP_URL", "")

        if not wp_url:
            logger.error("Missing WP_URL.")
            return None

        post_url = wp_url.rsplit("/posts", 1)[0] + "/posts"

        data = {
            "title": title,
            "content": content,
            "status": cfg.wp_publish_status or "publish",
        }

        if img_id:
            data["featured_media"] = img_id

        try:
            r = requests.post(
                post_url,
                headers=self.wp_headers(),
                json=data,
                timeout=90
            )

            if r.status_code in (200, 201):
                logger.info(f"✅ WordPress POST OK | featured_media={img_id or 'none'}")
                self.last_post_time = time.time()
                return r.json()

            logger.error(f"WordPress error: {r.status_code} | {r.text}")
            return None

        except Exception as e:
            logger.error(f"WordPress exception: {e}")
            return None

    def summarize_for_social(self, title: str, article_html: str, link: str) -> str:
        cfg = self.cfg_getter()
        client = self.get_openai()
        model = getattr(cfg, "ai_text_model", "gpt-5-nano") or "gpt-5-nano"
        plain = strip_html(article_html)[:3500]

        prompt = f"""
Summarize the article below for social media.

{self.language_rule()}

Keep it concise and suitable for a financial social post. Do not use bullets. Do not exaggerate. Include one sentence about market impact. Add the article link at the end. Length: 500-900 characters.

TITLE:
{title}

ARTICLE:
{plain}

LINK:
{link}
"""
        try:
            res = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_completion_tokens=400,
            )
            return self._repair_article_if_needed(news, news.get("title", "Market Update"), clean_ai_output(res.choices[0].message.content))
        except Exception as e:
            logger.error(f"Social summary error: {e}")
            return f"{title}\n\n{plain[:500]}...\n\n{link}"

    def create_and_post_once(self) -> Optional[Dict[str, Any]]:
        cfg = self.cfg_getter()

        logger.info("🔎 Scanning RSS/API news...")
        candidates = self.fetch_candidates()

        if not candidates:
            logger.info("No matching news found.")
            return None

        best_news = None
        best_score = -1

        for news in candidates:
            title = news.get("title", "")
            score = self.score_news(news)
            logger.info(f"📊 Score {score}: {title}")

            if score > best_score:
                best_score = score
                best_news = news

            # Keep scanning the candidate batch so the selected article is the hottest available item, not just the first item above the threshold.

        if not best_news:
            logger.info("Could not select news.")
            return None

        if best_score < cfg.min_score:
            logger.info(f"⛔ Score is below threshold {cfg.min_score}, skipping.")
            return None

        uid = str(best_news.get("id") or best_news.get("link") or best_news.get("title"))
        db.mark_seen_news(uid)

        logger.info("✍️ Generating title, article, and social draft in one optimized API call...")
        bundle = self.make_content_bundle(best_news)
        title_new = bundle["title"]
        article = bundle["article"]
        summary_template = bundle["summary"]
        logger.info(f"📰 New title: {title_new}")

        logger.info("🎨 Checking featured-image cost policy...")
        img = self.create_image(title_new, best_score)
        img_id = self.upload_image(img)

        logger.info("🚀 Publishing to WordPress...")
        wp_result = self.post_wp(title_new, article, img_id)

        if not wp_result:
            return None

        link = wp_result.get("link") or ""
        summary = summary_template.replace("{{LINK}}", link)
        if link and link not in summary:
            summary = f"{summary.rstrip()}\n\n{link}"

        return {
            "title": title_new,
            "article": article,
            "summary": summary,
            "link": link,
            "wp": wp_result
        }


# =========================================================
# SOCIAL POSTING WITH PLAYWRIGHT
# =========================================================

class PlaywrightSocialService:
    def __init__(self, cfg_getter):
        self.cfg_getter = cfg_getter

    def browser_headless(self) -> bool:
        # Railway/container: always run headless unless explicitly disabled locally.
        return str(os.getenv("PLAYWRIGHT_HEADLESS", "1")).strip().lower() not in {"0", "false", "no"}

    async def launch_persistent_context_safe(
        self,
        p,
        user_data_dir: str,
        *,
        headless: bool,
        viewport: Optional[Dict[str, int]] = None,
    ):
        """
        Mở Chromium profile riêng cho app.

        Lý do cần hàm này:
        - Nhiều máy Windows chưa chạy `playwright install chromium` nên launch mặc định lỗi.
        - Nếu có Chrome/Edge cài sẵn, app sẽ tự fallback sang channel chrome/msedge.
        - Khi lỗi, log sẽ chỉ rõ cách sửa thay vì im lặng không mở trình duyệt.
        """
        viewport = viewport or {"width": 1280, "height": 900}
        common_kwargs = dict(
            user_data_dir=user_data_dir,
            headless=headless,
            viewport=viewport,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )

        attempts = [
            ("Playwright Chromium", {}),
            ("Google Chrome", {"channel": "chrome"}),
            ("Microsoft Edge", {"channel": "msedge"}),
        ]

        last_error = None
        for label, extra in attempts:
            try:
                logger.info(f"🌐 Trying to open browser with {label}...")
                return await p.chromium.launch_persistent_context(**common_kwargs, **extra)
            except Exception as e:
                last_error = e
                logger.warning(f"Could not open {label}: {e}")

        raise RuntimeError(
            "Could not open the Playwright browser. "
            "Install the browser with: python -m playwright install chromium. "
            f"Last error details: {last_error}"
        )

    async def wait_until_browser_closed(self, context):
        """Chờ user đóng cửa sổ login thủ công, không làm app treo vô hạn nếu context đã đóng."""
        closed = asyncio.Event()

        def _mark_closed(*args):
            try:
                closed.set()
            except Exception:
                pass

        try:
            context.on("close", _mark_closed)
        except Exception:
            pass

        while True:
            if closed.is_set():
                break

            try:
                pages = context.pages
            except Exception:
                break

            if not pages:
                break

            await asyncio.sleep(1)

        try:
            await context.close()
        except Exception:
            pass

    async def open_x_login_browser(self):
        logger.info("🌐 Opening X browser for manual login...")
        async with async_playwright() as p:
            user_data_dir = str(PROFILE_DIR / "x_profile")
            context = await self.launch_persistent_context_safe(
                p,
                user_data_dir,
                headless=False,
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://x.com/login", wait_until="domcontentloaded", timeout=90000)
            logger.info("✅ X browser opened. Log in, then close the browser window to save the session.")
            await self.wait_until_browser_closed(context)
            logger.info("✅ X browser closed. Session saved to runtime/browser_profiles/x_profile.")

    async def open_facebook_login_browser(self):
        logger.info("🌐 Opening Facebook browser for manual login...")
        async with async_playwright() as p:
            user_data_dir = str(PROFILE_DIR / "facebook_profile")
            context = await self.launch_persistent_context_safe(
                p,
                user_data_dir,
                headless=False,
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded", timeout=90000)
            logger.info("✅ Facebook browser opened. Log in, then close the browser window to save the session.")
            await self.wait_until_browser_closed(context)
            logger.info("✅ Facebook browser closed. Session saved to runtime/browser_profiles/facebook_profile.")

    async def add_x_cookies_if_any(self, context):
        cfg = self.cfg_getter()
        cookies = []

        if cfg.x_auth_token:
            for domain in [".x.com", ".twitter.com"]:
                cookies.append({
                    "name": "auth_token",
                    "value": cfg.x_auth_token.strip(),
                    "domain": domain,
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "None"
                })

        if cfg.x_ct0:
            for domain in [".x.com", ".twitter.com"]:
                cookies.append({
                    "name": "ct0",
                    "value": cfg.x_ct0.strip(),
                    "domain": domain,
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax"
                })

        if cookies:
            await context.add_cookies(cookies)
            logger.info("✅ X cookies loaded.")

    async def add_facebook_cookies_if_any(self, context):
        cfg = self.cfg_getter()
        raw = (cfg.facebook_cookie_json or "").strip()

        if not raw:
            return

        try:
            cookies = json.loads(raw)

            if isinstance(cookies, dict):
                cookies = [cookies]

            fixed = []
            for c in cookies:
                item = dict(c)
                if "sameSite" in item:
                    val = str(item["sameSite"]).capitalize()
                    if val not in ["Strict", "Lax", "None"]:
                        item.pop("sameSite", None)
                    else:
                        item["sameSite"] = val

                if "domain" not in item:
                    item["domain"] = ".facebook.com"

                if "path" not in item:
                    item["path"] = "/"

                fixed.append(item)

            await context.add_cookies(fixed)
            logger.info("✅ Facebook cookies loaded.")

        except Exception as e:
            logger.error(f"Facebook Cookie JSON error: {e}")

    async def post_x(self, text: str):
        cfg = self.cfg_getter()
        if not cfg.enable_x_post:
            logger.info("X posting is disabled, skipping X.")
            return

        logger.info("🐦 Posting to X with Playwright...")

        async with async_playwright() as p:
            user_data_dir = str(PROFILE_DIR / "x_profile")

            context = await self.launch_persistent_context_safe(
                p,
                user_data_dir,
                headless=self.browser_headless(),
                viewport={"width": 1280, "height": 900},
            )

            await self.add_x_cookies_if_any(context)

            page = context.pages[0] if context.pages else await context.new_page()

            try:
                await page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(5000)

                if "login" in page.url.lower() or "flow/login" in page.url.lower():
                    raise RuntimeError("X is not logged in or cookies have expired. Paste auth_token and ct0 again on the dashboard, then click Save.")

                selectors = [
                    'div[data-testid="tweetTextarea_0"]',
                    'div[role="textbox"][data-testid="tweetTextarea_0"]',
                    'div[role="textbox"]'
                ]

                box = None
                for sel in selectors:
                    try:
                        loc = page.locator(sel).first
                        await loc.wait_for(timeout=12000)
                        box = loc
                        break
                    except Exception:
                        pass

                if not box:
                    raise RuntimeError("Could not find the X composer input. Log in again or check whether the X interface changed.")

                short_text = text
                if len(short_text) > 260:
                    short_text = short_text[:255] + "..."

                await box.click()
                await page.keyboard.insert_text(short_text)
                await page.wait_for_timeout(1000)

                buttons = [
                    'button[data-testid="tweetButton"]',
                    'button[data-testid="tweetButtonInline"]'
                ]

                clicked = False
                for sel in buttons:
                    try:
                        btn = page.locator(sel).first
                        await btn.wait_for(timeout=7000)
                        if await btn.is_enabled():
                            await btn.click()
                            clicked = True
                            break
                    except Exception:
                        pass

                if not clicked:
                    raise RuntimeError("Could not find or click the X post button.")

                await page.wait_for_timeout(5000)
                logger.info("✅ Posted to X.")

            finally:
                await context.close()

    async def post_facebook(self, text: str):
        cfg = self.cfg_getter()
        if not cfg.enable_facebook_post:
            logger.info("Facebook posting is disabled, skipping Facebook.")
            return

        target = cfg.facebook_target_url.strip() or "https://www.facebook.com/"

        logger.info("📘 Posting to Facebook with Playwright...")

        async with async_playwright() as p:
            user_data_dir = str(PROFILE_DIR / "facebook_profile")

            context = await self.launch_persistent_context_safe(
                p,
                user_data_dir,
                headless=self.browser_headless(),
                viewport={"width": 1280, "height": 900},
            )

            await self.add_facebook_cookies_if_any(context)

            page = context.pages[0] if context.pages else await context.new_page()

            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(6000)

                current_url = page.url.lower()
                if "login" in current_url or "checkpoint" in current_url:
                    raise RuntimeError("Facebook is not logged in, cookies expired, or the account is checkpointed. Paste Facebook Cookie JSON again on the dashboard, then click Save.")

                composer_triggers = [
                    "div[role=\"button\"]:has-text(\"What\'s on your mind\")",
                    'div[role="button"]:has-text("Bạn đang nghĩ gì")',
                    "span:has-text(\"What\'s on your mind\")",
                    'span:has-text("Bạn đang nghĩ gì")',
                    'div[aria-label*="Create a post"]',
                    'div[aria-label*="Tạo bài viết"]',
                    'div[role="button"][aria-label*="post" i]',
                ]

                opened = False
                for sel in composer_triggers:
                    try:
                        loc = page.locator(sel).first
                        await loc.wait_for(timeout=7000)
                        await loc.click()
                        opened = True
                        break
                    except Exception:
                        pass

                if not opened:
                    logger.warning("Could not find a clear composer button, trying direct input detection...")

                await page.wait_for_timeout(3000)

                textboxes = [
                    'div[role="textbox"][contenteditable="true"]',
                    'div[role="textbox"]',
                    "div[aria-label*=\"What\'s on your mind\"]",
                    'div[aria-label*="Bạn đang nghĩ gì"]'
                ]

                box = None
                for sel in textboxes:
                    try:
                        loc = page.locator(sel).last
                        await loc.wait_for(timeout=10000)
                        box = loc
                        break
                    except Exception:
                        pass

                if not box:
                    raise RuntimeError("Could not find the Facebook input box. Log in again or check whether the Target URL is the correct profile/page/group.")

                await box.click()
                await page.keyboard.insert_text(text)
                await page.wait_for_timeout(1000)

                post_buttons = [
                    'div[aria-label="Post"]',
                    'div[aria-label="Đăng"]',
                    'div[role="button"]:has-text("Post")',
                    'div[role="button"]:has-text("Đăng")',
                    'span:has-text("Post")',
                    'span:has-text("Đăng")'
                ]

                clicked = False
                for sel in post_buttons:
                    try:
                        btn = page.locator(sel).last
                        await btn.wait_for(timeout=10000)
                        await btn.click()
                        clicked = True
                        break
                    except Exception:
                        pass

                if not clicked:
                    raise RuntimeError("Could not find or click the Facebook Post button.")

                await page.wait_for_timeout(8000)
                logger.info("✅ Posted to Facebook.")

            finally:
                await context.close()

# =========================================================
# TELEGRAM SERVICE
# =========================================================

class TelegramService:
    def __init__(self, cfg_getter):
        self.cfg_getter = cfg_getter
        self.pending_client: Optional[TelegramClient] = None
        self.pending_phone: str = ""

    def session_path(self, session_name: str) -> str:
        clean = session_name.strip() or "forward_session"
        return str(SESSION_DIR / clean)

    def api_pair(self) -> Tuple[int, str]:
        cfg = self.cfg_getter()
        api_id_raw = cfg.telegram_api_id or os.getenv("API_ID", "")
        api_hash = cfg.telegram_api_hash or os.getenv("API_HASH", "")

        if not api_id_raw or not api_hash:
            raise RuntimeError("Missing Telegram API_ID or API_HASH.")

        return int(api_id_raw), api_hash

    async def send_login_code(self, phone: str):
        cfg = self.cfg_getter()
        api_id, api_hash = self.api_pair()

        session = self.session_path(cfg.telegram_session_name)
        client = TelegramClient(session, api_id, api_hash)

        await client.connect()

        if await client.is_user_authorized():
            logger.info("✅ Telegram session is already logged in.")
            await client.disconnect()
            return

        await client.send_code_request(phone)
        self.pending_client = client
        self.pending_phone = phone
        logger.info("✅ Telegram code sent. Enter the code and click Confirm.")

    async def confirm_login_code(self, code: str, password: str = ""):
        if not self.pending_client:
            raise RuntimeError("Telegram code has not been sent yet.")

        client = self.pending_client

        try:
            await client.sign_in(self.pending_phone, code)
        except SessionPasswordNeededError:
            if not password:
                raise RuntimeError("This account has 2FA enabled. Enter the 2FA password.")
            await client.sign_in(password=password)

        logger.info("✅ Telegram session created successfully.")
        await client.disconnect()
        self.pending_client = None

    async def test_session(self):
        cfg = self.cfg_getter()
        api_id, api_hash = self.api_pair()

        client = TelegramClient(
            self.session_path(cfg.telegram_session_name),
            api_id,
            api_hash
        )

        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("Telegram session is not logged in.")

        me = await client.get_me()
        logger.info(f"✅ Telegram session OK: {getattr(me, 'username', None) or me.id}")
        await client.disconnect()

    async def send_social_post(self, text: str):
        cfg = self.cfg_getter()
        if not cfg.enable_telegram_social_post:
            return

        targets = parse_lines(cfg.telegram_target_channels)
        if not targets:
            logger.info("No Telegram targets configured for social posting.")
            return

        api_id, api_hash = self.api_pair()
        client = TelegramClient(
            self.session_path(cfg.telegram_session_name),
            api_id,
            api_hash
        )

        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("Telegram session is not logged in.")

        for i, target in enumerate(targets, start=1):
            try:
                logger.info(f"📨 Telegram social [{i}/{len(targets)}] {target}")
                await client.send_message(target, text)
                await asyncio.sleep(2)
            except FloodWaitError as e:
                logger.warning(f"FloodWait {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"Telegram send error {target}: {e}")

        await client.disconnect()

    async def forward_loop(self, stop_event: threading.Event):
        cfg = self.cfg_getter()

        if not cfg.enable_telegram_forward:
            logger.info("Telegram forward is disabled.")
            return

        api_id, api_hash = self.api_pair()
        client = TelegramClient(
            self.session_path(cfg.telegram_session_name),
            api_id,
            api_hash
        )

        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            logger.error("Telegram session is not logged in; cannot forward.")
            return

        logger.info("🚀 Telegram forward loop started.")

        while not stop_event.is_set():
            try:
                cfg = self.cfg_getter()
                source = cfg.telegram_source_channel.strip()
                targets = parse_lines(cfg.telegram_target_channels)

                if not source or not targets:
                    await asyncio.sleep(10)
                    continue

                messages = await client.get_messages(source, limit=20)
                handled_groups = set()

                for msg in messages:
                    if stop_event.is_set():
                        break

                    if db.is_sent_message(msg.id):
                        continue

                    logger.info(f"🆕 Forward msg ID {msg.id}")

                    media_files = []
                    caption = msg.text or ""

                    if msg.grouped_id:
                        if msg.grouped_id in handled_groups:
                            continue

                        album = await client.get_messages(
                            source,
                            min_id=msg.id - 10,
                            max_id=msg.id + 10
                        )

                        album_msgs = [m for m in album if m.grouped_id == msg.grouped_id]
                        handled_groups.add(msg.grouped_id)

                        caption = ""
                        for m in album_msgs:
                            if m.media:
                                file = await m.download_media(file=str(RUNTIME_DIR))
                                media_files.append(file)
                            if m.text and not caption:
                                caption = m.text

                    else:
                        if msg.media:
                            file = await msg.download_media(file=str(RUNTIME_DIR))
                            media_files.append(file)

                    for target in targets:
                        try:
                            final_text = caption or ""
                            if media_files:
                                await client.send_file(target, media_files, caption=final_text)
                            else:
                                await client.send_message(target, final_text)

                            logger.info(f"✅ Forward OK: {target}")
                            await asyncio.sleep(3)

                        except FloodWaitError as e:
                            logger.warning(f"FloodWait {e.seconds}s")
                            await asyncio.sleep(e.seconds)

                        except Exception as e:
                            logger.error(f"Forward error {target}: {e}")

                    for f in media_files:
                        try:
                            if f and os.path.exists(f):
                                os.remove(f)
                        except Exception:
                            pass

                    db.mark_sent_message(msg.id)

                await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"Forward loop error: {e}")
                await asyncio.sleep(10)

        await client.disconnect()
        logger.info("Telegram forward loop stopped.")


# =========================================================
# BLOCK SCAM BASIC
# =========================================================

class BlockScamService:
    def __init__(self, cfg_getter, telegram_service: TelegramService):
        self.cfg_getter = cfg_getter
        self.telegram_service = telegram_service

    def contains_risky(self, text: str) -> bool:
        return self.rule_based_risk(text)[0]

    def rule_based_risk(self, text: str) -> Tuple[bool, str]:
        cfg = self.cfg_getter()
        keys = parse_lines(cfg.block_scam_keywords)
        norm = normalize_text(text)
        compact = re.sub(r"[\s\W_]+", "", norm)

        for k in keys:
            nk = normalize_text(k)
            nkc = re.sub(r"[\s\W_]+", "", nk)
            if nk and nk in norm:
                return True, f"keyword:{k}"
            if nkc and nkc in compact:
                return True, f"keyword:{k}"

        if re.search(r"(t\.me\/|joinchat|https?:\/\/)", text or "", re.I):
            return True, "link_or_invite"

        return False, "clean_by_rules"

    def ai_risk(self, text: str) -> Tuple[bool, str, int]:
        cfg = self.cfg_getter()
        if not getattr(cfg, "enable_block_scam_ai", True):
            return False, "ai_disabled", 0
        if not (cfg.openai_api_key or os.getenv("OPENAI_API_KEY", "")):
            return False, "missing_openai_key", 0
        if len((text or "").strip()) < 8:
            return False, "too_short", 0

        model = getattr(cfg, "block_scam_ai_model", "gpt-5-nano") or "gpt-5-nano"
        threshold = int(getattr(cfg, "block_scam_ai_threshold", 7) or 7)
        prompt = f"""
You are a Telegram anti-scam moderation classifier for a crypto/trading community.

Classify the message below. Return JSON only with:
- risk_score: integer 0-10
- should_delete: boolean
- reason: short reason

Delete only when the message is likely a scam, phishing attempt, fake support, suspicious investment solicitation, fake airdrop, wallet-draining link, impersonation, paid signal spam, or aggressive unsolicited promotion.
Do not delete normal discussion, market opinions, genuine questions, or harmless links.

MESSAGE:
{text[:1800]}
"""
        try:
            client = OpenAI(api_key=cfg.openai_api_key or os.getenv("OPENAI_API_KEY", ""))
            res = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_completion_tokens=160,
            )
            data = json.loads(clean_ai_output(res.choices[0].message.content or "{}"))
            score = int(data.get("risk_score", 0) or 0)
            reason = str(data.get("reason", "ai_classified"))[:120]
            should_delete = bool(data.get("should_delete", False)) or score >= threshold
            return should_delete, reason, score
        except Exception as e:
            logger.warning(f"BlockScam AI check failed: {e}")
            return False, "ai_error", 0

    def should_delete_message(self, text: str) -> Tuple[bool, str]:
        risky, reason = self.rule_based_risk(text)
        if risky:
            return True, reason
        ai_delete, ai_reason, ai_score = self.ai_risk(text)
        if ai_delete:
            return True, f"ai_score:{ai_score} {ai_reason}"
        return False, f"allowed ai_score:{ai_score} {ai_reason}"

    async def run_basic_monitor(self, stop_event: threading.Event):
        cfg = self.cfg_getter()
        if not cfg.enable_block_scam:
            logger.info("BlockScam is disabled.")
            return

        api_id, api_hash = self.telegram_service.api_pair()
        client = TelegramClient(
            self.telegram_service.session_path(cfg.telegram_session_name),
            api_id,
            api_hash
        )

        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            logger.error("Telegram session is not logged in; cannot run BlockScam.")
            return

        chats = parse_lines(cfg.block_scam_target_chats)
        if not chats:
            logger.warning("BlockScam is enabled but no chats are configured for scanning.")
            await client.disconnect()
            return

        logger.info("🛡️ BlockScam monitor started with keyword rules and optional AI classification.")

        while not stop_event.is_set():
            try:
                for chat in chats:
                    try:
                        messages = await client.get_messages(chat, limit=30)

                        for msg in messages:
                            if not msg or not msg.id:
                                continue

                            text = msg.text or ""

                            if not text:
                                continue

                            key = int(f"{abs(hash(str(chat))) % 100000}{msg.id % 100000}")
                            if db.is_sent_message(key):
                                continue

                            db.mark_sent_message(key)

                            should_delete, risk_reason = self.should_delete_message(text)
                            if should_delete:
                                logger.warning(f"🚫 Suspicious scam in {chat}: {risk_reason} | {text[:80]}")

                                try:
                                    await client.delete_messages(chat, msg.id)
                                    logger.info("✅ Scam message deleted.")
                                except Exception as e:
                                    logger.warning(f"Could not delete message: {e}")

                    except Exception as e:
                        logger.error(f"BlockScam chat error {chat}: {e}")

                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"BlockScam loop error: {e}")
                await asyncio.sleep(10)

        await client.disconnect()
        logger.info("BlockScam stopped.")


# =========================================================
# ASYNC RUNTIME
# =========================================================

class AsyncRuntime:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)



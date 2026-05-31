from __future__ import annotations

import os
import re
import sys
import json
import time
import base64
import hashlib
import shutil
import queue
import random
import sqlite3
import asyncio
import logging
import threading
from contextvars import ContextVar
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import urlparse, urlunparse

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
DEFAULT_PROJECT_OWNER_WALLET = "0x0000000000000000000000000000000000000000"
PROJECT_OWNER_WALLET = (
    os.getenv("PROJECT_OWNER_WALLET")
    or os.getenv("PROJECT_TREASURY")
    or DEFAULT_PROJECT_OWNER_WALLET
).strip()

# Demo wallets can be configured from Railway Variables.
# Supports both DEMO_WALLETS and PROJECT_DEMO_WALLETS for compatibility.
# If neither is set, the project owner wallet is treated as the demo wallet.
_demo_wallets_raw = (
    os.getenv("PROJECT_DEMO_WALLETS")
    or os.getenv("DEMO_WALLETS")
    or PROJECT_OWNER_WALLET
).strip()
PROJECT_DEMO_WALLETS = {
    wallet.strip().lower()
    for wallet in re.split(r"[,\n]+", _demo_wallets_raw)
    if wallet.strip().startswith("0x") and len(wallet.strip()) == 42
}
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
SOCIAL_IMAGE_DIR = RUNTIME_DIR / "social_images"
DB_PATH = RUNTIME_DIR / "app.db"
CONFIG_PATH = RUNTIME_DIR / "config.json"

for p in [RUNTIME_DIR, SESSION_DIR, PROFILE_DIR, SOCIAL_IMAGE_DIR]:
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

_LOG_WALLET_CONTEXT: ContextVar[str | None] = ContextVar("log_wallet_context", default=None)

def set_log_context(wallet_address: str | None):
    value = (wallet_address or "").lower() or None
    return _LOG_WALLET_CONTEXT.set(value)

def clear_log_context(token=None) -> None:
    try:
        if token is not None:
            _LOG_WALLET_CONTEXT.reset(token)
        else:
            _LOG_WALLET_CONTEXT.set(None)
    except Exception:
        _LOG_WALLET_CONTEXT.set(None)

def get_log_context() -> str | None:
    return _LOG_WALLET_CONTEXT.get()

class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            msg = self.format(record)
            self.q.put({"wallet": get_log_context(), "message": msg})
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

    telegram_source_channel: str = ""

    # Primary Telegram channel/group for social posts. This is shown in Login & Cookies
    # next to Telegram login settings, similar to Facebook Target URL.
    telegram_post_channel_url: str = ""

    # Preferred API-based Telegram posting. If these are configured, social posts are sent
    # through Telegram Bot API instead of browser/UI flows. The bot must be admin/member
    # in the target chat/channel. `telegram_bot_chat_ids` can contain one chat_id/@channel per line.
    telegram_bot_token: str = ""
    telegram_bot_chat_ids: str = ""

    # Existing multi-target field is kept for forward/social workflows.
    telegram_target_channels: str = ""
    enable_telegram_forward: bool = False
    enable_telegram_social_post: bool = True

    x_auth_token: str = ""
    x_ct0: str = ""
    # Preferred X API v2 user access token with tweet.write permission.
    # If configured, posting uses POST /2/tweets instead of trying to click the X UI.
    x_api_access_token: str = ""
    enable_x_post: bool = False

    facebook_cookie_json: str = ""
    facebook_target_url: str = ""
    # Preferred Meta Graph API Page publishing. If both values are configured, posting uses
    # /{page-id}/feed instead of trying to click the Facebook composer UI.
    facebook_page_id: str = ""
    facebook_page_access_token: str = ""
    enable_facebook_post: bool = False

    # Web3 subscription settings
    project_owner_wallet: str = PROJECT_OWNER_WALLET
    mantle_rpc_url: str = "https://rpc.mantle.xyz"
    mantlescan_api_url: str = "https://api.etherscan.io/v2/api"
    mantlescan_api_key: str = ""
    monthly_mnt_amount: float = 5.0
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

    # ERC-8004 / BlockScam moderation proof settings
    enable_erc8004_proof: bool = False
    erc8004_rpc_url: str = "https://rpc.mantle.xyz"
    erc8004_agent_registry: str = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
    erc8004_reputation_registry: str = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
    erc8004_validation_registry: str = ""
    erc8004_validator_address: str = ""
    erc8004_agent_id: str = ""
    erc8004_evidence_base_url: str = ""
    erc8004_private_key: str = ""
    erc8004_onchain_min_score: int = 70


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

    # ERC-8004 defaults can be injected from Railway Variables.
    # Important: empty env values do NOT erase per-wallet user settings.
    # This lets the project owner set the default BlockScam Agent/Validation Registry once,
    # while each user only saves their own Proof Writer private key in their workspace.
    def _env_non_empty(name: str, current: str) -> str:
        value = os.getenv(name)
        if value is None:
            return current
        value = value.strip()
        return value if value else current

    cfg.erc8004_rpc_url = _env_non_empty("ERC8004_RPC_URL", cfg.erc8004_rpc_url or cfg.mantle_rpc_url) or cfg.mantle_rpc_url
    cfg.erc8004_agent_registry = _env_non_empty("ERC8004_AGENT_REGISTRY", cfg.erc8004_agent_registry)
    cfg.erc8004_reputation_registry = _env_non_empty("ERC8004_REPUTATION_REGISTRY", cfg.erc8004_reputation_registry)
    cfg.erc8004_validation_registry = _env_non_empty("ERC8004_VALIDATION_REGISTRY", cfg.erc8004_validation_registry)
    cfg.erc8004_validator_address = _env_non_empty("ERC8004_VALIDATOR_ADDRESS", cfg.erc8004_validator_address)
    cfg.erc8004_agent_id = _env_non_empty("ERC8004_AGENT_ID", cfg.erc8004_agent_id)
    cfg.erc8004_evidence_base_url = _env_non_empty("ERC8004_EVIDENCE_BASE_URL", cfg.erc8004_evidence_base_url)

    # Private key override is optional. In public deployments, prefer NOT setting this
    # globally; let each user save a fresh low-balance Proof Writer wallet instead.
    cfg.erc8004_private_key = _env_non_empty("ERC8004_PRIVATE_KEY", cfg.erc8004_private_key)

    enable_env = os.getenv("ENABLE_ERC8004_PROOF")
    if enable_env is not None and enable_env.strip() != "":
        cfg.enable_erc8004_proof = enable_env.strip().lower() in {"1", "true", "yes", "on"}

    min_score_env = os.getenv("ERC8004_ONCHAIN_MIN_SCORE")
    try:
        cfg.erc8004_onchain_min_score = int((min_score_env if min_score_env is not None and min_score_env.strip() else str(cfg.erc8004_onchain_min_score)).strip())
    except Exception:
        cfg.erc8004_onchain_min_score = 70
    try:
        cfg.monthly_mnt_amount = float(os.getenv("MONTHLY_MNT_AMOUNT", str(cfg.monthly_mnt_amount)).strip())
    except Exception:
        cfg.monthly_mnt_amount = 5.0
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

SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "60000"))

def open_sqlite_connection(path: Path) -> sqlite3.Connection:
    """Open SQLite with settings that survive concurrent bot/web writes.

    The dashboard, Telegram forwarder, BlockScam monitor, proof writer, and
    web routes can all touch the same database. Railway volumes also make short
    write collisions more visible. WAL + busy_timeout + small retry loops avoid
    user-facing `database is locked` errors without changing the data model.
    """
    conn = sqlite3.connect(
        path,
        timeout=max(5, SQLITE_BUSY_TIMEOUT_MS / 1000),
        check_same_thread=False,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
    except Exception:
        # Some environments may reject a PRAGMA while another process is opening
        # the DB. The connection timeout/retry path below still protects writes.
        pass
    return conn


def is_sqlite_busy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database is busy" in text or "database table is locked" in text


class AppDB:
    def __init__(self, path: Path):
        self.conn = open_sqlite_connection(path)
        self.lock = threading.RLock()
        self.init()

    def _run(self, fn):
        delay = 0.20
        last_error = None
        with self.lock:
            for attempt in range(8):
                try:
                    return fn()
                except sqlite3.OperationalError as e:
                    if not is_sqlite_busy_error(e):
                        raise
                    last_error = e
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass
                    if attempt >= 7:
                        break
                    time.sleep(delay + random.random() * 0.10)
                    delay = min(2.5, delay * 1.7)
            raise last_error

    def init(self):
        def op():
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
            c.execute("""
            CREATE TABLE IF NOT EXISTS moderation_proofs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proof_hash TEXT UNIQUE,
                report_json TEXT NOT NULL,
                action TEXT,
                chat_hash TEXT,
                user_hash TEXT,
                message_hash TEXT,
                risk_score INTEGER,
                tx_hash TEXT,
                created_at TEXT
            )
            """)
            self.conn.commit()
        self._run(op)

    def is_seen_news(self, uid: str) -> bool:
        def op():
            c = self.conn.cursor()
            c.execute("SELECT 1 FROM seen_news WHERE uid=?", (uid,))
            return c.fetchone() is not None
        return bool(self._run(op))

    def mark_seen_news(self, uid: str):
        def op():
            c = self.conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO seen_news(uid, created_at) VALUES (?, ?)",
                (uid, datetime.now(timezone.utc).isoformat())
            )
            self.conn.commit()
        self._run(op)

    def is_sent_message(self, msg_id: int) -> bool:
        def op():
            c = self.conn.cursor()
            c.execute("SELECT 1 FROM sent_messages WHERE message_id=?", (msg_id,))
            return c.fetchone() is not None
        return bool(self._run(op))

    def mark_sent_message(self, msg_id: int):
        def op():
            c = self.conn.cursor()
            c.execute(
                "INSERT OR IGNORE INTO sent_messages(message_id, created_at) VALUES (?, ?)",
                (msg_id, datetime.now(timezone.utc).isoformat())
            )
            self.conn.commit()
        self._run(op)

    def save_moderation_proof(
        self,
        *,
        proof_hash: str,
        report_json: str,
        action: str,
        chat_hash: str,
        user_hash: str,
        message_hash: str,
        risk_score: int,
        tx_hash: str = "",
    ):
        def op():
            c = self.conn.cursor()
            c.execute(
                """
                INSERT OR IGNORE INTO moderation_proofs(
                    proof_hash, report_json, action, chat_hash, user_hash,
                    message_hash, risk_score, tx_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proof_hash,
                    report_json,
                    action,
                    chat_hash,
                    user_hash,
                    message_hash,
                    int(risk_score or 0),
                    tx_hash or "",
                    datetime.now(timezone.utc).isoformat(),
                )
            )
            self.conn.commit()
        self._run(op)

    def update_moderation_tx(self, proof_hash: str, tx_hash: str):
        def op():
            c = self.conn.cursor()
            c.execute(
                "UPDATE moderation_proofs SET tx_hash=? WHERE proof_hash=?",
                (tx_hash or "", proof_hash)
            )
            self.conn.commit()
        self._run(op)


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


def canonical_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return "0x" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_hash(value: Any, salt: str = "blockscam") -> str:
    return sha256_hex(f"{salt}:{value}")


def redact_message(text: str) -> str:
    text = text or ""
    text = re.sub(r"\b\d+(\.\d+)?\s*(usdt|usd|mnt|bnb|eth|btc)\b", "****", text, flags=re.I)
    text = re.sub(r"@[a-zA-Z0-9_]{3,}", "@***", text)
    text = re.sub(r"https?://\S+", "https://***", text)
    text = re.sub(r"t\.me/\S+", "t.me/***", text, flags=re.I)
    return text[:320]


# =========================================================
# NEWS / WORDPRESS SERVICE
# =========================================================

class NewsWordPressService:
    def __init__(self, cfg_getter):
        self.cfg_getter = cfg_getter
        self.last_post_time = 0
        self.last_uploaded_image_url = ""

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

    def save_social_image(self, img: Optional[bytes], title: str = "social-image") -> str:
        """Persist the generated WordPress image so social posters can attach it too."""
        if not img:
            return ""
        try:
            safe_title = re.sub(r"[^a-zA-Z0-9_-]+", "-", (title or "social-image").strip()).strip("-")[:60]
            digest = hashlib.sha256(img).hexdigest()[:12]
            filename = f"{int(time.time())}-{safe_title or 'social-image'}-{digest}.png"
            path = SOCIAL_IMAGE_DIR / filename
            path.write_bytes(img)
            return str(path)
        except Exception as e:
            logger.warning(f"Could not save social image attachment: {e}")
            return ""

    def upload_image(self, img: Optional[bytes]) -> Optional[int]:
        self.last_uploaded_image_url = ""
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
                payload = r.json()
                media_id = payload.get("id")
                self.last_uploaded_image_url = str(payload.get("source_url") or payload.get("guid", {}).get("rendered") or "")
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
            summary = clean_ai_output(res.choices[0].message.content or "")
            if link and link not in summary:
                summary = f"{summary.rstrip()}\n\n{link}"
            return summary or f"{title}\n\n{plain[:500]}...\n\n{link}"
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
        image_path = self.save_social_image(img, title_new)
        img_id = self.upload_image(img)
        image_url = self.last_uploaded_image_url

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
            "image_path": image_path,
            "image_url": image_url,
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

    def _env_or_cfg(self, cfg, attr: str, env_name: str = "") -> str:
        value = str(getattr(cfg, attr, "") or "").strip()
        if value:
            return value
        return os.getenv(env_name or attr.upper(), "").strip()

    def _shorten_at_word(self, text: str, limit: int) -> str:
        text = re.sub(r"\s+", " ", (text or "").strip())
        if len(text) <= limit:
            return text
        cut = text[: max(0, limit - 1)].rstrip()
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0].rstrip()
        return (cut or text[: max(0, limit - 1)].rstrip()) + "…"

    def _strip_trailing_ellipsis(self, text: str) -> str:
        """Remove AI/browser-looking trailing ellipses from a caption fragment."""
        return re.sub(r"(?:\s*(?:\.{3,}|…))+\s*$", "", (text or "").strip()).strip()

    def _finish_short_sentence(self, text: str) -> str:
        """Make a hard-cut X caption look intentional instead of unfinished."""
        text = self._strip_trailing_ellipsis(text)
        text = re.sub(r"[,:;،，]+\s*$", "", text).strip()
        if text and text[-1] not in ".!?。！？":
            text += "."
        return text

    def _shorten_for_x_body(self, text: str, limit: int) -> str:
        """Shorten X body without adding ellipsis.

        The previous version respected the 280-character limit but appended an
        ellipsis before the link. That made posts look like they were broken or
        auto-truncated. This helper deliberately creates a shorter, finished
        sentence/clause and keeps the link visible on a separate line.
        """
        text = self._strip_trailing_ellipsis(re.sub(r"\s+", " ", (text or "").strip()))
        if len(text) <= limit:
            return text

        window = text[:limit].rstrip()

        # Prefer a complete sentence.
        sentence_positions = [window.rfind(mark) for mark in [".", "!", "?", "。", "！", "？"]]
        sentence_cut = max(sentence_positions)
        if sentence_cut >= max(70, int(limit * 0.45)):
            return self._finish_short_sentence(window[: sentence_cut + 1])

        # Then prefer a natural clause boundary.
        clause_positions = [window.rfind(mark) for mark in [";", ":", "—", "–", ",", "，"]]
        clause_cut = max(clause_positions)
        if clause_cut >= max(70, int(limit * 0.45)):
            return self._finish_short_sentence(window[:clause_cut])

        # Last resort: cut at a word boundary and close the sentence.
        if " " in window:
            window = window.rsplit(" ", 1)[0].rstrip()
        return self._finish_short_sentence(window)

    def _x_post_text(self, text: str) -> str:
        """Create a compact X post and preserve the article link.

        X supports 280 characters, but long text + an image often looks messy in
        the timeline. Keep browser/API posts intentionally short so users never
        see a half-cut caption such as ``...`` before the link.
        """
        text = re.sub(r"\s+", " ", (text or "").strip())
        urls = re.findall(r"https?://\S+", text)
        url = urls[-1].rstrip(".,)") if urls else ""
        body = re.sub(r"https?://\S+", "", text).strip()
        body = self._strip_trailing_ellipsis(body)

        try:
            body_max = int(os.getenv("X_BODY_MAX_CHARS", "155") or "155")
        except Exception:
            body_max = 155
        body_max = max(80, min(body_max, 220))

        if url:
            # Leave room for the URL and spacing even when a real URL is longer
            # than X's t.co counted length.
            absolute_max_body = max(40, 270 - len(url) - 2)
            body = self._shorten_for_x_body(body, min(body_max, absolute_max_body))
            final = f"{body}\n\n{url}".strip() if body else url
        else:
            final = self._shorten_for_x_body(body, min(body_max, 240))

        # Never append ellipsis on X. If the string is still too long because of
        # an unusually long URL, make one more hard cut on the body only.
        if len(final) > 280 and url:
            allowed = max(20, 276 - len(url))
            body = self._shorten_for_x_body(body, allowed)
            final = f"{body}\n\n{url}".strip() if body else url
        return self._strip_trailing_ellipsis(final[:280]).rstrip()

    def _telegram_caption(self, text: str) -> str:
        text = (text or "").strip()
        if len(text) <= 1024:
            return text
        urls = re.findall(r"https?://\S+", text)
        url = urls[-1].rstrip(".,)") if urls else ""
        body = re.sub(r"https?://\S+", "", text).strip()
        if url:
            body = self._shorten_at_word(body, max(80, 1024 - len(url) - 2))
            return f"{body}\n{url}"[:1024].rstrip()
        return self._shorten_at_word(text, 1024)

    def _valid_image_path(self, image_path: str | None) -> str:
        if not image_path:
            return ""
        try:
            path = Path(str(image_path))
            if path.exists() and path.is_file() and path.stat().st_size > 0:
                return str(path)
        except Exception:
            pass
        return ""

    def post_x_api(self, text: str, access_token: str):
        payload = {"text": self._x_post_text(text)}
        r = requests.post(
            "https://api.twitter.com/2/tweets",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"X API post failed: HTTP {r.status_code} | {r.text[:500]}")
        logger.info("✅ Posted to X via API.")
        return r.json()

    def post_facebook_graph_api(self, text: str, page_id: str, page_access_token: str, image_path: str | None = None):
        version = os.getenv("META_GRAPH_API_VERSION", "v24.0").strip() or "v24.0"
        image_path = self._valid_image_path(image_path)

        if image_path:
            url = f"https://graph.facebook.com/{version}/{page_id}/photos"
            with open(image_path, "rb") as f:
                r = requests.post(
                    url,
                    data={
                        "message": (text or "").strip(),
                        "access_token": page_access_token,
                        "published": "true",
                    },
                    files={"source": (Path(image_path).name, f, "image/png")},
                    timeout=60,
                )
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Facebook Graph API photo post failed: HTTP {r.status_code} | {r.text[:500]}")
            logger.info("✅ Posted image + text to Facebook Page via Graph API.")
            return r.json()

        url = f"https://graph.facebook.com/{version}/{page_id}/feed"
        r = requests.post(
            url,
            data={
                "message": (text or "").strip(),
                "access_token": page_access_token,
            },
            timeout=30,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Facebook Graph API post failed: HTTP {r.status_code} | {r.text[:500]}")
        logger.info("✅ Posted to Facebook Page via Graph API.")
        return r.json()

    async def _try_click_or_shortcut(self, page, selectors: List[str], *, platform: str, timeout: int = 7000, allow_shortcut: bool = True) -> bool:
        """Click known publish buttons.

        Browser posting is a fallback path and social UIs change often. We only
        return True when a real visible/enabled publish button was clicked, or
        when shortcut fallback is explicitly allowed for that platform. Facebook
        is intentionally stricter so the log does not say "Posted" when the UI
        only received Ctrl+Enter but did not publish anything.
        """
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                if count <= 0:
                    continue
                for idx in range(count - 1, -1, -1):
                    btn = loc.nth(idx)
                    try:
                        if await btn.is_visible() and await btn.is_enabled():
                            await btn.click()
                            return True
                    except Exception:
                        continue
            except Exception:
                pass

        if not allow_shortcut:
            return False

        try:
            await page.keyboard.press("Control+Enter")
            await page.wait_for_timeout(2500)
            logger.info(f"{platform}: tried Control+Enter publish shortcut as browser fallback.")
            return True
        except Exception:
            pass

        try:
            await page.keyboard.press("Meta+Enter")
            await page.wait_for_timeout(2500)
            logger.info(f"{platform}: tried Meta+Enter publish shortcut as browser fallback.")
            return True
        except Exception:
            return False

    async def _facebook_composer_still_open(self, page) -> bool:
        """Best-effort check that Facebook's composer dialog is still open."""
        selectors = [
            'div[aria-label="Post"][role="button"]',
            'div[aria-label="Đăng"][role="button"]',
            'div[aria-label*="Post"][role="button"]',
            'div[aria-label*="Đăng"][role="button"]',
            'div[role="dialog"] div[role="textbox"]',
            'div[role="dialog"] div[contenteditable="true"]',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).last
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                pass
        return False

    async def _confirm_facebook_publish(self, page, *, timeout_ms: int = 25000) -> bool:
        deadline = time.time() + max(5, timeout_ms / 1000)
        while time.time() < deadline:
            if not await self._facebook_composer_still_open(page):
                return True
            await page.wait_for_timeout(1000)
        return False

    async def _attach_image_if_possible(self, page, image_path: str | None, *, platform: str) -> bool:
        image_path = self._valid_image_path(image_path)
        if not image_path:
            return False

        selectors = [
            'input[data-testid="fileInput"]',
            'input[type="file"][accept*="image"]',
            'input[type="file"]',
        ]

        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.set_input_files(image_path)
                    await page.wait_for_timeout(5000)
                    logger.info(f"🖼️ Attached generated image to {platform} post.")
                    return True
            except Exception:
                pass

        try:
            count = await page.locator('input[type="file"]').count()
            for idx in range(count):
                try:
                    await page.locator('input[type="file"]').nth(idx).set_input_files(image_path)
                    await page.wait_for_timeout(5000)
                    logger.info(f"🖼️ Attached generated image to {platform} post.")
                    return True
                except Exception:
                    continue
        except Exception:
            pass

        logger.warning(f"Could not attach generated image to {platform}; posting text only.")
        return False

    def _facebook_target_variants(self, target: str) -> List[str]:
        """Return desktop/mobile/basic Facebook URL variants for browser posting.

        The regular Facebook desktop composer is React-heavy and changes often.
        Mobile/basic pages are simpler and can expose stable textarea/form names
        such as xc_message/view_post on some profile/group surfaces.
        """
        raw = (target or "").strip() or "https://www.facebook.com/"
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw

        out: List[str] = []

        def add(url: str):
            if url and url not in out:
                out.append(url)

        add(raw)
        try:
            parsed = urlparse(raw)
            host = (parsed.netloc or "").lower()
            if "facebook.com" in host:
                path = parsed.path or "/"
                query = parsed.query or ""
                for mobile_host in ["m.facebook.com", "mbasic.facebook.com", "www.facebook.com"]:
                    add(urlunparse((parsed.scheme or "https", mobile_host, path, "", query, "")))
        except Exception:
            pass

        return out

    async def _save_facebook_debug_artifacts(self, page, label: str = "facebook") -> None:
        """Save a screenshot/HTML snapshot to runtime for debugging failed Facebook UI changes."""
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", label)[:40] or "facebook"
            png_path = RUNTIME_DIR / f"{safe_label}_debug_{stamp}.png"
            html_path = RUNTIME_DIR / f"{safe_label}_debug_{stamp}.html"
            await page.screenshot(path=str(png_path), full_page=True)
            html_path.write_text(await page.content(), encoding="utf-8", errors="ignore")
            logger.warning(f"Saved Facebook debug snapshot: {png_path.name}, {html_path.name}")
        except Exception as e:
            logger.warning(f"Could not save Facebook debug snapshot: {e}")

    async def _find_visible_locator(self, page, selectors: List[str], *, timeout: int = 1500, prefer_last: bool = False):
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                if count <= 0:
                    try:
                        await loc.first.wait_for(timeout=timeout)
                        count = await loc.count()
                    except Exception:
                        continue

                indexes = range(count - 1, -1, -1) if prefer_last else range(count)
                for idx in indexes:
                    candidate = loc.nth(idx)
                    try:
                        if await candidate.is_visible():
                            return candidate
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    def _facebook_composer_needles(self) -> List[str]:
        return [
            "what's on your mind", "what’s on your mind", "write something", "create post", "create a post",
            "post something", "start a post", "photo/video",
            "bạn đang nghĩ gì", "bạn nghĩ gì", "viết gì đó", "tạo bài viết", "ảnh/video",
            "bắt đầu bài viết", "chia sẻ nội dung",
        ]

    async def _facebook_click_by_text_js(self, page, needles: Optional[List[str]] = None) -> str:
        """Click a visible Facebook element by fuzzy text/aria-label matching.

        Facebook's desktop composer is frequently rebuilt and normal CSS selectors
        can miss the actual clickable ancestor. This JS fallback scans visible
        nodes, then clicks the closest clickable ancestor. It is intentionally
        limited to composer-related text to avoid clicking random feed controls.
        """
        needles = [str(x).lower() for x in (needles or self._facebook_composer_needles()) if str(x).strip()]
        try:
            return await page.evaluate(
                """
                (needles) => {
                  const visible = (el) => {
                    if (!el || !el.isConnected) return false;
                    const st = window.getComputedStyle(el);
                    if (!st || st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity || 1) === 0) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 8 && r.height > 8 && r.bottom > 0 && r.right > 0 && r.top < window.innerHeight && r.left < window.innerWidth;
                  };
                  const norm = (s) => String(s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
                  const clickable = (el) => {
                    let cur = el;
                    for (let i = 0; cur && i < 8; i++, cur = cur.parentElement) {
                      const tag = (cur.tagName || '').toLowerCase();
                      const role = cur.getAttribute('role') || '';
                      if (['button','a','textarea'].includes(tag) || role === 'button' || role === 'textbox' || cur.getAttribute('contenteditable') === 'true') return cur;
                    }
                    return el;
                  };
                  const nodes = Array.from(document.querySelectorAll('div,span,a,button,textarea,[role="button"],[role="textbox"],[contenteditable="true"]'));
                  for (const el of nodes) {
                    if (!visible(el)) continue;
                    const hay = norm([el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('placeholder')].filter(Boolean).join(' '));
                    if (!hay) continue;
                    if (needles.some(n => hay.includes(n))) {
                      const target = clickable(el);
                      target.scrollIntoView({block: 'center', inline: 'center'});
                      target.click();
                      return hay.slice(0, 180);
                    }
                  }
                  return '';
                }
                """,
                needles,
            ) or ""
        except Exception:
            return ""

    async def _facebook_find_composer_box(self, page):
        textboxes = [
            'div[role="dialog"] div[role="textbox"][contenteditable="true"]',
            'div[role="dialog"] div[contenteditable="true"]',
            'div[role="dialog"] textarea',
            'form textarea[name="xc_message"]',
            'textarea[name="xc_message"]',
            'textarea[name="status"]',
            'textarea[name="message"]',
            'textarea[name*="message"]',
            'div[role="textbox"][contenteditable="true"]',
            'div[role="textbox"]',
            'div[contenteditable="true"][aria-label*="What" i]',
            'div[contenteditable="true"][aria-label*="mind" i]',
            'div[contenteditable="true"][aria-label*="Write" i]',
            'div[contenteditable="true"][aria-label*="Bạn" i]',
            'div[contenteditable="true"][aria-label*="Viết" i]',
            'div[contenteditable="true"]',
            'textarea',
        ]
        box = await self._find_visible_locator(page, textboxes, timeout=1800, prefer_last=True)
        if box:
            return box

        # Fallback: locate any visible editable node via JS and mark it, then use a stable selector.
        try:
            marked = await page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const st = window.getComputedStyle(el);
                    if (!st || st.visibility === 'hidden' || st.display === 'none') return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 20 && r.height > 15 && r.bottom > 0 && r.right > 0 && r.top < window.innerHeight && r.left < window.innerWidth;
                  };
                  const nodes = Array.from(document.querySelectorAll('div[contenteditable="true"],div[role="textbox"],textarea'));
                  for (let i = nodes.length - 1; i >= 0; i--) {
                    const el = nodes[i];
                    if (!visible(el)) continue;
                    el.setAttribute('data-msp-facebook-composer', '1');
                    el.scrollIntoView({block:'center', inline:'center'});
                    el.click();
                    return true;
                  }
                  return false;
                }
                """
            )
            if marked:
                loc = page.locator('[data-msp-facebook-composer="1"]').last
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
        except Exception:
            pass
        return None

    async def _facebook_click_publish_button(self, page) -> bool:
        selectors = [
            'div[role="dialog"] div[aria-label="Post"][role="button"]',
            'div[role="dialog"] div[aria-label="Đăng"][role="button"]',
            'div[role="dialog"] div[aria-label*="Post" i][role="button"]',
            'div[role="dialog"] div[aria-label*="Đăng" i][role="button"]',
            'div[role="dialog"] div[aria-label*="Publish" i][role="button"]',
            'div[role="dialog"] div[aria-label*="Share" i][role="button"]',
            'div[role="dialog"] div[aria-label*="Chia sẻ" i][role="button"]',
            'div[role="dialog"] div[role="button"]:has-text("Post")',
            'div[role="dialog"] div[role="button"]:has-text("Đăng")',
            'div[role="dialog"] [role="button"]:has-text("Publish")',
            'div[role="dialog"] [role="button"]:has-text("Share")',
            'div[role="dialog"] [role="button"]:has-text("Chia sẻ")',
            'input[name="view_post"]',
            'button[name="view_post"]',
            'input[type="submit"][value="Post"]',
            'input[type="submit"][value="Đăng"]',
            'button:has-text("Post")',
            'button:has-text("Đăng")',
            'button:has-text("Share")',
            'button:has-text("Chia sẻ")',
            'div[aria-label="Post"][role="button"]',
            'div[aria-label="Đăng"][role="button"]',
            'div[aria-label*="Post" i][role="button"]',
            'div[aria-label*="Đăng" i][role="button"]',
        ]
        clicked = await self._try_click_or_shortcut(page, selectors, platform="Facebook", timeout=10000, allow_shortcut=False)
        if clicked:
            return True

        needles = ["post", "đăng", "publish", "share", "chia sẻ"]
        try:
            matched = await page.evaluate(
                """
                (needles) => {
                  const visible = (el) => {
                    const st = window.getComputedStyle(el);
                    if (!st || st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity || 1) === 0) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 18 && r.height > 18 && r.bottom > 0 && r.right > 0 && r.top < window.innerHeight && r.left < window.innerWidth;
                  };
                  const disabled = (el) => el.getAttribute('aria-disabled') === 'true' || el.getAttribute('disabled') !== null;
                  const norm = (s) => String(s || '').toLowerCase().replace(/\\s+/g, ' ').trim();
                  const nodes = Array.from(document.querySelectorAll('div[role="button"],button,input[type="submit"],a[role="button"]'));
                  for (let i = nodes.length - 1; i >= 0; i--) {
                    const el = nodes[i];
                    if (!visible(el) || disabled(el)) continue;
                    const hay = norm([el.innerText, el.textContent, el.getAttribute('aria-label'), el.getAttribute('value')].filter(Boolean).join(' '));
                    if (!hay) continue;
                    if (needles.some(n => hay === n || hay.includes(n))) {
                      el.scrollIntoView({block:'center', inline:'center'});
                      el.click();
                      return hay.slice(0, 120);
                    }
                  }
                  return '';
                }
                """,
                needles,
            ) or ""
            if matched:
                logger.info(f"Facebook publish button clicked by JS fallback: {matched}")
                return True
        except Exception:
            pass
        return False

    async def _fill_facebook_box(self, page, box, text: str) -> None:
        await box.click()
        try:
            tag = (await box.evaluate("el => (el.tagName || '').toLowerCase()")) or ""
        except Exception:
            tag = ""

        if tag in {"textarea", "input"}:
            try:
                await box.fill(text)
                return
            except Exception:
                pass

        try:
            await page.keyboard.insert_text(text)
        except Exception:
            await box.press_sequentially(text, delay=4)

    async def _post_facebook_basic_or_mobile(self, context, text: str, image_path: str | None, target: str) -> bool:
        """Post through mobile/basic Facebook as a non-API fallback.

        This path avoids the desktop React composer when possible. It still uses
        the user's logged-in browser/cookies and only succeeds when Facebook
        exposes a normal posting form on the chosen target surface.
        """
        page = await context.new_page()
        try:
            targets = [u for u in self._facebook_target_variants(target) if "mbasic.facebook.com" in u or "m.facebook.com" in u]
            if not targets:
                targets = self._facebook_target_variants(target)

            for url in targets:
                logger.info(f"📘 Trying Facebook mobile/basic fallback: {url}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                    await page.wait_for_timeout(4500)
                except Exception as e:
                    logger.warning(f"Facebook mobile/basic page open failed for {url}: {e}")
                    continue

                current_url = page.url.lower()
                if "login" in current_url or "checkpoint" in current_url:
                    raise RuntimeError("Facebook is not logged in, cookies expired, or the account is checkpointed. Paste Facebook Cookie JSON again on the dashboard, then click Save.")

                # Some mobile/basic pages show a separate 'Write something' link/button first.
                trigger_selectors = [
                    'a:has-text("Write something")',
                    'a:has-text("What\'s on your mind")',
                    'a:has-text("Create Post")',
                    'a:has-text("Create a post")',
                    'a:has-text("Tạo bài viết")',
                    'a:has-text("Bạn đang nghĩ gì")',
                    'a[href*="composer"]',
                    'a[href*="mbasic_inline_feed_composer"]',
                    'button:has-text("Write something")',
                    'button:has-text("Create Post")',
                    'button:has-text("Tạo bài viết")',
                ]
                trigger = await self._find_visible_locator(page, trigger_selectors, timeout=1200)
                if trigger:
                    try:
                        await trigger.click()
                        await page.wait_for_timeout(3500)
                    except Exception:
                        pass
                else:
                    matched = await self._facebook_click_by_text_js(page)
                    if matched:
                        logger.info(f"Facebook mobile/basic composer opened by text fallback: {matched}")
                        await page.wait_for_timeout(3500)

                textarea_selectors = [
                    'textarea[name="xc_message"]',
                    'textarea[name="status"]',
                    'textarea[name="message"]',
                    'textarea[name*="message"]',
                    'textarea',
                    '[contenteditable="true"]',
                    '[role="textbox"]',
                ]
                box = await self._find_visible_locator(page, textarea_selectors, timeout=2500, prefer_last=False)
                if not box:
                    box = await self._facebook_find_composer_box(page)
                if not box:
                    logger.info(f"Facebook mobile/basic: no composer input found on {url}")
                    continue

                await self._fill_facebook_box(page, box, text)
                await page.wait_for_timeout(800)

                # Image upload on m/mbasic is best-effort. Many Facebook surfaces hide it.
                await self._attach_image_if_possible(page, image_path, platform="Facebook mobile/basic")

                submit_selectors = [
                    'input[name="view_post"]',
                    'button[name="view_post"]',
                    'input[type="submit"][value="Post"]',
                    'input[type="submit"][value="Đăng"]',
                    'button:has-text("Post")',
                    'button:has-text("Đăng")',
                    'input[type="submit"]',
                ]

                submit = await self._find_visible_locator(page, submit_selectors, timeout=2000, prefer_last=True)
                before_url = page.url
                if submit:
                    await submit.click()
                else:
                    if not await self._facebook_click_publish_button(page):
                        logger.info(f"Facebook mobile/basic: no submit button found on {url}")
                        continue
                await page.wait_for_timeout(6000)

                # Basic Facebook may show a second confirmation screen. Click one more clear submit if present.
                second_submit = await self._find_visible_locator(page, submit_selectors, timeout=1200, prefer_last=True)
                if second_submit:
                    try:
                        await second_submit.click()
                        await page.wait_for_timeout(5000)
                    except Exception:
                        pass

                if page.url != before_url or not await self._find_visible_locator(page, textarea_selectors, timeout=800, prefer_last=False):
                    logger.info("✅ Posted to Facebook through mobile/basic fallback.")
                    return True

            return False
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _post_facebook_desktop(self, page, text: str, image_path: str | None, target: str) -> bool:
        """Post through the standard desktop composer with broader selectors."""
        await page.goto(target, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_timeout(6000)

        current_url = page.url.lower()
        if "login" in current_url or "checkpoint" in current_url:
            raise RuntimeError("Facebook is not logged in, cookies expired, or the account is checkpointed. Paste Facebook Cookie JSON again on the dashboard, then click Save.")

        composer_triggers = [
            'div[role="button"]:has-text("What\'s on your mind")',
            'span:has-text("What\'s on your mind")',
            'div[role="button"]:has-text("Write something")',
            'span:has-text("Write something")',
            'div[role="button"]:has-text("Create post")',
            'span:has-text("Create post")',
            'div[role="button"]:has-text("Bạn đang nghĩ gì")',
            'span:has-text("Bạn đang nghĩ gì")',
            'div[role="button"]:has-text("Tạo bài viết")',
            'span:has-text("Tạo bài viết")',
            'div[role="button"]:has-text("Viết gì đó")',
            'span:has-text("Viết gì đó")',
            'div[aria-label*="Create a post" i]',
            'div[aria-label*="Create post" i]',
            'div[aria-label*="Tạo bài viết" i]',
            'div[role="button"][aria-label*="post" i]',
            'div[role="button"][aria-label*="đăng" i]',
            '[data-pagelet*="Composer"] div[role="button"]',
        ]

        opened = False
        trigger = await self._find_visible_locator(page, composer_triggers, timeout=1800, prefer_last=False)
        if trigger:
            try:
                await trigger.click()
                opened = True
            except Exception:
                pass

        if not opened:
            matched = await self._facebook_click_by_text_js(page)
            if matched:
                logger.info(f"Facebook composer opened by text fallback: {matched}")
                opened = True

        if not opened:
            logger.warning("Could not find a clear composer button, trying direct input detection...")

        await page.wait_for_timeout(3500)

        box = await self._facebook_find_composer_box(page)
        if not box:
            logger.warning(f"Facebook desktop: no composer input found. title={await page.title()} url={page.url}")
            return False

        await self._fill_facebook_box(page, box, text)
        await page.wait_for_timeout(1200)
        await self._attach_image_if_possible(page, image_path, platform="Facebook")

        clicked = await self._facebook_click_publish_button(page)
        if not clicked:
            logger.warning("Facebook desktop: composer input was filled but no reliable Post/Đăng button was found.")
            return False

        if not await self._confirm_facebook_publish(page):
            return False

        logger.info("✅ Posted to Facebook and composer closed.")
        return True

    async def post_x(self, text: str, image_path: str | None = None):
        cfg = self.cfg_getter()
        if not cfg.enable_x_post:
            logger.info("X posting is disabled, skipping X.")
            return

        x_api_access_token = self._env_or_cfg(cfg, "x_api_access_token", "X_API_ACCESS_TOKEN")
        if x_api_access_token:
            if self._valid_image_path(image_path):
                logger.info("X API token is configured; posting text only because media upload is not enabled in this lightweight API path.")
            await asyncio.to_thread(self.post_x_api, text, x_api_access_token)
            return

        logger.info("🐦 Posting to X with Playwright browser fallback...")

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

                short_text = self._x_post_text(text)

                await box.click()
                await page.keyboard.insert_text(short_text)
                await page.wait_for_timeout(1000)
                await self._attach_image_if_possible(page, image_path, platform="X")

                buttons = [
                    'button[data-testid="tweetButton"]',
                    'button[data-testid="tweetButtonInline"]'
                ]

                clicked = await self._try_click_or_shortcut(page, buttons, platform="X", timeout=7000)

                if not clicked:
                    raise RuntimeError("Could not find or trigger the X post button. Prefer X API access token for reliable posting.")

                await page.wait_for_timeout(5000)
                logger.info("✅ Posted to X.")

            finally:
                await context.close()

    async def post_facebook(self, text: str, image_path: str | None = None, image_url: str | None = None):
        cfg = self.cfg_getter()
        if not cfg.enable_facebook_post:
            logger.info("Facebook posting is disabled, skipping Facebook.")
            return

        page_id = self._env_or_cfg(cfg, "facebook_page_id", "FACEBOOK_PAGE_ID")
        page_access_token = self._env_or_cfg(cfg, "facebook_page_access_token", "FACEBOOK_PAGE_ACCESS_TOKEN")
        if page_id and page_access_token:
            await asyncio.to_thread(self.post_facebook_graph_api, text, page_id, page_access_token, image_path)
            return

        target = cfg.facebook_target_url.strip() or "https://www.facebook.com/"
        logger.info("📘 Posting to Facebook with Playwright browser fallback...")

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
                # The most stable non-API path is mobile/basic first because it
                # often exposes normal textarea/form elements. If that surface is
                # unavailable for the target, fall back to the desktop composer.
                mobile_ok = await self._post_facebook_basic_or_mobile(context, text, image_path, target)
                if mobile_ok:
                    return

                logger.info("📘 Mobile/basic Facebook fallback did not expose a posting form; trying desktop composer...")
                desktop_ok = await self._post_facebook_desktop(page, text, image_path, target)
                if desktop_ok:
                    return

                await self._save_facebook_debug_artifacts(page, "facebook_post_failed")
                raise RuntimeError(
                    "Could not find a reliable Facebook composer/input/post button. "
                    "Check that the Facebook Target URL is a profile/page/group where this account can post, "
                    "refresh Facebook Cookie JSON, or open the Facebook login browser once to save a persistent session."
                )

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

    def session_path(self, session_name: str, purpose: str = "") -> str:
        """Return a Telethon session base path.

        The dashboard login writes the primary session. Long-running features
        such as Telegram forwarding, BlockScam, and one-off social posting must
        not open the exact same SQLite session file at the same time; Telethon
        can otherwise raise `database is locked`. For worker purposes we create
        a purpose-specific copy of the authorized base session and connect with
        that copy. The user still logs in only once.
        """
        clean = re.sub(r"[^a-zA-Z0-9_.-]+", "_", (session_name or "forward_session").strip()) or "forward_session"
        if not purpose:
            return str(SESSION_DIR / clean)

        purpose_clean = re.sub(r"[^a-zA-Z0-9_.-]+", "_", purpose.strip()) or "worker"
        base_no_ext = SESSION_DIR / clean
        worker_no_ext = SESSION_DIR / f"{clean}_{purpose_clean}"
        base_file = Path(str(base_no_ext) + ".session")
        worker_file = Path(str(worker_no_ext) + ".session")

        try:
            if base_file.exists():
                should_copy = (not worker_file.exists()) or (base_file.stat().st_mtime > worker_file.stat().st_mtime)
                if should_copy:
                    tmp_file = Path(str(worker_file) + ".tmp")
                    copied = False
                    try:
                        src = sqlite3.connect(str(base_file), timeout=max(5, SQLITE_BUSY_TIMEOUT_MS / 1000), uri=False)
                        dst = sqlite3.connect(str(tmp_file), timeout=max(5, SQLITE_BUSY_TIMEOUT_MS / 1000), uri=False)
                        with dst:
                            src.backup(dst)
                        src.close()
                        dst.close()
                        copied = True
                    except Exception:
                        try:
                            shutil.copy2(base_file, tmp_file)
                            copied = True
                        except Exception as copy_error:
                            logger.warning(f"Could not prepare Telegram {purpose_clean} session copy: {copy_error}")
                    if copied:
                        tmp_file.replace(worker_file)
        except Exception as e:
            logger.warning(f"Telegram session copy check failed for {purpose_clean}: {e}")

        return str(worker_no_ext)

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

    def _valid_image_path(self, image_path: str | None) -> str:
        if not image_path:
            return ""
        try:
            path = Path(str(image_path))
            if path.exists() and path.is_file() and path.stat().st_size > 0:
                return str(path)
        except Exception:
            pass
        return ""

    def _telegram_caption(self, text: str) -> str:
        text = (text or "").strip()
        if len(text) <= 1024:
            return text
        urls = re.findall(r"https?://\S+", text)
        url = urls[-1].rstrip(".,)") if urls else ""
        body = re.sub(r"https?://\S+", "", text).strip()
        if url:
            limit = max(80, 1024 - len(url) - 2)
            short = body[: max(0, limit - 1)].rstrip()
            if " " in short:
                short = short.rsplit(" ", 1)[0].rstrip()
            return f"{short}…\n{url}"[:1024].rstrip()
        return text[:1023].rstrip() + "…"

    def send_telegram_bot_api(self, text: str, bot_token: str, chat_ids: List[str], image_path: str | None = None):
        if not bot_token or not chat_ids:
            return
        image_path = self._valid_image_path(image_path)
        for i, chat_id in enumerate(chat_ids, start=1):
            if image_path:
                with open(image_path, "rb") as f:
                    r = requests.post(
                        f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                        data={
                            "chat_id": chat_id,
                            "caption": self._telegram_caption(text),
                        },
                        files={"photo": (Path(image_path).name, f, "image/png")},
                        timeout=60,
                    )
            else:
                r = requests.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": (text or "")[:4096],
                        "disable_web_page_preview": False,
                    },
                    timeout=30,
                )
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Telegram Bot API send failed for {chat_id}: HTTP {r.status_code} | {r.text[:500]}")
            logger.info(f"✅ Telegram Bot API social [{i}/{len(chat_ids)}] {chat_id}")

    async def send_social_post(self, text: str, image_path: str | None = None):
        cfg = self.cfg_getter()
        if not cfg.enable_telegram_social_post:
            return

        bot_token = (getattr(cfg, "telegram_bot_token", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        bot_chat_ids = parse_lines(getattr(cfg, "telegram_bot_chat_ids", "") or os.getenv("TELEGRAM_BOT_CHAT_IDS", ""))
        if bot_token and bot_chat_ids:
            await asyncio.to_thread(self.send_telegram_bot_api, text, bot_token, bot_chat_ids, image_path)
            return

        targets = []
        if getattr(cfg, "telegram_post_channel_url", "").strip():
            targets.append(cfg.telegram_post_channel_url.strip())
        targets.extend(parse_lines(cfg.telegram_target_channels))
        targets = list(dict.fromkeys([x for x in targets if x]))

        if not targets:
            logger.info("No Telegram targets configured for social posting.")
            return

        api_id, api_hash = self.api_pair()
        client = TelegramClient(
            self.session_path(cfg.telegram_session_name, "social_post"),
            api_id,
            api_hash
        )

        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError("Telegram session is not logged in. Log in once from the Telegram Login tab, then try again.")

        logger.info(f"✅ Telegram social session ready. Sending to {len(targets)} target(s).")

        for i, target in enumerate(targets, start=1):
            try:
                logger.info(f"📨 Telegram social [{i}/{len(targets)}] {target}")
                valid_image = self._valid_image_path(image_path)
                if valid_image:
                    await client.send_file(target, valid_image, caption=self._telegram_caption(text))
                else:
                    await client.send_message(target, text)
                logger.info(f"✅ Posted to Telegram social target: {target}")
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
            self.session_path(cfg.telegram_session_name, "forward_loop"),
            api_id,
            api_hash
        )

        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            logger.error("Telegram forward session is not logged in. Log in once from the Telegram Login tab, then restart the bot.")
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

                # Telegram channels can contain service/empty messages, for example
                # channel-created notices, pinned-message notices, or unsupported
                # objects. These messages have no text and no downloadable media.
                # Sending them to another chat causes Telegram to return:
                # "The message cannot be empty unless a file is provided".
                # Mark them as handled so the forward loop does not retry forever.
                for msg in messages:
                    if stop_event.is_set():
                        break

                    if not msg or not getattr(msg, "id", None):
                        continue

                    if db.is_sent_message(msg.id):
                        continue

                    logger.info(f"🆕 Forward msg ID {msg.id}")

                    media_files = []
                    caption = (getattr(msg, "text", None) or getattr(msg, "message", None) or "").strip()
                    album_message_ids = []

                    if getattr(msg, "grouped_id", None):
                        if msg.grouped_id in handled_groups:
                            continue

                        album = await client.get_messages(
                            source,
                            min_id=msg.id - 10,
                            max_id=msg.id + 10
                        )

                        album_msgs = [m for m in album if getattr(m, "grouped_id", None) == msg.grouped_id]
                        # Keep album files in Telegram order instead of newest-first.
                        album_msgs = sorted(album_msgs, key=lambda m: m.id)
                        album_message_ids = [m.id for m in album_msgs if getattr(m, "id", None)]
                        handled_groups.add(msg.grouped_id)

                        caption = ""
                        for m in album_msgs:
                            if getattr(m, "media", None):
                                try:
                                    file = await m.download_media(file=str(RUNTIME_DIR))
                                    if file and os.path.exists(file):
                                        media_files.append(file)
                                except Exception as e:
                                    logger.warning(f"Could not download album media msg={m.id}: {e}")
                            m_text = (getattr(m, "text", None) or getattr(m, "message", None) or "").strip()
                            if m_text and not caption:
                                caption = m_text

                    else:
                        if getattr(msg, "media", None):
                            try:
                                file = await msg.download_media(file=str(RUNTIME_DIR))
                                if file and os.path.exists(file):
                                    media_files.append(file)
                            except Exception as e:
                                logger.warning(f"Could not download media msg={msg.id}: {e}")

                    final_text = (caption or "").strip()
                    if not media_files and not final_text:
                        logger.info(f"⏭️ Skip empty/unsupported Telegram message ID {msg.id}")
                        db.mark_sent_message(msg.id)
                        for mid in album_message_ids:
                            db.mark_sent_message(mid)
                        continue

                    for target in targets:
                        try:
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
                    for mid in album_message_ids:
                        db.mark_sent_message(mid)

                await asyncio.sleep(60)

            except Exception as e:
                logger.error(f"Forward loop error: {e}")
                await asyncio.sleep(10)

        await client.disconnect()
        logger.info("Telegram forward loop stopped.")


# =========================================================
# ERC-8004 BLOCKSCAM PROOF + BLOCK SCAM BASIC
# =========================================================

class ERC8004ProofService:
    """Create local moderation evidence and optionally anchor its hash on-chain.

    The Telegram message, user id, and chat id are never written on-chain. The
    chain transaction only receives a bytes32 proof hash that can later be
    compared against the local evidence report.
    """

    VALIDATION_REGISTRY_ABI = [
        {
            "type": "function",
            "name": "validationRequest",
            "stateMutability": "nonpayable",
            "inputs": [
                {"name": "validatorAddress", "type": "address"},
                {"name": "agentId", "type": "uint256"},
                {"name": "requestURI", "type": "string"},
                {"name": "requestHash", "type": "bytes32"},
            ],
            "outputs": [],
        }
    ]

    def __init__(self, cfg_getter):
        self.cfg_getter = cfg_getter

    def build_report(
        self,
        *,
        chat: str,
        message_id: int,
        sender_id: Any,
        text: str,
        action: str,
        risk_score: int,
        risk_reason: str,
        matched_rules: List[str],
        bot_version: str = "blockscam-telegram-v2",
    ) -> Tuple[Dict[str, Any], str, str]:
        cfg = self.cfg_getter()
        chat_hash = safe_hash(chat, "blockscam-chat")
        user_hash = safe_hash(sender_id or "unknown", "blockscam-user")
        message_hash = safe_hash(f"{chat}:{message_id}:{text}", "blockscam-message")

        report = {
            "type": "telegram_moderation_action",
            "standard": "ERC-8004-compatible-offchain-evidence",
            "agentRegistry": getattr(cfg, "erc8004_agent_registry", ""),
            "agentId": str(getattr(cfg, "erc8004_agent_id", "") or ""),
            "platform": "telegram",
            "chatHash": chat_hash,
            "userHash": user_hash,
            "messageHash": message_hash,
            "telegramMessageId": int(message_id),
            "action": action,
            "riskScore": int(risk_score or 0),
            "riskReason": risk_reason,
            "matchedRules": matched_rules,
            "originalMessageRedacted": redact_message(text),
            "botVersion": bot_version,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        report_json = canonical_json(report)
        proof_hash = sha256_hex(report_json)
        return report, report_json, proof_hash

    def evidence_uri(self, proof_hash: str) -> str:
        cfg = self.cfg_getter()
        base = (getattr(cfg, "erc8004_evidence_base_url", "") or "").strip().rstrip("/")
        if not base:
            return ""
        return f"{base}/proof/{proof_hash}"

    def save_local_proof(self, report: Dict[str, Any], report_json: str, proof_hash: str, tx_hash: str = "") -> None:
        db.save_moderation_proof(
            proof_hash=proof_hash,
            report_json=report_json,
            action=str(report.get("action") or ""),
            chat_hash=str(report.get("chatHash") or ""),
            user_hash=str(report.get("userHash") or ""),
            message_hash=str(report.get("messageHash") or ""),
            risk_score=int(report.get("riskScore") or 0),
            tx_hash=tx_hash or "",
        )

    def submit_validation_request_if_ready(self, proof_hash: str) -> str:
        cfg = self.cfg_getter()
        if not getattr(cfg, "enable_erc8004_proof", False):
            return ""

        rpc_url = (getattr(cfg, "erc8004_rpc_url", "") or getattr(cfg, "mantle_rpc_url", "") or "").strip()
        private_key = (getattr(cfg, "erc8004_private_key", "") or "").strip()
        validation_registry = (getattr(cfg, "erc8004_validation_registry", "") or "").strip()
        validator_address = (getattr(cfg, "erc8004_validator_address", "") or "").strip()
        agent_id_raw = (getattr(cfg, "erc8004_agent_id", "") or "").strip()

        if not all([rpc_url, private_key, validation_registry, validator_address, agent_id_raw]):
            logger.info("ERC-8004 proof is enabled but RPC/private key/validation registry/validator/agent id is missing. Saved local proof only.")
            return ""

        try:
            from web3 import Web3
            from eth_account import Account as EthAccount

            w3 = Web3(Web3.HTTPProvider(rpc_url))
            if not w3.is_connected():
                logger.warning("ERC-8004 RPC is not reachable. Saved local proof only.")
                return ""

            account = EthAccount.from_key(private_key)
            agent_id = int(agent_id_raw)
            request_hash_bytes = bytes.fromhex(proof_hash.replace("0x", ""))
            request_uri = self.evidence_uri(proof_hash)

            contract = w3.eth.contract(
                address=Web3.to_checksum_address(validation_registry),
                abi=self.VALIDATION_REGISTRY_ABI,
            )
            tx = contract.functions.validationRequest(
                Web3.to_checksum_address(validator_address),
                agent_id,
                request_uri,
                request_hash_bytes,
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 260000,
                "gasPrice": w3.eth.gas_price,
                "chainId": w3.eth.chain_id,
            })
            signed = account.sign_transaction(tx)
            raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
            tx_hash = w3.eth.send_raw_transaction(raw_tx).hex()
            logger.info(f"✅ ERC-8004 validationRequest sent: {tx_hash}")
            return tx_hash
        except ImportError:
            logger.warning("web3 is not installed. Add web3 to requirements.txt and redeploy to anchor proofs on-chain.")
            return ""
        except Exception as e:
            logger.warning(f"ERC-8004 validationRequest failed; saved local proof only: {e}")
            return ""


class BlockScamService:
    def __init__(self, cfg_getter, telegram_service: TelegramService, proof_service: Optional[ERC8004ProofService] = None):
        self.cfg_getter = cfg_getter
        self.telegram_service = telegram_service
        self.proof_service = proof_service or ERC8004ProofService(cfg_getter)

    def contains_risky(self, text: str) -> bool:
        return self.rule_based_risk(text)[0]

    def rule_based_risk(self, text: str) -> Tuple[bool, str, int, List[str]]:
        cfg = self.cfg_getter()
        keys = parse_lines(cfg.block_scam_keywords)
        norm = normalize_text(text)
        compact = re.sub(r"[\s\W_]+", "", norm)
        matched: List[str] = []

        # Link-only posts are common for legitimate channel/article forwards.
        # Treat URL/Telegram links as context, not as a delete reason by itself.
        link_like_keywords = {"t.me", "t.me/", "joinchat", "http", "https", "http://", "https://"}

        for k in keys:
            nk = normalize_text(k)
            nkc = re.sub(r"[\s\W_]+", "", nk)
            label = f"keyword:{k}"
            is_link_context = nk.strip().lower() in link_like_keywords or nk.startswith("http")
            if nk and nk in norm:
                matched.append("LINK_CONTEXT" if is_link_context else label)
                continue
            if nkc and nkc in compact:
                matched.append("LINK_CONTEXT" if is_link_context else label)
                continue

        strong_patterns = [
            ("FREE_USDT_LURE", r"(mien phi|miễn phí|free).{0,40}(usdt|usd|mnt|tien|tiền|airdrop|thuong|thưởng)"),
            ("CONTACT_ME_PATTERN", r"(lien he toi|liên hệ tôi|inbox|ib|pm|dm|nhan tin rieng|nhắn tin riêng)"),
            ("EARN_MONEY_LURE", r"(kiem|kiếm|nhan|nhận).{0,40}(\d+).{0,20}(usdt|usd|mnt|trieu|triệu)"),
            ("LINK_CONTEXT", r"(t\.me/|joinchat|https?://)"),
            ("URGENT_INVITE", r"(nhanh tay|co hoi|cơ hội|slot|suat|suất|bao loi|bao lời)"),
        ]
        for name, pattern in strong_patterns:
            if re.search(pattern, norm, re.I):
                matched.append(name)

        matched = list(dict.fromkeys(matched))
        if not matched:
            return False, "clean_by_rules", 0, []

        matched_set = set(matched)

        # A normal article/channel post with only a link must never be deleted.
        # It can still be escalated when combined with giveaway, phishing, contact-me,
        # or aggressive invitation patterns.
        dangerous = matched_set - {"LINK_CONTEXT"}
        if not dangerous:
            return False, "link_only_allowed", 20, matched

        score = min(100, 52 + len(dangerous) * 12 + (6 if "LINK_CONTEXT" in matched_set else 0))
        if "FREE_USDT_LURE" in matched_set and ("CONTACT_ME_PATTERN" in matched_set or "LINK_CONTEXT" in matched_set):
            score = max(score, 94)
        if "EARN_MONEY_LURE" in matched_set and ("CONTACT_ME_PATTERN" in matched_set or "LINK_CONTEXT" in matched_set):
            score = max(score, 92)
        if "CONTACT_ME_PATTERN" in matched_set and "URGENT_INVITE" in matched_set and "LINK_CONTEXT" in matched_set:
            score = max(score, 88)

        return score >= 70, ",".join(matched), score, matched

    def should_skip_protected_message(self, msg: Any) -> Tuple[bool, str]:
        """Avoid moderating trusted channel forwards and service messages.

        Linked-channel reposts, forwarded channel articles, and outgoing posts are
        often legitimate project content. BlockScam should protect the community
        from unsolicited user spam, not delete posts that an admin/channel already
        forwarded into the monitored chat.
        """
        skip_forwarded = os.getenv("BLOCKSCAM_SKIP_FORWARDED_POSTS", "1").strip().lower() not in {"0", "false", "no", "off"}
        if getattr(msg, "action", None):
            return True, "telegram_service_message"
        if getattr(msg, "out", False):
            return True, "outgoing_or_own_message"
        if skip_forwarded and getattr(msg, "fwd_from", None):
            return True, "forwarded_or_linked_channel_post"
        if skip_forwarded and getattr(msg, "post", False):
            return True, "channel_post"
        return False, ""

    def ai_risk(self, text: str) -> Tuple[bool, str, int, List[str]]:
        cfg = self.cfg_getter()
        if not getattr(cfg, "enable_block_scam_ai", True):
            return False, "ai_disabled", 0, []
        if not (cfg.openai_api_key or os.getenv("OPENAI_API_KEY", "")):
            return False, "missing_openai_key", 0, []
        if len((text or "").strip()) < 8:
            return False, "too_short", 0, []

        model = getattr(cfg, "block_scam_ai_model", "gpt-5-nano") or "gpt-5-nano"
        threshold = int(getattr(cfg, "block_scam_ai_threshold", 7) or 7)
        prompt = f"""
You are a Telegram anti-scam moderation classifier for a crypto/trading community.

Classify the message below. Return JSON only with:
- risk_score: integer 0-10
- should_delete: boolean
- reason: short reason
- matched_rules: array of short rule names

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
                max_completion_tokens=180,
            )
            data = json.loads(clean_ai_output(res.choices[0].message.content or "{}"))
            score_10 = int(data.get("risk_score", 0) or 0)
            reason = str(data.get("reason", "ai_classified"))[:140]
            matched = data.get("matched_rules") or []
            if not isinstance(matched, list):
                matched = [str(matched)]
            score = max(0, min(100, score_10 * 10))
            should_delete = bool(data.get("should_delete", False)) or score_10 >= threshold
            return should_delete, reason, score, [f"ai:{str(x)[:60]}" for x in matched[:8]]
        except Exception as e:
            logger.warning(f"BlockScam AI check failed: {e}")
            return False, "ai_error", 0, []

    def should_delete_message(self, text: str) -> Tuple[bool, str, int, List[str]]:
        risky, reason, score, matched = self.rule_based_risk(text)
        if risky:
            return True, reason, score, matched
        ai_delete, ai_reason, ai_score, ai_matched = self.ai_risk(text)
        if ai_delete:
            return True, f"ai_score:{ai_score} {ai_reason}", ai_score, ai_matched or ["AI_SCAM_DETECTION"]
        return False, f"allowed ai_score:{ai_score} {ai_reason}", ai_score, ai_matched

    async def block_sender(self, client: TelegramClient, chat: str, sender_id: Any) -> bool:
        if not sender_id:
            logger.warning("No sender_id found, cannot block user.")
            return False
        try:
            await client.edit_permissions(chat, sender_id, view_messages=False)
            logger.info(f"✅ Blocked user from Telegram chat: {sender_id}")
            return True
        except Exception as e:
            logger.warning(f"edit_permissions block failed: {e}")
        try:
            await client.kick_participant(chat, sender_id)
            logger.info(f"✅ Kicked user from Telegram chat: {sender_id}")
            return True
        except Exception as e:
            logger.warning(f"kick_participant failed: {e}")
            return False

    async def run_basic_monitor(self, stop_event: threading.Event):
        cfg = self.cfg_getter()
        if not cfg.enable_block_scam:
            logger.info("BlockScam is disabled.")
            return

        api_id, api_hash = self.telegram_service.api_pair()
        client = TelegramClient(
            self.telegram_service.session_path(cfg.telegram_session_name, "blockscam"),
            api_id,
            api_hash
        )

        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            logger.error("Telegram BlockScam session is not logged in. Log in once from the Telegram Login tab, then restart the bot.")
            return

        chats = parse_lines(cfg.block_scam_target_chats)
        if not chats:
            logger.warning("BlockScam is enabled but no chats are configured for scanning.")
            await client.disconnect()
            return

        logger.info("🛡️ BlockScam monitor started with moderation proof support.")

        while not stop_event.is_set():
            try:
                cfg = self.cfg_getter()
                min_onchain_score = int(getattr(cfg, "erc8004_onchain_min_score", 70) or 70)
                block_user_min_score = int(os.getenv("BLOCKSCAM_BLOCK_USER_MIN_SCORE", "90") or "90")
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

                            protected, protected_reason = self.should_skip_protected_message(msg)
                            if protected:
                                db.mark_sent_message(key)
                                logger.info(f"🛡️ BlockScam skipped protected message ID {msg.id}: {protected_reason}")
                                continue

                            db.mark_sent_message(key)

                            should_delete, risk_reason, risk_score, matched_rules = self.should_delete_message(text)
                            if not should_delete:
                                continue

                            logger.warning(f"🚫 Suspicious scam in {chat}: score={risk_score} reason={risk_reason} | {text[:100]}")

                            action = "detect_only"
                            deleted = False
                            blocked = False
                            sender_id = getattr(msg, "sender_id", None)

                            try:
                                await client.delete_messages(chat, msg.id)
                                deleted = True
                                action = "delete_message"
                                logger.info("✅ Scam message deleted.")
                            except Exception as e:
                                logger.warning(f"Could not delete message: {e}")

                            if risk_score >= block_user_min_score:
                                blocked = await self.block_sender(client, chat, sender_id)
                                if blocked and deleted:
                                    action = "delete_message_and_block_user"
                                elif blocked:
                                    action = "block_user"
                                elif deleted:
                                    action = "delete_message_block_failed"

                            try:
                                report, report_json, proof_hash = self.proof_service.build_report(
                                    chat=str(chat),
                                    message_id=int(msg.id),
                                    sender_id=sender_id,
                                    text=text,
                                    action=action,
                                    risk_score=int(risk_score or 0),
                                    risk_reason=risk_reason,
                                    matched_rules=matched_rules,
                                )
                                tx_hash = ""
                                if risk_score >= min_onchain_score:
                                    tx_hash = self.proof_service.submit_validation_request_if_ready(proof_hash)
                                else:
                                    logger.info(f"ERC-8004 on-chain proof skipped: score={risk_score} is below min={min_onchain_score}. Saved local proof only.")
                                self.proof_service.save_local_proof(report, report_json, proof_hash, tx_hash)
                                if tx_hash:
                                    db.update_moderation_tx(proof_hash, tx_hash)
                                logger.info(f"🧾 BlockScam proof created | score={risk_score} | hash={proof_hash} | tx={tx_hash or 'local-only'}")
                            except Exception as e:
                                logger.warning(f"Could not create BlockScam proof: {e}")

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



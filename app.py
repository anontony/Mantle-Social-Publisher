from __future__ import annotations

import time
import html
import json
import secrets
import sqlite3
import threading
import os
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, Any, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, JSONResponse
from eth_account import Account
from eth_account.messages import encode_defunct

from core import (
    APP_NAME,
    AppConfig,
    ConfigStore,
    CATEGORY_KEYWORDS,
    log_queue,
    logger,
    NewsWordPressService,
    PlaywrightSocialService,
    TelegramService,
    BlockScamService,
    AsyncRuntime,
    DB_PATH,
    PROJECT_OWNER_WALLET,
    PROJECT_DEMO_WALLETS,
    CONTENT_LANGUAGES,
    apply_server_subscription_settings,
)

app = FastAPI(title=APP_NAME)

cfg = apply_server_subscription_settings(ConfigStore.load())
runtime = AsyncRuntime()
stop_event = threading.Event()
bot_thread: threading.Thread | None = None

news_service = NewsWordPressService(lambda: cfg)
social_service = PlaywrightSocialService(lambda: cfg)
telegram_service = TelegramService(lambda: cfg)
block_scam_service = BlockScamService(lambda: cfg, telegram_service)

log_buffer: list[str] = []
MAX_LOGS = 500


# =========================================================
# WEB3 AUTH / SUBSCRIPTION DB
# =========================================================

def auth_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db() -> None:
    with auth_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS web3_nonces (
            address TEXT PRIMARY KEY,
            nonce TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS web3_users (
            address TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_login_at TEXT NOT NULL,
            subscription_expires_at TEXT,
            last_payment_tx TEXT,
            last_payment_amount_wei TEXT,
            last_payment_at TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS web3_sessions (
            token TEXT PRIMARY KEY,
            address TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS user_configs (
            address TEXT PRIMARY KEY,
            config_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)
        conn.commit()


init_auth_db()


# =========================================================
# PER-USER CONFIG / SERVICE STATE
# =========================================================

def app_config_to_dict(c: AppConfig) -> dict:
    return dict(c.__dict__)


def app_config_from_dict(data: dict) -> AppConfig:
    valid = set(AppConfig.__dataclass_fields__.keys())
    clean = {k: v for k, v in (data or {}).items() if k in valid}
    try:
        c = AppConfig(**clean)
        apply_server_subscription_settings(c)
        if getattr(c, "content_language", "English") not in CONTENT_LANGUAGES:
            c.content_language = "English"
        return c
    except Exception:
        base = AppConfig()
        for k, v in clean.items():
            try:
                setattr(base, k, v)
            except Exception:
                pass
        apply_server_subscription_settings(base)
        if getattr(base, "content_language", "English") not in CONTENT_LANGUAGES:
            base.content_language = "English"
        return base


def default_config_for_user(address: str) -> AppConfig:
    c = apply_server_subscription_settings(AppConfig())
    short = normalize_address(address)[:10].replace('0x', '')
    c.telegram_session_name = f"session_{short}"
    return c


def ensure_user_config(address: str) -> AppConfig:
    a = normalize_address(address)
    with auth_conn() as conn:
        row = conn.execute("SELECT config_json FROM user_configs WHERE lower(address)=lower(?)", (a,)).fetchone()
        if row:
            try:
                return app_config_from_dict(json.loads(row["config_json"]))
            except Exception:
                pass
        c = default_config_for_user(a)
        conn.execute(
            "INSERT OR REPLACE INTO user_configs(address, config_json, updated_at) VALUES (?, ?, ?)",
            (a, json.dumps(app_config_to_dict(c), ensure_ascii=False), now_iso()),
        )
        conn.commit()
        return c


def load_config_for_user(user: Optional[dict]) -> AppConfig:
    if user and user.get("address"):
        return ensure_user_config(user["address"])
    return cfg


def save_config_for_user(address: str, c: AppConfig) -> None:
    a = normalize_address(address)
    apply_server_subscription_settings(c)
    with auth_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_configs(address, config_json, updated_at) VALUES (?, ?, ?)",
            (a, json.dumps(app_config_to_dict(c), ensure_ascii=False, indent=2), now_iso()),
        )
        conn.commit()
    state = user_service_state.get(a)
    if state:
        state["cfg"] = c


def activate_config_for_user(user: Optional[dict]) -> AppConfig:
    # The HTML render helpers were written against a module-level cfg object.
    # On every request we swap that object to the currently logged-in wallet's
    # config so each wallet sees its own saved keys, cookies, channels, etc.
    global cfg
    cfg = load_config_for_user(user)
    return cfg


user_service_state: dict[str, dict[str, Any]] = {}
user_bots: dict[str, dict[str, Any]] = {}


def get_user_services(address: str) -> dict[str, Any]:
    a = normalize_address(address)
    current_cfg = ensure_user_config(a)
    state = user_service_state.get(a)
    if not state:
        holder = {"cfg": current_cfg}
        state = {
            "cfg": current_cfg,
            "holder": holder,
            "news": NewsWordPressService(lambda h=holder: h["cfg"]),
            "social": PlaywrightSocialService(lambda h=holder: h["cfg"]),
            "telegram": TelegramService(lambda h=holder: h["cfg"]),
        }
        state["block"] = BlockScamService(lambda h=holder: h["cfg"], state["telegram"])
        user_service_state[a] = state
    else:
        state["cfg"] = current_cfg
        state["holder"]["cfg"] = current_cfg
    return state


def current_user_address(request: Request) -> Optional[str]:
    user = get_current_user(request)
    if not user:
        return None
    return normalize_address(user["address"])


def require_active_user(request: Request, tab: str = "profile") -> tuple[Optional[dict], Optional[RedirectResponse]]:
    user = get_current_user(request)
    if user_is_active(user):
        return user, None
    logger.warning("Subscription required before using this feature.")
    return user, RedirectResponse(f"/?tab={tab}&msg=subrequired", status_code=303)


def normalize_address(address: str) -> str:
    address = (address or "").strip()
    if not address.startswith("0x") or len(address) != 42:
        raise ValueError("Invalid wallet address.")
    return Account.from_key("0x" + "1" * 64).w3.to_checksum_address(address) if False else address.lower()


def username_from_address(address: str) -> str:
    a = normalize_address(address)
    return f"user_{a[:6]}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def subscription_days() -> int:
    try:
        return max(1, int(os.getenv("SUBSCRIPTION_DAYS", "30").strip()))
    except Exception:
        return 30


def is_demo_wallet(address: Optional[str]) -> bool:
    if not address:
        return False
    try:
        return normalize_address(address).lower() in PROJECT_DEMO_WALLETS
    except Exception:
        return False


def grant_demo_subscription(address: str) -> None:
    a = normalize_address(address)
    if not is_demo_wallet(a):
        return
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=subscription_days())
    with auth_conn() as conn:
        conn.execute(
            """
            UPDATE web3_users
            SET subscription_expires_at=?, last_payment_tx=?, last_payment_amount_wei=?, last_payment_at=?
            WHERE lower(address)=lower(?)
            """,
            (expires.isoformat(), "demo-access", "0", now.isoformat(), a),
        )
        conn.commit()


def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get("msp_session")
    if not token:
        return None
    with auth_conn() as conn:
        row = conn.execute(
            """
            SELECT u.* FROM web3_sessions s
            JOIN web3_users u ON lower(u.address)=lower(s.address)
            WHERE s.token=? AND s.expires_at>?
            """,
            (token, now_iso()),
        ).fetchone()
        return dict(row) if row else None


def user_is_active(user: Optional[dict]) -> bool:
    if not user:
        return False
    if is_demo_wallet(user.get("address")):
        return True
    exp = parse_iso(user.get("subscription_expires_at"))
    return bool(exp and exp > datetime.now(timezone.utc))


def subscription_window(user: Optional[dict]) -> tuple[Optional[datetime], Optional[datetime]]:
    if not user:
        return None, None
    paid_at = parse_iso(user.get("last_payment_at"))
    expires_at = parse_iso(user.get("subscription_expires_at"))
    if not expires_at:
        return paid_at, None
    if not paid_at:
        paid_at = expires_at - timedelta(days=subscription_days())
    return paid_at, expires_at


def user_credit_balance(user: Optional[dict]) -> int:
    """Return remaining monthly credits based on subscription time left."""
    credit_total = max(1, int(getattr(cfg, "monthly_credit_amount", 100) or 100))
    if is_demo_wallet(user.get("address") if user else None):
        return credit_total
    if not user_is_active(user):
        return 0
    start, expires = subscription_window(user)
    if not expires:
        return 0
    total_seconds = subscription_days() * 24 * 3600
    now = datetime.now(timezone.utc)
    remaining = max(0, (expires - now).total_seconds())
    value = int((remaining / total_seconds) * credit_total)
    return max(0, min(credit_total, value))


def user_credit_percent(user: Optional[dict]) -> int:
    total = max(1, int(getattr(cfg, "monthly_credit_amount", 100) or 100))
    return int((user_credit_balance(user) / total) * 100) if user_is_active(user) else 0


def user_days_remaining(user: Optional[dict]) -> int:
    if is_demo_wallet(user.get("address") if user else None):
        return subscription_days()
    if not user_is_active(user):
        return 0
    _, expires = subscription_window(user)
    if not expires:
        return 0
    seconds = max(0, (expires - datetime.now(timezone.utc)).total_seconds())
    return int((seconds + 86399) // 86400)


def mnt_to_wei(amount: float | str) -> int:
    return int(Decimal(str(amount)) * Decimal(10) ** 18)


def short_addr(address: str) -> str:
    a = normalize_address(address)
    return f"{a[:6]}...{a[-4:]}"


def _explorer_api_candidates() -> list[str]:
    """Return explorer API URLs to try.

    Etherscan-compatible V1 chain-specific endpoints such as api.mantlescan.xyz/api
    now return a V1 deprecation error for account/txlist on many explorers.
    Etherscan API V2 uses the unified endpoint plus chainid=5000 for Mantle.
    Keep the dashboard field for compatibility, but always try the V2 endpoint when
    the configured URL looks like an old V1 endpoint or returns a deprecation error.
    """
    configured = (cfg.mantlescan_api_url or "").strip()
    default_v2 = "https://api.etherscan.io/v2/api"
    urls: list[str] = []
    if configured:
        urls.append(configured)
    if default_v2 not in urls:
        urls.append(default_v2)
    return urls


def _call_explorer_txlist(api_url: str, address: str) -> list[dict]:
    params = {
        "chainid": 5000,
        "module": "account",
        "action": "txlist",
        "address": normalize_address(address),
        "startblock": 0,
        "endblock": 999999999,
        "page": 1,
        "offset": 100,
        "sort": "desc",
    }

    api_key = (cfg.mantlescan_api_key or os.getenv("ETHERSCAN_API_KEY") or os.getenv("MANTLESCAN_API_KEY") or "").strip()
    if api_key:
        params["apikey"] = api_key

    r = __import__("requests").get(api_url, params=params, timeout=30)
    data = r.json()
    result = data.get("result")

    if isinstance(result, list):
        return result

    message = str(data.get("message") or "")
    result_text = str(result or "")
    if "deprecated V1 endpoint" in result_text or "deprecated V1 endpoint" in message:
        raise RuntimeError("DEPRECATED_V1_ENDPOINT")

    raise RuntimeError(f"Explorer API did not return a transaction list: {result_text or message or data}")


def fetch_mantle_payments(address: str) -> list[dict]:
    owner = PROJECT_OWNER_WALLET.lower()

    tx_list: list[dict] | None = None
    last_error: Exception | None = None

    for api_url in _explorer_api_candidates():
        try:
            tx_list = _call_explorer_txlist(api_url, address)
            if "etherscan.io/v2" in api_url:
                logger.info("✅ Payment check using Etherscan API V2 with chainid=5000.")
            else:
                logger.info(f"✅ Payment check using explorer API: {api_url}")
            break
        except Exception as e:
            last_error = e
            if str(e) == "DEPRECATED_V1_ENDPOINT":
                logger.warning(f"Explorer API endpoint is deprecated, retrying Etherscan API V2: {api_url}")
                continue
            logger.warning(f"Explorer API request failed: {api_url} | {e}")
            continue

    if tx_list is None:
        raise RuntimeError(f"Payment check failed. Last explorer error: {last_error}")

    required = mnt_to_wei(cfg.monthly_mnt_amount)
    payments = []
    for tx in tx_list:
        try:
            if str(tx.get("from", "")).lower() != normalize_address(address):
                continue
            if str(tx.get("to", "")).lower() != owner:
                continue
            if str(tx.get("isError", "0")) not in ("0", "False", "false", ""):
                continue
            value = int(str(tx.get("value") or "0"))
            if value < required:
                continue
            ts = int(str(tx.get("timeStamp") or "0"))
            paid_at = datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.now(timezone.utc)
            payments.append({
                "hash": tx.get("hash") or tx.get("transactionHash") or "",
                "value": value,
                "paid_at": paid_at,
            })
        except Exception:
            continue
    return payments


def refresh_subscription(address: str) -> dict:
    a = normalize_address(address)
    if is_demo_wallet(a):
        grant_demo_subscription(a)
        return {"active": True, "expires_at": (datetime.now(timezone.utc) + timedelta(days=subscription_days())).isoformat(), "tx": "demo-access", "demo": True}
    payments = fetch_mantle_payments(a)
    if not payments:
        return {"active": False, "message": "No valid monthly payment found on Mantle Mainnet."}
    latest = max(payments, key=lambda x: x["paid_at"])
    expires = latest["paid_at"] + timedelta(days=subscription_days())
    with auth_conn() as conn:
        conn.execute(
            """
            UPDATE web3_users
            SET subscription_expires_at=?, last_payment_tx=?, last_payment_amount_wei=?, last_payment_at=?
            WHERE lower(address)=lower(?)
            """,
            (expires.isoformat(), latest["hash"], str(latest["value"]), latest["paid_at"].isoformat(), a),
        )
        conn.commit()
    return {"active": expires > datetime.now(timezone.utc), "expires_at": expires.isoformat(), "tx": latest["hash"]}


def require_active_redirect(request: Request, tab: str = "profile") -> Optional[RedirectResponse]:
    user = get_current_user(request)
    if user_is_active(user):
        return None
    logger.warning("Subscription required before using this feature.")
    return RedirectResponse(f"/?tab={tab}", status_code=303)


# =========================================================
# LOG / CONFIG HELPERS
# =========================================================

def drain_logs() -> None:
    while True:
        try:
            msg = log_queue.get_nowait()
            log_buffer.append(msg)
            if len(log_buffer) > MAX_LOGS:
                del log_buffer[: len(log_buffer) - MAX_LOGS]
        except Exception:
            break


def set_cfg_field(name: str, value: Any) -> None:
    if hasattr(cfg, name):
        setattr(cfg, name, value)


def save_from_form(data: Dict[str, Any]) -> None:
    """Update only fields that exist in the submitted tab form.

    This keeps the dashboard tab-based: saving the X/Facebook tab will not wipe
    WordPress, Telegram, BlockScam, or category values.
    """
    text_fields = [
        "openai_api_key", "wp_url", "wp_jwt", "crypto_panic", "custom_topic_filter", "content_language",
        "ai_text_model", "image_policy", "image_model", "image_quality", "image_size",
        "block_scam_ai_model",
        "telegram_api_id", "telegram_api_hash", "telegram_session_name", "telegram_phone",
        "telegram_source_channel", "telegram_target_channels",
        "x_auth_token", "x_ct0", "facebook_cookie_json", "facebook_target_url",
        "block_scam_keywords", "block_scam_target_chats", "wp_publish_status",
    ]
    for field in text_fields:
        if field in data:
            value = str(data.get(field) or "")
            if field == "content_language" and value not in CONTENT_LANGUAGES:
                value = "English"
            if field == "image_policy" and value not in ["off", "high_score_only", "every_post"]:
                value = "high_score_only"
            if field == "image_quality" and value not in ["low", "medium", "high"]:
                value = "low"
            if field == "image_size" and value not in ["1024x1024", "1536x1024", "1024x1536"]:
                value = "1536x1024"
            if field == "image_model" and value not in ["gpt-image-2", "gpt-image-1-mini", "gpt-image-1"]:
                value = "gpt-image-2"
            if field == "ai_text_model" and value not in ["gpt-5-nano", "gpt-5-mini"]:
                value = "gpt-5-nano"
            if field == "block_scam_ai_model" and value not in ["gpt-5-nano", "gpt-5-mini"]:
                value = "gpt-5-nano"
            set_cfg_field(field, value)
    apply_server_subscription_settings(cfg)

    int_defaults = {"min_score": 7, "post_interval_seconds": 17280, "posts_per_day": 5, "recent_hours": 6, "image_min_score": 9, "block_scam_ai_threshold": 7}
    for field, default in int_defaults.items():
        if field in data:
            try:
                set_cfg_field(field, int(str(data.get(field, default)).strip()))
            except Exception:
                set_cfg_field(field, default)


    bool_fields = [
        "create_image", "enable_ai_scoring", "enable_telegram_forward", "enable_telegram_social_post",
        "enable_x_post", "enable_facebook_post", "enable_block_scam", "enable_block_scam_ai",
    ]
    for field in bool_fields:
        marker = f"bool__{field}"
        if marker in data:
            set_cfg_field(field, data.get(field) in ("on", "true", "1", True))

    if "category_form" in data:
        selected = [cat for cat in CATEGORY_KEYWORDS if data.get(f"cat_{cat}") in ("on", "true", "1", True)]
        cfg.selected_categories = selected or ["Crypto"]

    logger.info("💾 Configuration updated from web dashboard.")


# =========================================================
# SERVICES
# =========================================================

async def publish_socials(summary: str):
    tasks = []
    if cfg.enable_telegram_social_post:
        tasks.append(telegram_service.send_social_post(summary))
    if cfg.enable_x_post:
        tasks.append(social_service.post_x(summary))
    if cfg.enable_facebook_post:
        tasks.append(social_service.post_facebook(summary))

    for task in tasks:
        try:
            await task
        except Exception as e:
            logger.error(f"Social posting error: {e}")


def seconds_between_posts(c: AppConfig) -> int:
    try:
        posts = int(getattr(c, "posts_per_day", 5) or 5)
    except Exception:
        posts = 5
    posts = max(1, min(24, posts))
    return max(3600, int(86400 / posts))


def bot_worker():
    next_wp_time = 0
    runtime.submit(telegram_service.forward_loop(stop_event))
    runtime.submit(block_scam_service.run_basic_monitor(stop_event))

    while not stop_event.is_set():
        try:
            now = time.time()
            if now >= next_wp_time:
                result = news_service.create_and_post_once()
                if result:
                    fut = runtime.submit(publish_socials(result["summary"]))
                    try:
                        fut.result(timeout=300)
                    except Exception as e:
                        logger.error(f"Social publishing error: {e}")
                next_wp_time = now + seconds_between_posts(cfg)
            time.sleep(5)
        except Exception as e:
            logger.error(f"Bot worker error: {e}")
            time.sleep(5)

    logger.info("Bot stopped.")


async def publish_socials_for_state(summary: str, state: dict[str, Any]):
    c = state["holder"]["cfg"]
    tasks = []
    if c.enable_telegram_social_post:
        tasks.append(state["telegram"].send_social_post(summary))
    if c.enable_x_post:
        tasks.append(state["social"].post_x(summary))
    if c.enable_facebook_post:
        tasks.append(state["social"].post_facebook(summary))
    for task in tasks:
        try:
            await task
        except Exception as e:
            logger.error(f"Social posting error: {e}")


def bot_worker_for_user(address: str, stop_evt: threading.Event):
    a = normalize_address(address)
    state = get_user_services(a)
    next_wp_time = 0
    runtime.submit(state["telegram"].forward_loop(stop_evt))
    runtime.submit(state["block"].run_basic_monitor(stop_evt))

    while not stop_evt.is_set():
        try:
            # Reload this wallet's saved configuration before each cycle.
            state["holder"]["cfg"] = ensure_user_config(a)
            c = state["holder"]["cfg"]
            now = time.time()
            if now >= next_wp_time:
                result = state["news"].create_and_post_once()
                if result:
                    fut = runtime.submit(publish_socials_for_state(result["summary"], state))
                    try:
                        fut.result(timeout=300)
                    except Exception as e:
                        logger.error(f"Social publishing error: {e}")
                next_wp_time = now + seconds_between_posts(c)
            time.sleep(5)
        except Exception as e:
            logger.error(f"User bot worker error for {short_addr(a)}: {e}")
            time.sleep(5)

    logger.info(f"Bot stopped for {short_addr(a)}.")


# =========================================================
# HTML HELPERS
# =========================================================

def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def checked(name: str) -> str:
    return "checked" if bool(getattr(cfg, name, False)) else ""


def selected(value: str, current: str) -> str:
    return "selected" if value == current else ""


def secret_input(name: str, value: Any, placeholder: str = "") -> str:
    field_id = "secret_" + re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
    return (
        f'<div class="secret-control">'
        f'<input id="{field_id}" class="secret-field" name="{esc(name)}" type="password" '
        f'value="{esc(value)}" placeholder="{esc(placeholder)}" autocomplete="off" spellcheck="false">'
        f'<button class="secret-toggle" type="button" data-target="{field_id}" aria-label="Show or hide value">👁</button>'
        f'</div>'
    )


def secret_textarea(name: str, value: Any, placeholder: str = "") -> str:
    field_id = "secret_" + re.sub(r"[^a-zA-Z0-9_\-]", "_", name)
    return (
        f'<div class="secret-control secret-control-textarea">'
        f'<textarea id="{field_id}" class="secret-field secret-masked" name="{esc(name)}" '
        f'placeholder="{esc(placeholder)}" autocomplete="off" spellcheck="false">{esc(value)}</textarea>'
        f'<button class="secret-toggle" type="button" data-target="{field_id}" aria-label="Show or hide value">👁</button>'
        f'</div>'
    )


def bot_status(user: Optional[dict] = None) -> str:
    if user and user.get("address"):
        state = user_bots.get(normalize_address(user["address"]))
        if state and state.get("thread") and state["thread"].is_alive() and not state["stop_event"].is_set():
            return "Running"
        return "Stopped"
    return "Running" if bot_thread and bot_thread.is_alive() and not stop_event.is_set() else "Stopped"


def nav_item(tab: str, label: str, current: str) -> str:
    active = "active" if tab == current else ""
    return f'<a class="nav {active}" href="/?tab={tab}">{label}</a>'


def page_shell(tab: str, title: str, content: str, message: str = "", user: Optional[dict] = None) -> str:
    drain_logs()
    logs = html.escape("\n".join(log_buffer[-160:]))
    status = bot_status(user)
    status_class = "running" if status == "Running" else "stopped"
    msg_html = f'<div class="toast">{esc(message)}</div>' if message else ""
    wallet_badge = f'<div class="wallet-pill">{esc(user.get("username", ""))}<br><span>{esc(short_addr(user.get("address", "")))}</span></div>' if user else '<div class="wallet-pill off">Wallet<br><span>Not connected</span></div>'

    return f"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(APP_NAME)}</title>
<style>
:root {{
  --bg0:#08070c;
  --bg1:#11101a;
  --bg2:#191124;
  --panel:rgba(20,18,30,.82);
  --side:rgba(16,14,24,.86);
  --text:#f8f4ff;
  --muted:#a79ab8;
  --line:rgba(255,255,255,.10);
  --field:rgba(255,255,255,.065);
  --pink:#ff4ecd;
  --pink2:#ff007a;
  --purple:#8b5cf6;
  --blue:#38bdf8;
  --green:#22c55e;
  --red:#fb7185;
  --shadow:0 24px 80px rgba(0,0,0,.42);
  --shadow2:0 12px 30px rgba(255,0,122,.18);
}}
*{{box-sizing:border-box}}
html{{scroll-behavior:smooth}}
body{{
  margin:0;
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
  color:var(--text);
  background:
    radial-gradient(circle at 12% 8%,rgba(255,0,122,.18),transparent 30%),
    radial-gradient(circle at 82% 0%,rgba(124,58,237,.18),transparent 34%),
    radial-gradient(circle at 72% 78%,rgba(56,189,248,.10),transparent 34%),
    linear-gradient(135deg,var(--bg0),var(--bg1) 48%,var(--bg2));
  min-height:100vh;
}}
body:before{{
  content:"";
  position:fixed;
  inset:0;
  pointer-events:none;
  background-image:linear-gradient(rgba(255,255,255,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.035) 1px,transparent 1px);
  background-size:42px 42px;
  mask-image:linear-gradient(to bottom,rgba(0,0,0,.42),transparent 76%);
}}
.layout{{display:flex;min-height:100vh;position:relative;z-index:1}}
.sidebar{{
  width:260px;
  background:var(--side);
  backdrop-filter:blur(26px);
  -webkit-backdrop-filter:blur(26px);
  padding:28px 18px;
  position:fixed;
  inset:18px auto 18px 18px;
  border:1px solid var(--line);
  box-shadow:var(--shadow);
  border-radius:30px;
}}
.brand{{
  font-size:32px;
  font-weight:900;
  letter-spacing:-.04em;
  margin:2px 0 4px 0;
  text-align:center;
  background:linear-gradient(90deg,var(--pink2),var(--purple));
  -webkit-background-clip:text;
  background-clip:text;
  color:transparent;
}}
.subbrand{{font-size:13px;text-align:center;color:var(--muted);margin-bottom:26px;font-weight:700}}
.nav-section{{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#7d718f;font-weight:950;margin:20px 10px 8px}}
.nav{{
  display:flex;align-items:center;justify-content:center;min-height:48px;text-decoration:none;color:#d8c9ee;
  background:rgba(255,255,255,.045);border:1px solid var(--line);padding:13px 14px;margin:10px 0;text-align:center;border-radius:18px;font-weight:800;box-shadow:0 8px 20px rgba(0,0,0,.18);transition:transform .16s ease,box-shadow .16s ease,background .16s ease;
}}
.nav:hover{{transform:translateY(-1px);background:rgba(255,255,255,.08);box-shadow:0 12px 26px rgba(255,0,122,.16)}}
.nav.active{{color:#fff;background:linear-gradient(135deg,var(--pink2),var(--purple));box-shadow:0 16px 34px rgba(255,0,122,.22)}}
.main{{margin-left:296px;flex:1;padding:36px 38px 26px;max-width:1420px}}
.header{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:24px}}
h1{{font-size:42px;line-height:1.05;margin:0 0 10px 0;letter-spacing:-.055em}}
.desc{{font-size:16px;color:var(--muted);margin:0;max-width:850px;line-height:1.55}}
.status{{background:rgba(255,255,255,.055);backdrop-filter:blur(18px);border:1px solid var(--line);padding:14px 18px;min-width:190px;text-align:center;box-shadow:var(--shadow2);border-radius:22px;color:var(--muted);font-weight:800}}
.status b{{display:inline-flex;margin-top:6px;padding:7px 13px;border-radius:999px;background:rgba(255,255,255,.08)}}
.status b.running{{color:var(--green);box-shadow:0 8px 18px rgba(22,163,74,.12)}}
.status b.stopped{{color:var(--red);box-shadow:0 8px 18px rgba(239,68,68,.12)}}
.wallet-pill{{margin-top:12px;background:rgba(255,255,255,.055);border:1px solid var(--line);padding:10px 14px;border-radius:18px;color:#fff;font-weight:900;text-align:center}}
.wallet-pill span{{display:block;margin-top:3px;color:var(--muted);font-size:12px;font-weight:800}}
.wallet-pill.off{{color:var(--red)}}
.card{{position:relative;background:var(--panel);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid var(--line);box-shadow:var(--shadow);padding:22px;margin:18px 0;border-radius:28px;overflow:hidden}}
.card:before{{content:"";position:absolute;inset:0 0 auto 0;height:4px;background:linear-gradient(90deg,var(--pink2),var(--pink),var(--purple),var(--blue));opacity:.9;pointer-events:none}}
.card>*{{position:relative;z-index:1}}
.card h2,.card h3{{margin:4px 0 18px 0;font-size:21px;letter-spacing:-.025em}}
.profile-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:14px 0}}
.metric{{background:rgba(255,255,255,.055);border:1px solid var(--line);padding:16px;border-radius:22px}}
.metric span{{display:block;color:var(--muted);font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.06em}}
.metric b{{display:block;margin-top:8px;font-size:18px;overflow-wrap:anywhere}}
.workflow{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-top:14px}}
.step{{background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:24px;padding:16px;min-height:132px}}
.step strong{{display:inline-flex;width:30px;height:30px;align-items:center;justify-content:center;border-radius:999px;background:linear-gradient(135deg,var(--pink2),var(--purple));margin-bottom:10px}}
.step h4{{margin:0 0 7px;font-size:15px}}
.step p{{font-size:13px;color:var(--muted);margin:0;line-height:1.45}}
fieldset{{border:1px solid var(--line);padding:16px;margin:14px 0;background:rgba(255,255,255,.035);border-radius:24px}}
legend{{font-size:14px;padding:0 8px;color:var(--muted);font-weight:900}}
.grid{{display:grid;grid-template-columns:230px minmax(0,1fr);gap:13px 16px;align-items:center}}
label{{font-size:15px;font-weight:750;color:#e8dcf8}}
input,textarea,select{{width:100%;padding:13px 15px;border:1px solid var(--line);background:var(--field);color:var(--text);font-size:14px;border-radius:16px;outline:none;transition:border .16s ease,box-shadow .16s ease,background .16s ease}}
.secret-control{{position:relative;width:100%}}
.secret-control input,.secret-control textarea{{padding-right:56px}}
.secret-control textarea.secret-masked{{-webkit-text-security:disc;text-security:disc}}
.secret-toggle{{position:absolute;right:8px;top:50%;transform:translateY(-50%);width:38px;min-height:34px;padding:0;border-radius:12px;background:rgba(255,255,255,.075);box-shadow:none;font-size:16px;line-height:1}}
.secret-control-textarea .secret-toggle{{top:22px;transform:none}}
.secret-toggle.is-visible{{background:linear-gradient(135deg,var(--pink2),var(--purple));color:#fff}}
input:focus,textarea:focus,select:focus{{border-color:rgba(255,0,122,.70);box-shadow:0 0 0 5px rgba(255,0,122,.14);background:rgba(255,255,255,.095)}}
textarea{{min-height:106px;font-family:Inter,ui-sans-serif,system-ui,"Segoe UI",Arial,sans-serif;resize:vertical;line-height:1.45}}
.checkrow{{margin:14px 0 8px;display:flex;flex-wrap:wrap;gap:10px 14px}}
.checkrow label{{display:inline-flex;align-items:center;gap:8px;margin:0;padding:10px 13px;background:rgba(255,255,255,.055);border:1px solid var(--line);border-radius:999px}}
.checkrow input{{width:auto;margin:0;accent-color:var(--pink2)}}
.actions{{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-top:14px}}
button,.button{{border:1px solid var(--line);background:rgba(255,255,255,.075);color:#f5edff;padding:13px 22px;font-weight:900;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;min-height:46px;border-radius:999px;box-shadow:0 12px 26px rgba(0,0,0,.26);transition:transform .16s ease,box-shadow .16s ease,filter .16s ease}}
button.primary,.button.primary{{background:linear-gradient(135deg,var(--pink2),var(--purple));color:#fff;box-shadow:0 16px 34px rgba(255,0,122,.22)}}
button.danger{{background:linear-gradient(135deg,#ff5b73,#9f1239);color:white;box-shadow:0 16px 30px rgba(190,18,60,.20)}}
button:hover,.button:hover{{transform:translateY(-1px);filter:brightness(1.02)}}
pre{{background:#07060b;color:#f5e8ff;border:1px solid var(--line);padding:18px;overflow:auto;min-height:360px;max-height:520px;white-space:pre-wrap;border-radius:22px;box-shadow:inset 0 1px 0 rgba(255,255,255,.08)}}
.toast{{background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.28);padding:13px 16px;margin:0 0 16px 0;font-weight:900;border-radius:18px;color:#bbf7d0}}
a{{color:#ff8bd8;font-weight:800}}
option{{background:#14121e;color:var(--text)}}
::placeholder{{color:#766985}}
.help{{font-size:13px;color:var(--muted);margin-top:8px}}

.mobile-topbar{{display:none}}
.mobile-overlay{{display:none}}
.mobile-title{{font-size:15px;font-weight:950;letter-spacing:-.02em}}
.mobile-menu-btn{{width:44px;min-height:44px;padding:0;border-radius:15px;font-size:20px}}
.mobile-wallet{{font-size:12px;color:var(--muted);font-weight:900;max-width:112px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:right}}

@media(max-width:920px){{
  body{{background-attachment:fixed;padding-bottom:18px}}
  .layout{{display:block;min-height:100vh}}
  .mobile-topbar{{
    display:flex;position:sticky;top:0;z-index:50;align-items:center;justify-content:space-between;gap:12px;
    margin:0;padding:12px 14px;background:rgba(8,7,12,.84);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);
    border-bottom:1px solid var(--line);box-shadow:0 12px 30px rgba(0,0,0,.24);
  }}
  .mobile-overlay{{
    display:block;position:fixed;inset:0;z-index:58;background:rgba(0,0,0,.55);opacity:0;pointer-events:none;transition:opacity .18s ease;
  }}
  body.menu-open .mobile-overlay{{opacity:1;pointer-events:auto}}
  .sidebar{{
    position:fixed;z-index:60;top:10px;bottom:10px;left:10px;right:auto;width:min(86vw,330px);height:auto;
    transform:translateX(calc(-100% - 22px));transition:transform .22s ease;margin:0;padding:22px 16px;border-radius:28px;overflow-y:auto;
  }}
  body.menu-open .sidebar{{transform:translateX(0)}}
  .brand{{font-size:28px;text-align:left;margin-left:8px}}
  .subbrand{{text-align:left;margin-left:8px;margin-bottom:16px}}
  .nav-section{{margin-top:16px}}
  .nav{{justify-content:flex-start;text-align:left;min-height:46px;margin:8px 0;border-radius:17px}}
  .main{{margin-left:0;padding:18px 14px 22px;max-width:none;width:100%}}
  .header{{display:block;margin-bottom:14px}}
  .header>.status,.header>div:last-child{{display:none}}
  h1{{font-size:32px;line-height:1.08;margin-top:4px}}
  .desc{{font-size:14px;line-height:1.45}}
  .card{{padding:18px;margin:14px 0;border-radius:24px}}
  .card h2,.card h3{{font-size:19px;margin-bottom:14px}}
  .grid{{display:block}}
  .grid label{{display:block;margin:14px 0 7px;font-size:14px}}
  input,textarea,select{{font-size:16px;padding:14px 15px;border-radius:15px}}
  textarea{{min-height:124px}}
  fieldset{{padding:14px;border-radius:22px;margin:12px 0}}
  .actions{{display:grid;grid-template-columns:1fr;gap:10px;width:100%}}
  button,.button{{width:100%;min-height:48px;padding:13px 16px}}
  .checkrow{{display:grid;grid-template-columns:1fr;gap:9px}}
  .checkrow label{{border-radius:16px;justify-content:flex-start}}
  pre{{min-height:240px;max-height:360px;font-size:12px;padding:14px;border-radius:18px}}
  .profile-grid,.workflow{{grid-template-columns:1fr!important}}
  .metric,.step{{border-radius:20px}}
  .secret-toggle{{width:40px;min-height:38px}}
}}

@media(max-width:420px){{
  .main{{padding-left:10px;padding-right:10px}}
  .mobile-topbar{{padding-left:10px;padding-right:10px}}
  h1{{font-size:28px}}
  .card{{padding:16px;border-radius:22px}}
  .mobile-wallet{{max-width:92px}}
}}
</style>
</head>
<body>
<div class="mobile-topbar">
  <button class="mobile-menu-btn" type="button" id="mobileMenuBtn" aria-label="Open menu">☰</button>
  <div class="mobile-title">Mantle Social Publisher</div>
  <div class="mobile-wallet">{esc(short_addr(user.get("address", ""))) if user else "Guest"}</div>
</div>
<div class="mobile-overlay" id="mobileOverlay"></div>
<div class="layout">
  <aside class="sidebar">
    <div class="brand">Mantle</div>
    <div class="subbrand">Social Publisher</div>
    <div class="nav-section">Account</div>
    {nav_item('home', 'Home', tab)}
    {nav_item('profile', 'User Profile', tab)}
    <div class="nav-section">Automation</div>
    {nav_item('wordpress', 'RSS / WordPress', tab)}
    {nav_item('social', 'Social Posting', tab)}
    {nav_item('forward', 'Telegram Forward', tab)}
    {nav_item('blockscam', 'BlockScam', tab)}
    <div class="nav-section">Settings</div>
    {nav_item('login', 'Login & Cookies', tab)}
    {nav_item('log', 'System Logs', tab)}
  </aside>
  <main class="main">
    <div class="header">
      <div>
        <h1>{esc(title)}</h1>
        <p class="desc">RSS → AI WordPress articles → social summaries → Telegram / X / Facebook posting → Telegram forwarding → scam filtering.</p>
      </div>
      <div><div class="status">Bot Status<br><b class="{status_class}">{status}</b></div>{wallet_badge}</div>
    </div>
    {msg_html}
    {content}
    {'' if tab == 'log' else f'<div class="card"><h3>Logs</h3><pre>{logs}</pre><p><a href="/logs" target="_blank">Open raw logs</a></p></div>'}
  </main>
</div>

<script>
document.addEventListener('DOMContentLoaded', () => {{
  const menuBtn = document.getElementById('mobileMenuBtn');
  const overlay = document.getElementById('mobileOverlay');
  const closeMenu = () => document.body.classList.remove('menu-open');
  if (menuBtn) menuBtn.addEventListener('click', () => document.body.classList.toggle('menu-open'));
  if (overlay) overlay.addEventListener('click', closeMenu);
  document.querySelectorAll('.sidebar .nav').forEach((link) => link.addEventListener('click', closeMenu));

  document.querySelectorAll('.secret-toggle').forEach((button) => {{
    button.addEventListener('click', () => {{
      const targetId = button.getAttribute('data-target');
      const field = document.getElementById(targetId);
      if (!field) return;

      if (field.tagName === 'TEXTAREA') {{
        const hidden = field.classList.toggle('secret-masked');
        button.classList.toggle('is-visible', !hidden);
        button.textContent = hidden ? '👁' : '🙈';
        return;
      }}

      const hidden = field.getAttribute('type') !== 'text';
      field.setAttribute('type', hidden ? 'text' : 'password');
      button.classList.toggle('is-visible', hidden);
      button.textContent = hidden ? '🙈' : '👁';
    }});
  }});
}});
</script>
</body>
</html>
"""


def profile_content(user: Optional[dict]) -> str:
    owner = PROJECT_OWNER_WALLET
    amount_wei = str(mnt_to_wei(cfg.monthly_mnt_amount))
    active = user_is_active(user)
    is_demo = is_demo_wallet(user.get("address") if user else None)
    credit = user_credit_balance(user)
    credit_percent = user_credit_percent(user)
    days_left = user_days_remaining(user)
    connected = bool(user)
    username = user.get("username") if user else "Guest"
    wallet = short_addr(user.get("address")) if user else "Not connected"
    exp = "Demo access" if is_demo else (user.get("subscription_expires_at") if user and user.get("subscription_expires_at") else "Not active")
    tx = "demo-access" if is_demo else (user.get("last_payment_tx") if user and user.get("last_payment_tx") else "None")

    if connected:
        access_title = "Demo access active" if is_demo else ("Full access unlocked" if active else "Subscription required")
        access_note = "This demo wallet includes the full monthly plan for project review." if is_demo else ("Your monthly access is active. Credit Balance decays from 100% to 0% over the monthly period." if active else "Pay the monthly Mantle plan, then refresh your Credit Balance to unlock all app features.")
        connect_action = f"""
        <div class="wallet-connected-card">
          <div>
            <span class="eyebrow">Connected account</span>
            <h3>{esc(username)}</h3>
            <p>{esc(wallet)}</p>
          </div>
          <form method="post" action="/web3/logout"><button type="submit">Disconnect</button></form>
        </div>
        """
    else:
        access_title = "Connect wallet to start"
        access_note = "Connect a MetaMask, Rabby, Coinbase Wallet, or any EVM wallet that supports Mantle Mainnet."
        connect_action = """
        <div class="connect-panel">
          <span class="eyebrow">Wallet authentication</span>
          <h3>Sign in with your Web3 wallet</h3>
          <p>Your username is created automatically from the first characters of your wallet address.</p>
          <div id="walletStatus" class="wallet-status">Checking wallet provider...</div>
          <div class="actions">
            <button class="primary" id="connectWalletBtn" type="button">Connect Wallet</button>
            <a class="button" id="openMetaMaskBtn" href="https://metamask.app.link/dapp/" target="_blank" rel="noreferrer">Open MetaMask Browser</a>
          </div>
        </div>
        """

    pay_disabled = "disabled" if (not connected or active) else ""
    pay_label = "Demo Active" if is_demo else ("Plan Active" if active else "Pay with Wallet")
    status_badge = "Demo" if is_demo else ("Active" if active else ("Connected" if connected else "Guest"))
    status_class = "ok" if active else ("warn" if connected else "bad")

    return f"""
<style>
.profile-hero{{display:grid;grid-template-columns:1.15fr .85fr;gap:18px;align-items:stretch;margin-bottom:18px}}
.profile-panel{{background:rgba(255,255,255,.055);border:1px solid var(--line);border-radius:28px;padding:22px;box-shadow:0 18px 46px rgba(0,0,0,.28)}}
.profile-panel h2,.profile-panel h3{{margin:6px 0 8px;font-size:24px;letter-spacing:-.035em}}
.profile-panel p{{margin:0;color:var(--muted);line-height:1.55}}
.eyebrow{{display:inline-flex;font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:950;color:#ffc5ed;background:rgba(255,0,122,.12);border:1px solid rgba(255,0,122,.20);padding:7px 10px;border-radius:999px}}
.access-badge{{display:inline-flex;align-items:center;gap:8px;padding:9px 12px;border-radius:999px;border:1px solid var(--line);font-weight:950}}
.access-badge.ok{{color:#bbf7d0;background:rgba(34,197,94,.11);border-color:rgba(34,197,94,.32)}}
.access-badge.warn{{color:#fde68a;background:rgba(245,158,11,.11);border-color:rgba(245,158,11,.30)}}
.access-badge.bad{{color:#fecdd3;background:rgba(251,113,133,.10);border-color:rgba(251,113,133,.30)}}
.credit-balance{{display:flex;align-items:end;gap:10px;margin:16px 0 8px}}
.credit-balance b{{font-size:48px;letter-spacing:-.06em;line-height:.95}}
.credit-balance span{{color:var(--muted);font-weight:900;margin-bottom:6px}}
.credit-bar{{height:11px;background:rgba(255,255,255,.08);border:1px solid var(--line);border-radius:999px;overflow:hidden;margin:14px 0}}
.credit-bar i{{display:block;height:100%;width:{credit_percent}%;background:linear-gradient(90deg,var(--pink2),var(--purple),var(--blue));border-radius:999px}}
.profile-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:14px 0}}
.metric{{background:rgba(255,255,255,.055);border:1px solid var(--line);padding:16px;border-radius:22px}}
.metric span{{display:block;color:var(--muted);font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.06em}}
.metric b{{display:block;margin-top:8px;font-size:18px;overflow-wrap:anywhere}}
.workflow{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-top:14px}}
.step{{background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:24px;padding:16px;min-height:132px}}
.step strong{{display:inline-flex;width:30px;height:30px;align-items:center;justify-content:center;border-radius:999px;background:linear-gradient(135deg,var(--pink2),var(--purple));margin-bottom:10px}}
.step h4{{margin:0 0 7px;font-size:15px}}
.step p{{font-size:13px;color:var(--muted);margin:0;line-height:1.45}}
.wallet-connected-card{{height:100%;display:flex;justify-content:space-between;gap:14px;align-items:flex-start;background:linear-gradient(135deg,rgba(255,0,122,.12),rgba(124,58,237,.12));border:1px solid var(--line);border-radius:28px;padding:22px}}
.wallet-connected-card h3{{font-size:30px;margin:8px 0 4px}}
.wallet-connected-card p{{color:var(--muted);font-weight:900}}
.connect-panel{{height:100%;background:linear-gradient(135deg,rgba(255,0,122,.10),rgba(56,189,248,.07));border:1px solid var(--line);border-radius:28px;padding:22px}}
button[disabled]{{opacity:.45;cursor:not-allowed;transform:none!important;filter:none!important}}
.wallet-status{{display:inline-flex;align-items:center;margin:14px 0 2px;padding:10px 13px;border-radius:16px;background:rgba(255,255,255,.055);border:1px solid var(--line);color:var(--muted);font-weight:800}}
.wallet-status.ok{{color:#bbf7d0;border-color:rgba(34,197,94,.35);background:rgba(34,197,94,.10)}}
.wallet-status.bad{{color:#fecdd3;border-color:rgba(251,113,133,.35);background:rgba(251,113,133,.10)}}
.account-table{{display:grid;grid-template-columns:220px minmax(0,1fr);gap:12px 16px;margin-top:10px}}
.account-table div:nth-child(odd){{color:var(--muted);font-weight:900}}
.account-table div:nth-child(even){{font-weight:850;overflow-wrap:anywhere}}
@media(max-width:1100px){{.profile-hero,.workflow{{grid-template-columns:1fr}}.profile-grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}
@media(max-width:720px){{.profile-grid,.account-table{{grid-template-columns:1fr}}.credit-balance b{{font-size:38px}}}}
</style>

<div class="profile-hero">
  <div class="profile-panel">
    <div class="access-badge {status_class}">{esc(status_badge)}</div>
    <h2>{esc(access_title)}</h2>
    <p>{esc(access_note)}</p>
    <div class="credit-balance"><b>{credit}</b><span>Credits left · {credit_percent}%</span></div>
    <div class="credit-bar"><i></i></div>
    <p class="help">Monthly plan: {esc(cfg.monthly_credit_amount)} Credits for {esc(cfg.monthly_mnt_amount)} MNT on Mantle Mainnet. Credits decrease daily according to the remaining time and reach 0 when the {subscription_days()}-day plan expires. Days left: {days_left}.</p>
  </div>
  {connect_action}
</div>

<div class="card">
  <h3>Access Plan</h3>
  <div class="profile-grid">
    <div class="metric"><span>Monthly Credits</span><b>{esc(cfg.monthly_credit_amount)} Credits</b></div>
    <div class="metric"><span>Plan Price</span><b>{esc(cfg.monthly_mnt_amount)} MNT</b></div>
    <div class="metric"><span>Network</span><b>Mantle Mainnet</b></div>
    <div class="metric"><span>Chain ID</span><b>5000</b></div>
  </div>
  <div class="actions">
    <button class="primary" id="payWithWalletBtn" type="button" {pay_disabled}>{pay_label}</button>
    <form method="post" action="/web3/check-payment"><button type="submit" {'disabled' if not connected else ''}>Refresh Credit Balance</button></form>
  </div>
  <p class="help">Project owner wallet: <b>{esc(owner)}</b></p>
</div>

<div class="card">
  <h3>How access works</h3>
  <div class="workflow">
    <div class="step"><strong>1</strong><h4>Connect wallet</h4><p>Use an EVM wallet such as MetaMask, Rabby, or Coinbase Wallet.</p></div>
    <div class="step"><strong>2</strong><h4>Pay monthly plan</h4><p>Send {esc(cfg.monthly_mnt_amount)} MNT to the fixed project owner wallet.</p></div>
    <div class="step"><strong>3</strong><h4>Verify on-chain</h4><p>The app checks Mantle Mainnet history for a valid payment transaction.</p></div>
    <div class="step"><strong>4</strong><h4>Unlock automation</h4><p>Active users get {subscription_days()} days of access. Credits start at {esc(cfg.monthly_credit_amount)} and decrease daily.</p></div>
  </div>
</div>

<div class="card">
  <h3>Account Details</h3>
  <div class="account-table">
    <div>Username</div><div>{esc(username)}</div>
    <div>Wallet</div><div>{esc(wallet)}</div>
    <div>Credit Balance</div><div>{credit} / {esc(cfg.monthly_credit_amount)} Credits ({credit_percent}%)</div>
    <div>Days Remaining</div><div>{days_left} days</div>
    <div>Subscription Expiry</div><div>{esc(exp)}</div>
    <div>Last Payment Tx</div><div>{esc(tx)}</div>
  </div>
</div>

<script>
const MANTLE_CHAIN_ID_HEX = '0x1388';
const MANTLE_RPC_URL = {json.dumps(cfg.mantle_rpc_url or 'https://rpc.mantle.xyz')};
const OWNER_WALLET = {json.dumps(owner)};
const MONTHLY_AMOUNT_WEI = BigInt({json.dumps(amount_wei)});

let ACTIVE_PROVIDER = null;
const DISCOVERED = [];

function setWalletStatus(message, kind) {{
  const el = document.getElementById('walletStatus');
  if (!el) return;
  el.textContent = message;
  el.classList.remove('ok', 'bad');
  if (kind) el.classList.add(kind);
}}

function rememberProvider(provider, info) {{
  if (!provider) return;
  const exists = DISCOVERED.some((x) => x.provider === provider || (info && x.info && x.info.uuid === info.uuid));
  if (!exists) DISCOVERED.push({{ provider, info: info || {{}} }});
}}

window.addEventListener('eip6963:announceProvider', (event) => {{
  if (event && event.detail) rememberProvider(event.detail.provider, event.detail.info);
}});

function collectInjectedProviders() {{
  try {{ window.dispatchEvent(new Event('eip6963:requestProvider')); }} catch (_) {{}}
  if (window.ethereum) {{
    if (Array.isArray(window.ethereum.providers)) {{
      window.ethereum.providers.forEach((p, i) => rememberProvider(p, {{ name: p.isMetaMask ? 'MetaMask' : (p.isRabby ? 'Rabby Wallet' : (p.isCoinbaseWallet ? 'Coinbase Wallet' : 'Wallet ' + (i + 1))) }}));
    }} else {{
      rememberProvider(window.ethereum, {{ name: window.ethereum.isMetaMask ? 'MetaMask' : 'Injected Wallet' }});
    }}
  }}
  return DISCOVERED;
}}

function getProviderName(item) {{
  if (!item) return 'Wallet';
  if (item.info && item.info.name) return item.info.name;
  const p = item.provider;
  if (p && p.isMetaMask) return 'MetaMask';
  if (p && p.isRabby) return 'Rabby Wallet';
  if (p && p.isCoinbaseWallet) return 'Coinbase Wallet';
  return 'Injected Wallet';
}}

function getEthereumProvider() {{
  collectInjectedProviders();
  if (ACTIVE_PROVIDER) return ACTIVE_PROVIDER;
  const mm = DISCOVERED.find((x) => x.provider && x.provider.isMetaMask);
  return (mm || DISCOVERED[0] || {{}}).provider || null;
}}

async function walletRequest(method, params, providerOverride) {{
  const provider = providerOverride || getEthereumProvider();
  if (!provider) throw new Error('No Web3 wallet detected. Use Chrome/Brave with MetaMask, Rabby, Coinbase Wallet, or open this page inside MetaMask mobile browser.');
  return provider.request({{ method, params }});
}}

function closeWalletModal() {{
  const old = document.getElementById('walletModal');
  if (old) old.remove();
}}

function openWalletModal() {{
  collectInjectedProviders();
  if (!DISCOVERED.length) {{
    const msg = 'No wallet provider detected. If MetaMask is installed, refresh this page and make sure the extension is enabled for this site.';
    setWalletStatus(msg, 'bad');
    alert(msg);
    return;
  }}

  closeWalletModal();
  const modal = document.createElement('div');
  modal.id = 'walletModal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.68);display:flex;align-items:center;justify-content:center;padding:20px;';

  const box = document.createElement('div');
  box.style.cssText = 'width:min(440px,92vw);background:#171222;border:1px solid rgba(255,255,255,.14);border-radius:28px;padding:22px;box-shadow:0 30px 80px rgba(0,0,0,.55);color:#fff;font-family:Inter,system-ui,Segoe UI,sans-serif;';
  box.innerHTML = '<h3 style="margin:0 0 8px;font-size:22px">Connect Wallet</h3><p style="margin:0 0 16px;color:#bdb2d0;font-size:14px">Choose a wallet to connect on Mantle Mainnet.</p>';

  DISCOVERED.forEach((item) => {{
    const row = document.createElement('button');
    row.type = 'button';
    row.textContent = getProviderName(item);
    row.style.cssText = 'width:100%;margin:8px 0;padding:14px 16px;border-radius:18px;border:1px solid rgba(255,255,255,.14);background:linear-gradient(135deg,rgba(255,0,122,.23),rgba(124,58,237,.23));color:#fff;font-weight:900;cursor:pointer;text-align:left;';
    row.addEventListener('click', () => {{
      ACTIVE_PROVIDER = item.provider;
      closeWalletModal();
      connectWalletWithProvider(item.provider, getProviderName(item));
    }});
    box.appendChild(row);
  }});

  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.textContent = 'Cancel';
  cancel.style.cssText = 'width:100%;margin-top:12px;padding:13px 16px;border-radius:18px;border:1px solid rgba(255,255,255,.14);background:rgba(255,255,255,.06);color:#fff;font-weight:900;cursor:pointer;';
  cancel.addEventListener('click', closeWalletModal);
  box.appendChild(cancel);

  modal.appendChild(box);
  modal.addEventListener('click', (e) => {{ if (e.target === modal) closeWalletModal(); }});
  document.body.appendChild(modal);
}}

async function ensureMantle(provider) {{
  let chainId = await walletRequest('eth_chainId', undefined, provider);
  if (String(chainId).toLowerCase() !== MANTLE_CHAIN_ID_HEX) {{
    try {{
      await walletRequest('wallet_switchEthereumChain', [{{ chainId: MANTLE_CHAIN_ID_HEX }}], provider);
    }} catch (switchError) {{
      const code = Number(switchError && switchError.code);
      if (code === 4902) {{
        await walletRequest('wallet_addEthereumChain', [{{
          chainId: MANTLE_CHAIN_ID_HEX,
          chainName: 'Mantle Mainnet',
          nativeCurrency: {{ name: 'MNT', symbol: 'MNT', decimals: 18 }},
          rpcUrls: [MANTLE_RPC_URL],
          blockExplorerUrls: ['https://mantlescan.xyz']
        }}], provider);
      }} else {{
        throw switchError;
      }}
    }}
  }}
}}

async function connectWalletWithProvider(provider, providerName) {{
  const btn = document.getElementById('connectWalletBtn');
  try {{
    if (btn) btn.disabled = true;
    setWalletStatus('Opening ' + (providerName || 'wallet') + '...', null);
    const accounts = await walletRequest('eth_requestAccounts', [], provider);
    if (!accounts || !accounts[0]) throw new Error('Wallet did not return an account.');
    const address = accounts[0];
    setWalletStatus('Switching to Mantle Mainnet...', null);
    await ensureMantle(provider);
    setWalletStatus('Waiting for signature...', null);
    const nonceRes = await fetch('/web3/nonce?address=' + encodeURIComponent(address));
    const nonceData = await nonceRes.json();
    if (!nonceRes.ok) throw new Error(nonceData.error || 'Could not create nonce');
    const signature = await walletRequest('personal_sign', [nonceData.message, address], provider);
    const verifyRes = await fetch('/web3/verify', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{address, signature}})
    }});
    const verifyData = await verifyRes.json();
    if (!verifyRes.ok) throw new Error(verifyData.error || 'Signature verification failed');
    setWalletStatus('Wallet connected. Redirecting...', 'ok');
    window.location.href='/?tab=profile';
  }} catch (e) {{
    const msg = (e && e.message) ? e.message : String(e);
    setWalletStatus(msg, 'bad');
    alert(msg);
  }} finally {{
    if (btn) btn.disabled = false;
  }}
}}

async function connectWallet() {{
  collectInjectedProviders();
  if (DISCOVERED.length > 1) return openWalletModal();
  if (DISCOVERED.length === 1) {{
    ACTIVE_PROVIDER = DISCOVERED[0].provider;
    return connectWalletWithProvider(DISCOVERED[0].provider, getProviderName(DISCOVERED[0]));
  }}
  return openWalletModal();
}}

async function payWithWallet() {{
  const btn = document.getElementById('payWithWalletBtn');
  try {{
    if (!OWNER_WALLET) throw new Error('Project owner wallet is missing in the app build.');
    if (btn) btn.disabled = true;
    collectInjectedProviders();
    const provider = getEthereumProvider();
    if (!provider) throw new Error('No Web3 wallet detected.');
    const accounts = await walletRequest('eth_requestAccounts', [], provider);
    if (!accounts || !accounts[0]) throw new Error('Wallet did not return an account.');
    await ensureMantle(provider);
    const from = accounts[0];
    const txHash = await walletRequest('eth_sendTransaction', [{{
      from,
      to: OWNER_WALLET,
      value: '0x' + MONTHLY_AMOUNT_WEI.toString(16)
    }}], provider);
    alert('Payment submitted: ' + txHash + '\\nWait for confirmation, then click Refresh Credit Balance.');
  }} catch (e) {{
    const msg = (e && e.message) ? e.message : String(e);
    alert(msg);
  }} finally {{
    if (btn && OWNER_WALLET) btn.disabled = false;
  }}
}}

function initWeb3Buttons() {{
  collectInjectedProviders();
  const connectBtn = document.getElementById('connectWalletBtn');
  const payBtn = document.getElementById('payWithWalletBtn');
  if (connectBtn) {{
    connectBtn.onclick = function(e) {{ e.preventDefault(); connectWallet(); }};
  }}
  if (payBtn) {{
    payBtn.onclick = function(e) {{ e.preventDefault(); payWithWallet(); }};
  }}

  const openMetaMaskBtn = document.getElementById('openMetaMaskBtn');
  if (openMetaMaskBtn) {{
    openMetaMaskBtn.href = 'https://metamask.app.link/dapp/' + window.location.host + window.location.pathname + '?tab=profile';
  }}

  if (document.getElementById('walletStatus')) {{
    if (DISCOVERED.length) {{
      const names = DISCOVERED.map(getProviderName).join(', ');
      setWalletStatus('Wallet detected: ' + names + '. Click Connect Wallet.', 'ok');
    }} else {{
      setWalletStatus('No wallet provider detected yet. Enable MetaMask/Rabby/Coinbase for this site, then refresh.', 'bad');
      setTimeout(() => {{
        collectInjectedProviders();
        if (DISCOVERED.length) setWalletStatus('Wallet detected. Click Connect Wallet.', 'ok');
      }}, 800);
    }}
  }}
}}

if (document.readyState === 'loading') {{
  document.addEventListener('DOMContentLoaded', initWeb3Buttons);
}} else {{
  initWeb3Buttons();
}}
</script>
"""

_render_user_context: Optional[dict] = None

def home_content() -> str:
    user = _render_user_context
    status = bot_status(user)
    return f"""
<div class="card">
  <h3>Command Center</h3>
  <div class="profile-grid">
    <div class="metric"><span>Bot</span><b>{esc(status)}</b></div>
    <div class="metric"><span>WordPress</span><b>{'Configured' if cfg.wp_url else 'Missing URL'}</b></div>
    <div class="metric"><span>Telegram</span><b>{'Enabled' if cfg.enable_telegram_social_post else 'Off'}</b></div>
    <div class="metric"><span>X / Facebook</span><b>{'Enabled' if (cfg.enable_x_post or cfg.enable_facebook_post) else 'Off'}</b></div>
  </div>
  <div class="actions">
    <form method="post" action="/start"><button class="primary">Start Bot</button></form>
    <form method="post" action="/stop"><button class="danger">Stop Bot</button></form>
    <form method="post" action="/post-once"><button class="primary">Write & Publish Now</button></form>
    <form method="post" action="/scan"><button>Scan RSS</button></form>
  </div>
</div>
<div class="card">
  <h3>Personal Workspace</h3>
  <p class="help">Each connected wallet has its own saved OpenAI key, WordPress credentials, Telegram session name, target channels, X cookies, Facebook cookies, posting switches, forward settings, and BlockScam settings. When a user logs in again, the dashboard reloads that wallet's personal setup automatically.</p>
</div>
<div class="card">
  <h3>Recommended Setup Flow</h3>
  <div class="workflow">
    <div class="step"><strong>1</strong><h4>User Profile</h4><p>Connect a wallet and activate the monthly Mantle plan.</p></div>
    <div class="step"><strong>2</strong><h4>RSS / WordPress</h4><p>Configure OpenAI, WordPress, categories, and publishing rules.</p></div>
    <div class="step"><strong>3</strong><h4>Login & Cookies</h4><p>Add Telegram session details and X/Facebook cookies.</p></div>
    <div class="step"><strong>4</strong><h4>Run Automation</h4><p>Test each channel, then start the bot or publish one article now.</p></div>
  </div>
</div>
"""

def wordpress_content() -> str:
    language_options = "".join(
        f'<option value="{esc(lang)}" {selected(lang, cfg.content_language)}>{esc(lang)}</option>'
        for lang in CONTENT_LANGUAGES
    )
    cats = "".join(
        f'<label><input type="checkbox" name="cat_{esc(cat)}" {"checked" if cat in cfg.selected_categories else ""}> {esc(cat)}</label>'
        for cat in CATEGORY_KEYWORDS
    )
    return f"""
<form method="post" action="/save?next=wordpress">
<input type="hidden" name="category_form" value="1">
<input type="hidden" name="bool__create_image" value="1">
<input type="hidden" name="bool__enable_ai_scoring" value="1">
<div class="card">
  <h3>OpenAI / WordPress / News</h3>
  <div class="grid">
    <label>OpenAI API Key</label>{secret_input("openai_api_key", cfg.openai_api_key)}
    <label>WP REST URL</label><input name="wp_url" value="{esc(cfg.wp_url)}" placeholder="https://domain.com/wp-json/wp/v2/posts">
    <label>WP JWT</label>{secret_input("wp_jwt", cfg.wp_jwt)}
    <label>CryptoPanic Token</label>{secret_input("crypto_panic", cfg.crypto_panic)}
    <label>Min Score</label><input name="min_score" value="{esc(cfg.min_score)}">
    <label>Posts Per Day</label><input name="posts_per_day" value="{esc(getattr(cfg, 'posts_per_day', 5))}" placeholder="5">
    <input type="hidden" name="post_interval_seconds" value="{esc(getattr(cfg, 'post_interval_seconds', 17280))}">
    <label>Recent News Hours</label><input name="recent_hours" value="{esc(cfg.recent_hours)}">
    <label>WordPress Status</label>
    <select name="wp_publish_status">
      <option value="publish" {selected('publish', cfg.wp_publish_status)}>publish</option>
      <option value="draft" {selected('draft', cfg.wp_publish_status)}>draft</option>
    </select>
    <label>Content Language</label>
    <select name="content_language">{language_options}</select>
    <label>Text Model</label>
    <select name="ai_text_model">
      <option value="gpt-5-nano" {selected('gpt-5-nano', getattr(cfg, 'ai_text_model', 'gpt-5-nano'))}>gpt-5-nano — lowest cost</option>
      <option value="gpt-5-mini" {selected('gpt-5-mini', getattr(cfg, 'ai_text_model', 'gpt-5-nano'))}>gpt-5-mini — stronger writing</option>
    </select>
    <label>Image Policy</label>
    <select name="image_policy">
      <option value="off" {selected('off', getattr(cfg, 'image_policy', 'high_score_only'))}>Off — cheapest</option>
      <option value="high_score_only" {selected('high_score_only', getattr(cfg, 'image_policy', 'high_score_only'))}>High-score news only</option>
      <option value="every_post" {selected('every_post', getattr(cfg, 'image_policy', 'high_score_only'))}>Every post</option>
    </select>
    <label>Image Min Score</label><input name="image_min_score" value="{esc(getattr(cfg, 'image_min_score', 9))}">
    <label>Image Model</label>
    <select name="image_model">
      <option value="gpt-image-2" {selected('gpt-image-2', getattr(cfg, 'image_model', 'gpt-image-2'))}>gpt-image-2</option>
      <option value="gpt-image-1-mini" {selected('gpt-image-1-mini', getattr(cfg, 'image_model', 'gpt-image-2'))}>gpt-image-1-mini</option>
      <option value="gpt-image-1" {selected('gpt-image-1', getattr(cfg, 'image_model', 'gpt-image-2'))}>gpt-image-1</option>
    </select>
    <label>Image Quality</label>
    <select name="image_quality">
      <option value="low" {selected('low', getattr(cfg, 'image_quality', 'low'))}>low</option>
      <option value="medium" {selected('medium', getattr(cfg, 'image_quality', 'low'))}>medium</option>
      <option value="high" {selected('high', getattr(cfg, 'image_quality', 'low'))}>high</option>
    </select>
    <label>Image Size</label>
    <select name="image_size">
      <option value="1536x1024" {selected('1536x1024', getattr(cfg, 'image_size', '1536x1024'))}>1536x1024 landscape</option>
      <option value="1024x1024" {selected('1024x1024', getattr(cfg, 'image_size', '1536x1024'))}>1024x1024 square</option>
      <option value="1024x1536" {selected('1024x1536', getattr(cfg, 'image_size', '1536x1024'))}>1024x1536 portrait</option>
    </select>
  </div>
  <p class="help">Cost optimized by default: 5 posts per day, local RSS scoring, one AI text call for title/article/social draft, and low-quality image generation only for high-score news.</p>
  <p class="help">The selected language is synced across the WordPress title, WordPress article, and every social network post.</p>
  <div class="checkrow">{cats}</div>
  <div class="checkrow"><label><input type="checkbox" name="create_image" {checked('create_image')}> Generate Featured Image</label><label><input type="checkbox" name="enable_ai_scoring" {checked('enable_ai_scoring')}> Use AI scoring for RSS candidates</label></div>
  <label>Extra Topic Filter</label><textarea name="custom_topic_filter">{esc(cfg.custom_topic_filter)}</textarea>
  <div class="actions"><button class="primary" type="submit">Save Configuration</button></div>
</div>
</form>
<div class="card">
  <div class="actions">
    <form method="post" action="/scan"><button>Scan RSS</button></form>
    <form method="post" action="/post-once"><button class="primary">Write & Publish Now</button></form>
  </div>
</div>
"""


def login_content() -> str:
    return f"""
<div class="card">
  <h3>Telegram Session</h3>
  <form method="post" action="/save?next=login">
    <div class="grid">
      <label>API ID</label><input name="telegram_api_id" value="{esc(cfg.telegram_api_id)}">
      <label>API Hash</label>{secret_input("telegram_api_hash", cfg.telegram_api_hash)}
      <label>Session name</label><input name="telegram_session_name" value="{esc(cfg.telegram_session_name)}">
      <label>Phone</label><input name="telegram_phone" value="{esc(cfg.telegram_phone)}">
    </div>
    <div class="actions"><button class="primary">Save Telegram</button></div>
  </form>
  <div class="actions">
    <form method="post" action="/telegram/send-code"><button>Send Telegram Code</button></form>
    <form method="post" action="/telegram/test"><button>Test Session</button></form>
  </div>
  <form method="post" action="/telegram/confirm">
    <fieldset>
      <legend>Confirm Telegram Code</legend>
      <div class="grid">
        <label>Telegram Code</label><input name="code">
        <label>2FA Password if needed</label>{secret_input("password", "")}
      </div>
      <div class="actions"><button class="primary">Confirm Code</button></div>
    </fieldset>
  </form>
</div>

<div class="card">
  <h3>X / Twitter Cookie</h3>
  <form method="post" action="/save?next=login">
    <input type="hidden" name="bool__enable_x_post" value="1">
    <div class="grid">
      <label>X auth_token</label>{secret_input("x_auth_token", cfg.x_auth_token)}
      <label>X ct0</label>{secret_input("x_ct0", cfg.x_ct0)}
    </div>
    <div class="checkrow"><label><input type="checkbox" name="enable_x_post" {checked('enable_x_post')}> Enable X Posting</label></div>
    <div class="actions"><button class="primary">Save X Cookie</button></div>
  </form>
  <form method="post" action="/test-x"><button>Test X Post</button></form>
</div>

<div class="card">
  <h3>Facebook Cookie</h3>
  <form method="post" action="/save?next=login">
    <input type="hidden" name="bool__enable_facebook_post" value="1">
    <div class="grid">
      <label>Facebook Target URL</label><input name="facebook_target_url" value="{esc(cfg.facebook_target_url)}">
      <label>Facebook Cookie JSON</label>{secret_textarea("facebook_cookie_json", cfg.facebook_cookie_json)}
    </div>
    <div class="checkrow"><label><input type="checkbox" name="enable_facebook_post" {checked('enable_facebook_post')}> Enable Facebook Posting</label></div>
    <div class="actions"><button class="primary">Save Facebook Cookie</button></div>
  </form>
  <form method="post" action="/test-facebook"><button>Test Facebook Post</button></form>
</div>
"""


def social_content() -> str:
    return f"""
<form method="post" action="/save?next=social">
<input type="hidden" name="bool__enable_telegram_social_post" value="1">
<input type="hidden" name="bool__enable_x_post" value="1">
<input type="hidden" name="bool__enable_facebook_post" value="1">
<div class="card">
  <h3>Social Channels</h3>
  <p class="help">Social posts automatically use the same language selected in RSS / WordPress: <b>{esc(cfg.content_language)}</b>.</p>
  <div class="checkrow">
    <label><input type="checkbox" name="enable_telegram_social_post" {checked('enable_telegram_social_post')}> Post to Telegram</label>
    <label><input type="checkbox" name="enable_x_post" {checked('enable_x_post')}> Post to X</label>
    <label><input type="checkbox" name="enable_facebook_post" {checked('enable_facebook_post')}> Post to Facebook</label>
  </div>
  <div class="actions"><button class="primary">Save Configuration</button></div>
</div>
</form>
<div class="card">
  <h3>Social Tests</h3>
  <div class="actions">
    <form method="post" action="/test-x"><button>Test X Post</button></form>
    <form method="post" action="/test-facebook"><button>Test Facebook Post</button></form>
  </div>
</div>
"""


def forward_content() -> str:
    return f"""
<form method="post" action="/save?next=forward">
<input type="hidden" name="bool__enable_telegram_forward" value="1">
<div class="card">
  <h3>Forward from Source Channel to Multiple Targets</h3>
  <div class="checkrow"><label><input type="checkbox" name="enable_telegram_forward" {checked('enable_telegram_forward')}> Enable Telegram Forward</label></div>
  <div class="grid">
    <label>Source channel</label><input name="telegram_source_channel" value="{esc(cfg.telegram_source_channel)}">
    <label>Target Channels, one per line</label><textarea name="telegram_target_channels">{esc(cfg.telegram_target_channels)}</textarea>
  </div>
  <div class="actions"><button class="primary">Save Configuration</button></div>
</div>
</form>
"""


def blockscam_content() -> str:
    return f"""
<form method="post" action="/save?next=blockscam">
<input type="hidden" name="bool__enable_block_scam" value="1">
<input type="hidden" name="bool__enable_block_scam_ai" value="1">
<div class="card">
  <h3>AI BlockScam Monitor</h3>
  <p class="help">BlockScam uses fast keyword rules first, then optional AI classification for messages that are not obvious by keyword. This improves accuracy while keeping API cost low.</p>
  <div class="checkrow">
    <label><input type="checkbox" name="enable_block_scam" {checked('enable_block_scam')}> Enable BlockScam</label>
    <label><input type="checkbox" name="enable_block_scam_ai" {checked('enable_block_scam_ai')}> Enable AI Scam Detection</label>
  </div>
  <div class="grid">
    <label>AI Model</label>
    <select name="block_scam_ai_model">
      <option value="gpt-5-nano" {selected('gpt-5-nano', getattr(cfg, 'block_scam_ai_model', 'gpt-5-nano'))}>gpt-5-nano — lowest cost</option>
      <option value="gpt-5-mini" {selected('gpt-5-mini', getattr(cfg, 'block_scam_ai_model', 'gpt-5-nano'))}>gpt-5-mini — stronger moderation</option>
    </select>
    <label>AI Delete Threshold</label><input name="block_scam_ai_threshold" value="{esc(getattr(cfg, 'block_scam_ai_threshold', 7))}" placeholder="7">
  </div>
  <label>Chats to Scan, one group/channel per line</label><textarea name="block_scam_target_chats">{esc(cfg.block_scam_target_chats)}</textarea>
  <label>Scam Keywords, one keyword per line</label><textarea name="block_scam_keywords">{esc(cfg.block_scam_keywords)}</textarea>
  <div class="actions"><button class="primary">Save Configuration</button></div>
</div>
</form>
"""


def log_content() -> str:
    drain_logs()
    logs = html.escape("\n".join(log_buffer[-MAX_LOGS:]))
    return f"""
<div class="card">
  <h3>System Logs</h3>
  <pre>{logs}</pre>
  <p><a href="/logs" target="_blank">Open raw logs</a></p>
</div>
"""


TAB_RENDERERS = {
    "home": ("Mantle Social Publisher", home_content),
    "profile": ("User Profile", profile_content),
    "wordpress": ("RSS / WordPress", wordpress_content),
    "login": ("Login", login_content),
    "social": ("Social Posting", social_content),
    "forward": ("Telegram Forward", forward_content),
    "blockscam": ("BlockScam", blockscam_content),
    "log": ("System Logs", log_content),
}


# =========================================================
# WEB3 ROUTES
# =========================================================

@app.get("/web3/nonce")
def web3_nonce(address: str):
    try:
        a = normalize_address(address)
        nonce = secrets.token_hex(16)
        message = (
            f"Sign in to Mantle Social Publisher\n\n"
            f"Wallet: {a}\n"
            f"Network: Mantle Mainnet\n"
            f"Chain ID: 5000\n"
            f"Nonce: {nonce}"
        )
        with auth_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO web3_nonces(address, nonce, created_at) VALUES (?, ?, ?)",
                (a, nonce, now_iso()),
            )
            conn.commit()
        return {"address": a, "nonce": nonce, "message": message}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/web3/verify")
async def web3_verify(request: Request):
    try:
        payload = await request.json()
        address = normalize_address(payload.get("address", ""))
        signature = str(payload.get("signature", ""))
        with auth_conn() as conn:
            row = conn.execute("SELECT nonce FROM web3_nonces WHERE lower(address)=lower(?)", (address,)).fetchone()
        if not row:
            return JSONResponse({"error": "Missing nonce. Connect wallet again."}, status_code=400)
        message = (
            f"Sign in to Mantle Social Publisher\n\n"
            f"Wallet: {address}\n"
            f"Network: Mantle Mainnet\n"
            f"Chain ID: 5000\n"
            f"Nonce: {row['nonce']}"
        )
        recovered = Account.recover_message(encode_defunct(text=message), signature=signature).lower()
        if recovered != address.lower():
            return JSONResponse({"error": "Wallet signature does not match the connected address."}, status_code=401)

        token = secrets.token_urlsafe(40)
        created = now_iso()
        session_exp = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        username = username_from_address(address)
        with auth_conn() as conn:
            existing = conn.execute("SELECT address FROM web3_users WHERE lower(address)=lower(?)", (address,)).fetchone()
            if existing:
                conn.execute("UPDATE web3_users SET last_login_at=? WHERE lower(address)=lower(?)", (created, address))
            else:
                conn.execute(
                    "INSERT INTO web3_users(address, username, created_at, last_login_at) VALUES (?, ?, ?, ?)",
                    (address, username, created, created),
                )
            conn.execute(
                "INSERT INTO web3_sessions(token, address, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, address, created, session_exp),
            )
            conn.execute("DELETE FROM web3_nonces WHERE lower(address)=lower(?)", (address,))
            conn.commit()
        if is_demo_wallet(address):
            grant_demo_subscription(address)
        ensure_user_config(address)
        res = JSONResponse({"ok": True, "username": username, "address": address})
        res.set_cookie("msp_session", token, httponly=True, secure=False, samesite="lax", max_age=30 * 24 * 3600)
        logger.info(f"✅ Web3 wallet login: {username} {short_addr(address)}")
        return res
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/web3/check-payment")
def web3_check_payment(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/?tab=profile", status_code=303)
    activate_config_for_user(user)
    try:
        result = refresh_subscription(user["address"])
        if result.get("active"):
            logger.info(f"✅ Subscription active for {short_addr(user['address'])}. Tx: {result.get('tx')}")
            return RedirectResponse("/?tab=profile&msg=paid", status_code=303)
        logger.warning(f"No valid subscription payment found for {short_addr(user['address'])}.")
        return RedirectResponse("/?tab=profile&msg=nopayment", status_code=303)
    except Exception as e:
        logger.error(f"Payment check error: {e}")
        return RedirectResponse("/?tab=profile&msg=nopayment", status_code=303)


@app.post("/web3/logout")
def web3_logout(request: Request):
    token = request.cookies.get("msp_session")
    if token:
        with auth_conn() as conn:
            conn.execute("DELETE FROM web3_sessions WHERE token=?", (token,))
            conn.commit()
    res = RedirectResponse("/?tab=profile&msg=logout", status_code=303)
    res.delete_cookie("msp_session")
    return res


# =========================================================
# ROUTES
# =========================================================

@app.get("/", response_class=HTMLResponse)
def home(request: Request, tab: str = "home", msg: str = ""):
    if tab not in TAB_RENDERERS:
        tab = "home"
    user = get_current_user(request)
    activate_config_for_user(user)
    global _render_user_context
    _render_user_context = user
    title, renderer = TAB_RENDERERS[tab]
    message_map = {
        "saved": "Configuration saved.",
        "paid": "On-chain payment confirmed. Your 30-day Credit Balance has been updated.",
        "nopayment": "No valid monthly payment found yet.",
        "logout": "Wallet disconnected.",
        "subrequired": "Monthly subscription required before using this feature.",
    }
    message = message_map.get(msg, "")
    if tab == "profile":
        content = renderer(user)
    else:
        content = renderer()
    return page_shell(tab, title, content, message, user=user)


@app.post("/save")
async def save(request: Request, next: str = "home"):
    user = get_current_user(request)
    activate_config_for_user(user)
    form = await request.form()
    save_from_form(dict(form))
    apply_server_subscription_settings(cfg)
    if user and user.get("address"):
        save_config_for_user(user["address"], cfg)
        logger.info(f"💾 Saved personal configuration for {short_addr(user['address'])}.")
    else:
        ConfigStore.save(cfg)
        logger.info("💾 Saved global fallback configuration.")
    if next not in TAB_RENDERERS:
        next = "home"
    return RedirectResponse(f"/?tab={next}&msg=saved", status_code=303)


@app.post("/start")
def start(request: Request):
    user, guard = require_active_user(request)
    if guard: return guard
    address = normalize_address(user["address"])
    state = user_bots.get(address)
    if state and state.get("thread") and state["thread"].is_alive():
        logger.info(f"Bot is already running for {short_addr(address)}.")
    else:
        stop_evt = threading.Event()
        thread = threading.Thread(target=bot_worker_for_user, args=(address, stop_evt), daemon=True)
        user_bots[address] = {"thread": thread, "stop_event": stop_evt}
        thread.start()
        logger.info(f"🚀 Bot started for {short_addr(address)}.")
    return RedirectResponse("/?tab=home", status_code=303)


@app.post("/stop")
def stop(request: Request):
    user, guard = require_active_user(request)
    if guard: return guard
    address = normalize_address(user["address"])
    state = user_bots.get(address)
    if state:
        state["stop_event"].set()
    logger.info(f"🛑 Stopping bot for {short_addr(address)}...")
    return RedirectResponse("/?tab=home", status_code=303)


@app.post("/scan")
def scan(request: Request):
    user, guard = require_active_user(request)
    if guard: return guard
    address = normalize_address(user["address"])
    state = get_user_services(address)
    def worker():
        try:
            candidates = state["news"].fetch_candidates()
            logger.info(f"🔎 Found {len(candidates)} matching news items for {short_addr(address)}.")
            for n in candidates[:10]:
                logger.info(f"- {n.get('title')}")
        except Exception as e:
            logger.error(f"Scan RSS error: {e}")

    threading.Thread(target=worker, daemon=True).start()
    return RedirectResponse("/?tab=wordpress", status_code=303)


@app.post("/post-once")
def post_once(request: Request):
    user, guard = require_active_user(request)
    if guard: return guard
    address = normalize_address(user["address"])
    state = get_user_services(address)
    def worker():
        try:
            result = state["news"].create_and_post_once()
            if result:
                fut = runtime.submit(publish_socials_for_state(result["summary"], state))
                fut.result(timeout=300)
        except Exception as e:
            logger.error(f"Post once error: {e}")

    threading.Thread(target=worker, daemon=True).start()
    return RedirectResponse("/?tab=home", status_code=303)


@app.post("/telegram/send-code")
def telegram_send_code(request: Request):
    user, guard = require_active_user(request)
    if guard: return guard
    try:
        address = normalize_address(user["address"])
        state = get_user_services(address)
        phone = state["holder"]["cfg"].telegram_phone.strip()
        if not phone:
            logger.error("Missing Telegram phone number.")
        else:
            runtime.submit(state["telegram"].send_login_code(phone))
    except Exception as e:
        logger.error(f"Telegram send code error: {e}")
    return RedirectResponse("/?tab=login", status_code=303)


@app.post("/telegram/confirm")
def telegram_confirm(request: Request, code: str = Form(...), password: str = Form("")):
    user, guard = require_active_user(request)
    if guard: return guard
    try:
        state = get_user_services(user["address"])
        runtime.submit(state["telegram"].confirm_login_code(code.strip(), password.strip()))
    except Exception as e:
        logger.error(f"Telegram confirm error: {e}")
    return RedirectResponse("/?tab=login", status_code=303)


@app.post("/telegram/test")
def telegram_test(request: Request):
    user, guard = require_active_user(request)
    if guard: return guard
    try:
        state = get_user_services(user["address"])
        runtime.submit(state["telegram"].test_session())
    except Exception as e:
        logger.error(f"Telegram test error: {e}")
    return RedirectResponse("/?tab=login", status_code=303)


@app.post("/test-x")
def test_x(request: Request):
    user, guard = require_active_user(request)
    if guard: return guard
    state = get_user_services(user["address"])
    def worker():
        c = state["holder"]["cfg"]
        old = c.enable_x_post
        try:
            c.enable_x_post = True
            fut = runtime.submit(state["social"].post_x("Automated test post from Mantle Social Publisher on Railway."))
            fut.result(timeout=180)
        except Exception as e:
            logger.error(f"Test X error: {e}")
        finally:
            c.enable_x_post = old

    threading.Thread(target=worker, daemon=True).start()
    return RedirectResponse("/?tab=login", status_code=303)


@app.post("/test-facebook")
def test_facebook(request: Request):
    user, guard = require_active_user(request)
    if guard: return guard
    state = get_user_services(user["address"])
    def worker():
        c = state["holder"]["cfg"]
        old = c.enable_facebook_post
        try:
            c.enable_facebook_post = True
            fut = runtime.submit(state["social"].post_facebook("Automated test post from Mantle Social Publisher on Railway."))
            fut.result(timeout=240)
        except Exception as e:
            logger.error(f"Test Facebook error: {e}")
        finally:
            c.enable_facebook_post = old

    threading.Thread(target=worker, daemon=True).start()
    return RedirectResponse("/?tab=login", status_code=303)


@app.get("/logs", response_class=PlainTextResponse)
def logs():
    drain_logs()
    return "\n".join(log_buffer[-MAX_LOGS:])


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

"""Telegram notification client (free Bot API, no vendor lock-in).

Replaces the Resend email path end to end:
- send_html: one message per chat, HTML parse mode, WOULD-NOTIFY log when
  no token is configured so the whole alert loop stays testable offline.
- watch_link: t.me deep link with a start payload that pre-registers one or
  more tickers. Telegram caps start payloads at 64 bytes, so bag lineups are
  packed until they no longer fit and the rest is dropped silently (the bot
  reply always lists what is actually watched).

Every outbound claim cites its filing exactly like the page does. No
forecasts, "not financial advice" footer on every message."""

import logging
import re
import time

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

# start-payload prefix that marks a watch registration
WATCH_PREFIX = "watch_"
# hard cap from Telegram for deep-link start payloads
START_PAYLOAD_MAX = 64


def available() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)


def bot_username() -> str:
    return TELEGRAM_BOT_USERNAME


def esc(text) -> str:
    """Escape dynamic text for Telegram's HTML parse mode."""
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _call(method: str, payload: dict = None, timeout: float = 35.0):
    if not available():
        return None
    url = API.format(token=TELEGRAM_BOT_TOKEN, method=method)
    try:
        r = httpx.post(url, json=payload or {}, timeout=timeout)
        data = r.json() if r.status_code == 200 else None
        if not data or not data.get("ok"):
            log.warning("telegram %s failed (%s): %s", method,
                        r.status_code, (data or {}).get("description"))
            return None
        return data.get("result")
    except Exception as exc:
        log.warning("telegram %s errored: %s", method, exc)
        return None


def get_updates(offset: int = 0, timeout_s: int = 25):
    """Long-poll pending bot updates; returns [] when unavailable."""
    result = _call("getUpdates", {
        "offset": offset,
        "timeout": timeout_s,
        "allowed_updates": ["message"],
    }, timeout=timeout_s + 10.0)
    return result or []


def set_webhook(url: str):
    """Register the production webhook with our shared secret header."""
    from .config import TG_WEBHOOK_SECRET
    payload = {"url": url}
    if TG_WEBHOOK_SECRET:
        payload["secret_token"] = TG_WEBHOOK_SECRET
    return bool(_call("setWebhook", payload))


def delete_webhook():
    return bool(_call("deleteWebhook"))


def send_html(chat_id: str, html: str) -> bool:
    """Send one HTML message to one chat. Returns False (and logs
    WOULD-NOTIFY) when no token is configured."""
    if not available():
        log.info("TELEGRAM_BOT_TOKEN not set. WOULD-NOTIFY chat=%s: %s",
                 chat_id, re.sub(r"<[^>]+>", "", html)[:120])
        return False
    result = _call("sendMessage", {
        "chat_id": chat_id,
        "text": html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    if result is None:
        return False
    # stay far under the ~1 msg/s per-chat guidance at any scale
    time.sleep(0.05)
    return True


def watch_payload(tickers: list) -> str:
    """Pack tickers into one deep-link start payload within Telegram's
    64-byte budget. Priority order is preserved (worst first for bags)."""
    body = ",".join(tickers)
    while body and len(WATCH_PREFIX) + len(body) > START_PAYLOAD_MAX:
        body = body.rsplit(",", 1)[0]
    return WATCH_PREFIX + body if body else ""


def watch_link(tickers: list) -> str:
    """t.me deep link registering the given tickers, or '' when the bot
    username is not configured yet."""
    if not bot_username():
        return ""
    payload = watch_payload(tickers)
    return f"https://t.me/{bot_username()}?start={payload}" if payload else \
        f"https://t.me/{bot_username()}"

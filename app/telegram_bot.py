"""decoded.report Telegram bot: subscriptions and notifications intake.

Users never hand over an email. They press START on a t.me deep link from a
report page (`?start=watch_NXL`) or type commands:

    /start [watch_TICKER]     welcome; payload auto-registers that ticker
    /watch TICKER,TICKER      watch up to 8 tickers (unknown symbols skipped)
    /unwatch [TICKERS|ALL]    stop watching; no argument = everything
    /list                     what this chat currently watches

Unknown tickers are answered honestly: not in the SEC system under that
symbol. The bot only ever learns the chat id and what it watches.

Run modes:
    python -m app.telegram_bot --poll            # resident long-poll (local dev)
    python -m app.telegram_bot --once            # drain pending updates once
    python -m app.telegram_bot --set-webhook URL # production wiring helper

The webhook route lives in the FastAPI app at POST /tg/webhook/{secret};
--set-webhook registers it with Telegram. Without a token everything logs
WOULD-NOTIFY / refuses politely: nothing else in the product blocks."""

import argparse
import json
import logging
import re
import time

from . import db
from . import telegram_client as tg
from .config import public_url
from .sec_client import SecClient

log = logging.getLogger(__name__)

MAX_WATCH = 8
TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
OFFSET_KEY = "tg:update_offset"
OFFSET_TTL = 10 * 365 * 86400

WELCOME = (
    "<b>decoded.report</b>: I read SEC filings so you do not have to. "
    "Every number cited to its source.\n\n"
    "Commands:\n"
    "/watch TICKER : watch a ticker for material filings\n"
    "/list : what you watch\n"
    "/unwatch [ALL] : stop watching\n\n"
    "<i>One message per material filing. Every line cites its official SEC "
    "source. Screening, never advice.</i>"
)


def _sanitize_list(raw: str) -> list:
    out, seen = [], set()
    for part in re.split(r"[,\s;]+", (raw or "").upper()):
        if part and TICKER_RE.match(part) and part not in seen:
            seen.add(part)
            out.append(part)
        if len(out) >= MAX_WATCH:
            break
    return out


def _fmt_watchlist(tickers: list) -> str:
    if not tickers:
        return "You are not watching anything yet. Try /watch TICKER"
    lines = []
    for t in tickers:
        lines.append(f'• <a href="{public_url("/" + t)}">{t}</a>')
    return "Watching:\n" + "\n".join(lines) + \
        "\n\nYou hear about material filings within one alert pass."


class Bot:
    def __init__(self):
        self.sec = SecClient()
        db.init_db()

    # ---- command handlers ------------------------------------------------

    def handle_update(self, update: dict) -> None:
        """Process one Telegram update. Never raises to the caller loop."""
        try:
            msg = update.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "")
            text = (msg.get("text") or "").strip()
            if not chat_id or not text:
                return
            if text.startswith("/start"):
                self._handle_start(chat_id, text)
            elif text.startswith("/watch"):
                self._handle_watch(chat_id, text)
            elif text.startswith("/unwatch"):
                self._handle_unwatch(chat_id, text)
            elif text.startswith("/list"):
                tg.send_html(chat_id, _fmt_watchlist(db.subs_for_chat(chat_id)))
            else:
                tg.send_html(chat_id,
                             "I only speak /start, /watch, /unwatch and /list.")
        except Exception as exc:
            log.warning("bot update failed: %s", exc)

    def _handle_start(self, chat_id: str, text: str) -> None:
        payload = text[len("/start"):].strip().removeprefix(tg.WATCH_PREFIX).upper()
        wanted = _sanitize_list(payload.replace("%2C", ","))
        tg.send_html(chat_id, WELCOME)
        if not wanted:
            return
        added = self._register(chat_id, wanted, source="deeplink")
        if added:
            tg.send_html(chat_id, _fmt_watchlist(db.subs_for_chat(chat_id)))
        else:
            tg.send_html(chat_id, _fmt_watchlist(db.subs_for_chat(chat_id)))

    def _handle_watch(self, chat_id: str, text: str) -> None:
        raw = text.split(" ", 1)[1] if " " in text else ""
        wanted = _sanitize_list(raw)
        if not wanted:
            tg.send_html(
                chat_id,
                "Send tickers like <code>/watch TICKER</code> (up to 8, comma separated).",
            )
            return
        self._register(chat_id, wanted, source="command")
        tg.send_html(chat_id, _fmt_watchlist(db.subs_for_chat(chat_id)))

    def _handle_unwatch(self, chat_id: str, text: str) -> None:
        raw = text.split(" ", 1)[1].strip() if " " in text else ""
        if not raw or raw.upper() == "ALL":
            n = db.sub_remove(chat_id)
            reply = f"Removed all {n} watching ent{'ry' if n == 1 else 'ries'}." \
                if n else "Nothing to remove."
        else:
            wanted = _sanitize_list(raw)
            n = db.sub_remove(chat_id, wanted) if wanted else 0
            reply = f"Stopped watching {n} ticker{'s' if n != 1 else ''}." \
                if n else "None of those were on your list."
        tg.send_html(chat_id, reply)

    def _register(self, chat_id: str, wanted: list, source: str) -> list:
        resolved, unknown = [], []
        for t in wanted:
            resolved.append(t) if self.sec.resolve(t) else unknown.append(t)
        added = db.sub_add(chat_id, resolved, source=source, utm="telegram")
        for t in added:
            db.log_event("lead", ticker=t, utm="telegram", captured=1)
        if unknown:
            tg.send_html(
                chat_id,
                "Not in the SEC system under " + ", ".join(tg.esc(u) for u in unknown)
                + ": delisted, private, or a typo. Nothing watched for those.",
            )
        return added


def _offset_get() -> int:
    val = db.cache_get(OFFSET_KEY, OFFSET_TTL)
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def _offset_put(update_id: int) -> None:
    db.cache_set(OFFSET_KEY, str(update_id + 1), OFFSET_TTL)


def drain(bot: Bot, once: bool = False) -> int:
    """Long-poll loop. Returns processed count for --once mode."""
    offset = _offset_get()
    processed = 0
    while True:
        updates = tg.get_updates(offset=offset)
        if not updates:
            if once:
                return processed
            time.sleep(1.0)
            continue
        for u in updates:
            bot.handle_update(u)
            offset = max(offset, u.get("update_id", 0))
            _offset_put(offset)
            processed += 1
        if once:
            return processed


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="decoded.report Telegram bot")
    ap.add_argument("--poll", action="store_true", help="resident long-poll loop")
    ap.add_argument("--once", action="store_true", help="drain pending updates once")
    ap.add_argument("--set-webhook", metavar="URL",
                    help="register URL as the production webhook and exit")
    args = ap.parse_args()
    if not tg.available():
        print("TELEGRAM_BOT_TOKEN missing in product/.env : nothing to do.")
        raise SystemExit(1)
    if args.set_webhook:
        ok = tg.set_webhook(args.set_webhook)
        print("webhook set:" , ok)
        raise SystemExit(0 if ok else 1)
    if not (args.poll or args.once):
        print("nothing to do: pass --poll or --once (or --set-webhook URL)")
        raise SystemExit(1)
    bot = Bot()
    if args.once:
        n = drain(bot, once=True)
        print(json.dumps({"processed": n}))
        return
    log.info("telegram bot polling started")
    while True:
        try:
            drain(bot)
        except Exception as exc:
            log.error("poll crashed: %s ; restarting in 5s", exc)
            time.sleep(5.0)


if __name__ == "__main__":
    main()

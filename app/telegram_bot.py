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
    python -m app.telegram_bot --drop-pending    # flush queued updates, exit
    python -m app.telegram_bot --set-webhook URL # production wiring helper

The webhook route lives in the FastAPI app at POST /tg/webhook/{secret};
--set-webhook registers it with Telegram. Without a token everything logs
WOULD-NOTIFY / refuses politely: nothing else in the product blocks."""

import argparse
import json
import logging
import re
import time
from collections import deque

from . import db
from . import telegram_client as tg
from .config import DB_PATH, TELEGRAM_BOT_TOKEN, public_url
from .sec_client import SecClient

log = logging.getLogger(__name__)

MAX_WATCH = 8
TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
OFFSET_KEY = "tg:update_offset"
OFFSET_TTL = 10 * 365 * 86400
# updates older than this were typed long ago (backlog from a queue nobody
# drained, or a redelivery after a crash); answering them reads as spam
STALE_UPDATE_S = 10 * 60
# catastrophic tripwire only: with the confirm protocol done right,
# floods cannot happen structurally; a human never approaches this
RATE_WINDOW_S = 60.0
RATE_MAX_PER_CHAT = 12
# periodic status line so a stuck worker is visible in plain logs
HEARTBEAT_S = 300.0


def _token_fp() -> str:
    return f"{TELEGRAM_BOT_TOKEN[:3]}..{len(TELEGRAM_BOT_TOKEN)}chars" \
        if TELEGRAM_BOT_TOKEN else "(missing)"

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
        self._sent_at = {}   # chat_id -> deque of monotonic send timestamps
        db.init_db()

    def _send(self, chat_id: str, html: str) -> bool:
        """Send, gated solely by a wide anti-runaway tripwire. Normal
        interactive use never touches it; only a runaway storm does."""
        now = time.monotonic()
        q = self._sent_at.setdefault(chat_id, deque())
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_MAX_PER_CHAT:
            log.warning("rate limit hit, dropping reply to chat=%s", chat_id)
            return False
        if tg.send_html(chat_id, html):
            q.append(now)
            return True
        return False

    # ---- command handlers ------------------------------------------------

    def handle_update(self, update: dict) -> None:
        """Process one Telegram update. Never raises to the caller loop."""
        try:
            msg = update.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "")
            text = (msg.get("text") or "").strip()
            if not chat_id or not text:
                return
            if ((msg.get("from") or {}).get("is_bot")):
                # never react to another bot's output: two reflexive bots in
                # one chat otherwise feed each other in an echo cascade
                log.debug("ignored bot-sourced message in chat=%s", chat_id)
                return
            if text.startswith("/start"):
                self._handle_start(chat_id, text)
            elif text.startswith("/watch"):
                self._handle_watch(chat_id, text)
            elif text.startswith("/unwatch"):
                self._handle_unwatch(chat_id, text)
            elif text.startswith("/list"):
                self._send(chat_id, _fmt_watchlist(db.subs_for_chat(chat_id)))
            else:
                if ((msg.get("chat") or {}).get("type") != "private"):
                    return  # stay silent on chatter from other humans/bots
                self._send(chat_id,
                           "I only speak /start, /watch, /unwatch and /list.")
        except Exception as exc:
            log.warning("bot update failed: %s", exc)

    def _handle_start(self, chat_id: str, text: str) -> None:
        payload = text[len("/start"):].strip().removeprefix(tg.WATCH_PREFIX).upper()
        wanted = _sanitize_list(payload.replace("%2C", ","))
        self._send(chat_id, WELCOME)
        if wanted:
            self._register(chat_id, wanted, source="deeplink")
            self._send(chat_id, _fmt_watchlist(db.subs_for_chat(chat_id)))

    def _handle_watch(self, chat_id: str, text: str) -> None:
        raw = text.split(" ", 1)[1].strip() if " " in text else ""
        wanted = _sanitize_list(raw)
        if not wanted:
            self._send(
                chat_id,
                "Send tickers like <code>/watch TICKER</code> (up to 8, comma separated).",
            )
            return
        self._register(chat_id, wanted, source="command")
        self._send(chat_id, _fmt_watchlist(db.subs_for_chat(chat_id)))

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
        self._send(chat_id, reply)

    def _register(self, chat_id: str, wanted: list, source: str) -> list:
        resolved, unknown = [], []
        for t in wanted:
            resolved.append(t) if self.sec.resolve(t) else unknown.append(t)
        added = db.sub_add(chat_id, resolved, source=source, utm="telegram")
        for t in added:
            db.log_event("lead", ticker=t, utm="telegram", captured=1)
        if unknown:
            self._send(
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


def _offset_put(next_wire_offset: int) -> None:
    """Store the exact value the next getUpdates call must send. The value
    must ALREADY be uid+1 past everything considered handled: Telegram
    confirms ids strictly below this number, and anything left below it is
    redelivered forever."""
    db.cache_set(OFFSET_KEY, str(int(next_wire_offset)), OFFSET_TTL)


def drain(bot: Bot, once: bool = False) -> int:
    """Long-poll loop. Returns processed count for --once mode."""
    offset = _offset_get()
    high_water = offset - 1          # last update_id this process handled
    started = time.monotonic()
    last_beat = started if once else 0.0
    anomaly_logged = False
    processed = 0
    while True:
        now_mono = time.monotonic()
        if now_mono - last_beat >= HEARTBEAT_S:
            log.info("heartbeat: up %.0fs, request_offset=%d, last_handled=%d, "
                     "handled_total=%d", now_mono - started, offset, high_water,
                     processed)
            last_beat = now_mono
        updates = tg.get_updates(offset=offset)
        if updates:
            first, last = updates[0].get("update_id"), updates[-1].get("update_id")
            if len(updates) == 1:
                log.debug("getUpdates offset=%d -> 1 update id=%s", offset, first)
            else:
                log.debug("getUpdates offset=%d -> %d updates ids=%s..%s",
                          offset, len(updates), first, last)
        if not updates:
            if once:
                return processed
            time.sleep(1.0)
            continue
        for u in updates:
            uid = u.get("update_id", 0)
            # THE protocol invariant: confirm STRICTLY PAST this id before
            # answering it. Requesting with a lower number used to leave each
            # batch's tail unconfirmed: redelivered forever at full poll speed
            # (= the endless welcome storm), while replies got rate-suppressed.
            next_wire = max(offset, uid + 1)
            try:
                _offset_put(next_wire)
            except Exception as exc:
                # Without a durable cursor we must not answer anything from
                # this batch: all of it would come back unconfirmed later.
                log.error("cannot persist tg offset=%d; aborting batch with "
                          "zero replies: %s", next_wire, exc)
                return processed
            offset = next_wire
            if uid <= high_water:
                # Already handled (foreign consumer, rebased cursor, replayed
                # state). Confirm-only: never answers twice by design.
                if not anomaly_logged:
                    log.warning("update_id=%s re-delivered although "
                                "last_handled=%d; confirmed without answering "
                                "(second consumer on token? stale copy?)",
                                uid, high_water)
                    anomaly_logged = True
                continue
            high_water = uid
            msg_ts = (u.get("message") or {}).get("date") or 0
            age = time.time() - msg_ts
            if age > STALE_UPDATE_S:
                log.info("skipped stale update %s (%ds old): backlog replay",
                         uid, int(age))
                continue
            bot.handle_update(u)
            processed += 1
        if once:
            return processed


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request URL at INFO, and our Telegram URLs embed the
    # bot token: never let those into shared/collected log streams.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description="decoded.report Telegram bot")
    ap.add_argument("--poll", action="store_true", help="resident long-poll loop")
    ap.add_argument("--once", action="store_true", help="drain pending updates once")
    ap.add_argument("--drop-pending", action="store_true",
                    help="flush every queued update without replying, then exit")
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
    if args.drop_pending:
        ok = tg.drop_pending_updates()
        print(json.dumps({"dropped_pending": ok}))
        raise SystemExit(0 if ok else 1)
    if not (args.poll or args.once):
        print("nothing to do: pass --poll or --once (or --set-webhook URL)")
        raise SystemExit(1)
    tg.set_my_commands()
    bot = Bot()
    if args.once:
        n = drain(bot, once=True)
        print(json.dumps({"processed": n}))
        return
    log.info("telegram bot polling started: token=%s db=%s offset=%d",
             _token_fp(), DB_PATH, _offset_get())
    delay = 5.0
    while True:
        try:
            drain(bot)
            delay = 5.0
        except Exception as exc:
            log.error("poll crashed: %s ; restarting in %.0fs", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2.0, 60.0)


if __name__ == "__main__":
    main()

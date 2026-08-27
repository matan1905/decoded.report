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
from .config import public_url
from .sec_client import SecClient

log = logging.getLogger(__name__)

MAX_WATCH = 8
TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
OFFSET_KEY = "tg:update_offset"
OFFSET_TTL = 10 * 365 * 86400
# updates older than this were typed long ago (backlog from a queue nobody
# drained, or a redelivery after a crash); answering them reads as spam
STALE_UPDATE_S = 10 * 60
# hard ceilings so a broken loop can physically never flood a chat:
RATE_WINDOW_S = 60.0
RATE_MAX_PER_CHAT = 3
DUP_SUPPRESS_S = 60.0

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
        self._last_hash = {} # chat_id -> (payload hash, monotonic ts)
        self._start_at = {}  # chat_id -> monotonic ts of last welcome sent
        db.init_db()

    def _send(self, chat_id: str, html: str, urgent: bool = False) -> bool:
        """Send with anti-flood ceilings. A reply is dropped when this chat
        already got RATE_MAX_PER_CHAT replies inside the window, or the
        identical payload was sent within DUP_SUPPRESS_S: even a crash loop
        that keeps redelivering one /start cannot spam beyond these caps.
        urgent=True (short usage/help echoes) skips only the duplicate check,
        never the rate cap, so tapping /watch always answers."""
        now = time.monotonic()
        q = self._sent_at.setdefault(chat_id, deque())
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_MAX_PER_CHAT:
            log.warning("rate limit hit, dropping reply to chat=%s", chat_id)
            return False
        h = hash(html)
        prev = self._last_hash.get(chat_id)
        if not urgent and prev and prev[0] == h and now - prev[1] < DUP_SUPPRESS_S:
            log.debug("duplicate suppressed for chat=%s", chat_id)
            return False
        if tg.send_html(chat_id, html):
            q.append(now)
            self._last_hash[chat_id] = (h, now)
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
                           "I only speak /start, /watch, /unwatch and /list.",
                           urgent=True)
        except Exception as exc:
            log.warning("bot update failed: %s", exc)

    def _handle_start(self, chat_id: str, text: str) -> None:
        # extra START taps within a minute are usually accidents or retries;
        # acknowledge the first, go quiet on the rest
        now = time.monotonic()
        last = self._start_at.get(chat_id)
        if last and now - last < 60.0:
            return
        self._start_at[chat_id] = now
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
                urgent=True,
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


def _offset_put(update_id: int) -> None:
    db.cache_set(OFFSET_KEY, str(update_id + 1), OFFSET_TTL)


def drain(bot: Bot, once: bool = False) -> int:
    """Long-poll loop. Returns processed count for --once mode."""
    offset = _offset_get()
    high_water = offset - 1          # last update_id this process handled
    anomaly_logged = False
    processed = 0
    while True:
        updates = tg.get_updates(offset=offset)
        if updates:
            log.debug("getUpdates batch n=%d ids=%s..%s from offset=%d",
                      len(updates), updates[0].get("update_id"),
                      updates[-1].get("update_id"), offset)
        if not updates:
            if once:
                return processed
            time.sleep(1.0)
            continue
        for u in updates:
            uid = u.get("update_id", 0)
            # Persist the cursor BEFORE answering anything. If it cannot be
            # saved, this batch is aborted with zero replies: sending anyway
            # would leave the update unconfirmed, redelivered next poll, and
            # answered again = an infinite welcome loop. Worst case of
            # persisting first is one dropped reply after a mid-handler crash.
            try:
                _offset_put(max(offset, uid))
            except Exception as exc:
                log.error("cannot persist tg offset; dropping batch un-answered: %s", exc)
                return processed
            offset = max(offset, uid)
            if uid <= high_water:
                # Something outside our model delivered an already-handled id
                # again (second consumer on the token, replayed state, ...).
                # Confirm it and move on; never answer it twice.
                if not anomaly_logged:
                    log.warning(
                        "update_id=%s re-delivered (high_water=%s): second "
                        "consumer or rebased cursor? confirmed w/o answer",
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
    log.info("telegram bot polling started")
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

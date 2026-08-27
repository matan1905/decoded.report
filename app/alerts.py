"""Alert engine: the retention loop the audience explicitly asked for.

A cron-style pass that re-runs the red-flag scanner and share-count check
for every watched ticker, diffs the result against the last stored state,
and pushes a cited Telegram message to every chat watching that ticker.

Design rules (carry the product's trust rules into the chat):
- Every claim in an alert cites its filing (form, date, SEC link) or is
  dropped. No forecasts, no price talk, "not financial advice" footer.
- The first run for a ticker only seeds state: nobody gets a blast of
  everything-on-file on day one.
- One alert per ticker per min-interval even if it keeps changing, and a
  change detected while rate-limited stays pending until the next pass.
- No TELEGRAM_BOT_TOKEN? Every send logs WOULD-NOTIFY and the run still
  records what would have gone out (notify_log.sent = 0). Nothing else
  changes.

Run modes:
    python -m app.alerts --dry-run          # compute + print, write nothing
    python -m app.alerts                    # real pass (WOULD-NOTIFY w/o token)
    python -m app.alerts --ticker TICKER     # single-ticker pass
    python -m app.alerts --loop 3600        # stay resident, run hourly

Subscriptions are managed by the bot (python -m app.telegram_bot): users
press START on a t.me deep link or send /watch TICKERS."""

import argparse
import hashlib
import json
import logging
import time
from datetime import date, timedelta

from . import db
from .config import public_url
from . import telegram_client as tg
from .risk import annotate_flags
from .sec_client import SecClient

log = logging.getLogger(__name__)

# how fresh an 8-K must be to count as "new material filing"
MATERIAL_WINDOW_DAYS = 7
# share-count growth between passes that counts as a dilution event
DILUTION_EVENT_PCT = 2.0
# minimum hours between two emails about the same ticker
MIN_ALERT_INTERVAL_HOURS = 20


def _sig(flags_annotated: list, shares_val, material_accn: str) -> str:
    payload = json.dumps({
        "flags": sorted(
            [f.get("label"), f.get("effective"), f.get("count_all")]
            for f in flags_annotated
            if f.get("label")
        ),
        "shares": shares_val,
        "filing": material_accn or "",
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _material_filing(sec: SecClient, cik: int):
    """Newest 8-K inside the material window: accession only, no doc fetch."""
    recent = sec.submissions(cik).get("filings", {}).get("recent", {})
    cutoff = (date.today() - timedelta(days=MATERIAL_WINDOW_DAYS)).isoformat()
    best = None
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accns = recent.get("accessionNumber", [])
    for f, d, a in zip(forms, dates, accns):
        if f == "8-K" and d >= cutoff:
            if best is None or d > best["date"]:
                best = {"date": d, "accession": a,
                        "url": sec.live_filing_link(cik, a)}
    return best


def check_ticker(sec: SecClient, t: str, cik: int) -> dict:
    """Fresh scan for one ticker. Bypasses the 24h flag cache so a cron pass
    sees filings that landed today; the refreshed result re-fills the cache
    so page visitors share the same fresh data."""
    flags_raw = sec.scan_flags(cik, refresh=True)
    flags = annotate_flags(flags_raw["results"])
    shares = sec.share_count(cik)
    material = _material_filing(sec, cik)
    sig = _sig(flags, (shares or {}).get("val"), (material or {}).get("accession"))
    return {
        "ticker": t,
        "flags": flags,
        "shares_val": (shares or {}).get("val"),
        "shares_form": (shares or {}).get("form"),
        "shares_end": (shares or {}).get("end"),
        "shares_accn": (shares or {}).get("accn"),
        "material": material,
        "sig": sig,
    }


def diff_states(old: dict, new: dict) -> list:
    """Human-readable, citation-backed list of what changed since last pass."""
    changes = []

    old_flags = {f.get("label"): (f.get("effective"), f.get("count_all"))
                 for f in (old.get("flags") or [])}
    for f in new.get("flags") or []:
        label = f.get("label")
        cur_eff, cur_all = f.get("effective"), f.get("count_all")
        prev_eff, prev_all = old_flags.get(label, (None, None))
        grew_eff = (cur_eff or 0) > (prev_eff or 0) if cur_eff is not None else False
        grew_all = (cur_all or 0) > (prev_all or 0) if cur_all is not None else False
        if grew_eff:
            top = next((h["url"] for h in (f.get("top") or [])[:1]), None)
            changes.append({
                "kind": "flag",
                "text": (
                    f"{label}: now {cur_eff} verified warning"
                    f"{'s' if cur_eff != 1 else ''} this year"
                    + (f" (was {prev_eff})" if prev_eff is not None else "")
                    + (f", {cur_all} all time" if cur_all else "")
                    + ". We open the linked documents and filter boilerplate before counting."
                ),
                "url": top,
            })
        elif grew_all and cur_eff == 0 and not (prev_all or 0):
            # historical count appeared where none existed before
            changes.append({
                "kind": "flag",
                "text": f"{label}: {cur_all} mentions on record across history, none verified this year.",
                "url": None,
            })

    ov, nv = old.get("shares_val"), new.get("shares_val")
    try:
        ov_f, nv_f = float(ov), float(nv)
    except (TypeError, ValueError):
        ov_f = nv_f = None
    if nv_f and ov_f and nv_f > ov_f * (1 + DILUTION_EVENT_PCT / 100):
        pct = (nv_f - ov_f) / ov_f * 100
        cite = ""
        if new.get("shares_accn"):
            cite = f" Reported in form {new.get('shares_form')} as of {new.get('shares_end')}."
        share_url = None
        if new.get("cik") and new.get("shares_accn"):
            share_url = (
                "https://www.sec.gov/Archives/edgar/data/"
                + str(new["cik"]).zfill(10) + "/"
                + str(new["shares_accn"]).replace("-", "") + "/"
            )
        changes.append({
            "kind": "dilution",
            "text": (
                f"Shares outstanding rose from {ov_f:,.0f} to {nv_f:,.0f} "
                f"(+{pct:.1f}%): holders are being diluted.{cite}"
            ),
            "url": share_url,
        })

    om, nm = old.get("material_accn"), (new.get("material") or {}).get("accession")
    mat = new.get("material") or {}
    if nm and nm != om:
        changes.append({
            "kind": "8-K",
            "text": (
                f"New 8-K filed {mat.get('date')}: the company's own "
                "'something happened' filing. Read it while everyone else is guessing."
            ),
            "url": mat.get("url"),
        })

    return changes


def _alert_message(t: str, name: str, changes: list) -> str:
    """Telegram HTML alert: bold header, cited bullets, case-file link."""
    e = tg.esc
    bullets = "\n".join(
        "• " + e(c["text"])
        + (f' <a href="{e(c["url"])}">[source filing]</a>' if c.get("url") else "")
        for c in changes
    )
    return (
        f"<b>decoded.report · {e(t)} FILING ALERT</b>\n"
        f"{e(name or '')}\n\n"
        f"{bullets}\n\n"
        f'<a href="{public_url("/" + t)}">OPEN THE FULL CASE FILE</a>\n'
        f"\n<i>Not financial advice. Every line links to its official SEC "
        f"source. Screening, never a recommendation. /unwatch to stop.</i>"
    )


def _baseline(new: dict) -> list:
    """Compact per-flag baseline stored between passes."""
    return [
        [f.get("label"), f.get("effective"), f.get("count_all")]
        for f in (new.get("flags") or [])
    ]


def _parse_baseline(old: dict) -> dict:
    """Normalize a stored alert_state row into the shape diff_states reads:
    {'flags': [{'label','effective','count_all'}], 'shares_val', 'material_accn'}"""
    if not old:
        return None
    out = {"flags": [], "shares_val": old.get("shares_val"),
           "material_accn": old.get("material_accn"),
           "alerted_at": old.get("alerted_at"), "note": old.get("note")}
    raw = old.get("flags")
    if isinstance(raw, str):
        try:
            rows = json.loads(raw)
        except (TypeError, ValueError):
            rows = []
    else:
        rows = raw or []
    for label, eff, cnt_all in rows:
        out["flags"].append({"label": label, "effective": eff, "count_all": cnt_all})
    return out


def process_ticker(sec: SecClient, t: str, dry_run: bool = False, verbose: bool = True) -> dict:
    resolved = sec.resolve(t)
    if not resolved:
        out = {"ticker": t, "status": "unknown_symbol"}
        if verbose:
            log.warning("alerts: %s unknown symbol, skipping", t)
        return out
    cik, name = resolved["cik"], resolved["name"]
    new = check_ticker(sec, t, cik)
    new["cik"] = cik
    old = _parse_baseline(db.alert_state_get(t))

    if dry_run:
        changes = diff_states(old, new) if old else ["first run: state seed only"]
        out = {"ticker": t, "status": "dry_run", "changes": len(changes),
               "sig": new["sig"][:12]}
        if verbose:
            log.info("[dry] %s: %s", t, out)
        return out

    if old is None:
        db.alert_state_put(
            t, new["sig"], new.get("shares_val"),
            note=f"seeded {name}"[:80],
            flags=_baseline(new),
            material_accn=(new.get("material") or {}).get("accession"),
        )
        if verbose:
            log.info("alerts: %s seeded (first pass, no email)", t)
        return {"ticker": t, "status": "seeded"}

    changes = diff_states(old, new)
    interval_ok = (
        not old.get("alerted_at")
        or (time.time() - old["alerted_at"]) >= MIN_ALERT_INTERVAL_HOURS * 3600
    )
    if not changes:
        db.alert_state_put(
            t, new["sig"], new.get("shares_val"), note=old.get("note"),
            flags=_baseline(new), material_accn=(new.get("material") or {}).get("accession"),
        )
        return {"ticker": t, "status": "no_change"}
    if not interval_ok:
        if verbose:
            log.info("alerts: %s changed but rate-limited until %.0fm",
                     t, MIN_ALERT_INTERVAL_HOURS - (time.time() - (old.get("alerted_at") or 0)) / 60)
        return {"ticker": t, "status": "rate_limited", "changes": len(changes)}

    chats = db.chats_for_ticker(t)
    sent_any, would_any = False, False
    if chats:
        message = _alert_message(t, name, changes)
        for chat_id in chats:
            sent = tg.send_html(chat_id, message)
            sent_any = sent_any or sent
            would_any = would_any or not sent
            db.log_notify(chat_id, t, "filing_alert", sent=1 if sent else 0)

    db.alert_state_put(
        t, new["sig"], new.get("shares_val"),
        alerted_at=time.time(),
        note=f"alerted {len(changes)} change(s) to {len(chats)} chat(s)"[:80],
        flags=_baseline(new), material_accn=(new.get("material") or {}).get("accession"),
    )
    status = "sent" if sent_any else ("would_notify" if would_any else "no_subs")
    if verbose:
        log.info("alerts: %s %s (%s change(s), %s chat(s))", t, status, len(changes), len(chats))
    return {"ticker": t, "status": status, "changes": len(changes), "chats": len(chats)}


def run(dry_run: bool = False, only_ticker: str = None, verbose: bool = True) -> list:
    sec = SecClient()
    # idempotent: guarantees schema + light migrations exist for CLI passes
    db.init_db()
    results = []
    if only_ticker:
        results.append(process_ticker(sec, only_ticker.upper(), dry_run=dry_run, verbose=verbose))
        return results
    watched = db.watched_tickers()
    if verbose:
        log.info("alerts: pass over %d watched ticker(s)", len(watched))
    for row in watched:
        try:
            results.append(process_ticker(sec, row["ticker"], dry_run=dry_run, verbose=verbose))
        except Exception as exc:
            log.warning("alerts: %s failed: %s", row["ticker"], exc)
            results.append({"ticker": row["ticker"], "status": "error", "error": str(exc)[:120]})
    return results


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="decoded.report alert engine pass")
    ap.add_argument("--dry-run", action="store_true", help="compute changes, write nothing")
    ap.add_argument("--ticker", help="run a single ticker instead of every watched ticker")
    ap.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                    help="stay resident and run a pass every SECONDS (cron alternative)")
    args = ap.parse_args()
    if args.loop > 0:
        while True:
            try:
                run(dry_run=args.dry_run, only_ticker=args.ticker)
            except Exception as exc:
                log.error("alert pass crashed: %s", exc)
            time.sleep(args.loop)
    else:
        results = run(dry_run=args.dry_run, only_ticker=args.ticker)
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()

import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db
from .config import APP_NAME, APP_VERSION, BASE_DIR, SEC_USER_AGENT
from .config import ADMIN_PASSWORD, ADMIN_USERNAME, DEFAULT_PUBLIC_URL, BASE_URL as CONFIG_BASE_URL
from .delta_engine import DeltaEngine, _fmt_compact
from . import osint
from . import telegram_client as tg
from .price_client import get_price
from .risk import compute_risk
from .sec_client import SecClient
from .verdict_engine import VerdictEngine, build_inputs

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title=APP_NAME, version=APP_VERSION)
admin_basic = HTTPBasic(auto_error=False)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _fmt_int(v):
    try:
        return f"{int(round(float(v))):,}"
    except Exception:
        return "n/a"


def _timestamp_ago(v):
    try:
        dt = max(0.0, time.time() - float(v))
    except Exception:
        return "?"
    if dt < 90:
        return f"{int(dt)}s ago"
    if dt < 5400:
        return f"{int(dt // 60)}m ago"
    if dt < 172800:
        return f"{int(dt // 3600)}h ago"
    return f"{int(dt // 86400)}d ago"


templates.env.filters["fmt_int"] = _fmt_int
templates.env.filters["fmt_compact"] = _fmt_compact
templates.env.filters["timestamp_ago"] = _timestamp_ago

sec = SecClient()
delta_engine = DeltaEngine(sec)
verdict_engine = VerdictEngine()

TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")

ENGINE_CACHE_TTL = 6 * 3600  # 6h


@app.on_event("startup")
def _startup():
    db.init_db()
    db.cache_clear_expired()
    log.info("started %s v%s, price mode: %s", APP_NAME, APP_VERSION, _price_state() or "none")
    threading.Thread(target=_warmup, daemon=True).start()


def _warmup():
    """Background: pay the one-time global costs (OFAC list) and pre-warm
    whatever tickers real visitors actually demanded recently, so returning
    traffic never pays cold-start. No hardcoded examples."""
    try:
        osint.ofac_screen("Warmup Probe Inc")
        log.info("warmup: sanctions list loaded")
    except Exception as exc:
        log.warning("warmup ofac failed: %s", exc)
    demanded = [r["ticker"] for r in db.recent_searched(limit=6, days=45)]
    if not demanded:
        log.info("warmup: no demand history yet, skipping ticker pre-warm")
        return
    for t in demanded:
        try:
            resolved = sec.resolve(t)
            if not resolved:
                continue
            _snapshot_data(t, resolved["cik"], resolved["name"])
        except Exception as exc:
            log.warning("warmup %s failed: %s", t, exc)
    log.info("warmup: %d demanded ticker(s) ready", len(demanded))


def _price_state() -> str:
    from . import price_client
    s = price_client._state()
    if s == "twelvedata":
        return "twelvedata"
    if s == "finnhub":
        return "finnhub"
    return None


def require_admin(credentials: HTTPBasicCredentials = Depends(admin_basic)):
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin password is not configured")
    if not credentials or not (
        secrets.compare_digest(credentials.username, ADMIN_USERNAME)
        and secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    ):
        raise HTTPException(
            status_code=401,
            detail="Admin authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _sanitize_ticker(raw: str) -> str:
    t = (raw or "").strip().upper()
    t = re.sub(r"[^A-Z0-9.\-]", "", t)
    return t if TICKER_RE.match(t) else ""


def _ticker_list_param(raw: str, cap: int = 8) -> list:
    """Parse a user-supplied ticker list (comma/space/semicolon separated)
    into sanitized, deduplicated tickers, capped like the Bag Check."""
    parts = [p for p in re.split(r"[,\s;]+", (raw or "").upper()) if p]
    seen, out = set(), []
    for p in parts:
        st = _sanitize_ticker(p)
        if st and st not in seen and len(out) < cap:
            seen.add(st)
            out.append(st)
    return out


def _utm(request: Request):
    q = parse_qs(urlparse(str(request.url)).query)
    return (q.get("utm_source", [""])[0] or "").strip()


def _base_url(request: Request) -> str:
    return str(request.url).split("?")[0]


def _origin(request: Request = None) -> str:
    """Public origin for generated links: the configured BASE_URL wins so a
    reverse proxy or CLI context still emits canonical URLs; otherwise the
    incoming request's own origin keeps local runs honest."""
    if CONFIG_BASE_URL:
        return CONFIG_BASE_URL
    if request is not None:
        return str(request.base_url).rstrip("/")
    return DEFAULT_PUBLIC_URL


# ---- landing + search ---------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    t = (request.query_params.get("t") or "").strip()
    utm = _utm(request)
    if t:
        return _snapshot_tpl(request, t)
    hot = [r["ticker"] for r in db.recent_searched(limit=8)]
    return templates.TemplateResponse(
        request, "landing.html",
        {"app_name": APP_NAME, "app_version": APP_VERSION, "hot": hot},
    )


@app.post("/search")
async def search_submit(ticker: str = Form(...), request: Request = None):
    lookup = _sanitize_ticker(ticker)
    if not lookup:
        return RedirectResponse(url="/", status_code=303)
    found = int(bool(sec.resolve(lookup)))
    utm = _utm(request)
    db.log_search(lookup, found=found, utm=utm or "search")
    db.log_event("search", ticker=lookup, utm=utm or "search")
    return RedirectResponse(url=f"/{lookup}?utm_source=search", status_code=303)


# ---- snapshot page ------------------------------------------------------------

def _snapshot_data(t: str, cik: int, name: str, deep: bool = True) -> dict:
    """Gather every signal for a ticker report. Pure data: no request, so
    the startup warmup can reuse it to fill caches. deep=False skips the
    three document-heavy probes (insider Form 4 XMLs, domain intel, breaking
    8-K text); those stream in over the websocket instead so a cold ticker
    page renders fast."""
    # every gather step degrades independently: a flaky SEC response or a
    # rate-limited probe must show an honest "unavailable" slot, never a 500
    try:
        submissions = sec.submissions(cik)
    except Exception as exc:
        log.warning("submissions unavailable for %s: %s", t, exc)
        submissions = {}
    identity = _identity(submissions)
    try:
        shares = sec.share_count(cik)
        share_hist = sec.share_history(cik)
        filings = sec.recent_filings(cik, 6)
    except Exception as exc:
        log.warning("filing index unavailable for %s: %s", t, exc)
        shares, share_hist, filings = None, [], []
    try:
        flags = sec.scan_flags(cik)
    except Exception as exc:
        log.warning("flag scan unavailable for %s: %s", t, exc)
        flags = {"results": [], "range_start": "", "range_end": ""}
    try:
        insider = sec.insider_summary(cik)
    except Exception as exc:
        log.warning("insider summary unavailable for %s: %s", t, exc)
        insider = {"count_90": 0, "count_30": 0, "last_date": None}
    insider_flows = None
    if deep:
        try:
            insider_flows = sec.insider_flows(cik)
        except Exception as exc:
            log.warning("insider flows failed for %s: %s", t, exc)
            insider_flows = None
    try:
        price = get_price(t)
    except Exception as exc:
        log.warning("price unavailable for %s: %s", t, exc)
        price = {"source": "none", "degraded": True, "cached": False}

    # keyless depth: reuse the cached delta output for runway + cash trend
    runway_months, cash_delta_pct, monthly_burn, cash_on_hand = None, None, None, None
    delta = None
    try:
        delta = _cached_delta(t, cik, deep=deep)
        for c in delta.get("changes", []):
            if c.get("key") == "burn":
                if c.get("runway_months") is not None:
                    runway_months = c["runway_months"]
                if c.get("monthly_burn"):
                    monthly_burn = c["monthly_burn"]
            elif c.get("label") == "Cash and equivalents" and not c.get("is_na"):
                if c.get("delta_pct") is not None:
                    cash_delta_pct = c["delta_pct"]
                cur = c.get("current") or {}
                if cur.get("val") is not None:
                    cash_on_hand = cur["val"]
    except Exception as exc:
        log.warning("delta inputs unavailable for risk score %s: %s", t, exc)
    try:
        critical_8k = sec.critical_8k_history(cik)
    except Exception as exc:
        log.warning("critical 8-K scan failed for %s: %s", t, exc)
        critical_8k = {"rows": [], "scored": {}}
    try:
        periodic = sec.last_periodic(cik)
    except Exception as exc:
        log.warning("periodic check failed for %s: %s", t, exc)
        periodic = {"status": "unknown"}
    try:
        risk = compute_risk(flags["results"], share_hist, runway_months, cash_delta_pct,
                            critical_8k.get("scored"))
    except Exception as exc:
        log.warning("risk computation failed for %s: %s", t, exc)
        risk = None

    # OSINT probes (keyless, each degrades independently)
    pipeline = osint.offering_pipeline(submissions)
    domain = sanctions = None
    if deep:
        try:
            domain = osint.domain_intel(sec, cik)
        except Exception as exc:
            log.warning("domain intel failed for %s: %s", t, exc)
            domain = None
        try:
            sanctions = osint.ofac_screen(name)
        except Exception as exc:
            log.warning("sanctions screen failed for %s: %s", t, exc)
            sanctions = {"status": "unavailable"}
    # cheap sync knowledge: did an 8-K land this week at all?
    from datetime import date, timedelta as _td
    cutoff7 = (date.today() - _td(days=7)).isoformat()
    eightk_recent = any(
        f == "8-K" and d >= cutoff7
        for f, d in zip(submissions.get("filings", {}).get("recent", {}).get("form", []),
                        submissions.get("filings", {}).get("recent", {}).get("filingDate", []))
    )

    # presentation extras: insider flow chart + who-else-is-watching pills
    chart = None
    if insider_flows:
        try:
            from .risk import insider_chart
            chart = insider_chart(insider_flows.get("rows") or [])
        except Exception as exc:
            log.warning("insider chart failed for %s: %s", t, exc)
    try:
        related = db.related_tickers(t)
    except Exception as exc:
        log.warning("related tickers failed for %s: %s", t, exc)
        related = []

    # unified page: delta + verdict + chart histories all in one place.
    # One delta computation feeds runway extraction here and section 04;
    # the cached result is reused rather than computed twice.
    verdict = None
    cash_chart = None
    revenue_chart = None
    try:
        if delta is None:
            delta = _cached_delta(t, cik, deep=deep)
        verdict = _cached_verdict(t, cik, price, _going_concern_flag(risk))
        hist = delta_engine.histories(cik)
        from .risk import bar_chart
        cash_chart = bar_chart(hist["cash"])
        revenue_chart = bar_chart(hist["revenue"])
    except Exception as exc:
        log.warning("unified engines degraded for %s: %s", t, exc)

    return {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "ticker": t,
        "name": name,
        "cik": cik,
        "cik10": str(cik).zfill(10),
        "identity": identity,
        "shares": shares,
        "share_link": sec.live_filing_link(cik, shares["accn"], sec.primary_doc_for(cik, shares["accn"])) if shares and shares.get("accn") else None,
        "filings": filings,
        "flags": risk["flags"] if risk else flags["results"],
        "flag_range": flags["range_start"],
        "flag_end": flags["range_end"],
        "risk": risk,
        "dilution": risk.get("dilution") if risk else None,
        "critical_8k": critical_8k,
        "periodic": periodic,
        "runway_months": runway_months,
        "cash_delta_pct": cash_delta_pct,
        "monthly_burn": monthly_burn,
        "cash_on_hand": cash_on_hand,
        "insider": insider,
        "insider_flows": insider_flows,
        "insider_chart": chart,
        "pipeline": pipeline,
        "domain": domain,
        "sanctions": sanctions,
        "related": related,
        "delta": delta,
        "changes": (delta or {}).get("changes", []),
        "new_filing": (delta or {}).get("new_filing"),
        "eightk_recent": eightk_recent,
        "verdict": verdict,
        "cash_chart": cash_chart,
        "revenue_chart": revenue_chart,
        "price": price,
        "show_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=" + str(cik).zfill(10),
        "user_agent_contact": SEC_USER_AGENT,
    }


def _hot_tickers(limit: int = 4) -> list:
    return [r["ticker"] for r in db.recent_searched(limit=limit)]


def _snapshot_tpl(request: Request, ticker: str):
    t = _sanitize_ticker(ticker)
    utm = _utm(request)
    if not t:
        return templates.TemplateResponse(
            request, "landing.html", {"app_name": APP_NAME, "app_version": APP_VERSION},
        )
    resolved = sec.resolve(t)
    if not resolved:
        db.log_event("notfound", ticker=t, utm=utm)
        return templates.TemplateResponse(
            request, "notfound.html", {"app_name": APP_NAME, "app_version": APP_VERSION, "ticker": t,
                                       "page_url": _base_url(request), "tg_watch_url": tg.watch_link([t]),
                                       "hot": _hot_tickers()},
            status_code=404,
        )
    cik, name = resolved["cik"], resolved["name"]
    db.log_event("page_view", ticker=t, utm=utm)

    data = _snapshot_data(t, cik, name)
    data["request"] = request
    data["share_url"] = _base_url(request) + "?utm_source=share"
    data["page_url"] = _base_url(request)
    data["og_image_url"] = _origin(request) + f"/og/{t}.png"
    data["tg_watch_url"] = tg.watch_link([t])
    data["hot"] = _hot_tickers()
    try:
        return templates.TemplateResponse(request, "snapshot.html", data)
    except Exception:
        # last-resort net: a malformed row or a template regression must
        # never turn the report page into a bare framework 500
        log.exception("snapshot render failed for %s", t)
        return HTMLResponse(
            content=(
                "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
                f"<title>{APP_NAME}</title></head>"
                "<body style=\"background:#0b0e13;color:#e8ebf2;font-family:monospace;"
                "max-width:640px;margin:80px auto;padding:0 20px;\">"
                f"<h1>{APP_NAME}</h1>"
                f"<p>The full report for <b>{t}</b> could not be assembled just now."
                " One section misbehaved; everything else is unaffected.</p>"
                "<p><a href=\"/\" style=\"color:#82a7ff;\">Try again or decode another ticker</a></p>"
                "</body></html>"
            ),
            status_code=500,
        )


def _form_label(f: str) -> str:
    if not f:
        return "n/a"
    return f


def _identity(submissions: dict) -> dict:
    addresses = submissions.get("addresses", {})
    biz = addresses.get("business", {})
    street = biz.get("street1") or ""
    if biz.get("street2"):
        street += ", " + biz["street2"]
    location = ", ".join(x for x in [street, biz.get("city"), biz.get("stateOrCountry")] if x)
    return {
        "sic": submissions.get("sic"),
        "sic_description": submissions.get("sicDescription"),
        "exchange": submissions.get("exchange"),
        "fiscal_year_end": submissions.get("fiscalYearEnd"),
        "location": location,
        "cik": submissions.get("cik"),
        "company_name": submissions.get("name"),
    }


# ---- telegram: production webhook ---------------------------------------------

@app.post("/tg/webhook/{secret}")
async def tg_webhook(secret: str, request: Request):
    """Production update intake (Telegram delivers updates here when the
    bot is in webhook mode; local dev uses the --poll CLI instead). The
    path secret must match TG_WEBHOOK_SECRET, mirroring Telegram's own
    X-Telegram-Bot-Api-Secret-Token header check."""
    from .config import TG_WEBHOOK_SECRET
    from .telegram_bot import Bot
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not TG_WEBHOOK_SECRET or secret != TG_WEBHOOK_SECRET or \
            (header and header != TG_WEBHOOK_SECRET):
        return JSONResponse({"ok": False}, status_code=403)
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": True})
    # answer Telegram immediately; processing is quick but never block delivery
    from starlette.concurrency import run_in_threadpool
    await run_in_threadpool(Bot().handle_update, update)
    return JSONResponse({"ok": True})


# ---- admin / health ------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True, "version": APP_VERSION, "subs_count": db.sub_count(),
            "watched": db.sub_rows_total(), "price_mode": _price_state(),
            "tg_bot": bool(tg.bot_username()), "tg_token": tg.available()}# ---- market-data websocket (rate-limited Massive slots) ------------------------

@app.websocket("/ws/market")
async def ws_market(websocket: WebSocket):
    """Streams the market-data slots (price, splits, float, short interest)
    for one or more tickers. The free Massive tier allows 5 calls/min, so
    every call goes through the shared rate limiter; the client gets honest
    'wait' updates while slots free up. Cache hits cost nothing."""
    await websocket.accept()
    from starlette.concurrency import run_in_threadpool
    from . import massive_client as mkt

    raw = websocket.query_params.get("t", "")
    parts = [p for p in re.split(r"[,\s;]+", raw.upper()) if p]
    wanted = []
    for p in parts:
        st = _sanitize_ticker(p)
        if st and st not in wanted:
            wanted.append(st)
        if len(wanted) >= 8:
            break
    try:
        if not wanted or not mkt.available():
            await websocket.send_json({"kind": "unavailable"})
            await websocket.close()
            return
        total = len(wanted)
        for idx, t in enumerate(wanted):
            await websocket.send_json({
                "kind": "status", "ticker": t,
                "msg": f"LOADING MARKET DATA FOR {t} ({idx + 1}/{total})",
            })
            jobs = [
                ("price", mkt.prev_close, t),
                ("splits", mkt.splits, t),
                ("float", mkt.free_float, t),
                ("short", mkt.short_interest, t),
            ]
            for kind, fn, arg in jobs:
                if not mkt.is_cached(kind, t):
                    wait = mkt.limiter.seconds_until_slot()
                    if wait > 1:
                        await websocket.send_json({"kind": "wait", "seconds": wait})
                data = await run_in_threadpool(fn, arg)
                if data is not None or kind in ("splits", "short"):
                    await websocket.send_json({
                        "kind": kind, "ticker": t,
                        "data": data if data is not None else None,
                    })

            # osint slots (keyless, no rate limiter; caches make warm runs free)
            resolved = sec.resolve(t)
            if resolved:
                cik = resolved["cik"]
                company_name = resolved.get("name") or ""

                def domain_probe():
                    return osint.domain_intel(sec, cik)
                domain = await run_in_threadpool(domain_probe)
                await websocket.send_json({"kind": "domain", "ticker": t, "data": domain})

                def flows_probe():
                    from .risk import insider_chart
                    flows = sec.insider_flows(cik)
                    if flows:
                        try:
                            flows["chart"] = insider_chart(flows.get("rows") or [])
                        except Exception:
                            pass
                    return flows
                flows = await run_in_threadpool(flows_probe)
                await websocket.send_json({"kind": "insider", "ticker": t, "data": flows})

                def breaking_probe():
                    delta = _cached_delta(t, cik, deep=True)
                    return (delta or {}).get("new_filing")
                breaking = await run_in_threadpool(breaking_probe)
                await websocket.send_json({"kind": "breaking", "ticker": t, "data": breaking})

                # deeper osint seams: legal identity (LEI) with full family
                # tree, institutional 13F holders, archive history, certificate
                # record, archived WHOIS registrant history, DNS/mail records.
                # Each degrades independently.
                def gleif_probe():
                    return osint.gleif_screen(company_name)
                gleif = await run_in_threadpool(gleif_probe)
                await websocket.send_json({"kind": "gleif", "ticker": t, "data": gleif})

                def holders_probe():
                    return osint.institutional_holders(sec, company_name)
                holders = await run_in_threadpool(holders_probe)
                await websocket.send_json({
                    "kind": "holders", "ticker": t,
                    "data": holders or {"status": "unavailable"},
                })

                dom_name = (domain or {}).get("domain") or ""
                if dom_name:
                    def wayback_probe():
                        return osint.wayback_history(dom_name)
                    wayback = await run_in_threadpool(wayback_probe)
                    await websocket.send_json({"kind": "wayback", "ticker": t, "data": wayback})

                    def certs_probe():
                        return osint.cert_history(dom_name)
                    certs = await run_in_threadpool(certs_probe)
                    await websocket.send_json({"kind": "certs", "ticker": t, "data": certs})

                    def whois_probe():
                        return osint.whois_history(dom_name)
                    whoishist = await run_in_threadpool(whois_probe)
                    await websocket.send_json({
                        "kind": "whoishist", "ticker": t,
                        "data": whoishist or {"status": "unavailable"},
                    })

                    def dns_probe():
                        return osint.dns_intel(dom_name, (domain or {}).get("nameservers"))
                    dns = await run_in_threadpool(dns_probe)
                    await websocket.send_json({"kind": "dns", "ticker": t, "data": dns})
                else:
                    for kind in ("wayback", "certs", "whoishist", "dns"):
                        await websocket.send_json({
                            "kind": kind, "ticker": t,
                            "data": {"status": "no_domain"},
                        })

            await websocket.send_json({"kind": "ticker_done", "ticker": t})
        await websocket.send_json({"kind": "all_done"})
    except WebSocketDisconnect:
        return
    except Exception as exc:
        log.warning("market websocket failed: %s", exc)
        try:
            await websocket.send_json({"kind": "error", "msg": str(exc)[:120]})
            await websocket.close()
        except Exception:
            pass


# ---- admin / health ------------------------------------------------------------

@app.get("/admin/subs")
def admin_subs(request: Request, _admin: str = Depends(require_admin)):
    return templates.TemplateResponse(
        request, "admin.html",
        {
            "app_name": APP_NAME, "app_version": APP_VERSION,
            "count": db.sub_count(), "subs_total": db.sub_rows_total(),
            "subs": db.recent_subs(20),
            "stats": db.demand_stats(7), "hot": db.recent_searched(12, days=30),
            "events": db.recent_events(40, days=30),
            "alert_states": db.alert_states(), "notify_logs": db.recent_notify_logs(15),
            "notify_sent_7d": db.notify_sent_7d(),
            "watched_count": len(db.watched_tickers()),
            "tg_ready": tg.available(), "tg_bot": tg.bot_username(),
        },
    )


# keep the old owner-console path alive: same page, new name
@app.get("/admin/leads")
def admin_leads(request: Request, _admin: str = Depends(require_admin)):
    return RedirectResponse(url="/admin/subs", status_code=302)


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "legal.html", {
        "app_name": APP_NAME, "page": "Privacy Policy",
    })


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(request, "legal.html", {
        "app_name": APP_NAME, "page": "Terms of Use",
    })


# ---- OG stamp cards (share-preview PNGs) ----------------------------------------

@app.get("/og/bag.png", response_class=Response)
def og_bag(request: Request):
    """Bag Check lineup card: ranked worst-first share image. Must be
    registered before the /og/{ticker}.png catch-all."""
    from . import ogimg
    raw = request.query_params.get("t") or ""
    parts = [p for p in re.split(r"[,\s;]+", raw.upper()) if p]
    seen, wanted = set(), []
    for p in parts:
        st = _sanitize_ticker(p)
        if st and st not in seen and len(wanted) < 8:
            seen.add(st)
            wanted.append(st)
    if not wanted:
        return RedirectResponse(url="/og/default.png", status_code=302)
    rows = []
    for t in wanted:
        resolved = sec.resolve(t)
        if not resolved:
            rows.append({"ticker": t[:10], "score": None, "band": None,
                         "cls": "dim", "error": True})
            continue
        score = band = cls = None
        try:
            risk = _risk_full(t, resolved["cik"])
            if risk:
                score, band, cls = risk["score"], risk["band"], risk["cls"]
        except Exception as exc:
            log.warning("bag og risk failed for %s: %s", t, exc)
        rows.append({"ticker": t, "score": score, "band": band or "",
                     "cls": cls or "warn", "error": False})
    try:
        png = ogimg.bag_png(rows)
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600"})
    except Exception as exc:
        log.warning("bag og generation failed: %s", exc)
        return RedirectResponse(url="/og/default.png", status_code=302)


def _last_close(t: str):
    """Best-effort last close for generated share cards: cached sources only
    where possible, so an OG image never blocks on a rate-limited slot."""
    try:
        from . import massive_client as mkt
        if mkt.available() and mkt.is_cached("price", t):
            d = mkt.prev_close(t) or {}
            if d.get("close"):
                return d.get("close")
    except Exception as exc:
        log.warning("massive price unavailable for card %s: %s", t, exc)
    try:
        p = get_price(t)
        if isinstance(p, dict) and p.get("close"):
            return p.get("close")
    except Exception as exc:
        log.warning("price unavailable for card %s: %s", t, exc)
    return None


@app.get("/og/slice.png", response_class=Response)
def og_slice(request: Request):
    """Personal dilution damage card for one holder's share count. Must be
    registered before the /og/{ticker}.png catch-all."""
    from . import ogimg
    t = _sanitize_ticker(request.query_params.get("t") or "")
    raw_sh = re.sub(r"[^0-9]", "", request.query_params.get("sh") or "")
    sh = int(raw_sh) if raw_sh else 0
    if not t or sh <= 0 or sh > 10 ** 15:
        return RedirectResponse(url="/og/default.png", status_code=302)
    resolved = sec.resolve(t)
    if not resolved:
        return RedirectResponse(url="/og/default.png", status_code=302)
    risk = None
    try:
        risk = _risk_full(t, resolved["cik"])
    except Exception as exc:
        log.warning("slice og risk failed for %s: %s", t, exc)
    dilution = (risk or {}).get("dilution") or {}
    try:
        png = ogimg.slice_png(t, sh, resolved.get("name") or "",
                              dilution.get("last_val"), dilution.get("pct_12m"),
                              _last_close(t))
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600"})
    except Exception as exc:
        log.warning("slice og generation failed: %s", exc)
        return RedirectResponse(url="/og/default.png", status_code=302)


def _avg_score(results: list):
    scores = [r["score"] for r in results
              if not r.get("error") and r.get("score") is not None]
    return round(sum(scores) / len(scores)) if scores else None


def _bag_vs_rows(a: list, b: list):
    """Ranked sides + averages for a Bag vs Bag matchup. Shared by the page
    and its share image so the two surfaces can never disagree."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        combined = list(pool.map(_ticker_summary, list(a) + list(b)))

    def rank(rs):
        return sorted(rs, key=lambda r: (r.get("error", False), -(r.get("score", 0))))

    side_a, side_b = rank(combined[:len(a)]), rank(combined[len(a):])
    avg_a, avg_b = _avg_score(side_a), _avg_score(side_b)
    winner = None
    if avg_a is not None and avg_b is not None and avg_a != avg_b:
        winner = "a" if avg_a > avg_b else "b"
    elif avg_a is not None and avg_b is not None:
        winner = "tie"
    return side_a, side_b, avg_a, avg_b, winner


@app.get("/og/bagvs.png", response_class=Response)
def og_bagvs(request: Request):
    """Bag vs Bag matchup card. Registered before the /og/{ticker}.png
    catch-all."""
    from . import ogimg
    a = _ticker_list_param(request.query_params.get("a") or "")
    b = _ticker_list_param(request.query_params.get("b") or "")
    if not a or not b:
        return RedirectResponse(url="/og/default.png", status_code=302)
    try:
        side_a, side_b, avg_a, avg_b, winner = _bag_vs_rows(a, b)
        png = ogimg.bagvs_png(side_a, avg_a, side_b, avg_b, winner or "")
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600"})
    except Exception as exc:
        log.warning("bagvs og generation failed: %s", exc)
        return RedirectResponse(url="/og/default.png", status_code=302)


@app.get("/og/{ticker}.png", response_class=Response)
def og_image(ticker: str):
    from . import ogimg
    from fastapi.responses import RedirectResponse as _Redir

    if ticker.lower() == "default":
        return Response(content=ogimg.default_png(), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    t = _sanitize_ticker(ticker)
    if not t or t != ticker.upper():
        return _Redir(url="/og/default.png", status_code=302)
    resolved = sec.resolve(t)
    if not resolved:
        return _Redir(url="/og/default.png", status_code=302)
    try:
        cik = resolved["cik"]
        risk = None
        try:
            risk = _risk_full(t, cik)
        except Exception as exc:
            log.warning("og risk fallback for %s: %s", t, exc)
        if risk:
            png = ogimg.ticker_png(t, risk["score"], risk["band"], risk["cls"], resolved.get("name") or "")
        else:
            png = ogimg.ticker_png(t, None, None, "bad", resolved.get("name") or "")
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600"})
    except Exception as exc:
        log.warning("og generation failed for %s: %s", t, exc)
        return _Redir(url="/og/default.png", status_code=302)


@app.get("/og/default.png", response_class=Response)
def og_default():
    from . import ogimg
    return Response(content=ogimg.default_png(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# ---- watchlist: all your bags, ranked -----------------------------------------

def _ticker_summary(ticker: str) -> dict:
    t = _sanitize_ticker(ticker)
    resolved = sec.resolve(t)
    if not resolved:
        return {"ticker": ticker.upper()[:10], "error": True}
    cik, name = resolved["cik"], resolved["name"]
    try:
        risk = _risk_full(t, cik)
        price = get_price(t)
        verdict = _cached_verdict(t, cik, price, _going_concern_flag(risk)).get("verdict")
        dilution = risk.get("dilution") or {}
        runway_months = risk.get("runway_months")
        driver = ", ".join(risk["drivers"][:2]) if risk["drivers"] else None
        return {
            "ticker": t, "name": name, "error": False,
            "score": risk["score"], "band": risk["band"], "cls": risk["cls"],
            "verdict": verdict, "driver": driver,
            "dilution_12m": dilution.get("pct_12m"),
            "runway_months": runway_months,
            "story": risk["story"],
        }
    except Exception as exc:
        log.warning("watchlist summary failed %s: %s", t, exc)
        return {"ticker": t, "name": name, "error": True}


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt(request: Request):
    base = _origin(request)
    return f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n"


@app.get("/sitemap.xml", response_class=Response)
def sitemap_xml(request: Request):
    base = _origin(request)
    urls = ["", "watchlist", "privacy", "terms"]
    hot = [r["ticker"] for r in db.recent_searched(limit=50, days=90)]
    urls.extend(f"{t}" for t in hot)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{base}/{u}</loc></url>" for u in urls)
        + "</urlset>"
    )
    return Response(content=body, media_type="application/xml")


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_get(request: Request):
    raw = (request.query_params.get("t") or "").strip()
    return _watchlist_tpl(request, raw)


@app.post("/watchlist")
def watchlist_post(request: Request, tickers: str = Form("")):
    return _watchlist_tpl(request, tickers or "")


def _watchlist_tpl(request: Request, raw: str):
    from concurrent.futures import ThreadPoolExecutor

    parts = [p for p in re.split(r"[,\s;]+", (raw or "").upper()) if p]
    seen, wanted = set(), []
    for p in parts:
        st = _sanitize_ticker(p)
        if st and st not in seen and len(wanted) < 8:
            seen.add(st)
            wanted.append(st)
    results = []
    if wanted:
        db.log_event("watchlist", ticker=",".join(wanted), utm=_utm(request))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_ticker_summary, wanted))
        results.sort(key=lambda r: (r.get("error", False), -(r.get("score", 0))))
    worst = next((r for r in results if not r.get("error")), None)
    context = {
        "request": request, "app_name": APP_NAME, "app_version": APP_VERSION,
        "tickers_raw": ",".join(wanted), "results": results, "queried": bool(wanted),
        "page_url": _base_url(request),
        "tg_watch_url": tg.watch_link(wanted),
        "og_title": (
            ("Bag check: " + ",".join(wanted)) if wanted else "The Bag Check"
        ),
        "og_desc": (
            f"Ranked by risk index, worst first. {worst['ticker']} leads at {worst.get('score')}/100 ({worst.get('band')})."
            if worst else "Paste every microcap you hold, get each one's risk index, red flags, dilution and cash countdown."
        ),
        "og_image_url": (
            _origin(request)
            + (f"/og/bag.png?t={','.join(wanted)}" if wanted else "/og/default.png")
        ),
    }
    return templates.TemplateResponse(request, "watchlist.html", context)


# ---- bag vs bag: settle whose lineup is worse ----------------------------------

@app.get("/bag-vs", response_class=HTMLResponse)
def bag_vs_get(request: Request):
    return _bag_vs_tpl(
        request,
        request.query_params.get("a") or "",
        request.query_params.get("b") or "",
    )


@app.post("/bag-vs")
def bag_vs_post(request: Request, mine: str = Form(""), theirs: str = Form("")):
    return _bag_vs_tpl(request, mine or "", theirs or "")


def _bag_vs_tpl(request: Request, raw_a: str, raw_b: str):
    a = _ticker_list_param(raw_a)
    b = _ticker_list_param(raw_b)
    queried = bool(a) and bool(b)
    side_a = side_b = []
    avg_a = avg_b = None
    winner = None
    if a:
        db.log_event("watchlist", ticker=",".join(a), utm=_utm(request))
    if b:
        db.log_event("watchlist", ticker=",".join(b), utm=_utm(request))
    if queried:
        db.log_event("bag_vs", ticker="|".join([",".join(a), ",".join(b)]),
                     utm=_utm(request))
        side_a, side_b, avg_a, avg_b, winner = _bag_vs_rows(a, b)
    verdict_line = None
    if winner == "tie":
        verdict_line = "Dead even. Two equally cursed bags."
    elif winner in ("a", "b"):
        hi, lo = (avg_a, avg_b) if winner == "a" else (avg_b, avg_a)
        verdict_line = f"BAG {winner.upper()} IS WORSE: average risk index {hi} vs {lo}."
    og_title = (
        f"Bag fight: {','.join(a)} vs {','.join(b)}" if queried else "Whose bag is worse?"
    )
    og_desc = (
        (f"{','.join(a)} averages {avg_a}/100 against {','.join(b)} at {avg_b}/100."
         if avg_a is not None and avg_b is not None
         else "Two portfolios, head to head, ranked by the same risk index as every report.")
        if queried else
        "Paste both lineups and settle it: whose microcap bag is in more trouble?"
    )
    context = {
        "request": request, "app_name": APP_NAME, "app_version": APP_VERSION,
        "queried": queried, "raw_a": ",".join(a), "raw_b": ",".join(b),
        "side_a": side_a, "side_b": side_b, "avg_a": avg_a, "avg_b": avg_b,
        "winner": winner, "verdict_line": verdict_line,
        "page_url": _base_url(request),
        "og_title": og_title, "og_desc": og_desc,
        "og_image_url": (
            _origin(request) + f"/og/bagvs.png?a={','.join(a)}&b={','.join(b)}"
            if queried else _origin(request) + "/og/default.png"
        ),
    }
    return templates.TemplateResponse(request, "bag_vs.html", context)


# ---- legacy engine routes (unified into the report page) -----------------------


def _cached_delta(t: str, cik: int, deep: bool = True) -> dict:
    # deep includes the breaking-8-K text fetch; shallow skips it so a cold
    # ticker page renders fast. Separate cache keys, same TTL.
    key = f"delta:{t}" if deep else f"deltacore:{t}"
    hit = db.cache_get(key, ENGINE_CACHE_TTL)
    if hit is not None:
        return json.loads(hit)
    out = delta_engine.compute(t, cik, deep=deep)
    db.cache_set(key, json.dumps(out, default=str), ENGINE_CACHE_TTL)
    return out


def _going_concern_flag(risk: dict) -> bool:
    """True when the verified scan found real going-concern warnings."""
    if not risk:
        return False
    for f in risk.get("flags", []):
        if f.get("key") == "going_concern" and (f.get("effective") or 0) > 0:
            return True
    return False


def _risk_full(t: str, cik: int):
    """The complete risk index for a ticker, exactly as the report page
    computes it: flags + share history + cash runway + official 8-K items.
    Shared by the report page, watchlist rows and OG stamp cards so every
    surface quotes the same number (a share card saying 87 while the page
    says 95 would burn trust)."""
    flags = sec.scan_flags(cik)
    share_hist = sec.share_history(cik)
    runway_months = cash_delta_pct = None
    try:
        delta = _cached_delta(t, cik)
        for c in delta.get("changes", []):
            if c.get("key") == "burn" and c.get("runway_months") is not None:
                runway_months = c["runway_months"]
            elif c.get("label") == "Cash and equivalents" and c.get("delta_pct") is not None:
                cash_delta_pct = c["delta_pct"]
    except Exception as exc:
        log.warning("risk_full delta unavailable for %s: %s", t, exc)
    try:
        events_scored = sec.critical_8k_history(cik).get("scored") or {}
    except Exception as exc:
        log.warning("critical 8-K scan failed for %s: %s", t, exc)
        events_scored = {}
    return compute_risk(flags["results"], share_hist, runway_months,
                        cash_delta_pct, events_scored)


def _cached_verdict(t: str, cik: int, price: dict, going_concern: bool = False) -> dict:
    key = f"verdict:{t}"
    hit = db.cache_get(key, 86400)
    if hit is not None:
        return json.loads(hit)
    delta = _cached_delta(t, cik)
    inputs = build_inputs(delta, price)
    inputs["going_concern"] = going_concern
    out = verdict_engine.compute(inputs)
    db.cache_set(key, json.dumps(out, default=str), 86400)
    return out


@app.get("/{ticker}/delta", response_class=HTMLResponse)
def delta_page(request: Request, ticker: str):
    """Legacy route: everything lives on the unified report now."""
    t = _sanitize_ticker(ticker)
    return RedirectResponse(url=f"/{t}#what-changed" if t else "/", status_code=302)


@app.get("/{ticker}/verdict", response_class=HTMLResponse)
def verdict_page(request: Request, ticker: str):
    """Legacy route: everything lives on the unified report now."""
    t = _sanitize_ticker(ticker)
    return RedirectResponse(url=f"/{t}#verdict" if t else "/", status_code=302)


# catch-all snapshot route must be registered last so specific paths win
@app.get("/{ticker}", response_class=HTMLResponse)
def snapshot_route(request: Request, ticker: str):
    # Utility pages are case-sensitive in Starlette, while visitors commonly
    # type labels such as PRIVACY in all caps. Do not interpret them as tickers.
    canonical = {
        "privacy": "/privacy",
        "terms": "/terms",
        "watchlist": "/watchlist",
        "bag-vs": "/bag-vs",
        "robots.txt": "/robots.txt",
        "sitemap.xml": "/sitemap.xml",
        "healthz": "/healthz",
    }.get((ticker or "").lower())
    if canonical:
        return RedirectResponse(url=canonical, status_code=308)
    return _snapshot_tpl(request, ticker)

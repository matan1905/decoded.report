import json
import logging

import httpx

from . import db
from .config import FINNHUB_API_KEY, TWELVEDATA_API_KEY

log = logging.getLogger(__name__)

TWELVE_QUOTE = "https://api.twelvedata.com/quote"
TWELVE_TS = "https://api.twelvedata.com/time_series"
FINNHUB_QUOTE = "https://finnhub.io/api/v1/quote"
FINNHUB_PROFILE = "https://finnhub.io/api/v1/stock/profile2"


def _state() -> str:
    if TWELVEDATA_API_KEY:
        return "twelvedata"
    if FINNHUB_API_KEY:
        return "finnhub"
    return "degraded"


def twelvedata_quote(client: httpx.Client, ticker: str) -> dict:
    r = client.get(TWELVEDATA_QUOTE, params={"symbol": ticker, "apikey": TWELVEDATA_API_KEY})
    r.raise_for_status()
    d = r.json()
    if d.get("status") == "error":
        raise RuntimeError(d.get("message", "twelve error"))
    close = _num(d.get("close"))
    high52, low52 = _twelve_52w(client, ticker, r)
    return {
        "source": "twelvedata",
        "close": close,
        "open": _num(d.get("open")),
        "high": _num(d.get("high")),
        "low": _num(d.get("low")),
        "52w_high": high52 or _num(None),
        "52w_low": low52 or _num(None),
        "name": d.get("name"),
        "exchange": d.get("exchange"),
        "currency": d.get("currency"),
    }


def _twelve_52w(client, ticker, quote_resp) -> tuple:
    r = client.get(
        TWELVE_TS,
        params={"symbol": ticker, "interval": "1day", "outputsize": "260", "apikey": TWELVEDATA_API_KEY},
    )
    r.raise_for_status()
    d = r.json()
    if d.get("status") == "error":
        return _num(None), _num(None)
    values = d.get("values", [])
    highs = [_num(v.get("high")) for v in values]
    lows = [_num(v.get("low")) for v in values]
    highs = [h for h in highs if h]
    lows = [l for l in lows if l]
    return (max(highs) if highs else None, min(lows) if lows else None)


def _num(v):
    try:
        return round(float(v), 2) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def finnhub_quote(client, ticker: str) -> dict:
    r = client.get(FINNHUB_QUOTE, params={"symbol": ticker, "token": FINNHUB_API_KEY})
    r.raise_for_status()
    d = r.json()
    close = _num(d.get("c"))
    if close is None:
        raise RuntimeError("finnhub no data")
    p = client.get(FINNHUB_PROFILE, params={"symbol": ticker, "token": FINNHUB_API_KEY})
    prof = p.json() if p.status_code == 200 else {}
    return {
        "source": "finnhub",
        "close": close,
        "open": _num(d.get("o")),
        "high": _num(d.get("h")),
        "low": _num(d.get("l")),
        "52w_high": _num(d.get("52w high") or p.get("52wHigh")),
        "52w_low": _num(d.get("52w low") or p.get("52wLow")),
        "name": prof.get("name"),
        "exchange": prof.get("exchange"),
        "currency": "USD",
    }


def get_price(ticker: str) -> dict:
    cache = _price_cache(ticker)
    if cache is not None:
        cache["cached"] = True
        return cache
    state = _state()
    if state == "degraded":
        log.warning(
            "No price API key configured (TWELVEDATA_API_KEY / FINNHUB_API_KEY). Degraded price mode, skipping quote fetch for %s.",
            ticker,
        )
        return {"source": "none", "degraded": True, "cached": False}
    client = httpx.Client(timeout=20.0)
    try:
        if state == "twelvedata":
            out = twelvedata_quote(client, ticker)
        else:
            out = finnhub_quote(client, ticker)
    except Exception as exc:
        # try fallback if primary failed
        out = None
        try:
            if state == "twelvedata" and FINNHUB_API_KEY:
                out = finnhub_quote(client, ticker)
                state = "finnhub"
            elif state == "finnhub" and TWELVEDATA_API_KEY:
                out = twelvedata_quote(client, ticker)
                state = "twelvedata"
        except Exception as exc2:
            log.warning("Price fetch failed for %s: %s / %s", ticker, exc, exc2)
    finally:
        client.close()
    if out is None:
        return {"source": "none", "degraded": True, "cached": False}
    out["degraded"] = False
    out["cached"] = False
    _set_price_cache(ticker, out)
    return out


CACHE_TTL = 86400


def _price_cache(ticker: str):
    hit = db.cache_get(f"price:{ticker}", CACHE_TTL)
    if hit is not None:
        return json.loads(hit)
    return None


def _set_price_cache(ticker: str, data: dict):
    db.cache_set(f"price:{ticker}", json.dumps(data), CACHE_TTL)
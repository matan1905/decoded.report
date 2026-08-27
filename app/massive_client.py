"""Massive.com market-data client (free tier: 5 calls/minute).

Design constraints:
- The free key allows ~5 requests per minute. A global sliding-window rate
  limiter gates every call; callers block until a slot frees.
- Everything is cached aggressively (price 24h, splits/short-interest/float
  7 days) so a warm ticker costs zero calls.
- Every function degrades to None: the page renders fully keyless and the
  websocket fills these slots opportunistically."""

import json
import logging
import threading
import time
from collections import deque

import httpx

from . import db
from .config import SEC_USER_AGENT

log = logging.getLogger(__name__)

BASE = "https://api.massive.com"

# free tier: 5 calls / minute
CALLS_PER_MIN = 5


class RateLimiter:
    """Sliding-window limiter shared by every Massive call in the process."""

    def __init__(self, calls_per_min: int = CALLS_PER_MIN):
        self.calls_per_min = calls_per_min
        self.window = 60.0
        self._stamps = deque()
        self._lock = threading.Lock()

    def wait_for_slot(self) -> float:
        """Block until a call slot frees. Returns the wait time."""
        while True:
            with self._lock:
                now = time.time()
                while self._stamps and now - self._stamps[0] > self.window:
                    self._stamps.popleft()
                if len(self._stamps) < self.calls_per_min:
                    self._stamps.append(now)
                    return 0.0
                wait = self.window - (now - self._stamps[0]) + 0.05
            time.sleep(min(wait, 5.0))

    def seconds_until_slot(self) -> float:
        with self._lock:
            now = time.time()
            while self._stamps and now - self._stamps[0] > self.window:
                self._stamps.popleft()
            if len(self._stamps) < self.calls_per_min:
                return 0.0
            return max(0.0, self.window - (now - self._stamps[0]))


limiter = RateLimiter()


def _api_key():
    from .config import MASSIVE_API_KEY
    return MASSIVE_API_KEY or None


def available() -> bool:
    return bool(_api_key())


def _massive_get(path: str, params: dict = None):
    key = _api_key()
    if not key:
        return None
    waited = limiter.wait_for_slot()
    if waited:
        log.info("massive rate limiter waited %.1fs for %s", waited, path)
    try:
        with httpx.Client(
            headers={"Authorization": f"Bearer {key}", "User-Agent": SEC_USER_AGENT},
            timeout=20.0,
        ) as c:
            r = c.get(BASE + path, params=params or {})
        if r.status_code == 429:
            log.warning("massive 429 for %s; backing off", path)
            return None
        if r.status_code in (401, 403):
            log.warning("massive auth failed for %s (%s)", path, r.status_code)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("massive call failed %s: %s", path, exc)
        return None


def _cached(key: str, ttl: int, fetch):
    hit = db.cache_get(key, ttl)
    if hit is not None:
        return json.loads(hit)
    data = fetch()
    if data is None:
        return None
    db.cache_set(key, json.dumps(data), ttl)
    return data


# ---- public getters ---------------------------------------------------------

def prev_close(ticker: str):
    def fetch():
        data = _massive_get(f"/v2/aggs/ticker/{ticker}/prev", {"adjusted": "true"})
        results = (data or {}).get("results") or []
        if not results:
            return None
        r = results[0]
        return {
            "close": r.get("c"),
            "high": r.get("h"),
            "low": r.get("l"),
            "volume": r.get("v"),
            "date": time.strftime("%Y-%m-%d", time.gmtime(r["t"] / 1000)) if r.get("t") else None,
            "source": "massive",
        }
    return _cached(f"massive:prev:{ticker}", 86400, fetch)


def splits(ticker: str):
    def fetch():
        data = _massive_get("/stocks/v1/splits", {"ticker": ticker, "limit": 20})
        results = (data or {}).get("results") or []
        out = []
        for r in results:
            try:
                ratio = float(r.get("split_to", 0)) / float(r.get("split_from", 1) or 1)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            out.append({
                "date": r.get("execution_date"),
                "split_from": r.get("split_from"),
                "split_to": r.get("split_to"),
                "reverse": ratio < 1,
            })
        out.sort(key=lambda s: s["date"] or "", reverse=True)
        return out
    return _cached(f"massive:splits:{ticker}", 7 * 86400, fetch)


def short_interest(ticker: str):
    def fetch():
        data = _massive_get("/stocks/v1/short-interest", {"ticker": ticker, "limit": 1})
        results = (data or {}).get("results") or []
        if not results:
            return None
        r = results[0]
        return {
            "shares": r.get("short_interest"),
            "avg_daily_volume": r.get("avg_daily_volume"),
            "days_to_cover": r.get("days_to_cover"),
            "settlement_date": r.get("settlement_date") or r.get("settlementdate"),
        }
    return _cached(f"massive:short:{ticker}", 7 * 86400, fetch)


def free_float(ticker: str):
    def fetch():
        data = _massive_get("/stocks/vX/float", {"ticker": ticker, "limit": 1})
        results = (data or {}).get("results") or []
        if not results:
            return None
        r = results[0]
        return {"float": r.get("float") or r.get("free_float")}
    return _cached(f"massive:float:{ticker}", 7 * 86400, fetch)


_KIND_TTL = {"price": 86400, "splits": 7 * 86400, "float": 7 * 86400, "short": 7 * 86400}


def is_cached(kind: str, ticker: str) -> bool:
    """True when the slot can be filled without spending a rate-limited call."""
    return db.cache_get(f"massive:{kind}:{ticker}", _KIND_TTL[kind]) is not None

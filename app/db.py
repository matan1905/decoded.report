import re
import sqlite3
import threading
import time

from .config import DB_PATH

# How long a connection waits for a competing writer before raising
# "database is locked". The web app, and any CLI pass, share one SQLite
# file, so a bounded wait lets short writes queue instead of failing.
BUSY_TIMEOUT_MS = 30000
_WAL_READY = threading.Event()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL,
  ticker TEXT,
  source_url TEXT,
  utm_source TEXT,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  utm_source TEXT,
  found INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  ticker TEXT,
  utm_source TEXT,
  captured INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
"""


def get_conn():
    """One short-lived connection per call, WAL-enabled.

    WAL lets readers run while a writer holds the write lock, which is what
    turned concurrent writes into spurious "database is locked" 500s under
    the old rollback-journal mode. busy_timeout then makes any remaining
    writer-vs-writer overlap queue instead of failing immediately.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not _WAL_READY.is_set():
        # journal_mode persists in the database file; setting it once per
        # process is enough and avoids a write on every connection.
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            _WAL_READY.set()
        except sqlite3.OperationalError:
            pass
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


# ---- cache helpers (generic 24h store) ----------------------------

def cache_get(key: str, ttl: int = 86400):
    conn = get_conn()
    row = conn.execute(
        "SELECT payload, expires_at FROM cache WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    if row["expires_at"] < time.time():
        return None
    return row["payload"]


def cache_put(key: str, payload: str, ttl: int = 86400):
    now = time.time()
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO cache (key, payload, created_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (key, payload, now, now + ttl),
    )
    conn.commit()
    conn.close()


cache_set = cache_put


def cache_clear_expired():
    conn = get_conn()
    conn.execute("DELETE FROM cache WHERE expires_at < ?", (time.time(),))
    conn.commit()
    conn.close()


# ---- events / logging ----------------------------------------------

def log_event(kind: str, ticker=None, utm=None, captured=0):
    conn = get_conn()
    conn.execute(
        "INSERT INTO events (kind, ticker, utm_source, captured, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, ticker, utm, int(captured), time.time()),
    )
    conn.commit()
    conn.close()


def log_search(ticker: str, found: int, utm=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO searches (ticker, utm_source, found, created_at) "
        "VALUES (?, ?, ?, ?)",
        (ticker, utm, int(found), time.time()),
    )
    conn.commit()
    conn.close()


def recent_events(limit: int = 40, days: int = 30) -> list:
    since = time.time() - days * 86400
    conn = get_conn()
    rows = conn.execute(
        "SELECT kind, ticker, utm_source, captured, created_at FROM events "
        "WHERE created_at > ? ORDER BY id DESC LIMIT ?",
        (since, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- demand signals ------------------------------------------------------------

def recent_searched(limit: int = 8, days: int = 14) -> list:
    """Tickers people actually looked up recently, most-demanded first.
    Draws from both the search box and direct ticker page views."""
    since = time.time() - days * 86400
    conn = get_conn()
    rows = conn.execute(
        "SELECT ticker, COUNT(*) AS c FROM ("
        "  SELECT ticker, created_at FROM searches WHERE found >= 0 AND created_at > ?"
        "  UNION ALL"
        "  SELECT ticker, created_at FROM events WHERE kind = 'page_view' AND created_at > ?"
        ") WHERE ticker IS NOT NULL AND ticker != ''"
        " GROUP BY ticker ORDER BY c DESC, MAX(created_at) DESC LIMIT ?",
        (since, since, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def demand_stats(days: int = 7) -> dict:
    since = time.time() - days * 86400
    conn = get_conn()
    out = {}
    for kind in ("page_view", "lead", "watchlist"):
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE kind = ? AND created_at > ?",
            (kind, since),
        ).fetchone()
        out[kind] = row["c"]
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM searches WHERE created_at > ?", (since,)
    ).fetchone()
    out["searches"] = row["c"]
    row = conn.execute("SELECT COUNT(*) AS c FROM leads").fetchone()
    out["leads_total"] = row["c"]
    conn.close()
    return out


# ---- related tickers (co-occurrence from real demand events) -----------------

def related_tickers(ticker: str, days: int = 90, limit: int = 4) -> list:
    """Tickers people checked together with this one.

    Two honest signals only:
    - watchlist lineups containing this ticker (strong: same bag)
    - page views / searches within a 5-minute window of this ticker's view
    Returns [{ticker, weight}] sorted by weight desc; empty when no signal."""
    me = (ticker or "").upper()
    if not me:
        return []
    since = time.time() - days * 86400
    conn = get_conn()
    rows = conn.execute(
        "SELECT kind, ticker, created_at FROM events "
        "WHERE created_at > ? AND ticker IS NOT NULL AND ticker != ''",
        (since,),
    ).fetchall()
    conn.close()

    counts = {}
    visits = []
    for r in rows:
        kind, tk, ts = r["kind"], (r["ticker"] or "").upper(), r["created_at"]
        if kind == "watchlist":
            members = [p for p in re.split(r"[,\s;]+", tk) if p]
            if me in members:
                for p in members:
                    if p != me and len(p) <= 10:
                        counts[p] = counts.get(p, 0.0) + 1.0
        elif kind in ("page_view", "search") and re.match(r"^[A-Z0-9.\-]{1,10}$", tk):
            visits.append((ts, tk))

    # proximity pairing: any other ticker touched within +/- 5 minutes of a
    # view of this ticker reads as the same person checking both
    import bisect

    visits.sort(key=lambda v: v[0])
    stamps = [v[0] for v in visits]
    window = 300.0
    for i, (ts, tk) in enumerate(visits):
        if tk != me:
            continue
        lo = bisect.bisect_left(stamps, ts - window)
        hi = bisect.bisect_right(stamps, ts + window)
        for j in range(lo, hi):
            other = visits[j][1]
            if other != me and other != tk:
                counts[other] = counts.get(other, 0.0) + 0.2

    ranked = [
        {"ticker": t, "weight": round(w, 2)}
        for t, w in sorted(counts.items(), key=lambda kv: -kv[1])
        if w >= 0.2
    ]
    return ranked[:limit]

import json
import re
import sqlite3
import time

from .config import DB_PATH

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
CREATE TABLE IF NOT EXISTS alert_state (
  ticker TEXT PRIMARY KEY,
  sig TEXT,
  shares_val TEXT,
  checked_at REAL,
  alerted_at REAL,
  note TEXT,
  flags TEXT,
  material_accn TEXT
);
CREATE TABLE IF NOT EXISTS tg_subs (
  chat_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  source TEXT,
  utm_source TEXT,
  created_at REAL NOT NULL,
  PRIMARY KEY (chat_id, ticker)
);
CREATE TABLE IF NOT EXISTS notify_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  ticker TEXT,
  kind TEXT,
  sent INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
"""


def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    # light migrations for databases created before a column existed
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(alert_state)").fetchall()}
    for new_col, ddl in (("flags", "TEXT"), ("material_accn", "TEXT")):
        if new_col not in cols:
            conn.execute(f"ALTER TABLE alert_state ADD COLUMN {new_col} {ddl}")
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


# ---- telegram subscriptions ---------------------------------------------

def sub_add(chat_id: str, tickers: list, source=None, utm=None) -> list:
    """Register one chat for one or more tickers. Returns the tickers that
    were newly added (already-watched ones are not re-added)."""
    now = time.time()
    added = []
    conn = get_conn()
    for t in tickers:
        cur = conn.execute(
            "INSERT OR IGNORE INTO tg_subs (chat_id, ticker, source, utm_source, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(chat_id), t, source, utm, now),
        )
        if cur.rowcount:
            added.append(t)
    conn.commit()
    conn.close()
    return added


def sub_remove(chat_id: str, tickers: list = None) -> int:
    """Unwatch: the given tickers, or everything for that chat when None."""
    conn = get_conn()
    if tickers:
        cur = conn.execute(
            "DELETE FROM tg_subs WHERE chat_id = ? AND ticker IN "
            "(%s)" % ",".join("?" * len(tickers)),
            [str(chat_id)] + list(tickers),
        )
    else:
        cur = conn.execute("DELETE FROM tg_subs WHERE chat_id = ?", (str(chat_id),))
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def subs_for_chat(chat_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT ticker FROM tg_subs WHERE chat_id = ? ORDER BY created_at",
        (str(chat_id),),
    ).fetchall()
    conn.close()
    return [r["ticker"] for r in rows]


def chats_for_ticker(ticker: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT chat_id FROM tg_subs WHERE ticker = ?", (ticker,)
    ).fetchall()
    conn.close()
    return [r["chat_id"] for r in rows]


def watched_tickers() -> list:
    """Distinct saved tickers with how many chats watch each, busiest first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT ticker, COUNT(DISTINCT chat_id) AS watchers FROM tg_subs "
        "GROUP BY ticker ORDER BY watchers DESC, MAX(created_at) DESC",
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sub_count() -> int:
    conn = get_conn()
    row = conn.execute("SELECT COUNT(DISTINCT chat_id) AS c FROM tg_subs").fetchone()
    conn.close()
    return row["c"]


def sub_rows_total() -> int:
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM tg_subs").fetchone()
    conn.close()
    return row["c"]


def recent_subs(limit: int = 20) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT chat_id, ticker, utm_source, created_at FROM tg_subs "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_notify(chat_id: str, ticker: str, kind: str, sent: int):
    conn = get_conn()
    conn.execute(
        "INSERT INTO notify_log (chat_id, ticker, kind, sent, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(chat_id), ticker, kind, int(sent), time.time()),
    )
    conn.commit()
    conn.close()


def recent_notify_logs(limit: int = 20) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT chat_id, ticker, kind, sent, created_at FROM notify_log "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def notify_sent_7d() -> int:
    since = time.time() - 7 * 86400
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM notify_log WHERE created_at > ? AND sent = 1",
        (since,),
    ).fetchone()
    conn.close()
    return row["c"]


def alert_state_get(ticker: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM alert_state WHERE ticker = ?", (ticker,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def alert_states() -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM alert_state ORDER BY checked_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def alert_state_put(ticker: str, sig: str, shares_val=None, alerted_at=None,
                    note=None, flags=None, material_accn=None):
    now = time.time()
    conn = get_conn()
    prior = conn.execute(
        "SELECT alerted_at FROM alert_state WHERE ticker = ?", (ticker,)
    ).fetchone()
    keep_alerted = alerted_at if alerted_at is not None else (
        prior["alerted_at"] if prior else None
    )
    conn.execute(
        "INSERT OR REPLACE INTO alert_state "
        "(ticker, sig, shares_val, checked_at, alerted_at, note, flags, material_accn) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticker, sig, shares_val, now, keep_alerted, note,
         json.dumps(flags) if flags is not None else None, material_accn),
    )
    conn.commit()
    conn.close()


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

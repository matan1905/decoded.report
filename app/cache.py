"""Process-local TTL cache. Replaces the SQLite cache entirely.

Everything here lives in memory and disappears on restart. That is the
tradeoff: no database file, no locks, no disk, and a cold cache after a
deploy. The callers only ever needed "remember this payload for N seconds",
so the same get/set surface is kept.

The store is bounded two ways so a long-running process cannot eat the box:
an entry cap and an approximate total-bytes cap. Expired entries go first,
then the ones closest to expiry.
"""

import threading
import time

_LOCK = threading.Lock()
_STORE = {}          # key -> (expires_at, payload)
_BYTES = 0           # approximate sum of len(payload)

MAX_ENTRIES = 5000
MAX_BYTES = 96 * 1024 * 1024  # keep the cache well under a small VPS's RAM


def _size(payload) -> int:
    try:
        return len(payload)
    except TypeError:
        return len(str(payload))


def cache_get(key: str, ttl: int = 86400):
    """Return the cached payload, or None when missing or expired."""
    global _BYTES
    now = time.time()
    with _LOCK:
        row = _STORE.get(key)
        if row is None:
            return None
        expires_at, payload = row
        if expires_at < now:
            _STORE.pop(key, None)
            _BYTES -= _size(payload)
            return None
        return payload


def cache_put(key: str, payload, ttl: int = 86400):
    global _BYTES
    now = time.time()
    with _LOCK:
        old = _STORE.get(key)
        if old is not None:
            _BYTES -= _size(old[1])
        _STORE[key] = (now + ttl, payload)
        _BYTES += _size(payload)
        _trim_locked(now)


cache_set = cache_put


def cache_clear_expired():
    now = time.time()
    with _LOCK:
        _drop_expired_locked(now)
        _trim_locked(now)


def _drop_expired_locked(now):
    global _BYTES
    for k, (expires_at, payload) in list(_STORE.items()):
        if expires_at < now:
            _STORE.pop(k, None)
            _BYTES -= _size(payload)


def _trim_locked(now):
    """Enforce the entry and byte caps. Cheap in the normal case; the sort
    only runs when a cap is actually crossed."""
    global _BYTES
    if len(_STORE) <= MAX_ENTRIES and _BYTES <= MAX_BYTES:
        return
    _drop_expired_locked(now)
    if len(_STORE) <= MAX_ENTRIES and _BYTES <= MAX_BYTES:
        return
    # evict soonest-to-expire first, which is roughly oldest-written
    for k, (expires_at, payload) in sorted(_STORE.items(), key=lambda kv: kv[1][0]):
        if len(_STORE) <= MAX_ENTRIES and _BYTES <= MAX_BYTES:
            break
        _STORE.pop(k, None)
        _BYTES -= _size(payload)


def stats() -> dict:
    """Small diagnostic surface for the owner console."""
    now = time.time()
    with _LOCK:
        live = sum(1 for expires_at, _ in _STORE.values() if expires_at >= now)
        return {"entries": len(_STORE), "live": live, "bytes": _BYTES}

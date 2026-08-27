"""QA: verify the full /ws/market stream sequence for one or more tickers.

Asserts message order per ticker:
  status -> price -> splits -> float -> short -> domain -> insider
  -> breaking -> gleif -> holders -> wayback -> certs -> whoishist
  -> dns -> ticker_done, then all_done.
Tolerances are honest: a probe may legitimately report degraded data, but
every kind must arrive exactly once per ticker, in order.
"""
import asyncio
import json
import sys

import websockets

BASE = "ws://127.0.0.1:8000/ws/market"

ORDER = ["status", "price", "splits", "float", "short", "domain",
         "insider", "breaking", "gleif", "holders", "wayback", "certs",
         "whoishist", "dns", "ticker_done"]

ALLOWED = set(ORDER) | {"wait", "all_done"}


async def run(tickers: list):
    ok = True
    async with websockets.connect(BASE + "?t=" + ",".join(tickers)) as ws:
        seen = []
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=180)
            except asyncio.TimeoutError:
                print("TIMEOUT waiting for messages; got:", [m[0] for m in seen])
                return False
            msg = json.loads(raw)
            kind = msg.get("kind")
            if kind not in ALLOWED:
                print("UNEXPECTED KIND:", kind)
                ok = False
            seen.append((kind, msg.get("ticker")))
            if kind == "all_done":
                break

    # split into per-ticker sequences by ticker_done markers
    per = {}
    cur = []
    cur_t = None
    for kind, t in seen:
        if kind == "status":
            cur_t = t
        cur.append(kind)
        if kind == "ticker_done":
            per[cur_t] = cur
            cur = []

    for t, seq in per.items():
        core = [k for k in seq if k not in ("wait",)]
        expected_core = ORDER[:-1] + ["ticker_done"]
        missing = [k for k in expected_core if k not in core]
        dupes = sorted({k for k in core if core.count(k) > 1})
        # order check on the canonical kinds, ignoring interleaved waits
        idx = [core.index(k) for k in ORDER if k in core]
        ordered = idx == sorted(idx)
        status = "OK" if (not missing and not dupes and ordered) else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"[{t}] {status} msgs={len(seq)} missing={missing} dupes={dupes} ordered={ordered}")
        print(f"      sequence: {core}")
    print("tail all_done:", seen[-1][0] == "all_done")
    return ok


if __name__ == "__main__":
    tickers = sys.argv[1:] or ["TICKER"]
    sys.exit(0 if asyncio.run(run(tickers)) else 1)

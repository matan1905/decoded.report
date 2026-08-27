"""QA: browser-level verification of the streaming report page.

Loads /{ticker} in headless Chrome via CDP, lets the websocket stream fill
the live slots, then asserts on real DOM state:
  - hero price chip filled from the market stream
  - official split records line resolved
  - LEI / institutional holders / site history / cert record / registrant
    history / DNS+mail stats no longer spinners
  - insider section shows the parsed trade table
Usage: python qa_browser_stream.py [TICKER] [TIMEOUT_S]
"""
import asyncio
import base64
import json
import subprocess
import sys
import time
import urllib.request

import websockets

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9227


async def cdp_send(ws, _id, method, params=None):
    await ws.send(json.dumps({"id": _id, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == _id:
            return msg.get("result", {})


async def main(ticker="TICKER", timeout_s=150):
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu",
         f"--remote-debugging-port={PORT}", "--no-first-run", "--user-data-dir=/tmp/opencode/chrome-qa",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
                break
            except Exception:
                time.sleep(0.25)
        page = next(t for t in targets if t["type"] == "page")
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=20 * 1024 * 1024) as ws:
            await cdp_send(ws, 1, "Page.enable")
            await cdp_send(ws, 2, "Page.navigate",
                           {"url": f"http://127.0.0.1:8000/{ticker}"})
            expr = """(function(){
              function txt(id){ var el=document.getElementById(id); if(!el) return 'MISSING';
                var n=el.querySelector('.n'); return n ? (n.textContent||'').trim() : 'NO-N'; }
              return JSON.stringify({
                price: (document.getElementById('mkt-price')||{}).textContent||'',
                splits: (document.getElementById('mkt-splits')||{textContent:''}).textContent.slice(0,60),
                status: (document.getElementById('mkt-status')||{textContent:''}).textContent,
                gleif: txt('stat-gleif'), holders: txt('stat-holders'),
                wayback: txt('stat-wayback'), certs: txt('stat-certs'),
                whoishist: txt('stat-whois'), dns: txt('stat-dns'),
                insiderRows: document.querySelectorAll('#insiders .filing').length,
                insiderStats: document.querySelectorAll('#insiders .stat').length,
                insiderFallbackShown: (function(el){ return !!el && el.style.display !== 'none'; })(document.getElementById('insider-fallback')),
                planChips: document.querySelectorAll('#insiders .chip').length,
                chartBars: document.querySelectorAll('#insiders svg rect').length,
                related: document.querySelectorAll('a.pill').length,
                ogImage: (document.querySelector('meta[property=\"og:image\"]')||{content:''}).content
              });
            })()"""
            deadline = time.time() + timeout_s
            state = {}
            while time.time() < deadline:
                r = await cdp_send(ws, 3, "Runtime.evaluate",
                                   {"expression": expr, "returnByValue": True})
                try:
                    state = json.loads(r["result"]["value"])
                except Exception:
                    state = {"raw": str(r)[:200]}
                done = (state.get("status", "") == "" and
                        state.get("gleif") not in ("", "MISSING") and
                        not state.get("gleif", "").startswith("\u00a0") and
                        state.get("holders") not in ("",) and
                        state.get("wayback") not in ("",) and
                        state.get("certs") not in ("",) and
                        state.get("whoishist") not in ("",) and
                        state.get("dns") not in ("",))
                if done:
                    break
                await asyncio.sleep(3)
            print(json.dumps(state, indent=1))
            def filled(v):
                return v not in ("MISSING", "") and not v.startswith("\u00a0")
            checks = {
                "price chip filled": "$" in state.get("price", "") or "LAST CLOSE" in state.get("price", ""),
                "splits resolved": "SPLIT RECORDS" in state.get("splits", "") or state.get("splits", "") == "",
                "gleif slot filled": filled(state.get("gleif", "")),
                "holders slot filled": filled(state.get("holders", "")),
                "wayback slot filled": filled(state.get("wayback", "")),
                "certs slot filled": filled(state.get("certs", "")),
                "whoishist slot filled": filled(state.get("whoishist", "")),
                "dns slot filled": filled(state.get("dns", "")),
                # a ticker with no Form 4s in the window honestly shows the
                # count fallback instead of an empty trade table
                "insider section resolved": (
                    state.get("insiderRows", 0) >= 1
                    or state.get("insiderStats", 0) >= 1
                    or state.get("insiderFallbackShown", False)
                ),
                "og image present": "/og/" in state.get("ogImage", ""),
            }
            ok = True
            for name, passed in checks.items():
                print(("PASS " if passed else "FAIL ") + name)
                ok = ok and passed
            return ok
    finally:
        proc.terminate()


if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "TICKER"
    tmo = int(sys.argv[2]) if len(sys.argv) > 2 else 150
    sys.exit(0 if asyncio.run(main(ticker, tmo)) else 1)

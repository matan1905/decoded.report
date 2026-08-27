"""Mobile screenshot via Chrome DevTools Protocol device emulation.
Usage: python mobile_shot.py <url> <out.png> [width] [height]
"""
import base64
import json
import subprocess
import sys
import time
import urllib.request

import websockets
import asyncio

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9223


async def shoot(url, out, width, height):
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", f"--remote-debugging-port={PORT}",
         "--no-first-run", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(30):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list") as r:
                    tabs = json.loads(r.read())
                target = next(t for t in tabs if t.get("type") == "page")
                break
            except Exception:
                time.sleep(0.3)
        if not target:
            raise RuntimeError("no CDP target")
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=50 * 1024 * 1024) as ws:
            mid = 0

            async def cmd(method, params=None):
                nonlocal mid
                mid += 1
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == mid:
                        return msg

            await cmd("Page.enable")
            await cmd("Page.navigate", {"url": url})
            await asyncio.sleep(2)
            await cmd("Emulation.setDeviceMetricsOverride",
                      {"width": width, "height": height, "deviceScaleFactor": 2, "mobile": True})
            await asyncio.sleep(7)
            shot = await cmd("Page.captureScreenshot", {"format": "png"})
            with open(out, "wb") as f:
                f.write(base64.b64decode(shot["result"]["data"]))
            # report layout overflow
            ev = await cmd("Runtime.evaluate",
                           {"expression": "document.documentElement.scrollWidth + ' vs ' + window.innerWidth",
                            "returnByValue": True})
            print("scrollWidth vs innerWidth:", ev["result"]["result"].get("value"))
    finally:
        proc.terminate()


if __name__ == "__main__":
    url, out = sys.argv[1], sys.argv[2]
    w = int(sys.argv[3]) if len(sys.argv) > 3 else 390
    h = int(sys.argv[4]) if len(sys.argv) > 4 else 844
    asyncio.run(shoot(url, out, w, h))

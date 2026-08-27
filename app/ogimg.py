"""OG stamp cards: generated share-preview images (1200x630 PNG).

Discord/X/Twitter do not render SVG previews, so the dossier stamp card is
rasterized with Pillow: dark case-file background, double-rule masthead,
giant ticker, the color-coded risk stamp, and a decoded.report footer.
Everything is deterministic from data already on the page; images are
cached on disk keyed by ticker + score + band so a warm render is free.

No network calls, no keys, no forecast content: only numbers already cited
in the report."""

import logging
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import APP_NAME, PRODUCT_DIR

log = logging.getLogger(__name__)

OG_DIR = PRODUCT_DIR / "data" / "og"

W, H = 1200, 630
BG = "#0b0e13"
LINE = "#212836"
LINE2 = "#2b3446"
INK = "#e8ebf2"
MUT = "#8d95a9"
DIM = "#5c6478"
BAD = "#ff4d5e"
WARN = "#ffb224"
OK = "#35d49a"
ACC = "#82a7ff"

BAND_COLORS = {"bad": BAD, "warn": WARN, "ok": OK, "dim": MUT}

_FONT_DIRS = [
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/Library/Fonts",
]

_MONO_CANDIDATES = {
    "bold": ["Menlo Bold.ttf", "Menlo.ttc", "Monaco.ttf", "DejaVuSansMono-Bold.ttf",
             "LiberationMono-Bold.ttf", "NotoSansMono-Bold.ttf", "UbuntuMono-B.ttf"],
    "regular": ["Menlo Regular.ttf", "Menlo.ttc", "Monaco.ttf", "DejaVuSansMono.ttf",
                "LiberationMono-Regular.ttf", "NotoSansMono-Regular.ttf", "UbuntuMono-R.ttf"],
}
_SANS_BOLD_CANDIDATES = [
    "Helvetica Neue Bold.ttf", "Helvetica.ttc", "Arial Bold.ttf",
    "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "NotoSans-Bold.ttf",
]

# resolved font paths cached per candidate list so the filesystem walk runs
# once per process, not once per rendered card
_FONT_CACHE = {}


def _find_font_file(name: str):
    """Locate a font by filename across the known font roots. Distros lay
    fonts out differently (Debian nests under truetype/, Fedora under
    dejavu/), so this walks the tree instead of guessing one path."""
    for root in _FONT_DIRS:
        base = Path(root)
        if not base.exists():
            continue
        try:
            for p in base.rglob(name):
                return str(p)
        except OSError:
            continue
    return None


def _load(candidates: list, size: int):
    key = (tuple(candidates), size)
    hit = _FONT_CACHE.get(key)
    if hit is not None:
        return hit
    font = None
    for name in candidates:
        path = _find_font_file(name)
        if path:
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        # last resort: any bold-capable system mono/sans via PIL's bundled
        # default keeps cards legible on bare containers
        try:
            font = ImageFont.load_default(size)
        except TypeError:
            font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _mono(size: int, bold: bool = False):
    return _load(_MONO_CANDIDATES["bold" if bold else "regular"], size)


def _sans_bold(size: int):
    return _load(_SANS_BOLD_CANDIDATES, size)


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._\-]", "_", s)[:48]


def _brand(d, x: int = 70, y: int = 58, size: int = 36) -> None:
    """Masthead wordmark: 'decoded' in ink, '.report' in accent."""
    f = _mono(size, True)
    d.text((x, y), "decoded", font=f, fill=INK)
    w = d.textlength("decoded", font=f)
    d.text((x + w, y), ".report", font=f, fill=ACC)


def build_card(ticker: str, score=None, band=None, cls="bad", subtitle: str = "",
               footer_url: str = None) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # faint scanline texture
    for y in range(0, H, 3):
        d.line([(0, y), (W, y)], fill="#0d1017", width=1)

    # double-rule frame
    d.rectangle([24, 24, W - 25, H - 25], outline=LINE2, width=2)
    d.rectangle([32, 32, W - 33, H - 33], outline=LINE, width=1)

    # masthead
    _brand(d)
    mono_s = _mono(24)
    tagline = "LIVE FROM SEC FILINGS · EVERY NUMBER CITED"
    tw = d.textlength(tagline, font=mono_s)
    d.text((W - 70 - tw, 68), tagline, font=mono_s, fill=DIM)

    # rule under masthead
    d.line([(70, 130), (W - 70, 130)], fill=LINE2, width=3)
    d.line([(70, 138), (W - 70, 138)], fill=LINE, width=1)

    # giant ticker
    if len(ticker) > 8:
        f_ticker = _sans_bold(86)
    else:
        f_ticker = _sans_bold(190 if len(ticker) <= 5 else 150)
    d.text((64, 200), ticker, font=f_ticker, fill=INK)

    # subtitle (company name / lineup description), truncated to fit
    if subtitle:
        sub = " ".join(subtitle.strip().split())
        while sub and d.textlength(sub, font=mono_s) > W - 140:
            sub = sub[:-2].rstrip()
        if sub:
            d.text((72, 430), sub, font=_mono(28), fill=MUT)

    # risk stamp block (right side)
    color = BAND_COLORS.get(cls, BAD)
    sx0, sy0, sx1, sy1 = W - 400, 170, W - 80, 400
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=10, outline=color, width=4,
                        fill="#000000")
    if score is not None:
        score_txt = str(score)
        f_score = _sans_bold(96)
        sw = d.textlength(score_txt, font=f_score)
        d.text((sx0 + ((sx1 - sx0) - sw) / 2, sy0 + 22), score_txt, font=f_score, fill=color)
        band_txt = (band or "").upper()
        f_band = _mono(30, True)
        bw_ = d.textlength(band_txt, font=f_band)
        d.text((sx0 + ((sx1 - sx0) - bw_) / 2, sy0 + 148), band_txt, font=f_band, fill=color)
    lbl = "RISK INDEX"
    f_lbl = _mono(20)
    lw = d.textlength(lbl, font=f_lbl)
    d.text((sx0 + ((sx1 - sx0) - lw) / 2, sy1 - 40), lbl, font=f_lbl, fill=MUT)

    # footer
    url = footer_url or f"decoded.report/{ticker}"
    d.text((70, H - 92), url, font=_mono(28, True), fill=ACC)
    nfa = "NOT FINANCIAL ADVICE"
    nw = d.textlength(nfa, font=_mono(22))
    d.text((W - 70 - nw, H - 88), nfa, font=_mono(22), fill=WARN)
    return img


def card_path(ticker: str, cache_key: str) -> Path:
    return OG_DIR / f"{_slug(ticker)}_{_slug(cache_key)}.png"


def ticker_png(ticker: str, score, band, cls: str, name: str) -> bytes:
    """Cache-first stamp card bytes for one ticker report."""
    key = f"{score}_{band}"
    path = card_path(ticker, key)
    if path.exists():
        try:
            return path.read_bytes()
        except OSError:
            pass
    img = build_card(ticker, score, band, cls, name or "")
    OG_DIR.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)
    prefix = _slug(ticker) + "_"
    for old in OG_DIR.glob(prefix + "*.png"):
        if old.name != path.name:
            try:
                old.unlink()
            except OSError:
                pass
    return path.read_bytes()


def bag_png(rows: list) -> bytes:
    """Bag Check lineup card: one row per ticker, ranked worst first.
    rows: [{ticker, score, band, cls}] (score None = unknown/unresolved)."""
    key = "|".join(f"{r['ticker']}_{r.get('score')}_{r.get('band')}" for r in rows)
    path = card_path("_bag", key)
    if path.exists():
        try:
            return path.read_bytes()
        except OSError:
            pass
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    for y in range(0, H, 3):
        d.line([(0, y), (W, y)], fill="#0d1017", width=1)
    d.rectangle([24, 24, W - 25, H - 25], outline=LINE2, width=2)
    d.rectangle([32, 32, W - 33, H - 33], outline=LINE, width=1)

    _brand(d, 70, 58)

    mono_s = _mono(24)
    tagline = "THE BAG CHECK · RANKED WORST FIRST"
    tw = d.textlength(tagline, font=mono_s)
    d.text((W - 70 - tw, 68), tagline, font=mono_s, fill=DIM)

    d.line([(70, 130), (W - 70, 130)], fill=LINE2, width=3)
    d.line([(70, 138), (W - 70, 138)], fill=LINE, width=1)

    f_tick = _mono(44, True)
    f_score = _sans_bold(44)
    f_band = _mono(22)
    row_y = 168
    shown = [r for r in rows if not r.get("error")][:6]
    for i, r in enumerate(shown):
        color = BAND_COLORS.get(r.get("cls") or "", MUT)
        rank = _mono(26)
        d.text((76, row_y + 12), f"{i + 1}.", font=rank, fill=DIM)
        d.text((126, row_y + 4), str(r["ticker"]), font=f_tick, fill=INK)
        score_txt = "?" if r.get("score") is None else str(r["score"])
        sw = d.textlength(score_txt, font=f_score)
        band_txt = (r.get("band") or "NO FILINGS").upper()
        bw_ = d.textlength(band_txt, font=f_band)
        total = sw + 18 + bw_
        sx = W - 90 - total
        d.text((sx, row_y + 10), score_txt, font=f_score, fill=color)
        d.text((sx + sw + 18, row_y + 20), band_txt, font=f_band, fill=color)
        ry = row_y + 74
        d.line([(76, ry), (W - 76, ry)], fill=LINE, width=1)
        row_y += 78
        if row_y > H - 150:
            break

    url = "decoded.report/watchlist"
    d.text((70, H - 92), url, font=_mono(28, True), fill=ACC)
    nfa = "NOT FINANCIAL ADVICE"
    nw = d.textlength(nfa, font=_mono(22))
    d.text((W - 70 - nw, H - 88), nfa, font=_mono(22), fill=WARN)

    OG_DIR.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)
    prefix = "_bag_"
    for old in OG_DIR.glob(prefix + "*.png"):
        if old.name != path.name:
            try:
                old.unlink()
            except OSError:
                pass
    return path.read_bytes()


def _frame(d) -> None:
    """Shared card chrome: scanline texture + double-rule frame."""
    for y in range(0, H, 3):
        d.line([(0, y), (W, y)], fill="#0d1017", width=1)
    d.rectangle([24, 24, W - 25, H - 25], outline=LINE2, width=2)
    d.rectangle([32, 32, W - 33, H - 33], outline=LINE, width=1)


def _masthead(d, tagline: str) -> None:
    _brand(d, 70, 58)
    mono_s = _mono(24)
    tw = d.textlength(tagline, font=mono_s)
    d.text((W - 70 - tw, 68), tagline, font=mono_s, fill=DIM)
    d.line([(70, 130), (W - 70, 130)], fill=LINE2, width=3)
    d.line([(70, 138), (W - 70, 138)], fill=LINE, width=1)


def _footer(d, url: str) -> None:
    d.text((70, H - 92), url, font=_mono(28, True), fill=ACC)
    nfa = "NOT FINANCIAL ADVICE"
    nw = d.textlength(nfa, font=_mono(22))
    d.text((W - 70 - nw, H - 88), nfa, font=_mono(22), fill=WARN)


def _ledger(d, rows: list, y0: int, step: int = 30, size: int = 24) -> None:
    """Dotted-leader rows: (label, value, color). Caller guarantees the rows
    fit above the footer."""
    f_k = _mono(size)
    f_v = _mono(size, True)
    f_dots = _mono(16)
    y = y0
    for label, val, color in rows:
        label_u = label.upper()
        d.text((72, y), label_u, font=f_k, fill=MUT)
        vw = d.textlength(val, font=f_v)
        d.text((W - 72 - vw, y), val, font=f_v, fill=color or INK)
        gap = W - 144 - d.textlength(label_u, font=f_k) - vw
        if gap > 24:
            dots = "." * int(gap / d.textlength(".", font=f_dots))
            d.text((80 + d.textlength(label_u, font=f_k), y + 7), dots,
                   font=f_dots, fill=DIM)
        y += step


def slice_png(ticker: str, sh: int, name: str, shares_now=None, pct_12m=None,
              close=None) -> bytes:
    """Personal dilution damage card for one holder's share count.
    Same math as the on-page calculator, rendered as a shareable stamp."""
    key = f"v3_{ticker}_{sh}_{shares_now}_{pct_12m}_{close}"
    path = card_path("_slice", key)
    if path.exists():
        try:
            return path.read_bytes()
        except OSError:
            pass
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _frame(d)
    _masthead(d, "YOUR STAKE · LIVE FROM SEC FILINGS")

    f_ticker = _sans_bold(130 if len(ticker) <= 5 else 108)
    d.text((64, 160), ticker, font=f_ticker, fill=INK)

    sub = " ".join((name or "").strip().split())
    while sub and d.textlength(sub, font=_mono(24)) > W - 140:
        sub = sub[:-2].rstrip()
    if sub:
        d.text((72, 312), sub, font=_mono(24), fill=MUT)

    # right stamp: the share-count move over the trailing 12 months
    pct = None
    try:
        pct = float(pct_12m) if pct_12m is not None else None
    except (TypeError, ValueError):
        pct = None
    if pct is None:
        color = MUT
    elif pct > 5:
        color = BAD
    elif pct < 0:
        color = OK
    else:
        color = WARN
    sx0, sy0, sx1, sy1 = W - 400, 160, W - 80, 378
    d.rounded_rectangle([sx0, sy0, sx1, sy1], radius=10, outline=color, width=4,
                        fill="#000000")
    if pct is None:
        big, small = "N/A", "NO 12M COUNT"
    elif pct >= 0:
        big, small = f"+{pct:.1f}%", "SHARE COUNT · 12M"
    else:
        big, small = f"{pct:.1f}%", "COUNT SHRANK · 12M"
    f_big = _sans_bold(84 if len(big) <= 6 else 64)
    bw = d.textlength(big, font=f_big)
    d.text((sx0 + ((sx1 - sx0) - bw) / 2, sy0 + 26), big, font=f_big, fill=color)
    f_small = _mono(20, True)
    sw = d.textlength(small, font=f_small)
    d.text((sx0 + ((sx1 - sx0) - sw) / 2, sy0 + 136), small, font=f_small, fill=color)
    lbl = "THE PRINTING PRESS"
    f_lbl = _mono(20)
    lw = d.textlength(lbl, font=f_lbl)
    d.text((sx0 + ((sx1 - sx0) - lw) / 2, sy1 - 38), lbl, font=f_lbl, fill=MUT)

    # ledger of what the position means, most shareable lines first.
    # At most 3 rows: everything must clear the footer line at y ~ 510.
    rows = []
    sh_txt = f"{sh:,}"
    rows.append(("YOU HOLD", f"{sh_txt} SHARES", INK))
    if shares_now:
        stake_now = sh / float(shares_now) * 100
        now_txt = f"{int(shares_now):,}"
        rows.append(("STAKE TODAY",
                     f"{stake_now:.4f}% OF {now_txt} OUTSTANDING", INK))
        if pct is not None and pct > 0:
            needed = round(sh * (1 + pct / 100))
            more = needed - sh
            rows.append(("TO KEEP THAT SLICE YOU NEED",
                         f"{needed:,} (+{more:,})", BAD))
        elif pct is not None and pct < 0:
            rows.append(("YOUR SLICE TODAY", "BIGGER THAN A YEAR AGO", OK))
    if close:
        try:
            val = sh * float(close)
            prec = 4 if float(close) < 1 else 2
            rows.append(("AT LAST CLOSE ($" + f"{float(close):.{prec}f})",
                         "~$" + f"{val:,.0f}", INK))
        except (TypeError, ValueError):
            pass
    if pct is not None and pct > 0 and shares_now and len(rows) < 3:
        year_ago = float(shares_now) / (1 + pct / 100)
        stake_then = sh / year_ago * 100
        rows.append(("SAME SLICE A YEAR AGO", f"{stake_then:.4f}%", MUT))
    _ledger(d, rows[:3], 416, step=36)

    _footer(d, f"decoded.report/{ticker}")

    OG_DIR.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)
    # personal cards pile up fast; prune stale ones beyond a day
    cutoff = time.time() - 86400
    for old in OG_DIR.glob("_slice_*.png"):
        if old.name != path.name:
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    return path.read_bytes()


def bagvs_png(rows_a: list, avg_a, rows_b: list, avg_b, winner: str) -> bytes:
    """Bag vs Bag matchup card: two lineups, average risk index crowned."""
    key = "|".join(["v3", f"A:{avg_a}", *[f"{r['ticker']}_{r.get('score')}" for r in rows_a],
                    f"B:{avg_b}", *[f"{r['ticker']}_{r.get('score')}" for r in rows_b],
                    winner])
    path = card_path("_bagvs", key)
    if path.exists():
        try:
            return path.read_bytes()
        except OSError:
            pass
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    _frame(d)
    _masthead(d, "WHOSE BAG IS WORSE")

    mid = W // 2
    d.line([(mid, 160), (mid, H - 130)], fill=LINE, width=1)

    def side(x0, x1, rows, avg, tag, is_winner):
        head_c = BAD if is_winner else MUT
        f_tag = _mono(30, True)
        tw = d.textlength(tag, font=f_tag)
        d.text((x0 + ((x1 - x0) - tw) / 2, 176), tag, font=f_tag, fill=head_c)
        f_avg = _sans_bold(110)
        avg_txt = "?" if avg is None else str(int(round(avg)))
        aw = d.textlength(avg_txt, font=f_avg)
        d.text((x0 + ((x1 - x0) - aw) / 2, 230), avg_txt, font=f_avg, fill=head_c)
        cap = "AVG RISK INDEX"
        f_cap = _mono(20)
        cw = d.textlength(cap, font=f_cap)
        d.text((x0 + ((x1 - x0) - cw) / 2, 356), cap, font=f_cap, fill=DIM)
        if is_winner:
            crown = "WORSE BAG"
            f_crown = _mono(22, True)
            kw = d.textlength(crown, font=f_crown)
            d.text((x0 + ((x1 - x0) - kw) / 2, 382), crown, font=f_crown, fill=BAD)
        f_row = _mono(28)
        f_sc = _sans_bold(28)
        shown = [r for r in rows if not r.get("error")][:3]
        ry = 420
        for r in shown:
            c = BAND_COLORS.get(r.get("cls") or "", MUT)
            score_txt = "?" if r.get("score") is None else str(r["score"])
            d.text((x0 + 40, ry), str(r["ticker"])[:6], font=f_row, fill=INK)
            sw = d.textlength(score_txt, font=f_sc)
            d.text((x1 - 40 - sw, ry), score_txt, font=f_sc, fill=c)
            ry += 33

    side(40, mid - 10, rows_a, avg_a, "BAG A", winner == "a")
    side(mid + 10, W - 40, rows_b, avg_b, "BAG B", winner == "b")

    _footer(d, "decoded.report/bag-vs")

    OG_DIR.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)
    prefix = "_bagvs_"
    for old in OG_DIR.glob(prefix + "*.png"):
        if old.name != path.name:
            try:
                old.unlink()
            except OSError:
                pass
    return path.read_bytes()


def default_png() -> bytes:
    """Home/landing share card, cached on disk under a fixed name."""
    path = card_path("_default", "v2")
    if path.exists():
        try:
            return path.read_bytes()
        except OSError:
            pass
    img = build_card("decoded.report", None, None, "bad",
                     "Read the actual filings: every number cited.",
                     footer_url="decoded.report")
    OG_DIR.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)
    return path.read_bytes()

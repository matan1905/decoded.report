"""Risk scoring and plain-English signal interpretation.

Deterministic, no external keys: everything derives from SEC EDGAR facts
already fetched by SecClient / DeltaEngine. Tune the weights dict freely;
the score is a screening heuristic, never a forecast."""

from datetime import date

# ---- tunable thresholds ------------------------------------------------

WEIGHTS = {
    "going_concern": {"base": 20, "per_hit": 2, "cap": 32},
    "atm": {"base": 6, "per_hit": 2, "cap": 24},
    "reverse_split": {"base": 10, "per_hit": 1.5, "cap": 18},
    "delisting": {"base": 12, "per_hit": 1.5, "cap": 22},
}

DILUTION_12M = [(50.0, 20), (25.0, 14), (10.0, 8), (3.0, 4)]  # pct -> points
RUNWAY_MONTHS = [(6, 14), (12, 8), (24, 3)]
CASH_FALL_PCT = 30
EVENTS_CAP = 15  # max points from official critical-item 8-Ks (trailing 12m)

BANDS = [
    (65, "SEVERE", "bad"),
    (45, "ELEVATED", "warn"),
    (25, "GUARDED", "warn"),
    (0, "QUIET", "ok"),
]

# phrase -> (flag dict key, plain-English meaning for a holder)
MEANINGS = {
    "at the market": (
        "atm",
        "An ATM lets the company print and sell new shares into the market any day it wants. Every print dilutes your slice.",
    ),
    "reverse stock split": (
        "reverse_split",
        "A reverse split shrinks the share count to lift the price above $1. Repeat reverse splits are the classic death-spiral signature.",
    ),
    "going concern": (
        "going_concern",
        "Auditors flagged substantial doubt the company survives the next 12 months without new money.",
    ),
    "delisting": (
        "delisting",
        "The exchange has threatened to kick the stock off. Delisting pressure usually ends in a reverse split or a fire-sale raise.",
    ),
}


def _band(score: int):
    for floor, label, cls in BANDS:
        if score >= floor:
            return label, cls
    return BANDS[-1][1], BANDS[-1][2]


def _flag_key(phrase: str) -> str:
    return MEANINGS.get(phrase, ("", ""))[0]


def _flag_meaning(phrase: str) -> str:
    return MEANINGS.get(phrase, ("", ""))[1]


def _severity(count) -> str:
    if count is None:
        return "unknown"
    if count <= 0:
        return "ok"
    if count <= 2:
        return "warn"
    return "bad"


def annotate_flags(flags: list) -> list:
    out = []
    for f in flags:
        g = dict(f)
        g.setdefault("count_all", None)
        g.setdefault("verified", None)
        # verified = we opened the linked documents and filtered boilerplate;
        # fall back to the raw full-text count when verification unavailable
        g["effective"] = g["verified"] if g["verified"] is not None else g.get("count")
        g["key"] = _flag_key(f.get("phrase", ""))
        g["meaning"] = _flag_meaning(f.get("phrase", ""))
        g["severity"] = _severity(g["effective"])
        out.append(g)
    return out


def _flag_points(annotated: list) -> int:
    pts = 0
    for f in annotated:
        w = WEIGHTS.get(f.get("key"))
        if not w:
            continue
        eff = f.get("effective")
        if eff is None or eff <= 0:
            continue
        raw = w["base"] + w["per_hit"] * min(eff, 12)
        pts += min(w["cap"], int(round(raw)))
    return pts


def dilution_profile(history: list, runway_months=None) -> dict:
    """Share-count trend from the XBRL series: 12-month growth, yearly
    doubling pace, and an inline SVG sparkline path (0 0 W H viewbox)."""
    if not history or len(history) < 2:
        return None
    first, last = history[0], history[-1]
    if not first.get("val") or not last.get("val"):
        return None
    try:
        d_last = date.fromisoformat(last["end"])
        d_first = date.fromisoformat(first["end"])
        span_days = max((d_last - d_first).days, 1)
    except Exception:
        return None

    # growth over the trailing 365 days
    pct_12m = None
    ref = None
    cutoff = "0000-00-00"
    target_ts = None
    try:
        from datetime import timedelta

        target_ts = d_last - timedelta(days=365)
        cutoff = target_ts.isoformat()
    except Exception:
        pass
    for p in history:
        if p["end"] <= cutoff and p.get("val"):
            ref = p
    if ref and ref["val"]:
        pct_12m = round((last["val"] - ref["val"]) / ref["val"] * 100, 1)

    years = span_days / 365.25
    cagr = ((last["val"] / first["val"]) ** (1 / years) - 1) * 100 if last["val"] > 0 else None

    # svg sparkline, 560x90 viewbox
    vals = [p["val"] for p in history if p.get("val")]
    W, H, PAD = 560.0, 90.0, 6.0
    vmin, vmax = min(vals), max(vals)
    spread = max(vmax - vmin, vmax * 0.02, 1.0)
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = PAD + (W - 2 * PAD) * (i / max(n - 1, 1))
        y = H - PAD - (H - 2 * PAD) * ((v - vmin) / spread)
        pts.append(f"{x:.1f},{y:.1f}")
    line = " ".join(pts)
    area = f"M{PAD},{H - PAD} L" + " L".join(pts) + f" L{W - PAD},{H - PAD} Z"

    growing = (cagr or 0) > 10 or (pct_12m or 0) > 10

    # markers: share-count collapses > 40% are reverse splits (the count
    # only drops like that when old shares are consolidated)
    markers = []
    for i in range(1, len(history)):
        prev_v, v = history[i - 1].get("val"), history[i].get("val")
        if not prev_v or not v:
            continue
        if v < prev_v * 0.6:
            x = PAD + (W - 2 * PAD) * (i / max(n - 1, 1))
            y = H - PAD - (H - 2 * PAD) * ((v - vmin) / spread)
            markers.append({"x": round(x, 1), "y": round(y, 1), "end": history[i]["end"]})

    return {
        "n": n,
        "first_val": first["val"],
        "last_val": last["val"],
        "first_end": first["end"],
        "last_end": last["end"],
        "span_years": round(years, 1),
        "growth_total_pct": round((last["val"] - first["val"]) / first["val"] * 100, 1) if first["val"] else None,
        "pct_12m": pct_12m,
        "cagr_pct": round(cagr, 1) if cagr is not None else None,
        "growing": growing,
        "svg_line": line,
        "svg_area": area,
        "svg_w": W,
        "svg_h": H,
        "markers": markers,
    }


def bar_chart(entries: list) -> dict:
    """Vertical bar chart for a value series (cash or quarterly revenue).
    entries: [{end, val}] oldest -> newest. Pure inline SVG, no JS."""
    if not entries or len(entries) < 2:
        return None
    vals = [e["val"] for e in entries if e.get("val") is not None]
    if len(vals) < 2:
        return None
    W, H, PAD_B, PAD_T = 560.0, 120.0, 22.0, 26.0
    n = len(entries)
    vmax = max(vals + [1.0])
    vmin = min(vals + [0.0])
    spread = max(vmax - vmin, vmax * 0.15, 1.0)
    slot = (W - 20) / n
    bw = min(46.0, slot * 0.62)
    rects = []
    labels = []
    last_up = vals[-1] >= vals[-2]
    for i, e in enumerate(entries):
        v = e.get("val")
        if v is None:
            continue
        x = 10 + slot * i + (slot - bw) / 2
        frac = (v - vmin) / spread
        bh = max(3.0, (H - PAD_B - PAD_T) * frac)
        y = H - PAD_B - bh
        color = "#35d49a" if (i == n - 1 and last_up) else ("#ff4d5e" if i == n - 1 else "#39435a")
        rects.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="2" fill="{color}"></rect>'
        )
        q = e["end"]
        labels.append(
            f'<text x="{x + bw / 2:.1f}" y="{H - 6}" font-size="10" fill="#5c6478" text-anchor="middle" font-family="monospace">{q[:4]}·{int(q[5:7])}</text>'
        )
    first_v, last_v = vals[0], vals[-1]
    pct = round((last_v - first_v) / first_v * 100, 1) if first_v else None
    return {
        "svg": "".join(rects) + "".join(labels),
        "w": W,
        "h": H,
        "n": n,
        "first": {"end": entries[0]["end"], "val": first_v},
        "last": {"end": entries[-1]["end"], "val": last_v},
        "pct": pct,
        "last_up": last_up,
    }


def insider_chart(rows: list, months_back: int = 6):
    """Monthly open-market buy vs sell dollar flow from parsed Form 4 rows.
    Bars above the line are insider cash buying; below the line is selling.
    Pure inline SVG. Returns None when there is nothing meaningful to draw."""
    if not rows:
        return None
    buckets = {}
    for r in rows:
        if r.get("direction") not in ("BUY", "SELL") or r.get("kind") != "common":
            continue
        d = (r.get("date") or "")[:7]
        if len(d) != 7:
            continue
        sh = r.get("shares") or 0.0
        px = r.get("price") or 0.0
        val = sh * px
        b = buckets.setdefault(d, {"buy": 0.0, "sell": 0.0})
        b["buy" if r["direction"] == "BUY" else "sell"] += val
    if not buckets:
        return None

    # last N calendar months ending at the most recent month with data
    def _add_month(m: str, delta: int) -> str:
        y, mo = int(m[:4]), int(m[5:7])
        idx = y * 12 + (mo - 1) + delta
        return f"{idx // 12:04d}-{idx % 12 + 1:02d}"

    latest = max(buckets)
    series = [_add_month(latest, -(months_back - 1 - i)) for i in range(months_back)]
    vals = [buckets.get(m, {"buy": 0.0, "sell": 0.0}) for m in series]
    if all(v["buy"] == 0 and v["sell"] == 0 for v in vals):
        return None

    W, H, MID = 560.0, 120.0, 60.0
    PAD_L, PAD_R = 8.0, 8.0
    vmax = max([v["buy"] for v in vals] + [v["sell"] for v in vals] + [1.0])
    half = (H / 2) - 14
    slot = (W - PAD_L - PAD_R) / len(series)
    bw = min(16.0, slot * 0.28)
    parts = []
    labels = []
    for i, m in enumerate(series):
        cx = PAD_L + slot * i + slot / 2
        v = vals[i]
        if v["sell"] > 0:
            hgt = max(2.5, half * (v["sell"] / vmax))
            parts.append(
                f'<rect x="{cx - bw - 1:.1f}" y="{MID:.1f}" width="{bw:.1f}" height="{hgt:.1f}" rx="1.5" fill="#ff4d5e"></rect>'
            )
        if v["buy"] > 0:
            hgt = max(2.5, half * (v["buy"] / vmax))
            parts.append(
                f'<rect x="{cx + 1:.1f}" y="{MID - hgt:.1f}" width="{bw:.1f}" height="{hgt:.1f}" rx="1.5" fill="#35d49a"></rect>'
            )
        labels.append(
            f'<text x="{cx:.1f}" y="{H - 6}" font-size="10" fill="#5c6478" text-anchor="middle" font-family="monospace">{m}</text>'
        )
    total_buy = sum(v["buy"] for v in vals)
    total_sell = sum(v["sell"] for v in vals)
    return {
        "svg": "".join(parts),
        "labels": "".join(labels),
        "w": W,
        "h": H,
        "mid": MID,
        "months": [
            {"m": m, "buy": round(vals[i]["buy"]), "sell": round(vals[i]["sell"])}
            for i, m in enumerate(series)
        ],
        "total_buy": round(total_buy),
        "total_sell": round(total_sell),
    }


def compute_risk(flags: list, history: list, runway_months=None, cash_delta_pct=None,
                 events_scored: dict = None) -> dict:
    annotated = annotate_flags(flags)
    pts = _flag_points(annotated)

    # official critical-item 8-Ks inside the trailing 12 months: the
    # company's own materiality notices (bankruptcy, delisting notice,
    # withdrawn numbers), once per type, capped
    events_pts = min(EVENTS_CAP, sum((events_scored or {}).values()))

    dil = dilution_profile(history)
    dil_pts = 0
    if dil:
        p12 = dil.get("pct_12m")
        if p12 is not None:
            for floor, add in DILUTION_12M:
                if p12 >= floor:
                    dil_pts = add
                    break

    run_pts = 0
    if runway_months is not None:
        for floor, add in RUNWAY_MONTHS:
            if runway_months < floor:
                run_pts = add
                break

    cash_pts = 0
    if cash_delta_pct is not None and cash_delta_pct <= -CASH_FALL_PCT:
        cash_pts = 5

    # chronic behavior: the flag existed for years, not just this cycle.
    # Only live flags count: a phrase filtered as boilerplate this year
    # should not keep charging points for years-old boilerplate either.
    chronic_pts = 0
    for f in annotated:
        eff, c_all = f.get("effective"), f.get("count_all")
        if eff and c_all and c_all >= max(10, 3 * eff):
            chronic_pts += 2
    chronic_pts = min(chronic_pts, 6)

    score = int(min(100, pts + dil_pts + run_pts + cash_pts + chronic_pts + events_pts))
    band, cls = _band(score)

    worst = sorted(
        [f for f in annotated if f.get("severity") == "bad"],
        key=lambda f: -(f.get("count") or 0),
    )
    drivers = []
    for f in worst[:2]:
        drivers.append(f["label"].split(" (")[0].lower())
    if dil_pts >= 8:
        drivers.append("share count climbing")
    if run_pts >= 8:
        drivers.append("months of cash left")
    if events_pts >= 6:
        drivers.append("official exchange/bankruptcy notices")

    return {
        "score": score,
        "band": band,
        "cls": cls,
        "flags_score": pts,
        "dilution_score": dil_pts,
        "runway_score": run_pts,
        "cash_score": cash_pts,
        "chronic_score": chronic_pts,
        "events_score": events_pts,
        "events_scored": dict(events_scored or {}),
        "runway_months": runway_months,
        "drivers": drivers,
        "flags": annotated,
        "dilution": dil,
        "story": _story(annotated, dil, runway_months, score),
    }


def _story(flags: list, dil: dict, runway_months, score: int) -> str:
    parts = []
    bad = [f for f in flags if f.get("severity") == "bad"]
    total_hits = sum(f["effective"] for f in flags if f.get("effective"))
    total_all = sum(f.get("count_all") or 0 for f in flags if f.get("count_all"))
    if bad:
        names = " and ".join(f["label"].split(" (")[0].lower() for f in bad[:2])
        lead = f"{total_hits} red-flag mentions across this year's filings, led by {names}"
        if total_all > total_hits * 2 and total_all >= 15:
            lead += f", and {total_all} since 2001, so this is a long-running pattern"
        parts.append(lead)
    elif total_hits:
        parts.append("no concentrated red flags in this year's filings")
    else:
        parts.append("the red-flag scanner came back clean this year")

    if dil and dil.get("pct_12m") is not None:
        p = dil["pct_12m"]
        if p >= 25:
            parts.append(f"share count jumped {p:.0f}% in a year (holders are being diluted fast)")
        elif p >= 10:
            parts.append(f"share count rose {p:.0f}% over the past year")
        elif p <= -5:
            parts.append(f"share count shrank {abs(p):.0f}% over the past year (shrinking, not printing)")
        else:
            parts.append("share count roughly stable")

    if runway_months is not None:
        m = max(int(round(runway_months)), 0)
        if m <= 6:
            parts.append(f"only about {m} month{'s' if m != 1 else ''} of cash at the current burn rate")
        elif m <= 18:
            parts.append(f"roughly {m} months of cash at the current burn rate")

    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]

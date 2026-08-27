import logging

log = logging.getLogger(__name__)

# Tunable thresholds. All heuristics are deterministic rules over cited
# filing facts. Nothing here is a forecast.
THRESHOLDS = {
    "runway_years_healthy": 2.0,
    "runway_years_warn": 1.0,
    "runway_years_avoid": 0.5,
    "monthly_burn_ratio_avoid": 0.5,   # monthly burn > 50% of cash balance
    "dilution_qoq_warn": 0.10,          # +10% shares in a quarter is notable
    "dilution_qoq_avoid": 0.25,         # +25% shares in a quarter is heavy
    "rev_growth_yoy_bull": 0.30,        # 30%+ YoY revenue growth
    "rev_decline_yoy_bear": -0.10,      # -10%+ YoY revenue decline
    "cash_per_share_floor_ratio": 0.25, # cash/share >= 25% of price supports the range
    "fv_earnings_mult_low": 10.0,       # fair-value bracket: trailing EPS x band
    "fv_earnings_mult_high": 18.0,
    "fv_sales_mult_low": 1.0,           # fair-value bracket: trailing revenue/share x band
    "fv_sales_mult_high": 3.0,
}

DISCLAIMER = (
    "Not financial advice. The verdict is a deterministic reading of cited filing "
    "facts (share count, cash, revenue, burn, going concern), not a prediction, "
    "forecast, or recommendation. Fair value is a range derived from the same "
    "facts, not a target price."
)


def _pct(cur, prior):
    try:
        if prior in (None, 0):
            return None
        return (cur - prior) / prior
    except TypeError:
        return None


def _fmt(v):
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "not reported"


def _fmt_ps(v):
    try:
        f = float(v)
        return f"${f:,.4f}" if 0 < abs(f) < 1 else f"${f:,.2f}"
    except (TypeError, ValueError):
        return "not reported"


class VerdictEngine:
    def __init__(self, thresholds: dict = None):
        self.t = thresholds or dict(THRESHOLDS)

    def compute(self, data: dict) -> dict:
        """data keys: shares_cur, shares_prior, cash_cur, cash_prior,
        revenue_cur, revenue_prior, burn_months, cash_for_runway,
        dilution_qoq, cash_per_share, price, price_date, going_concern"""
        reasons = []
        cons = []
        flags = []

        dilution = _pct(data.get("shares_cur"), data.get("shares_prior"))
        cash = data.get("cash_cur")
        rev_cur = data.get("revenue_cur")
        rev_prior = data.get("revenue_prior")
        rev_growth = _pct(rev_cur, rev_prior)
        runway = data.get("burn_months")
        price = data.get("price")

        # ---- dilution (share count) ----------------------------------
        if dilution is not None:
            if dilution >= self.t["dilution_qoq_avoid"]:
                flags.append("AVOID")
                reasons.append(
                    f"Shares outstanding grew {dilution*100:.1f}% between the last two reported quarters, "
                    "a heavy dilution event."
                )
            elif dilution >= self.t["dilution_qoq_warn"]:
                flags.append("WARN")
                reasons.append(
                    f"Shares outstanding grew {dilution*100:.1f}% between the last two reported quarters, "
                    "meaningful dilution."
                )
            else:
                reasons.append(
                    f"Share count is stable-ish ({dilution*100:+.1f}% change between the last two reported quarters)."
                )
        else:
            cons.append("The filings do not publish a comparable share count for the last two quarters, so dilution could not be measured for this verdict.")

        # ---- cash & burn / runway -------------------------------------
        if cash is not None and runway is not None and cash > 0:
            if runway >= self.t["runway_years_healthy"]:
                reasons.append(f"Cash of {_fmt(cash)} funds over {runway:.1f} years of operating burn.")
            elif runway >= self.t["runway_years_warn"]:
                flags.append("WARN")
                cons.append(f"Cash of {_fmt(cash)} funds only about {runway:.1f} years of operating burn.")
            elif runway >= self.t["runway_years_avoid"]:
                flags.append("SELL")
                cons.append(
                    f"Cash of {_fmt(cash)} funds only ~{runway:.1f} years of operating burn "
                    f"(that is a going-concern pressure without a raise)."
                )
            else:
                flags.append("AVOID")
                cons.append(
                    f"Cash of {_fmt(cash)} funds under half a year of operating burn, a hard going-concern "
                    "pressure without fresh capital."
                )
        elif cash is not None and data.get("monthly_burn"):
            flags.append("AVOID")
            cons.append(
                f"Monthly burn {_fmt(data['monthly_burn'])} is over half of the {_fmt(cash)} cash balance; "
                "runway is short unless capital is raised."
            )
        else:
            cons.append(
                "The filings do not report usable cash and operating-burn figures, "
                "so runway could not be computed for this verdict."
            )

        # ---- revenue trend ---------------------------------------------
        if rev_growth is not None:
            if rev_growth >= self.t["rev_growth_yoy_bull"]:
                reasons.append(f"Revenue grew {rev_growth*100:.1f}% period over period, a bullish signal in the filings.")
            elif rev_growth <= self.t["rev_decline_yoy_bear"]:
                flags.append("SELL")
                cons.append(f"Revenue fell {abs(rev_growth)*100:.1f}% period over period.")
            else:
                reasons.append(f"Revenue was roughly flat ({rev_growth*100:+.1f}% period over period).")
        else:
            cons.append("The filings do not publish comparable revenue figures for two consecutive periods, so the revenue trend could not be measured.")

        # ---- fair value range ------------------------------------------
        fair = self._fair_range(data)
        if fair:
            reasons.append(
                f"Fair-value range anchored to cited facts: {_fmt_ps(fair['low'])} to {_fmt_ps(fair['high'])} per share."
            )
        else:
            cons.append("No fair-value bracket: the filings (or the optional price feed) do not provide enough cited numbers to compute one honestly.")

        # ---- going concern (verified facts-level) -----------------------
        if data.get("going_concern"):
            flags.append("AVOID")
            cons.append(
                "Verified going-concern warnings in a recent filing: auditors or "
                "management flagged substantial doubt about continuing (document "
                "text checked, boilerplate excluded)."
            )

        # ---- verdict ---------------------------------------------------
        verdict, confidence = self._verdict(flags, reasons, cons, data)

        basis = " ".join(reasons) if reasons else "No cited positive facts available."
        con_text = " ".join(cons) if cons else "No cited negative facts available."

        return {
            "verdict": verdict,
            "basis": " ".join(reasons) if reasons else "No cited positive facts available.",
            "con": " ".join(cons) if cons else "No cited negative facts available.",
            "confidence": confidence,
            "reasons": reasons,
            "cons": cons,
            "fair_value": fair,
            "disclaimer": DISCLAIMER,
            "method": (
                "Deterministic rules over cited SEC filing facts: share-count change between the last two "
                "reported quarters (dilution), cash-to-burn runway from the most recent operating cash flow, "
                "period-over-period revenue growth, and a fair-value range anchored to cash per share vs price. "
                "Thresholds are tunable in app/verdict_engine.py. Not a prediction."
            ),
        }

    def _fair_range(self, data: dict) -> dict:
        """Multi-anchor screening bracket. Anchor selection by situation:
        profitable -> trailing earnings multiple; dying (< 1yr cash, losing
        money) -> book equity and cash in a wind-down; otherwise -> trailing
        revenue multiple with book support. Deterministic and cited, never a
        target price."""
        shares = data.get("shares_cur")
        if not shares:
            return None
        price = data.get("price")
        cash = data.get("cash_cur")
        cash_ps = (cash / shares) if cash else None
        floor = cash_ps * 0.5 if cash_ps is not None else 0.0

        ttm_ni = data.get("ttm_net_income")
        ttm_rev = data.get("ttm_revenue")
        equity = data.get("equity")
        book_ps = (equity / shares) if (equity and equity > 0 and shares) else None
        dying = (
            data.get("runway_months") is not None and data["runway_months"] < 12
            and (ttm_ni is None or ttm_ni <= 0)
        )

        low = None
        high = None
        anchors = []

        if ttm_ni is not None and ttm_ni > 0:
            eps = ttm_ni / shares
            low = eps * self.t["fv_earnings_mult_low"]
            high = eps * self.t["fv_earnings_mult_high"]
            anchors.append(
                f"trailing earnings of {_fmt_ps(eps)}/share at {self.t['fv_earnings_mult_low']}-{self.t['fv_earnings_mult_high']}x"
            )
            if book_ps:
                low = max(low, book_ps * 0.5)
                anchors.append(f"book equity of {_fmt_ps(book_ps)}/share as a support level")
        elif dying:
            if book_ps:
                low, high = book_ps * 0.5, book_ps * 1.0
                anchors.append(f"book equity of {_fmt_ps(book_ps)}/share at 0.5-1.0x")
            elif cash_ps is not None:
                low, high = cash_ps * 0.5, cash_ps * 1.2
            if cash_ps is not None:
                high = min(high, cash_ps * 1.2)
                anchors.append("multiples stripped: under a year of cash at the current burn, so the bracket caps at wind-down cash")
        elif ttm_rev is not None and ttm_rev > 0:
            rps = ttm_rev / shares
            low = rps * self.t["fv_sales_mult_low"]
            high = rps * self.t["fv_sales_mult_high"]
            anchors.append(
                f"trailing revenue of {_fmt_ps(rps)}/share at {self.t['fv_sales_mult_low']}-{self.t['fv_sales_mult_high']}x"
            )
            if book_ps:
                low = max(low, book_ps * 0.5)
                anchors.append(f"book equity of {_fmt_ps(book_ps)}/share as a support level")
        elif book_ps:
            low, high = book_ps * 0.7, book_ps * 1.5
            anchors.append(f"book equity of {_fmt_ps(book_ps)}/share at 0.7-1.5x")

        if low is None:
            if cash_ps is None:
                return None
            low, high = cash_ps * 0.5, cash_ps * 2.0
            anchors.append("cash per share in a wind-down (no earnings, revenue or book equity to anchor on)")

        if cash_ps is not None:
            anchors.append(f"cash floor: {_fmt_ps(cash_ps)}/share in a wind-down")

        if price is not None:
            low = min(low, price)
        return {
            "low": round(max(low, 0), 4),
            "high": round(max(high, low), 4),
            "cash_per_share": round(cash_ps, 4) if cash_ps is not None else None,
            "anchors": anchors,
            "price_note": "bracket computed from filings alone; the market price loads at the top of this page when the free market-data tier has quota" if price is None else None,
        }

    def _verdict(self, flags, reasons, cons, data) -> tuple:
        have = sum(1 for v in [
            data.get("shares_cur"), data.get("cash_cur"), data.get("revenue_cur"),
            data.get("burn_months"), data.get("price"),
        ] if v is not None)

        if "AVOID" in flags:
            verdict = "AVOID"
        elif flags.count("SELL") >= 2:
            verdict = "SELL"
        elif "SELL" in flags:
            # cash < 1 year runway is a sell signal, unless revenue is strongly growing
            rev_growth = _pct(data.get("revenue_cur"), data.get("revenue_prior"))
            if rev_growth is not None and rev_growth >= self.t["rev_growth_yoy_bull"]:
                verdict = "HOLD"
            else:
                verdict = "SELL"
        elif "WARN" in flags:
            verdict = "HOLD"
        elif len(reasons) >= 2 and all(
            "grew" in r or "stable" in r or "flat" in r for r in reasons
        ):
            verdict = "HOLD"
        else:
            verdict = "HOLD"

        if have >= 4:
            confidence = "HIGH"
        elif have >= 2:
            confidence = "MED"
        else:
            confidence = "LOW"
        return verdict, confidence


def _change_value(change):
    if not change or not change.get("current") or change["current"].get("val") is None:
        return None
    return change["current"]["val"]


def build_inputs(delta: dict, price: dict) -> dict:
    """Build the verdict input dict from a DeltaEngine output + price."""
    changes = {c.get("key"): c for c in delta.get("changes", [])}
    sha = delta.get("shares_cur") or {}
    burn = changes.get("burn") or {}
    shares = sha.get("val")
    cash = _change_value(changes.get("cash"))
    revenue = _change_value(changes.get("revenue"))
    rev_prior = None
    rev_prior_note = None
    if changes.get("revenue") and changes["revenue"].get("prior") and changes["revenue"]["prior"].get("val") is not None:
        rev_prior = changes["revenue"]["prior"]["val"]
    else:
        rev_prior_note = "prior revenue not reported"
    burn_months = burn.get("runway_months")
    months_years = (burn_months / 12.0) if burn_months else None
    price_close = None
    price_date = None
    if price and not price.get("degraded") and price.get("close") is not None:
        price_close = price.get("close")
        price_date = price.get("date") or price.get("timestamp")
    return {
        "shares_cur": shares,
        "shares_prior": (delta.get("shares_prior") or {}).get("val"),
        "cash_cur": cash,
        "cash_prior": (changes.get("cash") and changes["cash"].get("prior") or {}).get("val"),
        "revenue_cur": revenue,
        "revenue_prior": rev_prior,
        "revenue_prior_note": rev_prior_note,
        "burn_months": months_years,
        "runway_months": burn_months,
        "monthly_burn": burn.get("monthly_burn"),
        "dilution_qoq": _pct(shares, (delta.get("shares_prior") or {}).get("val")),
        "cash_per_share": (cash / shares) if (cash and shares) else None,
        "price": price_close,
        "price_date": price_date,
        "price_source": (price or {}).get("source"),
        "price_degraded": bool(price and price.get("degraded")),
        "going_concern": None,
        "ttm_net_income": (delta.get("valuation") or {}).get("ttm_net_income"),
        "ttm_revenue": (delta.get("valuation") or {}).get("ttm_revenue"),
        "equity": (delta.get("valuation") or {}).get("equity"),
    }
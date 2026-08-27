import logging
import re
from datetime import date, timedelta
from html import unescape

from . import db
from .sec_client import SecClient

log = logging.getLogger(__name__)

SHARE_CONCEPT = "EntityCommonStockSharesOutstanding"

# concept priority lists: (primary, [fallbacks]) in tax/unit groups
CASH = "CashAndCashEquivalentsAtCarryingValue"
CASH_ALT = ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsAndShortTermInvestments"]
ASSETS = ["Assets", "AssetsCurrent"]
LIAB = ["Liabilities", "LiabilitiesCurrent"]
REV = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenuesNetOfInterestIncome"]
OCF = ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]

ITEM_RE = re.compile(r"Item\s+(\d+\.\d+)\s+(.*?)(?=Item\s+\d+\.\d+|SIGNATURES|CERTIFICATION|EXHIBIT|^\s*$)", re.S | re.I)


def _fmt_compact(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    if abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:,.0f}"


def _fmt_shares(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "n/a"
    if abs(v) >= 1e9:
        return f"{v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"{v/1e6:.2f}M"
    return f"{v:,.0f}"


class DeltaEngine:
    def __init__(self, sec: SecClient):
        self.sec = sec

    # ---- low level facts ---------------------------------------------

    def _entries(self, facts, tax, concept, unit):
        return (facts.get("facts", {}).get(tax, {}).get(concept, {}).get("units", {}).get(unit) or [])

    def _best_concept(self, facts, tax, concepts, unit, require_start):
        best = None
        latest = "0000-00-00"
        for c in concepts:
            cand = self._entries(facts, tax, c, unit)
            if not cand:
                continue
            ends = []
            for e in cand:
                if require_start and not (e.get("start") and e.get("end")):
                    continue
                if not require_start and not e.get("end"):
                    continue
                ends.append(e.get("end", ""))
            if not ends:
                continue
            top = max(ends)
            if top > latest:
                latest = top
                best = cand
        return best

    def _last_two(self, facts, tax, concepts, unit):
        best = self._best_concept(facts, tax, concepts, unit, require_start=False)
        if not best:
            return None, None
        by_end = {}
        for e in best:
            end = e.get("end")
            if not end:
                continue
            prior = by_end.get(end)
            if prior is None or e.get("filed", "") >= prior.get("filed", ""):
                by_end[end] = e
        ordered = sorted(by_end.values(), key=lambda e: e.get("end", ""), reverse=True)
        return (ordered[0], ordered[1]) if len(ordered) >= 2 else (ordered[0], None)

    def _last_duration(self, facts, tax, concepts, unit):
        best = self._best_concept(facts, tax, concepts, unit, require_start=True)
        if not best:
            return None
        by_end = {}
        for e in best:
            if not e.get("start") or not e.get("end"):
                continue
            end = e.get("end")
            try:
                days = (date.fromisoformat(end) - date.fromisoformat(e["start"])).days
            except Exception:
                days = 10_000
            prior = by_end.get(end)
            if prior is None or days < prior.get("_days", 10_000) or (
                days == prior.get("_days", 10_000) and e.get("filed", "") >= prior.get("filed", "")
            ):
                entry = dict(e)
                entry["_days"] = days
                by_end[end] = entry
        ordered = sorted(by_end.values(), key=lambda e: e.get("end", ""), reverse=True)
        return ordered[0] if ordered else None

    # ---- computed change set -----------------------------------------

    def compute(self, ticker: str, cik: int, deep: bool = True) -> dict:
        facts = self.sec.facts(cik)
        changes = []

        hist = self.sec.share_history(cik)
        shares_cur = hist[-1] if hist else None
        shares_prior = hist[-2] if len(hist) >= 2 else None
        changes.extend(self._share_change(shares_cur, shares_prior, cik))

        cash_cur, cash_prior = self._last_two(facts, "us-gaap", CASH_ALT, "USD")
        changes.extend(self._cash_change(cash_cur, cash_prior, cik))

        changes.extend(self._balance_change(facts, cik))

        revenue_change, rev_cur = self._revenue_change(facts, cik)
        changes.append(revenue_change)

        burn = self._burn(facts, cik, cash_cur)
        changes.append(burn)

        new_filing = self._new_filing(cik) if deep else None

        try:
            valuation = self.valuation_facts(cik)
        except Exception as exc:
            log.warning("valuation facts failed for cik %s: %s", cik, exc)
            valuation = {}

        return {
            "ticker": ticker,
            "cik": cik,
            "cik10": str(cik).zfill(10),
            "changes": [c for c in changes if c is not None],
            "new_filing": new_filing,
            "shares_cur": shares_cur,
            "shares_prior": shares_prior,
            "valuation": valuation,
        }

    # ---- sub-computations ---------------------------------------------

    def _share_change(self, cur, prior, cik) -> list:
        if not cur or not prior:
            return [self._na("Shares outstanding", "a share count for two consecutive periods")]
        cv, pv = cur.get("val"), prior.get("val")
        if cv is None or pv is None or pv == 0:
            return [self._na("Shares outstanding", "a share count for two consecutive periods")]
        delta_pct = (cv - pv) / pv * 100
        direction = "rose" if delta_pct > 0 else ("fell" if delta_pct < 0 else "held flat at")
        dil = " (dilution)" if delta_pct > 3 else (" (buyback/shrink)" if delta_pct < -3 else "")
        text = (
            f"Shares outstanding {direction} {_fmt_shares(pv)} to {_fmt_shares(cv)} "
            f"({delta_pct:+.1f}%) between {prior.get('form')} {prior.get('end')} "
            f"and {cur.get('form')} {cur.get('end')}{dil}."
        )
        return [{
            "key": "shares",
            "label": "Shares outstanding",
            "text": text,
            "current": self._cite(cur, cik),
            "prior": self._cite(prior, cik),
            "delta_pct": round(delta_pct, 1),
        }]

    def _cash_change(self, cur, prior, cik) -> list:
        if not cur or not prior:
            return [self._na("Cash and equivalents", "us-gaap:" + CASH)]
        cv, pv = cur.get("val"), prior.get("val")
        if cv is None or pv is None:
            return [self._na("Cash and equivalents", "us-gaap:" + CASH)]
        delta_pct = (cv - pv) / pv * 100 if pv else 0
        direction = "rose" if delta_pct > 0 else ("fell" if delta_pct < 0 else "held flat at")
        dollar = _fmt_compact(cv - pv)
        adj = dollar if delta_pct >= 0 else f"-{_fmt_compact(pv - cv)}"
        text = (
            f"Cash and equivalents {direction} {_fmt_compact(pv)} to {_fmt_compact(cv)} "
            f"({adj}, {delta_pct:+.1f}%) between {prior.get('form')} {prior.get('end')} "
            f"and {cur.get('form')} {cur.get('end')}."
        )
        return [{
            "key": "cash",
            "label": "Cash and equivalents",
            "text": text,
            "current": self._cite(cur, cik),
            "prior": self._cite(prior, cik),
            "delta_pct": round(delta_pct, 1),
        }]

    def _balance_change(self, facts, cik) -> list:
        out = []
        for concept, label in [(ASSETS, "Total assets"), (LIAB, "Total liabilities")]:
            cur, prior = self._last_two(facts, "us-gaap", concept, "USD")
            if not cur or not prior or cur.get("val") is None or prior.get("val") is None:
                out.append(self._na(label, "us-gaap:" + concept[0]))
                continue
            pv, cv = prior.get("val"), cur.get("val")
            delta_pct = (cv - pv) / pv * 100 if pv else 0
            direction = "rose to" if delta_pct > 0 else ("fell to" if delta_pct < 0 else "held at")
            text = (
                f"{label} {direction} {_fmt_compact(cv)} "
                f"(from {_fmt_compact(pv)}, {delta_pct:+.1f}%) between {prior.get('form')} {prior.get('end')} "
                f"and {cur.get('form')} {cur.get('end')}."
            )
            out.append({
                "key": concept[0].lower(),
                "label": label,
                "text": text,
                "current": self._cite(cur, cik),
                "prior": self._cite(prior, cik),
                "delta_pct": round(delta_pct, 1),
            })
        return out

    def _revenue_change(self, facts, cik):
        cur = self._last_duration(facts, "us-gaap", REV, "USD")
        prior = None
        if cur and cur.get("start") and cur.get("end"):
            prior = self._prior_duration(
                facts, cur.get("end"), self._period_class(cur["start"], cur["end"])
            )
        if not cur or not prior or cur.get("val") is None or prior.get("val") is None:
            return self._na("Revenue", "us-gaap:" + REV[0]), None
        pv, cv = prior.get("val"), cur.get("val")
        delta_pct = (cv - pv) / pv * 100 if pv else 0
        direction = "rose" if delta_pct > 0 else ("fell" if delta_pct < 0 else "held flat at")
        text = (
            f"Revenue {direction} {_fmt_compact(pv)} to {_fmt_compact(cv)} "
            f"({delta_pct:+.1f}%) between the period ended {prior.get('end')} "
            f"and the period ended {cur.get('end')} ({cur.get('form')})."
        )
        return {
            "key": "revenue",
            "label": "Revenue",
            "text": text,
            "current": self._cite(cur, cik),
            "prior": self._cite(prior, cik),
            "delta_pct": round(delta_pct, 1),
        }, cur

    def _period_class(self, start: str, end: str) -> str:
        try:
            days = (date.fromisoformat(end) - date.fromisoformat(start)).days
        except Exception:
            return "unknown"
        if days >= 300:
            return "annual"
        if days <= 160:
            return "quarter"
        return "ytd"

    def _prior_duration(self, facts, cur_end, cur_class, unit="USD"):
        best = self._best_concept(facts, "us-gaap", REV, unit, require_start=True)
        if not best:
            return None
        by_end = {}
        for e in best:
            if not e.get("start") or not e.get("end") or e.get("end") == cur_end:
                continue
            clazz = self._period_class(e["start"], e["end"])
            if clazz != cur_class:
                continue
            end = e.get("end")
            prior = by_end.get(end)
            if prior is None or e.get("filed", "") >= prior.get("filed", ""):
                by_end[end] = e
        ordered = sorted(by_end.values(), key=lambda e: e.get("end", ""), reverse=True)
        return ordered[0] if ordered else None

    def _burn(self, facts, cik, cash_cur) -> dict:
        ocf = self._last_duration(facts, "us-gaap", OCF, "USD")
        if not ocf or ocf.get("val") is None:
            return self._na("Cash burn / runway", "us-gaap:" + OCF[0])
        val = ocf.get("val")
        start, end = ocf.get("start"), ocf.get("end")
        if not start or not end:
            return self._na("Cash burn / runway", "us-gaap:" + OCF[0])
        try:
            months = max(1.0, (date.fromisoformat(end) - date.fromisoformat(start)).days / 30.44)
        except Exception:
            months = 3.0
        monthly_burn = abs(val) / months if val < 0 else 0.0
        cash = (cash_cur or {}).get("val")
        if not cash:
            return {
                "key": "burn",
                "label": "Cash burn / runway",
                "text": (
                    f"Operating cash flow was {_fmt_compact(val)} over the period ended {end} "
                    f"(~{_fmt_compact(monthly_burn)}/month burn). Cash for runway is not published in the "
                    f"facts, so runway is n/a."
                ),
                "current": self._cite(ocf, cik),
                "prior": None,
                "runway_months": None,
            }
        runway = cash / monthly_burn if monthly_burn > 0 else None
        if monthly_burn == 0:
            text = f"Operating cash flow was positive ({_fmt_compact(val)}) over the period ended {end}; no cash deficit to burn."
        else:
            text = (
                f"Monthly operating cash deficit ~{_fmt_compact(monthly_burn)} ({_fmt_compact(val)} over the period ended {end} "
                f"in {ocf.get('form')}). Against {_fmt_compact(cash)} cash "
                f"is roughly {runway/12:.1f} years of runway ({runway:.0f} months)."
            )
        return {
            "key": "burn",
            "label": "Cash burn / runway",
            "text": text,
            "current": self._cite(ocf, cik),
            "prior": None,
            "delta_pct": None,
            "runway_months": runway,
            "monthly_burn": monthly_burn if monthly_burn else None,
            "ocf_val": val,
            "period_end": end,
            "period_start": start,
        }

    # ---- histories for charts -----------------------------------------

    def histories(self, cik: int, max_points: int = 8) -> dict:
        """Cash and quarterly revenue series, oldest -> newest, for charts."""
        facts = self.sec.facts(cik)

        cash = []
        best = self._best_concept(facts, "us-gaap", CASH_ALT, "USD", require_start=False)
        if best:
            by_end = {}
            for e in best:
                end = e.get("end")
                if not end or e.get("val") is None:
                    continue
                prior = by_end.get(end)
                if prior is None or e.get("filed", "") >= prior.get("filed", ""):
                    by_end[end] = e
            cash = sorted(by_end.values(), key=lambda e: e["end"])[-max_points:]

        revenue = []
        rev_best = self._best_concept(facts, "us-gaap", REV, "USD", require_start=True)
        if rev_best:
            by_end = {}
            for e in rev_best:
                if not e.get("start") or not e.get("end"):
                    continue
                if self._period_class(e["start"], e["end"]) != "quarter":
                    continue
                end = e.get("end")
                prior = by_end.get(end)
                if prior is None or e.get("filed", "") >= prior.get("filed", ""):
                    by_end[end] = e
            revenue = sorted(by_end.values(), key=lambda e: e["end"])[-max_points:]

        return {
            "cash": [
                {"end": e["end"], "val": e["val"], "form": e.get("form"), "accn": e.get("accn")}
                for e in cash if e.get("val") is not None
            ],
            "revenue": [
                {"end": e["end"], "val": e["val"], "form": e.get("form"), "accn": e.get("accn")}
                for e in revenue if e.get("val") is not None
            ],
        }

    # ---- valuation facts (TTM earnings / revenue / book equity) --------

    NI_CONCEPTS = ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]
    EQUITY_CONCEPTS = [
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "StockholdersEquity",
    ]

    def _sum_ttm(self, facts, concepts) -> tuple:
        """Sum of the last 4 consecutive quarter-class periods; falls back to
        the latest annual period. Returns (value, basis_label) or (None, None)."""
        for concept in concepts:
            entries = self._entries(facts, "us-gaap", concept, "USD")
            if not entries:
                continue
            by_end = {}
            for e in entries:
                if not e.get("start") or not e.get("end") or e.get("val") is None:
                    continue
                if self._period_class(e["start"], e["end"]) == "quarter":
                    prior = by_end.get(e["end"])
                    if prior is None or e.get("filed", "") >= prior.get("filed", ""):
                        by_end[e["end"]] = e
            quarters = sorted(by_end.values(), key=lambda e: e["end"])[-4:]
            if len(quarters) == 4:
                try:
                    from datetime import date as _d
                    ends = [q["end"] for q in quarters]
                    span = (_d.fromisoformat(ends[-1]) - _d.fromisoformat(ends[0])).days
                    if 250 <= span <= 400:
                        return sum(q["val"] for q in quarters), f"trailing four quarters through {ends[-1]}"
                except Exception:
                    pass
            # annual fallback
            annual = [e for e in entries
                      if e.get("start") and e.get("end") and e.get("val") is not None
                      and self._period_class(e["start"], e["end"]) == "annual"]
            if annual:
                best = max(annual, key=lambda e: e["end"])
                return best["val"], f"last annual period through {best['end']}"
        return None, None

    def valuation_facts(self, cik: int) -> dict:
        facts = self.sec.facts(cik)
        ttm_rev, rev_basis = self._sum_ttm(facts, REV)
        ttm_ni, ni_basis = self._sum_ttm(facts, self.NI_CONCEPTS)
        equity = None
        equity_end = None
        for concept in self.EQUITY_CONCEPTS:
            entries = self._entries(facts, "us-gaap", concept, "USD")
            instants = [e for e in entries if e.get("end") and e.get("val") is not None]
            if instants:
                best = max(instants, key=lambda e: e["end"])
                equity, equity_end = best["val"], best["end"]
                break
        return {
            "ttm_revenue": ttm_rev,
            "revenue_basis": rev_basis,
            "ttm_net_income": ttm_ni,
            "net_income_basis": ni_basis,
            "equity": equity,
            "equity_end": equity_end,
        }

    # ---- new filing (8-K event) --------------------------------------

    def _new_filing(self, cik):
        recent = self.sec.submissions(cik).get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        found = None
        for i, f in enumerate(forms):
            if f == "8-K" and dates[i] >= cutoff:
                found = {"date": dates[i], "accession": accns[i], "cik": cik}
                break
        if not found:
            return None
        try:
            html = self.sec.filing_html(cik, found["accession"])
        except Exception as exc:
            log.warning("8-K html fetch failed cik %s: %s", cik, exc)
            return {
                "date": found["date"],
                "accession": found["accession"],
                "url": self.sec.live_filing_link(cik, found["accession"]),
                "items": [{
                    "item": "?",
                    "text": "HTML body could not be fetched for this filing.",
                }],
            }
        return {
            "date": found["date"],
            "accession": found["accession"],
            "url": self.sec.live_filing_link(cik, found["accession"]),
            "items": self._items(html),
        }

    def _items(self, html_text: str):
        text = self._to_text(html_text)
        items = []
        seen = set()
        for m in ITEM_RE.finditer(text):
            code = m.group(1)
            if code in seen:
                continue
            seen.add(code)
            snippet = re.sub(r"\s+", " ", m.group(2)).strip()
            snippet = re.sub(r"^(Entry into|Amendment to|Creation of|Results of|Departure of|Changes in|Notice of|Failure to|Other Events|Regulation FD|Submission of Matters|Costs Associated with|Material Modifications)[^.]*[.:]\s*", "", snippet)
            cut = snippet[:600]
            if len(snippet) > 600:
                cut = cut.rstrip() + "..."
            if len(cut) >= 80:
                items.append({"item": code, "text": cut})
            if len(items) >= 3:
                break
        return items

    @staticmethod
    def _to_text(html_text: str) -> str:
        text = re.sub(r"<script.*?</script>", " ", html_text, flags=re.S)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text)
        return re.sub(r"[ \t]+", " ", text)

    def _cite(self, entry: dict, cik: int) -> dict:
        accn = entry.get("accn", "")
        return {
            "form": entry.get("form"),
            "end": entry.get("end"),
            "filed": entry.get("filed"),
            "accn": accn,
            "val": entry.get("val"),
            "url": self.sec.live_filing_link(cik, accn) if accn else None,
        }

    def _na(self, label: str, concept: str) -> dict:
        return {
            "key": "na",
            "label": label,
            "text": (
                f"{label}: nothing to compare. This company's filings do not publish "
                f"{concept}, so no honest change can be computed."
            ),
            "current": None,
            "prior": None,
            "delta_pct": None,
            "is_na": True,
        }
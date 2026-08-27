import json
import logging
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import httpx

from . import db
from .config import SEC_USER_AGENT

log = logging.getLogger(__name__)

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

SLEEP = 0.15

SHARE_CONCEPT = "EntityCommonStockSharesOutstanding"

# (exact phrase, display label, SEC form filter, verify against boilerplate)
FLAGS = [
    ("at the market", "Dilution (at the market offering)", "10-K,10-Q,8-K,S-1,S-3,424B5,20-F,6-K,F-1,F-3", True),
    ("reverse stock split", "Reverse stock split", "10-K,10-Q,8-K,S-1,S-3,424B5,20-F,6-K,F-1,F-3", True),
    ("going concern", "Going concern doubt", "10-K,10-Q,8-K,S-1,S-3,424B5,20-F,6-K,F-1,F-3", True),
    ("delisting", "Delisting / exchange risk", "10-K,10-Q,8-K,S-1,S-3,424B5,20-F,6-K,F-1,F-3", True),
]

# phrases whose boilerplate is contractual rather than negational: warrant
# and agreement exhibits list "reverse stock split" among mechanical
# adjustment triggers ("upon any reclassification, recapitalization, ...
# or reverse stock split"). For these, a hit counts as a real warning only
# when the surrounding text shows a concrete event: a stated ratio, or an
# intent verb close to the phrase WITHOUT contract-adjustment vocabulary.
INTENT_REQUIRED = {
    "reverse stock split": {
        "ratio": ["1-for-", "1 for "],
        "verbs": [
            "effect", "effects", "effected", "approve", "approves",
            "approved", "propose", "proposes", "proposed", "intend",
            "intends", "intended", "completed", "consummated",
            "authorized", "declared",
        ],
        "mechanics": [
            "reclassification", "recapitalization", "stock dividend",
            "exercise price", "exercise price", "conversion price",
            "conversion rate", "conversion ratio", "fractional shares",
            "warrant to purchase", "pre-funded", "subscription amount",
        ],
    },
}

# 8-K item codes that matter to a holder, read straight from the SEC
# filing index (no document fetch): code -> (label, tone, plain English)
CRITICAL_8K_ITEMS = {
    "1.03": (
        "BANKRUPTCY / RECEIVERSHIP", "bad",
        "the company filed for bankruptcy, or a creditor moved to force it into one",
    ),
    "2.03": (
        "NEW DIRECT OBLIGATION", "",
        "a new debt or liability landed on the books: loan, guarantee, or an accelerated default",
    ),
    "3.01": (
        "DELISTING / EXCHANGE NOTICE", "warn",
        "the exchange formally warned the company it fails listing rules, or the stock moved markets",
    ),
    "3.02": (
        "UNREGISTERED SHARE SALE", "warn",
        "shares were sold or issued without public registration: new holders who often flip",
    ),
    "3.03": (
        "HOLDER RIGHTS MODIFIED", "warn",
        "the rights attached to your shares were materially changed",
    ),
    "4.01": (
        "AUDITOR CHANGE", "warn",
        "the certifying accountant changed midstream: worth reading whether they quit or were fired",
    ),
    "4.02": (
        "OLD NUMBERS WITHDRAWN", "bad",
        "the company told investors to stop relying on its previously reported financials",
    ),
    "5.02": (
        "OFFICER / DIRECTOR EXIT", "",
        "an officer or director left, was removed, or did not stand for re-election",
    ),
}

# points contributed to the risk index when the item was filed inside the
# trailing 12 months; applied once per type, capped by the caller
EVENT_SCORES = {
    "4.02": 12,
    "1.03": 10,
    "3.01": 6,
}

# window markers that mark a phrase match as boilerplate rather than a live
# warning (e.g. credit agreements defining "without a going concern
# qualification", or ASC language concluding there is NO doubt)
NEGATION_MARKERS = [
    "without a", "without any", "without said", "no substantial doubt",
    "does not believe", "do not believe", "not aware of",
    "concluded that there is no", "absence of", "no longer", "eliminated",
    "alleviated", "removed", "not experienced", "none of", "other than",
    "no such", "unless", "would not", "did not have", "has not had",
]

# phrase-specific resolution language: an exchange deficiency that the
# company later cured is history, not a live threat. Without this veto a
# filing saying "we regained compliance" would still count as a delisting
# warning because the phrase appears in it.
RESOLUTION_MARKERS = {
    "delisting": [
        "regained compliance", "regained conformity", "cured the deficiency",
        "cured this deficiency", "deficiency has been cured",
        "compliance has been regained", "compliance was regained",
        "relisted on", "restored to the list",
    ],
}

# how many top full-text hits per phrase we actually open and read during
# verification. Five covers most issuers' yearly hit counts; when the raw
# count of hit documents exceeds this, the UI says "at least N" instead of
# implying complete coverage.
VERIFY_DOC_CAP = 5


def _pad10(cik: int) -> str:
    return str(cik).zfill(10)


class SecClient:
    def __init__(self):
        self._client = httpx.Client(
            headers={"User-Agent": SEC_USER_AGENT},
            timeout=30.0,
            follow_redirects=True,
        )
        self._ticker_map = None
        self._ticker_map_at = 0.0
        # singleflight guards: racing workers asking for the same document
        # share one HTTP fetch instead of downloading it twice
        self._fetch_locks = {}
        self._fetch_locks_guard = threading.Lock()

    # ---- low level ---------------------------------------------------

    def _fetch_lock(self, key: str) -> threading.Lock:
        with self._fetch_locks_guard:
            return self._fetch_locks.setdefault(key, threading.Lock())

    def _get_json(self, url: str) -> dict:
        time.sleep(SLEEP)
        resp = self._client.get(url)
        resp.raise_for_status()
        return resp.json()

    def _cached(self, key: str, url: str, ttl: int = 86400) -> dict:
        hit = db.cache_get(key, ttl)
        if hit is not None:
            return json.loads(hit)
        data = self._get_json(url)
        db.cache_set(key, json.dumps(data), ttl)
        return data

    def get_text(self, url: str, cache_key: str = None, ttl: int = 86400) -> str:
        if cache_key:
            hit = db.cache_get(cache_key, ttl)
            if hit is not None:
                return hit
        # dedupe concurrent fetches of the same document (the flag scanner,
        # domain probe and 8-K parser can race on one filing): the first
        # worker downloads, the rest reuse the result it caches.
        lock = self._fetch_lock(cache_key or url)
        with lock:
            if cache_key:
                hit = db.cache_get(cache_key, ttl)
                if hit is not None:
                    return hit
            time.sleep(SLEEP)
            resp = self._client.get(url)
            resp.raise_for_status()
            text = resp.text
            if cache_key:
                db.cache_set(cache_key, text, ttl)
            return text

    def filing_html(self, cik: int, accession: str) -> str:
        pdoc = self.primary_doc_for(cik, accession)
        url = self.live_filing_link(cik, accession, pdoc)
        return self.get_text(url, cache_key=f"html:{cik}:{accession}")

    def two_recent_periodic(self, cik: int) -> list:
        recent = self.submissions(cik).get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
        out = []
        for i, f in enumerate(forms):
            if f in ("10-K", "10-Q", "8-K", "10-K/A", "10-Q/A"):
                out.append({
                    "form": f,
                    "filing_date": dates[i] if i < len(dates) else "",
                    "accession": accns[i] if i < len(accns) else "",
                })
            if len(out) >= 2:
                break
        return out

    # ---- ticker to CIK ------------------------------------------------

    def ticker_map(self) -> dict:
        now = time.time()
        if self._ticker_map is not None and now - self._ticker_map_at < 86400:
            return self._ticker_map
        bulk = self._cached("cik:map", TICKER_MAP_URL, 86400)
        mapping = {}
        for item in bulk.values():
            ticker = str(item.get("ticker", "")).upper()
            if ticker:
                mapping[ticker] = {"cik": int(item["cik_str"]), "name": item.get("title")}
        self._ticker_map = mapping
        self._ticker_map_at = now
        return mapping

    def resolve(self, ticker: str):
        return self.ticker_map().get(ticker.upper())

    def submissions(self, cik: int) -> dict:
        cik10 = _pad10(cik)
        return self._cached(f"sub:{cik10}", SUBMISSIONS_URL.format(cik10=cik10))

    # ---- share count via XBRL -------------------------------------------

    def facts(self, cik: int) -> dict:
        cik10 = _pad10(cik)
        return self._cached(f"facts:{cik10}", COMPANYFACTS_URL.format(cik10=cik10))

    def _share_units(self, cik: int):
        """Share-count series with a fallback chain: the dei point-in-time
        count, then us-gaap CommonStockSharesOutstanding, then the
        weighted-average diluted count (used when a company never publishes
        a point-in-time figure). Returns (units, source)."""
        try:
            facts = self.facts(cik)
        except Exception:
            return [], None
        chain = [
            ("dei", SHARE_CONCEPT, "shares", "reported"),
            ("us-gaap", "CommonStockSharesOutstanding", "shares", "reported"),
            ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares", "weighted-average diluted"),
        ]
        for tax, concept, unit, source in chain:
            units = (
                facts.get("facts", {})
                .get(tax, {})
                .get(concept, {})
                .get("units", {})
                .get(unit)
                or []
            )
            if units:
                return units, source
        return [], None

    def share_count(self, cik: int):
        units, source = self._share_units(cik)
        if not units:
            return None
        last = units[-1]
        return {
            "val": last.get("val"),
            "end": last.get("end"),
            "filed": last.get("filed"),
            "form": last.get("form"),
            "accn": last.get("accn"),
            "source": source,
        }

    def share_history(self, cik: int) -> list:
        """Deduplicated share-count series ordered oldest -> newest.

        One point per period end (latest filed wins). This is the raw
        material for the dilution timeline."""
        units, _source = self._share_units(cik)
        by_end = {}
        for e in units:
            end = e.get("end")
            val = e.get("val")
            if not end or val is None:
                continue
            prior = by_end.get(end)
            if prior is None or (e.get("filed") or "") >= (prior.get("filed") or ""):
                by_end[end] = {
                    "val": val,
                    "end": end,
                    "filed": e.get("filed"),
                    "form": e.get("form"),
                    "accn": e.get("accn"),
                }
        return sorted(by_end.values(), key=lambda x: x["end"])

    # ---- filing links ---------------------------------------------------

    def primary_doc_for(self, cik: int, accession: str) -> str:
        recent = self.submissions(cik).get("filings", {}).get("recent", {})
        accns = recent.get("accessionNumber", [])
        prim = recent.get("primaryDocument", [])
        for i, a in enumerate(accns):
            if a == accession:
                return prim[i] if i < len(prim) else ""
        return ""

    def live_filing_link(self, cik: int, accession: str, primary_doc: str = "") -> str:
        folder = accession.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{folder}"
        return f"{base}/{primary_doc}" if primary_doc else f"{base}/"

    # ---- filings rail ---------------------------------------------------

    def recent_filings(self, cik: int, limit: int = 6) -> list:
        recent = self.submissions(cik).get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
        prim = recent.get("primaryDocument", [])
        out = []
        for i in range(min(limit, len(forms))):
            accn = accns[i] if i < len(accns) else ""
            pdoc = prim[i] if i < len(prim) else ""
            out.append({
                "form": forms[i],
                "filing_date": dates[i],
                "accession": accn,
                "url": self.live_filing_link(cik, accn, pdoc),
            })
        return out

    # ---- insider (form 4 activity from submissions) -----------------------

    def insider_summary(self, cik: int) -> dict:
        recent = self.submissions(cik).get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        cutoff30 = (date.today() - timedelta(days=30)).isoformat()
        cutoff90 = (date.today() - timedelta(days=90)).isoformat()
        c30, c90, last_date = 0, 0, None
        for f, d in zip(forms, dates):
            if f in ("4", "4/A"):
                if last_date is None or d > last_date:
                    last_date = d
                if d >= cutoff90:
                    c90 += 1
                    if d >= cutoff30:
                        c30 += 1
        return {"count_90": c90, "count_30": c30, "last_date": last_date}

    # ---- critical 8-K history (from the filing index, zero doc fetches) ----

    def critical_8k_history(self, cik: int, days: int = 730) -> dict:
        """Official 8-K item codes for the last `days`, parsed from the
        submissions index the app already fetches. This is what the company
        itself flagged as material, in the SEC's own numbering: bankruptcy
        (1.03), delisting notices (3.01), auditor changes (4.01),
        withdrawn numbers (4.02). No extra HTTP calls."""
        recent = self.submissions(cik).get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
        prim = recent.get("primaryDocument", [])
        items_col = recent.get("items", [])
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        cutoff12 = (date.today() - timedelta(days=365)).isoformat()
        rows = []
        scored = {}
        for i, f in enumerate(forms):
            if f not in ("8-K", "8-K/A"):
                continue
            d = dates[i] if i < len(dates) else ""
            if not d or d < cutoff:
                continue
            raw = items_col[i] if i < len(items_col) else ""
            crit = []
            for code in re.split(r"[,\s]+", raw or ""):
                spec = CRITICAL_8K_ITEMS.get(code.strip())
                if not spec:
                    continue
                label, tone, meaning = spec
                crit.append({"item": code.strip(), "label": label,
                             "tone": tone, "meaning": meaning})
                if d >= cutoff12 and EVENT_SCORES.get(code.strip()):
                    scored[code.strip()] = EVENT_SCORES[code.strip()]
            if not crit:
                continue
            accn = accns[i] if i < len(accns) else ""
            rows.append({
                "date": d,
                "accn": accn,
                "url": self.live_filing_link(cik, accn, prim[i] if i < len(prim) else ""),
                "crits": crit,
            })
        return {"rows": rows, "scored": scored}

    def insider_form4_count(self, cik: int, days: int = 90) -> int:
        return self.insider_summary(cik)["count_90"]

    # ---- reporting health: has the company stopped filing? ----------------

    PERIODIC_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A",
                      "10-KSB", "10-QSB", "40-H")
    # A public company must land a periodic report at least every ~4 months
    # (10-Q deadline ~45d after quarter end). Past 150 days without one, at
    # least one filing is officially late: late, suspended, or deregistering.
    STALE_AFTER_DAYS = 150

    def last_periodic(self, cik: int) -> dict:
        """Most recent annual/quarterly report from the submissions index,
        zero extra HTTP. A stopped clock here is a first-order tell: the
        company is late with its SEC reporting, suspended, or winding down."""
        recent = self.submissions(cik).get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        cutoff = (date.today() - timedelta(days=800)).isoformat()
        best = None
        for f, d in zip(forms, dates):
            if d < cutoff:
                break  # newest-first index; nothing older is worth scanning
            if f in self.PERIODIC_FORMS and (best is None or d > best["date"]):
                best = {"form": f, "date": d}
        if not best:
            return {"status": "none_recent"}
        try:
            days = (date.today() - date.fromisoformat(best["date"])).days
        except Exception:
            return {"status": "unknown", **best}
        out = {**best, "status": "ok", "days_since": days}
        out["stale"] = days > self.STALE_AFTER_DAYS
        return out

    # ---- insider flows: parse the actual Form 4 XMLs ----------------------

    FORM4_CODE_LABELS = {
        "P": "OPEN-MARKET BUY",
        "S": "OPEN-MARKET SELL",
        "A": "GRANT/AWARD",
        "M": "OPTION EXERCISE",
        "F": "TAX WITHHOLDING",
        "G": "GIFT",
        "C": "CONVERSION",
        "X": "OPTION EXERCISE",
    }

    def _form4_accessions(self, cik: int, limit: int = 10, days: int = 90) -> list:
        recent = self.submissions(cik).get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        out = []
        for f, d, a in zip(forms, dates, accns):
            if f in ("4", "4/A") and d >= cutoff:
                out.append({"accession": a, "filed": d})
            if len(out) >= limit:
                break
        return out

    def _fetch_form4_xml(self, cik: int, accession: str):
        key = f"form4:{cik}:{accession}"
        hit = db.cache_get(key, 30 * 86400)
        if hit is not None:
            return hit
        pdoc = self.primary_doc_for(cik, accession)
        doc = pdoc.split("/")[-1] if pdoc else "ownership.xml"
        url = self.live_filing_link(cik, accession, doc)
        time.sleep(SLEEP)
        resp = self._client.get(url)
        resp.raise_for_status()
        db.cache_set(key, resp.text, 30 * 86400)
        return resp.text

    # phrases in Form 4 footnotes that mark a trade as pre-scheduled under a
    # Rule 10b5-1 plan (less alarming than a discretionary dump)
    PLAN_RE = re.compile(r"10\s*b\s*5\s*[- ]?\s*1|rule\s*10b5|trading\s+plan", re.I)

    @staticmethod
    def _parse_form4_xml(xml_text: str) -> dict:
        import xml.etree.ElementTree as ET

        def txt(el, path, default=""):
            node = el.find(path)
            return (node.text or "").strip() if node is not None and node.text else default

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return {}
        owner_name = txt(root, "reportingOwner/reportingOwnerId/rptOwnerName")
        rel = root.find("reportingOwner/reportingOwnerRelationship")
        roles = []
        if rel is not None:
            if txt(rel, "isDirector") == "1":
                roles.append("DIRECTOR")
            if txt(rel, "isOfficer") == "1":
                title = txt(root, "reportingOwner/reportingOwnerRelationship/officerTitle")
                roles.append((title or "OFFICER").upper()[:24])
            if txt(rel, "isTenPercentOwner") == "1":
                roles.append("10% OWNER")
        # footnotes carry the 10b5-1 plan language; transactions reference
        # them by id
        notes = {}
        for fn in root.findall("footnotes/footnote"):
            fid = fn.get("id")
            text = re.sub(r"\s+", " ", " ".join(fn.itertext())).strip()
            if fid and text:
                notes[fid] = text
        entries = []
        period = txt(root, "periodOfReport")
        for table_tag, child_tag, kind in [
            ("nonDerivativeTable", "nonDerivativeTransaction", "common"),
            ("derivativeTable", "derivativeTransaction", "derivative"),
        ]:
            table = root.find(table_tag)
            if table is None:
                continue
            for tx in table.findall(child_tag):
                code = txt(tx, "transactionCoding/transactionCode").upper()
                shares = txt(tx, "transactionAmounts/transactionShares/value")
                price = txt(tx, "transactionAmounts/transactionPricePerShare/value")
                d = txt(tx, "transactionDate/value") or period
                ad = txt(tx, "transactionAmounts/transactionAcquiredDisposedCode/value").upper()
                sec_title = txt(tx, "securityTitle/value")[:40]
                note_texts = []
                for fi in tx.iter("footnoteId"):
                    # references appear as <footnoteId id="F1"/> (attribute)
                    # or <footnoteId>F1</footnoteId> (text), anywhere inside
                    # the transaction row
                    fid = ((fi.get("id") or "") or (fi.text or "")).strip()
                    if fid in notes:
                        note_texts.append(notes[fid])
                plan_note = next((n for n in note_texts if SecClient.PLAN_RE.search(n)), None)
                try:
                    shares_f = float(shares)
                except ValueError:
                    shares_f = None
                try:
                    price_f = float(price)
                except ValueError:
                    price_f = None
                entries.append({
                    "date": d,
                    "code": code,
                    "label": SecClient.FORM4_CODE_LABELS.get(code, "OTHER"),
                    "shares": shares_f,
                    "price": price_f,
                    "acquired": ad == "A",
                    "kind": kind,
                    "security": sec_title,
                    "plan": bool(plan_note),
                    "plan_note": (plan_note or "")[:160],
                })
        return {"owner": owner_name, "roles": roles, "entries": entries}

    def insider_flows(self, cik: int, limit: int = 10, days: int = 120) -> dict:
        """Aggregate real insider transactions from Form 4 XMLs.
        Degrades to None when nothing parses; the caller keeps the
        count-only summary as fallback."""
        accessions = self._form4_accessions(cik, limit=limit, days=days)
        if not accessions:
            return None

        def one(a):
            try:
                xml_text = self._fetch_form4_xml(cik, a["accession"])
                parsed = self._parse_form4_xml(xml_text)
                parsed["filed"] = a["filed"]
                parsed["accession"] = a["accession"]
                parsed["url"] = self.live_filing_link(cik, a["accession"])
                return parsed
            except Exception as exc:
                log.warning("form4 parse failed cik %s accn %s: %s", cik, a["accession"], exc)
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            docs = list(pool.map(one, accessions))
        docs = [d for d in docs if d and d.get("entries")]

        buys = {"n": 0, "shares": 0.0, "value": 0.0}
        sells = {"n": 0, "shares": 0.0, "value": 0.0}
        grants = {"n": 0, "shares": 0.0}
        plan = {"n": 0, "shares": 0.0, "sells_n": 0, "sells_shares": 0.0, "buys_n": 0}
        rows = []
        for d in docs:
            for e in d["entries"]:
                sh = e.get("shares") or 0.0
                px = e.get("price") or 0.0
                if e["code"] == "P" and e["kind"] == "common":
                    buys["n"] += 1
                    buys["shares"] += sh
                    buys["value"] += sh * (px or 0)
                    direction = "BUY"
                elif e["code"] == "S" and e["kind"] == "common":
                    sells["n"] += 1
                    sells["shares"] += sh
                    sells["value"] += sh * (px or 0)
                    direction = "SELL"
                elif e["code"] in ("A", "M", "X", "G", "C"):
                    grants["n"] += 1
                    grants["shares"] += sh
                    direction = None
                else:
                    direction = None
                if e.get("plan"):
                    plan["n"] += 1
                    plan["shares"] += sh
                    if e["code"] == "S" and e["kind"] == "common":
                        plan["sells_n"] += 1
                        plan["sells_shares"] += sh
                    elif e["code"] == "P" and e["kind"] == "common":
                        plan["buys_n"] += 1
                rows.append({
                    "date": e["date"],
                    "filed": d["filed"],
                    "owner": d.get("owner") or "?",
                    "roles": d.get("roles") or [],
                    "label": e["label"],
                    "direction": direction,
                    "shares": e.get("shares"),
                    "price": e.get("price"),
                    "kind": e["kind"],
                    "security": e.get("security"),
                    "plan": bool(e.get("plan")),
                    "url": d["url"],
                })
        rows.sort(key=lambda r: (r["date"], r["filed"]), reverse=True)
        net = buys["shares"] - sells["shares"]
        if buys["n"] == 0 and sells["n"] == 0:
            verdict = "GRANTS ONLY"
        elif net > 0:
            verdict = "NET BUYING"
        elif net < 0:
            verdict = "NET SELLING"
        else:
            verdict = "BALANCED"
        return {
            "buys": buys,
            "sells": sells,
            "grants": grants,
            "plan": plan,
            "all_sells_planned": bool(sells["n"]) and plan["sells_n"] >= sells["n"],
            "net_shares": net,
            "verdict": verdict,
            "rows": rows[:12],
            "docs_parsed": len(docs),
            "docs_seen": len(accessions),
        }

    # ---- red-flag scanner (EFTS) ------------------------------------------

    def scan_flags(self, cik: int, refresh: bool = False):
        key = f"flags:v3:{_pad10(cik)}"
        if not refresh:
            cached = db.cache_get(key, 86400)
            if cached is not None:
                return json.loads(cached)
        out = self._scan_flags_uncached(cik)
        db.cache_set(key, json.dumps(out), 86400)
        return out

    def _scan_flags_uncached(self, cik: int):
        cik10 = _pad10(cik)
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=365)).isoformat()
        # EDGAR full-text search covers filings back to 2001; that is the
        # issuer's whole searchable history for practical purposes.
        allstart = "2001-01-01"

        # fire all phrase x window queries concurrently (well inside the
        # 10 req/s SEC budget), then verify documents concurrently
        def run_query(spec):
            phrase, forms, window_start, want_top = spec
            try:
                return spec, self._efts_query(cik, phrase, forms, window_start, end, want_top)
            except Exception as exc:
                log.warning("EFTS scan failed for cik %s phrase %r: %s", cik, phrase, exc)
                return spec, (None, [])

        specs = []
        for phrase, label, forms, verify in FLAGS:
            specs.append((phrase, forms, start, True))
            specs.append((phrase, forms, allstart, False))
        with ThreadPoolExecutor(max_workers=6) as pool:
            query_results = list(pool.map(run_query, specs))

        by_phrase = {}
        for (phrase, _forms, wstart, want_top), (total, top) in query_results:
            slot = by_phrase.setdefault(phrase, {"recent": None, "all": None})
            if want_top:
                slot["recent"] = (total, top)
            else:
                slot["all"] = total

        # document verification for boilerplate-prone phrases, concurrent.
        # Jobs are deduped by accession: one document can carry several
        # flagged phrases (or be hit twice by racing queries), and each
        # unique document must be fetched exactly once. Capped at
        # VERIFY_DOC_CAP documents per phrase so a pathological filer with
        # hundreds of hits cannot stall the render.
        verify_by_accn = {}
        for phrase, label, forms, verify in FLAGS:
            slot = by_phrase.get(phrase, {})
            recent = slot.get("recent")
            if verify and recent and recent[1]:
                for h in recent[1][:VERIFY_DOC_CAP]:
                    if h.get("accn"):
                        verify_by_accn.setdefault(h["accn"], set()).add(phrase)

        def run_verify(job):
            accn, phrases = job
            try:
                html = self.filing_html(cik, accn)
            except Exception as exc:
                log.warning("verify fetch failed cik %s accn %s: %s", cik, accn, exc)
                return job, {p: (0, 0) for p in phrases}
            text = self._normalized_doc_text(html)
            return job, {p: self._count_phrase(text, p) for p in phrases}

        verify_by_phrase = {}
        if verify_by_accn:
            with ThreadPoolExecutor(max_workers=4) as pool:
                for (_accn, phrases), per_phrase in pool.map(run_verify, list(verify_by_accn.items())):
                    for phrase in phrases:
                        occ, real = per_phrase.get(phrase, (0, 0))
                        agg = verify_by_phrase.setdefault(phrase, {"occ": 0, "real": 0})
                        agg["occ"] += occ
                        agg["real"] += real

        results = []
        for phrase, label, forms, verify in FLAGS:
            slot = by_phrase.get(phrase, {})
            recent = slot.get("recent") or (None, [])
            entry = {
                "label": label, "phrase": phrase,
                "count": recent[0], "count_all": slot.get("all"),
                "verified": None, "top": recent[1],
                "docs_checked": 0, "docs_matched": 0,
                "partial_coverage": False,
            }
            agg = verify_by_phrase.get(phrase)
            if verify and agg is not None:
                opened = {h.get("accn") for h in recent[1][:VERIFY_DOC_CAP]}
                entry["verified"] = agg["real"] if agg["occ"] else 0
                entry["docs_checked"] = len(opened)
                entry["docs_matched"] = recent[0] or 0
                # when more documents than we could open carry the phrase
                # this year, the verified count is a floor rather than a
                # total: the UI says "at least N" so it never over-claims.
                entry["partial_coverage"] = entry["docs_matched"] > VERIFY_DOC_CAP
                entry["top"] = [h for h in recent[1] if h.get("accn") in opened]
            results.append(entry)
        return {"results": results, "range_start": start, "range_end": end}

    def _normalized_doc_text(self, html: str) -> str:
        """Strip tags and normalize punctuation variants so hyphenated terms
        ("at-the-market") match the same way the SEC's own phrase search
        treats them."""
        text = self._strip_html(html or "")[:1_500_000]
        text = re.sub(r"[-\u2010-\u2015/\\]", " ", text)
        return re.sub(r"\s+", " ", text)

    @staticmethod
    def _count_phrase(text: str, phrase: str) -> tuple:
        """Count occurrences of a phrase in normalized document text.
        Returns (occurrences, real_warnings): matches wrapped in
        negation/boilerplate or resolution language do not count as real
        warnings, and phrases in INTENT_REQUIRED must additionally show
        concrete-event language (a stated ratio, or an intent verb with no
        contract adjustment vocabulary nearby)."""
        low = text.lower()
        p = phrase.lower()
        spec = INTENT_REQUIRED.get(phrase)
        resolved = RESOLUTION_MARKERS.get(phrase, [])
        occ, real = 0, 0
        start_at = 0
        while True:
            i = low.find(p, start_at)
            if i < 0:
                break
            occ += 1
            window = low[max(0, i - 260): i + len(p) + 260]
            pre = low[max(0, i - 120): i]
            ok = not any(neg in window for neg in NEGATION_MARKERS)
            if ok and resolved:
                ok = not any(r in window for r in resolved)
            if ok and spec:
                has_ratio = any(r in window for r in spec["ratio"])
                has_verb = any(v in pre for v in spec["verbs"])
                has_mech = any(m in window for m in spec["mechanics"])
                ok = has_ratio or (has_verb and not has_mech)
            if ok:
                real += 1
            start_at = i + len(p)
        return occ, real

    def _verify_phrase_in_doc(self, cik: int, accn: str, phrase: str):
        """Open the actual document a full-text hit points at, count phrase
        occurrences, and drop ones wrapped in boilerplate/negation language.
        Returns (occurrences, real_warnings)."""
        try:
            html = self.filing_html(cik, accn)
        except Exception as exc:
            log.warning("verify fetch failed cik %s accn %s: %s", cik, accn, exc)
            return 0, 0
        return self._count_phrase(self._normalized_doc_text(html), phrase)

    def efts_search(self, q: str, forms: str, start: str, end: str,
                    size: int = 20) -> dict:
        """Generic EDGAR full-text search without a CIK filter (used for
        cross-filer searches like "which 13F institutions hold this issuer").
        Returns the raw EFTS payload; caller parses hits."""
        params = {
            "q": f'"{q}"',
            "forms": forms,
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
            "from": "0",
            "size": str(size),
        }
        url = EFTS_URL + "?" + urllib.parse.urlencode(params)
        return self._get_json(url)

    @staticmethod
    def _strip_html(html_text: str) -> str:
        from html import unescape
        text = re.sub(r"<script.*?</script>", " ", html_text, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = unescape(text)
        return re.sub(r"\s+", " ", text)

    def _efts_query(self, cik: int, phrase: str, forms: str, start: str, end: str, want_top: bool = True):
        cik10 = _pad10(cik)
        params = {
            "q": f'"{phrase}"',
            "forms": forms,
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
            "ciks": cik10,
            "from": "0",
            "size": str(VERIFY_DOC_CAP) if want_top else "0",
        }
        url = EFTS_URL + "?" + urllib.parse.urlencode(params)
        data = self._get_json(url)
        total = data.get("hits", {}).get("total", {}).get("value", 0)
        top = []
        if want_top:
            for hit in data.get("hits", {}).get("hits", [])[:VERIFY_DOC_CAP]:
                src = hit.get("_source", {})
                accn = src.get("adsh", "")
                if accn:
                    form = src.get("form", "")
                    fdate = src.get("file_date", "")
                    # the hit's own document filename (the phrase may live in
                    # an exhibit, not the primary document)
                    doc = (hit.get("_id", "").split(":", 1)[1]
                           if ":" in hit.get("_id", "") else "")
                    if not doc:
                        doc = self.primary_doc_for(cik, accn)
                    top.append({
                        "form": form,
                        "date": fdate,
                        "accn": accn,
                        "url": self.live_filing_link(cik, accn, doc),
                    })
        return total, top
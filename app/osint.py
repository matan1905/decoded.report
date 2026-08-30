"""OSINT layer: signals from outside the financial statements.

Everything here is keyless and verified in research/00-verification-notes.md:
- RDAP domain intel (registrar, registration age, expiry) via rdap.verisign.com
  with rdap.org fallback for non .com/.net TLDs
- Offering pipeline + control filings derived from the issuer's own SEC
  submissions index (424B5, S-1/S-3/F-1, SC 13D/G)
- OFAC sanctions screen via the official SDN CSV (cached weekly)

Each probe degrades to an honest "not found" instead of crashing the page."""

import csv
import io
import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import httpx

from . import db
from .config import SEC_USER_AGENT

log = logging.getLogger(__name__)

RDAP_VERISIGN = "https://rdap.verisign.com/{gtld}/v1/domain/{domain}"
RDAP_REDIRECT = "https://rdap.org/domain/{domain}"
OFAC_CSV = "https://sanctionslistservice.ofac.treas.gov/api/download/sdn.csv"

DOMAIN_RE = re.compile(
    r"https?://(?:www\.)?([a-z0-9][a-z0-9\-]{1,61}\.(?:com|net|org|io|co|ai|xyz))\b",
    re.I,
)
# investor-relations subdomains are the strongest identity hint a filing
# carries: the issuer's investor-relations host -> bare registrable domain
IR_HINT_RE = re.compile(
    r"(?:https?://)?(?:investors?|ir)\.([a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+)",
    re.I,
)
BAD_DOMAINS = {
    "sec.gov", "www.sec.gov", "edgar.com", "nasdaq.com", "nyse.com",
    "google.com", "youtube.com", "twitter.com", "x.com", "facebook.com",
    "linkedin.com", "instagram.com", "github.com", "w3.org",
    "fasb.org", "xbrl.org", "doi.org", "iana.org", "unicode.org", "iso.org",
    "uspto.gov", "irs.gov", "treasury.gov", "federalreserve.gov", "bls.gov",
    "census.gov", "gpo.gov", "law.cornell.edu", "sec.gov.uk", "otcmarkets.com",
    "globenewswire.com", "businesswire.com", "prnewswire.com", "reuters.com",
    "bloomberg.com", "glassdoor.com", "dtcc.com", "theocc.com", "finra.org",
    "sipc.org", "moodys.com", "spglobal.com", "zoom.us", "outlook.com",
    "hotmail.com", "gmail.com", "yahoo.com", "office.com", "microsoft.com",
    "adobe.com", "docusign.net", "salesforce.com", "constantcontact.com",
}
BAD_SUBSTR = ("sec.gov", "edgar", "w3.org", "schema.org", "example.com", "xbrl",
              "fasb", "doi.org", "isbn", "uspto", "irs.gov", "treasury")

TLD_TO_GTLD = {"com": "com", "net": "net"}


def _client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": SEC_USER_AGENT},
        timeout=timeout,
        follow_redirects=True,
    )


def _cached_json(key: str, ttl: int, fetch):
    hit = db.cache_get(key, ttl)
    if hit is not None:
        import json
        return json.loads(hit)
    try:
        data = fetch()
    except Exception as exc:
        log.warning("osint probe %s failed: %s", key, exc)
        return None
    if data is None:
        return None
    import json
    db.cache_set(key, json.dumps(data), ttl)
    return data


# ---- offering pipeline + control filings (from submissions index) ---------

def offering_pipeline(submissions: dict) -> dict:
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    cutoff = (date.today() - timedelta(days=365)).isoformat()
    b5, shelf, control = 0, 0, 0
    last_b5 = None
    for f, d in zip(forms, dates):
        if d < cutoff:
            continue
        if f.startswith("424B"):
            b5 += 1
            if last_b5 is None or d > last_b5:
                last_b5 = d
        elif f in ("S-1", "S-1/A", "S-3", "S-3/A", "F-1", "F-1/A", "F-3", "F-3/A"):
            shelf += 1
        elif f.startswith("SC 13D") or f.startswith("SC 13G"):
            control += 1
    return {
        "424B5_12m": b5,
        "last_424B5": last_b5,
        "shelf_12m": shelf,
        "control_12m": control,
    }


# ---- company domain from the filings themselves ----------------------------

def find_domain(sec, cik: int):
    """Pull the issuer's own web address out of its latest periodic filings."""

    def probe():
        recent = sec.submissions(cik).get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accns = recent.get("accessionNumber", [])
        priority = ("10-Q", "10-K", "8-K", "20-F", "S-1", "10-K/A", "10-Q/A")
        order = sorted(
            range(len(forms)),
            key=lambda i: priority.index(forms[i]) if forms[i] in priority else 99,
        )
        # one document per form type: variety beats depth here (the website
        # reference usually lives on a 10-K cover even when 10-Qs omit it)
        picked = []
        seen_forms = set()
        for i in order:
            f = forms[i]
            if f not in priority or f in seen_forms:
                continue
            seen_forms.add(f)
            picked.append(i)
            if len(picked) >= 4:
                break
        body_counts = Counter()
        ir_hints = Counter()
        ns_counts = Counter()
        tried = 0

        def fetch_doc(i):
            accn = accns[i] if i < len(accns) else ""
            if not accn:
                return None
            try:
                return i, sec.filing_html(cik, accn)
            except Exception:
                return i, None

        with ThreadPoolExecutor(max_workers=4) as pool:
            fetched = [r for r in pool.map(fetch_doc, picked) if r and r[1]]
        for i, html in fetched:
            tried += 1
            html = html[:1_500_000]
            # 1) investor-relations subdomains: strongest signal, the issuer
            #    points holders at its own domain here
            for m in IR_HINT_RE.finditer(html):
                d = m.group(1).lower().rstrip(".")
                if "." in d and d not in BAD_DOMAINS and not any(b in d for b in BAD_SUBSTR):
                    ir_hints[d] += 5
            # 2) domains referenced in the document body (strong signal)
            stripped = re.sub(r"xmlns(:[A-Za-z0-9_\-]+)?\s*=\s*\"[^\"]*\"", " ", html)
            stripped = re.sub(r"xmlns(:[A-Za-z0-9_\-]+)?\s*=\s*'[^']*'", " ", stripped)
            for m in DOMAIN_RE.finditer(stripped):
                d = m.group(1).lower()
                if d in BAD_DOMAINS or any(b in d for b in BAD_SUBSTR):
                    continue
                body_counts[d] += 2
            # 3) XBRL filer namespaces: the convention is http://(company domain)/...
            for m in re.finditer(
                r"xmlns[^=]*=\s*[\"']https?://(?:www\.)?([^/\"'\s]+)", html
            ):
                d = m.group(1).lower()
                d = d[4:] if d.startswith("www.") else d
                if ("." not in d) or d in BAD_DOMAINS or any(b in d for b in BAD_SUBSTR):
                    continue
                ns_counts[d] += 1
        merged = body_counts + ir_hints
        source_counts = merged or ns_counts
        if source_counts:
            dom, n = source_counts.most_common(1)[0]
            src = "ir-hint" if ir_hints and dom in ir_hints else ("body" if body_counts else "namespace")
            return {"domain": dom, "mentions": n, "source": src}
        return {"domain": None, "mentions": 0}

    return _cached_json(f"osint:domain:{cik}", 7 * 86400, probe)


# ---- RDAP domain intel ------------------------------------------------------

def _rdap_fetch(domain: str):
    tld = domain.rsplit(".", 1)[-1]
    urls = [RDAP_VERISIGN.format(gtld=TLD_TO_GTLD.get(tld, tld), domain=domain)]
    if tld not in TLD_TO_GTLD:
        urls.insert(0, RDAP_REDIRECT.format(domain=domain))
    with _client() as c:
        for url in urls:
            try:
                r = c.get(url)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 404:
                    return {"not_found": True}
            except Exception:
                continue
    return None


def _vcard_name(entities: list, role: str):
    for e in entities or []:
        if role in (e.get("roles") or []):
            v = e.get("vcardArray")
            if v and len(v) > 1:
                for item in v[1]:
                    if item and item[0] == "fn" and len(item) > 3:
                        return item[3]
    return None


def _event_date(events: list, action: str):
    for e in events or []:
        if e.get("eventAction") == action:
            return (e.get("eventDate") or "")[:10]
    return None


def domain_intel(sec, cik: int) -> dict:
    found = find_domain(sec, cik) or {}
    domain = found.get("domain")
    out = {"domain": domain, "mentions": found.get("mentions", 0)}
    if not domain:
        return out
    raw = _cached_json(f"osint:rdap:{domain}", 7 * 86400, lambda: _rdap_fetch(domain))
    if not raw:
        out["status"] = "unreachable"
        return out
    if raw.get("not_found"):
        out["status"] = "not_registered"
        return out
    reg = _event_date(raw.get("events"), "registration")
    exp = _event_date(raw.get("events"), "expiration")
    age_years = None
    if reg:
        try:
            age_years = round((date.today() - date.fromisoformat(reg)).days / 365.25, 1)
        except Exception:
            pass
    days_left = None
    if exp:
        try:
            days_left = (date.fromisoformat(exp) - date.today()).days
        except Exception:
            pass
    flags = []
    if age_years is not None and age_years < 1:
        flags.append("Web address registered within the last year")
    if days_left is not None and days_left < 90:
        flags.append("Web address registration expires soon")
    ns = []
    for n in raw.get("nameservers") or []:
        h = ((n.get("ldhName") if isinstance(n, dict) else n) or "")
        h = str(h).lower().rstrip(".")
        if h and h not in ns:
            ns.append(h)
    out.update({
        "status": "ok",
        "registrar": _vcard_name(raw.get("entities"), "registrar"),
        "registered": reg,
        "expires": exp,
        "age_years": age_years,
        "days_left": days_left,
        "nameservers": ns,
        "flags": flags,
    })
    return out


# ---- OFAC sanctions screen (company legal name) -----------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _ofac_fetch():
    with _client() as c:
        r = c.get(OFAC_CSV)
        r.raise_for_status()
        rows = list(csv.reader(io.StringIO(r.text)))
    names = set()
    for row in rows[1:]:
        if len(row) >= 2:
            names.add(_norm(row[1]))
    return {"names": sorted(names)}


def ofac_screen(name: str) -> dict:
    data = _cached_json("osint:ofac:sdn", 7 * 86400, _ofac_fetch)
    if not data:
        return {"status": "unavailable"}
    target = _norm(name)
    hit = target in set(data["names"]) if target else False
    return {"status": "clear" if not hit else "HIT", "list_size": len(data["names"])}


# ---- GLEIF LEI screen (legal identity + parent chain) ----------------------
# Every issuer of traded securities is required to hold an LEI. The GLEIF API
# is free and keyless (verified in research/global-registries/01): a lapsed or
# missing registration for a company that keeps selling stock is a tell.

GLEIF_API = "https://api.gleif.org/api/v1/lei-records"

LEGAL_SUFFIXES = (
    ", inc.", " inc.", " incorporated", " corp.", " corp", " corporation",
    " co.", " company", " ltd.", " ltd", " limited", " llc", " plc",
    ", inc", " n.v.", " s.a.",
)


def _name_variants(name: str) -> list:
    n = re.sub(r"\s+", " ", (name or "").strip()).lower()
    out = [n]
    for suf in LEGAL_SUFFIXES:
        if n.endswith(suf) and len(n) > len(suf) + 2:
            out.append(n[: -len(suf)].rstrip(","))
            break
    return out


def _gleif_get(c, path: str, params: dict = None):
    r = c.get(GLEIF_API + path, params=params or {})
    if r.status_code != 200:
        return None
    return r.json()


def _gleif_parent_chain(c, lei: str, hops: int = 3) -> list:
    """Every ancestor on record, not just one lineage: walk all direct-parent
    hops, then merge the ultimate parent when it is a distinct entity further
    up (a child can report several consolidating parents)."""
    chain = []
    seen = {lei}
    cur = lei
    for _ in range(hops):
        data = _gleif_get(c, f"/{cur}/direct-parent")
        node = (data or {}).get("data")
        if not node:
            break
        parent_lei = node.get("id") or ""
        nm = ((node.get("attributes", {}).get("entity") or {})
              .get("legalName") or {}).get("name")
        if not parent_lei or parent_lei in seen:
            break
        chain.append({"lei": parent_lei, "name": nm, "role": "direct"})
        seen.add(parent_lei)
        cur = parent_lei
    ult = _gleif_get(c, f"/{lei}/ultimate-parent")
    unode = (ult or {}).get("data")
    if unode:
        u_lei = unode.get("id") or ""
        if u_lei and u_lei not in seen:
            u_nm = ((unode.get("attributes", {}).get("entity") or {})
                    .get("legalName") or {}).get("name")
            chain.append({"lei": u_lei, "name": u_nm, "role": "ultimate"})
    return chain


def _gleif_children(c, lei: str, limit: int = 6) -> list:
    """The issuer's own direct subsidiaries from the official registry.
    A pile of LAPSED/RETIRED child entities is a restructuring tell."""
    data = _gleif_get(c, f"/{lei}/direct-children", {"page[size]": limit})
    out = []
    for node in (data or {}).get("data") or []:
        attrs = node.get("attributes", {})
        ent = attrs.get("entity") or {}
        out.append({
            "lei": node.get("id") or "",
            "name": ((ent.get("legalName") or {}).get("name")),
            "status": (attrs.get("registration") or {}).get("status"),
        })
    return out


def gleif_screen(name: str) -> dict:
    def probe():
        with _client() as c:
            matched = None
            for variant in _name_variants(name)[:2]:
                data = _gleif_get(
                    c, "",
                    {"filter[entity.legalName]": variant.upper(), "page[size]": 5},
                )
                rows = (data or {}).get("data") or []
                if not rows:
                    continue
                target = _norm(variant)
                exact = [
                    r for r in rows
                    if _norm(((r.get("attributes", {}).get("entity") or {})
                              .get("legalName") or {}).get("name")) == target
                ]
                matched = exact[0] if exact else rows[0]
                break
            if not matched:
                return {"status": "none_found", "checked_name": name}
            lei = matched.get("id") or ""
            attrs = matched.get("attributes", {})
            entity = attrs.get("entity") or {}
            reg = attrs.get("registration") or {}
            country = ((entity.get("legalAddress") or {}).get("country")) or None
            out = {
                "status": "ok",
                "lei": lei,
                "lei_status": reg.get("status"),
                "entity_status": entity.get("status"),
                "registered_name": ((entity.get("legalName") or {}).get("name")),
                "country": country,
                "parent_chain": _gleif_parent_chain(c, lei) if lei else [],
                "children": _gleif_children(c, lei) if lei else [],
                "children_truncated": False,
            }
            # the children endpoint is paginated; when a page comes back full
            # there are probably more, say so instead of implying completeness
            if len(out["children"]) >= 6:
                out["children_truncated"] = True
            return out

    return _cached_json(f"osint:gleif:{_norm(name)[:64]}", 30 * 86400, probe)


# ---- Wayback Machine site history ------------------------------------------
# First capture vs latest capture of the issuer's own domain. A web presence
# that only appeared recently, or an archive that went quiet while the company
# kept filing, are both shell tells. Free CDX API, no key.

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
# availability API: much faster and more reliable than CDX for point lookups.
# Closest capture to the oldest possible date IS the earliest capture.
WAYBACK_AVAIL = "https://archive.org/wayback/available"


def wayback_history(domain: str) -> dict:
    if not domain:
        return {"status": "no_domain"}
    key = f"osint:wayback:{domain}"
    # full results cache for a month; partial ones (archive flakiness)
    # cache only briefly so the next pass retries the archive
    import json as _json
    for ttl in (30 * 86400, 6 * 3600):
        hit = db.cache_get(f"{key}:{ttl}", ttl)
        if hit is not None:
            return _json.loads(hit)

    def iso(ts):
        try:
            return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
        except Exception:
            return None

    def closest(c, stamp: str):
        r = c.get(WAYBACK_AVAIL, params={"url": domain, "timestamp": stamp})
        if r.status_code != 200:
            return None
        snap = ((r.json() or {}).get("archived_snapshots") or {}).get("closest") or {}
        return snap.get("timestamp")

    first_ts = last_ts = None
    with _client(timeout=15.0) as c:
        try:
            first_ts = closest(c, "19960101")
        except Exception as exc:
            log.warning("wayback first failed %s: %s", domain, exc)
        try:
            last_ts = closest(c, date.today().strftime("%Y%m%d"))
        except Exception as exc:
            log.warning("wayback latest failed %s: %s", domain, exc)

    if not first_ts and not last_ts:
        # nothing usable this pass: do not cache, let the next one retry
        return {"status": "unarchived"}

    flags = []
    first_iso, last_iso = iso(first_ts), iso(last_ts)
    days_since = None
    if last_iso:
        try:
            days_since = (date.today() - date.fromisoformat(last_iso)).days
        except Exception:
            pass
    first_years = None
    if first_iso:
        try:
            first_years = round((date.today() - date.fromisoformat(first_iso)).days / 365.25, 1)
        except Exception:
            pass
    if first_years is not None and first_years < 3:
        flags.append("Web archive history starts recently: young web presence")
    if days_since is not None and days_since > 240:
        flags.append("Archive has had nothing new to capture for months: the website may be abandoned")
    out = {
        "status": "ok",
        "domain": domain,
        "first_seen": first_iso,
        "last_seen": last_iso,
        "years_on_archive": first_years,
        "days_since_capture": days_since,
        "flags": flags,
        "partial": bool(not first_iso or not last_iso),
    }
    ttl = 30 * 86400 if not out["partial"] else 6 * 3600
    db.cache_set(f"{key}:{ttl}", _json.dumps(out), ttl)
    return out


# ---- Certificate Transparency host record -----------------------------------
# CertSpotter's free keyless endpoint lists TLS certificates logged for the
# domain and its subdomains. The earliest certificate dates the infrastructure;
# a handful of fresh certs on an old company suggests someone stood up new
# front infrastructure quickly.

CERTSPOTTER = "https://api.certspotter.com/v1/issuances"


def cert_history(domain: str) -> dict:
    if not domain:
        return {"status": "no_domain"}

    def probe():
        params = {
            "domain": domain,
            "include_subdomains": "true",
            "expand": "dns_names",
        }
        with _client(timeout=25.0) as c:
            r = c.get(CERTSPOTTER, params=params)
        if r.status_code != 200:
            return {"status": "unavailable"}
        issuances = r.json() or []
        if not issuances:
            return {"status": "no_certs", "domain": domain}
        hosts = set()
        earliest = None
        latest = None
        for iss in issuances:
            nb = (iss.get("not_before") or "")[:10]
            if nb:
                earliest = nb if earliest is None or nb < earliest else earliest
                latest = nb if latest is None or nb > latest else latest
            for h in iss.get("dns_names") or []:
                hosts.add(h.lower().lstrip("*."))
        years = None
        if earliest:
            try:
                years = round((date.today() - date.fromisoformat(earliest)).days / 365.25, 1)
            except Exception:
                pass
        flags = []
        # CertSpotter's free feed returns only the most recent issuances, so
        # when the result set is large it is truncated and "earliest" is not
        # the true first certificate. The freshness tell only fires on small,
        # plausibly complete result sets: a false alarm here would burn trust.
        coverage_complete = len(issuances) < 40
        if coverage_complete and years is not None and years < 2:
            flags.append("First certificate on record is recent: fresh web infrastructure")
        return {
            "status": "ok",
            "domain": domain,
            "hosts": len(hosts),
            "earliest_cert": earliest,
            "latest_cert": latest,
            "cert_age_years": years,
            "issuances_seen": len(issuances),
            "coverage_truncated": not coverage_complete,
            "flags": flags,
        }

    return _cached_json(f"osint:certs:{domain}", 30 * 86400, probe)


# ---- 13F institutional holders (EDGAR full-text over info tables) ----------
# Every institutional manager with $100M+ of assets files quarterly 13F-HR
# holdings tables. EDGAR full-text search indexes those tables themselves,
# so a query for the issuer's exact filing name restricted to form 13F-HR
# is effectively an institutional-holder list from primary documents:
# free, keyless, and cited to each fund's own filing.

def institutional_holders(sec, name: str, quarters: int = 5) -> dict:
    """Funds whose latest quarterly 13F tables still list this issuer.
    EDGAR full-text search is exact-phrase, so we try the issuer's SEC name
    plus a suffix-stripped variant ("... Inc." -> "...") and merge results
    by fund CIK. Degrades honestly: variants can still miss some funds."""
    if not (name or "").strip():
        return {"status": "no_name"}
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=quarters * 92)).isoformat()

    def probe():
        insts = {}
        periods = set()
        matched_variants = []
        for variant in _holder_name_variants(name)[:3]:
            data = sec.efts_search(variant, "13F-HR", start, end, size=100)
            hits = data.get("hits", {}).get("hits", []) or []
            if hits:
                matched_variants.append(variant)
            for hit in hits:
                src = hit.get("_source", {})
                ciks = src.get("ciks") or []
                names = src.get("display_names") or []
                key = ciks[0] if ciks else (names[0] if names else None)
                if not key:
                    continue
                fdate = src.get("file_date") or ""
                period = src.get("period_ending") or ""
                if period:
                    periods.add(period)
                prev = insts.get(key)
                if prev is not None and prev["last_filed"] >= fdate:
                    continue
                clean = re.sub(r"\s*\([^)]*\)", "", names[0] or "").strip() \
                    if names else None
                insts[key] = {
                    "name": clean,
                    "cik": ciks[0] if ciks else None,
                    "last_filed": fdate,
                    "period": period,
                    "link": ("https://www.sec.gov/cgi-bin/browse-edgar"
                             "?action=getcompany&CIK=" + str(ciks[0]).zfill(10)
                             + "&type=13F-HR") if ciks else None,
                }
        rows = sorted(insts.values(), key=lambda r: r["last_filed"], reverse=True)
        return {
            "status": "ok" if rows else "none_found",
            "institutions": len(rows),
            "latest_period": max(periods) if periods else None,
            "window_start": start,
            "variants_matched": matched_variants,
            "top": rows[:5],
        }

    return _cached_json(f"osint:holders:v2:{_norm(name)[:64]}", 7 * 86400, probe)


def _holder_name_variants(name: str) -> list:
    n = re.sub(r"\s+", " ", (name or "").strip())
    out = [n]
    stripped = re.sub(r"[,\s]+(inc|corp|corporation|ltd|limited|co|company|plc)"
                      r"[\. ]*$", "", n, flags=re.I).strip().rstrip(",")
    if stripped and stripped.lower() != n.lower() and len(stripped) >= 4:
        out.append(stripped)
    return out


# ---- registrant history from archived WHOIS pages ---------------------------
# WHOIS pages captured by the public archive years ago show who registered
# the address back then. A registrant that changed hands mid-life, or one
# that only shows up recently under privacy redaction on an old company,
# are both identity tells. Free CDX + capture APIs, no key.

WHOIS_CDX_PATTERNS = [
    "whois.com/whois/{domain}",
    "www.whois.com/whois/{domain}",
    "who.is/whois/{domain}",
]

_REDACTED_WORDS = ("redacted", "privacy", "protected", "data protected",
                   "whois guard", "domains by proxy", "by proxy")


def _strip_tags(text: str) -> str:
    from html import unescape
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    return unescape(text)


def _clean_registrant(val: str):
    if not val:
        return None
    v = val.strip()[:80]
    low = v.lower()
    if any(w in low for w in _REDACTED_WORDS):
        return "REDACTED FOR PRIVACY"
    return v or None


def _parse_whois_page(html: str) -> dict:
    """Pull the registrant/registrar out of archived WHOIS pages. Handles
    both the boxed 'Registrant Contact' layout (who.is, whois.com) and
    classic raw-WHOIS 'Registrant Organization:' lines."""
    text = _strip_tags(html or "")[:400_000]
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    joined = "\n".join(lines)
    out = {"registrant": None, "registrar": None}
    # boxed layout: the registrant block lists Name:/Organization: rows
    idx = joined.lower().find("registrant contact")
    if idx >= 0:
        block = joined[idx:idx + 900]
        mo = re.search(r"organization\s*:\s*\n([^\n]{2,80})", block, re.I)
        mn = re.search(r"^name\s*:\s*\n([^\n]{2,80})", block, re.I | re.M)
        out["registrant"] = _clean_registrant(
            (mo.group(1) if mo else None) or (mn.group(1) if mn else None))
        if not out["registrant"]:
            # some captures put the value on the same line as the label
            ms = re.search(r"organization\s*:\s*([^\n]{2,80})", block, re.I)
            out["registrant"] = _clean_registrant(ms.group(1) if ms else None)
    # classic raw-WHOIS layouts
    if not out["registrant"]:
        m = (re.search(r"registrant organization\s*:?\s*\n?\s*([^\n]{2,80})", joined, re.I)
             or re.search(r"registrant name\s*:?\s*\n?\s*([^\n]{2,80})", joined, re.I)
             or re.search(r"^registrant\s*:?\s*([^\n]{2,80})$", joined, re.I | re.M))
        out["registrant"] = _clean_registrant(m.group(1) if m else None)
    mr = re.search(r"registrar(?:\s*of record)?\s*:?\s*\n([^\n]{2,60})", joined, re.I)
    if not mr:
        mr = re.search(r"registrar\s*:\s*([^\n]{2,60})", joined, re.I)
    if mr:
        cand = mr.group(1).strip()
        if cand and not cand.lower().startswith(("url", "abuse", "contact",
                                                 "referral", "iana")):
            out["registrar"] = cand[:60]
    return out


def whois_history(domain: str) -> dict:
    if not domain:
        return {"status": "no_domain"}
    key = f"osint:whoishist:{domain}"
    import json as _json
    # full results cache for a month; partial ones or empty archive reads
    # cache only briefly so the next pass retries
    for ttl in (30 * 86400, 6 * 3600):
        hit = db.cache_get(f"{key}:{ttl}", ttl)
        if hit is not None:
            return _json.loads(hit)

    def probe():
        captures = []
        with _client(timeout=25.0) as c:
            for pat in WHOIS_CDX_PATTERNS:
                try:
                    r = c.get(WAYBACK_CDX, params={
                        "url": pat.format(domain=domain),
                        "output": "json",
                        "fl": "timestamp,original,statuscode",
                        "filter": "statuscode:200",
                    })
                    rows = r.json() or [] if r.status_code == 200 else []
                    for row in rows[1:]:
                        if len(row) >= 2:
                            captures.append((row[0], row[1]))
                except Exception as exc:
                    log.warning("whois cdx failed %s (%s): %s", domain, pat, exc)
                if len(captures) >= 3:
                    break
            if not captures:
                return {"status": "no_archived_whois", "domain": domain}
            captures.sort()
            # one capture per distinct date, oldest to newest, sampled so the
            # earliest, a midpoint and the LATEST snapshot are all represented
            uniq_ts = []
            for cap in captures:
                if not uniq_ts or uniq_ts[-1][0] != cap[0]:
                    uniq_ts.append(cap)
            if len(uniq_ts) >= 3:
                picks = [uniq_ts[0], uniq_ts[len(uniq_ts) // 2], uniq_ts[-1]]
            else:
                picks = uniq_ts[:]
            snapshots = []
            for ts, orig in picks:
                html = ""
                try:
                    r = c.get(f"https://web.archive.org/web/{ts}id_/{orig}")
                    if r.status_code == 200:
                        html = r.text
                except Exception as exc:
                    log.warning("whois snapshot fetch failed %s@%s: %s", orig, ts, exc)
                rec = _parse_whois_page(html)
                snapshots.append({
                    "captured": f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}",
                    "registrant": rec.get("registrant"),
                    "registrar": rec.get("registrar"),
                })
            registrants = {s["registrant"].lower() for s in snapshots
                           if s.get("registrant")
                           and s["registrant"] != "REDACTED FOR PRIVACY"}
            redacted_later = (
                len(snapshots) > 1
                and snapshots[0].get("registrant")
                and snapshots[0]["registrant"] != "REDACTED FOR PRIVACY"
                and all(s.get("registrant") == "REDACTED FOR PRIVACY"
                        for s in snapshots[1:])
            )
            flags = []
            if len(registrants) > 1:
                flags.append(
                    "Archived WHOIS pages show the registered owner changed hands over time"
                )
            if redacted_later:
                flags.append(
                    "Registrant was public years ago and is now behind privacy redaction"
                )
            return {
                "status": "ok",
                "domain": domain,
                "archived_captures": len(captures),
                "snapshots": snapshots,
                "distinct_registrants": len(registrants),
                "flags": flags,
                "partial": bool(len(snapshots) < len(picks)),
            }

    def run():
        out = probe()
        if isinstance(out, dict):
            partial = (out.get("partial")
                       or out.get("status") == "no_archived_whois")
            ttl = 6 * 3600 if partial else 30 * 86400
            db.cache_set(f"{key}:{ttl}", _json.dumps(out), ttl)
        return out

    # short-lived wrapper so concurrent page loads share one archive pass
    return _cached_json(f"osint:whoishist:inflight:{domain}", 600, run)


# ---- nameserver + mail-record read (DNS over HTTPS, free, no key) ----------
# Where the address is hosted and whether it can receive mail at all.
# A web address parked on a for-sale nameserver while filings claim active
# operations is a hard shell tell; no mail records means nobody runs
# operations through this property either.

DOH_ENDPOINTS = (
    ("https://dns.google/resolve", {}),
    ("https://cloudflare-dns.com/dns-query", {"Accept": "application/dns-json"}),
)

PARKING_NS_MARKERS = (
    "sedoparking", "bodis", "afternic", "parkingcrew", "above.com",
    "undeveloped", "dan.com",
)

MX_PROVIDERS = (
    (("googlemail", ".google.com", "aspmx", "smtp.google"), "Google Workspace"),
    (("protection.outlook", ".outlook."), "Microsoft 365"),
    (("zoho",), "Zoho Mail"),
    (("protonmail", "proton.me"), "Proton"),
    (("pphosted", "proofpoint"), "Proofpoint"),
    (("mimecast",), "Mimecast"),
    (("secureserver",), "GoDaddy"),
    (("mailgun", "sendgrid", "amazonses", "emailsrvr"), "a bulk email service"),
)


def _doh_mx(domain: str):
    for url, headers in DOH_ENDPOINTS:
        try:
            with _client(timeout=12.0) as c:
                r = c.get(url, params={"name": domain, "type": "MX"},
                          headers=headers)
            if r.status_code != 200:
                continue
            answers = ((r.json() or {}).get("Answer")) or []
            mx = []
            for a in answers:
                if a.get("type") != 15:
                    continue
                parts = str(a.get("data") or "").split()
                host = (parts[1] if len(parts) > 1 else parts[-1]) \
                    .strip().rstrip(".").lower()
                if host and host not in mx:
                    mx.append(host)
            # an empty Answer section IS a valid answer: the domain has no mail
            return mx
        except Exception:
            continue
    return None


def dns_intel(domain: str, nameservers=None) -> dict:
    if not domain:
        return {"status": "no_domain"}
    ns = sorted({str(n).lower().rstrip(".") for n in (nameservers or [])})

    def probe():
        mx = _doh_mx(domain)
        if mx is None:
            return {"status": "dns_unavailable", "domain": domain,
                    "nameservers": ns}
        flags = []
        joined_ns = " ".join(ns)
        if any(p in joined_ns for p in PARKING_NS_MARKERS):
            flags.append(
                "The web address sits on a parking/for-sale nameserver: "
                "nobody appears to be operating this property"
            )
        provider = None
        if mx:
            blob = " ".join(mx)
            for keys, label in MX_PROVIDERS:
                if any(k in blob for k in keys):
                    provider = label
                    break
        own_mail = bool(mx) and all(d == domain or d.endswith("." + domain)
                                    for d in mx)
        return {
            "status": "ok",
            "domain": domain,
            "nameservers": ns,
            "mx": mx,
            "mx_provider": provider,
            "own_mail_domain": own_mail,
            "no_mail": not mx,
            "flags": flags,
        }

    return _cached_json(f"osint:dns:{domain}", 7 * 86400, probe)

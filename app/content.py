"""Editorial content: the guide library that sits alongside the tool.

The reports explain one company. The guides explain the mechanics behind the
numbers (dilution, going concern, 8-K items, reverse splits) so a reader can
judge a filing without trusting a score. Each guide has a template at
app/templates/guides/<slug>.html; this module only holds the metadata used by
the index, the related-links list, the sitemap and the RSS feed.
"""

GUIDES = [
    {
        "slug": "spot-share-dilution",
        "title": "How to Spot Share Dilution Before It Wrecks Your Position",
        "description": (
            "Dilution is the slow leak that sinks most microcaps. Here is "
            "where the numbers live in the filings and how to check them in "
            "two minutes."
        ),
        "minutes": 8,
        "published": "2026-10-10",
        "updated": "2026-10-10",
        "tags": ["dilution", "offerings", "basics"],
    },
    {
        "slug": "going-concern-warning",
        "title": "Going Concern Warnings: What They Actually Mean for Holders",
        "description": (
            "A going concern paragraph is not a bankruptcy notice. It is a "
            "specific accounting warning. Here is what triggers it and what "
            "usually follows."
        ),
        "minutes": 7,
        "published": "2026-10-10",
        "updated": "2026-10-10",
        "tags": ["going concern", "accounting", "basics"],
    },
    {
        "slug": "how-to-read-8k",
        "title": "How to Read an 8-K in Five Minutes",
        "description": (
            "An 8-K is the company telling you something happened. Most items "
            "are routine. A handful change the story. Learn to tell them "
            "apart fast."
        ),
        "minutes": 6,
        "published": "2026-10-10",
        "updated": "2026-10-10",
        "tags": ["8-K", "filings", "basics"],
    },
    {
        "slug": "reverse-splits",
        "title": "Reverse Splits: Why Companies Do Them and What They Do to You",
        "description": (
            "A reverse split does not change what your position is worth, but "
            "it is rarely good news. Here is the mechanics and the pattern "
            "that usually follows."
        ),
        "minutes": 7,
        "published": "2026-10-10",
        "updated": "2026-10-10",
        "tags": ["reverse split", "listing", "basics"],
    },
    {
        "slug": "shelf-offerings-atm",
        "title": "Shelf Offerings and ATMs: How Companies Print New Shares",
        "description": (
            "Shelf registrations and at-the-market programs let a company "
            "sell shares into the open market on demand. Understand the "
            "machinery and the paperwork you can watch."
        ),
        "minutes": 9,
        "published": "2026-10-10",
        "updated": "2026-10-10",
        "tags": ["offerings", "ATM", "S-3", "dilution"],
    },
    {
        "slug": "cash-runway",
        "title": "Cash Runway: How Many Months Does the Company Have Left?",
        "description": (
            "Runway is cash on hand divided by burn. Two numbers, both in the "
            "filing, and both easy to misread. Here is how to get it right."
        ),
        "minutes": 7,
        "published": "2026-10-10",
        "updated": "2026-10-10",
        "tags": ["cash", "runway", "basics"],
    },
    {
        "slug": "insider-selling-form-4",
        "title": "Insider Form 4s: When Selling Matters and When It Does Not",
        "description": (
            "Every Form 4 is public within two business days. Most are "
            "routine grants and tax withholding. A few are worth your "
            "attention. Here is how to read the codes."
        ),
        "minutes": 8,
        "published": "2026-10-10",
        "updated": "2026-10-10",
        "tags": ["insiders", "Form 4", "basics"],
    },
    {
        "slug": "screen-a-microcap",
        "title": "How to Screen a Microcap in Ten Minutes",
        "description": (
            "A repeatable routine you can run on any ticker: cash, share "
            "count, recent 8-Ks, offerings, insiders. The same order every "
            "time, so your process cannot be talked out of it."
        ),
        "minutes": 9,
        "published": "2026-10-10",
        "updated": "2026-10-10",
        "tags": ["process", "screening", "basics"],
    },
]


def guide_meta(slug: str):
    for g in GUIDES:
        if g["slug"] == slug:
            return g
    return None


def related(slug: str, limit: int = 3) -> list:
    """Content-based neighbours: share the most tags with this guide, then pad
    with the most recent others so a guide never dead-ends."""
    me = guide_meta(slug)
    if not me:
        return []
    my_tags = set(me["tags"])
    scored = []
    for g in GUIDES:
        if g["slug"] == slug:
            continue
        overlap = len(my_tags & set(g["tags"]))
        scored.append((overlap, g))
    scored.sort(key=lambda kv: (-kv[0], kv[1]["published"]), reverse=False)
    ranked = [g for overlap, g in scored if overlap > 0]
    rest = [g for overlap, g in scored if overlap == 0]
    return (ranked + rest)[:limit]

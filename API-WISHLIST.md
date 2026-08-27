# API Wishlist, ranked by value given

Rule of thumb: everything on the ticker page already works with zero keys
(SEC filings + RDAP + sanctions list). Each item below is what a key buys on
top, ranked by how much visible product value it unlocks per dollar/effort.

## DONE: Massive.com (free tier, 5 calls/min) - integrated with gating
`app/massive_client.py` + `/ws/market` websocket. Slots filled live: last
close, CONFIRMED split records (upgrades the dilution chart's collapse
markers to official events), public float, FINRA short interest with days to
cover. Token-bucket limiter (5/min), cache-first (price 24h, rest 7d), and
the websocket tells the user exactly what is loading and how long the quota
wait is. The rest of the report never blocks on it.

## DONE: Telegram notifications (free Bot API, no key vendor) - replaces email
`app/telegram_client.py` + `app/telegram_bot.py`. Subscriptions are deep
links: report pages carry `t.me/{bot}?start=watch_TICKER`, the user presses
START, the bot registers the chat (poll CLI locally, webhook route in
production). The alert pass pushes cited filings to watching chats; without
TELEGRAM_BOT_TOKEN everything logs WOULD-NOTIFY. Free forever at this scale,
no deliverability problem, and it is where this audience already is.

## Tier 1: unlocks the actual product loop

### 1. TELEGRAM_BOT_TOKEN + TELEGRAM_BOT_USERNAME (from @BotFather, free)
Value given: the alert promise becomes real end to end. Today "WATCH <TICKER> ON
TELEGRAM" shows a pending-config note and the pass logs WOULD-NOTIFY. With a
token the bot answers /watch commands and the cron pass sends "<TICKER> just filed
something scary" with citations. This is now the single highest-leverage
step: it converts the funnel into the retention engine the audience asked
for in comments ("tell me when X files").
Where it plugs in: product/.env (already wired, token-gated).

## Tier 2: makes the report feel alive with market context

### 2. TWELVEDATA_API_KEY (primary price data) - free tier ~800 credits/day
Value given: last close + 52-week range on every page, and the killer line:
"last close $0.42 vs cash-per-share floor $0.05" (price vs fair-value gap).
Non-realtime is fine for this audience; 24h cache keeps usage tiny.
Where it plugs in: `app/price_client.py` (already written, key-gated).

### 3. FINNHUB_API_KEY (price fallback) - free tier 60 calls/min
Value given: same as above plus redundancy when TwelveData credits run out.
Already wired as automatic fallback in `app/price_client.py`.

## Tier 3: new signals that would add real OSINT depth

### 4. SEC-only (still free, no key): reverse-split calendar via EDGAR
Not an API key: a cron that diffs share-count series and 8-K item 3.03/3.05
filings would let us mark confirmed reverse splits (not just "collapse in the
chart"). Zero cost, needs the Phase-2 scheduler.

### 5. Polygon.io (~$29/mo starter) - splits/dividends calendar + better history
Value given: confirmed split events with exact ratios and dates, corporate
action calendar ("<TICKER> announced a 1-for-20 reverse split, effective X").
Only worth it after Tier 1+2 are live and people are returning.

### 6. Brandfetch / brand.dev (free tier) - company logo + brand colors
Value given: logo in the case-file header makes shared links look
trustworthy in Discord/X posts. Cosmetic but improves share CTR.

## Explicitly NOT wanted
- yfinance: blocks datacenter IPs (verified in phase1-spec).
- Alpha Vantage free: 25 req/day is useless for a public funnel.
- Any realtime quote feed: the product is filings-first, not day-trading.

## Current degraded modes (no keys needed to ship)
- Price strip: hidden entirely (no fake placeholder).
- Telegram CTA: shows an honest pending-config note; alert pass logs
  WOULD-NOTIFY per chat.
- Everything else: fully functional on free sources.

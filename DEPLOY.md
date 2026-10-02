# decoded.report deployment

## Coolify

Create a Docker Compose resource from this repository and set the public
service to `web` on container port `8000`. Coolify handles the public HTTPS
certificate and reverse proxy.

The Compose file runs a single service:

- `web`: FastAPI application on port 8000

It owns `./data`, which keeps the SQLite database and image cache across
container restarts and redeploys. Do not use an ephemeral volume. SQLite runs
in WAL mode with a 30s busy timeout, so the app tolerates overlapping writes
without "database is locked" errors.

## Required environment variables

Set these in Coolify. Never commit their values:

```text
SEC_USER_AGENT=decoded.report research contact@decoded.report
BASE_URL=https://decoded.report
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<long-random-password>
MASSIVE_API_KEY=<key>
```

`TWELVEDATA_API_KEY` and `FINNHUB_API_KEY` are optional. The report remains
useful without them.

## Ads

Optional. The site is live with or without an ad network, since every slot
renders nothing until configured. For Google AdSense, set the client ID and a
slot ID per placement:

```text
ADSENSE_CLIENT=ca-pub-<publisher-id>
AD_SLOT_TOP=<slot>
AD_SLOT_MID=<slot>
AD_SLOT_FOOTER=<slot>
AD_SLOT_SIDEBAR=<slot>
AD_SLOT_ANCHOR=<slot>
```

For any other network (Ezoic, Mediavine, a direct sponsor), override a single
placement with raw markup instead:

```text
AD_HTML_TOP=<network snippet>
```

`/ads.txt` is generated from `ADSENSE_CLIENT`, so the authorized-seller record
never drifts from the live units.

## First deploy checks

1. Open `https://decoded.report/healthz` and confirm `ok: true` and `ads`
   reflects your configuration.
2. Open `/`, one company report path (`/{TICKER}`), `/watchlist?t=TICKER1,TICKER2`,
   and `/bag-vs`.
3. Confirm the market-data websocket fills slots after the page loads.
4. Open `/admin/subs` and confirm the browser prompts for Basic Auth.
5. Confirm `/privacy`, `/terms`, and `/ads.txt` are reachable.
6. Visit a report and a Bag Check, then verify Recent Events in the admin console.
7. With ad slots configured, confirm units render below the hero, mid-report,
   in the footer, and (on wide screens) in the rail.

The local SQLite database is intentionally not included in the image or Git.

# decoded.report deployment

## Coolify

Create a Docker Compose resource from this repository and set the public
service to `web` on container port `8000`. Coolify handles the public HTTPS
certificate and reverse proxy. Do not expose the `telegram-bot` or `alerts`
services publicly.

The Compose file runs three services:

- `web`: FastAPI application on port 8000
- `telegram-bot`: Telegram long poll worker
- `alerts`: hourly filing alert worker

All three services share `./data`, which keeps the SQLite database and image
cache across container restarts and redeploys. Do not use an ephemeral volume.

## Required environment variables

Set these in Coolify. Never commit their values:

```text
SEC_USER_AGENT=decoded.report research contact@decoded.report
BASE_URL=https://decoded.report
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<long-random-password>
MASSIVE_API_KEY=<key>
TELEGRAM_BOT_TOKEN=<BotFather-token>
TELEGRAM_BOT_USERNAME=<bot-username-without-at-sign>
TG_WEBHOOK_SECRET=<long-random-secret>
```

`TWELVEDATA_API_KEY` and `FINNHUB_API_KEY` are optional. The report remains
useful without them. The Telegram workers intentionally restart until a bot
token is supplied, so add the token before enabling the resource in
production.

## Telegram mode

The default Compose setup uses long polling. No webhook setup is needed.
After deployment, verify the bot with `/start`, `/watch <any ticker you follow>`, `/list`, and
`/unwatch ALL`.

If webhook mode is preferred, stop the `telegram-bot` service and register:

```text
https://decoded.report/tg/webhook/<TG_WEBHOOK_SECRET>
```

The route validates both the secret path and Telegram's secret-token header.

## First deploy checks

1. Open `https://decoded.report/healthz` and confirm `ok: true`.
2. Open `/healthz`, `/`, one company report path (`/{TICKER}`),
   `/watchlist?t=TICKER1,TICKER2`, and `/bag-vs`.
3. Confirm the market-data websocket fills slots after the page loads.
4. Open `/admin/subs` and confirm the browser prompts for Basic Auth.
5. Confirm `/privacy` and `/terms` are reachable from the footer.
6. Visit a report and a Bag Check, then verify Recent Events in the admin console.
7. Subscribe through Telegram and confirm the subscription appears in admin.
8. Confirm the first alert pass seeds state without sending a notification.

The local SQLite database is intentionally not included in the image or Git.

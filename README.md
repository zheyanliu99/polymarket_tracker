# Polymarket Weather Tracker

Small local tracker for ColdMath's public Polymarket weather activity.

Tracked wallet:

```text
0x594edb9112f526fa6a80b8f858a6379c8a2c1c11
```

## Quick Start

```bash
python3 coldmath_tracker.py recent --trades-only --limit 20
python3 coldmath_tracker.py positions --limit 50 --open-only
python3 coldmath_tracker.py watch --trades-only --interval 60
```

Export recent trades to CSV:

```bash
python3 coldmath_tracker.py recent --trades-only --limit 100 --csv coldmath_weather_trades.csv
```

## Low-Latency Alerts

`coldmath_live_alert.py` watches Polygon `OrderFilled` logs for ColdMath buys and writes latency audit logs under `data/live/`.
For each buy, `price` and `coldmath_paid_price` are computed from the on-chain fill as USDC paid divided by outcome tokens received.
It also polls the Polymarket Data API as a continuous safety net so current buys are still logged if a provider misses a WSS log.

Required:

```bash
export POLYGON_WSS_URL='wss://polygon-mainnet.g.alchemy.com/v2/your-api-key'
```

Email alerts use SMTP:

```bash
export SMTP_HOST='smtp.example.com'
export SMTP_PORT='587'
export SMTP_USERNAME='your-user'
export SMTP_PASSWORD='your-password'
export SMTP_FROM='you@example.com'
export ALERT_EMAIL_TO='you@example.com'
```

Run:

```bash
python3 coldmath_live_alert.py
```

Test notifications without connecting to Polygon:

```bash
python3 coldmath_live_alert.py --test-notification
```

Runtime files:

- `data/live/coldmath_buy_events.jsonl`
- `data/live/coldmath_latency.csv`
- `data/live/coldmath_daemon.log`
- `data/live/coldmath_event_state.json`

Generated datasets, live logs, and `.env` files are intentionally ignored by git. Copy `.env.example` to `.env` for local credentials if you prefer file-based setup.

## Commands

- `recent`: Shows recent weather-related activity from the wallet.
- `recent --trades-only`: Shows only buys and sells, excluding merge/redeem mechanics.
- `positions`: Shows current weather-related positions reported by Polymarket.
- `positions --open-only`: Hides positions that appear resolved or redeemable.
- `watch`: Polls the feed and prints newly seen activity.

## Notes

The public feed can include `MERGE` and `REDEEM` events after a market resolves. These are not fresh bets. Use `--trades-only` when you want signal about what ColdMath is actively buying or selling.

Weather markets can look fair but still be hard to follow profitably. The edge usually comes from resolution-rule details, source timing, fast forecast updates, and avoiding bad fills after the trader has already moved the price.

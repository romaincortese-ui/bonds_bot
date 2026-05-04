# Bonds Bot

Sovereign bonds and rates trading bot scaffold for an OANDA sub-account. The first build is paper-first and DV01-native: it can deploy to Railway without secrets, run deterministic 30-day validation backtests, and switch to OANDA practice/live market data once the operator adds OANDA and Telegram credentials.

## What It Trades

The initial universe is conservative and focused on liquid government-rate exposures:

- US front end and curve: `US2Y`, `US5Y`, `US10Y`, `US30Y`
- Europe and UK benchmarks: `DE10Y`, `UK10Y`

The bot models risk in DV01, country, and tenor buckets. It does not size by naive notional. The production-ready first strategy is duration trend; carry/roll-down and curve-spread metadata are included as conservative paper/research sleeves.

## Quick Start

```bash
python -m bondsbot.backtest.run_backtest
RUN_ONCE=true python bot.py
```

The runtime stays in paper mode unless `PAPER_TRADE=false` and valid OANDA credentials are present.

## Railway Variables

Copy `.env.example` into Railway variables. The operator only needs to fill:

- `OANDA_ACCOUNT_ID`
- `OANDA_API_TOKEN`
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional later input: `FRED_API_KEY` for live macro/yield-curve enrichment.

OANDA rates/bond CFD coverage is account and region dependent. The runtime discovers account instruments, maps available symbols to canonical products, and falls back to fixture research data for symbols that are not available. If fewer than `MIN_OANDA_RATES_PRODUCTS` are mapped, it keeps the deployment in research/paper mode.

## Deployment

`railway.toml` mirrors the current bot pattern:

```toml
[build]
builder = "NIXPACKS"

[deploy]
startCommand = "python bot.py"
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 5
```

Mount a Railway volume at `/data` so runtime state survives restarts.

## Backtest Standard

The default 30-day validation backtest uses built-in deterministic fixture data because broker credentials are not available during CI/deployment setup. The report records `data_provider=fixture`. Once OANDA credentials are configured, broker candles can be used by the runtime for mapped instruments.

Live go/no-go should require 30-60 calendar days of paper trading, broker spread and financing review, no DV01 cap violations, and manual approval before `PAPER_TRADE=false`.

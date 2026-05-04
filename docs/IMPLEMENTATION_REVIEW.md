# Bonds Bot Implementation Review

This implementation follows the national bonds guide as a Sprint 1 rates scaffold:

- DV01-native sizing and portfolio/country/tenor caps are implemented from day one.
- Runtime is paper-first and refuses live mode without OANDA credentials.
- OANDA product coverage is discovered at startup because account/region coverage is variable.
- If OANDA has insufficient mapped bond/rates products, the bot stays in research/paper mode.
- Fixture data is deterministic and used only for no-secret validation and CI-style backtests.
- Duration trend is the first production-quality strategy. Carry/roll-down is conservative; curve, macro event, and auction systems are scaffolded for later richer data.

The current 30-day backtest is a fixture validation, not a live-capital go/no-go. Before disabling paper mode, the operator should run 30-60 calendar days of OANDA practice scans, review spreads and financing, and confirm broker instruments are suitable.

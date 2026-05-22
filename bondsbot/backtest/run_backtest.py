from __future__ import annotations

from bondsbot.backtest.data import FixtureRatesDataProvider, OandaRatesBacktestDataProvider
from bondsbot.backtest.engine import BacktestEngine, write_report
from bondsbot.config import BondsConfig


def main() -> int:
    config = BondsConfig.from_env()
    days = max(90, config.backtest_days + 60)
    if config.backtest_data_provider == "oanda":
        provider = OandaRatesBacktestDataProvider(config, days=days)
        config.backtest_data_provider = "oanda_or_fixture"
    else:
        provider = FixtureRatesDataProvider(days=days)
    result = BacktestEngine(config, provider).run()
    write_report(result, config.backtest_output_dir)
    by_country = ", ".join(f"{key}:{value:.2f}" for key, value in sorted(result.by_country.items()))
    by_strategy = ", ".join(f"{key}:{value:.2f}" for key, value in sorted(result.by_strategy.items()))
    print(f"data_provider={result.data_provider}")
    if isinstance(provider, OandaRatesBacktestDataProvider):
        counts = provider.provider_counts
        print(f"provider_counts=oanda:{counts['oanda']} fixture:{counts['fixture']}")
        if provider.failures:
            print("provider_failures=" + "; ".join(provider.failures[:5]))
    print(f"trades={result.total_trades} wins={result.wins} losses={result.losses} win_rate={result.win_rate:.2%}")
    print(f"pnl={result.total_pnl:.2f} return={result.return_pct:.2%} pf={result.profit_factor:.2f} max_dd={result.max_drawdown_pct:.2%}")
    print(f"by_country={by_country}")
    print(f"by_strategy={by_strategy}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

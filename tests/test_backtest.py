from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bondsbot.backtest import run_backtest
from bondsbot.backtest.data import FixtureRatesDataProvider
from bondsbot.backtest.engine import BacktestEngine
from bondsbot.config import BondsConfig


class BacktestTests(unittest.TestCase):
    def test_fixture_backtest_is_positive(self) -> None:
        config = BondsConfig.from_env()
        provider = FixtureRatesDataProvider(days=max(90, config.backtest_days + 60))
        result = BacktestEngine(config, provider).run()
        self.assertGreater(result.total_trades, 0)
        self.assertGreater(result.total_pnl, 0.0)
        self.assertGreater(result.profit_factor, 1.0)
        self.assertTrue(all(value > 0.0 for value in result.by_country.values()))

    def test_profit_lock_backtest_improves_fixture_pnl(self) -> None:
        env = dict(os.environ)
        env["BACKTEST_DAYS"] = "30"
        provider = FixtureRatesDataProvider(days=90)
        with patch.dict(os.environ, {**env, "PROFIT_LOCK_ENABLED": "false"}, clear=True):
            baseline = BacktestEngine(BondsConfig.from_env(), provider).run()
        with patch.dict(os.environ, {**env, "PROFIT_LOCK_ENABLED": "true"}, clear=True):
            candidate = BacktestEngine(BondsConfig.from_env(), provider).run()
        self.assertGreater(candidate.total_pnl, baseline.total_pnl)
        self.assertTrue(any(trade.exit_reason == "peak_pullback_profit_lock" for trade in candidate.trades))

    def test_backtest_command_succeeds_when_metrics_are_negative(self) -> None:
        result = SimpleNamespace(
            data_provider="fixture",
            total_trades=3,
            wins=1,
            losses=2,
            win_rate=1 / 3,
            total_pnl=-8.0,
            return_pct=-0.0008,
            profit_factor=0.8,
            max_drawdown_pct=-0.002,
            by_country={"US": -8.0},
            by_strategy={"DURATION_TREND": -8.0},
        )

        class FakeBacktestEngine:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def run(self):
                return result

        with patch.object(run_backtest, "BacktestEngine", FakeBacktestEngine), patch.object(run_backtest, "write_report"):
            self.assertEqual(run_backtest.main(), 0)


if __name__ == "__main__":
    unittest.main()
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
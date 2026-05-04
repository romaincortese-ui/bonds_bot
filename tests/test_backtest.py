from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
from __future__ import annotations

import unittest

from bondsbot.backtest.data import FixtureRatesDataProvider
from bondsbot.config import BondsConfig
from bondsbot.strategies import generate_signal


class StrategyTests(unittest.TestCase):
    def test_duration_trend_generates_rates_signal(self) -> None:
        config = BondsConfig.from_env()
        provider = FixtureRatesDataProvider(days=100)
        signal = generate_signal("US10Y", provider.history("US10Y"), config)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.strategy, "DURATION_TREND")
        self.assertEqual(signal.country, "US")
        self.assertEqual(signal.tenor_bucket, "10Y")
        self.assertIn(signal.side, {"LONG", "SHORT"})
        self.assertNotEqual(signal.stop_price, signal.take_profit_price)


if __name__ == "__main__":
    unittest.main()

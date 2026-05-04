from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from bondsbot.config import BondsConfig, DEFAULT_UNIVERSE
from bondsbot.risk import max_portfolio_dv01


class ConfigTests(unittest.TestCase):
    def test_defaults_are_paper_safe(self) -> None:
        env = {key: value for key, value in os.environ.items() if key not in {"OANDA_ACCOUNT_ID", "OANDA_API_TOKEN", "PAPER_TRADE"}}
        with patch.dict(os.environ, env, clear=True):
            config = BondsConfig.from_env()
        self.assertTrue(config.paper_trade)
        self.assertFalse(config.has_oanda_credentials)
        self.assertEqual(config.universe, DEFAULT_UNIVERSE)
        self.assertEqual(config.scan_interval_seconds, 3600)

    def test_list_variables_accept_commas_or_whitespace(self) -> None:
        env = {"BONDS_UNIVERSE": "US2Y US5Y,US10Y", "BONDS_STRATEGIES": "DURATION_TREND CARRY_ROLLDOWN"}
        with patch.dict(os.environ, env, clear=True):
            config = BondsConfig.from_env()
        self.assertEqual(config.universe, ("US2Y", "US5Y", "US10Y"))
        self.assertEqual(config.strategies, ("DURATION_TREND", "CARRY_ROLLDOWN"))

    def test_dv01_cap_uses_10bp_nav_loss(self) -> None:
        config = BondsConfig.from_env()
        self.assertAlmostEqual(max_portfolio_dv01(10000.0, config), 2.5)


if __name__ == "__main__":
    unittest.main()

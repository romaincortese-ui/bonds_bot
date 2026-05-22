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
        self.assertEqual(config.scan_interval_seconds, 300)
        self.assertEqual(config.heartbeat_seconds, 21600)
        self.assertEqual(config.min_live_order_units, 1)
        self.assertGreaterEqual(config.max_portfolio_dv01_nav_10bp, 0.0025)
        self.assertLessEqual(config.max_portfolio_dv01_nav_10bp, 0.007)
        self.assertGreaterEqual(config.max_country_dv01_nav_10bp, 0.0015)
        self.assertLessEqual(config.max_country_dv01_nav_10bp, 0.0045)
        self.assertGreaterEqual(config.max_tenor_dv01_nav_10bp, 0.001)
        self.assertLessEqual(config.max_tenor_dv01_nav_10bp, 0.003)
        self.assertGreaterEqual(config.min_live_unit_score, 72.0)
        self.assertLessEqual(config.min_live_unit_score, 88.0)
        self.assertGreaterEqual(config.profit_lock_trigger_pct, 8.0)
        self.assertLessEqual(config.profit_lock_trigger_pct, 20.0)
        self.assertGreaterEqual(config.profit_lock_pullback_pct, 1.0)
        self.assertLessEqual(config.profit_lock_pullback_pct, 3.0)
        self.assertLess(config.profit_lock_pullback_pct, config.profit_lock_trigger_pct)
        self.assertEqual(config.max_min_unit_nav_10bp, 0.0250)

    def test_list_variables_accept_commas_or_whitespace(self) -> None:
        env = {"BONDS_UNIVERSE": "US2Y US5Y,US10Y", "BONDS_STRATEGIES": "DURATION_TREND CARRY_ROLLDOWN"}
        with patch.dict(os.environ, env, clear=True):
            config = BondsConfig.from_env()
        self.assertEqual(config.universe, ("US2Y", "US5Y", "US10Y"))
        self.assertEqual(config.strategies, ("DURATION_TREND", "CARRY_ROLLDOWN"))

    def test_heartbeat_env_is_clamped_to_six_hours(self) -> None:
        with patch.dict(os.environ, {"BONDS_HEARTBEAT_SECONDS": "3600"}, clear=True):
            config = BondsConfig.from_env()
        self.assertEqual(config.heartbeat_seconds, 21600)
        with patch.dict(os.environ, {"HEARTBEAT_SECONDS": "3600"}, clear=True):
            config = BondsConfig.from_env()
        self.assertEqual(config.heartbeat_seconds, 21600)

    def test_dv01_cap_uses_10bp_nav_loss(self) -> None:
        config = BondsConfig.from_env()
        self.assertAlmostEqual(max_portfolio_dv01(10000.0, config), 10000.0 * config.max_portfolio_dv01_nav_10bp / 10.0)


if __name__ == "__main__":
    unittest.main()

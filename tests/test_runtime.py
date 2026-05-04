from __future__ import annotations

import unittest

from bondsbot.config import BondsConfig
from bondsbot.runtime import _format_scan_message


class RuntimeMessageTests(unittest.TestCase):
    def test_scan_message_is_clear_and_visual(self) -> None:
        config = BondsConfig.from_env()
        snapshot = {
            "time": "2026-05-04T17:15:00+00:00",
            "research_only": False,
            "data_provider_counts": {"oanda": 4, "fixture": 2},
            "mapped_oanda_instruments": {"US10Y": "USB10Y_USD"},
            "tradeable_instrument_count": 123,
            "risk_caps": {"portfolio_dv01": 2.5, "country_dv01": 1.5, "tenor_dv01": 1.0},
            "failures": ["UK10Y:OANDA request failed"],
            "top_signals": [
                {
                    "canonical": "US10Y",
                    "side": "SHORT",
                    "score": 88.42,
                    "strategy": "DURATION_TREND",
                    "country": "US",
                    "tenor": "10Y",
                    "data_provider": "oanda",
                    "oanda_instrument": "USB10Y_USD",
                }
            ],
        }

        message = _format_scan_message(snapshot, config)

        self.assertIn("🏦 Bonds Bot", message)
        self.assertIn("📊 Data: OANDA 4/6", message)
        self.assertIn("🛡️ DV01 caps", message)
        self.assertIn("🔴 SHORT US10Y", message)
        self.assertIn("📡 OANDA | USB10Y_USD", message)


if __name__ == "__main__":
    unittest.main()
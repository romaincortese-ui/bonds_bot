from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from bondsbot.config import BondsConfig
from bondsbot.models import RatesSignal
from bondsbot.runtime import _execute_live_orders, _format_scan_message, _should_send_heartbeat


class FakeOandaClient:
    def __init__(self) -> None:
        self.orders: list[dict[str, object]] = []
        self.tradeable = True

    def open_positions(self) -> set[str]:
        return set()

    def instrument_tradeable(self, instrument: str) -> tuple[bool, str]:
        return self.tradeable, "tradeable" if self.tradeable else "pricing_status_non_tradeable"

    def place_market_order(self, instrument: str, units: float, tag: str, *, stop_loss: float | None = None, take_profit: float | None = None) -> dict[str, object]:
        self.orders.append({"instrument": instrument, "units": units, "tag": tag, "stop_loss": stop_loss, "take_profit": take_profit})
        return {"orderFillTransaction": {"id": "order-1"}}


def _live_config() -> BondsConfig:
    return replace(
        BondsConfig.from_env(),
        paper_trade=False,
        live_trading_enabled=True,
        oanda_account_id="acct",
        oanda_api_token="token",
        max_live_orders_per_scan=1,
    )


def _oanda_signal() -> RatesSignal:
    return RatesSignal(
        canonical="US10Y",
        side="SHORT",
        strategy="DURATION_TREND",
        score=88.0,
        target_dv01=0.0,
        entry_price=96.0,
        entry_yield_bps=395.0,
        stop_yield_bps=390.0,
        stop_price=97.0,
        take_profit_price=94.0,
        expected_hold_bars=8,
        event_risk="NORMAL",
        country="US",
        tenor_bucket="10Y",
        metadata={"data_provider": "oanda", "oanda_instrument": "USB10Y_USD", "time": datetime(2026, 5, 4, tzinfo=timezone.utc)},
    )


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

    def test_heartbeat_gate_limits_routine_telegram_messages(self) -> None:
        self.assertTrue(_should_send_heartbeat({}, 1000.0, 3600))
        self.assertFalse(_should_send_heartbeat({"last_telegram_heartbeat_at": 900.0}, 1000.0, 3600))
        self.assertTrue(_should_send_heartbeat({"last_telegram_heartbeat_at": 900.0}, 4600.0, 3600))

    def test_live_config_executes_oanda_signal_with_brackets(self) -> None:
        config = _live_config()
        signal = _oanda_signal()
        state: dict[str, object] = {}
        client = FakeOandaClient()

        orders, errors = _execute_live_orders([signal], config, client, state, 10000.0, False)

        self.assertEqual(errors, [])
        self.assertEqual(len(orders), 1)
        self.assertEqual(client.orders[0]["instrument"], "USB10Y_USD")
        self.assertLess(client.orders[0]["units"], 0)
        self.assertEqual(client.orders[0]["stop_loss"], 97.0)
        self.assertEqual(client.orders[0]["take_profit"], 94.0)
        self.assertEqual(state["open_positions"][0]["order_id"], "order-1")

    def test_live_config_skips_order_when_oanda_market_is_not_tradeable(self) -> None:
        config = _live_config()
        signal = _oanda_signal()
        state: dict[str, object] = {}
        client = FakeOandaClient()
        client.tradeable = False

        orders, errors = _execute_live_orders([signal], config, client, state, 10000.0, False)

        self.assertEqual(orders, [])
        self.assertEqual(client.orders, [])
        self.assertEqual(errors[0]["stage"], "market")
        self.assertEqual(errors[0]["instrument"], "USB10Y_USD")
        self.assertIn("non_tradeable", str(errors[0]["reason"]))


if __name__ == "__main__":
    unittest.main()
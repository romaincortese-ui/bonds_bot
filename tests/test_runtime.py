from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone

from bondsbot.config import BondsConfig
from bondsbot.models import RatesSignal
from bondsbot.runtime import _execute_live_orders, _format_scan_message, _send_trade_lifecycle_alerts, _should_send_heartbeat


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
        return {"orderFillTransaction": {"id": "fill-1", "price": "95.875", "tradeOpened": {"tradeID": "trade-1"}}}


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str | None]] = []

    def send(self, message: str, parse_mode: str | None = None) -> None:
        self.messages.append((message, parse_mode))


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
        self.assertEqual(state["open_positions"][0]["order_id"], "trade-1")
        self.assertEqual(state["open_positions"][0]["entry_price"], 95.875)

    def test_live_order_alert_matches_forex_lifecycle_style(self) -> None:
        config = _live_config()
        signal = _oanda_signal()
        state: dict[str, object] = {}
        client = FakeOandaClient()
        notifier = FakeNotifier()

        orders, errors = _execute_live_orders([signal], config, client, state, 10000.0, False)
        _send_trade_lifecycle_alerts(notifier, orders, [])

        self.assertEqual(errors, [])
        self.assertEqual(len(notifier.messages), 1)
        message, parse_mode = notifier.messages[0]
        self.assertEqual(parse_mode, "HTML")
        self.assertIn("<b>Duration Trend SHORT</b> | US10Y", message)
        self.assertIn("Entry: 95.87500", message)
        self.assertIn("TP: 94.00000", message)
        self.assertIn("SL: 97.00000", message)
        self.assertIn("DV01", message)
        self.assertIn("Order: trade-1", message)

    def test_closed_oanda_position_generates_broker_close_alert(self) -> None:
        config = _live_config()
        state: dict[str, object] = {
            "open_positions": [
                {
                    "canonical": "US10Y",
                    "instrument": "USB10Y_USD",
                    "side": "SHORT",
                    "strategy": "DURATION_TREND",
                    "units": 8.0,
                    "entry_price": 96.0,
                    "entry_yield_bps": 395.0,
                    "dv01": 0.25,
                    "stop_price": 97.0,
                    "take_profit_price": 94.0,
                    "opened_at": "2026-05-04T17:00:00+00:00",
                    "country": "US",
                    "tenor_bucket": "10Y",
                    "order_id": "trade-1",
                    "metadata": {},
                }
            ]
        }
        client = FakeOandaClient()
        notifier = FakeNotifier()

        orders, errors = _execute_live_orders([], config, client, state, 10000.0, False)
        _send_trade_lifecycle_alerts(notifier, orders, state["last_closed_positions"])

        self.assertEqual(orders, [])
        self.assertEqual(errors, [])
        self.assertEqual(state["open_positions"], [])
        self.assertEqual(len(notifier.messages), 1)
        message, parse_mode = notifier.messages[0]
        self.assertEqual(parse_mode, "HTML")
        self.assertIn("<b>Duration Trend Closed at broker</b> | US10Y", message)
        self.assertIn("Exit: broker reported closed", message)
        self.assertIn("Reason: OANDA position no longer open", message)

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
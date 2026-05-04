from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from bondsbot.backtest.data import FixtureRatesDataProvider
from bondsbot.config import BondsConfig
from bondsbot.models import RatesPosition, RatesSignal
from bondsbot.oanda_client import OandaClient
from bondsbot.risk import can_open, country_dv01, max_country_dv01, max_portfolio_dv01, max_tenor_dv01, position_from_signal, tenor_dv01
from bondsbot.state import StateStore
from bondsbot.strategies import generate_signal
from bondsbot.telegram import TelegramNotifier


LAST_HEARTBEAT_AT_KEY = "last_telegram_heartbeat_at"


def _side_label(side: str) -> str:
    return "🟢 LONG" if side.upper() == "LONG" else "🔴 SHORT"


def _provider_label(provider: str) -> str:
    return "📡 OANDA" if provider == "oanda" else "🧪 Fixture"


def _pretty_strategy(strategy: str) -> str:
    return strategy.replace("_", " ").title()


def _format_time(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _execution_label(config: BondsConfig, research_only: bool) -> str:
    if research_only:
        return "research signals"
    if config.paper_trade:
        return "paper signals"
    if config.live_trading_enabled:
        return "live orders"
    return "signals only"


def _coerce_time(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _order_id(response: dict[str, object]) -> str:
    for key in ("orderFillTransaction", "orderCreateTransaction", "orderCancelTransaction"):
        item = response.get(key)
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return "live"


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _account_equity(config: BondsConfig, client: OandaClient, state: dict[str, Any]) -> float:
    if not config.paper_trade and config.has_oanda_credentials:
        try:
            equity = client.account_nav()
            if equity > 0:
                state["equity"] = equity
                return equity
        except RuntimeError as exc:
            state["last_account_nav_error"] = str(exc)
    try:
        return float(state.get("equity") or config.backtest_initial_balance)
    except (TypeError, ValueError):
        return config.backtest_initial_balance


def _position_from_state(row: dict[str, object]) -> RatesPosition | None:
    try:
        return RatesPosition(
            canonical=str(row.get("canonical") or "").upper(),
            side=str(row.get("side") or "LONG").upper(),
            strategy=str(row.get("strategy") or "LIVE"),
            units=abs(float(row.get("units") or 0.0)),
            entry_price=float(row.get("entry_price") or 0.0),
            entry_yield_bps=float(row["entry_yield_bps"]) if row.get("entry_yield_bps") is not None else None,
            dv01=float(row.get("dv01") or 0.0),
            stop_price=float(row.get("stop_price") or 0.0),
            take_profit_price=float(row.get("take_profit_price") or 0.0),
            opened_at=_coerce_time(row.get("opened_at")),
            country=str(row.get("country") or "OTHER"),
            tenor_bucket=str(row.get("tenor_bucket") or "OTHER"),
            order_id=str(row.get("order_id") or "live"),
            metadata=dict(row.get("metadata") or {}),
        )
    except (TypeError, ValueError):
        return None


def _state_position_rows(state: dict[str, Any]) -> list[dict[str, object]]:
    rows = state.get("open_positions", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _sync_open_position_rows(config: BondsConfig, client: OandaClient, state: dict[str, Any]) -> tuple[list[dict[str, object]], set[str], list[dict[str, object]]]:
    rows = _state_position_rows(state)
    errors: list[dict[str, object]] = []
    open_instruments: set[str] = set()
    if not config.has_oanda_credentials:
        return rows, open_instruments, errors
    try:
        open_instruments = client.open_positions()
        rows = [row for row in rows if str(row.get("instrument") or "").upper() in open_instruments]
        state["open_positions"] = rows
    except RuntimeError as exc:
        errors.append({"stage": "open_positions", "error": str(exc)})
    return rows, open_instruments, errors


def _position_row(position: RatesPosition, instrument: str, signed_units: float) -> dict[str, object]:
    return {
        "canonical": position.canonical,
        "instrument": instrument,
        "side": position.side,
        "strategy": position.strategy,
        "units": position.units,
        "order_units": signed_units,
        "entry_price": position.entry_price,
        "entry_yield_bps": position.entry_yield_bps,
        "dv01": position.dv01,
        "stop_price": position.stop_price,
        "take_profit_price": position.take_profit_price,
        "opened_at": position.opened_at.isoformat(),
        "country": position.country,
        "tenor_bucket": position.tenor_bucket,
        "order_id": position.order_id,
        "metadata": _json_safe(position.metadata),
    }


def _execute_live_orders(signals: list[RatesSignal], config: BondsConfig, client: OandaClient, state: dict[str, Any], equity: float, research_only: bool) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if config.paper_trade or not config.live_trading_enabled or research_only:
        return [], []
    rows, open_instruments, errors = _sync_open_position_rows(config, client, state)
    positions = [position for position in (_position_from_state(row) for row in rows) if position is not None]
    orders: list[dict[str, object]] = []
    for signal in signals:
        if len(orders) >= config.max_live_orders_per_scan:
            break
        if len(positions) >= config.max_open_positions or len(open_instruments) >= config.max_open_positions:
            break
        if str(signal.metadata.get("data_provider") or "") != "oanda":
            continue
        instrument = str(signal.metadata.get("oanda_instrument") or "").strip().upper()
        if not instrument or instrument in open_instruments:
            continue
        allowed, reason = can_open(signal, positions, equity, config)
        if not allowed:
            errors.append({"canonical": signal.canonical, "stage": "risk", "reason": reason})
            continue
        position = position_from_signal(signal, equity, positions, config)
        if position.units <= 0 or position.dv01 <= 0:
            continue
        signed_units = position.units if signal.side.upper() == "LONG" else -position.units
        try:
            response = client.place_market_order(
                instrument,
                signed_units,
                f"bonds-{signal.canonical.lower()}",
                stop_loss=position.stop_price,
                take_profit=position.take_profit_price,
            )
        except RuntimeError as exc:
            errors.append({"canonical": signal.canonical, "instrument": instrument, "stage": "order", "error": str(exc)})
            continue
        position.order_id = _order_id(response)
        row = _position_row(position, instrument, signed_units)
        rows.append(row)
        positions.append(position)
        open_instruments.add(instrument)
        orders.append(
            {
                "canonical": signal.canonical,
                "side": signal.side,
                "instrument": instrument,
                "units": round(signed_units, 4),
                "dv01": round(position.dv01, 4),
                "order_id": position.order_id,
                "score": round(signal.score, 2),
            }
        )
    state["open_positions"] = rows
    state["last_live_orders"] = orders
    state["last_live_order_errors"] = errors
    return orders, errors


def _format_scan_message(snapshot: dict[str, object], config: BondsConfig) -> str:
    counts = snapshot.get("data_provider_counts", {})
    oanda_count = int(counts.get("oanda", 0)) if isinstance(counts, dict) else 0
    fixture_count = int(counts.get("fixture", 0)) if isinstance(counts, dict) else 0
    total_count = max(oanda_count + fixture_count, len(config.universe))
    mapped = snapshot.get("mapped_oanda_instruments", {})
    mapped_count = len(mapped) if isinstance(mapped, dict) else 0
    failures = snapshot.get("failures", [])
    failure_count = len(failures) if isinstance(failures, list) else 0
    research_only = bool(snapshot.get("research_only", False))
    if research_only:
        mode_icon = "🟠"
        mode_text = "RESEARCH ONLY"
    elif config.paper_trade:
        mode_icon = "🧪"
        mode_text = "PAPER"
    else:
        mode_icon = "🔴"
        mode_text = "LIVE config"
    execution = _execution_label(config, research_only)
    risk_caps = snapshot.get("risk_caps", {}) if isinstance(snapshot.get("risk_caps", {}), dict) else {}

    lines = [
        "🏦 Bonds Bot",
        f"{mode_icon} Mode: {mode_text} | Execution: {execution}",
        f"🕒 Scan: {_format_time(snapshot.get('time', ''))}",
        f"📊 Data: OANDA {oanda_count}/{total_count} | Fixture {fixture_count}/{total_count} | Mapped {mapped_count}/{total_count} | Tradeable {snapshot.get('tradeable_instrument_count', 0)}",
        (
            "🛡️ DV01 caps: "
            f"Portfolio {float(risk_caps.get('portfolio_dv01', 0.0)):.2f} | "
            f"Country {float(risk_caps.get('country_dv01', 0.0)):.2f} | "
            f"Tenor {float(risk_caps.get('tenor_dv01', 0.0)):.2f}"
        ),
    ]
    if failure_count:
        failed_symbols = ", ".join(str(item).split(":", 1)[0] for item in failures[:3]) if isinstance(failures, list) else "some symbols"
        suffix = "" if failure_count <= 3 else f" +{failure_count - 3} more"
        lines.append(f"⚠️ Fallbacks: {failure_count} OANDA fetch issue(s): {failed_symbols}{suffix}")

    top_signals = snapshot.get("top_signals", [])
    if isinstance(top_signals, list) and top_signals:
        lines.append("")
        lines.append("🏆 Top Rates Signals")
        for index, row in enumerate(top_signals[:5], start=1):
            if not isinstance(row, dict):
                continue
            provider = str(row.get("data_provider", "fixture"))
            instrument = row.get("oanda_instrument")
            instrument_text = f" | {instrument}" if provider == "oanda" and instrument else ""
            lines.append(
                f"{index}. {_side_label(str(row.get('side', '')))} {row.get('canonical', '?')} ({row.get('country', '?')} {row.get('tenor', '?')}) | Score {float(row.get('score', 0.0)):.1f} | {_provider_label(provider)}{instrument_text}"
            )
            lines.append(f"   ↳ {_pretty_strategy(str(row.get('strategy', '')))}")
    else:
        lines.append("😴 No qualified rates signals this scan.")

    live_orders = snapshot.get("live_orders", [])
    if isinstance(live_orders, list) and live_orders:
        lines.append("")
        lines.append("✅ Live Orders")
        for row in live_orders[:3]:
            if isinstance(row, dict):
                lines.append(f"{row.get('side', '?')} {row.get('canonical', '?')} | {row.get('instrument', '?')} | units {row.get('units', '?')} | DV01 {row.get('dv01', '?')}")

    live_errors = snapshot.get("live_order_errors", [])
    if isinstance(live_errors, list) and live_errors:
        lines.append(f"⚠️ Live order issue(s): {len(live_errors)}")

    return "\n".join(lines)


def _should_send_heartbeat(state: dict[str, object], now_ts: float, heartbeat_seconds: int) -> bool:
    try:
        last_sent = float(state.get(LAST_HEARTBEAT_AT_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        last_sent = 0.0
    if last_sent <= 0.0:
        return True
    return now_ts - last_sent >= max(0, heartbeat_seconds)


def run_scan(config: BondsConfig, client: OandaClient, state_store: StateStore, notifier: TelegramNotifier) -> dict[str, object]:
    state = state_store.load()
    tradeable: set[str] = set()
    if config.has_oanda_credentials:
        try:
            tradeable = set(client.tradeable_instruments())
        except RuntimeError:
            tradeable = set()

    provider = FixtureRatesDataProvider(days=max(90, config.backtest_days + 60))
    data_provider_counts = {"oanda": 0, "fixture": 0}
    mapped_instruments: dict[str, str] = {}
    failures: list[str] = []
    signals = []
    for canonical in config.universe:
        oanda_instrument = config.oanda_instrument_for(canonical, tradeable) if tradeable else ""
        candles = []
        data_provider = "fixture"
        if oanda_instrument:
            try:
                candles = client.candles(oanda_instrument, count=max(90, config.backtest_days + 60), granularity="D")
            except RuntimeError as exc:
                failures.append(f"{canonical}:{exc}")
            if len(candles) >= 45:
                data_provider = "oanda"
                mapped_instruments[canonical] = oanda_instrument
            else:
                candles = []
        if not candles:
            candles = provider.history(canonical)
        data_provider_counts[data_provider] += 1
        signal = generate_signal(canonical, candles, config)
        if signal is not None:
            signal.metadata["data_provider"] = data_provider
            if oanda_instrument:
                signal.metadata["oanda_instrument"] = oanda_instrument
            signals.append(signal)

    signals.sort(key=lambda item: item.score, reverse=True)
    equity = _account_equity(config, client, state)
    research_only = config.has_oanda_credentials and len(mapped_instruments) < config.min_oanda_rates_products
    live_orders, live_order_errors = _execute_live_orders(signals, config, client, state, equity, research_only)
    snapshot = {
        "time": datetime.now(timezone.utc).isoformat(),
        "paper_trade": config.paper_trade or research_only,
        "research_only": research_only,
        "configured_symbols": list(config.universe),
        "mapped_oanda_instruments": mapped_instruments,
        "tradeable_instrument_count": len(tradeable),
        "data_provider_counts": data_provider_counts,
        "failures": failures[:5],
        "live_orders": live_orders,
        "live_order_errors": live_order_errors[:5],
        "risk_caps": {
            "portfolio_dv01": round(max_portfolio_dv01(equity, config), 4),
            "country_dv01": round(max_country_dv01(equity, config), 4),
            "tenor_dv01": round(max_tenor_dv01(equity, config), 4),
        },
        "open_dv01_by_country": country_dv01([]),
        "open_dv01_by_tenor": tenor_dv01([]),
        "top_signals": [
            {
                "canonical": signal.canonical,
                "side": signal.side,
                "score": round(signal.score, 2),
                "strategy": signal.strategy,
                "country": signal.country,
                "tenor": signal.tenor_bucket,
                "data_provider": signal.metadata.get("data_provider", "fixture"),
                "oanda_instrument": signal.metadata.get("oanda_instrument", ""),
            }
            for signal in signals[:5]
        ],
    }
    message = _format_scan_message(snapshot, config)
    print(message, flush=True)
    state["last_snapshot"] = snapshot
    now_ts = time.time()
    if _should_send_heartbeat(state, now_ts, config.heartbeat_seconds):
        state[LAST_HEARTBEAT_AT_KEY] = now_ts
        state["last_telegram_heartbeat_time"] = snapshot["time"]
        notifier.send(message)
    state_store.save(state)
    return snapshot


def run_bot() -> None:
    config = BondsConfig.from_env()
    notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id)
    state_store = StateStore(config.state_file)
    client = OandaClient(config)

    if not config.paper_trade and not config.has_oanda_credentials:
        raise RuntimeError("Live trading requested but OANDA_ACCOUNT_ID/OANDA_API_TOKEN are missing")

    while True:
        run_scan(config, client, state_store, notifier)
        if config.run_once:
            return
        time.sleep(max(30, config.scan_interval_seconds))

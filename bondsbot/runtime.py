from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from html import escape
from typing import Any

from bondsbot.backtest.data import FixtureRatesDataProvider
from bondsbot.config import BondsConfig
from bondsbot.models import RatesPosition, RatesSignal
from bondsbot.oanda_client import OandaClient
from bondsbot.risk import can_open, country_dv01, dv01_per_unit, max_country_dv01, max_portfolio_dv01, max_tenor_dv01, position_from_signal, tenor_dv01
from bondsbot.state import StateStore
from bondsbot.strategies import generate_signal
from bondsbot.telegram import TelegramNotifier


LAST_HEARTBEAT_AT_KEY = "last_telegram_heartbeat_at"
LAST_TELEGRAM_UPDATE_ID_KEY = "last_telegram_update_id"
TELEGRAM_POLL_SECONDS = 5.0


def _pretty_strategy(strategy: str) -> str:
    return strategy.replace("_", " ").title()


def _html(value: object) -> str:
    return escape(str(value), quote=False)


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _format_price(value: object) -> str:
    price = _float_or_none(value)
    if price is None:
        return "n/a"
    return f"{price:.5f}"


def _format_amount(value: object, digits: int = 2) -> str:
    amount = _float_or_none(value)
    if amount is None:
        return "n/a"
    return f"{amount:.{digits}f}"


def _format_money(value: object) -> str:
    amount = _float_or_none(value)
    if amount is None:
        return "n/a"
    sign = "-" if amount < 0 else ""
    return f"{sign}£{abs(amount):.2f}"


def _format_signed_money(value: object) -> str:
    amount = _float_or_none(value)
    if amount is None:
        return "n/a"
    sign = "+" if amount >= 0 else "-"
    return f"{sign}£{abs(amount):.2f}"


def _format_signed_percent(value: object) -> str:
    amount = _float_or_none(value)
    if amount is None:
        return "n/a"
    sign = "+" if amount >= 0 else ""
    return f"{sign}{amount:.2f}%"


def _position_entry_budget(row: dict[str, object]) -> float | None:
    for key in ("entry_budget", "initial_margin_required", "margin_used"):
        value = _float_or_none(row.get(key))
        if value is not None and value > 0:
            return value
    return None


def _position_unrealized_pl(row: dict[str, object]) -> float | None:
    for key in ("unrealized_pl", "unrealizedPL"):
        value = _float_or_none(row.get(key))
        if value is not None:
            return value
    entry_budget = _position_entry_budget(row)
    current_value = _float_or_none(row.get("current_value"))
    if entry_budget is not None and current_value is not None:
        return current_value - entry_budget
    if entry_budget is not None:
        return 0.0
    return None


def _position_current_value(row: dict[str, object]) -> float | None:
    current_value = _float_or_none(row.get("current_value"))
    if current_value is not None:
        return current_value
    entry_budget = _position_entry_budget(row)
    unrealized_pl = _position_unrealized_pl(row)
    if entry_budget is not None and unrealized_pl is not None:
        return entry_budget + unrealized_pl
    return None


def _position_pnl_pct(row: dict[str, object]) -> float | None:
    entry_budget = _position_entry_budget(row)
    unrealized_pl = _position_unrealized_pl(row)
    if entry_budget is None or entry_budget <= 0 or unrealized_pl is None:
        return None
    return unrealized_pl / entry_budget * 100.0


def _session_pnl(rows: list[object]) -> tuple[float, float]:
    total_entry_budget = 0.0
    total_pnl = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        unrealized_pl = _position_unrealized_pl(row)
        if unrealized_pl is None:
            continue
        total_pnl += unrealized_pl
        entry_budget = _position_entry_budget(row)
        if entry_budget is not None and entry_budget > 0:
            total_entry_budget += entry_budget
    pnl_pct = total_pnl / total_entry_budget * 100.0 if total_entry_budget > 0 else 0.0
    return pnl_pct, total_pnl


def _allocated_balance(rows: list[object]) -> float:
    total = 0.0
    for row in rows:
        if isinstance(row, dict):
            total += _position_entry_budget(row) or 0.0
    return total


def _closed_trade_stats(state: dict[str, Any], closed_positions: list[object]) -> tuple[int, float | None]:
    closed_count = int(state.get("runtime_closed_trade_count", 0) or 0)
    if closed_positions:
        closed_count += len([row for row in closed_positions if isinstance(row, dict)])
        state["runtime_closed_trade_count"] = closed_count
    pnls: list[float] = []
    for row in state.get("last_profit_protection_updates", []):
        if isinstance(row, dict):
            value = _position_unrealized_pl(row)
            if value is not None:
                pnls.append(value)
    if not pnls:
        return closed_count, None
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = -sum(value for value in pnls if value < 0)
    if gross_loss > 0:
        return closed_count, gross_profit / gross_loss
    return closed_count, 999.0 if gross_profit > 0 else 0.0


def _runtime_status_payload(config: BondsConfig, state: dict[str, Any], snapshot: dict[str, object]) -> dict[str, object]:
    open_positions = snapshot.get("open_positions", [])
    open_rows = open_positions if isinstance(open_positions, list) else []
    closed_positions = snapshot.get("closed_positions", [])
    closed_rows = closed_positions if isinstance(closed_positions, list) else []
    total_trades, profit_factor = _closed_trade_stats(state, closed_rows)
    account_balance = _float_or_none(snapshot.get("account_balance"))
    available_balance = _float_or_none(snapshot.get("available_balance"))
    allocated_balance = _float_or_none(snapshot.get("allocated_balance"))
    if allocated_balance is None:
        allocated_balance = _allocated_balance(open_rows)
    payload = {
        "service": "bonds",
        "state": "running",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paper_trade": bool(snapshot.get("paper_trade")),
        "research_only": bool(snapshot.get("research_only")),
        "account_balance": account_balance,
        "account_nav": account_balance,
        "available_balance": available_balance,
        "allocated_balance": allocated_balance,
        "unrealized_pl": _float_or_none(snapshot.get("session_pnl_amount")),
        "pnl_amount": _float_or_none(snapshot.get("session_pnl_amount")),
        "pnl_pct": _float_or_none(snapshot.get("session_pnl_pct")),
        "open_trades": len(open_rows),
        "open_positions": open_rows,
        "total_trades": total_trades + len(open_rows),
        "profit_factor": profit_factor,
        "last_scan": snapshot,
    }
    return payload


def _publish_runtime_status(config: BondsConfig, payload: dict[str, object]) -> bool:
    if not config.redis_url or not config.runtime_status_redis_key or config.runtime_status_ttl_seconds <= 0:
        return False
    try:
        import redis

        client = redis.from_url(config.redis_url, socket_connect_timeout=5, socket_timeout=5)
        client.set(config.runtime_status_redis_key, json.dumps(payload, default=str), ex=config.runtime_status_ttl_seconds)
        return True
    except Exception:
        return False


def _held_minutes(row: dict[str, object], closed_at: datetime | None = None) -> float:
    opened_at = _coerce_time(row.get("opened_at"))
    closed_time = closed_at or datetime.now(timezone.utc)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    return max(0.0, (closed_time - opened_at.astimezone(timezone.utc)).total_seconds() / 60.0)


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


def _format_boot_message(config: BondsConfig, state: dict[str, Any], started_at: datetime | None = None) -> str:
    mode_icon = "🔴" if not config.paper_trade else "🧪"
    mode_text = "LIVE config" if not config.paper_trade else "PAPER"
    started = started_at or datetime.now(timezone.utc)
    rows = _state_position_rows(state)
    lines = [
        "🚀 <b>Bonds Bot Boot</b>",
        "━━━━━━━━━━━━━━━",
        f"Mode: {mode_icon} {mode_text} | Execution: {_execution_label(config, False)}",
        f"Universe: {len(config.universe)} instruments",
        f"Open positions after OANDA sync: {len(rows)} / {config.max_open_positions}",
        f"Scan interval: {max(30, config.scan_interval_seconds)}s | Heartbeat: {config.heartbeat_seconds}s",
        f"Started: {_format_time(started)}",
    ]
    if rows:
        lines.append("")
        lines.append("📂 <b>Open positions</b>")
        for row in rows[:4]:
            lines.extend(_format_position_lines(row))
        if len(rows) > 4:
            lines.append(f"+{len(rows) - 4} more")
    return "\n".join(lines)


def _format_live_issue(row: dict[str, object]) -> str:
    subject = row.get("canonical") or row.get("instrument") or "unknown"
    stage = row.get("stage") or "live"
    detail = row.get("reason") or row.get("error") or "blocked"
    text = str(detail)
    if len(text) > 160:
        text = text[:157] + "..."
    return f"{subject} | {stage}: {text}"


def _format_help_message() -> str:
    return "\n".join(
        [
            "📋 <b>Bonds Bot Commands</b>",
            "/status — Bot, account, scan, and open positions",
            "/positions — Open positions only",
            "/help — This message",
        ]
    )


def _signed_oanda_units(units: float, side: str) -> int:
    whole_units = int(abs(units))
    if whole_units < 1:
        return 0
    return whole_units if side.upper() == "LONG" else -whole_units


def _coerce_time(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _order_id(response: dict[str, object]) -> str:
    fill = response.get("orderFillTransaction")
    if isinstance(fill, dict):
        opened = fill.get("tradeOpened")
        if isinstance(opened, dict) and opened.get("tradeID"):
            return str(opened["tradeID"])
    for key in ("orderFillTransaction", "orderCreateTransaction", "orderCancelTransaction"):
        item = response.get(key)
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return "live"


def _fill_price(response: dict[str, object]) -> float | None:
    fill = response.get("orderFillTransaction")
    if not isinstance(fill, dict):
        return None
    return _float_or_none(fill.get("price"))


def _order_cancel_reason(response: dict[str, object]) -> str:
    cancel = response.get("orderCancelTransaction")
    if isinstance(cancel, dict):
        return str(cancel.get("reason") or cancel.get("rejectReason") or "order_not_filled")
    reject = response.get("orderRejectTransaction")
    if isinstance(reject, dict):
        return str(reject.get("rejectReason") or reject.get("reason") or "order_rejected")
    return "order_not_filled"


def _recenter_brackets(position: RatesPosition, signal: RatesSignal, bid: float, ask: float) -> None:
    stop_distance = max(abs(signal.entry_price - signal.stop_price), signal.entry_price * 0.0018)
    target_distance = max(abs(signal.take_profit_price - signal.entry_price), stop_distance)
    if signal.side == "LONG":
        position.entry_price = ask
        position.stop_price = max(0.00001, ask - stop_distance)
        position.take_profit_price = ask + target_distance
    else:
        position.entry_price = bid
        position.stop_price = bid + stop_distance
        position.take_profit_price = max(0.00001, bid - target_distance)


def _apply_minimum_live_units(position: RatesPosition, signal: RatesSignal, equity: float, config: BondsConfig) -> tuple[bool, str | None]:
    min_units = max(0, int(config.min_live_order_units))
    if min_units <= 0 or position.units >= min_units:
        return True, None
    if signal.score < config.min_live_unit_score:
        return False, "position_size_below_one_oanda_unit"
    unit_dv01 = dv01_per_unit(signal.canonical, position.entry_price, config)
    floor_dv01 = unit_dv01 * min_units
    nav_10bp = floor_dv01 * 10.0 / max(equity, 0.0001)
    if nav_10bp > config.max_min_unit_nav_10bp:
        return False, f"min_unit_nav_10bp_{nav_10bp:.4f}_above_{config.max_min_unit_nav_10bp:.4f}"
    position.units = float(min_units)
    position.dv01 = floor_dv01
    position.metadata["min_unit_floor_applied"] = True
    position.metadata["min_unit_nav_10bp"] = round(nav_10bp, 4)
    return True, None


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


def _account_available_balance(config: BondsConfig, client: OandaClient, state: dict[str, Any], equity: float | None = None) -> float:
    if not config.paper_trade and config.has_oanda_credentials:
        try:
            available_balance = client.account_available_balance()
            if available_balance >= 0:
                state["available_balance"] = available_balance
                return available_balance
        except RuntimeError as exc:
            state["last_account_available_balance_error"] = str(exc)
    try:
        return float(state.get("available_balance") or equity or state.get("equity") or config.backtest_initial_balance)
    except (TypeError, ValueError):
        return equity if equity is not None else config.backtest_initial_balance


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


def _strategy_for_canonical(canonical: str) -> str:
    if canonical in {"US5Y", "US10Y"}:
        return "BROKER_DURATION_FADE"
    return "DURATION_TREND"


def _canonical_for_instrument(config: BondsConfig, instrument: str) -> str:
    instrument = instrument.strip().upper()
    for canonical in config.universe:
        if instrument in config.oanda_candidates_for(canonical):
            return canonical
    for canonical in ("US2Y", "US5Y", "US10Y", "US30Y", "DE10Y", "UK10Y"):
        if instrument in config.oanda_candidates_for(canonical):
            return canonical
    return ""


def _oanda_order_price(trade: dict[str, object], order_key: str) -> float:
    order = trade.get(order_key)
    if not isinstance(order, dict):
        return 0.0
    return _float_or_none(order.get("price")) or 0.0


def _row_from_oanda_trade(config: BondsConfig, trade: dict[str, object], existing: dict[str, object] | None = None) -> dict[str, object] | None:
    instrument = str(trade.get("instrument") or "").strip().upper()
    canonical = _canonical_for_instrument(config, instrument)
    signed_units = _float_or_none(trade.get("currentUnits")) or 0.0
    entry_price = _float_or_none(trade.get("price")) or 0.0
    if not instrument or not canonical or signed_units == 0.0 or entry_price <= 0.0:
        return None
    existing = existing or {}
    side = "LONG" if signed_units > 0 else "SHORT"
    units = abs(signed_units)
    stop_price = _oanda_order_price(trade, "stopLossOrder") or _float_or_none(existing.get("stop_price")) or 0.0
    take_profit_price = _oanda_order_price(trade, "takeProfitOrder") or _float_or_none(existing.get("take_profit_price")) or 0.0
    dv01 = _float_or_none(existing.get("dv01"))
    if dv01 is None:
        dv01 = units * dv01_per_unit(canonical, entry_price, config)
    initial_margin_required = _float_or_none(trade.get("initialMarginRequired"))
    margin_used = _float_or_none(trade.get("marginUsed"))
    entry_budget = initial_margin_required
    if entry_budget is None or entry_budget <= 0:
        entry_budget = _float_or_none(existing.get("entry_budget")) or margin_used
    unrealized_pl = _float_or_none(trade.get("unrealizedPL"))
    if unrealized_pl is None:
        unrealized_pl = _float_or_none(existing.get("unrealized_pl"))
    current_value = entry_budget + unrealized_pl if entry_budget is not None and unrealized_pl is not None else _float_or_none(existing.get("current_value"))
    metadata = dict(existing.get("metadata") or {}) if isinstance(existing.get("metadata"), dict) else {}
    metadata["reconciled_from_oanda"] = True
    client_extensions = trade.get("clientExtensions")
    if isinstance(client_extensions, dict) and client_extensions.get("tag"):
        metadata["oanda_tag"] = str(client_extensions["tag"])
    strategy = str(existing.get("strategy") or _strategy_for_canonical(canonical))
    return {
        "canonical": canonical,
        "instrument": instrument,
        "side": side,
        "strategy": strategy,
        "units": units,
        "order_units": signed_units,
        "entry_price": entry_price,
        "entry_yield_bps": existing.get("entry_yield_bps"),
        "dv01": dv01 or 0.0,
        "stop_price": stop_price,
        "take_profit_price": take_profit_price,
        "entry_budget": entry_budget,
        "initial_margin_required": initial_margin_required,
        "margin_used": margin_used,
        "unrealized_pl": unrealized_pl,
        "current_value": current_value,
        "opened_at": _coerce_time(trade.get("openTime")).isoformat(),
        "country": config.country_for(canonical),
        "tenor_bucket": config.tenor_bucket_for(canonical),
        "order_id": str(trade.get("id") or existing.get("order_id") or "live"),
        "metadata": _json_safe(metadata),
    }


def _format_position_lines(row: dict[str, object]) -> list[str]:
    side = str(row.get("side") or "?").upper()
    side_icon = "🟢" if side == "LONG" else "🔴"
    entry_budget = _position_entry_budget(row)
    pnl_pct = _position_pnl_pct(row)
    current_value = _position_current_value(row)
    return [
        (
            f"{side_icon} {_html(row.get('canonical') or '?')} {side} | "
            f"Entry Budget: {_format_money(entry_budget)} | P&L: {_format_signed_percent(pnl_pct)} | "
            f"Current value: {_format_money(current_value)}"
        ),
    ]


def _format_profit_lock_message(row: dict[str, object]) -> str:
    return "\n".join(
        [
            "✅ <b>PROFIT TAKEN: PEAK PULLBACK</b>",
            _format_position_lines(row)[0],
            (
                f"Peak: {_format_signed_percent(row.get('peak_pnl_pct'))} | "
                f"Pullback: {_format_amount(row.get('pullback_from_peak_pct'), 2)} pts | closed at market"
            ),
        ]
    )


def _metadata_dict(row: dict[str, object]) -> dict[str, object]:
    metadata = row.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)
    return {}


def _update_peak_pnl(row: dict[str, object], now: datetime) -> tuple[float | None, float | None]:
    pnl_pct = _position_pnl_pct(row)
    if pnl_pct is None:
        return None, None
    metadata = _metadata_dict(row)
    peak_pnl_pct = _float_or_none(metadata.get("peak_pnl_pct"))
    if peak_pnl_pct is None or pnl_pct > peak_pnl_pct:
        peak_pnl_pct = pnl_pct
        metadata["peak_pnl_pct"] = peak_pnl_pct
        metadata["peak_seen_at"] = now.isoformat()
        metadata["peak_current_value"] = _position_current_value(row)
        metadata["peak_unrealized_pl"] = _position_unrealized_pl(row)
        row["metadata"] = _json_safe(metadata)
    return pnl_pct, peak_pnl_pct


def _apply_profit_protection(config: BondsConfig, client: OandaClient, state: dict[str, Any]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    updates: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    if not config.profit_lock_enabled or config.paper_trade or not config.live_trading_enabled or not config.has_oanda_credentials:
        state["last_profit_protection_updates"] = []
        return updates, errors
    rows = _state_position_rows(state)
    kept_rows: list[dict[str, object]] = []
    now = datetime.now(timezone.utc)
    for row in rows:
        instrument = str(row.get("instrument") or "").strip().upper()
        trade_id = str(row.get("order_id") or "").strip()
        if not instrument or not trade_id:
            kept_rows.append(row)
            continue
        pnl_pct, peak_pnl_pct = _update_peak_pnl(row, now)
        if pnl_pct is None or peak_pnl_pct is None:
            kept_rows.append(row)
            continue
        pullback_pct = peak_pnl_pct - pnl_pct
        if peak_pnl_pct < max(0.0, config.profit_lock_trigger_pct) or pullback_pct < max(0.0, config.profit_lock_pullback_pct) or pnl_pct <= 0.0:
            kept_rows.append(row)
            continue
        try:
            client.close_trade(trade_id)
        except RuntimeError as exc:
            errors.append({"canonical": row.get("canonical") or instrument, "instrument": instrument, "stage": "profit_lock_close", "error": str(exc)})
            kept_rows.append(row)
            continue
        row["peak_pnl_pct"] = peak_pnl_pct
        row["pullback_from_peak_pct"] = pullback_pct
        row["closed_at"] = now.isoformat()
        row["exit_reason"] = "peak_pullback_profit_lock"
        updates.append(dict(row))
    state["open_positions"] = kept_rows
    state["last_profit_protection_updates"] = updates
    state["last_profit_protection_errors"] = errors[:5]
    return updates, errors


def _sync_open_position_rows(config: BondsConfig, client: OandaClient, state: dict[str, Any]) -> tuple[list[dict[str, object]], set[str], list[dict[str, object]], list[dict[str, object]]]:
    rows = _state_position_rows(state)
    errors: list[dict[str, object]] = []
    closed_rows: list[dict[str, object]] = []
    open_instruments: set[str] = set()
    if not config.has_oanda_credentials:
        return rows, open_instruments, errors, closed_rows
    try:
        trades = client.open_trades()
        live_trade_ids = {str(trade.get("id") or "") for trade in trades if isinstance(trade, dict) and trade.get("id")}
        existing_by_order_id = {str(row.get("order_id") or ""): row for row in rows if row.get("order_id")}
        existing_by_instrument: dict[str, dict[str, object]] = {}
        for row in rows:
            instrument = str(row.get("instrument") or "").strip().upper()
            if instrument and instrument not in existing_by_instrument:
                existing_by_instrument[instrument] = row
        reconciled_rows: list[dict[str, object]] = []
        for trade in trades:
            instrument = str(trade.get("instrument") or "").strip().upper()
            if instrument:
                open_instruments.add(instrument)
            existing = existing_by_order_id.get(str(trade.get("id") or "")) or existing_by_instrument.get(instrument)
            row = _row_from_oanda_trade(config, trade, existing)
            if row is None:
                errors.append({"stage": "reconcile", "instrument": instrument or "unknown", "reason": "unmapped_open_trade"})
                continue
            reconciled_rows.append(row)
        for row in rows:
            order_id = str(row.get("order_id") or "")
            instrument = str(row.get("instrument") or "").strip().upper()
            if (order_id and order_id in live_trade_ids) or (not order_id and instrument in open_instruments):
                continue
            if instrument:
                closed_rows.append(row)
        rows = reconciled_rows
        state["open_positions"] = rows
        state["last_oanda_reconciled_at"] = datetime.now(timezone.utc).isoformat()
    except RuntimeError as exc:
        errors.append({"stage": "open_positions", "error": str(exc)})
    return rows, open_instruments, errors, closed_rows


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
        "unrealized_pl": 0.0,
        "opened_at": position.opened_at.isoformat(),
        "country": position.country,
        "tenor_bucket": position.tenor_bucket,
        "order_id": position.order_id,
        "metadata": _json_safe(position.metadata),
    }


def _format_order_opened_message(row: dict[str, object]) -> str:
    return "\n".join(
        [
            "✅ <b>TRADE OPENED</b>",
            _format_position_lines(row)[0],
        ]
    )


def _format_broker_close_message(row: dict[str, object]) -> str:
    direction = str(row.get("side") or "?").upper()
    dir_arrow = "⬆️" if direction == "LONG" else "⬇️"
    strategy = _pretty_strategy(str(row.get("strategy") or "LIVE"))
    held_min = _held_minutes(row)
    return "\n".join(
        [
            f"🔄 <b>{_html(strategy)} Closed at broker</b> | {_html(row.get('canonical') or '?')} {dir_arrow}",
            f"Instrument: {_html(row.get('instrument') or '?')}",
            f"Entry: {_format_price(row.get('entry_price'))} → Exit: broker reported closed",
            "P&L: unavailable from open-position sync",
            f"Reason: OANDA position no longer open | Held: {held_min:.0f}min",
        ]
    )


def _send_trade_lifecycle_alerts(notifier: TelegramNotifier, live_orders: list[dict[str, object]], closed_positions: list[dict[str, object]]) -> None:
    for row in closed_positions:
        notifier.send(_format_broker_close_message(row), parse_mode="HTML")
    for row in live_orders:
        notifier.send(_format_order_opened_message(row), parse_mode="HTML")


def _send_profit_lock_alerts(notifier: TelegramNotifier, updates: list[dict[str, object]]) -> None:
    if not isinstance(updates, list):
        return
    for row in updates:
        if isinstance(row, dict):
            notifier.send(_format_profit_lock_message(row), parse_mode="HTML")


def _execute_live_orders(signals: list[RatesSignal], config: BondsConfig, client: OandaClient, state: dict[str, Any], equity: float, research_only: bool) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if config.paper_trade or not config.live_trading_enabled or research_only:
        state.setdefault("last_closed_positions", [])
        return [], []
    rows, open_instruments, errors, closed_positions = _sync_open_position_rows(config, client, state)
    state["last_closed_positions"] = closed_positions
    profit_updates, profit_errors = _apply_profit_protection(config, client, state)
    errors.extend(profit_errors)
    if profit_updates:
        rows = _state_position_rows(state)
    positions = [position for position in (_position_from_state(row) for row in rows) if position is not None]
    orders: list[dict[str, object]] = []
    market_statuses: dict[str, tuple[bool, str]] = {}
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
        if instrument not in market_statuses:
            try:
                market_statuses[instrument] = client.instrument_tradeable(instrument)
            except RuntimeError as exc:
                market_statuses[instrument] = (False, str(exc))
        market_open, market_reason = market_statuses[instrument]
        if not market_open:
            errors.append({"canonical": signal.canonical, "instrument": instrument, "stage": "market", "reason": market_reason})
            continue
        allowed, reason = can_open(signal, positions, equity, config)
        if not allowed:
            errors.append({"canonical": signal.canonical, "stage": "risk", "reason": reason})
            continue
        position = position_from_signal(signal, equity, positions, config)
        if position.units <= 0 or position.dv01 <= 0:
            continue
        try:
            bid, ask = client.current_bid_ask(instrument)
        except RuntimeError as exc:
            errors.append({"canonical": signal.canonical, "instrument": instrument, "stage": "pricing", "error": str(exc)})
            continue
        _recenter_brackets(position, signal, bid, ask)
        sized, size_reason = _apply_minimum_live_units(position, signal, equity, config)
        if not sized:
            errors.append({"canonical": signal.canonical, "stage": "units", "reason": size_reason or "position_size_below_one_oanda_unit"})
            continue
        signed_units = _signed_oanda_units(position.units, signal.side)
        if signed_units == 0:
            errors.append({"canonical": signal.canonical, "stage": "units", "reason": "position_size_below_one_oanda_unit"})
            continue
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
        if not isinstance(response.get("orderFillTransaction"), dict):
            errors.append({"canonical": signal.canonical, "instrument": instrument, "stage": "order", "reason": _order_cancel_reason(response)})
            continue
        fill_price = _fill_price(response)
        if fill_price is not None:
            position.entry_price = fill_price
        position.order_id = _order_id(response)
        row = _position_row(position, instrument, signed_units)
        try:
            live_trade = next((trade for trade in client.open_trades() if str(trade.get("id") or "") == position.order_id), None)
            enriched_row = _row_from_oanda_trade(config, live_trade, row) if live_trade else None
            if enriched_row is not None:
                row = enriched_row
        except RuntimeError as exc:
            errors.append({"canonical": signal.canonical, "instrument": instrument, "stage": "reconcile", "error": str(exc)})
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
                "strategy": position.strategy,
                "entry_price": position.entry_price,
                "stop_price": position.stop_price,
                "take_profit_price": position.take_profit_price,
                "entry_budget": row.get("entry_budget"),
                "unrealized_pl": row.get("unrealized_pl"),
                "current_value": row.get("current_value"),
                "country": position.country,
                "tenor_bucket": position.tenor_bucket,
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
    open_positions = snapshot.get("open_positions", [])
    open_positions = open_positions if isinstance(open_positions, list) else []
    session_pnl_pct, session_pnl_amount = _session_pnl(open_positions)

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
        f"📂 Open positions: {len(open_positions)} / {config.max_open_positions}",
        f"💷 Total P&L: {_format_signed_percent(session_pnl_pct)} | {_format_signed_money(session_pnl_amount)}",
        f"💰 Available Balance: {_format_money(snapshot.get('available_balance'))}",
    ]
    for row in open_positions[:4]:
        if isinstance(row, dict):
            lines.extend(_format_position_lines(row))
    if len(open_positions) > 4:
        lines.append(f"+{len(open_positions) - 4} more open positions")
    if failure_count:
        failed_symbols = ", ".join(str(item).split(":", 1)[0] for item in failures[:3]) if isinstance(failures, list) else "some symbols"
        suffix = "" if failure_count <= 3 else f" +{failure_count - 3} more"
        lines.append(f"⚠️ Fallbacks: {failure_count} OANDA fetch issue(s): {failed_symbols}{suffix}")

    live_orders = snapshot.get("live_orders", [])
    if isinstance(live_orders, list) and live_orders:
        lines.append("")
        lines.append("✅ Opened this scan")
        for row in live_orders[:3]:
            if isinstance(row, dict):
                lines.append(f"TRADE OPENED: {_format_position_lines(row)[0]}")

    profit_updates = snapshot.get("profit_protection_updates", [])
    if isinstance(profit_updates, list) and profit_updates:
        lines.append("")
        lines.append("✅ Profit taken on peak pullback")
        for row in profit_updates[:3]:
            if isinstance(row, dict):
                lines.append(f"{row.get('canonical', '?')} closed | peak {_format_signed_percent(row.get('peak_pnl_pct'))} | pullback {_format_amount(row.get('pullback_from_peak_pct'), 2)} pts")

    live_errors = snapshot.get("live_order_errors", [])
    if isinstance(live_errors, list) and live_errors:
        lines.append(f"⚠️ Live order issue(s): {len(live_errors)}")
        for row in live_errors[:2]:
            if isinstance(row, dict):
                lines.append(f"   ↳ {_format_live_issue(row)}")

    return "\n".join(lines)


def _format_positions_message(config: BondsConfig, state: dict[str, Any]) -> str:
    rows = _state_position_rows(state)
    if not rows:
        return "📭 <b>No open bond positions.</b>"
    lines = ["📂 <b>Bond Positions</b>", "━━━━━━━━━━━━━━━", "Synced from OANDA before this reply."]
    for row in rows[:10]:
        lines.extend(_format_position_lines(row))
    if len(rows) > 10:
        lines.append(f"+{len(rows) - 10} more")
    return "\n".join(lines)


def _format_status_message(config: BondsConfig, state: dict[str, Any], equity: float | None = None, sync_errors: list[dict[str, object]] | None = None) -> str:
    snapshot = state.get("last_snapshot", {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    counts = snapshot.get("data_provider_counts", {})
    counts = counts if isinstance(counts, dict) else {}
    oanda_count = int(counts.get("oanda", 0) or 0)
    fixture_count = int(counts.get("fixture", 0) or 0)
    total_count = max(oanda_count + fixture_count, len(config.universe))
    live_errors = snapshot.get("live_order_errors", [])
    live_error_count = len(live_errors) if isinstance(live_errors, list) else 0
    failures = snapshot.get("failures", [])
    failure_count = len(failures) if isinstance(failures, list) else 0
    risk_caps = snapshot.get("risk_caps", {})
    risk_caps = risk_caps if isinstance(risk_caps, dict) else {}
    sync_errors = sync_errors or []
    rows = _state_position_rows(state)
    last_scan = _format_time(snapshot.get("time")) if snapshot.get("time") else "n/a"
    last_sync = _format_time(state.get("last_oanda_reconciled_at")) if state.get("last_oanda_reconciled_at") else "n/a"
    equity_text = _format_amount(equity, 2) if equity is not None else "n/a"
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
    lines = [
        "📊 <b>Bonds Status</b>",
        "━━━━━━━━━━━━━━━",
        f"Mode: {mode_icon} {mode_text} | Execution: {_execution_label(config, research_only)}",
        "Bot: ▶️ Running",
        f"Equity/NAV: {equity_text}",
        f"Open positions: {len(rows)} / {config.max_open_positions}",
        f"Last scan: {last_scan}",
        f"Last OANDA position sync: {last_sync}",
        f"Data: OANDA {oanda_count}/{total_count} | Fixture {fixture_count}/{total_count}",
        f"Mapped: {len(snapshot.get('mapped_oanda_instruments', {})) if isinstance(snapshot.get('mapped_oanda_instruments', {}), dict) else 0}/{total_count} | Tradeable {snapshot.get('tradeable_instrument_count', 0)}",
        f"DV01 caps: portfolio {_format_amount(risk_caps.get('portfolio_dv01'), 4)} | country {_format_amount(risk_caps.get('country_dv01'), 4)} | tenor {_format_amount(risk_caps.get('tenor_dv01'), 4)}",
        f"OANDA fetch issues: {failure_count} | Live order issues: {live_error_count} | Sync issues: {len(sync_errors)}",
        f"Telegram: commands online",
    ]
    if rows:
        lines.append("")
        lines.append("📂 <b>Open positions</b>")
        for row in rows[:5]:
            lines.extend(_format_position_lines(row))
    return "\n".join(lines)


def _handle_status_command(config: BondsConfig, client: OandaClient, state: dict[str, Any], notifier: TelegramNotifier) -> None:
    sync_errors: list[dict[str, object]] = []
    closed_positions: list[dict[str, object]] = []
    if config.has_oanda_credentials:
        _, _, sync_errors, closed_positions = _sync_open_position_rows(config, client, state)
        state["last_closed_positions"] = closed_positions
    equity = _account_equity(config, client, state)
    if closed_positions:
        _send_trade_lifecycle_alerts(notifier, [], closed_positions)
    profit_updates, profit_errors = _apply_profit_protection(config, client, state)
    sync_errors.extend(profit_errors)
    if profit_updates:
        _send_profit_lock_alerts(notifier, profit_updates)
    notifier.send(_format_status_message(config, state, equity, sync_errors), parse_mode="HTML")


def _handle_positions_command(config: BondsConfig, client: OandaClient, state: dict[str, Any], notifier: TelegramNotifier) -> None:
    sync_errors: list[dict[str, object]] = []
    closed_positions: list[dict[str, object]] = []
    if config.has_oanda_credentials:
        _, _, sync_errors, closed_positions = _sync_open_position_rows(config, client, state)
        state["last_closed_positions"] = closed_positions
    if closed_positions:
        _send_trade_lifecycle_alerts(notifier, [], closed_positions)
    profit_updates, profit_errors = _apply_profit_protection(config, client, state)
    sync_errors.extend(profit_errors)
    if profit_updates:
        _send_profit_lock_alerts(notifier, profit_updates)
    if sync_errors:
        state["last_position_sync_errors"] = sync_errors[:5]
    notifier.send(_format_positions_message(config, state), parse_mode="HTML")


def _command_from_text(text: str) -> str:
    if not text.strip():
        return ""
    command = text.strip().split()[0].lower()
    return command.split("@", 1)[0]


def _poll_telegram_commands(config: BondsConfig, client: OandaClient, state_store: StateStore, notifier: TelegramNotifier) -> None:
    if not notifier.enabled:
        return
    state = state_store.load()
    try:
        last_update_id = int(state.get(LAST_TELEGRAM_UPDATE_ID_KEY, 0) or 0)
    except (TypeError, ValueError):
        last_update_id = 0
    updates = notifier.get_updates(last_update_id + 1, timeout=1)
    if not updates:
        return
    for update in updates:
        if not isinstance(update, dict):
            continue
        try:
            last_update_id = max(last_update_id, int(update.get("update_id", last_update_id)))
        except (TypeError, ValueError):
            pass
        message = update.get("message", {})
        if not isinstance(message, dict):
            continue
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", "")) if isinstance(chat, dict) else ""
        if chat_id != str(notifier.chat_id):
            continue
        command = _command_from_text(str(message.get("text") or ""))
        if command == "/status":
            _handle_status_command(config, client, state, notifier)
        elif command == "/positions":
            _handle_positions_command(config, client, state, notifier)
        elif command == "/help":
            notifier.send(_format_help_message(), parse_mode="HTML")
    state[LAST_TELEGRAM_UPDATE_ID_KEY] = last_update_id
    state_store.save(state)


def _sleep_with_telegram_polling(config: BondsConfig, client: OandaClient, state_store: StateStore, notifier: TelegramNotifier, seconds: float) -> None:
    deadline = time.time() + max(0.0, seconds)
    while True:
        _poll_telegram_commands(config, client, state_store, notifier)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(TELEGRAM_POLL_SECONDS, remaining))


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
    sync_errors: list[dict[str, object]] = []
    if config.has_oanda_credentials and (config.paper_trade or not config.live_trading_enabled or research_only):
        _, _, sync_errors, closed_positions = _sync_open_position_rows(config, client, state)
        state["last_closed_positions"] = closed_positions
    live_orders, live_order_errors = _execute_live_orders(signals, config, client, state, equity, research_only)
    open_positions = _state_position_rows(state)
    available_balance = _account_available_balance(config, client, state, equity)
    session_pnl_pct, session_pnl_amount = _session_pnl(open_positions)
    profit_updates = state.get("last_profit_protection_updates", [])
    profit_errors = state.get("last_profit_protection_errors", [])
    open_position_models = [position for position in (_position_from_state(row) for row in open_positions) if position is not None]
    snapshot = {
        "time": datetime.now(timezone.utc).isoformat(),
        "paper_trade": config.paper_trade or research_only,
        "research_only": research_only,
        "account_balance": round(equity, 2),
        "configured_symbols": list(config.universe),
        "mapped_oanda_instruments": mapped_instruments,
        "tradeable_instrument_count": len(tradeable),
        "data_provider_counts": data_provider_counts,
        "failures": failures[:5],
        "live_orders": live_orders,
        "live_order_errors": live_order_errors[:5],
        "profit_protection_updates": profit_updates if isinstance(profit_updates, list) else [],
        "profit_protection_errors": profit_errors if isinstance(profit_errors, list) else [],
        "sync_errors": sync_errors[:5],
        "open_positions": open_positions,
        "session_pnl_pct": round(session_pnl_pct, 4),
        "session_pnl_amount": round(session_pnl_amount, 2),
        "available_balance": round(available_balance, 2),
        "allocated_balance": round(_allocated_balance(open_positions), 2),
        "closed_positions": state.get("last_closed_positions", []),
        "risk_caps": {
            "portfolio_dv01": round(max_portfolio_dv01(equity, config), 4),
            "country_dv01": round(max_country_dv01(equity, config), 4),
            "tenor_dv01": round(max_tenor_dv01(equity, config), 4),
        },
        "open_dv01_by_country": country_dv01(open_position_models),
        "open_dv01_by_tenor": tenor_dv01(open_position_models),
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
    heartbeat_due = _should_send_heartbeat(state, now_ts, config.heartbeat_seconds)
    closed_positions = snapshot.get("closed_positions", [])
    if not isinstance(closed_positions, list):
        closed_positions = []
    _send_trade_lifecycle_alerts(notifier, live_orders, [row for row in closed_positions if isinstance(row, dict)])
    _send_profit_lock_alerts(notifier, [row for row in snapshot["profit_protection_updates"] if isinstance(row, dict)])
    if heartbeat_due:
        state[LAST_HEARTBEAT_AT_KEY] = now_ts
        state["last_telegram_heartbeat_time"] = snapshot["time"]
        notifier.send(message)
    _publish_runtime_status(config, _runtime_status_payload(config, state, snapshot))
    state_store.save(state)
    return snapshot


def run_bot() -> None:
    config = BondsConfig.from_env()
    notifier = TelegramNotifier(config.telegram_token, config.telegram_chat_id)
    state_store = StateStore(config.state_file)
    client = OandaClient(config)

    if not config.paper_trade and not config.has_oanda_credentials:
        raise RuntimeError("Live trading requested but OANDA_ACCOUNT_ID/OANDA_API_TOKEN are missing")

    boot_state = state_store.load()
    boot_closed_positions: list[dict[str, object]] = []
    if config.has_oanda_credentials:
        _, _, boot_sync_errors, boot_closed_positions = _sync_open_position_rows(config, client, boot_state)
        boot_state["last_closed_positions"] = boot_closed_positions
        if boot_sync_errors:
            boot_state["last_boot_sync_errors"] = boot_sync_errors[:5]
        boot_profit_updates, boot_profit_errors = _apply_profit_protection(config, client, boot_state)
        if boot_profit_errors:
            boot_state["last_boot_profit_lock_errors"] = boot_profit_errors[:5]
        state_store.save(boot_state)
    notifier.send(_format_boot_message(config, boot_state), parse_mode="HTML")
    if boot_closed_positions:
        _send_trade_lifecycle_alerts(notifier, [], boot_closed_positions)
    if config.has_oanda_credentials:
        _send_profit_lock_alerts(notifier, boot_state.get("last_profit_protection_updates", []))

    while True:
        _poll_telegram_commands(config, client, state_store, notifier)
        run_scan(config, client, state_store, notifier)
        if config.run_once:
            return
        _sleep_with_telegram_polling(config, client, state_store, notifier, max(30, config.scan_interval_seconds))

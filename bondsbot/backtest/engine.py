from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from bondsbot.config import BondsConfig
from bondsbot.exits import evaluate_exit
from bondsbot.models import BacktestResult, RatesPosition, Trade
from bondsbot.risk import can_open, dv01_per_unit, position_from_signal
from bondsbot.strategies import generate_signal


FEE_RATE = 0.00008
SLIPPAGE_RATE = 0.00005


class BacktestEngine:
    def __init__(self, config: BondsConfig, provider) -> None:
        self.config = config
        self.provider = provider

    def run(self) -> BacktestResult:
        histories = {symbol: self.provider.history(symbol) for symbol in self.config.universe}
        min_bars = min(len(candles) for candles in histories.values() if candles)
        start_index = max(45, min_bars - self.config.backtest_days)
        balance = self.config.backtest_initial_balance
        equity_curve = [balance]
        positions: list[RatesPosition] = []
        trades: list[Trade] = []

        for index in range(start_index, min_bars):
            for position in list(positions):
                candle = histories[position.canonical][index]
                should_exit, exit_price, reason = evaluate_exit(position, candle)
                profit_lock_exit = _evaluate_profit_lock(position, candle, self.config)
                if profit_lock_exit is not None and (not should_exit or reason in {"yield_stop", "time_stop", "hold"}):
                    should_exit = True
                    exit_price, reason = profit_lock_exit
                if should_exit:
                    pnl = _pnl(position, exit_price)
                    balance += pnl
                    trades.append(_trade_from_position(position, candle.time, exit_price, pnl, reason))
                    positions.remove(position)

            candidates = []
            for symbol, candles in histories.items():
                signal = generate_signal(symbol, candles[: index + 1], self.config)
                if signal is not None:
                    candidates.append(signal)
            candidates.sort(key=lambda signal: signal.score, reverse=True)

            for signal in candidates:
                allowed, _ = can_open(signal, positions, max(balance, 1.0), self.config)
                if not allowed:
                    continue
                position = position_from_signal(signal, max(balance, 1.0), positions, self.config)
                if not _apply_min_units_floor(position, signal, max(balance, 1.0), self.config):
                    continue
                if position.units > 0 and position.dv01 > 0:
                    positions.append(position)
                if len(positions) >= self.config.max_open_positions:
                    break

            marked = balance + sum(_gross_pnl(position, histories[position.canonical][index].close) for position in positions)
            equity_curve.append(marked)

        final_index = min_bars - 1
        for position in list(positions):
            candle = histories[position.canonical][final_index]
            pnl = _pnl(position, candle.close)
            balance += pnl
            trades.append(_trade_from_position(position, candle.time, candle.close, pnl, "final_mark"))

        return build_result(self.config.backtest_initial_balance, balance, trades, equity_curve, self.config.backtest_data_provider)


def _gross_pnl(position: RatesPosition, exit_price: float) -> float:
    return (exit_price - position.entry_price) * position.units * position.direction


def _apply_min_units_floor(position: RatesPosition, signal, equity: float, config: BondsConfig) -> bool:
    """Mirror runtime._apply_minimum_live_units so the backtest reflects live unit constraints."""
    min_units = max(0, int(config.min_live_order_units))
    if min_units <= 0 or position.units >= min_units:
        return True
    if signal.score < config.min_live_unit_score:
        return False
    unit_dv01 = dv01_per_unit(signal.canonical, position.entry_price, config)
    floor_dv01 = unit_dv01 * min_units
    nav_10bp = floor_dv01 * 10.0 / max(equity, 0.0001)
    if nav_10bp > config.max_min_unit_nav_10bp:
        return False
    position.units = float(min_units)
    position.dv01 = floor_dv01
    position.metadata["min_unit_floor_applied"] = True
    return True


def _pnl(position: RatesPosition, exit_price: float) -> float:
    gross = _gross_pnl(position, exit_price)
    notional = abs(position.entry_price * position.units) + abs(exit_price * position.units)
    fees = notional * FEE_RATE
    slippage = abs(exit_price * position.units) * SLIPPAGE_RATE
    return gross - fees - slippage


def _trade_from_position(position: RatesPosition, exit_time, exit_price: float, pnl: float, reason: str) -> Trade:
    stop_risk = max(abs(position.entry_price - position.stop_price) * position.units, 0.0001)
    return Trade(
        canonical=position.canonical,
        side=position.side,
        strategy=position.strategy,
        country=position.country,
        tenor_bucket=position.tenor_bucket,
        entry_time=position.opened_at,
        exit_time=exit_time,
        entry_price=position.entry_price,
        exit_price=exit_price,
        units=position.units,
        dv01=position.dv01,
        pnl=pnl,
        return_r=pnl / stop_risk,
        exit_reason=reason,
    )


def _evaluate_profit_lock(position: RatesPosition, candle, config: BondsConfig) -> tuple[float, str] | None:
    if not config.profit_lock_enabled:
        return None
    metadata = position.metadata if isinstance(position.metadata, dict) else {}
    if metadata is not position.metadata:
        position.metadata = metadata
    armed_before = bool(metadata.get("profit_lock_armed"))
    favorable_price = candle.high if position.direction > 0 else candle.low
    peak_pct = _net_pnl_pct(position, favorable_price)
    if peak_pct is None:
        return None
    previous_peak = _float_or_none(metadata.get("profit_lock_peak_pnl_pct"))
    if previous_peak is None or peak_pct > previous_peak:
        previous_peak = peak_pct
        metadata["profit_lock_peak_pnl_pct"] = peak_pct
        metadata["profit_lock_peak_price"] = favorable_price
    trigger_pct = max(0.0, float(config.profit_lock_trigger_pct))
    pullback_pct = max(0.0, float(config.profit_lock_pullback_pct))
    if previous_peak < trigger_pct:
        return None
    floor_pct = max(0.0, previous_peak - pullback_pct)
    metadata["profit_lock_armed"] = True
    metadata["profit_lock_floor_pnl_pct"] = floor_pct
    if not armed_before or floor_pct <= 0.0:
        return None
    exit_price = _price_for_net_pnl_pct(position, floor_pct)
    if exit_price is None or exit_price <= 0:
        return None
    if position.direction > 0 and candle.low <= exit_price:
        return exit_price, "peak_pullback_profit_lock"
    if position.direction < 0 and candle.high >= exit_price:
        return exit_price, "peak_pullback_profit_lock"
    return None


def _net_pnl_pct(position: RatesPosition, price: float) -> float | None:
    budget = _profit_lock_budget(position)
    if budget <= 0:
        return None
    return _pnl(position, price) / budget * 100.0


def _profit_lock_budget(position: RatesPosition) -> float:
    return max(abs(position.entry_price - position.stop_price) * position.units, 0.0001)


def _price_for_net_pnl_pct(position: RatesPosition, pnl_pct: float) -> float | None:
    units = abs(float(position.units))
    if units <= 0:
        return None
    target_pnl = max(0.0, float(pnl_pct)) / 100.0 * _profit_lock_budget(position)
    entry = float(position.entry_price)
    if position.direction > 0:
        denominator = 1.0 - FEE_RATE - SLIPPAGE_RATE
        if denominator <= 0:
            return None
        return (target_pnl / units + entry * (1.0 + FEE_RATE)) / denominator
    denominator = 1.0 + FEE_RATE + SLIPPAGE_RATE
    return (entry * (1.0 - FEE_RATE) - target_pnl / units) / denominator


def _float_or_none(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_result(initial_balance: float, final_balance: float, trades: list[Trade], equity_curve: list[float], data_provider: str) -> BacktestResult:
    total_pnl = final_balance - initial_balance
    wins = sum(1 for trade in trades if trade.pnl > 0)
    losses = sum(1 for trade in trades if trade.pnl <= 0)
    gross_profit = sum(trade.pnl for trade in trades if trade.pnl > 0)
    gross_loss = abs(sum(trade.pnl for trade in trades if trade.pnl < 0))
    peak = equity_curve[0] if equity_curve else initial_balance
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    by_strategy: dict[str, float] = defaultdict(float)
    by_country: dict[str, float] = defaultdict(float)
    by_tenor_bucket: dict[str, float] = defaultdict(float)
    for trade in trades:
        by_strategy[trade.strategy] += trade.pnl
        by_country[trade.country] += trade.pnl
        by_tenor_bucket[trade.tenor_bucket] += trade.pnl
    return BacktestResult(
        initial_balance=initial_balance,
        final_balance=final_balance,
        total_pnl=total_pnl,
        return_pct=total_pnl / initial_balance if initial_balance else 0.0,
        max_drawdown_pct=max_dd,
        total_trades=len(trades),
        wins=wins,
        losses=losses,
        win_rate=wins / len(trades) if trades else 0.0,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
        data_provider=data_provider,
        by_strategy=dict(by_strategy),
        by_country=dict(by_country),
        by_tenor_bucket=dict(by_tenor_bucket),
        trades=trades,
    )


def write_report(result: BacktestResult, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = asdict(result)
    summary.pop("trades")
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    with (output_dir / "trade_journal.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(result.trades[0]).keys()) if result.trades else ["canonical"])
        writer.writeheader()
        for trade in result.trades:
            writer.writerow(asdict(trade))

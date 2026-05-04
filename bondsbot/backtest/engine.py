from __future__ import annotations

import csv
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from bondsbot.config import BondsConfig
from bondsbot.exits import evaluate_exit
from bondsbot.models import BacktestResult, RatesPosition, Trade
from bondsbot.risk import can_open, position_from_signal
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

from __future__ import annotations

from collections import defaultdict

from bondsbot.config import BondsConfig
from bondsbot.models import RatesPosition, RatesSignal


def max_portfolio_dv01(equity: float, config: BondsConfig) -> float:
    return max(0.0, equity * config.max_portfolio_dv01_nav_10bp / 10.0)


def max_country_dv01(equity: float, config: BondsConfig) -> float:
    return max(0.0, equity * config.max_country_dv01_nav_10bp / 10.0)


def max_tenor_dv01(equity: float, config: BondsConfig) -> float:
    return max(0.0, equity * config.max_tenor_dv01_nav_10bp / 10.0)


def dv01_per_unit(canonical: str, price: float, config: BondsConfig) -> float:
    return max(abs(price) * config.duration_for(canonical) * 0.0001, 0.0001)


def open_dv01(positions: list[RatesPosition]) -> float:
    return sum(abs(position.dv01) for position in positions)


def country_dv01(positions: list[RatesPosition]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for position in positions:
        totals[position.country] += abs(position.dv01)
    return dict(totals)


def tenor_dv01(positions: list[RatesPosition]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for position in positions:
        totals[position.tenor_bucket] += abs(position.dv01)
    return dict(totals)


def target_dv01_for_signal(signal: RatesSignal, positions: list[RatesPosition], equity: float, config: BondsConfig) -> float:
    portfolio_cap = max_portfolio_dv01(equity, config)
    country_cap = max_country_dv01(equity, config)
    tenor_cap = max_tenor_dv01(equity, config)
    remaining_portfolio = max(0.0, portfolio_cap - open_dv01(positions))
    remaining_country = max(0.0, country_cap - country_dv01(positions).get(signal.country, 0.0))
    remaining_tenor = max(0.0, tenor_cap - tenor_dv01(positions).get(signal.tenor_bucket, 0.0))
    base = min(remaining_portfolio, remaining_country, remaining_tenor)
    quality = max(0.35, min(1.15, signal.score / 85.0))
    if signal.tenor_bucket == "30Y":
        quality *= 0.65
    if signal.event_risk == "HIGH":
        quality *= 0.50
    return max(0.0, base * 0.72 * quality)


def can_open(signal: RatesSignal, positions: list[RatesPosition], equity: float, config: BondsConfig) -> tuple[bool, str]:
    if len(positions) >= config.max_open_positions:
        return False, "max_positions"
    if any(position.canonical == signal.canonical for position in positions):
        return False, "canonical_already_open"
    if open_dv01(positions) >= max_portfolio_dv01(equity, config):
        return False, "portfolio_dv01_cap"
    if country_dv01(positions).get(signal.country, 0.0) >= max_country_dv01(equity, config):
        return False, "country_dv01_cap"
    if tenor_dv01(positions).get(signal.tenor_bucket, 0.0) >= max_tenor_dv01(equity, config):
        return False, "tenor_dv01_cap"
    return True, "ok"


def position_from_signal(signal: RatesSignal, equity: float, positions: list[RatesPosition], config: BondsConfig) -> RatesPosition:
    target_dv01 = signal.target_dv01 or target_dv01_for_signal(signal, positions, equity, config)
    per_unit = dv01_per_unit(signal.canonical, signal.entry_price, config)
    units = target_dv01 / per_unit if per_unit > 0 else 0.0
    return RatesPosition(
        canonical=signal.canonical,
        side=signal.side,
        strategy=signal.strategy,
        units=units,
        entry_price=signal.entry_price,
        entry_yield_bps=signal.entry_yield_bps,
        dv01=target_dv01,
        stop_price=signal.stop_price,
        take_profit_price=signal.take_profit_price,
        opened_at=signal.metadata.get("time"),
        country=signal.country,
        tenor_bucket=signal.tenor_bucket,
        metadata=dict(signal.metadata),
    )

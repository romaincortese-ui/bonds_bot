from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class RatesInstrument:
    canonical: str
    broker_symbol: str
    country: str
    tenor_years: float
    price_quote_type: str
    estimated_duration: float
    tick_size: float
    min_units: float


@dataclass(slots=True)
class RateCandle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    yield_bps: float | None = None


@dataclass(slots=True)
class RatesSignal:
    canonical: str
    side: str
    strategy: str
    score: float
    target_dv01: float
    entry_price: float
    entry_yield_bps: float | None
    stop_yield_bps: float | None
    stop_price: float
    take_profit_price: float
    expected_hold_bars: int
    event_risk: str
    country: str
    tenor_bucket: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class RatesPosition:
    canonical: str
    side: str
    strategy: str
    units: float
    entry_price: float
    entry_yield_bps: float | None
    dv01: float
    stop_price: float
    take_profit_price: float
    opened_at: datetime
    country: str
    tenor_bucket: str
    order_id: str = "paper"
    bars_held: int = 0
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def direction(self) -> int:
        return 1 if self.side == "LONG" else -1


@dataclass(slots=True)
class Trade:
    canonical: str
    side: str
    strategy: str
    country: str
    tenor_bucket: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    units: float
    dv01: float
    pnl: float
    return_r: float
    exit_reason: str


@dataclass(slots=True)
class BacktestResult:
    initial_balance: float
    final_balance: float
    total_pnl: float
    return_pct: float
    max_drawdown_pct: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    data_provider: str
    by_strategy: dict[str, float]
    by_country: dict[str, float]
    by_tenor_bucket: dict[str, float]
    trades: list[Trade]

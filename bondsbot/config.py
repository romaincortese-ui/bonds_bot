from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_UNIVERSE = ("US2Y", "US5Y", "US10Y", "US30Y", "DE10Y", "UK10Y")
DEFAULT_STRATEGIES = ("DURATION_TREND", "CARRY_ROLLDOWN", "CURVE_SPREAD", "MACRO_EVENT", "AUCTION_RELIEF")

COUNTRIES = {
    "US2Y": "US",
    "US5Y": "US",
    "US10Y": "US",
    "US30Y": "US",
    "DE10Y": "DE",
    "UK10Y": "UK",
}

TENOR_YEARS = {
    "US2Y": 2.0,
    "US5Y": 5.0,
    "US10Y": 10.0,
    "US30Y": 30.0,
    "DE10Y": 10.0,
    "UK10Y": 10.0,
}

TENOR_BUCKETS = {
    "US2Y": "2Y",
    "US5Y": "5Y",
    "US10Y": "10Y",
    "US30Y": "30Y",
    "DE10Y": "10Y",
    "UK10Y": "10Y",
}

ESTIMATED_DURATIONS = {
    "US2Y": 1.9,
    "US5Y": 4.5,
    "US10Y": 8.4,
    "US30Y": 18.5,
    "DE10Y": 8.8,
    "UK10Y": 8.7,
}

BASE_PRICES = {
    "US2Y": 98.80,
    "US5Y": 97.90,
    "US10Y": 96.70,
    "US30Y": 92.50,
    "DE10Y": 101.80,
    "UK10Y": 95.60,
}

BASE_YIELDS_BPS = {
    "US2Y": 430.0,
    "US5Y": 405.0,
    "US10Y": 395.0,
    "US30Y": 420.0,
    "DE10Y": 250.0,
    "UK10Y": 420.0,
}

OANDA_INSTRUMENT_CANDIDATES = {
    "US2Y": ("USB02Y_USD", "US02Y_USD", "US2Y_USD"),
    "US5Y": ("USB05Y_USD", "US05Y_USD", "US5Y_USD"),
    "US10Y": ("USB10Y_USD", "US10Y_USD"),
    "US30Y": ("USB30Y_USD", "US30Y_USD"),
    "DE10Y": ("DE10YB_EUR", "DE10Y_EUR", "BUND_EUR"),
    "UK10Y": ("UK10YB_GBP", "UK10Y_GBP", "GILT_GBP"),
}


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    values = tuple(part.strip().upper() for part in re.split(r"[,\s]+", raw) if part.strip())
    return values or default


@dataclass(slots=True)
class BondsConfig:
    oanda_env: str
    oanda_account_id: str
    oanda_api_token: str
    paper_trade: bool
    live_trading_enabled: bool
    state_file: Path
    universe: tuple[str, ...]
    strategies: tuple[str, ...]
    scan_interval_seconds: int
    heartbeat_seconds: int
    run_once: bool
    max_open_positions: int
    max_live_orders_per_scan: int
    max_portfolio_dv01_nav_10bp: float
    max_country_dv01_nav_10bp: float
    max_tenor_dv01_nav_10bp: float
    min_live_order_units: int
    min_live_unit_score: float
    max_min_unit_nav_10bp: float
    daily_loss_halt_pct: float
    rolling_dd_throttle_pct: float
    rolling_dd_halt_pct: float
    profit_lock_enabled: bool
    profit_lock_trigger_pct: float
    profit_lock_pullback_pct: float
    cpi_fomc_block_minutes_before: int
    cpi_fomc_block_minutes_after: int
    min_oanda_rates_products: int
    backtest_initial_balance: float
    backtest_days: int
    backtest_data_provider: str
    backtest_output_dir: Path
    fred_api_key: str
    telegram_token: str
    telegram_chat_id: str

    @classmethod
    def from_env(cls) -> "BondsConfig":
        paper_trade = _bool("PAPER_TRADE", True)
        return cls(
            oanda_env=os.environ.get("OANDA_ENV", "practice").strip().lower(),
            oanda_account_id=os.environ.get("OANDA_ACCOUNT_ID", "").strip(),
            oanda_api_token=os.environ.get("OANDA_API_TOKEN", "").strip(),
            paper_trade=paper_trade,
            live_trading_enabled=_bool("LIVE_TRADING_ENABLED", not paper_trade),
            state_file=Path(os.environ.get("BONDS_STATE_FILE", "runtime_state.json")),
            universe=_csv("BONDS_UNIVERSE", DEFAULT_UNIVERSE),
            strategies=_csv("BONDS_STRATEGIES", DEFAULT_STRATEGIES),
            scan_interval_seconds=_int("SCAN_INTERVAL_SECONDS", 300),
            heartbeat_seconds=_int("BONDS_HEARTBEAT_SECONDS", _int("HEARTBEAT_SECONDS", 3600)),
            run_once=_bool("RUN_ONCE", False),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 4),
            max_live_orders_per_scan=_int("MAX_LIVE_ORDERS_PER_SCAN", 1),
            max_portfolio_dv01_nav_10bp=_float("MAX_PORTFOLIO_DV01_NAV_10BP", 0.003125),
            max_country_dv01_nav_10bp=_float("MAX_COUNTRY_DV01_NAV_10BP", 0.001875),
            max_tenor_dv01_nav_10bp=_float("MAX_TENOR_DV01_NAV_10BP", 0.00125),
            min_live_order_units=_int("BONDS_MIN_LIVE_ORDER_UNITS", 1),
            min_live_unit_score=_float("BONDS_MIN_LIVE_UNIT_SCORE", 80.0),
            max_min_unit_nav_10bp=_float("BONDS_MAX_MIN_UNIT_NAV_10BP", 0.0250),
            daily_loss_halt_pct=_float("DAILY_LOSS_HALT_PCT", 0.010),
            rolling_dd_throttle_pct=_float("ROLLING_DD_THROTTLE_PCT", 0.040),
            rolling_dd_halt_pct=_float("ROLLING_DD_HALT_PCT", 0.070),
            profit_lock_enabled=_bool("PROFIT_LOCK_ENABLED", True),
            profit_lock_trigger_pct=_float("PROFIT_LOCK_TRIGGER_PCT", 15.0),
            profit_lock_pullback_pct=_float("PROFIT_LOCK_PULLBACK_PCT", 2.0),
            cpi_fomc_block_minutes_before=_int("CPI_FOMC_BLOCK_MINUTES_BEFORE", 30),
            cpi_fomc_block_minutes_after=_int("CPI_FOMC_BLOCK_MINUTES_AFTER", 10),
            min_oanda_rates_products=_int("MIN_OANDA_RATES_PRODUCTS", 4),
            backtest_initial_balance=_float("BACKTEST_INITIAL_BALANCE", 10000.0),
            backtest_days=_int("BACKTEST_DAYS", 30),
            backtest_data_provider=os.environ.get("BACKTEST_DATA_PROVIDER", "fixture").strip().lower(),
            backtest_output_dir=Path(os.environ.get("BACKTEST_OUTPUT_DIR", "backtest_output")),
            fred_api_key=os.environ.get("FRED_API_KEY", "").strip(),
            telegram_token=os.environ.get("TELEGRAM_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        )

    @property
    def oanda_base_url(self) -> str:
        if self.oanda_env == "live":
            return "https://api-fxtrade.oanda.com"
        return "https://api-fxpractice.oanda.com"

    @property
    def has_oanda_credentials(self) -> bool:
        return bool(self.oanda_account_id and self.oanda_api_token)

    def oanda_candidates_for(self, canonical: str) -> tuple[str, ...]:
        canonical = canonical.upper()
        override = os.environ.get(f"OANDA_INSTRUMENT_{canonical}", "").strip().upper()
        defaults = OANDA_INSTRUMENT_CANDIDATES.get(canonical, ())
        return (override, *defaults) if override else defaults

    def oanda_instrument_for(self, canonical: str, available: set[str] | None = None) -> str:
        candidates = self.oanda_candidates_for(canonical)
        if available is None:
            return candidates[0] if candidates else ""
        for candidate in candidates:
            if candidate in available:
                return candidate
        return ""

    def duration_for(self, canonical: str) -> float:
        return ESTIMATED_DURATIONS.get(canonical.upper(), 7.0)

    def country_for(self, canonical: str) -> str:
        return COUNTRIES.get(canonical.upper(), "OTHER")

    def tenor_bucket_for(self, canonical: str) -> str:
        return TENOR_BUCKETS.get(canonical.upper(), "OTHER")

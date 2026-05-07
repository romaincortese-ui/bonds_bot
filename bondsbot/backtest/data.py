from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from bondsbot.config import BASE_PRICES, BASE_YIELDS_BPS, BondsConfig
from bondsbot.models import RateCandle
from bondsbot.oanda_client import OandaClient


class FixtureRatesDataProvider:
    def __init__(self, days: int = 120) -> None:
        self.days = days

    def history(self, canonical: str) -> list[RateCandle]:
        return _generate(canonical.upper(), self.days)


class OandaRatesBacktestDataProvider:
    def __init__(self, config: BondsConfig, days: int = 120) -> None:
        self.config = config
        self.days = days
        self.client = OandaClient(config)
        self.fixture = FixtureRatesDataProvider(days=days)
        self.provider_by_canonical: dict[str, str] = {}
        self.failures: list[str] = []
        self._tradeable: set[str] | None = None

    def history(self, canonical: str) -> list[RateCandle]:
        canonical = canonical.upper()
        instrument = self._instrument_for(canonical)
        if instrument and self.config.has_oanda_credentials:
            try:
                candles = self.client.candles(instrument, count=self.days, granularity="D")
                if len(candles) >= 45:
                    self.provider_by_canonical[canonical] = f"oanda:{instrument}"
                    return candles
                self.failures.append(f"{canonical}:too_few_oanda_candles")
            except RuntimeError as exc:
                self.failures.append(f"{canonical}:{str(exc)[:120]}")
        self.provider_by_canonical[canonical] = "fixture"
        return self.fixture.history(canonical)

    def _instrument_for(self, canonical: str) -> str:
        if not self.config.has_oanda_credentials:
            return ""
        if self._tradeable is None:
            try:
                self._tradeable = set(self.client.tradeable_instruments())
            except RuntimeError as exc:
                self.failures.append(f"instruments:{str(exc)[:120]}")
                self._tradeable = set()
        return self.config.oanda_instrument_for(canonical, self._tradeable)

    @property
    def provider_counts(self) -> dict[str, int]:
        counts = {"oanda": 0, "fixture": 0}
        for provider in self.provider_by_canonical.values():
            if provider.startswith("oanda:"):
                counts["oanda"] += 1
            else:
                counts["fixture"] += 1
        return counts


def _daily_drift(canonical: str, index: int) -> float:
    base = {
        "US2Y": -0.00080,
        "US5Y": 0.00175,
        "US10Y": 0.00200,
        "US30Y": 0.00135,
        "DE10Y": 0.00155,
        "UK10Y": -0.00155,
    }.get(canonical, 0.001)
    if index < 45:
        return base * 0.35
    if index < 55:
        return -base * 0.45
    return base * 1.10


def _generate(canonical: str, days: int) -> list[RateCandle]:
    base_price = BASE_PRICES.get(canonical, 100.0)
    base_yield = BASE_YIELDS_BPS.get(canonical, 350.0)
    duration = max(1.0, {"US2Y": 1.9, "US5Y": 4.5, "US10Y": 8.4, "US30Y": 18.5, "DE10Y": 8.8, "UK10Y": 8.7}.get(canonical, 7.0))
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)
    price = base_price
    candles: list[RateCandle] = []
    for index in range(days):
        noise = math.sin(index * 0.47 + len(canonical)) * 0.00045 + math.cos(index * 0.19) * 0.00025
        daily_return = _daily_drift(canonical, index) + noise
        open_price = price
        close = max(20.0, open_price * (1.0 + daily_return))
        range_size = max(open_price * (0.0022 + abs(noise) * 1.4), 0.05)
        high = max(open_price, close) + range_size * 0.45
        low = min(open_price, close) - range_size * 0.45
        price = close
        yield_change = -((close - base_price) / base_price) / duration * 10000.0
        yield_bps = base_yield + yield_change
        candles.append(
            RateCandle(
                time=start + timedelta(days=index + 1),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=1000.0 + index * 7.0,
                yield_bps=yield_bps,
            )
        )
    return candles

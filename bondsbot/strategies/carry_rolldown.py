from __future__ import annotations

from bondsbot.config import BondsConfig
from bondsbot.indicators import clamp, momentum, stdev
from bondsbot.models import RateCandle, RatesSignal
from bondsbot.strategies.common import signal_from_price_move


def carry_rolldown_signal(canonical: str, candles: list[RateCandle], config: BondsConfig) -> RatesSignal | None:
    if len(candles) < 60 or config.tenor_bucket_for(canonical) not in {"5Y", "10Y"}:
        return None
    closes = [candle.close for candle in candles]
    returns = [(closes[index] - closes[index - 1]) / closes[index - 1] for index in range(1, len(closes)) if closes[index - 1] != 0]
    vol = stdev(returns, 30)
    trend = momentum(closes, 30)
    latest_yield = candles[-1].yield_bps or 0.0
    if vol > 0.004 or trend < -0.001 or latest_yield <= 150.0:
        return None
    score = 67.0 + clamp((0.004 - vol) * 1800.0, 0.0, 10.0) + clamp(trend * 350.0, 0.0, 8.0)
    if score < 72.0:
        return None
    return signal_from_price_move(
        canonical=canonical,
        candles=candles,
        config=config,
        side="LONG",
        score=score,
        strategy="CARRY_ROLLDOWN",
        stop_multiple=1.25,
        target_multiple=1.25,
        expected_hold_bars=22,
        event_risk="LOW",
        metadata={"carry_yield_bps": round(latest_yield, 2), "realized_vol_30": round(vol, 6)},
    )

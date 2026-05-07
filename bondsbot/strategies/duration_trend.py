from __future__ import annotations

from bondsbot.config import BondsConfig
from bondsbot.indicators import clamp, momentum, sma, slope
from bondsbot.models import RateCandle, RatesSignal
from bondsbot.strategies.common import signal_from_price_move


def duration_trend_signal(canonical: str, candles: list[RateCandle], config: BondsConfig) -> RatesSignal | None:
    if len(candles) < 45:
        return None
    closes = [candle.close for candle in candles]
    yields = [candle.yield_bps for candle in candles if candle.yield_bps is not None]
    fast = sma(closes, 12)
    slow = sma(closes, 35)
    price_momentum = momentum(closes, 20)
    price_slope = slope(closes, 12)
    yield_slope = slope(yields, 12) if len(yields) >= 20 else -price_slope * 10.0
    trend_strength = abs((fast - slow) / max(slow, 0.0001)) + abs(price_momentum)
    if fast > slow and price_momentum > 0.003 and yield_slope <= 0.25:
        side = "LONG"
    elif fast < slow and price_momentum < -0.003 and yield_slope >= -0.25:
        side = "SHORT"
    else:
        return None
    score = 70.0 + clamp(trend_strength * 1200.0, 0.0, 22.0)
    if canonical.endswith("30Y"):
        score -= 4.0
    if score < 72.0:
        return None
    price_only_broker_candles = candles[-1].yield_bps is None
    if price_only_broker_candles:
        if canonical not in {"US5Y", "US10Y"} or score < 78.0:
            return None
        fade_side = "LONG" if side == "SHORT" else "SHORT"
        return signal_from_price_move(
            canonical=canonical,
            candles=candles,
            config=config,
            side=fade_side,
            score=score,
            strategy="BROKER_DURATION_FADE",
            stop_multiple=1.10,
            target_multiple=1.70,
            expected_hold_bars=15,
            metadata={
                "price_momentum_20": round(price_momentum, 5),
                "yield_slope_12_bps": round(yield_slope, 4),
                "original_duration_side": side,
                "broker_price_only": True,
            },
        )
    return signal_from_price_move(
        canonical=canonical,
        candles=candles,
        config=config,
        side=side,
        score=score,
        strategy="DURATION_TREND",
        stop_multiple=1.10,
        target_multiple=1.70,
        expected_hold_bars=15,
        metadata={"price_momentum_20": round(price_momentum, 5), "yield_slope_12_bps": round(yield_slope, 4)},
    )

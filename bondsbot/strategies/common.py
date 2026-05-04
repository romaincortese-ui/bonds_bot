from __future__ import annotations

from bondsbot.config import BondsConfig
from bondsbot.indicators import atr
from bondsbot.models import RateCandle, RatesSignal


def price_atr(candles: list[RateCandle], period: int = 20) -> float:
    highs = [candle.high for candle in candles]
    lows = [candle.low for candle in candles]
    closes = [candle.close for candle in candles]
    return atr(highs, lows, closes, period)


def signal_from_price_move(
    canonical: str,
    candles: list[RateCandle],
    config: BondsConfig,
    side: str,
    score: float,
    strategy: str,
    stop_multiple: float,
    target_multiple: float,
    expected_hold_bars: int,
    event_risk: str = "NORMAL",
    metadata: dict[str, object] | None = None,
) -> RatesSignal:
    last = candles[-1]
    move_atr = max(price_atr(candles), last.close * 0.0018)
    stop_distance = move_atr * stop_multiple
    target_distance = stop_distance * target_multiple
    if side == "LONG":
        stop_price = last.close - stop_distance
        take_profit = last.close + target_distance
        stop_yield = None if last.yield_bps is None else last.yield_bps + (stop_distance / max(last.close, 0.0001)) / config.duration_for(canonical) * 10000.0
    else:
        stop_price = last.close + stop_distance
        take_profit = last.close - target_distance
        stop_yield = None if last.yield_bps is None else last.yield_bps - (stop_distance / max(last.close, 0.0001)) / config.duration_for(canonical) * 10000.0
    payload = {"time": last.time, "expected_hold_bars": expected_hold_bars}
    if metadata:
        payload.update(metadata)
    return RatesSignal(
        canonical=canonical,
        side=side,
        strategy=strategy,
        score=score,
        target_dv01=0.0,
        entry_price=last.close,
        entry_yield_bps=last.yield_bps,
        stop_yield_bps=stop_yield,
        stop_price=stop_price,
        take_profit_price=take_profit,
        expected_hold_bars=expected_hold_bars,
        event_risk=event_risk,
        country=config.country_for(canonical),
        tenor_bucket=config.tenor_bucket_for(canonical),
        metadata=payload,
    )

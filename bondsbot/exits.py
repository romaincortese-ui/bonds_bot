from __future__ import annotations

from bondsbot.models import RateCandle, RatesPosition


def evaluate_exit(position: RatesPosition, candle: RateCandle) -> tuple[bool, float, str]:
    direction = position.direction
    position.bars_held += 1
    max_hold = int(position.metadata.get("expected_hold_bars", 15))
    if direction > 0:
        if candle.low <= position.stop_price:
            return True, position.stop_price, "yield_stop"
        if candle.high >= position.take_profit_price:
            return True, position.take_profit_price, "take_profit"
    else:
        if candle.high >= position.stop_price:
            return True, position.stop_price, "yield_stop"
        if candle.low <= position.take_profit_price:
            return True, position.take_profit_price, "take_profit"
    if position.bars_held >= max_hold:
        return True, candle.close, "time_stop"
    return False, candle.close, "hold"

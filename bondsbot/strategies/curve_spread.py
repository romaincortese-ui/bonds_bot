from __future__ import annotations

from bondsbot.models import RateCandle


def curve_spread_bps(front: list[RateCandle], long: list[RateCandle]) -> float | None:
    if not front or not long or front[-1].yield_bps is None or long[-1].yield_bps is None:
        return None
    return long[-1].yield_bps - front[-1].yield_bps

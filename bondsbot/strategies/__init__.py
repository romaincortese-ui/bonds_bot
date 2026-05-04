from __future__ import annotations

from bondsbot.config import BondsConfig
from bondsbot.models import RateCandle, RatesSignal
from bondsbot.strategies.carry_rolldown import carry_rolldown_signal
from bondsbot.strategies.duration_trend import duration_trend_signal


def generate_signal(canonical: str, candles: list[RateCandle], config: BondsConfig) -> RatesSignal | None:
    candidates = []
    if "DURATION_TREND" in config.strategies:
        signal = duration_trend_signal(canonical, candles, config)
        if signal is not None:
            candidates.append(signal)
    if "CARRY_ROLLDOWN" in config.strategies:
        signal = carry_rolldown_signal(canonical, candles, config)
        if signal is not None:
            candidates.append(signal)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates[0]

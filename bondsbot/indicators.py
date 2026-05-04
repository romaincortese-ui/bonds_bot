from __future__ import annotations

import math


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def sma(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    window = values[-period:] if len(values) >= period else values
    return sum(window) / len(window)


def stdev(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    window = values[-period:] if len(values) >= period else values
    if len(window) < 2:
        return 0.0
    mean = sum(window) / len(window)
    variance = sum((value - mean) ** 2 for value in window) / (len(window) - 1)
    return math.sqrt(variance)


def atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> float:
    if len(closes) < 2:
        return 0.0
    true_ranges = []
    for index in range(1, len(closes)):
        true_ranges.append(max(highs[index] - lows[index], abs(highs[index] - closes[index - 1]), abs(lows[index] - closes[index - 1])))
    return sma(true_ranges, period)


def momentum(values: list[float], period: int) -> float:
    if len(values) <= period:
        return 0.0
    prior = values[-period - 1]
    if prior == 0:
        return 0.0
    return (values[-1] - prior) / abs(prior)


def slope(values: list[float], period: int) -> float:
    if len(values) <= period:
        return 0.0
    return (values[-1] - values[-period - 1]) / period

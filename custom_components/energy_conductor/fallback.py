"""Seasonal fallback estimate when no forecast is available.

Cosine over day-of-year between two configurable bounds. Honest about
what's known (sun angle is deterministic) vs what isn't (cloud cover) —
the value returned is *pessimistic*, not expected.
"""

from __future__ import annotations

import math
from datetime import datetime

NORTHERN_PEAK_DAY = 172  # summer solstice (June 21, day-of-year)
SOUTHERN_PEAK_DAY = 355  # December 21


def seasonal_fallback_kwh(
    now: datetime,
    winter_min: float,
    summer_max: float,
    *,
    southern_hemisphere: bool = False,
) -> float:
    """Return today's pessimistic solar fallback in kWh.

    Computes a cosine between `winter_min` and `summer_max` peaking on the
    relevant solstice. `now` must be timezone-aware; only its date is used.
    """
    if now.tzinfo is None:
        raise ValueError("seasonal_fallback_kwh requires a timezone-aware datetime")
    peak_day = SOUTHERN_PEAK_DAY if southern_hemisphere else NORTHERN_PEAK_DAY
    day_of_year = now.timetuple().tm_yday
    phase = math.cos(2 * math.pi * (day_of_year - peak_day) / 365)  # -1..+1
    normalised = (phase + 1) / 2  # 0..1
    return winter_min + (summer_max - winter_min) * normalised


def forecast_implausible(total_kwh: float, summer_max_kwh: float, *, margin: float) -> bool:
    """True if a forecast total exceeds the physical-plausibility ceiling.

    The ceiling is the configured summer-max generation times a margin (headroom
    for an exceptional clear day). Exceeding it almost certainly indicates a unit
    error in the forecast source (e.g. kW interpreted as kWh) rather than real
    generation. A summer_max of 0 disables the check (returns False).
    """
    if summer_max_kwh <= 0:
        return False
    return total_kwh > summer_max_kwh * margin

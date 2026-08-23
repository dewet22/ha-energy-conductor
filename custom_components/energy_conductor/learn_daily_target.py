"""Learned daily-kWh target from historical house-energy totals.

Rather than rely on a static configured value for the "calculated daily
target" diagnostic sensor (and the hot-water depletion estimate, which uses
this same helper against its own history), we learn it from the past N days
of actual house energy: the percentile of daily totals robust to one
anomalous day (party, EV cabin warm-up, day away).
"""

from __future__ import annotations

import math
import statistics


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def learned_daily_kwh(
    daily_totals: list[float], *, percentile: float, min_samples: int
) -> float | None:
    """Percentile of daily kWh totals, or None if too little data.

    Drops non-finite and negative values defensively (a day's consumption is
    >= 0; negatives can come from recorder meter-reset diffs). Returns None
    when fewer than `min_samples` usable values remain, so the caller can fall
    back to a static default.
    """
    usable = [float(v) for v in daily_totals if _is_finite_number(v) and v >= 0]
    if len(usable) < min_samples:
        return None
    return statistics.quantiles(usable, n=100)[int(percentile * 100) - 1]

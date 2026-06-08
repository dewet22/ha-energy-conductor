"""Hot-water reserve estimate for a solar diverter (myenergi Eddi).

Open-loop energy balance for a hot-water cylinder with no temperature sensor. The
estimate is anchored by the diverter's "Max temp reached" status (ground-truth full):
reserve snaps to capacity at each full event, then depletes by a lumped daily rate
(standing loss + draw) and is topped up by measured green diversion since. Because the
full anchor recurs whenever solar (or a boost) refills the tank, drift is bounded to the
length of a cloudy spell.

Pure functions only — no Home Assistant imports. The adapter supplies the recorder-derived
inputs (last full timestamp, green energy since, learned depletion).
"""

from __future__ import annotations

import math

from .learn_daily_target import learned_daily_kwh


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def estimate_reserve(
    *,
    elapsed_hours_since_full: float,
    energy_in_since_full_kwh: float,
    depletion_kwh_per_day: float,
    capacity_kwh: float,
) -> float:
    """Estimated usable energy in the tank (kWh), clamped to [0, capacity].

    At the last "Max temp reached" the tank was full (= capacity). Since then it has lost
    `depletion_kwh_per_day` per day and gained `energy_in_since_full_kwh` of green diversion.
    The clamp at capacity absorbs diversion the full tank would have rejected.
    """
    elapsed_days = max(0.0, elapsed_hours_since_full) / 24.0
    reserve = capacity_kwh - depletion_kwh_per_day * elapsed_days + energy_in_since_full_kwh
    return _clamp(reserve, 0.0, capacity_kwh)


def learn_depletion(
    steady_day_green_totals: list[float],
    *,
    percentile: float,
    min_samples: int,
    fallback: float,
) -> tuple[float, str]:
    """Lumped daily depletion (kWh/day) and its source.

    A "steady" day is one bracketed by "Max temp reached" at both ends (tank full→full):
    net tank energy change ≈ 0, so that day's green diversion ≈ the day's depletion
    (standing loss + draw). The percentile is robust to one anomalous day. Falls back to
    the configured constant when fewer than `min_samples` steady days are available.
    """
    value = learned_daily_kwh(
        steady_day_green_totals, percentile=percentile, min_samples=min_samples
    )
    if value is None:
        return fallback, "default"
    return value, "stats"


def boost_recommendation(
    *,
    reserve_kwh: float,
    capacity_kwh: float,
    threshold_percent: float,
    expected_refill_kwh: float,
    depletion_kwh_per_day: float,
    heater_kw: float,
    min_hours: int,
    max_hours: int,
) -> tuple[bool, float | None]:
    """Whether to prompt a manual boost, and for how many hours.

    Projects the reserve one day forward (lose a day's depletion, gain the expected solar
    refill). If that stays at or above the comfort threshold, no prompt. Otherwise the
    suggested duration tops the projected reserve back up to the threshold at the heater's
    power, clamped to [min_hours, max_hours] — a top-up to ride out the spell, not a full refill.
    """
    threshold_kwh = capacity_kwh * threshold_percent / 100.0
    projected_kwh = reserve_kwh - depletion_kwh_per_day + expected_refill_kwh
    if projected_kwh >= threshold_kwh:
        return False, None
    deficit_kwh = threshold_kwh - projected_kwh
    hours = math.ceil(deficit_kwh / heater_kw) if heater_kw > 0 else max_hours
    return True, float(_clamp(hours, min_hours, max_hours))

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
from datetime import datetime, timedelta

from .learn_daily_target import learned_daily_kwh


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def corroborated_full_events(
    full_events: list[datetime],
    diversion_by_hour: dict[datetime, float],
    *,
    window_hours: int,
    min_kwh: float,
) -> list[datetime]:
    """The 'Max temp reached' events with real diversion in the hours leading up to them.

    A genuine full is the terminus of an active diversion session, so meaningful green
    energy flowed in the window ending at the event. The diverter's false 'Max temp
    reached' — element isolated at the safety switch (open circuit, no current draw) or
    the status republished on a reconnect/restart — carries zero diversion in that window
    and must not anchor the reserve. ``diversion_by_hour`` maps each hour-bucket start to
    that hour's diverted kWh; an event is corroborated when the buckets covering its hour
    and the ``window_hours - 1`` preceding hours sum to at least ``min_kwh``. The adapter
    takes the latest of these as the reserve anchor (a later false event never overrides an
    earlier genuine one) and their dates for steady-day depletion learning.
    """
    corroborated: list[datetime] = []
    for event in full_events:
        bucket = event.replace(minute=0, second=0, microsecond=0)
        recent_kwh = sum(
            diversion_by_hour.get(bucket - timedelta(hours=i), 0.0) for i in range(window_hours)
        )
        if recent_kwh >= min_kwh:
            corroborated.append(event)
    return corroborated


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

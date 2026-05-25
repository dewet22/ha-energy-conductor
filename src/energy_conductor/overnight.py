"""Overnight charge target planning. Pure function, runs once per evening."""

from __future__ import annotations

from energy_conductor.decisions import Decision, DecisionKind
from energy_conductor.model import SiteState

MEANINGFUL_SLOT_KWH = 0.25  # 500W average over a 30min slot
MORNING_GAP_CAP_H = 6  # absolute cap on morning_gap_hours
MISSING_FORECAST_GAP_H = 4  # default gap when no forecast slots are present


def _first_meaningful_slot_start(state: SiteState):
    for slot in state.solar_forecast.slots:
        if slot.energy_kwh >= MEANINGFUL_SLOT_KWH:
            return slot.start
    return None


def _morning_gap_hours(state: SiteState) -> float:
    """Hours between cheap_window_end and first meaningful solar, clamped to [0, cap]."""
    if not state.solar_forecast.slots:
        return float(MISSING_FORECAST_GAP_H)
    if state.tariff.cheap_window_end is None:
        return float(MISSING_FORECAST_GAP_H)
    first = _first_meaningful_slot_start(state)
    if first is None:
        return float(MORNING_GAP_CAP_H)
    delta = first - state.tariff.cheap_window_end
    hours = max(0.0, delta.total_seconds() / 3600)
    return min(hours, float(MORNING_GAP_CAP_H))


def plan_overnight(
    state: SiteState,
    *,
    target_entity: str,
    daily_kwh_target: float,
) -> Decision:
    """Compute the overnight battery charge target."""
    morning_gap_hours = _morning_gap_hours(state)
    morning_gap_kwh = state.baseline_load_w * morning_gap_hours / 1000.0

    forecast_kwh = state.solar_forecast.total_kwh_today
    forecast_deficit = max(0.0, daily_kwh_target - forecast_kwh)

    target_kwh = morning_gap_kwh + forecast_deficit
    raw_percent = round(target_kwh / state.battery.capacity_kwh * 100)
    target_percent = max(int(state.battery.reserve_percent), min(raw_percent, 100))

    is_fallback = not state.solar_forecast.slots
    fallback_note = (
        f", fallback {state.solar_forecast.fallback_source}"
        if is_fallback and state.solar_forecast.fallback_source
        else ""
    )
    reason = (
        f"Morning gap {morning_gap_hours:.1f}h x {state.baseline_load_w:.0f}W "
        f"= {morning_gap_kwh:.1f} kWh; forecast {forecast_kwh:.1f} kWh; "
        f"target {target_percent}%{fallback_note}"
    )

    plan_date = state.now.date().isoformat()
    return Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity=target_entity,
        value=target_percent,
        reason=reason,
        dedupe_key=f"overnight-{plan_date}-{target_percent}",
    )

"""Overnight charge target planning. Pure function, runs once per evening."""

from __future__ import annotations

from .decisions import Decision, DecisionKind
from .model import SiteState

MEANINGFUL_SLOT_KWH = 0.25  # 500W average over a 30min slot
MORNING_GAP_CAP_H = 6  # absolute cap on morning_gap_hours
MISSING_FORECAST_GAP_H = 4  # default gap when no forecast slots are present


def _first_meaningful_slot_start(state: SiteState):
    starts = [
        slot.start for slot in state.solar_forecast.slots if slot.energy_kwh >= MEANINGFUL_SLOT_KWH
    ]
    return min(starts, default=None)


def _morning_gap_hours(state: SiteState) -> float:
    """Hours between off_peak_window_end and first meaningful solar, clamped to [0, cap]."""
    if not state.solar_forecast.slots:
        return float(MISSING_FORECAST_GAP_H)
    if state.tariff.off_peak_window_end is None:
        return float(MISSING_FORECAST_GAP_H)
    first = _first_meaningful_slot_start(state)
    if first is None:
        return float(MORNING_GAP_CAP_H)
    delta = first - state.tariff.off_peak_window_end
    hours = max(0.0, delta.total_seconds() / 3600)
    return min(hours, float(MORNING_GAP_CAP_H))


def plan_overnight(
    state: SiteState,
    *,
    target_entity: str,
    daily_kwh_target: float,
    min_target_soc_percent: float,
) -> Decision:
    """Compute the overnight battery charge target.

    The energy the battery must hold by morning (`target_kwh`) is *usable* energy:
    it sits ABOVE the reserve floor, because everything at or below the reserve is
    unavailable to the load. So the SoC target is `reserve + target_kwh/capacity`,
    not just `target_kwh/capacity` — the earlier formula under-provisioned by the
    reserve band (e.g. a 6% target left only 2% usable above a 4% floor).

    `min_target_soc_percent` is an absolute floor that doubles as the safety margin:
    it guards against forecast/baseline error and against BMS SoC unreliability near
    empty (the inverter can cut out above the nominal reserve, and the SoC reading is
    least trustworthy at the bottom). The final target is clamped up to the highest of
    {min target, reserve, computed}.
    """
    morning_gap_hours = _morning_gap_hours(state)
    morning_gap_kwh = state.baseline_load_w * morning_gap_hours / 1000.0

    forecast_kwh = state.solar_forecast.total_kwh_forecast
    forecast_deficit = max(0.0, daily_kwh_target - forecast_kwh)

    reserve_percent = float(state.battery.reserve_percent)
    # Usable energy needed, expressed as a percentage band ABOVE the reserve floor.
    usable_kwh = morning_gap_kwh + forecast_deficit
    usable_percent = usable_kwh / state.battery.capacity_kwh * 100
    computed_percent = reserve_percent + usable_percent

    target_percent = round(
        min(100.0, max(min_target_soc_percent, reserve_percent, computed_percent))
    )

    # Safety check: if the live BMS reserve floor sits above the user's intended
    # minimum target, the inverter will not supply down to where we planned — the
    # intent is misconfigured. Surface it (the math already clamps up correctly).
    bms_floor_note = ""
    if reserve_percent > min_target_soc_percent:
        bms_floor_note = (
            f"; WARNING BMS reserve {reserve_percent:.0f}% exceeds min-target "
            f"{min_target_soc_percent:.0f}% (raise min-target or lower BMS reserve)"
        )

    is_fallback = not state.solar_forecast.slots
    fallback_note = (
        f", fallback {state.solar_forecast.fallback_source or 'unknown'}" if is_fallback else ""
    )
    reason = (
        f"Morning gap {morning_gap_hours:.1f}h x {state.baseline_load_w:.0f}W "
        f"= {morning_gap_kwh:.1f} kWh; forecast {forecast_kwh:.1f} kWh; "
        f"reserve {reserve_percent:.0f}% + usable {usable_percent:.0f}% "
        f"-> target {target_percent}%{fallback_note}{bms_floor_note}"
    )

    plan_date = state.now.date().isoformat()
    return Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity=target_entity,
        value=target_percent,
        reason=reason,
        dedupe_key=f"overnight-{plan_date}-{target_percent}",
    )

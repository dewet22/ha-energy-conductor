"""Overnight charge target planning. Pure function, runs once per evening."""

from __future__ import annotations

from .const import MIN_OVERNIGHT_USABLE_KWH
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
) -> Decision:
    """Compute the overnight battery charge target.

    The energy the battery must hold by morning is *usable* energy: it sits ABOVE
    the reserve floor, because everything at or below the reserve is unavailable to
    the load. So the SoC target is `reserve + usable/capacity`, not just
    `usable/capacity` — a bare gap target would under-provision by the reserve band.

    `usable` is the larger of what the morning actually needs (gap + forecast deficit)
    and `MIN_OVERNIGHT_USABLE_KWH` — a baked-in safety margin that guards against
    forecast/baseline error and against BMS SoC unreliability near empty (the inverter
    can cut out above the nominal reserve, and the SoC reading is least trustworthy at
    the bottom). The margin only binds on the sunniest, smallest-gap nights.
    """
    morning_gap_hours = _morning_gap_hours(state)
    morning_gap_kwh = state.baseline_load_w * morning_gap_hours / 1000.0

    forecast_kwh = state.solar_forecast.total_kwh_forecast
    forecast_deficit = max(0.0, daily_kwh_target - forecast_kwh)

    reserve_percent = float(state.battery.reserve_percent)
    # Usable energy needed, floored at the baked-in safety margin, expressed as a
    # percentage band ABOVE the reserve floor.
    usable_kwh = max(morning_gap_kwh + forecast_deficit, MIN_OVERNIGHT_USABLE_KWH)
    usable_percent = usable_kwh / state.battery.capacity_kwh * 100

    target_percent = round(min(100.0, reserve_percent + usable_percent))

    is_fallback = not state.solar_forecast.slots
    fallback_note = (
        f", fallback {state.solar_forecast.fallback_source or 'unknown'}" if is_fallback else ""
    )

    dawn_note = ""
    if state.tariff.off_peak_window_end is not None:
        hours_until_dawn = max(
            0.0, (state.tariff.off_peak_window_end - state.now).total_seconds() / 3600
        )
        # The discharge guard idles the battery once off-peak begins, so it only
        # drains at baseline until the window STARTS, then holds until dawn. With
        # no known start — or a stale one in the past — assume full drain to dawn,
        # the conservative direction (under-projects dawn SoC, so the note is
        # suppressed rather than wrong). Known caveat: the off-peak sensor also
        # covers short Intelligent dispatch slots, so an early dispatch can stop
        # the projected drain before the main window resumes it; the overstatement
        # is bounded by the inter-slot gap (an hour or two of baseline, a few
        # percent SoC) and this note is advisory-only — it never changes the
        # written target. Proper interval data arrives with the SoC-setpoint
        # redesign.
        hours_discharging = hours_until_dawn
        if state.tariff.off_peak_now:
            hours_discharging = 0.0
        elif (
            state.tariff.next_off_peak_window_start is not None
            and state.tariff.next_off_peak_window_start >= state.now
        ):
            hours_discharging = min(
                hours_until_dawn,
                (state.tariff.next_off_peak_window_start - state.now).total_seconds() / 3600,
            )
        discharge_pct = state.baseline_load_w * hours_discharging / state.battery.capacity_kwh / 10
        expected_soc = round(state.battery.soc_percent - discharge_pct)
        if expected_soc > target_percent:
            dawn_note = f"; battery on track for ~{expected_soc}% at dawn — no charge needed"

    reason = (
        f"Morning gap {morning_gap_hours:.1f}h x {state.baseline_load_w:.0f}W "
        f"= {morning_gap_kwh:.1f} kWh; forecast {forecast_kwh:.1f} kWh; "
        f"reserve {reserve_percent:.0f}% + usable {usable_percent:.0f}% "
        f"-> target {target_percent}%{fallback_note}{dawn_note}"
    )

    plan_date = state.now.date().isoformat()
    return Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity=target_entity,
        value=target_percent,
        reason=reason,
        dedupe_key=f"overnight-{plan_date}-{target_percent}",
    )

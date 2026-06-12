"""Overnight charge target planning. Pure function, runs once per evening."""

from __future__ import annotations

from datetime import datetime, timedelta

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
    """Hours between the overnight boundary and first meaningful solar, clamped to [0, cap].

    The boundary prefers the CONFIGURED overnight end: the off-peak sensor's period
    end can belong to a short Intelligent dispatch slot (e.g. ending 22:30), which
    would inflate the gap to the cap and substantially over-provision the written
    target. Falls back to the sensor-derived end when no configured boundary exists.
    """
    if not state.solar_forecast.slots:
        return float(MISSING_FORECAST_GAP_H)
    boundary = state.tariff.overnight_window_end or state.tariff.off_peak_window_end
    if boundary is None:
        return float(MISSING_FORECAST_GAP_H)
    first = _first_meaningful_slot_start(state)
    if first is None:
        return float(MORNING_GAP_CAP_H)
    delta = first - boundary
    hours = max(0.0, delta.total_seconds() / 3600)
    return min(hours, float(MORNING_GAP_CAP_H))


def _is_off_peak_at(state: SiteState, t: datetime) -> bool:
    """Whether `t` falls in the overnight charging window.

    Uses the configured overnight boundary rather than the sensor-derived
    off_peak_window_end, which can be a short Intelligent dispatch slot.
    """
    end = state.tariff.overnight_window_end or state.tariff.off_peak_window_end
    if state.tariff.off_peak_now:
        return end is None or t < end
    start = state.tariff.next_off_peak_window_start
    return start is not None and end is not None and start <= t < end


def _forecast_kw_at(state: SiteState, t: datetime) -> float:
    """Average forecast PV power (kW) over the slot containing `t`, 0 outside slots."""
    slots = state.solar_forecast.slots
    for i, slot in enumerate(slots):
        slot_end = slots[i + 1].start if i + 1 < len(slots) else slot.start + timedelta(minutes=30)
        if slot.start <= t < slot_end:
            hours = (slot_end - slot.start).total_seconds() / 3600
            return slot.energy_kwh / hours if hours > 0 else 0.0
    return 0.0


def project_soc(
    state: SiteState,
    *,
    target_percent: float,
    hours: int = 12,
    step_minutes: int = 30,
) -> list[tuple[datetime, float]]:
    """Project SoC forward for the mission tape. Honest but simple, by design.

    Mirrors what the two decision loops will actually do: inside an off-peak
    window the discharge guard idles the battery and the overnight plan charges
    it toward `target_percent` at the battery's max charge power; outside it the
    house draws the baseline load net of forecast PV (a PV surplus charges).
    Clamped to [reserve, 100]. This is a *projection* — the consumer renders it
    unmistakably as one.
    """
    battery = state.battery
    soc = battery.soc_percent
    step_h = step_minutes / 60.0
    points = [(state.now, round(soc, 1))]
    t = state.now
    for _ in range(int(hours * 60 / step_minutes)):
        if _is_off_peak_at(state, t):
            if soc < target_percent:
                charge_pct = battery.max_charge_power_w * step_h / battery.capacity_kwh / 10
                soc = min(float(target_percent), soc + charge_pct)
            # at/above target: the guard idles the battery — hold.
        else:
            net_kw = state.baseline_load_w / 1000.0 - _forecast_kw_at(state, t)
            soc -= net_kw * step_h / battery.capacity_kwh * 100
            soc = min(100.0, max(float(battery.reserve_percent), soc))
        t = t + timedelta(minutes=step_minutes)
        points.append((t, round(soc, 1)))
    return points


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
    dawn = state.tariff.overnight_window_end
    if dawn is not None:
        hours_until_dawn = max(0.0, (dawn - state.now).total_seconds() / 3600)
        # Dawn is anchored to the CONFIGURED overnight end — never to the off-peak
        # sensor's current/next period end, which can belong to a short Intelligent
        # dispatch slot. The discharge guard idles the battery during any off-peak
        # period, but a hold (no further drain) is only credited when the sensor's
        # period provably reaches dawn (period end >= dawn). A dispatch slot's end
        # falls hours short, dropping to the conservative full-drain projection —
        # where unseen holds only raise the real dawn SoC above the claim. A stale
        # next-start (in the past) is treated the same way.
        period_end = state.tariff.off_peak_window_end
        reaches_dawn = period_end is not None and period_end >= dawn
        hours_discharging = hours_until_dawn
        if state.tariff.off_peak_now:
            if reaches_dawn:
                hours_discharging = 0.0
        elif (
            reaches_dawn
            and state.tariff.next_off_peak_window_start is not None
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

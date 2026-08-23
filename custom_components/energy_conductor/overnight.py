"""SoC projection for the mission tape. Pure functions, no HA dependencies."""

from __future__ import annotations

from datetime import datetime, timedelta

from .model import SiteState


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
    """Average forecast PV power (kW) over the slot containing `t`, 0 outside slots.

    Prefers `projection_forecast` (today-remaining + tomorrow) so a daytime
    projection sees today's sun; the tomorrow-only `solar_forecast` is the
    fallback when no today forecast is configured.
    """
    forecast = state.projection_forecast or state.solar_forecast
    for slot in forecast.slots:
        if slot.start <= t < slot.start + timedelta(minutes=30):
            return slot.energy_kwh / 0.5
    return 0.0


def project_soc(
    state: SiteState,
    *,
    target_percent: float = 100.0,
    hours: int = 12,
    step_minutes: int = 30,
) -> list[tuple[datetime, float]]:
    """Project SoC forward for the mission tape. Honest but simple, by design.

    Mirrors what the regime model will actually do: inside a cheap window the
    setpoint engine charges toward 100% at the battery's max charge power and the
    discharge guard holds; outside it the house draws the baseline load net of
    forecast PV (a PV surplus charges) down to the reserve. Clamped to
    [reserve, 100]. This is a *projection* — the consumer renders it unmistakably
    as one.
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

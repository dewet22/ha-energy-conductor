"""Default-everything builders for SiteState and its components.

Each builder accepts keyword overrides; callers specify only what the
test cares about. Defaults are summer solstice noon, mid-SOC, idle EV,
no off-peak rate, modest forecast — i.e. a 'do nothing' baseline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from energy_conductor.model import (
    Battery,
    EVCharger,
    ForecastSlot,
    GridState,
    SiteState,
    SolarForecast,
    TariffState,
)

DEFAULT_NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)  # summer solstice noon UTC


def a_battery(**overrides: Any) -> Battery:
    defaults: dict[str, Any] = dict(
        soc_percent=50.0,
        capacity_kwh=10.0,
        max_charge_power_w=3000,
        max_discharge_power_w=3000,
        reserve_percent=10.0,
    )
    return Battery(**(defaults | overrides))


def an_ev_charger(**overrides: Any) -> EVCharger:
    defaults: dict[str, Any] = dict(
        power_w=0.0,
        min_activation_power_w=1400,
        is_plugged_in=False,
    )
    return EVCharger(**(defaults | overrides))


def a_grid_state(**overrides: Any) -> GridState:
    defaults: dict[str, Any] = dict(import_w=0.0, export_w=0.0)
    return GridState(**(defaults | overrides))


def a_tariff(**overrides: Any) -> TariffState:
    defaults: dict[str, Any] = dict(
        off_peak_now=False,
        ev_dispatching_now=False,
        off_peak_window_end=None,
        next_off_peak_window_start=None,
        overnight_window_end=None,
    )
    return TariffState(**(defaults | overrides))


def a_forecast_with_slots(
    *,
    first_slot_at: datetime,
    slot_count: int = 12,
    kwh_per_slot: float = 1.0,
    slot_minutes: int = 30,
) -> SolarForecast:
    """Build a SolarForecast with `slot_count` consecutive slots starting at `first_slot_at`."""
    slots = [
        ForecastSlot(
            start=first_slot_at + timedelta(minutes=slot_minutes * i),
            energy_kwh=kwh_per_slot,
        )
        for i in range(slot_count)
    ]
    return SolarForecast(slots=slots, fallback_kwh=None, fallback_source=None)


def a_forecast_with_fallback(kwh: float = 5.0, source: str = "seasonal") -> SolarForecast:
    return SolarForecast(slots=[], fallback_kwh=kwh, fallback_source=source)


def a_site_state(**overrides: Any) -> SiteState:
    defaults: dict[str, Any] = dict(
        now=DEFAULT_NOW,
        battery=a_battery(),
        ev_charger=an_ev_charger(),
        solar_forecast=a_forecast_with_fallback(),
        tariff=a_tariff(),
        baseline_load_w=400.0,
    )
    return SiteState(**(defaults | overrides))

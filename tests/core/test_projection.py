"""Tests for the SoC projection that feeds the mission tape (overnight.project_soc)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from energy_conductor.model import ForecastSlot, SolarForecast
from energy_conductor.overnight import project_soc

from .builders import a_battery, a_forecast_with_slots, a_site_state, a_tariff

NOW = datetime(2026, 6, 12, 20, 0, tzinfo=UTC)


def _state(**overrides):
    defaults = dict(now=NOW, battery=a_battery(), baseline_load_w=400.0)
    return a_site_state(**(defaults | overrides))


class TestProjectSoc:
    def test_starts_at_now_with_current_soc(self):
        points = project_soc(_state(), target_percent=80, hours=12, step_minutes=60)
        assert points[0] == (NOW, 50.0)
        assert len(points) == 13
        assert points[-1][0] == NOW + timedelta(hours=12)

    def test_declines_at_baseline_rate_without_solar_or_off_peak(self):
        # 400 W on a 10 kWh battery = 4%/h.
        points = project_soc(_state(), target_percent=80, hours=3, step_minutes=60)
        socs = [p[1] for p in points]
        assert socs == [50.0, 46.0, 42.0, 38.0]

    def test_floors_at_reserve(self):
        battery = a_battery(soc_percent=14.0, reserve_percent=10.0)
        points = project_soc(_state(battery=battery), target_percent=80, hours=6, step_minutes=60)
        assert points[-1][1] == 10.0
        assert min(p[1] for p in points) == 10.0

    def test_off_peak_charges_to_target_and_holds(self):
        # In window for the whole horizon: charge at 3 kW (30%/h) to the 80% target,
        # then hold (the discharge guard idles the battery off-peak).
        tariff = a_tariff(off_peak_now=True, off_peak_window_end=NOW + timedelta(hours=12))
        points = project_soc(_state(tariff=tariff), target_percent=80, hours=4, step_minutes=60)
        socs = [p[1] for p in points]
        assert socs == [50.0, 80.0, 80.0, 80.0, 80.0]

    def test_upcoming_window_charge_starts_at_window_start(self):
        tariff = a_tariff(
            off_peak_now=False,
            next_off_peak_window_start=NOW + timedelta(hours=2),
            off_peak_window_end=NOW + timedelta(hours=8),
        )
        points = project_soc(_state(tariff=tariff), target_percent=80, hours=4, step_minutes=60)
        socs = [p[1] for p in points]
        # 2h decline (4%/h), then charging.
        assert socs[0] == 50.0
        assert socs[1] == 46.0
        assert socs[2] == 42.0
        assert socs[3] > socs[2]
        assert socs[4] == 80.0

    def test_solar_surplus_charges_toward_full(self):
        # 2 kW forecast against a 400 W baseline: +1.6 kW into a 10 kWh battery.
        forecast = a_forecast_with_slots(
            first_slot_at=NOW, slot_count=24, kwh_per_slot=1.0, slot_minutes=30
        )
        points = project_soc(
            _state(solar_forecast=forecast), target_percent=80, hours=4, step_minutes=60
        )
        socs = [p[1] for p in points]
        assert socs[1] == pytest.approx(66.0)
        assert all(b >= a for a, b in itertools.pairwise(socs))

    def test_caps_at_100(self):
        forecast = a_forecast_with_slots(
            first_slot_at=NOW, slot_count=48, kwh_per_slot=2.0, slot_minutes=30
        )
        battery = a_battery(soc_percent=95.0)
        points = project_soc(
            _state(battery=battery, solar_forecast=forecast),
            target_percent=80,
            hours=6,
            step_minutes=60,
        )
        assert max(p[1] for p in points) == 100.0

    def test_forecast_uses_fixed_thirty_minute_slot_duration(self):
        # If two forecast slots are separated by a 90-min gap (non-contiguous),
        # each slot still represents 30 min of generation, not the gap to the next.
        # Slot 0 at now: 2 kWh in 30 min = 4 kW. Baseline 400 W. Net = -3.6 kW.
        # SoC rise: 3.6 kW * 0.5h / 10 kWh * 100 = 18%.
        now = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)
        slots = [
            ForecastSlot(start=now, energy_kwh=2.0),
            ForecastSlot(start=now + timedelta(minutes=90), energy_kwh=1.0),
        ]
        forecast = SolarForecast(slots=slots, fallback_kwh=None, fallback_source=None)
        state = a_site_state(
            now=now,
            battery=a_battery(soc_percent=50.0, capacity_kwh=10.0, reserve_percent=10.0),
            baseline_load_w=400.0,
            solar_forecast=forecast,
            tariff=a_tariff(),
        )
        points = project_soc(state, target_percent=80, hours=0.5, step_minutes=30)
        # SoC should rise from 50% to 68% (18% absorbed from 3.6 kW net over 0.5h).
        # Buggy code uses 90-min gap → kW = 2/1.5 ≈ 1.33 → net ≈ -0.93 → rises only ~4.7%.
        assert len(points) == 2
        assert points[1][1] == pytest.approx(68.0, abs=0.1)

    def test_daytime_projection_uses_today_forecast_not_just_tomorrow(self):
        # The planner forecast (solar_forecast) carries only TOMORROW's slots, but
        # the projection runs over TODAY's daylight. Without a today-aware forecast
        # the midday sun is invisible and a full battery bleeds down at baseline.
        # projection_forecast carries today's slots so a sunny midday holds at 100%.
        now = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)
        tomorrow_only = SolarForecast(
            slots=[ForecastSlot(start=now + timedelta(days=1), energy_kwh=2.0)],
            fallback_kwh=None,
            fallback_source=None,
        )
        today = SolarForecast(
            slots=[
                ForecastSlot(start=now + timedelta(minutes=30 * i), energy_kwh=2.0)
                for i in range(4)
            ],
            fallback_kwh=None,
            fallback_source=None,
        )
        state = a_site_state(
            now=now,
            battery=a_battery(soc_percent=100.0, capacity_kwh=10.0, reserve_percent=10.0),
            baseline_load_w=500.0,
            solar_forecast=tomorrow_only,
            projection_forecast=today,
            tariff=a_tariff(),  # midday, not off-peak
        )
        points = project_soc(state, target_percent=80, hours=2, step_minutes=30)
        # 4 kW PV - 0.5 kW load = +3.5 kW net; already full, so it holds at 100%.
        assert all(p[1] == pytest.approx(100.0) for p in points)

    def test_projection_falls_back_to_planner_forecast_without_a_today_forecast(self):
        # No projection_forecast (today sensor unconfigured): behaviour is unchanged
        # - the projection reads the planner's solar_forecast slots as before.
        now = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)
        planner = SolarForecast(
            slots=[ForecastSlot(start=now, energy_kwh=2.0)],
            fallback_kwh=None,
            fallback_source=None,
        )
        state = a_site_state(
            now=now,
            battery=a_battery(soc_percent=50.0, capacity_kwh=10.0, reserve_percent=10.0),
            baseline_load_w=400.0,
            solar_forecast=planner,
            tariff=a_tariff(),
        )
        points = project_soc(state, target_percent=80, hours=0.5, step_minutes=30)
        # 4 kW - 0.4 kW = 3.6 kW net over 0.5h on a 10 kWh pack -> +18%.
        assert points[1][1] == pytest.approx(68.0, abs=0.1)

    def test_projection_charges_through_configured_dawn_not_dispatch_end(self):
        # Regression: off_peak_window_end can be a short Intelligent dispatch slot
        # (e.g. ending 22:30), while the charge setpoint targets overnight_window_end
        # (05:30). The projection must charge through 05:30, not stop at 22:30.
        dispatch_end = datetime(2026, 6, 1, 22, 30, tzinfo=UTC)  # brief dispatch slot
        overnight_end = datetime(2026, 6, 2, 5, 30, tzinfo=UTC)  # configured boundary
        state = a_site_state(
            now=datetime(2026, 6, 1, 22, 0, tzinfo=UTC),
            battery=a_battery(soc_percent=50.0, capacity_kwh=10.0, reserve_percent=10.0),
            tariff=a_tariff(
                off_peak_now=True,
                off_peak_window_end=dispatch_end,
                overnight_window_end=overnight_end,
            ),
        )
        points = project_soc(state, target_percent=90.0, hours=2, step_minutes=30)
        # At t+30min (22:30, dispatch ends), projection should still be charging.
        # With the bug, it stops charging at dispatch_end and starts discharging.
        soc_at_22_00 = points[0][1]
        soc_at_22_30 = points[1][1]
        soc_at_23_00 = points[2][1]
        assert soc_at_22_30 >= soc_at_22_00, "should still charge past the dispatch end"
        assert soc_at_23_00 >= soc_at_22_30, "should still charge an hour past the dispatch end"

"""Tests for the SoC projection that feeds the mission tape (overnight.project_soc)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

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

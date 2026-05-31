from datetime import datetime

import pytest

from energy_conductor.model import (
    Battery,
    EVCharger,
    ForecastSlot,
    SiteState,
    SolarForecast,
    TariffState,
)

from .conftest import utc


def _battery(**overrides):
    defaults = dict(
        soc_percent=50.0,
        capacity_kwh=10.0,
        max_charge_power_w=3000,
        max_discharge_power_w=3000,
        reserve_percent=10.0,
    )
    return Battery(**(defaults | overrides))


def _tariff(**overrides):
    defaults = dict(
        off_peak_now=False,
        ev_dispatching_now=False,
        off_peak_window_end=None,
        next_off_peak_window_start=None,
    )
    return TariffState(**(defaults | overrides))


def _forecast(**overrides):
    defaults = dict(slots=[], fallback_kwh=5.0, fallback_source="seasonal")
    return SolarForecast(**(defaults | overrides))


class TestNaiveDatetimeRejected:
    def test_site_state_rejects_naive_now(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            SiteState(
                now=datetime(2026, 6, 1, 12, 0),  # naive  # noqa: DTZ001
                battery=_battery(),
                ev_charger=None,
                solar_forecast=_forecast(),
                tariff=_tariff(),
                baseline_load_w=400.0,
            )

    def test_forecast_slot_rejects_naive_start(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            ForecastSlot(start=datetime(2026, 6, 1, 10, 0), energy_kwh=1.0)  # noqa: DTZ001

    def test_tariff_rejects_naive_window_end(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            _tariff(off_peak_window_end=datetime(2026, 6, 1, 6, 0))  # noqa: DTZ001


class TestSolarForecastContract:
    def test_slots_and_fallback_are_mutually_exclusive(self):
        with pytest.raises(ValueError, match="exactly one"):
            SolarForecast(
                slots=[ForecastSlot(start=utc(hour=10), energy_kwh=1.0)],
                fallback_kwh=5.0,  # both populated — invalid
                fallback_source="seasonal",
            )

    def test_empty_slots_requires_fallback(self):
        with pytest.raises(ValueError, match="exactly one"):
            SolarForecast(slots=[], fallback_kwh=None, fallback_source=None)

    def test_total_kwh_forecast_sums_slots(self):
        forecast = SolarForecast(
            slots=[
                ForecastSlot(start=utc(hour=10), energy_kwh=1.5),
                ForecastSlot(start=utc(hour=11), energy_kwh=2.0),
            ],
            fallback_kwh=None,
            fallback_source=None,
        )
        assert forecast.total_kwh_forecast == pytest.approx(3.5)

    def test_total_kwh_forecast_uses_fallback_when_no_slots(self):
        forecast = SolarForecast(slots=[], fallback_kwh=4.2, fallback_source="seasonal")
        assert forecast.total_kwh_forecast == pytest.approx(4.2)


class TestEVChargerOptional:
    def test_site_state_allows_no_ev_charger(self):
        SiteState(
            now=utc(),
            battery=_battery(),
            ev_charger=None,
            solar_forecast=_forecast(),
            tariff=_tariff(),
            baseline_load_w=400.0,
        )

    def test_ev_charger_carries_min_activation(self):
        ev = EVCharger(power_w=1500.0, min_activation_power_w=1400, is_plugged_in=True)
        assert ev.min_activation_power_w == 1400

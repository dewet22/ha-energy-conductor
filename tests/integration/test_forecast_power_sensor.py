"""Tests for the PV-forecast-power sensor that backs the tape's forecast line.

The mission tape draws its dashed PV-forecast line. Every other tape curve is
recorder-backed and so survives midnight, but the forecast was a live read of the
Solcast `detailedForecast` attribute, which loses yesterday when the "today"
sensor rolls over. This sensor records the forecast power "now" each tick so the
recorder retains it, letting the card draw the past half of the line from history.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.energy_conductor.const import (
    CONF_FORECAST_SOLCAST_SENSOR,
    CONF_FORECAST_SOLCAST_TODAY_SENSOR,
    CONF_FORECAST_SOURCE,
    DOMAIN,
    FORECAST_SOURCE_SOLCAST,
)
from custom_components.energy_conductor.model import ForecastSlot, SolarForecast
from custom_components.energy_conductor.sensor import _forecast_power_w
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import MOCK_CONFIG
from .test_sensor_availability import _arrange_entities, _setup
from .test_tape_attrs import _entity

NOON = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _forecast(first_slot_at: datetime, kws: list[float]) -> SolarForecast:
    slots = [
        ForecastSlot(start=first_slot_at + timedelta(minutes=30 * i), energy_kwh=kw * 0.5)
        for i, kw in enumerate(kws)
    ]
    return SolarForecast(slots=slots, fallback_kwh=None, fallback_source=None)


def test_returns_containing_slot_average_power_in_watts() -> None:
    # Slot [12:00, 12:30) has a 2.0 kW average → 2000 W.
    forecast = _forecast(NOON, [2.0, 3.0])
    assert _forecast_power_w(forecast, NOON) == 2000.0
    # 12:15 is still inside the first slot.
    assert _forecast_power_w(forecast, NOON + timedelta(minutes=15)) == 2000.0
    # 12:30 belongs to the second slot (3.0 kW → 3000 W); boundary is half-open.
    assert _forecast_power_w(forecast, NOON + timedelta(minutes=30)) == 3000.0


def test_none_when_now_outside_forecast_horizon() -> None:
    forecast = _forecast(NOON, [2.0, 3.0])
    assert _forecast_power_w(forecast, NOON - timedelta(minutes=1)) is None
    assert _forecast_power_w(forecast, NOON + timedelta(hours=1)) is None


def test_none_when_no_forecast() -> None:
    assert _forecast_power_w(None, NOON) is None


SOLCAST_TAPE_CONFIG = {
    **MOCK_CONFIG,
    CONF_FORECAST_SOURCE: FORECAST_SOURCE_SOLCAST,
    CONF_FORECAST_SOLCAST_SENSOR: "sensor.solcast_tomorrow",
    CONF_FORECAST_SOLCAST_TODAY_SENSOR: "sensor.solcast_today",
}


async def test_tape_sources_advertises_forecast_power_entity(hass: HomeAssistant) -> None:
    """With a Solcast forecast configured, the EC forecast-power entity id is
    published in tape_sources so the card can fetch its recorder history."""
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=SOLCAST_TAPE_CONFIG, entry_id="fp1")
    assert await _setup(hass, entry)

    status_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}-status")
    sources = hass.states.get(status_id).attributes.get("tape_sources")
    assert sources is not None
    assert sources["solar_forecast_power"] == _entity(hass, entry, "pv-forecast-power")


async def test_tape_sources_omits_forecast_power_without_solcast(hass: HomeAssistant) -> None:
    """No Solcast forecast → no recorder-backed forecast line, so the key is absent."""
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="fp2")
    assert await _setup(hass, entry)

    status_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}-status")
    sources = hass.states.get(status_id).attributes.get("tape_sources")
    assert sources is not None
    assert "solar_forecast_power" not in sources


async def test_sensor_reports_current_slot_power_end_to_end(hass: HomeAssistant) -> None:
    """Full chain: today's Solcast forecast → projection_forecast → native_value.

    A constant 2.0 kW across every half-hour slot of today means whichever slot
    brackets `now`, the sensor reads 2000 W — deterministic without freezing time.
    """
    await hass.config.async_set_time_zone("UTC")
    _arrange_entities(hass, soc="50")
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    slots = [
        {"period_start": midnight + timedelta(minutes=30 * i), "pv_estimate": 2.0}
        for i in range(48)
    ]
    hass.states.async_set("sensor.solcast_today", "24.0", {"detailedForecast": slots})

    entry = MockConfigEntry(domain=DOMAIN, data=SOLCAST_TAPE_CONFIG, entry_id="fp3")
    assert await _setup(hass, entry)

    state = hass.states.get(_entity(hass, entry, "pv-forecast-power"))
    assert state is not None
    assert float(state.state) == 2000.0

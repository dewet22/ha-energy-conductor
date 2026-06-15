"""Tests for the attributes feeding the mission tape (soc_projection, tape_sources)."""

from __future__ import annotations

from custom_components.energy_conductor.const import (
    CONF_DISPATCHING_SENSOR,
    CONF_FORECAST_SOLCAST_TODAY_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_HOME_LOAD_SENSOR,
    CONF_SOLAR_POWER_SENSOR,
    DOMAIN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import MOCK_CONFIG
from .test_sensor_availability import _arrange_entities, _setup

TAPE_CONFIG = {
    **MOCK_CONFIG,
    CONF_SOLAR_POWER_SENSOR: "sensor.pv_power",
    CONF_HOME_LOAD_SENSOR: "sensor.home_load",
    CONF_DISPATCHING_SENSOR: "binary_sensor.dispatching",
    CONF_GRID_EXPORT_SENSOR: "sensor.grid_export_w",
    CONF_FORECAST_SOLCAST_TODAY_SENSOR: "sensor.forecast_today",
}


def _entity(hass, entry, key):
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}-{key}")


async def test_overnight_plan_exposes_soc_projection(hass: HomeAssistant) -> None:
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="tp1")
    assert await _setup(hass, entry)

    attrs = hass.states.get(_entity(hass, entry, "overnight-plan")).attributes
    projection = attrs.get("soc_projection")
    assert isinstance(projection, list) and len(projection) > 12
    first = projection[0]
    assert set(first) == {"t", "soc"}
    assert first["soc"] == 50.0
    assert isinstance(first["t"], str)  # isoformat


async def test_status_sensor_exposes_tape_sources(hass: HomeAssistant) -> None:
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=TAPE_CONFIG, entry_id="tp2")
    assert await _setup(hass, entry)

    status_id = _entity(hass, entry, "status")
    sources = hass.states.get(status_id).attributes.get("tape_sources")
    assert sources is not None
    assert sources["solar_power"] == "sensor.pv_power"
    assert sources["home_load"] == "sensor.home_load"
    assert sources["dispatching"] == "binary_sensor.dispatching"
    assert sources["grid_export_w"] == "sensor.grid_export_w"
    assert sources["off_peak"] == MOCK_CONFIG["off_peak_sensor"]
    assert sources["solar_forecast_today"] == "sensor.forecast_today"
    # Unconfigured feeds are absent keys.
    assert "solar_forecast" not in sources
    assert "grid_import_w" not in sources


async def test_status_sensor_exposes_level_sources(hass: HomeAssistant) -> None:
    # The long-term card renders SoC from mean/min/max statistics, so the
    # battery SoC entity is published in its own map (measurement series, not a
    # counter) - the configured source, not EC's spike-prone passthrough.
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="tp3")
    assert await _setup(hass, entry)

    status_id = _entity(hass, entry, "status")
    sources = hass.states.get(status_id).attributes.get("level_sources")
    assert sources is not None
    assert sources["battery_soc"] == MOCK_CONFIG["battery_soc_sensor"]

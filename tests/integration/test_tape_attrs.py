"""Tests for the attributes feeding the mission tape (soc_projection, tape_sources)."""

from __future__ import annotations

from datetime import timedelta

from custom_components.energy_conductor.const import (
    CONF_DISPATCHING_SENSOR,
    CONF_EV_POWER_SENSOR,
    CONF_FORECAST_SOLCAST_TODAY_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_HOME_LOAD_SENSOR,
    CONF_HOTWATER_POWER_SENSOR,
    CONF_SOLAR_POWER_SENSOR,
    DOMAIN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
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
    CONF_HOTWATER_POWER_SENSOR: "sensor.eddi_power",
    CONF_EV_POWER_SENSOR: "sensor.zappi_power",
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


async def test_setpoint_sensor_reflects_regime(hass: HomeAssistant) -> None:
    """The repurposed entity reads the live setpoint and names its regime."""
    _arrange_entities(hass, soc="50")
    hass.states.async_set(MOCK_CONFIG["off_peak_sensor"], "on", {})
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="tp4")
    assert await _setup(hass, entry)

    state = hass.states.get(_entity(hass, entry, "overnight-plan"))
    assert float(state.state) == 100.0
    assert state.attributes["regime"] == "cheap_charge"


async def test_setpoint_sensor_reflects_self_consume_regime(hass: HomeAssistant) -> None:
    """Out of the cheap window the setpoint sits at the control's own minimum."""
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="tp5")
    assert await _setup(hass, entry)

    state = hass.states.get(_entity(hass, entry, "overnight-plan"))
    assert float(state.state) == 4.0  # charge control's default minimum
    assert state.attributes["regime"] == "self_consume"


async def test_soc_projection_targets_full(hass: HomeAssistant) -> None:
    """Regression: the projection must aim at 100%, not at the current setpoint.

    In self-consume the setpoint is the control minimum (~4%). Feeding *that* to
    project_soc made the off-peak leg of the tape hold flat instead of rising, so
    the tape claimed the battery would never refill overnight.
    """
    now = dt_util.utcnow()
    _arrange_entities(hass, soc="50")
    hass.states.async_set(
        MOCK_CONFIG["off_peak_sensor"],
        "off",
        {
            "next_start": (now + timedelta(hours=1)).isoformat(),
            "next_end": (now + timedelta(hours=7)).isoformat(),
        },
    )
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="tp6")
    assert await _setup(hass, entry)

    setpoint = hass.states.get(_entity(hass, entry, "overnight-plan"))
    assert setpoint.attributes["regime"] == "self_consume"
    projection = setpoint.attributes["soc_projection"]
    in_window = [
        p["soc"]
        for p in projection
        if now + timedelta(hours=1) <= dt_util.parse_datetime(p["t"]) < now + timedelta(hours=7)
    ]
    assert len(in_window) > 4
    assert in_window[-1] > in_window[0], "off-peak leg must rise, not hold flat"
    assert max(in_window) == 100.0


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
    assert sources["diversion_power"] == "sensor.eddi_power"
    assert sources["ev_power"] == "sensor.zappi_power"
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

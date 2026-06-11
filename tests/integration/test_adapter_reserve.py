"""Adapter reserve-floor tests.

EC can read the minimum-SoC floor from a live entity (e.g. GivEnergy
battery_soc_reserve) instead of a static config percent, so it tracks the
inverter's actual floor. Falls back to config when unset or unreadable.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from custom_components.energy_conductor.adapter import Adapter
from custom_components.energy_conductor.const import (
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_RESERVE_SOC_SENSOR,
)

RESERVE_SENSOR = "number.givenergy_battery_soc_reserve"


def _adapter(hass, config: dict) -> Adapter:
    return Adapter(hass, config)


async def test_reserve_read_from_sensor_when_configured(hass, mock_config_entry):
    hass.states.async_set(RESERVE_SENSOR, "4")
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)
    adapter = _adapter(
        hass,
        {CONF_RESERVE_SOC_SENSOR: RESERVE_SENSOR, CONF_BATTERY_RESERVE_PERCENT: 10},
    )

    assert adapter._reserve_percent() == pytest.approx(4.0)


async def test_reserve_falls_back_to_config_when_sensor_unset(hass, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    adapter = _adapter(hass, {CONF_BATTERY_RESERVE_PERCENT: 10})

    assert adapter._reserve_percent() == pytest.approx(10.0)


async def test_reserve_survives_a_never_changing_sensor(hass, mock_config_entry, freezer):
    """A reserve number that never changes must not go 'stale' (live regression 2026-06-11).

    GivEnergy number entities only write state when the value changes; a static 4%
    reserve crossed the old 24h staleness threshold and EC silently switched to the
    config fallback, shifting every plan target by the difference. Like the EV power
    sensor, rely on unavailable/unknown only.
    """
    hass.states.async_set(RESERVE_SENSOR, "4")
    await hass.async_block_till_done()
    freezer.tick(timedelta(days=3))
    mock_config_entry.add_to_hass(hass)
    adapter = _adapter(
        hass,
        {CONF_RESERVE_SOC_SENSOR: RESERVE_SENSOR, CONF_BATTERY_RESERVE_PERCENT: 10},
    )

    assert adapter._reserve_percent() == pytest.approx(4.0)


async def test_reserve_falls_back_to_config_when_sensor_unreadable(hass, mock_config_entry):
    hass.states.async_set(RESERVE_SENSOR, "unavailable")
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)
    adapter = _adapter(
        hass,
        {CONF_RESERVE_SOC_SENSOR: RESERVE_SENSOR, CONF_BATTERY_RESERVE_PERCENT: 10},
    )

    # Unreadable sensor → config value, no crash.
    assert adapter._reserve_percent() == pytest.approx(10.0)


async def test_reserve_sensor_value_flows_into_site_state(hass, mock_config_entry):
    """End-to-end: a live reserve sensor sets Battery.reserve_percent."""
    # Arrange the full set of required entities for build_site_state.
    hass.states.async_set("sensor.bat_soc", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("number.charge", "40", {"max": 100})
    hass.states.async_set("number.discharge", "40", {"max": 100})
    hass.states.async_set("binary_sensor.off_peak", "off")
    hass.states.async_set(RESERVE_SENSOR, "4")
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)

    from custom_components.energy_conductor.const import (
        CONF_BATTERY_CAPACITY_KWH,
        CONF_BATTERY_CHARGE_CONTROL,
        CONF_BATTERY_DISCHARGE_LIMIT,
        CONF_BATTERY_SOC_SENSOR,
        CONF_FORECAST_SOURCE,
        CONF_OFF_PEAK_SENSOR,
        FORECAST_SOURCE_NONE,
    )

    adapter = _adapter(
        hass,
        {
            CONF_BATTERY_SOC_SENSOR: "sensor.bat_soc",
            CONF_BATTERY_CHARGE_CONTROL: "number.charge",
            CONF_BATTERY_DISCHARGE_LIMIT: "number.discharge",
            CONF_BATTERY_CAPACITY_KWH: 17.7,
            CONF_OFF_PEAK_SENSOR: "binary_sensor.off_peak",
            CONF_RESERVE_SOC_SENSOR: RESERVE_SENSOR,
            CONF_FORECAST_SOURCE: FORECAST_SOURCE_NONE,
        },
    )

    state = await adapter.build_site_state()
    assert state.battery.reserve_percent == pytest.approx(4.0)

"""Adapter charge-target minimum tests.

Battery.charge_target_min_percent is read from the charge-control entity's own
`min` attribute (the lowest setpoint the inverter accepts), mirroring `_max_attr`.
Defaults to 4.0 when the attribute is absent; clamped to [0, 100] against a bogus
upstream value, since the self-consume regime writes this straight to hardware.
"""

from __future__ import annotations

import pytest
from custom_components.energy_conductor.adapter import Adapter
from custom_components.energy_conductor.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_FORECAST_SOURCE,
    CONF_OFF_PEAK_SENSOR,
    FORECAST_SOURCE_NONE,
)


def _config() -> dict:
    return {
        CONF_BATTERY_SOC_SENSOR: "sensor.bat_soc",
        CONF_BATTERY_CHARGE_CONTROL: "number.charge",
        CONF_BATTERY_DISCHARGE_LIMIT: "number.discharge",
        CONF_BATTERY_CAPACITY_KWH: 17.7,
        CONF_OFF_PEAK_SENSOR: "binary_sensor.off_peak",
        CONF_FORECAST_SOURCE: FORECAST_SOURCE_NONE,
    }


async def test_charge_target_min_read_from_control(hass, mock_config_entry):
    hass.states.async_set("sensor.bat_soc", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("number.charge", "40", {"max": 100, "min": 10})
    hass.states.async_set("number.discharge", "40", {"max": 100})
    hass.states.async_set("binary_sensor.off_peak", "off")
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)
    adapter = Adapter(hass, _config())

    state = await adapter.build_site_state()

    assert state.battery.charge_target_min_percent == pytest.approx(10.0)


async def test_charge_target_min_defaults_when_missing(hass, mock_config_entry):
    hass.states.async_set("sensor.bat_soc", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("number.charge", "40", {"max": 100})  # no `min` attribute
    hass.states.async_set("number.discharge", "40", {"max": 100})
    hass.states.async_set("binary_sensor.off_peak", "off")
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)
    adapter = Adapter(hass, _config())

    state = await adapter.build_site_state()

    assert state.battery.charge_target_min_percent == pytest.approx(4.0)


async def test_charge_target_min_clamped(hass, mock_config_entry):
    hass.states.async_set("sensor.bat_soc", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("number.charge", "40", {"max": 100, "min": -50})
    hass.states.async_set("number.discharge", "40", {"max": 100})
    hass.states.async_set("binary_sensor.off_peak", "off")
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)
    adapter = Adapter(hass, _config())

    state = await adapter.build_site_state()

    assert state.battery.charge_target_min_percent == pytest.approx(0.0)

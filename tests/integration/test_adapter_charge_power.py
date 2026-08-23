"""Adapter charge-power plausibility tests.

`Battery.max_charge_power_w` comes from the charge control's `max` attribute, but
under the setpoint regime that same entity is the SoC *target* control, whose max
is 100 (percent, not watts). A %-scale max would tell the tape's projection the
battery charges at 100 W, so it is gated back to the default charge-rate estimate.

The discharge side is deliberately NOT gated — see the comments in adapter.py.
"""

from __future__ import annotations

from custom_components.energy_conductor.adapter import Adapter
from custom_components.energy_conductor.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_FORECAST_SOURCE,
    CONF_OFF_PEAK_SENSOR,
    DEFAULT_BATTERY_MAX_POWER_W,
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


async def _build(hass, mock_config_entry, *, charge_attrs: dict, discharge_attrs: dict):
    hass.states.async_set("sensor.bat_soc", "50", {"unit_of_measurement": "%"})
    hass.states.async_set("number.charge", "40", charge_attrs)
    hass.states.async_set("number.discharge", "40", discharge_attrs)
    hass.states.async_set("binary_sensor.off_peak", "off")
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)
    return await Adapter(hass, _config()).build_site_state()


async def test_percent_scale_charge_control_falls_back_to_default_power(hass, mock_config_entry):
    # A GivEnergy-style charge_target_soc control: min 4 %, max 100 %.
    state = await _build(
        hass,
        mock_config_entry,
        charge_attrs={"min": 4, "max": 100},
        discharge_attrs={"max": 100},
    )

    assert state.battery.max_charge_power_w == DEFAULT_BATTERY_MAX_POWER_W


async def test_real_charge_power_control_passes_through(hass, mock_config_entry):
    state = await _build(
        hass,
        mock_config_entry,
        charge_attrs={"max": 3600},
        discharge_attrs={"max": 3600},
    )

    assert state.battery.max_charge_power_w == 3600


async def test_discharge_limit_is_not_plausibility_gated(hass, mock_config_entry):
    # The discharge guard writes this value verbatim to the limit entity, so on a
    # %-based limit control (0-50) its own max IS the correct "unconstrained" write.
    state = await _build(
        hass,
        mock_config_entry,
        charge_attrs={"max": 3600},
        discharge_attrs={"max": 50},
    )

    assert state.battery.max_discharge_power_w == 50

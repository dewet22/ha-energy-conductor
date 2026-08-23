"""Fixtures for the HA-glue integration tests.

These exercise the thin Home Assistant layer (notifier, coordinator wiring,
sensors) that the pure-core suite in tests/core deliberately does not import.
"""

from __future__ import annotations

import pytest
from custom_components.energy_conductor.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DAILY_KWH_TARGET,
    CONF_FORECAST_SOURCE,
    CONF_NOTIFY_TARGET,
    CONF_OFF_PEAK_SENSOR,
    CONF_SUMMER_MAX_KWH,
    CONF_WINTER_MIN_KWH,
    CONF_WRITE_MODE,
    DOMAIN,
    FORECAST_SOURCE_NONE,
    WRITE_MODE_DRY_RUN,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the energy_conductor custom integration in tests."""
    yield


MOCK_CONFIG = {
    CONF_BATTERY_SOC_SENSOR: "sensor.battery_soc",
    CONF_BATTERY_CHARGE_CONTROL: "number.battery_charge_target",
    CONF_BATTERY_DISCHARGE_LIMIT: "number.battery_discharge_limit",
    CONF_BATTERY_CAPACITY_KWH: 10.0,
    CONF_BATTERY_RESERVE_PERCENT: 10,
    CONF_OFF_PEAK_SENSOR: "binary_sensor.off_peak_rate",
    CONF_FORECAST_SOURCE: FORECAST_SOURCE_NONE,
    CONF_WINTER_MIN_KWH: 0.0,
    CONF_SUMMER_MAX_KWH: 8.0,
    CONF_WRITE_MODE: WRITE_MODE_DRY_RUN,
    CONF_NOTIFY_TARGET: "notify.pixel_9a",
    CONF_DAILY_KWH_TARGET: 10.0,
}


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A config entry with the minimal valid energy_conductor config."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        entry_id="test_entry",
        title="Energy Conductor",
    )

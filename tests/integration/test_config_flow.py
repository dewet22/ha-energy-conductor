"""Tests for the config flow: wizard, menu-based options flow, and v2→v3 migration."""

from __future__ import annotations

from custom_components.energy_conductor import async_migrate_entry
from custom_components.energy_conductor.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DAILY_KWH_TARGET,
    CONF_ENTITY_REFS,
    CONF_FORECAST_SOLCAST_SENSOR,
    CONF_FORECAST_SOURCE,
    CONF_NOTIFY_TARGET,
    CONF_OFF_PEAK_SENSOR,
    CONF_OVERNIGHT_PLAN_TIME,
    CONF_OVERNIGHT_WINDOW_END_TIME,
    CONF_SOUTHERN_HEMISPHERE,
    CONF_SUMMER_MAX_KWH,
    CONF_WINTER_MIN_KWH,
    CONF_WRITE_MODE,
    DOMAIN,
    FORECAST_SOURCE_SOLCAST,
    WRITE_MODE_DRY_RUN,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _register(hass, domain, platform, unique_id, object_id):
    return (
        er.async_get(hass)
        .async_get_or_create(domain, platform, unique_id, suggested_object_id=object_id)
        .entity_id
    )


async def test_wizard_creates_v3_entry_with_anchors(hass):
    soc = _register(hass, "sensor", "givenergy", "soc", "battery_soc")
    charge = _register(hass, "number", "givenergy", "charge", "charge_target")
    discharge = _register(hass, "number", "givenergy", "discharge", "discharge_limit")
    off_peak = _register(hass, "binary_sensor", "octopus", "offpeak", "off_peak")
    solcast = _register(hass, "sensor", "solcast", "fc", "forecast_tomorrow")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "battery"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_BATTERY_SOC_SENSOR: soc,
            CONF_BATTERY_CHARGE_CONTROL: charge,
            CONF_BATTERY_DISCHARGE_LIMIT: discharge,
            CONF_BATTERY_CAPACITY_KWH: 17.7,
            CONF_BATTERY_RESERVE_PERCENT: 10,
        },
    )
    assert result["step_id"] == "tariff"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_OFF_PEAK_SENSOR: off_peak, CONF_OVERNIGHT_WINDOW_END_TIME: "05:30:00"},
    )
    assert result["step_id"] == "forecast_source"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FORECAST_SOURCE: FORECAST_SOURCE_SOLCAST}
    )
    assert result["step_id"] == "forecast"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_FORECAST_SOLCAST_SENSOR: solcast,
            CONF_WINTER_MIN_KWH: 0.0,
            CONF_SUMMER_MAX_KWH: 25.0,
            CONF_SOUTHERN_HEMISPHERE: False,
        },
    )
    assert result["step_id"] == "loads"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DAILY_KWH_TARGET: 10.0}
    )
    assert result["step_id"] == "ev"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "hotwater"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "grid"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "behaviour"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_WRITE_MODE: WRITE_MODE_DRY_RUN,
            CONF_NOTIFY_TARGET: "notify.test",
            CONF_OVERNIGHT_PLAN_TIME: "21:00:00",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["result"].version == 3

    refs = result["data"][CONF_ENTITY_REFS]
    assert refs[CONF_BATTERY_SOC_SENSOR] == {"platform": "givenergy", "unique_id": "soc"}
    assert refs[CONF_FORECAST_SOLCAST_SENSOR] == {"platform": "solcast", "unique_id": "fc"}


def _v3_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            CONF_BATTERY_SOC_SENSOR: "sensor.battery_soc",
            CONF_BATTERY_CAPACITY_KWH: 10.0,
            CONF_OFF_PEAK_SENSOR: "binary_sensor.off_peak",
            CONF_FORECAST_SOURCE: "none",
            CONF_NOTIFY_TARGET: "notify.test",
            CONF_WRITE_MODE: WRITE_MODE_DRY_RUN,
        },
        entry_id="opt_entry",
    )


async def test_options_menu_lists_groups(hass):
    entry = _v3_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    assert set(result["menu_options"]) == {
        "battery",
        "tariff",
        "solar",
        "loads",
        "ev",
        "hotwater",
        "grid",
        "behaviour",
    }


async def test_options_battery_substep_persists_and_preserves(hass):
    new_soc = _register(hass, "sensor", "givenergy", "soc-2", "loft_battery_soc")
    entry = _v3_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "battery"}
    )
    assert result["step_id"] == "battery"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_BATTERY_SOC_SENSOR: new_soc,
            CONF_BATTERY_CHARGE_CONTROL: "number.charge",
            CONF_BATTERY_DISCHARGE_LIMIT: "number.discharge",
            CONF_BATTERY_CAPACITY_KWH: 17.7,
            CONF_BATTERY_RESERVE_PERCENT: 10,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # The edited battery key lands in options; the re-pointed entity is anchored.
    assert entry.options[CONF_BATTERY_SOC_SENSOR] == new_soc
    assert entry.options[CONF_BATTERY_CAPACITY_KWH] == 17.7
    assert entry.options[CONF_ENTITY_REFS][CONF_BATTERY_SOC_SENSOR] == {
        "platform": "givenergy",
        "unique_id": "soc-2",
    }
    # An untouched key from another group is not clobbered (still only in data).
    assert entry.data[CONF_NOTIFY_TARGET] == "notify.test"


async def test_migrate_v2_to_v3_backfills_anchors(hass):
    soc = _register(hass, "sensor", "givenergy", "soc", "battery_soc")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_BATTERY_SOC_SENSOR: soc,
            CONF_OFF_PEAK_SENSOR: "binary_sensor.unregistered",
        },
        entry_id="mig_entry",
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 3
    refs = entry.data[CONF_ENTITY_REFS]
    assert refs[CONF_BATTERY_SOC_SENSOR] == {"platform": "givenergy", "unique_id": "soc"}
    # Unregistered entity is left anchor-less (falls back to entity_id at resolve time).
    assert CONF_OFF_PEAK_SENSOR not in refs

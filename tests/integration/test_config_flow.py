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
    CONF_HOTWATER_POWER_SENSOR,
    CONF_IMPORT_COST_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    CONF_NOTIFY_TARGET,
    CONF_OFF_PEAK_SENSOR,
    CONF_OVERNIGHT_PLAN_TIME,
    CONF_OVERNIGHT_WINDOW_END_TIME,
    CONF_SOLAR_POWER_SENSOR,
    CONF_SOUTHERN_HEMISPHERE,
    CONF_SUMMER_MAX_KWH,
    CONF_SYSTEM_CAPITAL_COST,
    CONF_WINTER_MIN_KWH,
    CONF_WRITE_MODE,
    DOMAIN,
    FORECAST_SOURCE_DAILY,
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
    assert result["step_id"] == "costs"

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
        "costs",
        "behaviour",
    }


async def test_options_costs_substep_persists_and_anchors(hass):
    import_cost = _register(hass, "sensor", "octopus_energy", "elec-cost", "electricity_cost")
    import_rate = _register(hass, "sensor", "octopus_energy", "elec-rate", "electricity_rate")
    entry = _v3_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "costs"}
    )
    assert result["step_id"] == "costs"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_IMPORT_COST_SENSOR: import_cost,
            CONF_IMPORT_RATE_SENSOR: import_rate,
            CONF_SYSTEM_CAPITAL_COST: 11500.0,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    assert entry.options[CONF_IMPORT_COST_SENSOR] == import_cost
    assert entry.options[CONF_SYSTEM_CAPITAL_COST] == 11500.0
    assert entry.options[CONF_ENTITY_REFS][CONF_IMPORT_COST_SENSOR] == {
        "platform": "octopus_energy",
        "unique_id": "elec-cost",
    }
    assert entry.options[CONF_ENTITY_REFS][CONF_IMPORT_RATE_SENSOR] == {
        "platform": "octopus_energy",
        "unique_id": "elec-rate",
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


async def test_options_hotwater_substep_persists_power_sensor(hass):
    """Regression: the diverter-power field must survive _save (be in HOTWATER_KEYS),
    not merely render in the form - else the tape's diversion rail never wires up."""
    power = _register(hass, "sensor", "myenergi", "eddi-power", "eddi_hwc_power")
    entry = _v3_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hotwater"}
    )
    assert result["step_id"] == "hotwater"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HOTWATER_POWER_SENSOR: power}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_HOTWATER_POWER_SENSOR] == power


def test_no_options_field_is_silently_dropped_on_save():
    """Every field rendered in an options sub-step must be in that step's save whitelist
    (the *_KEYS passed to _save). A field in the schema but not the whitelist is silently
    filtered out by _save and the user's input is lost - the diverter-power regression.
    This guards the whole flow against that class, not just the one field."""
    from custom_components.energy_conductor import config_flow as cf

    def form_fields(schema):
        return {getattr(m, "schema", m) for m in schema.schema}

    pairs = {
        "battery": (cf.battery_schema({}, options=True), cf.BATTERY_KEYS),
        "tariff": (cf.tariff_schema({}, options=True), cf.TARIFF_KEYS),
        "forecast/solcast": (
            cf.forecast_schema(FORECAST_SOURCE_SOLCAST, {}, options=True),
            cf.SOLAR_KEYS,
        ),
        "forecast/daily": (
            cf.forecast_schema(FORECAST_SOURCE_DAILY, {}, options=True),
            cf.SOLAR_KEYS,
        ),
        "loads": (cf.loads_schema({}, options=True), cf.LOADS_KEYS),
        "ev": (cf.ev_schema({}, options=True), cf.EV_KEYS),
        "hotwater": (cf.hotwater_schema({}, options=True), cf.HOTWATER_KEYS),
        "grid": (cf.grid_schema({}, options=True), cf.GRID_KEYS),
        "costs": (cf.costs_schema({}, options=True), cf.COSTS_KEYS),
        "behaviour": (cf.behaviour_schema({}, options=True), cf.BEHAVIOUR_KEYS),
    }
    dropped = {
        name: sorted(form_fields(schema) - set(keys))
        for name, (schema, keys) in pairs.items()
        if form_fields(schema) - set(keys)
    }
    assert not dropped, f"options fields rendered but not persisted by _save: {dropped}"


def test_every_config_flow_field_has_a_translation():
    """A field rendered without an en.json label shows its raw key in the UI (the
    hotwater_power_sensor regression). Every config and options field must be labelled."""
    import json
    from pathlib import Path

    from custom_components.energy_conductor import config_flow as cf

    def form_fields(schema):
        return {getattr(m, "schema", m) for m in schema.schema}

    def forecast(opts):
        return form_fields(
            cf.forecast_schema(FORECAST_SOURCE_SOLCAST, {}, options=opts)
        ) | form_fields(cf.forecast_schema(FORECAST_SOURCE_DAILY, {}, options=opts))

    config_steps = {
        "battery": form_fields(cf.battery_schema({}, options=False)),
        "tariff": form_fields(cf.tariff_schema({}, options=False)),
        "forecast_source": form_fields(cf.forecast_source_schema({}, options=False)),
        "forecast": forecast(False),
        "loads": form_fields(cf.loads_schema({}, options=False)),
        "ev": form_fields(cf.ev_schema({}, options=False)),
        "hotwater": form_fields(cf.hotwater_schema({}, options=False)),
        "grid": form_fields(cf.grid_schema({}, options=False)),
        "costs": form_fields(cf.costs_schema({}, options=False)),
        "behaviour": form_fields(cf.behaviour_schema({}, options=False)),
    }
    options_steps = {
        "battery": form_fields(cf.battery_schema({}, options=True)),
        "tariff": form_fields(cf.tariff_schema({}, options=True)),
        "solar": form_fields(cf.forecast_source_schema({}, options=True)),
        "solar_details": forecast(True),
        "loads": form_fields(cf.loads_schema({}, options=True)),
        "ev": form_fields(cf.ev_schema({}, options=True)),
        "hotwater": form_fields(cf.hotwater_schema({}, options=True)),
        "grid": form_fields(cf.grid_schema({}, options=True)),
        "costs": form_fields(cf.costs_schema({}, options=True)),
        "behaviour": form_fields(cf.behaviour_schema({}, options=True)),
    }
    tr = json.loads((Path(cf.__file__).parent / "translations" / "en.json").read_text())

    missing = {}
    for flow, steps in (("config", config_steps), ("options", options_steps)):
        block = tr[flow]["step"]
        for step, flds in steps.items():
            data = block.get(step, {}).get("data", {})
            gap = sorted(flds - set(data))
            if gap:
                missing[f"{flow}.{step}"] = gap
    assert not missing, f"config-flow fields without an en.json label: {missing}"


async def test_options_form_shows_resolved_entity_not_stored(hass):
    """The form must show what the runtime reads, not the raw stored id (live find 2026-06-11).

    A stale anchor redirected the Solcast reference at resolve time while the form kept
    rendering the innocent stored entity_id — the mismatch was invisible everywhere
    (diagnostics redact entity_refs). Defaults now pass through resolve_config so a
    redirect or rename surfaces in the picker.
    """
    current_soc = _register(hass, "sensor", "givenergy", "soc", "loft_battery_soc")
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            **_v3_entry().data,
            # Stored id predates a rename; the anchor resolves to the current entity.
            CONF_BATTERY_SOC_SENSOR: "sensor.battery_soc",
            CONF_ENTITY_REFS: {
                CONF_BATTERY_SOC_SENSOR: {"platform": "givenergy", "unique_id": "soc"}
            },
        },
        entry_id="resolved_entry",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "battery"}
    )

    markers = {str(k): k for k in result["data_schema"].schema}
    suggested = markers[CONF_BATTERY_SOC_SENSOR].description["suggested_value"]
    assert suggested == current_soc, "form must render the resolved entity, not the stale id"


async def test_solar_options_offers_power_sensor_without_solcast(hass):
    # The Mission tape's solar curve reads CONF_SOLAR_POWER_SENSOR regardless of
    # forecast source, so the selector must render even when the entry is not on
    # Solcast (here: forecast source "none").
    entry = _v3_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "solar"}
    )
    # The solar group first picks a forecast source; choose the daily-total source
    # (not Solcast) and the power-sensor selector must still appear on the form.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_FORECAST_SOURCE: FORECAST_SOURCE_DAILY}
    )
    markers = {str(k) for k in result["data_schema"].schema}
    assert CONF_SOLAR_POWER_SENSOR in markers


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

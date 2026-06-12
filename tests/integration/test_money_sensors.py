"""Tests for the money sensors: gating, accumulation wiring, restore, payback attrs."""

from __future__ import annotations

import pytest
from custom_components.energy_conductor.const import (
    CONF_DAILY_ENERGY_SENSOR,
    CONF_EV_ENERGY_SENSOR,
    CONF_EXPORT_EARNINGS_SENSOR,
    CONF_GRID_EXPORT_ENERGY_SENSOR,
    CONF_IMPORT_COST_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    CONF_PV_ENERGY_SENSOR,
    CONF_SYSTEM_CAPITAL_COST,
    DOMAIN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from .conftest import MOCK_CONFIG
from .test_sensor_availability import _arrange_entities, _setup

HOUSE = "sensor.house_energy"
RATE = "sensor.import_rate"
IMPORT_COST = "sensor.import_cost"
EXPORT_EARNINGS = "sensor.export_earnings"
PV = "sensor.pv_today"
EXPORT_KWH = "sensor.export_today"
EV = "sensor.ev_today"

COSTS_CONFIG = {
    **MOCK_CONFIG,
    CONF_DAILY_ENERGY_SENSOR: HOUSE,
    CONF_IMPORT_RATE_SENSOR: RATE,
    CONF_IMPORT_COST_SENSOR: IMPORT_COST,
    CONF_EXPORT_EARNINGS_SENSOR: EXPORT_EARNINGS,
    CONF_PV_ENERGY_SENSOR: PV,
    CONF_GRID_EXPORT_ENERGY_SENSOR: EXPORT_KWH,
    CONF_EV_ENERGY_SENSOR: EV,
    CONF_SYSTEM_CAPITAL_COST: 11500.0,
}

MONEY_KEYS = (
    "counterfactual-cost-today",
    "savings-today",
    "ev-charge-cost-today",
    "cumulative-savings",
)


def _arrange_money_entities(hass: HomeAssistant) -> None:
    hass.states.async_set(HOUSE, "10.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set(RATE, "0.30", {"unit_of_measurement": "GBP/kWh"})
    hass.states.async_set(IMPORT_COST, "1.00", {"unit_of_measurement": "GBP"})
    hass.states.async_set(EXPORT_EARNINGS, "0.25", {"unit_of_measurement": "GBP"})
    hass.states.async_set(PV, "8.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set(EXPORT_KWH, "2.0", {"unit_of_measurement": "kWh"})
    hass.states.async_set(EV, "0.0", {"unit_of_measurement": "kWh"})


def _money_entity_id(hass: HomeAssistant, entry, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}-{key}")


async def test_money_sensors_absent_without_costs_config(hass: HomeAssistant) -> None:
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="m0")
    assert await _setup(hass, entry)
    for key in MONEY_KEYS:
        assert _money_entity_id(hass, entry, key) is None, key


async def test_money_sensors_created_and_accumulate(hass: HomeAssistant) -> None:
    _arrange_entities(hass, soc="50")
    _arrange_money_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=COSTS_CONFIG, entry_id="m1")
    assert await _setup(hass, entry)

    for key in MONEY_KEYS:
        assert _money_entity_id(hass, entry, key) is not None, key

    # First tick set the baseline; a second tick with +2 kWh accumulates at 30p.
    hass.states.async_set(HOUSE, "12.0", {"unit_of_measurement": "kWh"})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    counterfactual = hass.states.get(_money_entity_id(hass, entry, "counterfactual-cost-today"))
    assert float(counterfactual.state) == pytest.approx(0.60)

    # savings = counterfactual 0.60 - import cost 1.00 + export 0.25
    savings = hass.states.get(_money_entity_id(hass, entry, "savings-today"))
    assert float(savings.state) == pytest.approx(-0.15)
    assert "solar_self_use_gbp" in savings.attributes
    assert "battery_peak_shift_gbp" in savings.attributes
    assert "hot_water_gas_displacement_gbp" in savings.attributes
    assert "ev_solar_charge_gbp" in savings.attributes

    cumulative = hass.states.get(_money_entity_id(hass, entry, "cumulative-savings"))
    assert float(cumulative.state) == pytest.approx(-0.15)
    attrs = cumulative.attributes
    assert attrs["capital_cost_gbp"] == 11500.0
    assert "recovered_pct" in attrs
    assert "run_rate_gbp_per_year" in attrs
    assert "projected_breakeven" in attrs


async def test_money_sensor_unavailable_during_rate_outage(hass: HomeAssistant) -> None:
    _arrange_entities(hass, soc="50")
    _arrange_money_entities(hass)
    hass.states.async_set(RATE, "unavailable")
    entry = MockConfigEntry(domain=DOMAIN, data=COSTS_CONFIG, entry_id="m2")
    assert await _setup(hass, entry)

    counterfactual = hass.states.get(_money_entity_id(hass, entry, "counterfactual-cost-today"))
    assert counterfactual.state == "unavailable"

    hass.states.async_set(RATE, "0.30", {"unit_of_measurement": "GBP/kWh"})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    counterfactual = hass.states.get(_money_entity_id(hass, entry, "counterfactual-cost-today"))
    assert counterfactual.state not in ("unavailable", "unknown")


async def test_counterfactual_restores_running_total(hass: HomeAssistant) -> None:
    """A same-day restart resumes the accumulator instead of restarting at zero."""
    _arrange_entities(hass, soc="50")
    _arrange_money_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=COSTS_CONFIG, entry_id="m3")

    # Pre-register the entity so the restore cache can be keyed to its entity_id.
    eid = (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}-counterfactual-cost-today",
            suggested_object_id="ec_counterfactual",
        )
        .entity_id
    )
    from homeassistant.util import dt as dt_util

    today = dt_util.now().date().isoformat()
    mock_restore_cache(
        hass,
        [State(eid, "1.50", {"day": today, "source_counter_kwh": 9.0})],
    )

    assert await _setup(hass, entry)
    # First tick: house counter 10.0 vs restored 9.0 -> 1.50 + 1 kWh * 0.30.
    state = hass.states.get(eid)
    assert float(state.state) == pytest.approx(1.80)

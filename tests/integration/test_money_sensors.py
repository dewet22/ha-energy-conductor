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
    CONF_PUBLIC_CHARGING_RATE,
    CONF_PV_ENERGY_SENSOR,
    CONF_SYSTEM_CAPITAL_COST,
    DOMAIN,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
    mock_restore_cache_with_extra_data,
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
    CONF_PUBLIC_CHARGING_RATE: 0.79,
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

    ev = hass.states.get(_money_entity_id(hass, entry, "ev-charge-cost-today"))
    assert ev.attributes["public_charging_rate_gbp_per_kwh"] == 0.79

    cumulative = hass.states.get(_money_entity_id(hass, entry, "cumulative-savings"))
    assert float(cumulative.state) == pytest.approx(-0.15)
    attrs = cumulative.attributes
    assert attrs["capital_cost_gbp"] == 11500.0
    assert "recovered_pct" in attrs
    assert "run_rate_gbp_per_year" in attrs
    assert "projected_breakeven" in attrs
    # Fresh tracking window (day one) is under a season, so the run-rate is
    # flagged provisional and the dated break-even withheld.
    assert attrs["run_rate_provisional"] is True
    assert attrs["projected_breakeven"] is None


async def test_money_sensor_unavailable_during_rate_outage(hass: HomeAssistant) -> None:
    _arrange_entities(hass, soc="50")
    _arrange_money_entities(hass)
    hass.states.async_set(RATE, "unavailable")
    entry = MockConfigEntry(domain=DOMAIN, data=COSTS_CONFIG, entry_id="m2")
    assert await _setup(hass, entry)

    for key in ("counterfactual-cost-today", "savings-today", "cumulative-savings"):
        state = hass.states.get(_money_entity_id(hass, entry, key))
        assert state.state == "unavailable", f"{key} should be unavailable during rate outage"

    hass.states.async_set(RATE, "0.30", {"unit_of_measurement": "GBP/kWh"})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    for key in ("counterfactual-cost-today", "savings-today"):
        state = hass.states.get(_money_entity_id(hass, entry, key))
        assert state.state not in ("unavailable", "unknown"), f"{key} should recover"


async def test_cumulative_unavailable_during_later_rate_outage(hass: HomeAssistant) -> None:
    """Cumulative goes unavailable during a rate outage even after accumulation starts."""
    _arrange_entities(hass, soc="50")
    _arrange_money_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=COSTS_CONFIG, entry_id="m6")
    assert await _setup(hass, entry)

    # Accumulate a non-zero cumulative value first.
    hass.states.async_set(HOUSE, "12.0", {"unit_of_measurement": "kWh"})
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    cumulative_id = _money_entity_id(hass, entry, "cumulative-savings")
    assert hass.states.get(cumulative_id).state not in ("unavailable", "unknown")

    # Rate disappears mid-day — cumulative must go unavailable, not show stale value.
    hass.states.async_set(RATE, "unavailable")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(cumulative_id).state == "unavailable"

    # Rate comes back — sensor recovers.
    hass.states.async_set(RATE, "0.30", {"unit_of_measurement": "GBP/kWh"})
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(cumulative_id).state not in ("unavailable", "unknown")


async def test_status_sensor_exposes_money_sources(hass: HomeAssistant) -> None:
    """The dashboard cards resolve the configured costs entities via this attribute."""
    _arrange_entities(hass, soc="50")
    _arrange_money_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=COSTS_CONFIG, entry_id="m4")
    assert await _setup(hass, entry)

    status_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}-status")
    sources = hass.states.get(status_id).attributes.get("money_sources")
    assert sources is not None
    assert sources["house"] == HOUSE
    assert sources["import_rate"] == RATE
    assert sources["import_cost"] == IMPORT_COST
    assert sources["pv"] == PV
    assert sources["grid_export"] == EXPORT_KWH
    assert sources["ev"] == EV
    # Unconfigured sources are absent, not None — the cards gate on key presence.
    assert "gas" not in sources
    assert "hot_water" not in sources


async def test_status_sensor_money_sources_absent_without_config(hass: HomeAssistant) -> None:
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="m5")
    assert await _setup(hass, entry)

    status_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}-status")
    assert hass.states.get(status_id).attributes.get("money_sources") is None


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


async def test_savings_breakdown_restores_across_restart(hass: HomeAssistant) -> None:
    """The per-line breakdown accumulators resume after a restart, not reset to zero.

    Covers both seed paths: self_use has a live first-tick baseline (merge: restored
    running total + the down-time counter gap priced at the current rate); peak_shift
    has no configured input (adopt the restored snapshot directly).
    """
    _arrange_entities(hass, soc="50")
    _arrange_money_entities(hass)  # PV 8.0 - export 2.0 -> self_use counter 6.0; rate 0.30
    entry = MockConfigEntry(domain=DOMAIN, data=COSTS_CONFIG, entry_id="m6")

    eid = (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}-savings-today",
            suggested_object_id="ec_savings",
        )
        .entity_id
    )
    from homeassistant.util import dt as dt_util

    today = dt_util.now().date().isoformat()
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(eid, "1.20"),
                {
                    "accumulators": {
                        "self_use": {"day": today, "last_counter_kwh": 4.0, "cost_gbp": 1.20},
                        "peak_shift": {"day": today, "last_counter_kwh": 3.0, "cost_gbp": 0.50},
                    }
                },
            )
        ],
    )

    assert await _setup(hass, entry)
    savings = hass.states.get(eid)
    # self_use: restored 1.20 + (6.0 - 4.0) kWh * 0.30 = 1.80 (merge).
    assert savings.attributes["solar_self_use_gbp"] == pytest.approx(1.80)
    # peak_shift: no live input this run, so the restored snapshot is adopted as-is.
    assert savings.attributes["battery_peak_shift_gbp"] == pytest.approx(0.50)

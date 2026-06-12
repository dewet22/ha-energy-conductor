"""Tests for the MoneyTracker HA-glue: entity reads feeding the pure money core."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from custom_components.energy_conductor.const import (
    CONF_DAILY_ENERGY_SENSOR,
    CONF_EV_ENERGY_SENSOR,
    CONF_EV_GREEN_ENERGY_SENSOR,
    CONF_EXPORT_EARNINGS_SENSOR,
    CONF_GAS_RATE_SENSOR,
    CONF_GRID_EXPORT_ENERGY_SENSOR,
    CONF_HOTWATER_ENERGY_SENSOR,
    CONF_IMPORT_COST_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    CONF_PV_ENERGY_SENSOR,
)
from custom_components.energy_conductor.money import CumulativeSavings, DailyCost
from custom_components.energy_conductor.money_tracker import (
    ACC_COUNTERFACTUAL,
    ACC_EV_COST,
    ACC_EV_SOLAR,
    ACC_HOTWATER_GAS,
    ACC_SELF_USE,
    MoneyTracker,
)

HOUSE = "sensor.house_energy"
RATE = "sensor.import_rate"
IMPORT_COST = "sensor.import_cost"
EXPORT_EARNINGS = "sensor.export_earnings"
PV = "sensor.pv_today"
EXPORT_KWH = "sensor.export_today"
EV = "sensor.ev_today"
EV_GREEN = "sensor.ev_green_today"
HW = "sensor.hw_today"
GAS_RATE = "sensor.gas_rate"

FULL_CONFIG = {
    CONF_DAILY_ENERGY_SENSOR: HOUSE,
    CONF_IMPORT_RATE_SENSOR: RATE,
    CONF_IMPORT_COST_SENSOR: IMPORT_COST,
    CONF_EXPORT_EARNINGS_SENSOR: EXPORT_EARNINGS,
    CONF_PV_ENERGY_SENSOR: PV,
    CONF_GRID_EXPORT_ENERGY_SENSOR: EXPORT_KWH,
    CONF_EV_ENERGY_SENSOR: EV,
    CONF_EV_GREEN_ENERGY_SENSOR: EV_GREEN,
    CONF_HOTWATER_ENERGY_SENSOR: HW,
    CONF_GAS_RATE_SENSOR: GAS_RATE,
}

DAY1 = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)
DAY1_LATER = datetime(2026, 6, 12, 10, 30, tzinfo=UTC)
DAY2 = datetime(2026, 6, 13, 0, 1, tzinfo=UTC)


def _set(hass, entity_id, value, unit=None):
    attrs = {"unit_of_measurement": unit} if unit else {}
    hass.states.async_set(entity_id, str(value), attrs)


def _arrange_all(hass):
    _set(hass, HOUSE, 10.0)
    _set(hass, RATE, 0.30, "GBP/kWh")
    _set(hass, IMPORT_COST, 1.00, "GBP")
    _set(hass, EXPORT_EARNINGS, 0.25, "GBP")
    _set(hass, PV, 8.0, "kWh")
    _set(hass, EXPORT_KWH, 2.0, "kWh")
    _set(hass, EV, 0.0, "kWh")
    _set(hass, EV_GREEN, 0.0, "kWh")
    _set(hass, HW, 1.0, "kWh")
    _set(hass, GAS_RATE, 0.056, "GBP/kWh")


async def test_counterfactual_accumulates_house_energy_at_import_rate(hass):
    tracker = MoneyTracker(hass, FULL_CONFIG)
    _arrange_all(hass)
    tracker.tick(DAY1)
    _set(hass, HOUSE, 12.0)
    tracker.tick(DAY1_LATER)
    assert tracker.daily[ACC_COUNTERFACTUAL].cost_gbp == pytest.approx(2.0 * 0.30)


async def test_pence_rate_is_normalised(hass):
    tracker = MoneyTracker(hass, FULL_CONFIG)
    _arrange_all(hass)
    _set(hass, RATE, 30.0, "p/kWh")
    tracker.tick(DAY1)
    _set(hass, HOUSE, 11.0)
    tracker.tick(DAY1_LATER)
    assert tracker.daily[ACC_COUNTERFACTUAL].cost_gbp == pytest.approx(0.30)


async def test_rate_outage_holds_then_prices_gap_at_resumed_rate(hass):
    tracker = MoneyTracker(hass, FULL_CONFIG)
    _arrange_all(hass)
    tracker.tick(DAY1)
    _set(hass, RATE, "unavailable")
    _set(hass, HOUSE, 12.0)
    tracker.tick(DAY1_LATER)
    assert tracker.rate_available is False
    assert tracker.daily[ACC_COUNTERFACTUAL].cost_gbp == 0.0
    _set(hass, RATE, 0.10, "GBP/kWh")
    _set(hass, HOUSE, 13.0)
    tracker.tick(DAY1_LATER)
    assert tracker.rate_available is True
    assert tracker.daily[ACC_COUNTERFACTUAL].cost_gbp == pytest.approx(3.0 * 0.10)


async def test_self_use_is_pv_minus_export_clamped(hass):
    tracker = MoneyTracker(hass, FULL_CONFIG)
    _arrange_all(hass)
    tracker.tick(DAY1)  # baseline: self-use counter = 8 - 2 = 6
    _set(hass, PV, 10.0)  # +2 PV, +1 export -> +1 self-use
    _set(hass, EXPORT_KWH, 3.0)
    tracker.tick(DAY1_LATER)
    assert tracker.daily[ACC_SELF_USE].cost_gbp == pytest.approx(1.0 * 0.30)


async def test_hotwater_line_priced_at_gas_rate(hass):
    tracker = MoneyTracker(hass, FULL_CONFIG)
    _arrange_all(hass)
    tracker.tick(DAY1)
    _set(hass, HW, 3.0)
    tracker.tick(DAY1_LATER)
    assert tracker.daily[ACC_HOTWATER_GAS].cost_gbp == pytest.approx(2.0 * 0.056)


async def test_ev_lines_accumulate(hass):
    tracker = MoneyTracker(hass, FULL_CONFIG)
    _arrange_all(hass)
    tracker.tick(DAY1)
    _set(hass, EV, 5.0)
    _set(hass, EV_GREEN, 2.0)
    tracker.tick(DAY1_LATER)
    assert tracker.daily[ACC_EV_COST].cost_gbp == pytest.approx(5.0 * 0.30)
    assert tracker.daily[ACC_EV_SOLAR].cost_gbp == pytest.approx(2.0 * 0.30)


async def test_unconfigured_lines_stay_none(hass):
    config = {CONF_DAILY_ENERGY_SENSOR: HOUSE, CONF_IMPORT_RATE_SENSOR: RATE}
    tracker = MoneyTracker(hass, config)
    _arrange_all(hass)
    tracker.tick(DAY1)
    assert tracker.daily[ACC_COUNTERFACTUAL] is not None
    assert tracker.daily[ACC_SELF_USE] is None
    assert tracker.daily[ACC_EV_COST] is None
    assert tracker.daily[ACC_HOTWATER_GAS] is None


async def test_savings_and_cumulative_bank_at_rollover(hass):
    tracker = MoneyTracker(hass, FULL_CONFIG)
    _arrange_all(hass)
    tracker.tick(DAY1)
    _set(hass, HOUSE, 20.0)  # counterfactual = 10 * 0.30 = 3.00
    tracker.tick(DAY1_LATER)
    # savings = 3.00 - import cost 1.00 + export 0.25
    assert tracker.savings_today == pytest.approx(2.25)
    assert tracker.cumulative.total_gbp == pytest.approx(2.25)
    # New day: yesterday's savings bank into base; today restarts.
    tracker.tick(DAY2)
    assert tracker.cumulative.base_gbp == pytest.approx(2.25)
    assert tracker.cumulative.day == date(2026, 6, 13)


async def test_savings_none_without_import_cost(hass):
    config = {CONF_DAILY_ENERGY_SENSOR: HOUSE, CONF_IMPORT_RATE_SENSOR: RATE}
    tracker = MoneyTracker(hass, config)
    _arrange_all(hass)
    tracker.tick(DAY1)
    assert tracker.savings_today is None
    assert tracker.cumulative is None


async def test_seed_applies_only_before_first_tick(hass):
    tracker = MoneyTracker(hass, FULL_CONFIG)
    restored = DailyCost(day=date(2026, 6, 12), last_counter_kwh=10.0, cost_gbp=1.5)
    tracker.seed_daily(ACC_COUNTERFACTUAL, restored)
    _arrange_all(hass)
    _set(hass, HOUSE, 11.0)
    tracker.tick(DAY1)
    # Restored cost continued from, not rebaselined: 1.5 + 1 kWh * 0.30.
    assert tracker.daily[ACC_COUNTERFACTUAL].cost_gbp == pytest.approx(1.8)
    # A late seed (after ticking has begun) is ignored.
    tracker.seed_daily(ACC_COUNTERFACTUAL, restored)
    assert tracker.daily[ACC_COUNTERFACTUAL].cost_gbp == pytest.approx(1.8)


async def test_seed_cumulative_restores_bank(hass):
    tracker = MoneyTracker(hass, FULL_CONFIG)
    tracker.seed_cumulative(
        CumulativeSavings(
            day=date(2026, 6, 11), started=date(2026, 6, 1), base_gbp=40.0, today_gbp=2.0
        )
    )
    _arrange_all(hass)
    _set(hass, HOUSE, 20.0)
    tracker.tick(DAY1)
    # Restored from a previous day: yesterday's 2.00 banks, today starts fresh.
    assert tracker.cumulative.base_gbp == pytest.approx(42.0)
    assert tracker.cumulative.started == date(2026, 6, 1)

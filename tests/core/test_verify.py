"""Tests for the actuation verifier (anti-drain + charge checks)."""

from __future__ import annotations

import pytest

from energy_conductor.decisions import Decision, DecisionKind
from energy_conductor.verify import (
    check_actuation,
    check_charge_actuation,
    check_discharge_actuation,
)

from .builders import a_battery, a_grid_state, a_site_state, a_tariff


def _cap(value: int = 0) -> Decision:
    """A discharge decision; value=0 is the off-peak anti-drain cap."""
    return Decision(
        kind=DecisionKind.SET_DISCHARGE_LIMIT,
        target_entity="number.discharge",
        value=value,
        reason="off-peak",
        dedupe_key=f"discharge-{value}",
    )


def _charge(target: int = 80) -> Decision:
    return Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity="number.charge",
        value=target,
        reason="overnight plan",
        dedupe_key=f"overnight-{target}",
    )


def _state(*, off_peak: bool = True, battery_power: float | None, soc: float = 50.0, grid=None):
    return a_site_state(
        tariff=a_tariff(off_peak_now=off_peak),
        battery=a_battery(power_w=battery_power, soc_percent=soc),
        grid=grid,
    )


# --- discharge (anti-drain) -------------------------------------------------


def test_discharge_mismatch_when_battery_discharges_under_cap():
    result = check_discharge_actuation(_state(battery_power=2000.0), _cap(0), "applied")
    assert result is not None
    assert result.ok is False
    assert "discharging" in result.detail
    assert "2000" in result.detail


def test_discharge_ok_when_battery_idle():
    result = check_discharge_actuation(_state(battery_power=40.0), _cap(0), "applied")
    assert result.ok is True
    assert "idle" in result.detail


def test_discharge_ok_and_labelled_charging():
    result = check_discharge_actuation(_state(battery_power=-2000.0), _cap(0), "applied")
    assert result.ok is True
    assert "charging at 2000 W" in result.detail


def test_discharge_grid_import_in_detail():
    state = _state(battery_power=2000.0, grid=a_grid_state(import_w=120.0))
    assert "grid import 120 W" in check_discharge_actuation(state, _cap(0), "applied").detail


def test_discharge_unchanged_outcome_still_checked():
    assert check_discharge_actuation(_state(battery_power=2000.0), _cap(0), "unchanged").ok is False


@pytest.mark.parametrize("outcome", ["dry_run", "failed", None, "notified"])
def test_discharge_not_applicable_when_not_live(outcome):
    assert check_discharge_actuation(_state(battery_power=2000.0), _cap(0), outcome) is None


def test_discharge_not_applicable_when_not_capped():
    assert check_discharge_actuation(_state(battery_power=2000.0), _cap(3000), "applied") is None


def test_discharge_not_applicable_when_not_off_peak():
    state = _state(off_peak=False, battery_power=2000.0)
    assert check_discharge_actuation(state, _cap(0), "applied") is None


def test_discharge_not_applicable_without_battery_power():
    assert check_discharge_actuation(_state(battery_power=None), _cap(0), "applied") is None


def test_discharge_not_applicable_for_charge_decision():
    assert check_discharge_actuation(_state(battery_power=2000.0), _charge(80), "applied") is None
    assert check_discharge_actuation(_state(battery_power=2000.0), None, "applied") is None


# --- charge -----------------------------------------------------------------


def test_charge_mismatch_when_not_charging_below_target():
    # SoC 50 < target 80, off-peak, battery idle (not drawing) → should be charging.
    result = check_charge_actuation(_state(battery_power=0.0, soc=50.0), _charge(80), "applied")
    assert result is not None
    assert result.ok is False
    assert "charge target 80%" in result.detail
    assert "not charging" in result.detail


def test_charge_ok_when_charging():
    result = check_charge_actuation(_state(battery_power=-2500.0, soc=50.0), _charge(80), "applied")
    assert result.ok is True
    assert "charging to 80%" in result.detail


def test_charge_not_applicable_when_soc_at_target():
    assert (
        check_charge_actuation(_state(battery_power=0.0, soc=85.0), _charge(80), "applied") is None
    )


def test_charge_not_applicable_when_not_off_peak():
    state = _state(off_peak=False, battery_power=0.0, soc=50.0)
    assert check_charge_actuation(state, _charge(80), "applied") is None


@pytest.mark.parametrize("outcome", ["dry_run", "failed", None])
def test_charge_not_applicable_when_not_live(outcome):
    assert check_charge_actuation(_state(battery_power=0.0, soc=50.0), _charge(80), outcome) is None


def test_charge_not_applicable_for_discharge_decision():
    assert check_charge_actuation(_state(battery_power=0.0, soc=50.0), _cap(0), "applied") is None


def test_charge_not_applicable_without_battery_power():
    state = _state(battery_power=None, soc=50.0)
    assert check_charge_actuation(state, _charge(80), "applied") is None


# --- combiner ---------------------------------------------------------------


def test_combiner_none_when_neither_applies():
    state = _state(off_peak=False, battery_power=0.0)
    assert (
        check_actuation(
            state, discharge=_cap(0), discharge_outcome="applied", charge=None, charge_outcome=None
        )
        is None
    )


def test_combiner_discharge_mismatch_wins():
    # Discharge mismatch (battery discharging) reported even if charge would be ok.
    state = _state(battery_power=2000.0, soc=50.0)
    result = check_actuation(
        state,
        discharge=_cap(0),
        discharge_outcome="applied",
        charge=_charge(80),
        charge_outcome="applied",
    )
    assert result.ok is False
    assert "discharging" in result.detail


def test_combiner_charge_mismatch_when_discharge_ok():
    # Battery idle (0 W): discharge ok, but should be charging toward target → charge mismatch.
    state = _state(battery_power=0.0, soc=50.0)
    result = check_actuation(
        state,
        discharge=_cap(0),
        discharge_outcome="applied",
        charge=_charge(80),
        charge_outcome="applied",
    )
    assert result.ok is False
    assert "charge target" in result.detail


def test_combiner_ok_when_charging():
    # Charging: discharge ok (not discharging) and charge ok (drawing toward target).
    state = _state(battery_power=-2500.0, soc=50.0)
    result = check_actuation(
        state,
        discharge=_cap(0),
        discharge_outcome="applied",
        charge=_charge(80),
        charge_outcome="applied",
    )
    assert result.ok is True

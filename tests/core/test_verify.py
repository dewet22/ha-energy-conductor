"""Tests for the actuation verifier (anti-drain check)."""

from __future__ import annotations

import pytest

from energy_conductor.decisions import Decision, DecisionKind
from energy_conductor.verify import check_actuation

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


def _state(*, off_peak: bool = True, battery_power: float | None, grid=None):
    return a_site_state(
        tariff=a_tariff(off_peak_now=off_peak),
        battery=a_battery(power_w=battery_power),
        grid=grid,
    )


def test_mismatch_when_battery_discharges_under_cap():
    state = _state(battery_power=2000.0)
    result = check_actuation(state, _cap(0), "applied")
    assert result is not None
    assert result.ok is False
    assert "discharging" in result.detail
    assert "2000" in result.detail


def test_ok_when_battery_idle_under_cap():
    state = _state(battery_power=40.0)  # below VERIFY_DISCHARGE_THRESHOLD_W
    result = check_actuation(state, _cap(0), "applied")
    assert result is not None
    assert result.ok is True


def test_grid_import_included_in_detail():
    state = _state(battery_power=2000.0, grid=a_grid_state(import_w=120.0))
    result = check_actuation(state, _cap(0), "applied")
    assert "grid import 120 W" in result.detail


def test_unchanged_outcome_is_still_checked():
    # A steady off-peak tick reports "unchanged" (the cap is deduped) but is still live.
    state = _state(battery_power=2000.0)
    assert check_actuation(state, _cap(0), "unchanged").ok is False


@pytest.mark.parametrize("outcome", ["dry_run", "failed", None, "notified"])
def test_not_applicable_when_cap_not_live(outcome):
    state = _state(battery_power=2000.0)
    assert check_actuation(state, _cap(0), outcome) is None


def test_not_applicable_when_not_capped():
    state = _state(battery_power=2000.0)
    assert check_actuation(state, _cap(3000), "applied") is None


def test_not_applicable_when_not_off_peak():
    state = _state(off_peak=False, battery_power=2000.0)
    assert check_actuation(state, _cap(0), "applied") is None


def test_not_applicable_without_battery_power():
    state = _state(battery_power=None)
    assert check_actuation(state, _cap(0), "applied") is None


def test_not_applicable_for_other_decision_kinds():
    state = _state(battery_power=2000.0)
    charge = Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity="number.charge",
        value=0,
        reason="x",
        dedupe_key="overnight-0",
    )
    assert check_actuation(state, charge, "applied") is None
    assert check_actuation(state, None, "applied") is None

"""Tests for the actuation verifier (anti-drain check)."""

from __future__ import annotations

import pytest

from energy_conductor.decisions import Decision, DecisionKind
from energy_conductor.verify import (
    check_actuation,
    check_time_write_landed,
    check_write_landed,
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
    assert "idle" in result.detail


def test_ok_and_labelled_charging_when_battery_charging():
    # Charging is fine under a discharge cap; the detail should say "charging", not "idle".
    state = _state(battery_power=-2000.0)
    result = check_actuation(state, _cap(0), "applied")
    assert result.ok is True
    assert "charging at 2000 W" in result.detail


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


# --- write-readback (check_write_landed) -------------------------------------


def test_write_landed_match():
    result = check_write_landed("set_discharge_limit", 0.0, 0.0)
    assert result.ok is True
    assert "as commanded" in result.detail


def test_write_landed_within_tolerance():
    # Setpoint registers are integers; allow float/rounding slack only.
    assert check_write_landed("set_discharge_limit", 50.0, 50.4).ok is True


def test_write_landed_flip_back_mismatch():
    result = check_write_landed("set_discharge_limit", 0.0, 50.0)
    assert result.ok is False
    assert "commanded set_discharge_limit=0 but entity reads 50" in result.detail


def test_write_landed_detail_uses_label_not_entity_id():
    # The detail flows into diagnostics + notifications, so it must carry a non-identifying
    # label (the decision kind), never the entity_id (room/device names) — Codex review.
    for value in (0.0, 50.0):  # both ok and mismatch paths
        detail = check_write_landed("set_discharge_limit", 0.0, value).detail
        assert "number." not in detail
        assert "set_discharge_limit" in detail


def test_write_landed_unreadable_entity_no_verdict():
    assert check_write_landed("set_discharge_limit", 0.0, None) is None


# --- time-entity readback (check_time_write_landed) --------------------------


def test_time_write_landed_match():
    result = check_time_write_landed("set_slot_time", "00:00:00", "00:00:00")
    assert result is not None and result.ok
    assert "as commanded" in result.detail


def test_time_write_landed_mismatch():
    result = check_time_write_landed("set_slot_time", "00:00:00", "23:30:00")
    assert result is not None and not result.ok
    assert "00:00:00" in result.detail and "23:30:00" in result.detail
    # Same privacy contract as the numeric readback: label, never entity_id.
    assert "time." not in result.detail


def test_time_write_landed_unreadable():
    assert check_time_write_landed("set_slot_time", "00:00:00", None) is None

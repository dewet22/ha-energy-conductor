"""Tests for the actuation verifier (anti-drain check)."""

from __future__ import annotations

import pytest

from energy_conductor.const import VERIFY_CHARGE_SOC_MARGIN
from energy_conductor.decisions import Decision, DecisionKind
from energy_conductor.verify import (
    check_actuation,
    check_charge_actuation,
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


def _state(
    *, off_peak: bool = True, dispatching: bool = False, battery_power: float | None, grid=None
):
    return a_site_state(
        tariff=a_tariff(off_peak_now=off_peak, ev_dispatching_now=dispatching),
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
    # Neither off-peak flag: the guard wouldn't be capping, so there is nothing to assert.
    state = _state(off_peak=False, battery_power=2000.0)
    assert check_actuation(state, _cap(0), "applied") is None


def test_dispatch_only_cap_is_judged():
    """The guard caps on off-peak OR dispatch, so verification must cover both.

    A dispatch outside the off-peak window is precisely the EV-drain scenario this check
    exists for — declining to judge it would blind the check where it matters most.
    """
    drained = _state(off_peak=False, dispatching=True, battery_power=2000.0)
    result = check_actuation(drained, _cap(0), "applied")
    assert result is not None
    assert result.ok is False
    assert "discharging" in result.detail

    idle = _state(off_peak=False, dispatching=True, battery_power=10.0)
    assert check_actuation(idle, _cap(0), "applied").ok is True


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


# --- charge actuation (check_charge_actuation) -------------------------------


def _setpoint(value: float = 100) -> Decision:
    return Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity="number.charge",
        value=value,
        reason="off-peak",
        dedupe_key=f"setpoint-off_peak_charge-{value:g}",
    )


def _charge_state(*, soc: float, battery_power: float | None, grid=None):
    return a_site_state(
        tariff=a_tariff(off_peak_now=True),
        battery=a_battery(soc_percent=soc, power_w=battery_power),
        grid=grid,
    )


def test_charge_stall_when_setpoint_above_soc_and_battery_idle():
    """The Nov 2026 incident: setpoint 100 landed, SoC sat at the floor, battery idle at
    ~2 W for two weeks — the anti-drain check read that as a healthy "idle"."""
    result = check_charge_actuation(
        _charge_state(soc=3, battery_power=2.0), _setpoint(100), "unchanged"
    )
    assert result is not None
    assert result.ok is False
    assert "setpoint 100%" in result.detail
    assert "SoC 3%" in result.detail
    assert "not charging" in result.detail


def test_charge_stall_detail_includes_grid_import():
    state = _charge_state(soc=3, battery_power=2.0, grid=a_grid_state(import_w=450.0))
    result = check_charge_actuation(state, _setpoint(100), "applied")
    assert "grid import 450 W" in result.detail


def test_charge_ok_when_battery_charging():
    result = check_charge_actuation(
        _charge_state(soc=30, battery_power=-2500.0), _setpoint(100), "applied"
    )
    assert result is not None
    assert result.ok is True
    assert "charging at 2500 W" in result.detail


def test_charge_stall_when_battery_discharging_below_setpoint():
    result = check_charge_actuation(
        _charge_state(soc=30, battery_power=800.0), _setpoint(100), "applied"
    )
    assert result.ok is False


def test_charge_not_judged_within_soc_margin_of_setpoint():
    # Near the setpoint the charge tapers (and a full battery holds) — no verdict.
    soc = 100 - VERIFY_CHARGE_SOC_MARGIN
    assert (
        check_charge_actuation(_charge_state(soc=soc, battery_power=0.0), _setpoint(100), "applied")
        is None
    )


def test_charge_not_judged_at_self_consume_floor():
    # Self-consume setpoint = control minimum; SoC hovering at the floor is not a stall.
    assert (
        check_charge_actuation(_charge_state(soc=3, battery_power=0.0), _setpoint(4), "applied")
        is None
    )


@pytest.mark.parametrize("outcome", ["dry_run", "failed", None, "notified"])
def test_charge_not_judged_when_setpoint_not_live(outcome):
    assert (
        check_charge_actuation(_charge_state(soc=3, battery_power=0.0), _setpoint(100), outcome)
        is None
    )


def test_charge_not_judged_without_battery_power():
    assert (
        check_charge_actuation(_charge_state(soc=3, battery_power=None), _setpoint(100), "applied")
        is None
    )


def test_charge_not_judged_for_other_decision_kinds():
    stalled = _charge_state(soc=3, battery_power=0.0)
    assert check_charge_actuation(stalled, _cap(0), "applied") is None
    assert check_charge_actuation(stalled, None, "applied") is None


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

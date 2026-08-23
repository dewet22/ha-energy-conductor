"""Regime engine: cheap windows fill to 100, otherwise get out of the way."""

from energy_conductor.decisions import DecisionKind
from energy_conductor.regimes import charge_setpoint, current_regime

from .builders import a_battery, a_site_state, a_tariff

TARGET = "number.charge_target"


def _decide(**state_overrides):
    return charge_setpoint(a_site_state(**state_overrides), target_entity=TARGET)


class TestCurrentRegime:
    def test_off_peak_is_cheap_charge(self):
        state = a_site_state(tariff=a_tariff(off_peak_now=True, ev_dispatching_now=False))
        assert current_regime(state) == "cheap_charge"

    def test_dispatch_alone_is_cheap_charge(self):
        # Dispatch outside the fixed window: off_peak usually flips lock-step, but the
        # regime must not depend on that coupling.
        state = a_site_state(tariff=a_tariff(off_peak_now=False, ev_dispatching_now=True))
        assert current_regime(state) == "cheap_charge"

    def test_neither_is_self_consume(self):
        state = a_site_state(tariff=a_tariff(off_peak_now=False, ev_dispatching_now=False))
        assert current_regime(state) == "self_consume"


class TestChargeSetpoint:
    def test_cheap_charge_setpoint_is_100(self):
        decision = _decide(tariff=a_tariff(off_peak_now=True))
        assert decision.kind is DecisionKind.SET_CHARGE_TARGET
        assert decision.target_entity == TARGET
        assert decision.value == 100
        assert decision.dedupe_key == "setpoint-cheap_charge-100"

    def test_self_consume_setpoint_is_control_minimum(self):
        # Deliberately not the model's 4.0 default, and not integral: a setpoint that
        # hardcoded the default would pass against 4.0, and only a fractional minimum
        # exercises the dedupe key's `:g` formatting.
        decision = _decide(battery=a_battery(charge_target_min_percent=7.5))
        assert decision.value == 7.5
        assert decision.dedupe_key == "setpoint-self_consume-7.5"

    def test_setpoint_reason_mentions_regime(self):
        assert "cheap" in _decide(tariff=a_tariff(off_peak_now=True)).reason.lower()

    def test_regime_change_changes_dedupe_key(self):
        cheap = _decide(tariff=a_tariff(off_peak_now=True))
        self_consume = _decide()
        assert cheap.dedupe_key != self_consume.dedupe_key

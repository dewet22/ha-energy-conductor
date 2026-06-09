"""Tests for the three-regime discharge guard (spec §4.2).

Regime table:
  1. off_peak_now             → limit 0W
  2. pre-off-peak hold window → limit 0W
  3. default                  → limit max_discharge_power_w

The former "EV dispatch → cap at baseline" regime was removed: on a whole-house
meter, dispatch always coincides with off_peak, so regime 1 already idles the
battery while the EV grid-charges. EV state no longer affects the discharge limit.
"""

from datetime import timedelta

from energy_conductor.decisions import DecisionKind
from energy_conductor.discharge_guard import discharge_limit

from .builders import DEFAULT_NOW, a_battery, a_site_state, a_tariff, an_ev_charger

DISCHARGE_ENTITY = "number.inverter_discharge_power_limit"


def _decide(**state_overrides):
    state = a_site_state(**state_overrides)
    return discharge_limit(state, target_entity=DISCHARGE_ENTITY)


class TestDischargeRegimes:
    def test_default_unconstrained(self):
        decision = _decide(battery=a_battery(max_discharge_power_w=3000))
        assert decision.kind == DecisionKind.SET_DISCHARGE_LIMIT
        assert decision.value == 3000
        assert "unconstrained" in decision.reason.lower()

    def test_off_peak_suppresses(self):
        decision = _decide(tariff=a_tariff(off_peak_now=True))
        assert decision.value == 0
        assert "off-peak" in decision.reason.lower()

    def test_off_peak_suppresses_regardless_of_ev(self):
        # Off-peak idles the battery whether or not the EV is dispatching/drawing.
        decision = _decide(
            tariff=a_tariff(off_peak_now=True, ev_dispatching_now=True),
            ev_charger=an_ev_charger(power_w=2000.0),
        )
        assert decision.value == 0

    def test_ev_dispatch_no_longer_caps_discharge(self):
        # Regime 3 removed: a dispatching, hard-drawing EV outside off-peak no longer
        # caps the battery — the limit is unconstrained. (In practice a real dispatch
        # always coincides with off_peak, which idles the battery via regime 1.)
        decision = _decide(
            tariff=a_tariff(ev_dispatching_now=True, off_peak_now=False),
            ev_charger=an_ev_charger(power_w=7000.0, min_activation_power_w=1400),
            battery=a_battery(max_discharge_power_w=3000),
        )
        assert decision.value == 3000
        assert "unconstrained" in decision.reason.lower()


class TestDedupeKeyBucketing:
    def test_regime_change_changes_key(self):
        d_default = _decide()
        d_off_peak = _decide(tariff=a_tariff(off_peak_now=True))
        assert d_default.dedupe_key != d_off_peak.dedupe_key


class TestPreOffPeakHold:
    def test_holds_at_zero_when_within_hold_window(self):
        decision = _decide(
            tariff=a_tariff(next_off_peak_window_start=DEFAULT_NOW + timedelta(minutes=15)),
        )
        assert decision.value == 0
        assert "pre-off-peak" in decision.reason.lower()

    def test_off_peak_now_takes_priority_over_pre_hold(self):
        decision = _decide(
            tariff=a_tariff(
                off_peak_now=True,
                next_off_peak_window_start=DEFAULT_NOW + timedelta(minutes=15),
            ),
        )
        assert decision.value == 0
        assert "off-peak rate" in decision.reason.lower()

    def test_beyond_hold_window_is_unconstrained(self):
        decision = _decide(
            tariff=a_tariff(next_off_peak_window_start=DEFAULT_NOW + timedelta(minutes=31)),
            battery=a_battery(max_discharge_power_w=3000),
        )
        assert decision.value == 3000

    def test_no_start_sensor_is_unconstrained(self):
        decision = _decide(
            tariff=a_tariff(next_off_peak_window_start=None),
            battery=a_battery(max_discharge_power_w=3000),
        )
        assert decision.value == 3000

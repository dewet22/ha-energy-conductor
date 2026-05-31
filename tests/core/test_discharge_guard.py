"""Tests for the three-regime discharge guard (spec §4.2).

Regime table:
  1. off_peak_now              → limit 0W
  2. ev_dispatching_now AND EV drawing → limit baseline_load_w
  3. default                        → limit max_discharge_power_w
"""

from energy_conductor.decisions import DecisionKind
from energy_conductor.discharge_guard import discharge_limit

from .builders import a_battery, a_site_state, a_tariff, an_ev_charger

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

    def test_off_peak_takes_priority_over_ev_dispatch(self):
        decision = _decide(
            tariff=a_tariff(off_peak_now=True, ev_dispatching_now=True),
            ev_charger=an_ev_charger(power_w=2000.0),
        )
        assert decision.value == 0

    def test_ev_dispatch_with_drawing_ev_caps_at_baseline(self):
        decision = _decide(
            tariff=a_tariff(ev_dispatching_now=True),
            ev_charger=an_ev_charger(power_w=2000.0, min_activation_power_w=1400),
            baseline_load_w=480.0,
        )
        assert decision.value == 480
        assert "ev dispatch" in decision.reason.lower()

    def test_ev_dispatch_without_drawing_ev_is_unconstrained(self):
        decision = _decide(
            tariff=a_tariff(ev_dispatching_now=True),
            ev_charger=an_ev_charger(power_w=800.0, min_activation_power_w=1400),
            battery=a_battery(max_discharge_power_w=3000),
        )
        assert decision.value == 3000

    def test_no_ev_charger_configured_treats_as_no_dispatch(self):
        decision = _decide(
            tariff=a_tariff(ev_dispatching_now=True),
            ev_charger=None,
            battery=a_battery(max_discharge_power_w=3000),
        )
        assert decision.value == 3000


class TestActivationThreshold:
    def test_at_threshold_treated_as_drawing(self):
        decision = _decide(
            tariff=a_tariff(ev_dispatching_now=True),
            ev_charger=an_ev_charger(power_w=1400.0, min_activation_power_w=1400),
            baseline_load_w=500.0,
        )
        assert decision.value == 500

    def test_below_threshold_not_drawing(self):
        decision = _decide(
            tariff=a_tariff(ev_dispatching_now=True),
            ev_charger=an_ev_charger(power_w=1399.0, min_activation_power_w=1400),
            battery=a_battery(max_discharge_power_w=3000),
        )
        assert decision.value == 3000


class TestDedupeKeyBucketing:
    def test_similar_baseline_loads_share_bucket(self):
        d1 = _decide(
            tariff=a_tariff(ev_dispatching_now=True),
            ev_charger=an_ev_charger(power_w=2000.0),
            baseline_load_w=480.0,
        )
        d2 = _decide(
            tariff=a_tariff(ev_dispatching_now=True),
            ev_charger=an_ev_charger(power_w=2000.0),
            baseline_load_w=520.0,
        )
        # both bucket to 4xx/5xx in 100W increments; specifically 480//100=4 and 520//100=5
        # so dedupe keys differ — but adjacent 480 and 499 should NOT
        d3 = _decide(
            tariff=a_tariff(ev_dispatching_now=True),
            ev_charger=an_ev_charger(power_w=2000.0),
            baseline_load_w=499.0,
        )
        assert d1.dedupe_key == d3.dedupe_key  # both bucket 4
        assert d1.dedupe_key != d2.dedupe_key  # 4 vs 5

    def test_regime_change_changes_key(self):
        d_default = _decide()
        d_off_peak = _decide(tariff=a_tariff(off_peak_now=True))
        assert d_default.dedupe_key != d_off_peak.dedupe_key

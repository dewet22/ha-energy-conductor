"""Tests for the two-regime discharge guard (spec §4.2).

Regime table:
  1. off_peak_now → limit 0W
  2. default      → limit max_discharge_power_w

Two regimes were removed:
- "EV dispatch → cap at baseline": on a whole-house meter dispatch always
  coincides with off_peak, so regime 1 already idles the battery. EV state no
  longer affects the discharge limit.
- "pre-off-peak hold" (idle for 30 min before off-peak): those minutes are still
  peak, where discharging saves the most, and the held-back charge had no payoff.
  The battery now discharges right up to off-peak (see the counter-test below).
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


class TestDedupeKey:
    def test_regime_change_changes_key(self):
        d_default = _decide()
        d_off_peak = _decide(tariff=a_tariff(off_peak_now=True))
        assert d_default.dedupe_key != d_off_peak.dedupe_key

    def test_percent_scale_max_distinct_from_off_peak(self):
        # On a %-based discharge control, the entity's `max` reads as 50 (not watts), so
        # max_discharge_power_w is 50. Off-peak (0) and unconstrained (50) must still produce
        # DISTINCT dedupe keys — the old `// 100` bucketing collapsed both to "discharge-0",
        # so the off-peak transition was deduped away and never written (the live bug).
        d_unconstrained = _decide(battery=a_battery(max_discharge_power_w=50))
        d_off_peak = _decide(
            tariff=a_tariff(off_peak_now=True),
            battery=a_battery(max_discharge_power_w=50),
        )
        assert d_unconstrained.value == 50
        assert d_off_peak.value == 0
        assert d_unconstrained.dedupe_key != d_off_peak.dedupe_key


class TestNoPreOffPeakHold:
    """The pre-off-peak hold was removed — the battery discharges right up to off-peak."""

    def test_keeps_discharging_when_off_peak_is_near(self):
        # Counter-test for the removed pre-hold. Off-peak opens in 15 min (the old
        # 30-min hold would have idled the battery here), but those minutes are still
        # peak — the most valuable discharge of the day. The battery must KEEP
        # discharging (unconstrained), not hold back. Guards against re-introducing
        # a pre-hold; "if it runs out before off-peak, that's fine" (reserve floor
        # handles the bottom).
        decision = _decide(
            tariff=a_tariff(next_off_peak_window_start=DEFAULT_NOW + timedelta(minutes=15)),
            battery=a_battery(max_discharge_power_w=3000),
        )
        assert decision.value == 3000
        assert "unconstrained" in decision.reason.lower()

    def test_off_peak_now_still_idles_regardless_of_next_start(self):
        # Once off-peak is actually active, idle — independent of next-start timing.
        decision = _decide(
            tariff=a_tariff(
                off_peak_now=True,
                next_off_peak_window_start=DEFAULT_NOW + timedelta(minutes=15),
            ),
        )
        assert decision.value == 0
        assert "off-peak rate" in decision.reason.lower()

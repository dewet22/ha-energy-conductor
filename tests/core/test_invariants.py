"""Property-based test for a spec invariant (§8).

1. discharge_limit() returns only 0 (off-peak energy: window or dispatch) or
   max_discharge_power_w (default) — EV power and the activation threshold never
   influence it, and the removed baseline-cap regime must never reappear.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from energy_conductor.discharge_guard import discharge_limit

from .builders import a_battery, a_site_state, a_tariff, an_ev_charger

# Reasonable bounds — avoid degenerate cases hypothesis would otherwise explore.
load_w = st.floats(min_value=50, max_value=5000, allow_nan=False, allow_infinity=False)


@given(
    power_w=st.floats(min_value=0, max_value=8000, allow_nan=False, allow_infinity=False),
    min_activation=st.integers(min_value=500, max_value=3000),
    baseline=load_w,
    off_peak_now=st.booleans(),
    dispatching_now=st.booleans(),
)
@settings(max_examples=200)
def test_discharge_no_intermediate_regime(
    power_w, min_activation, baseline, off_peak_now, dispatching_now
):
    """The limit is fully determined by the tariff flags: 0 when either off-peak
    flag is set, max_discharge_power_w otherwise — regardless of EV power, activation
    threshold, or baseline load. In particular round(baseline) (the removed dispatch
    baseline-cap regime) must never be returned."""
    state = a_site_state(
        tariff=a_tariff(off_peak_now=off_peak_now, ev_dispatching_now=dispatching_now),
        ev_charger=an_ev_charger(power_w=power_w, min_activation_power_w=min_activation),
        battery=a_battery(max_discharge_power_w=3000),
        baseline_load_w=baseline,
    )
    decision = discharge_limit(state, target_entity="number.discharge_limit")
    expected = 0 if (off_peak_now or dispatching_now) else 3000
    assert decision.value == expected

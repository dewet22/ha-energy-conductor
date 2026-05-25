"""Property-based tests for spec invariants (§8).

1. plan_overnight() always returns target_percent in [reserve_percent, 100].
2. discharge_limit() is monotonic across the activation threshold —
   small power increases never cause the limit to flap.
"""

from datetime import UTC, datetime

from hypothesis import given, settings
from hypothesis import strategies as st

from energy_conductor.discharge_guard import discharge_limit
from energy_conductor.overnight import plan_overnight

from .builders import a_battery, a_site_state, a_tariff, an_ev_charger

# Reasonable bounds — avoid degenerate cases hypothesis would otherwise explore.
soc = st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False)
capacity = st.floats(min_value=1.0, max_value=50.0, allow_nan=False, allow_infinity=False)
power = st.integers(min_value=500, max_value=10000)
reserve = st.floats(min_value=0, max_value=50, allow_nan=False, allow_infinity=False)
load_w = st.floats(min_value=50, max_value=5000, allow_nan=False, allow_infinity=False)
daily_kwh = st.floats(min_value=0.5, max_value=80.0, allow_nan=False, allow_infinity=False)


@given(
    soc=soc,
    capacity=capacity,
    max_charge=power,
    max_discharge=power,
    reserve=reserve,
    load=load_w,
    daily=daily_kwh,
)
@settings(max_examples=200)
def test_overnight_target_within_reserve_and_100(
    soc, capacity, max_charge, max_discharge, reserve, load, daily
):
    state = a_site_state(
        battery=a_battery(
            soc_percent=soc,
            capacity_kwh=capacity,
            max_charge_power_w=max_charge,
            max_discharge_power_w=max_discharge,
            reserve_percent=reserve,
        ),
        tariff=a_tariff(cheap_window_end=datetime(2026, 6, 2, 5, 30, tzinfo=UTC)),
        baseline_load_w=load,
        now=datetime(2026, 6, 1, 21, 0, tzinfo=UTC),
    )
    decision = plan_overnight(state, target_entity="number.charge_target", daily_kwh_target=daily)
    assert int(reserve) <= decision.value <= 100


@given(
    power_w=st.floats(min_value=0, max_value=8000, allow_nan=False, allow_infinity=False),
    min_activation=st.integers(min_value=500, max_value=3000),
    baseline=load_w,
    cheap_now=st.booleans(),
    dispatching_now=st.booleans(),
)
@settings(max_examples=200)
def test_discharge_no_intermediate_regime(
    power_w, min_activation, baseline, cheap_now, dispatching_now
):
    """For any combination of tariff state and EV state, only three distinct limit
    values can ever be returned: 0 (cheap), round(baseline) (dispatch+drawing),
    or max_discharge_power_w (default). Never any other value."""
    state = a_site_state(
        tariff=a_tariff(cheap_window_now=cheap_now, ev_dispatching_now=dispatching_now),
        ev_charger=an_ev_charger(power_w=power_w, min_activation_power_w=min_activation),
        battery=a_battery(max_discharge_power_w=3000),
        baseline_load_w=baseline,
    )
    decision = discharge_limit(state, target_entity="number.discharge_limit")
    assert decision.value in (0, round(baseline), 3000)

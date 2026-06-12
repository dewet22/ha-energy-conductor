"""Tests for the pure-core money arithmetic (tick pricing, rollover, payback)."""

from __future__ import annotations

from datetime import date

import pytest
from custom_components.energy_conductor.money import (
    CumulativeSavings,
    DailyCost,
    accumulate_daily_cost,
    normalise_rate,
    payback_projection,
    roll_cumulative,
    savings_today_gbp,
)

DAY = date(2026, 6, 12)
NEXT_DAY = date(2026, 6, 13)


class TestNormaliseRate:
    def test_gbp_per_kwh_passes_through(self):
        assert normalise_rate(0.30, "GBP/kWh") == 0.30

    def test_pound_sign_unit_passes_through(self):
        # ASCII-only source elsewhere; the unit string itself may carry the symbol.
        assert normalise_rate(0.069, "£/kWh") == 0.069

    def test_pence_is_divided_by_100(self):
        assert normalise_rate(6.9, "p/kWh") == pytest.approx(0.069)

    def test_gbp_pence_unit_is_divided_by_100(self):
        assert normalise_rate(30.0, "GBp/kWh") == pytest.approx(0.30)

    def test_pence_word_unit_is_divided_by_100(self):
        assert normalise_rate(12.0, "pence/kWh") == pytest.approx(0.12)

    def test_unit_match_is_case_insensitive(self):
        assert normalise_rate(6.9, "P/KWH") == pytest.approx(0.069)

    def test_missing_unit_assumes_gbp(self):
        assert normalise_rate(0.15, None) == 0.15

    def test_unrecognised_unit_refuses(self):
        assert normalise_rate(0.25, "EUR/kWh") is None

    def test_none_value_is_none(self):
        assert normalise_rate(None, "GBP/kWh") is None


class TestAccumulateDailyCost:
    def test_first_tick_sets_baseline_with_zero_cost(self):
        state = accumulate_daily_cost(None, day=DAY, counter_kwh=5.0, rate_gbp_per_kwh=0.30)
        assert state == DailyCost(day=DAY, last_counter_kwh=5.0, cost_gbp=0.0)

    def test_delta_is_priced_at_the_rate_in_force(self):
        state = DailyCost(day=DAY, last_counter_kwh=5.0, cost_gbp=0.0)
        state = accumulate_daily_cost(state, day=DAY, counter_kwh=6.0, rate_gbp_per_kwh=0.30)
        assert state.cost_gbp == pytest.approx(0.30)
        assert state.last_counter_kwh == 6.0

    def test_rate_change_prices_each_delta_at_its_own_rate(self):
        state = DailyCost(day=DAY, last_counter_kwh=0.0, cost_gbp=0.0)
        state = accumulate_daily_cost(state, day=DAY, counter_kwh=2.0, rate_gbp_per_kwh=0.069)
        state = accumulate_daily_cost(state, day=DAY, counter_kwh=3.0, rate_gbp_per_kwh=0.30)
        assert state.cost_gbp == pytest.approx(2 * 0.069 + 1 * 0.30)

    def test_new_day_resets_cost_and_rebaselines(self):
        state = DailyCost(day=DAY, last_counter_kwh=12.5, cost_gbp=2.5)
        state = accumulate_daily_cost(state, day=NEXT_DAY, counter_kwh=12.6, rate_gbp_per_kwh=0.30)
        assert state.day == NEXT_DAY
        assert state.cost_gbp == 0.0
        assert state.last_counter_kwh == 12.6

    def test_counter_reset_within_day_prices_from_zero(self):
        # A daily-reset source whose midnight lags our clock's day flip.
        state = DailyCost(day=DAY, last_counter_kwh=12.5, cost_gbp=2.5)
        state = accumulate_daily_cost(state, day=DAY, counter_kwh=0.2, rate_gbp_per_kwh=0.069)
        assert state.cost_gbp == pytest.approx(2.5 + 0.2 * 0.069)
        assert state.last_counter_kwh == 0.2

    def test_small_negative_jitter_is_ignored_not_a_reset(self):
        state = DailyCost(day=DAY, last_counter_kwh=5.0, cost_gbp=1.0)
        state = accumulate_daily_cost(state, day=DAY, counter_kwh=4.97, rate_gbp_per_kwh=0.30)
        assert state.cost_gbp == 1.0
        assert state.last_counter_kwh == 4.97

    def test_none_counter_leaves_state_unchanged(self):
        before = DailyCost(day=DAY, last_counter_kwh=5.0, cost_gbp=1.0)
        assert (
            accumulate_daily_cost(before, day=DAY, counter_kwh=None, rate_gbp_per_kwh=0.3) is before
        )

    def test_none_rate_holds_state_then_prices_gap_at_resumed_rate(self):
        state = DailyCost(day=DAY, last_counter_kwh=2.0, cost_gbp=0.5)
        held = accumulate_daily_cost(state, day=DAY, counter_kwh=3.0, rate_gbp_per_kwh=None)
        assert held is state  # outage: nothing accumulated, baseline kept
        resumed = accumulate_daily_cost(held, day=DAY, counter_kwh=4.0, rate_gbp_per_kwh=0.10)
        assert resumed.cost_gbp == pytest.approx(0.5 + 2.0 * 0.10)

    def test_first_tick_with_unavailable_inputs_stays_none(self):
        assert accumulate_daily_cost(None, day=DAY, counter_kwh=None, rate_gbp_per_kwh=0.3) is None
        assert accumulate_daily_cost(None, day=DAY, counter_kwh=1.0, rate_gbp_per_kwh=None) is None


class TestSavingsToday:
    def test_basic_arithmetic(self):
        assert savings_today_gbp(
            counterfactual_gbp=4.0, import_cost_gbp=1.0, export_earnings_gbp=0.5
        ) == pytest.approx(3.5)

    def test_missing_counterfactual_is_none(self):
        assert (
            savings_today_gbp(counterfactual_gbp=None, import_cost_gbp=1.0, export_earnings_gbp=0.5)
            is None
        )

    def test_missing_import_cost_is_none(self):
        assert (
            savings_today_gbp(counterfactual_gbp=4.0, import_cost_gbp=None, export_earnings_gbp=0.5)
            is None
        )

    def test_missing_export_earnings_counts_as_zero(self):
        assert savings_today_gbp(
            counterfactual_gbp=4.0, import_cost_gbp=1.0, export_earnings_gbp=None
        ) == pytest.approx(3.0)


class TestRollCumulative:
    def test_fresh_start(self):
        state = roll_cumulative(None, day=DAY, savings_today_gbp=1.2)
        assert state == CumulativeSavings(day=DAY, started=DAY, base_gbp=0.0, today_gbp=1.2)
        assert state.total_gbp == pytest.approx(1.2)

    def test_same_day_update_replaces_today(self):
        state = CumulativeSavings(day=DAY, started=DAY, base_gbp=10.0, today_gbp=1.0)
        state = roll_cumulative(state, day=DAY, savings_today_gbp=1.5)
        assert state.base_gbp == 10.0
        assert state.today_gbp == 1.5
        assert state.total_gbp == pytest.approx(11.5)

    def test_day_change_banks_yesterday_into_base(self):
        state = CumulativeSavings(day=DAY, started=DAY, base_gbp=10.0, today_gbp=2.0)
        state = roll_cumulative(state, day=NEXT_DAY, savings_today_gbp=0.1)
        assert state.base_gbp == pytest.approx(12.0)
        assert state.today_gbp == 0.1
        assert state.started == DAY

    def test_negative_day_still_banks(self):
        state = CumulativeSavings(day=DAY, started=DAY, base_gbp=10.0, today_gbp=-0.5)
        state = roll_cumulative(state, day=NEXT_DAY, savings_today_gbp=0.0)
        assert state.base_gbp == pytest.approx(9.5)


class TestPaybackProjection:
    def test_no_capital_cost_means_no_projection(self):
        assert (
            payback_projection(
                capital_cost_gbp=None, recovered_gbp=100.0, started=DAY, today=NEXT_DAY
            )
            is None
        )
        assert (
            payback_projection(
                capital_cost_gbp=0.0, recovered_gbp=100.0, started=DAY, today=NEXT_DAY
            )
            is None
        )

    def test_recovered_pct_and_run_rate(self):
        # 36.525 over ten days tracked -> 3.6525/day -> 1334.07.../yr
        p = payback_projection(
            capital_cost_gbp=10000.0,
            recovered_gbp=36.525,
            started=date(2026, 6, 3),
            today=DAY,
        )
        assert p is not None
        assert p.recovered_pct == pytest.approx(0.36525)
        assert p.run_rate_gbp_per_year == pytest.approx(3.6525 * 365.25)

    def test_projected_breakeven_date(self):
        # 1 GBP/day, 9990 remaining -> 9990 days out.
        p = payback_projection(
            capital_cost_gbp=10000.0,
            recovered_gbp=10.0,
            started=date(2026, 6, 3),
            today=DAY,
        )
        assert p is not None
        remaining_days = (p.projected_breakeven - DAY).days
        assert remaining_days == 9990

    def test_zero_run_rate_has_no_breakeven(self):
        p = payback_projection(
            capital_cost_gbp=10000.0, recovered_gbp=0.0, started=DAY, today=NEXT_DAY
        )
        assert p is not None
        assert p.recovered_pct == 0.0
        assert p.projected_breakeven is None

    def test_already_broken_even(self):
        p = payback_projection(
            capital_cost_gbp=100.0, recovered_gbp=150.0, started=date(2026, 6, 3), today=DAY
        )
        assert p is not None
        assert p.recovered_pct == pytest.approx(150.0)
        assert p.projected_breakeven == DAY

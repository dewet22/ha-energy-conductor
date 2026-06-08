"""Tests for the pure-core hot-water reserve estimate.

Recap:
  reserve = clamp(capacity - depletion*elapsed_days + green_since_full, 0, capacity)
  depletion learned from steady (full→full) days' green diversion, else configured fallback
  boost recommended when reserve projected one day forward (- depletion + expected refill)
  falls below the comfort threshold; suggested hours = ceil(deficit/heater_kw), clamped 1..2
"""

from energy_conductor.hotwater import (
    boost_recommendation,
    estimate_reserve,
    learn_depletion,
)

CAPACITY = 11.0
THRESHOLD = 20.0
HEATER_KW = 2.7


class TestEstimateReserve:
    def test_recent_full_is_capacity(self):
        # Just reached max temp: full regardless of energy-in (clamped).
        assert (
            estimate_reserve(
                elapsed_hours_since_full=0.0,
                energy_in_since_full_kwh=0.0,
                depletion_kwh_per_day=3.0,
                capacity_kwh=CAPACITY,
            )
            == CAPACITY
        )

    def test_energy_in_cannot_exceed_capacity(self):
        assert (
            estimate_reserve(
                elapsed_hours_since_full=0.0,
                energy_in_since_full_kwh=5.0,
                depletion_kwh_per_day=3.0,
                capacity_kwh=CAPACITY,
            )
            == CAPACITY
        )

    def test_depletes_over_time(self):
        # 2 days since full, little diversion: 11 - 3*2 + 1 = 6.
        reserve = estimate_reserve(
            elapsed_hours_since_full=48.0,
            energy_in_since_full_kwh=1.0,
            depletion_kwh_per_day=3.0,
            capacity_kwh=CAPACITY,
        )
        assert reserve == 6.0

    def test_floors_at_zero(self):
        reserve = estimate_reserve(
            elapsed_hours_since_full=240.0,  # 10 days
            energy_in_since_full_kwh=2.0,
            depletion_kwh_per_day=3.0,
            capacity_kwh=CAPACITY,
        )
        assert reserve == 0.0

    def test_diversion_raises_reserve(self):
        common = dict(
            elapsed_hours_since_full=48.0, depletion_kwh_per_day=3.0, capacity_kwh=CAPACITY
        )
        low = estimate_reserve(energy_in_since_full_kwh=0.0, **common)
        high = estimate_reserve(energy_in_since_full_kwh=4.0, **common)
        assert high > low


class TestLearnDepletion:
    def test_learns_median_from_steady_days(self):
        samples = [2.0, 2.5, 3.0, 2.2, 2.8, 3.1]
        value, source = learn_depletion(samples, percentile=0.5, min_samples=5, fallback=3.0)
        assert source == "stats"
        assert min(samples) <= value <= max(samples)

    def test_falls_back_when_too_few_steady_days(self):
        value, source = learn_depletion([2.0, 3.0], percentile=0.5, min_samples=5, fallback=3.0)
        assert (value, source) == (3.0, "default")


class TestBoostRecommendation:
    def _rec(self, *, reserve, depletion=3.0, refill=0.0):
        return boost_recommendation(
            reserve_kwh=reserve,
            capacity_kwh=CAPACITY,
            threshold_percent=THRESHOLD,
            expected_refill_kwh=refill,
            depletion_kwh_per_day=depletion,
            heater_kw=HEATER_KW,
            min_hours=1,
            max_hours=2,
        )

    def test_no_prompt_when_projection_above_threshold(self):
        # reserve 8 - 3 + refill 2 = 7 >= 2.2 → no prompt
        assert self._rec(reserve=8.0, refill=2.0) == (False, None)

    def test_small_deficit_suggests_one_hour(self):
        # reserve 3 - 3 + 0 = 0; deficit 2.2; ceil(2.2/2.7) = 1
        assert self._rec(reserve=3.0) == (True, 1.0)

    def test_larger_deficit_suggests_two_hours(self):
        # reserve 2 - 3 + 0 = -1; deficit 3.2; ceil(3.2/2.7) = 2
        assert self._rec(reserve=2.0) == (True, 2.0)

    def test_hours_clamped_to_max(self):
        # reserve 0 - 4 + 0 = -4; deficit 6.2; ceil = 3 → clamped to 2
        assert self._rec(reserve=0.0, depletion=4.0) == (True, 2.0)

    def test_zero_heater_power_falls_back_to_max_hours(self):
        recommended, hours = boost_recommendation(
            reserve_kwh=1.0,
            capacity_kwh=CAPACITY,
            threshold_percent=THRESHOLD,
            expected_refill_kwh=0.0,
            depletion_kwh_per_day=3.0,
            heater_kw=0.0,
            min_hours=1,
            max_hours=2,
        )
        assert (recommended, hours) == (True, 2.0)

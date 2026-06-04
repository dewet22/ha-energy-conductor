"""Tests for the pure-core daily-target learning helper."""

from __future__ import annotations

import math

import pytest
from custom_components.energy_conductor.learn_daily_target import learned_daily_kwh


class TestLearnedDailyKwh:
    def test_empty_returns_none(self):
        assert learned_daily_kwh([], percentile=0.5, min_samples=7) is None

    def test_below_min_samples_returns_none(self):
        assert learned_daily_kwh([15.0] * 3, percentile=0.5, min_samples=7) is None

    def test_min_samples_boundary(self):
        assert learned_daily_kwh([15.0] * 7, percentile=0.5, min_samples=7) is not None
        assert learned_daily_kwh([15.0] * 6, percentile=0.5, min_samples=7) is None

    def test_p50_of_known_set(self):
        # 14 days, mix of 12 and 20 kWh → median between them.
        samples = [12.0] * 7 + [20.0] * 7
        result = learned_daily_kwh(samples, percentile=0.5, min_samples=7)
        assert result == pytest.approx(16, abs=4.5)

    def test_drops_negative_and_non_finite(self):
        # 7 valid values + 3 junk → still learns from the valid ones.
        samples = [15.0] * 7 + [-2.0, float("nan"), float("inf")]
        result = learned_daily_kwh(samples, percentile=0.5, min_samples=7)
        assert result == pytest.approx(15, abs=0.5)

    def test_insufficient_after_junk_drop_returns_none(self):
        # 5 valid + 5 junk → only 5 usable, below min_samples=7.
        samples = [15.0] * 5 + [-1.0, float("nan"), float("inf"), float("-inf"), float("nan")]
        assert learned_daily_kwh(samples, percentile=0.5, min_samples=7) is None

    def test_realistic_household(self):
        # Mirrors a UK household ~14-22 kWh/day; median lands ~17.
        samples = [13.4, 15.1, 16.8, 17.2, 18.0, 19.5, 22.1, 14.9, 16.3, 17.8, 18.6, 20.2]
        result = learned_daily_kwh(samples, percentile=0.5, min_samples=7)
        assert result == pytest.approx(17.5, abs=2)
        assert math.isfinite(result)

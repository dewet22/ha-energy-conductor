"""Tests for the pure-core baseline learning helpers."""

from __future__ import annotations

import math

import pytest
from custom_components.energy_conductor.baseline import (
    idle_floor_samples,
    learned_baseline_w,
)

IDLE = 50.0


class TestIdleFloorSamples:
    def test_empty_managed_list_keeps_all_finite_home_buckets(self):
        home = {1: 700.0, 2: 720.0, 3: 680.0}
        assert idle_floor_samples(home, [], idle_threshold_w=IDLE) == [700.0, 720.0, 680.0]

    def test_drops_bucket_where_managed_is_active(self):
        home = {1: 700.0, 2: 7000.0, 3: 680.0}  # bucket 2 = EV charging
        ev = {1: 0.0, 2: 6200.0, 3: 0.0}
        assert idle_floor_samples(home, [ev], idle_threshold_w=IDLE) == [700.0, 680.0]

    def test_drops_bucket_where_managed_key_missing(self):
        # Managed sensor offline for bucket 2 while home-load kept reporting:
        # we cannot confirm idle, so the bucket must be excluded (not leaked).
        home = {1: 700.0, 2: 7000.0, 3: 680.0}
        ev = {1: 0.0, 3: 0.0}  # bucket 2 absent
        assert idle_floor_samples(home, [ev], idle_threshold_w=IDLE) == [700.0, 680.0]

    def test_all_managed_must_be_idle_simultaneously(self):
        home = {1: 700.0, 2: 710.0, 3: 720.0}
        ev = {1: 0.0, 2: 0.0, 3: 0.0}
        hwc = {1: 0.0, 2: 800.0, 3: 0.0}  # diverter active in bucket 2
        assert idle_floor_samples(home, [ev, hwc], idle_threshold_w=IDLE) == [700.0, 720.0]

    def test_threshold_boundary_counts_as_idle(self):
        home = {1: 700.0, 2: 710.0}
        ev = {1: 50.0, 2: 50.1}  # exactly at threshold idle; just over is active
        assert idle_floor_samples(home, [ev], idle_threshold_w=IDLE) == [700.0]

    def test_drops_non_finite_home_values(self):
        home = {1: 700.0, 2: float("nan"), 3: float("inf"), 4: 680.0}
        assert idle_floor_samples(home, [], idle_threshold_w=IDLE) == [700.0, 680.0]


class TestLearnedBaselineW:
    def test_empty_returns_none(self):
        assert learned_baseline_w([], percentile=0.5, min_samples=48) is None

    def test_below_min_samples_returns_none(self):
        assert learned_baseline_w([700.0] * 10, percentile=0.5, min_samples=48) is None

    def test_min_samples_boundary(self):
        # Exactly min_samples usable values → a number; one fewer → None.
        assert learned_baseline_w([700.0] * 48, percentile=0.5, min_samples=48) is not None
        assert learned_baseline_w([700.0] * 47, percentile=0.5, min_samples=48) is None

    def test_p50_of_known_set(self):
        # 48 values: 24 at 700, 24 at 800 → p50 ≈ 700 (lower-mid of the gap).
        samples = [700.0] * 24 + [800.0] * 24
        result = learned_baseline_w(samples, percentile=0.5, min_samples=48)
        assert result == pytest.approx(750, abs=55)

    def test_percentile_param_respected(self):
        samples = list(range(1, 101)) * 2  # 200 values, 1..100 twice
        median = learned_baseline_w(samples, percentile=0.5, min_samples=10)
        p25 = learned_baseline_w(samples, percentile=0.25, min_samples=10)
        assert p25 < median

    def test_drops_negative_and_non_finite(self):
        samples = [700.0] * 48 + [-5.0, float("nan"), float("inf")]
        # The 3 junk values are dropped; the 48 valid ones remain → ~700.
        result = learned_baseline_w(samples, percentile=0.5, min_samples=48)
        assert result == pytest.approx(700, abs=1)

    def test_realistic_idle_floor(self):
        # Mirrors measured idle-floor range ~648..867 W; p50 should land ~740.
        samples = [648, 666, 680, 695, 710, 725, 743, 760, 780, 800, 830, 867] * 5
        result = learned_baseline_w(samples, percentile=0.5, min_samples=48)
        assert result == pytest.approx(740, abs=40)
        assert math.isfinite(result)

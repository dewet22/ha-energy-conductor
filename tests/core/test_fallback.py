import pytest

from energy_conductor.fallback import seasonal_fallback_kwh

from .conftest import utc


class TestSeasonalFallback:
    def test_summer_solstice_returns_summer_max(self):
        # June 21 in northern hemisphere → peak
        result = seasonal_fallback_kwh(utc(2026, 6, 21), winter_min=0, summer_max=10)
        assert result == pytest.approx(10.0, abs=0.05)

    def test_winter_solstice_returns_winter_min(self):
        # Dec 21 in northern hemisphere → trough
        result = seasonal_fallback_kwh(utc(2026, 12, 21), winter_min=1.0, summer_max=10.0)
        assert result == pytest.approx(1.0, abs=0.05)

    def test_equinox_returns_midpoint(self):
        # March 21 → roughly midway
        result = seasonal_fallback_kwh(utc(2026, 3, 21), winter_min=0, summer_max=10)
        assert 4.0 <= result <= 6.0  # cosine isn't exactly 0.5 at equinox; band is fine

    def test_southern_hemisphere_inverts(self):
        northern = seasonal_fallback_kwh(utc(2026, 6, 21), winter_min=1, summer_max=10)
        southern = seasonal_fallback_kwh(
            utc(2026, 6, 21), winter_min=1, summer_max=10, southern_hemisphere=True
        )
        # June 21 is winter in southern hemisphere
        assert northern > southern
        assert southern == pytest.approx(1.0, abs=0.05)

    def test_output_bounded_by_min_and_max(self):
        for month in range(1, 13):
            result = seasonal_fallback_kwh(utc(2026, month, 15), winter_min=2.0, summer_max=8.0)
            assert 2.0 - 0.01 <= result <= 8.0 + 0.01

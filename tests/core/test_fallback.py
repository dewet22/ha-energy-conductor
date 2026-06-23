import pytest

from energy_conductor.fallback import (
    NORTHERN_PEAK_DAY,
    SOUTHERN_PEAK_DAY,
    forecast_implausible,
    seasonal_fallback_kwh,
    seasonal_weight,
)

from .conftest import utc


class TestSeasonalWeight:
    def test_northern_peak_day_returns_summer_max(self):
        assert seasonal_weight(NORTHERN_PEAK_DAY, 0.0, 10.0) == pytest.approx(10.0, abs=0.01)

    def test_northern_trough_returns_near_winter_min(self):
        # Half a year past the peak sits at the cosine trough.
        trough = (NORTHERN_PEAK_DAY + 183) % 365
        assert seasonal_weight(trough, 1.0, 10.0) == pytest.approx(1.0, abs=0.05)

    def test_quarter_year_from_peak_is_midpoint(self):
        # A quarter-period from the peak the cosine crosses zero → midpoint.
        quarter = NORTHERN_PEAK_DAY + 91
        assert seasonal_weight(quarter, 0.0, 10.0) == pytest.approx(5.0, abs=0.2)

    def test_southern_hemisphere_peaks_half_a_year_later(self):
        north = seasonal_weight(NORTHERN_PEAK_DAY, 1.0, 10.0)
        south = seasonal_weight(NORTHERN_PEAK_DAY, 1.0, 10.0, southern_hemisphere=True)
        assert north == pytest.approx(10.0, abs=0.01)
        assert south == pytest.approx(1.0, abs=0.05)
        assert seasonal_weight(SOUTHERN_PEAK_DAY, 1.0, 10.0, southern_hemisphere=True) == (
            pytest.approx(10.0, abs=0.01)
        )

    def test_flat_config_has_no_seasonality(self):
        for doy in range(1, 366, 30):
            assert seasonal_weight(doy, 5.0, 5.0) == pytest.approx(5.0)


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


class TestForecastImplausible:
    def test_within_ceiling_is_plausible(self):
        # 20 kWh vs summer_max 22, margin 1.5 → ceiling 33; well within
        assert forecast_implausible(20.0, 22.0, margin=1.5) is False

    def test_at_typical_summer_max_is_plausible(self):
        assert forecast_implausible(22.0, 22.0, margin=1.5) is False

    def test_exceeds_ceiling_is_implausible(self):
        # The 2x-bug case: 44.78 vs summer_max 22 → ceiling 33; flagged
        assert forecast_implausible(44.78, 22.0, margin=1.5) is True

    def test_boundary_exactly_at_ceiling_is_plausible(self):
        # 33 == 22 * 1.5; strictly-greater means the boundary is NOT implausible
        assert forecast_implausible(33.0, 22.0, margin=1.5) is False

    def test_zero_summer_max_disables_check(self):
        # No configured ceiling → never flag (avoids false positives when unset)
        assert forecast_implausible(1000.0, 0.0, margin=1.5) is False

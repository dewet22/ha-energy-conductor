"""Adapter hot-water reserve tests (recorder status history + green stats wiring).

Exercises Adapter._hot_water_state directly with mocked recorder calls, mirroring
tests/integration/test_adapter_daily_target.py. The pure-core maths is covered in
tests/core/test_hotwater.py; here we check the recorder-derived inputs are assembled right.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from custom_components.energy_conductor.adapter import Adapter
from custom_components.energy_conductor.const import (
    CONF_HOTWATER_CAPACITY_KWH,
    CONF_HOTWATER_DEPLETION_KWH,
    CONF_HOTWATER_ENERGY_SENSOR,
    CONF_HOTWATER_GREEN_SENSOR,
    CONF_HOTWATER_HEATER_KW,
    CONF_HOTWATER_STATUS_SENSOR,
    CONF_HOTWATER_THRESHOLD_PERCENT,
)
from custom_components.energy_conductor.model import SolarForecast

GREEN = "sensor.eddi_green"
STATUS = "sensor.eddi_status"
MAX_TEMP = "Max temp reached"

_PATCH_STATS = "custom_components.energy_conductor.adapter.statistics_during_period"
_PATCH_HISTORY = "custom_components.energy_conductor.adapter.state_changes_during_period"


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 6, 8, 12, 0, tzinfo=UTC)


def _forecast(kwh: float = 10.0) -> SolarForecast:
    return SolarForecast(slots=(), fallback_kwh=kwh, fallback_source="test")


def _full_states(days_with_full: list[int], *, hour: int = 3) -> list[SimpleNamespace]:
    """Status states reaching 'Max temp reached' at HH:00 on each given June day."""
    return [
        SimpleNamespace(state=MAX_TEMP, last_changed=datetime(2026, 6, day, hour, 0, tzinfo=UTC))
        for day in days_with_full
    ]


def _daily_green(days: list[int], kwh: float) -> list[dict]:
    return [{"start": datetime(2026, 6, d, 0, 0, tzinfo=UTC), "change": kwh} for d in days]


def _adapter(hass, extra: dict | None = None) -> Adapter:
    config = {
        CONF_HOTWATER_GREEN_SENSOR: GREEN,
        CONF_HOTWATER_STATUS_SENSOR: STATUS,
        CONF_HOTWATER_CAPACITY_KWH: 11.0,
        CONF_HOTWATER_HEATER_KW: 2.7,
        CONF_HOTWATER_THRESHOLD_PERCENT: 20,
        CONF_HOTWATER_DEPLETION_KWH: 3.0,
    }
    config.update(extra or {})
    return Adapter(hass, config)


def _stats_side_effect(daily_rows: list[dict], hourly_total: float):
    def _impl(hass, start, end, ids, period, units, types):
        if period == "day":
            return {GREEN: daily_rows}
        # hourly green-since-full query
        return {GREEN: [{"start": start, "change": hourly_total}]}

    return _impl


async def test_unconfigured_returns_none(hass, now):
    adapter = Adapter(hass, {})  # no hot-water sensors
    with patch(_PATCH_STATS) as stats, patch(_PATCH_HISTORY) as hist:
        assert adapter._hot_water_state(now, _forecast()) is None
    stats.assert_not_called()
    hist.assert_not_called()


async def test_recent_full_reserve_high_no_boost(hass, now):
    # Tank reached full every day incl. this morning → reserve ≈ capacity, no prompt.
    full_days = [1, 2, 3, 4, 5, 6, 7, 8]
    daily_rows = _daily_green([1, 2, 3, 4, 5, 6, 7], 2.5)
    adapter = _adapter(hass)
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: _full_states(full_days)}),
        patch(_PATCH_STATS, side_effect=_stats_side_effect(daily_rows, hourly_total=0.5)),
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    assert hw.reserve_percent > 90
    assert hw.boost_recommended is False
    assert hw.depletion_source == "stats"  # 6 steady (full→full) days ≥ min samples
    assert hw.last_full_at == datetime(2026, 6, 8, 3, 0, tzinfo=UTC)


async def test_stale_full_low_reserve_recommends_boost(hass, now):
    # Last full was 6 days ago, little diversion since → reserve depletes → prompt.
    full_days = [1, 2]
    daily_rows = _daily_green([1, 2], 2.5)  # too few steady days → depletion falls back
    adapter = _adapter(hass)
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: _full_states(full_days)}),
        patch(_PATCH_STATS, side_effect=_stats_side_effect(daily_rows, hourly_total=0.0)),
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    assert hw.reserve_percent < 20
    assert hw.boost_recommended is True
    assert hw.suggested_boost_hours in (1.0, 2.0)
    assert hw.depletion_source == "default"


async def test_no_full_in_lookback_assumes_depleted(hass, now):
    adapter = _adapter(hass)
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: []}),
        patch(_PATCH_STATS, side_effect=_stats_side_effect([], hourly_total=0.0)),
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    assert hw.last_full_at is None
    assert hw.reserve_kwh == 0.0
    assert hw.boost_recommended is True


async def test_recorder_failure_degrades_to_none(hass, now):
    adapter = _adapter(hass)
    with patch(_PATCH_HISTORY, side_effect=RuntimeError("recorder down")):
        assert adapter._hot_water_state(now, _forecast()) is None


async def test_non_finite_recorder_rows_skipped(hass, now):
    """Security audit H-3: inf/nan recorder `change` rows must not poison sums/learning."""
    adapter = _adapter(hass)

    hourly_rows = [
        {"start": now, "change": 1.0},
        {"start": now, "change": float("inf")},  # skipped
        {"start": now, "change": float("nan")},  # skipped
        {"start": now, "change": 0.5},
    ]
    with patch(_PATCH_STATS, return_value={GREEN: hourly_rows}):
        assert adapter._hot_water_green_since(now, now, GREEN) == pytest.approx(1.5)

    daily_rows = [
        {"start": datetime(2026, 6, 1, 0, 0, tzinfo=UTC), "change": 2.5},
        {"start": datetime(2026, 6, 2, 0, 0, tzinfo=UTC), "change": float("nan")},  # skipped
    ]
    with patch(_PATCH_STATS, return_value={GREEN: daily_rows}):
        daily = adapter._hot_water_daily_kwh(now, GREEN)
    assert list(daily.values()) == [2.5]


TOTAL = "sensor.eddi_total"


def _stats_side_effect_with_total(
    green_daily: list[dict], total_daily: list[dict], hourly_total: float
):
    """Stats mock that distinguishes green vs total entity for day-period queries."""

    def _impl(hass, start, end, ids, period, units, types):
        if period == "day":
            if TOTAL in ids:
                return {TOTAL: total_daily}
            return {GREEN: green_daily}
        # hourly green-since-full query
        return {GREEN: [{"start": start, "change": hourly_total}]}

    return _impl


async def test_total_sensor_used_for_depletion_learning(hass, now):
    # Green-only steady days show ~1 kWh/day (post-boost marginal top-up).
    # Total-in steady days show ~2.5 kWh/day (actual heat loss + draw).
    # With the total sensor configured, depletion should be learned from total.
    full_days = [1, 2, 3, 4, 5, 6, 7, 8]
    green_daily = _daily_green([1, 2, 3, 4, 5, 6, 7], 1.0)  # understated
    total_daily = [
        {"start": datetime(2026, 6, d, 0, 0, tzinfo=UTC), "change": 2.5}
        for d in [1, 2, 3, 4, 5, 6, 7]
    ]
    adapter = _adapter(hass, extra={CONF_HOTWATER_ENERGY_SENSOR: TOTAL})
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: _full_states(full_days)}),
        patch(
            _PATCH_STATS,
            side_effect=_stats_side_effect_with_total(green_daily, total_daily, 0.5),
        ),
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    assert hw.depletion_source == "stats"
    # Should be learned from total (~2.5), not from green (~1.0)
    assert hw.depletion_kwh_per_day == pytest.approx(2.5, abs=0.1)

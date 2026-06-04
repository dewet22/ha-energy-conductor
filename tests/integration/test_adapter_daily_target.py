"""Adapter daily-target-learning tests (recorder query + fallback wiring).

Exercises Adapter._compute_daily_target / _stats_based_daily_target directly with
a mocked recorder, mirroring tests/integration/test_adapter_baseline.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from custom_components.energy_conductor.adapter import Adapter
from custom_components.energy_conductor.const import (
    CONF_DAILY_ENERGY_SENSOR,
    CONF_DAILY_KWH_TARGET,
)

ENERGY = "sensor.house_energy"
STATIC_DEFAULT = 10.0

_PATCH_TARGET = "custom_components.energy_conductor.adapter.statistics_during_period"


def _daily_rows(daily_kwh: list[float], *, start: datetime) -> list[dict]:
    """Build statistics rows representing a total_increasing sensor's cumulative `sum`.

    Each row is one calendar day's sum-since-recording-started; diffing
    consecutive rows recovers the daily kWh values in `daily_kwh`.
    The first row carries the baseline (sum=0) so the next N rows produce N diffs.
    """
    rows: list[dict] = [{"start": start, "sum": 0.0}]
    running = 0.0
    for i, kwh in enumerate(daily_kwh):
        running += kwh
        rows.append({"start": start + timedelta(days=i + 1), "sum": running})
    return rows


@pytest.fixture
def now() -> datetime:
    # Late afternoon UTC; "today" in local tz is the same date in most reasonable setups.
    return datetime(2026, 5, 31, 16, 0, tzinfo=UTC)


def _adapter(hass, config: dict) -> Adapter:
    base = {CONF_DAILY_KWH_TARGET: STATIC_DEFAULT}
    return Adapter(hass, {**base, **config})


async def test_learns_daily_target_from_history(hass, now):
    # 14 historical days of ~15-20 kWh.
    daily_kwh = [13.0, 15.0, 17.0, 19.0, 21.0, 14.0, 16.0, 18.0, 20.0, 15.5, 16.5, 17.5, 18.5, 19.5]
    base = now - timedelta(days=15)  # rows span back beyond LOOKBACK so all 14 diffs fall in range
    rows = _daily_rows(daily_kwh, start=base)

    adapter = _adapter(hass, {CONF_DAILY_ENERGY_SENSOR: ENERGY})
    with patch(_PATCH_TARGET, return_value={ENERGY: rows}) as mock_stats:
        value, source, n_days = adapter._compute_daily_target(now)

    mock_stats.assert_called_once()
    assert source == "stats"
    assert n_days == len(daily_kwh)
    # p50 of the 14-day distribution lies in the cluster around 17.
    assert value == pytest.approx(17.5, abs=2)


async def test_no_sensor_uses_default_without_querying(hass, now):
    adapter = _adapter(hass, {})  # no CONF_DAILY_ENERGY_SENSOR
    with patch(_PATCH_TARGET) as mock_stats:
        value, source, n_days = adapter._compute_daily_target(now)

    assert (value, source, n_days) == (STATIC_DEFAULT, "default", None)
    mock_stats.assert_not_called()


async def test_recorder_exception_falls_back_to_default(hass, now):
    adapter = _adapter(hass, {CONF_DAILY_ENERGY_SENSOR: ENERGY})
    with patch(_PATCH_TARGET, side_effect=RuntimeError("recorder down")):
        value, source, n_days = adapter._compute_daily_target(now)

    assert (value, source, n_days) == (STATIC_DEFAULT, "default", None)


async def test_insufficient_samples_falls_back_to_default(hass, now):
    # Only 4 historical days → below DAILY_TARGET_MIN_SAMPLES (7).
    daily_kwh = [15.0, 16.0, 17.0, 18.0]
    base = now - timedelta(days=5)
    rows = _daily_rows(daily_kwh, start=base)

    adapter = _adapter(hass, {CONF_DAILY_ENERGY_SENSOR: ENERGY})
    with patch(_PATCH_TARGET, return_value={ENERGY: rows}):
        value, source, n_days = adapter._compute_daily_target(now)

    assert (value, source, n_days) == (STATIC_DEFAULT, "default", None)


async def test_partial_today_excluded(hass, now):
    # Last row is dated TODAY (in local tz) — it should be dropped so a
    # half-day reading doesn't drag the median.
    daily_kwh = [15.0] * 13 + [3.0]  # last bucket is today's tiny partial
    base = now - timedelta(days=14)
    rows = _daily_rows(daily_kwh, start=base)

    adapter = _adapter(hass, {CONF_DAILY_ENERGY_SENSOR: ENERGY})
    with patch(_PATCH_TARGET, return_value={ENERGY: rows}):
        value, source, n_days = adapter._compute_daily_target(now)

    assert source == "stats"
    # Today's bucket dropped → 13 qualifying days, all 15 kWh.
    assert n_days == 13
    assert value == pytest.approx(15, abs=0.5)


async def test_meter_reset_skipped(hass, now):
    # A negative diff (meter reset / counter rollover) must be skipped.
    base = now - timedelta(days=15)
    rows: list[dict] = [{"start": base, "sum": 100.0}]
    rows.append({"start": base + timedelta(days=1), "sum": 50.0})  # reset → negative diff
    for i in range(2, 15):
        rows.append({"start": base + timedelta(days=i), "sum": 50.0 + (i - 1) * 16.0})

    adapter = _adapter(hass, {CONF_DAILY_ENERGY_SENSOR: ENERGY})
    with patch(_PATCH_TARGET, return_value={ENERGY: rows}):
        value, source, n_days = adapter._compute_daily_target(now)

    assert source == "stats"
    # 14 diffs total; 1 negative dropped → 13.
    assert n_days == 13
    assert value == pytest.approx(16, abs=1)

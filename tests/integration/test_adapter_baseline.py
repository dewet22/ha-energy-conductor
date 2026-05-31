"""Adapter baseline-learning tests (recorder query + fallback wiring).

Exercises Adapter._compute_baseline / _stats_based_baseline directly with a
mocked recorder, so we don't need to arrange the full SiteState build.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from custom_components.energy_conductor.adapter import Adapter
from custom_components.energy_conductor.const import (
    CONF_HOME_LOAD_SENSOR,
    CONF_MANAGED_LOAD_SENSORS,
    DEFAULT_BASELINE_LOAD_W,
)

HOME = "sensor.house_load_power"
EV = "sensor.ev_power"

_PATCH_TARGET = "custom_components.energy_conductor.adapter.statistics_during_period"


def _hourly_rows(values: list[float | None], *, start: datetime) -> list[dict]:
    """Build statistics rows with datetime `start` keys, one per hour."""
    rows = []
    for i, v in enumerate(values):
        row: dict = {"start": start + timedelta(hours=i)}
        if v is not None:
            row["mean"] = v
        rows.append(row)
    return rows


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 31, 12, 0, tzinfo=UTC)


def _adapter(hass, config: dict) -> Adapter:
    return Adapter(hass, config)


async def test_learns_baseline_from_idle_floor(hass, now):
    # 60 buckets: floor ~700-760 when EV idle; EV-active buckets spike home-load.
    base = now - timedelta(days=14)
    home_vals = ([700.0, 740.0, 760.0, 720.0] * 14) + [7000.0] * 4  # last 4 = EV charging
    ev_vals = ([0.0] * 56) + [6000.0] * 4
    home_rows = _hourly_rows(home_vals, start=base)
    ev_rows = _hourly_rows(ev_vals, start=base)

    config = {CONF_HOME_LOAD_SENSOR: HOME, CONF_MANAGED_LOAD_SENSORS: [EV]}
    adapter = _adapter(hass, config)

    with patch(_PATCH_TARGET, return_value={HOME: home_rows, EV: ev_rows}) as mock_stats:
        value, source = adapter._compute_baseline(now)

    mock_stats.assert_called_once()
    assert source == "stats"
    # EV-active buckets excluded → p50 of the ~700-760 floor.
    assert value == pytest.approx(730, abs=40)


async def test_no_home_sensor_uses_default_without_querying(hass, now):
    adapter = _adapter(hass, {})  # no CONF_HOME_LOAD_SENSOR
    with patch(_PATCH_TARGET) as mock_stats:
        value, source = adapter._compute_baseline(now)

    assert (value, source) == (DEFAULT_BASELINE_LOAD_W, "default")
    mock_stats.assert_not_called()


async def test_recorder_exception_falls_back_to_default(hass, now):
    adapter = _adapter(hass, {CONF_HOME_LOAD_SENSOR: HOME})
    with patch(_PATCH_TARGET, side_effect=RuntimeError("recorder down")):
        value, source = adapter._compute_baseline(now)

    assert (value, source) == (DEFAULT_BASELINE_LOAD_W, "default")


async def test_insufficient_samples_falls_back_to_default(hass, now):
    base = now - timedelta(days=1)
    home_rows = _hourly_rows([700.0] * 10, start=base)  # only 10 < min_samples (48)
    adapter = _adapter(hass, {CONF_HOME_LOAD_SENSOR: HOME, CONF_MANAGED_LOAD_SENSORS: []})
    with patch(_PATCH_TARGET, return_value={HOME: home_rows}):
        value, source = adapter._compute_baseline(now)

    assert (value, source) == (DEFAULT_BASELINE_LOAD_W, "default")


async def test_ev_active_but_series_missing_bucket_excluded(hass, now):
    # Home-load shows a 7kW bucket; EV series lacks that bucket key (sensor offline).
    # That bucket must be excluded, not leaked into the floor.
    base = now - timedelta(days=14)
    home_vals = ([700.0] * 50) + [7000.0]  # 51 buckets, last is the suspicious spike
    home_rows = _hourly_rows(home_vals, start=base)
    # EV rows cover only the first 50 buckets (missing the spike bucket).
    ev_rows = _hourly_rows([0.0] * 50, start=base)

    adapter = _adapter(hass, {CONF_HOME_LOAD_SENSOR: HOME, CONF_MANAGED_LOAD_SENSORS: [EV]})
    with patch(_PATCH_TARGET, return_value={HOME: home_rows, EV: ev_rows}):
        value, source = adapter._compute_baseline(now)

    assert source == "stats"
    assert value == pytest.approx(700, abs=1)  # spike excluded


async def test_empty_managed_list_uses_whole_series(hass, now):
    base = now - timedelta(days=14)
    home_rows = _hourly_rows([700.0, 760.0] * 30, start=base)  # 60 buckets, no managed
    adapter = _adapter(hass, {CONF_HOME_LOAD_SENSOR: HOME, CONF_MANAGED_LOAD_SENSORS: []})
    with patch(_PATCH_TARGET, return_value={HOME: home_rows}):
        value, source = adapter._compute_baseline(now)

    assert source == "stats"
    assert value == pytest.approx(730, abs=40)

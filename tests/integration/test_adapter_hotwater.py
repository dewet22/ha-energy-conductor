"""Adapter hot-water reserve tests (recorder status history + energy stats wiring).

Exercises Adapter._hot_water_state directly with mocked recorder calls, mirroring
tests/integration/test_adapter_daily_target.py. The pure-core maths is covered in
tests/core/test_hotwater.py; here we check the recorder-derived inputs are assembled right.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from custom_components.energy_conductor.adapter import Adapter
from custom_components.energy_conductor.const import (
    CONF_HOTWATER_CAPACITY_KWH,
    CONF_HOTWATER_DEPLETION_KWH,
    CONF_HOTWATER_ENERGY_SENSOR,
    CONF_HOTWATER_HEATER_KW,
    CONF_HOTWATER_STATUS_SENSOR,
    CONF_HOTWATER_THRESHOLD_PERCENT,
)
from custom_components.energy_conductor.model import SolarForecast

ENERGY = "sensor.eddi_energy"
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
    """Status states reaching 'Max temp reached' at HH:00 on each given June day, each
    preceded by an active 'Diverting' state so the transition gate honours them as genuine
    fulls (a real full trips to max temp while power is flowing)."""
    states: list[SimpleNamespace] = []
    for day in days_with_full:
        at = datetime(2026, 6, day, hour, 0, tzinfo=UTC)
        states.append(SimpleNamespace(state="Diverting", last_changed=at - timedelta(minutes=5)))
        states.append(SimpleNamespace(state=MAX_TEMP, last_changed=at))
    return states


def _daily_energy(days: list[int], kwh: float) -> list[dict]:
    return [{"start": datetime(2026, 6, d, 0, 0, tzinfo=UTC), "change": kwh} for d in days]


def _full_hours(days: list[int], *, hour: int = 3) -> list[datetime]:
    """Datetimes of the 'Max temp reached' events — where adjacent diversion must sit."""
    return [datetime(2026, 6, day, hour, 0, tzinfo=UTC) for day in days]


def _hourly_corroboration_rows(full_hours: list[datetime], kwh: float = 0.3) -> list[dict]:
    """Hourly energy rows placing delivered energy in each full event's own hour, so the
    corroboration honours those fulls as genuine (a real diversion session ran into them)."""
    return [{"start": h, "change": kwh} for h in full_hours]


def _adapter(hass, extra: dict | None = None) -> Adapter:
    config = {
        CONF_HOTWATER_ENERGY_SENSOR: ENERGY,
        CONF_HOTWATER_STATUS_SENSOR: STATUS,
        CONF_HOTWATER_CAPACITY_KWH: 11.0,
        CONF_HOTWATER_HEATER_KW: 2.7,
        CONF_HOTWATER_THRESHOLD_PERCENT: 20,
        CONF_HOTWATER_DEPLETION_KWH: 3.0,
    }
    config.update(extra or {})
    return Adapter(hass, config)


def _stats_side_effect(daily_rows: list[dict], hourly_total: float, full_hours=()):
    def _impl(hass, start, end, ids, period, units, types):
        if period == "day":
            return {ENERGY: daily_rows}
        # Two hourly queries share this branch; the corroboration one spans the full
        # lookback, the green-since-full one only [last_full, now].
        if end - start >= timedelta(days=9):
            return {ENERGY: _hourly_corroboration_rows(list(full_hours))}
        return {ENERGY: [{"start": start, "change": hourly_total}]}

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
    daily_rows = _daily_energy([1, 2, 3, 4, 5, 6, 7], 2.5)
    adapter = _adapter(hass)
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: _full_states(full_days)}),
        patch(
            _PATCH_STATS,
            side_effect=_stats_side_effect(
                daily_rows, hourly_total=0.5, full_hours=_full_hours(full_days)
            ),
        ),
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    assert hw.reserve_percent > 90
    assert hw.boost_recommended is False
    assert hw.depletion_source == "stats"  # 6 steady (full→full) days ≥ min samples
    assert hw.last_full_at == datetime(2026, 6, 8, 3, 0, tzinfo=UTC)
    assert hw.reserve_source == "anchored"


async def test_stale_full_low_reserve_recommends_boost(hass, now):
    # Last full was 6 days ago, little diversion since → reserve depletes → prompt.
    full_days = [1, 2]
    daily_rows = _daily_energy([1, 2], 2.5)  # too few steady days → depletion falls back
    adapter = _adapter(hass)
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: _full_states(full_days)}),
        patch(
            _PATCH_STATS,
            side_effect=_stats_side_effect(
                daily_rows, hourly_total=0.0, full_hours=_full_hours(full_days)
            ),
        ),
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    assert hw.last_full_at == datetime(2026, 6, 2, 3, 0, tzinfo=UTC)  # genuine, stale
    assert hw.reserve_percent < 20
    assert hw.boost_recommended is True
    assert hw.suggested_boost_hours in (1.0, 2.0)
    assert hw.depletion_source == "default"
    assert hw.reserve_source == "anchored"


async def test_uncorroborated_full_ignored_anchors_last_genuine(hass, now):
    """A 'Max temp reached' with no diversion behind it (element isolated at the safety
    switch, or republished on a reconnect/restart) must not anchor the reserve; the last
    genuine full stands. Regression for the live bug where a phantom full pinned it to ~100%.
    """
    # Genuine full on day 6 (diversion in its hour); phantom full this morning (none).
    full_days = [6, 8]
    daily_rows = _daily_energy([6, 7], 2.5)
    adapter = _adapter(hass)
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: _full_states(full_days)}),
        patch(
            _PATCH_STATS,
            side_effect=_stats_side_effect(
                daily_rows,
                hourly_total=0.0,
                full_hours=_full_hours([6]),  # only day 6 corroborated
            ),
        ),
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    # Day 8's full is uncorroborated → ignored; the anchor is the genuine day-6 full,
    # and the reserve has depleted since then rather than snapping back to capacity.
    assert hw.last_full_at == datetime(2026, 6, 6, 3, 0, tzinfo=UTC)
    assert hw.reserve_percent < 90


async def test_full_from_stopped_not_anchored(hass, now):
    """Regression for 2026-06-23: a 'Max temp reached' that transitions from an idle 'Stopped'
    state (cold tank, 30 s spurious blip) must not anchor the reserve — even though diversion
    is flowing — while the genuine earlier Diverting→Max full stands. The transition gate drops
    it; the corroboration gate would NOT (diversion is present), so this is the sharper sibling
    of test_uncorroborated_full_ignored_anchors_last_genuine.
    """
    day6 = datetime(2026, 6, 6, 3, 0, tzinfo=UTC)
    day8 = datetime(2026, 6, 8, 3, 0, tzinfo=UTC)
    status = [
        SimpleNamespace(state="Diverting", last_changed=day6 - timedelta(minutes=5)),
        SimpleNamespace(state=MAX_TEMP, last_changed=day6),
        SimpleNamespace(state="Stopped", last_changed=day8 - timedelta(minutes=5)),
        SimpleNamespace(state=MAX_TEMP, last_changed=day8),
    ]
    daily_rows = _daily_energy([6, 7], 2.5)
    adapter = _adapter(hass)
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: status}),
        patch(
            _PATCH_STATS,
            side_effect=_stats_side_effect(
                daily_rows,
                hourly_total=0.0,
                full_hours=_full_hours([6, 8]),  # BOTH corroborated
            ),
        ),
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    # Day-8 Stopped→Max dropped by the transition gate (despite its diversion); anchor = day 6.
    assert hw.last_full_at == day6
    assert hw.reserve_source == "anchored"
    assert hw.reserve_percent < 90  # depleted since day 6, not snapped to 100


async def test_cold_fill_integrates_up_from_zero(hass, now):
    """No full survives the gates, but green has been diverting into a cold tank → the reserve
    integrates up from zero with the absorbed kWh (capped at capacity) instead of a flat 0."""
    adapter = _adapter(hass, extra={CONF_HOTWATER_CAPACITY_KWH: 12.0})
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: []}),  # no full events at all
        patch(_PATCH_STATS, side_effect=_stats_side_effect([], hourly_total=4.0)),
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    assert hw.last_full_at is None
    assert hw.reserve_source == "cold_fill"
    assert hw.reserve_kwh == pytest.approx(4.0)  # green-since-midnight, capped at 12
    assert hw.reserve_percent == pytest.approx(33.3, abs=0.1)


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
    assert hw.reserve_source == "cold_fill"
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
    with patch(_PATCH_STATS, return_value={ENERGY: hourly_rows}):
        assert adapter._hot_water_energy_since(now, now, ENERGY) == pytest.approx(1.5)

    daily_rows = [
        {"start": datetime(2026, 6, 1, 0, 0, tzinfo=UTC), "change": 2.5},
        {"start": datetime(2026, 6, 2, 0, 0, tzinfo=UTC), "change": float("nan")},  # skipped
    ]
    with patch(_PATCH_STATS, return_value={ENERGY: daily_rows}):
        daily = adapter._hot_water_daily_kwh(now, ENERGY)
    assert list(daily.values()) == [2.5]


async def test_hourly_kwh_clamps_negative_and_skips_nonfinite(hass, now):
    """Corroboration buckets: a negative meter-reset delta clamps to 0 (so it can't cancel
    real diversion in the window and falsely drop a genuine full); non-finite rows skip."""
    adapter = _adapter(hass)
    h10 = datetime(2026, 6, 8, 10, 0, tzinfo=UTC)
    h11 = datetime(2026, 6, 8, 11, 0, tzinfo=UTC)
    h12 = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    rows = [
        {"start": h10, "change": 0.3},
        {"start": h11, "change": -0.5},  # meter reset/correction → clamped to 0
        {"start": h12, "change": float("nan")},  # skipped
    ]
    with patch(_PATCH_STATS, return_value={ENERGY: rows}):
        by_hour = adapter._hot_water_hourly_kwh(now, now, ENERGY)
    assert by_hour[h10] == 0.3
    assert by_hour[h11] == 0.0
    assert h12 not in by_hour


async def test_boost_origin_full_anchors_via_delivered_energy(hass, now):
    """A scheduled grid boost that trips 'Max temp reached' anchors the reserve: the
    transition gate sees the Boosting origin and the corroboration gate sees the boost's
    delivered energy on the single energy-in sensor (the green-only starvation defect,
    live-confirmed 2026-08-23, must not return)."""
    at = datetime(2026, 6, 8, 4, 0, tzinfo=UTC)
    states = [
        SimpleNamespace(state="Boosting", last_changed=at - timedelta(minutes=20)),
        SimpleNamespace(state=MAX_TEMP, last_changed=at),
    ]
    adapter = _adapter(hass)
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: states}),
        patch(
            _PATCH_STATS,
            side_effect=_stats_side_effect(_daily_energy([1, 2], 2.5), 0.5, full_hours=[at]),
        ),
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    assert hw.last_full_at == at
    assert hw.reserve_source == "anchored"
    assert hw.reserve_kwh > 0


async def test_corroboration_starvation_surfaced(hass, now, caplog):
    """Transition-gated fulls exist but the energy series is dead across their windows:
    the anchor stays un-set (no false full) and the condition is SURFACED — reserve_source
    'corroboration_starved' plus a warning log — instead of silently reading as cold_fill.
    (The repo's standing lesson: every silent fallback needs observability.)"""
    full_days = [6, 7, 8]
    adapter = _adapter(hass)
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: _full_states(full_days)}),
        patch(_PATCH_STATS, return_value={}),  # energy stats empty everywhere
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    assert hw.last_full_at is None  # starved corroboration must not anchor
    assert hw.reserve_source == "corroboration_starved"
    assert any("corroboration" in r.message.lower() for r in caplog.records)


async def test_corroboration_starvation_warning_is_episodic(hass, now, caplog):
    """The warning fires once per starvation episode, not every tick."""
    full_days = [6, 7, 8]
    adapter = _adapter(hass)
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: _full_states(full_days)}),
        patch(_PATCH_STATS, return_value={}),
    ):
        adapter._hot_water_state(now, _forecast())
        first = sum("corroboration" in r.message.lower() for r in caplog.records)
        adapter._hot_water_state(now, _forecast())
        second = sum("corroboration" in r.message.lower() for r in caplog.records)

    assert first == 1
    assert second == 1  # no new warning on the next tick


async def test_phantom_full_still_rejected_without_starvation_flag(hass, now):
    """Idle-origin 'Max temp reached' (isolated tank) fails the TRANSITION gate — that is
    an ordinary rejection, not corroboration starvation: reserve_source stays cold_fill."""
    at = datetime(2026, 6, 8, 3, 0, tzinfo=UTC)
    states = [
        SimpleNamespace(state="Stopped", last_changed=at - timedelta(minutes=5)),
        SimpleNamespace(state=MAX_TEMP, last_changed=at),
    ]
    adapter = _adapter(hass)
    with (
        patch(_PATCH_HISTORY, return_value={STATUS: states}),
        patch(_PATCH_STATS, return_value={}),
    ):
        hw = adapter._hot_water_state(now, _forecast())

    assert hw is not None
    assert hw.last_full_at is None
    assert hw.reserve_source == "cold_fill"

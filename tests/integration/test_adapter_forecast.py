"""Adapter solar-forecast tests.

Three bugs fixed in this module:
1. Solcast slots were silently dropped because ForecastSlot requires UTC but the
   Solcast HA integration provides local-timezone datetimes (e.g. BST/Europe/London).
   Every slot raised ValueError in ForecastSlot.__post_init__ and was swallowed by
   the except block, leaving the forecast always empty and falling through to seasonal.
2. _slots_from_solcast now filters to tomorrow's local date only, so configuring
   the "forecast today" sensor or an aggregate sensor degrades cleanly to fallback
   rather than producing a 0-morning-gap planning error.
3. daily_total_sensor created a synthetic slot with start=now which made
   _morning_gap_hours() return 0 (the slot predated tomorrow's window end),
   so the overnight plan never provisioned a morning gap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from custom_components.energy_conductor.adapter import Adapter
from custom_components.energy_conductor.const import (
    CONF_FORECAST_DAILY_SENSOR,
    CONF_FORECAST_SOURCE,
    FORECAST_SOURCE_DAILY,
)

SOLCAST = "sensor.solcast_forecast_tomorrow"
DAILY = "sensor.solar_today"

BST = timezone(timedelta(hours=1), "BST")  # UTC+1, as Solcast returns for UK installs


def _adapter(hass, config: dict) -> Adapter:
    return Adapter(hass, config)


def _solcast_slot(dt_local: datetime, kwh: float) -> dict:
    return {"period_start": dt_local, "pv_estimate": kwh}


@pytest.fixture
def now_utc() -> datetime:
    # 22:00 BST on June 1 (21:00 UTC) — typical overnight-plan execution time
    return datetime(2026, 6, 1, 21, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def set_london_tz(hass):
    """Pin the HA timezone to Europe/London so dt_util.as_local is deterministic.

    Without this, dt_util.as_local uses whatever timezone the CI runner is in,
    which can shift 'tomorrow' boundaries unpredictably (e.g. UTC-7 would make
    June 2 00:00 UTC appear as June 1 17:00 local, breaking the filter assertions).
    """
    await hass.config.async_set_time_zone("Europe/London")


async def test_solcast_utc_conversion_bug_fixed(hass, mock_config_entry, now_utc):
    """Bug 1: BST datetimes from Solcast were rejected by ForecastSlot (requires UTC).

    Before the fix every slot raised ValueError and was silently dropped, leaving
    the forecast always empty. After the fix slots are converted to UTC and retained.
    """
    # 8 slots on June 2 in BST (01:00-08:00 BST = 00:00-07:00 UTC)
    tomorrow_bst = datetime(2026, 6, 2, 1, 0, tzinfo=BST)
    slots_bst = [_solcast_slot(tomorrow_bst + timedelta(hours=h), 0.5) for h in range(8)]
    hass.states.async_set(SOLCAST, "14.44", {"detailedForecast": slots_bst})
    await hass.async_block_till_done()

    mock_config_entry.add_to_hass(hass)
    adapter = _adapter(hass, {})

    slots = adapter._slots_from_solcast(SOLCAST, now_utc)

    assert len(slots) == 8, "all tomorrow's slots should be retained after UTC conversion"
    for slot in slots:
        assert slot.start.utcoffset() == timedelta(0), "start must be UTC"
    # Spot-check first slot: BST 01:00 → UTC 00:00
    assert slots[0].start == datetime(2026, 6, 2, 0, 0, tzinfo=UTC)


async def test_solcast_tomorrow_filter(hass, mock_config_entry, now_utc):
    """Bug 2 (defence): today's slots are dropped; only tomorrow's are kept.

    If the user configures the 'forecast today' sensor, filtering ensures we fall
    back to seasonal/stats rather than producing a 0-morning-gap plan.
    """
    today_bst = datetime(2026, 6, 1, 14, 0, tzinfo=BST)  # today — must be dropped
    tomorrow_bst = datetime(2026, 6, 2, 8, 0, tzinfo=BST)  # tomorrow — must be kept

    hass.states.async_set(
        SOLCAST,
        "8.0",
        {
            "detailedForecast": [
                _solcast_slot(today_bst, 2.0),
                _solcast_slot(tomorrow_bst, 1.5),
            ]
        },
    )
    await hass.async_block_till_done()

    mock_config_entry.add_to_hass(hass)
    adapter = _adapter(hass, {})

    slots = adapter._slots_from_solcast(SOLCAST, now_utc)

    assert len(slots) == 1
    assert abs(slots[0].energy_kwh - 1.5) < 0.001


async def test_daily_sensor_uses_fallback_kwh_not_synthetic_slot(hass, mock_config_entry, now_utc):
    """Bug 3: daily_total_sensor now stores value as fallback_kwh (not a synthetic slot).

    The old synthetic slot had start=now, which made _morning_gap_hours() return 0
    (the slot predates tomorrow's window end). Now the value is stored as fallback_kwh
    so the planner uses MISSING_FORECAST_GAP_H (4 h) as the morning gap.
    """
    hass.states.async_set(DAILY, "8.5")
    await hass.async_block_till_done()

    mock_config_entry.add_to_hass(hass)
    config = {
        CONF_FORECAST_SOURCE: FORECAST_SOURCE_DAILY,
        CONF_FORECAST_DAILY_SENSOR: DAILY,
    }
    adapter = _adapter(hass, config)

    solar = await adapter._build_forecast(now_utc)

    # Result must use fallback_kwh path (no slots), NOT a synthetic slot
    assert solar.slots == ()
    assert solar.fallback_kwh == pytest.approx(8.5)
    assert solar.fallback_source == "daily_sensor"
    assert solar.total_kwh_forecast == pytest.approx(8.5)

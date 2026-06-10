"""Adapter input-validation tests (security audit H-3, issue #7).

float("inf")/float("nan") parse without error, so a sensor whose state string is
"nan"/"inf" (upstream bug, recorder anomaly, or compromised integration) used to
pass _read_float and poison plan math — and ultimately hardware writes. Non-finite
values must be rejected like any other bad state.
"""

from __future__ import annotations

import pytest
from custom_components.energy_conductor.adapter import (
    Adapter,
    EntityProblem,
    _max_attr,
    _read_float,
    parse_hh_mm,
)
from custom_components.energy_conductor.const import (
    CONF_BATTERY_POWER_POSITIVE_IS_CHARGING,
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_OFF_PEAK_SENSOR,
    CONF_OVERNIGHT_WINDOW_END_TIME,
    CONF_RESERVE_SOC_SENSOR,
)

SENSOR = "sensor.some_float"


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "Infinity", "-Infinity", "NaN"])
async def test_read_float_rejects_non_finite(hass, bad):
    hass.states.async_set(SENSOR, bad)
    await hass.async_block_till_done()

    with pytest.raises(EntityProblem, match="non-finite"):
        _read_float(hass, SENSOR, max_age_seconds=3600)


async def test_read_float_accepts_finite(hass):
    hass.states.async_set(SENSOR, "42.5")
    await hass.async_block_till_done()

    assert _read_float(hass, SENSOR, max_age_seconds=3600) == pytest.approx(42.5)


async def test_non_finite_reserve_sensor_falls_back_to_config(hass, mock_config_entry):
    # The reserve floor feeds the overnight charge target — a "nan" reserve sensor
    # must degrade to the configured static percent, not flow into plan math.
    hass.states.async_set("number.reserve", "nan")
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)
    adapter = Adapter(
        hass, {CONF_RESERVE_SOC_SENSOR: "number.reserve", CONF_BATTERY_RESERVE_PERCENT: 10}
    )

    assert adapter._reserve_percent() == pytest.approx(10.0)


# --- M-3: bounds clamps -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw_max", "expected"),
    [(50000, 20000), (-100, 1), (0, 1), (3600, 3600)],
)
async def test_max_attr_clamps_to_plausible_range(hass, raw_max, expected):
    hass.states.async_set("number.discharge", "40", {"max": raw_max})
    await hass.async_block_till_done()
    assert _max_attr(hass, "number.discharge", default=3000) == expected


async def test_max_attr_returns_default_when_absent(hass):
    hass.states.async_set("number.discharge", "40")  # no `max` attribute
    await hass.async_block_till_done()
    assert _max_attr(hass, "number.discharge", default=3000) == 3000


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
async def test_max_attr_non_finite_falls_back_to_default(hass, bad):
    # int(float("inf")) raises OverflowError, int(float("nan")) raises ValueError —
    # a float-valued non-finite `max` from a buggy integration must not crash the tick.
    hass.states.async_set("number.discharge", "40", {"max": bad})
    await hass.async_block_till_done()
    assert _max_attr(hass, "number.discharge", default=3000) == 3000


@pytest.mark.parametrize(("raw", "expected"), [("150", 100.0), ("-5", 0.0), ("4", 4.0)])
async def test_reserve_percent_clamped_to_0_100(hass, mock_config_entry, raw, expected):
    hass.states.async_set("number.reserve", raw)
    await hass.async_block_till_done()
    mock_config_entry.add_to_hass(hass)
    adapter = Adapter(
        hass, {CONF_RESERVE_SOC_SENSOR: "number.reserve", CONF_BATTERY_RESERVE_PERCENT: 10}
    )

    assert adapter._reserve_percent() == pytest.approx(expected)


# --- L-1: time-string parsing -----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("21:30", (21, 30)),
        ("21:30:00", (21, 30)),
        ("06:05", (6, 5)),
        ("0:0", (0, 0)),
        ("23:59", (23, 59)),
    ],
)
def test_parse_hh_mm_valid(raw, expected):
    assert parse_hh_mm(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "abc", "21", "21:xx", "25:00", "12:60", "-1:00", None, 21.5],
)
def test_parse_hh_mm_malformed_returns_none(raw):
    # A hand-edited or migration-corrupted value must yield None (caller falls back), never raise.
    assert parse_hh_mm(raw) is None


async def test_off_peak_window_end_ignores_malformed_config_time(hass, caplog):
    # No window-end attribute on the off-peak sensor, so the config time is consulted — a
    # malformed stored value must degrade to "no window end", not raise (L-1).
    from datetime import UTC, datetime

    hass.states.async_set("binary_sensor.off_peak", "off")  # no current_end/next_end attrs
    await hass.async_block_till_done()
    adapter = Adapter(
        hass,
        {
            CONF_OFF_PEAK_SENSOR: "binary_sensor.off_peak",
            CONF_OVERNIGHT_WINDOW_END_TIME: "not-a-time",
        },
    )

    now = datetime(2026, 6, 8, 21, 0, tzinfo=UTC)
    assert adapter._off_peak_window_end(now, off_peak_now=False) is None
    assert "malformed overnight window end time" in caplog.text.lower()


# --- grid meter + battery power (verification inputs) ------------------------


async def test_grid_state_from_two_sensors(hass):
    hass.states.async_set("sensor.grid_import", "1200")
    hass.states.async_set("sensor.grid_export", "0")
    await hass.async_block_till_done()
    adapter = Adapter(
        hass,
        {
            CONF_GRID_IMPORT_SENSOR: "sensor.grid_import",
            CONF_GRID_EXPORT_SENSOR: "sensor.grid_export",
        },
    )
    grid = adapter._grid_state()
    assert grid is not None
    assert grid.import_w == pytest.approx(1200.0)
    assert grid.export_w == pytest.approx(0.0)
    assert grid.net_w == pytest.approx(1200.0)


async def test_grid_state_none_when_partial(hass):
    hass.states.async_set("sensor.grid_import", "1200")
    await hass.async_block_till_done()
    adapter = Adapter(hass, {CONF_GRID_IMPORT_SENSOR: "sensor.grid_import"})  # no export sensor
    assert adapter._grid_state() is None


async def test_grid_state_none_when_unavailable(hass):
    hass.states.async_set("sensor.grid_import", "unavailable")
    hass.states.async_set("sensor.grid_export", "0")
    await hass.async_block_till_done()
    adapter = Adapter(
        hass,
        {
            CONF_GRID_IMPORT_SENSOR: "sensor.grid_import",
            CONF_GRID_EXPORT_SENSOR: "sensor.grid_export",
        },
    )
    assert adapter._grid_state() is None


async def test_battery_power_read(hass):
    hass.states.async_set("sensor.battery_power", "1500")
    await hass.async_block_till_done()
    adapter = Adapter(hass, {CONF_BATTERY_POWER_SENSOR: "sensor.battery_power"})
    assert adapter._battery_power_w() == pytest.approx(1500.0)


async def test_battery_power_invert_toggle(hass):
    # Sensor reads +ve = charging; the toggle flips it to EC's +ve = discharging convention.
    hass.states.async_set("sensor.battery_power", "1500")
    await hass.async_block_till_done()
    adapter = Adapter(
        hass,
        {
            CONF_BATTERY_POWER_SENSOR: "sensor.battery_power",
            CONF_BATTERY_POWER_POSITIVE_IS_CHARGING: True,
        },
    )
    assert adapter._battery_power_w() == pytest.approx(-1500.0)


async def test_battery_power_none_when_unconfigured(hass):
    assert Adapter(hass, {})._battery_power_w() is None


async def test_battery_power_none_when_stale(hass, freezer):
    # Stale state must not drive the active verifier: a frozen "discharging" reading from a
    # stalled integration would otherwise confirm a false mismatch (Codex review).
    from datetime import timedelta

    from custom_components.energy_conductor.const import STALE_POWER_SECONDS

    hass.states.async_set("sensor.battery_power", "2000")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=STALE_POWER_SECONDS + 60))
    adapter = Adapter(hass, {CONF_BATTERY_POWER_SENSOR: "sensor.battery_power"})
    assert adapter._battery_power_w() is None

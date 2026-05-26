"""Unit tests for the jitter helper."""

from __future__ import annotations

import pytest

from energy_conductor.jitter import hourly_jitter_offset


def test_zero_offset_is_55_00():
    assert hourly_jitter_offset(0) == (55, 0)


def test_positive_60_offset_is_56_00():
    assert hourly_jitter_offset(60) == (56, 0)


def test_negative_60_offset_is_54_00():
    assert hourly_jitter_offset(-60) == (54, 0)


def test_positive_30_offset_is_55_30():
    assert hourly_jitter_offset(30) == (55, 30)


def test_negative_30_offset_is_54_30():
    assert hourly_jitter_offset(-30) == (54, 30)


def test_offset_below_range_raises():
    with pytest.raises(ValueError, match=r"-60, 60"):
        hourly_jitter_offset(-61)


def test_offset_above_range_raises():
    with pytest.raises(ValueError, match=r"-60, 60"):
        hourly_jitter_offset(61)


def test_minute_always_in_54_55_56():
    for off in range(-60, 61):
        minute, _ = hourly_jitter_offset(off)
        assert minute in (54, 55, 56)


def test_second_always_in_0_59():
    for off in range(-60, 61):
        _, second = hourly_jitter_offset(off)
        assert 0 <= second <= 59

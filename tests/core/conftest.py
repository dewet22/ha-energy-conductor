"""Shared helpers for core tests."""

from datetime import UTC, datetime


def utc(year: int = 2026, month: int = 6, day: int = 1, hour: int = 0, minute: int = 0) -> datetime:
    """Return a tz-aware UTC datetime. Defaults to summer solstice noon for stable tests."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def at(hhmm: str, *, on: datetime | None = None) -> datetime:
    """Return a UTC-aware datetime at HH:MM on a fixed test date (or `on` if provided)."""
    hour, minute = (int(p) for p in hhmm.split(":"))
    base = on or utc()
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

"""Pure helpers for scheduling jitter (no HA dependencies).

Used to spread `async_track_time_change` callbacks across the user
base so many HA instances of the integration don't fire at the
same wall-clock second (stampeding herd).
"""

from __future__ import annotations


def hourly_jitter_offset(rand_offset_seconds: int) -> tuple[int, int]:
    """Return (minute, second) for an hourly trigger jittered around HH:55:00.

    Args:
        rand_offset_seconds: A value in [-60, 60]. Caller picks via
            ``random.randint(-60, 60)`` once at startup so the same
            instance fires at the same time every hour.

    Returns:
        ``(minute, second)`` tuple suitable for
        ``async_track_time_change(hass, cb, minute=m, second=s)``.

    The base is HH:55:00 (5 minutes before the hour) so the plan rolls
    before any other hourly automations might key off it. Jitter spreads
    the actual fire time across HH:54:00..HH:56:00 (inclusive both ends).
    """
    if not -60 <= rand_offset_seconds <= 60:
        raise ValueError(f"rand_offset_seconds must be in [-60, 60], got {rand_offset_seconds}")
    total = 55 * 60 + rand_offset_seconds
    return divmod(total, 60)

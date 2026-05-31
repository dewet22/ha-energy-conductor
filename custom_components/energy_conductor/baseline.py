"""Learned uncontrolled-house baseline from historical power samples.

The baseline is the always-on house floor (fridge, network, standby, lighting)
that the battery must cover — explicitly EXCLUDING the managed loads EC schedules
(EV charger, hot-water diverter, …).

Rather than subtract managed loads from a home-load total (which would mix
readings from different meters/devices), we use a *filter-to-idle* rule: keep a
home-load reading only for buckets in which every declared managed load was idle
(below a small threshold). In those buckets the home-load sensor already *is* the
clean floor. A low-ish percentile (p50, biased up — under-provisioning the
baseline costs grid import) over the kept samples then rejects genuinely-unnamed
transient appliances (kettle, oven), which occupy a minority of hours.

The managed-load list is the single, explicit declaration of what to net out, so
the same logic works whether the home-load sensor includes managed loads (declare
them) or already excludes them (declare nothing — every bucket then qualifies).
"""

from __future__ import annotations

import math
import statistics


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def idle_floor_samples(
    home_load_by_bucket: dict[int, float],
    managed_by_bucket: list[dict[int, float]],
    *,
    idle_threshold_w: float,
) -> list[float]:
    """Home-load readings for buckets where every managed load was idle.

    `home_load_by_bucket` maps a bucket key (e.g. an hour-start timestamp) to the
    home-load mean power (W). `managed_by_bucket` is one such mapping per declared
    managed load.

    A bucket is kept only when:
      - its home-load value is a finite number, AND
      - every managed series has a finite value for that bucket key that is
        <= `idle_threshold_w`.

    A managed series that LACKS the bucket key drops the bucket: we cannot confirm
    the load was idle (e.g. the managed sensor was offline while the home-load
    sensor kept reporting), so excluding it avoids leaking an active-load bucket
    into the floor. With an empty managed list every finite home-load bucket is
    kept.
    """
    samples: list[float] = []
    for bucket, home_value in home_load_by_bucket.items():
        if not _is_finite_number(home_value):
            continue
        all_idle = True
        for managed in managed_by_bucket:
            managed_value = managed.get(bucket)
            if not _is_finite_number(managed_value) or managed_value > idle_threshold_w:
                all_idle = False
                break
        if all_idle:
            samples.append(float(home_value))
    return samples


def learned_baseline_w(
    samples: list[float], *, percentile: float, min_samples: int
) -> float | None:
    """Percentile of the idle-floor samples in watts, or None if too little data.

    Non-finite and negative values are dropped defensively (a baseline floor is
    >= 0). Returns None when fewer than `min_samples` usable values remain, so the
    caller can fall back to a static default.
    """
    usable = [float(v) for v in samples if _is_finite_number(v) and v >= 0]
    if len(usable) < min_samples:
        return None
    return statistics.quantiles(usable, n=100)[int(percentile * 100) - 1]

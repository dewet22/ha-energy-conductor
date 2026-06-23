"""Domain model for the energy_conductor core.

All datetime fields are timezone-aware UTC by contract (see spec §10).
Naive datetimes are rejected in __post_init__.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta


def _require_aware(name: str, value: datetime | None) -> None:
    if value is None:
        return
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (got naive datetime)")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC (+00:00), got utcoffset={value.utcoffset()!r}")


@dataclass(frozen=True)
class Battery:
    soc_percent: float
    capacity_kwh: float
    max_charge_power_w: int
    max_discharge_power_w: int
    reserve_percent: float
    # Live battery power, EC convention +ve = discharging. None when no sensor is configured.
    power_w: float | None = None

    def __post_init__(self) -> None:
        # capacity_kwh comes straight from config with no clamp site; 0 would be a
        # ZeroDivisionError deep in plan_overnight (swallowed → opaque silent stop), and
        # nan/inf slip past `<= 0` (nan compares false) to produce a bogus 100% target.
        if not math.isfinite(self.capacity_kwh) or self.capacity_kwh <= 0:
            raise ValueError(
                f"Battery.capacity_kwh must be finite and > 0 (got {self.capacity_kwh!r})"
            )
        # Invariant on the model; the adapter already clamps the live reserve sensor,
        # so this guards other construction paths and documents the contract.
        if not 0 <= self.reserve_percent <= 100:
            raise ValueError(
                f"Battery.reserve_percent must be in [0, 100] (got {self.reserve_percent!r})"
            )


@dataclass(frozen=True)
class EVCharger:
    power_w: float
    min_activation_power_w: int
    is_plugged_in: bool | None = None


@dataclass(frozen=True)
class GridState:
    """Live meter-boundary flow from two always-positive sensors (the form givenergy-hass
    exposes). Read-only observability — never feeds planning."""

    import_w: float
    export_w: float

    @property
    def net_w(self) -> float:
        """Net grid flow, +ve = importing."""
        return self.import_w - self.export_w


@dataclass(frozen=True)
class ForecastSlot:
    start: datetime
    energy_kwh: float

    def __post_init__(self) -> None:
        _require_aware("ForecastSlot.start", self.start)


@dataclass(frozen=True)
class SolarForecast:
    """Either `slots` is non-empty OR `fallback_kwh` is set — never both, never neither."""

    slots: tuple[ForecastSlot, ...]
    fallback_kwh: float | None
    fallback_source: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "slots", tuple(self.slots))
        has_slots = bool(self.slots)
        has_fallback = self.fallback_kwh is not None
        if has_slots == has_fallback:
            raise ValueError(
                "SolarForecast requires exactly one of (non-empty slots) "
                "or (fallback_kwh is not None)"
            )

    @property
    def total_kwh_forecast(self) -> float:
        if self.slots:
            return sum(slot.energy_kwh for slot in self.slots)
        assert self.fallback_kwh is not None  # invariant from __post_init__
        return self.fallback_kwh


@dataclass(frozen=True)
class HotWaterState:
    """Open-loop hot-water reserve estimate for a solar diverter (see hotwater.py)."""

    reserve_kwh: float
    capacity_kwh: float
    reserve_percent: float
    last_full_at: datetime | None  # most recent "Max temp reached"; None = none in lookback
    depletion_kwh_per_day: float
    depletion_source: str | None  # "stats" | "default"
    boost_recommended: bool
    suggested_boost_hours: float | None = None
    reserve_source: str | None = None  # "anchored" (confirmed full) | "cold_fill" (no anchor)

    def __post_init__(self) -> None:
        _require_aware("HotWaterState.last_full_at", self.last_full_at)


@dataclass(frozen=True)
class TariffState:
    off_peak_now: bool
    ev_dispatching_now: bool
    off_peak_window_end: datetime | None
    next_off_peak_window_start: datetime | None
    # The CONFIGURED overnight end (HH:MM rolled forward), independent of the off-peak
    # sensor — whose current/next period end can belong to a short dispatch slot. This
    # is the only trustworthy "dawn" for projections.
    overnight_window_end: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware("TariffState.off_peak_window_end", self.off_peak_window_end)
        _require_aware("TariffState.next_off_peak_window_start", self.next_off_peak_window_start)
        _require_aware("TariffState.overnight_window_end", self.overnight_window_end)


@dataclass(frozen=True)
class SiteState:
    now: datetime
    battery: Battery
    ev_charger: EVCharger | None
    solar_forecast: SolarForecast
    tariff: TariffState
    baseline_load_w: float
    baseline_source: str | None = None  # "stats" | "default"
    baseline_qualifying_buckets: int | None = None  # idle-floor buckets that fed the percentile
    daily_kwh_target: float = 0.0  # learned or static; consumed by overnight planner
    daily_kwh_target_source: str | None = None  # "stats" | "default"
    daily_kwh_target_qualifying_days: int | None = None  # daily totals that fed the percentile
    hot_water: HotWaterState | None = None  # None when the diverter isn't configured
    grid: GridState | None = None  # None when the grid meter sensors aren't configured

    def __post_init__(self) -> None:
        _require_aware("SiteState.now", self.now)

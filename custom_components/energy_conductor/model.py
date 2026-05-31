"""Domain model for the energy_conductor core.

All datetime fields are timezone-aware UTC by contract (see spec §10).
Naive datetimes are rejected in __post_init__.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class EVCharger:
    power_w: float
    min_activation_power_w: int
    is_plugged_in: bool | None = None


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
    def total_kwh_today(self) -> float:
        if self.slots:
            return sum(slot.energy_kwh for slot in self.slots)
        assert self.fallback_kwh is not None  # invariant from __post_init__
        return self.fallback_kwh


@dataclass(frozen=True)
class TariffState:
    off_peak_now: bool
    ev_dispatching_now: bool
    off_peak_window_end: datetime | None
    next_off_peak_window_start: datetime | None

    def __post_init__(self) -> None:
        _require_aware("TariffState.off_peak_window_end", self.off_peak_window_end)
        _require_aware("TariffState.next_off_peak_window_start", self.next_off_peak_window_start)


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

    def __post_init__(self) -> None:
        _require_aware("SiteState.now", self.now)

"""HA ↔ energy_conductor translation seam.

The ONLY file in custom_components that knows about both worlds.
Reads HA state, builds a SiteState, and (separately) handles the recorder
stats query for the seasonal fallback.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Any

from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CHEAP_RATE_SENSOR,
    CONF_DISPATCHING_SENSOR,
    CONF_EV_MIN_ACTIVATION_W,
    CONF_EV_POWER_SENSOR,
    CONF_FORECAST_DAILY_SENSOR,
    CONF_FORECAST_SOLCAST_SENSOR,
    CONF_FORECAST_SOURCE,
    CONF_SOLAR_GENERATION_SENSOR,
    CONF_SOUTHERN_HEMISPHERE,
    CONF_SUMMER_MAX_KWH,
    CONF_WINTER_MIN_KWH,
    DEFAULT_EV_MIN_ACTIVATION_W,
    DEFAULT_RESERVE_PERCENT,
    FORECAST_SOURCE_DAILY,
    FORECAST_SOURCE_NONE,
    FORECAST_SOURCE_SOLCAST,
    STALE_FORECAST_SECONDS,
    STALE_POWER_SECONDS,
    STATS_CALENDAR_WINDOW_DAYS,
    STATS_LOOKBACK_DAYS,
    STATS_MIN_DATA_POINTS,
    STATS_PERCENTILE,
)
from .fallback import seasonal_fallback_kwh
from .model import (
    Battery,
    EVCharger,
    ForecastSlot,
    SiteState,
    SolarForecast,
    TariffState,
)

_LOGGER = logging.getLogger(__name__)


def _read_float(hass: HomeAssistant, entity_id: str, *, max_age_seconds: int) -> float:
    state = hass.states.get(entity_id)
    if state is None:
        raise EntityProblem(f"{entity_id}: not found")
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""):
        raise EntityProblem(f"{entity_id}: state {state.state!r}")
    age = (dt_util.utcnow() - state.last_updated).total_seconds()
    if age > max_age_seconds:
        raise EntityProblem(f"{entity_id}: stale ({age:.0f}s > {max_age_seconds}s)")
    try:
        return float(state.state)
    except (TypeError, ValueError) as exc:
        raise EntityProblem(f"{entity_id}: not a float ({state.state!r})") from exc


def _read_bool(hass: HomeAssistant, entity_id: str | None) -> bool:
    if entity_id is None:
        return False
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""):
        return False
    return state.state == STATE_ON


def _max_attr(hass: HomeAssistant, entity_id: str, default: int) -> int:
    state = hass.states.get(entity_id)
    if state is None:
        return default
    raw = state.attributes.get("max")
    try:
        return int(raw)
    except TypeError, ValueError:
        return default


class EntityProblem(RuntimeError):
    """Raised when an entity we depend on is missing, unavailable, stale, or unparseable."""


class Adapter:
    """Builds SiteState from current HA state. One instance per config entry."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config

    async def build_site_state(self) -> SiteState:
        """Read entities and assemble a SiteState. Raises EntityProblem on hard failures."""
        now = dt_util.utcnow()

        # Battery — required entities; failure here is hard.
        # SoC sensors don't update when the battery is idle — use a loose staleness threshold
        soc = _read_float(
            self.hass, self.config[CONF_BATTERY_SOC_SENSOR], max_age_seconds=STALE_FORECAST_SECONDS
        )
        max_charge = _max_attr(self.hass, self.config[CONF_BATTERY_CHARGE_CONTROL], default=3000)
        max_discharge = _max_attr(
            self.hass, self.config[CONF_BATTERY_DISCHARGE_LIMIT], default=3000
        )
        battery = Battery(
            soc_percent=soc,
            capacity_kwh=float(self.config[CONF_BATTERY_CAPACITY_KWH]),
            max_charge_power_w=max_charge,
            max_discharge_power_w=max_discharge,
            reserve_percent=float(
                self.config.get(CONF_BATTERY_RESERVE_PERCENT, DEFAULT_RESERVE_PERCENT)
            ),
        )

        # Tariff
        cheap_now = _read_bool(self.hass, self.config[CONF_CHEAP_RATE_SENSOR])
        dispatching_now = _read_bool(self.hass, self.config.get(CONF_DISPATCHING_SENSOR))
        tariff = TariffState(
            cheap_window_now=cheap_now,
            ev_dispatching_now=dispatching_now,
            cheap_window_end=self._cheap_window_end(now, cheap_now),
            next_cheap_window_start=None,  # v1 does not compute this
        )

        # EV charger — optional
        ev_sensor = self.config.get(CONF_EV_POWER_SENSOR)
        ev_charger: EVCharger | None = None
        if ev_sensor:
            try:
                ev_power = _read_float(self.hass, ev_sensor, max_age_seconds=STALE_POWER_SECONDS)
                ev_charger = EVCharger(
                    power_w=ev_power,
                    min_activation_power_w=int(
                        self.config.get(CONF_EV_MIN_ACTIVATION_W, DEFAULT_EV_MIN_ACTIVATION_W)
                    ),
                    is_plugged_in=None,
                )
            except EntityProblem as exc:
                _LOGGER.warning("EV charger sensor unavailable: %s", exc)

        # Solar forecast
        solar_forecast = await self._build_forecast(now)

        # Baseline load — v1 placeholder; v2 will read this from recorder stats
        baseline_load_w = 400.0

        return SiteState(
            now=now,
            battery=battery,
            ev_charger=ev_charger,
            solar_forecast=solar_forecast,
            tariff=tariff,
            baseline_load_w=baseline_load_w,
        )

    def _cheap_window_end(self, now: datetime, cheap_now: bool) -> datetime | None:
        """Best-effort: derive the upcoming cheap-window end from config.

        v1 uses the configured `overnight_window_end_time` (local) and projects it onto
        today or tomorrow depending on whether it's already passed.
        """
        from .const import CONF_OVERNIGHT_WINDOW_END_TIME

        raw = self.config.get(CONF_OVERNIGHT_WINDOW_END_TIME)
        if raw is None:
            return None
        # raw is "HH:MM:SS" from TimeSelector
        hh, mm, *_ = (int(p) for p in raw.split(":"))
        local_now = dt_util.as_local(now)
        # Use combine+as_local so DST offset is recalculated from scratch for that wall-clock time
        candidate_local = dt_util.as_local(datetime.combine(local_now.date(), dt_time(hh, mm, 0)))
        if candidate_local <= local_now:
            # Roll forward using date arithmetic so DST transitions don't shift the wall-clock time
            tomorrow = (local_now + timedelta(days=1)).date()
            candidate_local = dt_util.as_local(datetime.combine(tomorrow, dt_time(hh, mm, 0)))
        return dt_util.as_utc(candidate_local)

    async def _build_forecast(self, now: datetime) -> SolarForecast:
        source = self.config.get(CONF_FORECAST_SOURCE, FORECAST_SOURCE_NONE)
        slots: list[ForecastSlot] = []

        if source == FORECAST_SOURCE_SOLCAST:
            sensor_id = self.config.get(CONF_FORECAST_SOLCAST_SENSOR)
            if sensor_id:
                slots = self._slots_from_solcast(sensor_id)
        elif source == FORECAST_SOURCE_DAILY:
            # Daily-total sensor: synthesise a single slot covering today
            sensor_id = self.config.get(CONF_FORECAST_DAILY_SENSOR)
            if sensor_id:
                try:
                    kwh = _read_float(self.hass, sensor_id, max_age_seconds=STALE_FORECAST_SECONDS)
                    slots = [ForecastSlot(start=now, energy_kwh=kwh)]
                except EntityProblem as exc:
                    _LOGGER.warning("Daily forecast sensor unavailable: %s", exc)

        if slots:
            return SolarForecast(slots=slots, fallback_kwh=None, fallback_source=None)

        fallback_kwh, fallback_source = await self._compute_fallback(now)
        return SolarForecast(slots=[], fallback_kwh=fallback_kwh, fallback_source=fallback_source)

    def _slots_from_solcast(self, sensor_id: str) -> list[ForecastSlot]:
        state = self.hass.states.get(sensor_id)
        if state is None:
            return []
        # Solcast HA integration exposes a 'detailedForecast' attribute as a list of dicts:
        # [{'period_start': '2026-06-21T05:30:00+00:00', 'pv_estimate': 0.5}, ...]
        raw = state.attributes.get("detailedForecast") or []
        slots: list[ForecastSlot] = []
        for item in raw:
            try:
                start_raw = item["period_start"]
                start = (
                    start_raw
                    if isinstance(start_raw, datetime)
                    else dt_util.parse_datetime(start_raw)
                )
                if start is None or start.tzinfo is None or start.utcoffset() is None:
                    continue
                kwh = float(item.get("pv_estimate", 0.0))
                slots.append(ForecastSlot(start=start, energy_kwh=kwh))
            except KeyError, TypeError, ValueError:
                continue
        return slots

    async def _compute_fallback(self, now: datetime) -> tuple[float, str]:
        gen_sensor = self.config.get(CONF_SOLAR_GENERATION_SENSOR)
        if gen_sensor:
            try:
                value = await self._stats_based_fallback(now, gen_sensor)
                if value is not None:
                    return value, "stats"
            except Exception:  # recorder failures must not crash the integration
                _LOGGER.exception("Stats-based fallback failed; falling through to seasonal")
        seasonal = seasonal_fallback_kwh(
            now,
            winter_min=float(self.config.get(CONF_WINTER_MIN_KWH, 0.0)),
            summer_max=float(self.config.get(CONF_SUMMER_MAX_KWH, 8.0)),
            southern_hemisphere=bool(self.config.get(CONF_SOUTHERN_HEMISPHERE, False)),
        )
        return seasonal, "seasonal"

    async def _stats_based_fallback(self, now: datetime, generation_entity: str) -> float | None:
        start_period = now - timedelta(days=STATS_LOOKBACK_DAYS)
        # statistics_during_period is async in HA 2024+
        stats = await statistics_during_period(
            self.hass,
            start_period,
            now,
            {generation_entity},
            "day",
            None,
            {"sum"},
        )
        rows = stats.get(generation_entity, [])
        local_today = dt_util.as_local(now).date()
        # Normalise to a leap year so DOY comparisons are on a consistent 366-day ring
        # and Feb-28/Mar-1 boundaries are handled correctly across leap and non-leap years.
        target_doy = local_today.replace(year=2000).timetuple().tm_yday
        window_values: list[float] = []
        # Solar generation sensors are total_increasing: `sum` is cumulative lifetime total.
        # Daily generation = difference between consecutive daily rows.
        for i in range(1, len(rows)):
            prev_sum = rows[i - 1].get("sum")
            curr_sum = rows[i].get("sum")
            if prev_sum is None or curr_sum is None:
                continue
            daily_kwh = float(curr_sum) - float(prev_sum)
            if daily_kwh < 0:
                continue  # skip meter resets
            ts = rows[i].get("start")
            if ts is None:
                continue
            ts_local = dt_util.as_local(
                ts if isinstance(ts, datetime) else dt_util.utc_from_timestamp(ts)
            )
            doy = ts_local.date().replace(year=2000).timetuple().tm_yday
            # Wrap-aware distance so Jan 1-14 can match late-December historical data
            dist = abs(doy - target_doy)
            dist = min(dist, 366 - dist)
            if dist <= STATS_CALENDAR_WINDOW_DAYS:
                window_values.append(daily_kwh)
        if len(window_values) < STATS_MIN_DATA_POINTS:
            return None
        return statistics.quantiles(window_values, n=100)[int(STATS_PERCENTILE * 100) - 1]

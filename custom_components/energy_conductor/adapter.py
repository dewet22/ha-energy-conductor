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

from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .baseline import idle_floor_samples, learned_baseline_w
from .const import (
    BASELINE_IDLE_THRESHOLD_W,
    BASELINE_LOOKBACK_DAYS,
    BASELINE_MIN_SAMPLES,
    BASELINE_PERCENTILE,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DAILY_ENERGY_SENSOR,
    CONF_DAILY_KWH_TARGET,
    CONF_DISPATCHING_SENSOR,
    CONF_EV_MIN_ACTIVATION_W,
    CONF_EV_POWER_SENSOR,
    CONF_FORECAST_DAILY_SENSOR,
    CONF_FORECAST_SOLCAST_SENSOR,
    CONF_FORECAST_SOURCE,
    CONF_HOME_LOAD_SENSOR,
    CONF_HOTWATER_CAPACITY_KWH,
    CONF_HOTWATER_DEPLETION_KWH,
    CONF_HOTWATER_GREEN_SENSOR,
    CONF_HOTWATER_HEATER_KW,
    CONF_HOTWATER_MAX_TEMP_STATE,
    CONF_HOTWATER_STATUS_SENSOR,
    CONF_HOTWATER_THRESHOLD_PERCENT,
    CONF_MANAGED_LOAD_SENSORS,
    CONF_OFF_PEAK_SENSOR,
    CONF_RESERVE_SOC_SENSOR,
    CONF_SOLAR_GENERATION_SENSOR,
    CONF_SOUTHERN_HEMISPHERE,
    CONF_SUMMER_MAX_KWH,
    CONF_WINTER_MIN_KWH,
    DAILY_TARGET_LOOKBACK_DAYS,
    DAILY_TARGET_MAX_KWH,
    DAILY_TARGET_MIN_SAMPLES,
    DAILY_TARGET_PERCENTILE,
    DEFAULT_BASELINE_LOAD_W,
    DEFAULT_DAILY_KWH_TARGET,
    DEFAULT_EV_MIN_ACTIVATION_W,
    DEFAULT_HOTWATER_CAPACITY_KWH,
    DEFAULT_HOTWATER_DEPLETION_KWH,
    DEFAULT_HOTWATER_HEATER_KW,
    DEFAULT_HOTWATER_MAX_TEMP_STATE,
    DEFAULT_HOTWATER_THRESHOLD_PERCENT,
    DEFAULT_RESERVE_PERCENT,
    DEFAULT_SUMMER_MAX_KWH,
    FORECAST_PLAUSIBILITY_MARGIN,
    FORECAST_SOURCE_DAILY,
    FORECAST_SOURCE_NONE,
    FORECAST_SOURCE_SOLCAST,
    HOTWATER_DEPLETION_MAX_KWH,
    HOTWATER_DEPLETION_MIN_SAMPLES,
    HOTWATER_DEPLETION_PERCENTILE,
    HOTWATER_DIVERSION_FRACTION,
    HOTWATER_LOOKBACK_DAYS,
    HOTWATER_MAX_BOOST_HOURS,
    HOTWATER_MIN_BOOST_HOURS,
    SOLCAST_SLOT_HOURS,
    STALE_FORECAST_SECONDS,
    STATS_CALENDAR_WINDOW_DAYS,
    STATS_LOOKBACK_DAYS,
    STATS_MIN_DATA_POINTS,
    STATS_PERCENTILE,
)
from .fallback import forecast_implausible, seasonal_fallback_kwh
from .hotwater import boost_recommendation, estimate_reserve, learn_depletion
from .learn_daily_target import learned_daily_kwh
from .model import (
    Battery,
    EVCharger,
    ForecastSlot,
    HotWaterState,
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


def _read_off_peak_attr(hass: HomeAssistant, entity_id: str, attr: str) -> datetime | None:
    """Read a timestamp attribute from the off-peak sensor, or None if absent/unparseable."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    raw = state.attributes.get(attr)
    if raw is None:
        return None
    parsed = dt_util.parse_datetime(str(raw))
    if parsed is None:
        return None
    return dt_util.as_utc(parsed)


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
            reserve_percent=self._reserve_percent(),
        )

        # Tariff
        off_peak_now = _read_bool(self.hass, self.config[CONF_OFF_PEAK_SENSOR])
        dispatching_now = _read_bool(self.hass, self.config.get(CONF_DISPATCHING_SENSOR))
        off_peak_sensor = self.config[CONF_OFF_PEAK_SENSOR]
        next_off_peak_start = _read_off_peak_attr(self.hass, off_peak_sensor, "next_start")
        tariff = TariffState(
            off_peak_now=off_peak_now,
            ev_dispatching_now=dispatching_now,
            off_peak_window_end=self._off_peak_window_end(now, off_peak_now),
            next_off_peak_window_start=next_off_peak_start,
        )

        # EV charger — optional.
        # Deliberately avoid the time-based staleness check here: some EV integrations
        # (e.g. myenergi) only write state when the value changes, so last_updated can be
        # many minutes old even during active charging.  HA's own unavailable/unknown
        # states are sufficient to detect a device going offline.
        ev_sensor = self.config.get(CONF_EV_POWER_SENSOR)
        ev_charger: EVCharger | None = None
        if ev_sensor:
            ev_state = self.hass.states.get(ev_sensor)
            if ev_state is not None and ev_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
                None,
                "",
            ):
                try:
                    ev_charger = EVCharger(
                        power_w=float(ev_state.state),
                        min_activation_power_w=int(
                            self.config.get(CONF_EV_MIN_ACTIVATION_W, DEFAULT_EV_MIN_ACTIVATION_W)
                        ),
                        is_plugged_in=None,
                    )
                except (TypeError, ValueError) as exc:
                    _LOGGER.warning("EV charger sensor unreadable (%s): %s", ev_sensor, exc)
            elif ev_state is None:
                _LOGGER.warning("EV charger sensor not found: %s", ev_sensor)
            else:
                _LOGGER.debug("EV charger sensor offline: %s (state=%r)", ev_sensor, ev_state.state)

        # Solar forecast
        solar_forecast = await self._build_forecast(now)

        # Baseline load — learned from the home-load sensor's idle-floor history.
        baseline_load_w, baseline_source, baseline_qualifying_buckets = self._compute_baseline(now)

        # Daily kWh target — learned from house-energy history when configured.
        daily_kwh_target, daily_target_source, daily_target_qualifying_days = (
            self._compute_daily_target(now)
        )

        # Hot-water reserve — optional; None unless the Eddi green + status sensors are set.
        hot_water = self._hot_water_state(now, solar_forecast)

        return SiteState(
            now=now,
            battery=battery,
            ev_charger=ev_charger,
            solar_forecast=solar_forecast,
            tariff=tariff,
            baseline_load_w=baseline_load_w,
            baseline_source=baseline_source,
            baseline_qualifying_buckets=baseline_qualifying_buckets,
            daily_kwh_target=daily_kwh_target,
            daily_kwh_target_source=daily_target_source,
            daily_kwh_target_qualifying_days=daily_target_qualifying_days,
            hot_water=hot_water,
        )

    def _reserve_percent(self) -> float:
        """Minimum-SoC reserve floor.

        Prefer a live reserve sensor (e.g. GivEnergy battery_soc_reserve) when one
        is configured, so EC tracks the inverter's actual floor. Fall back to the
        static configured reserve percent if the sensor is unset or unreadable.
        """
        sensor = self.config.get(CONF_RESERVE_SOC_SENSOR)
        if sensor:
            try:
                return _read_float(self.hass, sensor, max_age_seconds=STALE_FORECAST_SECONDS)
            except EntityProblem as exc:
                _LOGGER.warning("Reserve SoC sensor unreadable (%s); using config value", exc)
        return float(self.config.get(CONF_BATTERY_RESERVE_PERCENT, DEFAULT_RESERVE_PERCENT))

    def _off_peak_window_end(self, now: datetime, off_peak_now: bool) -> datetime | None:
        """Best-effort: derive the upcoming off-peak window end.

        Prefers next_end/current_end attributes on the off-peak sensor (present in
        integrations like Octopus Energy), falling back to the configured HH:MM time.
        """
        from .const import CONF_OVERNIGHT_WINDOW_END_TIME

        attr = "current_end" if off_peak_now else "next_end"
        ts = _read_off_peak_attr(self.hass, self.config[CONF_OFF_PEAK_SENSOR], attr)
        if ts is not None:
            return ts

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
                slots = self._slots_from_solcast(sensor_id, now)
        elif source == FORECAST_SOURCE_DAILY:
            sensor_id = self.config.get(CONF_FORECAST_DAILY_SENSOR)
            if sensor_id:
                try:
                    kwh = _read_float(self.hass, sensor_id, max_age_seconds=STALE_FORECAST_SECONDS)
                    # Use as fallback_kwh, not as a synthetic slot. A daily-total
                    # sensor shows today's actual generation by planning time (~21:00).
                    # Creating a synthetic slot with start=now made _morning_gap_hours()
                    # return 0 (the slot predates tomorrow's window end), so the plan
                    # never provisioned a morning gap. Treating it as fallback_kwh
                    # preserves the MISSING_FORECAST_GAP_H default gap assumption.
                    return self._checked_forecast(
                        SolarForecast(slots=[], fallback_kwh=kwh, fallback_source="daily_sensor")
                    )
                except EntityProblem as exc:
                    _LOGGER.warning("Daily forecast sensor unavailable: %s", exc)

        if slots:
            return self._checked_forecast(
                SolarForecast(slots=slots, fallback_kwh=None, fallback_source=None)
            )

        fallback_kwh, fallback_source = await self._compute_fallback(now)
        return SolarForecast(slots=[], fallback_kwh=fallback_kwh, fallback_source=fallback_source)

    def _checked_forecast(self, forecast: SolarForecast) -> SolarForecast:
        """Log a warning if the forecast total is physically implausible.

        Guards against unit bugs (e.g. kW read as kWh) by comparing against the
        configured summer-max ceiling. Does not clamp — the data is preserved for
        inspection; the warning makes the anomaly visible. Only checked for real
        forecast sources (Solcast slots, daily sensor); the seasonal/stats fallback
        is bounded by summer_max by construction.
        """
        total = forecast.total_kwh_forecast
        summer_max = float(self.config.get(CONF_SUMMER_MAX_KWH, DEFAULT_SUMMER_MAX_KWH))
        if forecast_implausible(total, summer_max, margin=FORECAST_PLAUSIBILITY_MARGIN):
            _LOGGER.warning(
                "Solar forecast %.1f kWh (source=%s) exceeds plausibility ceiling "
                "(summer_max %.1f kWh x %.1f). Possible unit error in the forecast source.",
                total,
                forecast.fallback_source or "slots",
                summer_max,
                FORECAST_PLAUSIBILITY_MARGIN,
            )
        return forecast

    def _slots_from_solcast(self, sensor_id: str, now: datetime) -> list[ForecastSlot]:
        state = self.hass.states.get(sensor_id)
        if state is None:
            return []
        # Solcast HA integration exposes a 'detailedForecast' attribute as a list of dicts:
        # [{'period_start': datetime(2026-06-01, tzinfo=Europe/London), 'pv_estimate': 0.5}, ...]
        # period_start is a timezone-aware datetime in the HA instance's local timezone.
        raw = state.attributes.get("detailedForecast") or []
        # Determine tomorrow in local time for the date filter below.
        tomorrow_local = dt_util.as_local(now + timedelta(days=1)).date()
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
                # ForecastSlot requires UTC. Solcast provides local-timezone datetimes
                # (e.g. BST / Europe/London). Convert explicitly — without this, every
                # slot raises ValueError in ForecastSlot.__post_init__ and is silently
                # dropped by the except block, leaving the forecast always empty.
                start_utc = dt_util.as_utc(start)
                # Keep only tomorrow's local-date slots. This filters out today's
                # past/current slots (which inflate the planning total) and multi-day
                # lookahead — defence-in-depth if the user configures a "forecast today"
                # or aggregate sensor instead of the dedicated "forecast tomorrow" sensor.
                if dt_util.as_local(start_utc).date() != tomorrow_local:
                    continue
                # pv_estimate is AVERAGE POWER (kW) over the 30-min slot, NOT energy.
                # Energy for the slot = power * slot duration (0.5h).
                kwh = float(item.get("pv_estimate", 0.0)) * SOLCAST_SLOT_HOURS
                slots.append(ForecastSlot(start=start_utc, energy_kwh=kwh))
            except KeyError, TypeError, ValueError:
                continue
        return slots

    async def _compute_fallback(self, now: datetime) -> tuple[float, str]:
        gen_sensor = self.config.get(CONF_SOLAR_GENERATION_SENSOR)
        if gen_sensor:
            try:
                value = self._stats_based_fallback(now, gen_sensor)
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

    def _stats_based_fallback(self, now: datetime, generation_entity: str) -> float | None:
        start_period = now - timedelta(days=STATS_LOOKBACK_DAYS)
        # statistics_during_period is synchronous in HA 2026.5+ (returns a defaultdict directly).
        stats = statistics_during_period(
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

    def _compute_baseline(self, now: datetime) -> tuple[float, str, int | None]:
        """Return (baseline_w, source, qualifying_buckets) for the uncontrolled house floor.

        Learns from the home-load sensor's idle-floor history when configured;
        otherwise (or on insufficient data / recorder failure) returns the static
        default. `source` is "stats" or "default"; `qualifying_buckets` is the number
        of idle-floor hours that fed the percentile (None when falling back to default).
        """
        home_sensor = self.config.get(CONF_HOME_LOAD_SENSOR)
        if home_sensor:
            try:
                result = self._stats_based_baseline(now, home_sensor)
                if result is not None:
                    value, n_buckets = result
                    return value, "stats", n_buckets
            except Exception:  # recorder failures must not crash the integration
                _LOGGER.exception("Stats-based baseline failed; using default")
        return DEFAULT_BASELINE_LOAD_W, "default", None

    def _stats_based_baseline(self, now: datetime, home_entity: str) -> tuple[float, int] | None:
        managed_entities = list(self.config.get(CONF_MANAGED_LOAD_SENSORS) or [])
        entities = {home_entity, *managed_entities}
        start_period = now - timedelta(days=BASELINE_LOOKBACK_DAYS)
        # statistics_during_period is synchronous in HA 2026.5+ (returns a defaultdict).
        # The home-load sensor is instantaneous power (state_class=measurement), so we
        # query the hourly "mean" directly — no delta computation.
        stats = statistics_during_period(
            self.hass, start_period, now, entities, "hour", None, {"mean"}
        )

        def _by_bucket(entity_id: str) -> dict[int, float]:
            buckets: dict[int, float] = {}
            for row in stats.get(entity_id, []):
                start = row.get("start")
                mean = row.get("mean")
                if start is None or mean is None:
                    continue
                # Normalise the bucket key to an int second-resolution timestamp.
                key = int(start.timestamp()) if isinstance(start, datetime) else int(start)
                buckets[key] = float(mean)
            return buckets

        home_by_bucket = _by_bucket(home_entity)
        managed_by_bucket = [_by_bucket(e) for e in managed_entities]
        # Idle-filter + missing-managed-data exclusion live in the pure helper.
        samples = idle_floor_samples(
            home_by_bucket, managed_by_bucket, idle_threshold_w=BASELINE_IDLE_THRESHOLD_W
        )
        value = learned_baseline_w(
            samples, percentile=BASELINE_PERCENTILE, min_samples=BASELINE_MIN_SAMPLES
        )
        if value is None:
            return None
        return value, len(samples)

    def _compute_daily_target(self, now: datetime) -> tuple[float, str, int | None]:
        """Return (daily_kwh_target, source, qualifying_days) for the overnight planner.

        Learns from a cumulative house-energy sensor's daily totals when configured;
        otherwise (or on insufficient data / recorder failure) returns the static
        configured value. `source` is "stats" or "default"; `qualifying_days` is the
        number of daily totals that fed the percentile (None when falling back).
        """
        static_default = float(self.config.get(CONF_DAILY_KWH_TARGET, DEFAULT_DAILY_KWH_TARGET))
        energy_sensor = self.config.get(CONF_DAILY_ENERGY_SENSOR)
        if energy_sensor:
            try:
                result = self._stats_based_daily_target(now, energy_sensor)
                if result is not None:
                    value, n_days = result
                    return value, "stats", n_days
            except Exception:  # recorder failures must not crash the integration
                _LOGGER.exception("Stats-based daily target failed; using static default")
        return static_default, "default", None

    def _stats_based_daily_target(
        self, now: datetime, energy_entity: str
    ) -> tuple[float, int] | None:
        start_period = now - timedelta(days=DAILY_TARGET_LOOKBACK_DAYS + 1)
        # statistics_during_period is synchronous in HA 2026.5+ (returns a defaultdict).
        # The energy sensor is total_increasing; `sum` accumulates lifetime kWh and the
        # recorder accounts for daily resets internally. Daily kWh = diff between
        # consecutive daily-period rows (same pattern as `_stats_based_fallback`).
        stats = statistics_during_period(
            self.hass,
            start_period,
            now,
            {energy_entity},
            "day",
            None,
            {"sum"},
        )
        rows = stats.get(energy_entity, [])
        # Exclude today's partial day so a low half-day reading doesn't drag the median.
        local_today = dt_util.as_local(now).date()
        daily_totals: list[float] = []
        for i in range(1, len(rows)):
            prev_sum = rows[i - 1].get("sum")
            curr_sum = rows[i].get("sum")
            if prev_sum is None or curr_sum is None:
                continue
            ts = rows[i].get("start")
            if ts is None:
                continue
            ts_local = dt_util.as_local(
                ts if isinstance(ts, datetime) else dt_util.utc_from_timestamp(ts)
            )
            if ts_local.date() >= local_today:
                continue
            daily_kwh = float(curr_sum) - float(prev_sum)
            if not (0 < daily_kwh <= DAILY_TARGET_MAX_KWH):
                continue  # skip resets, rollover, and strategy-change outliers
            daily_totals.append(daily_kwh)
        value = learned_daily_kwh(
            daily_totals,
            percentile=DAILY_TARGET_PERCENTILE,
            min_samples=DAILY_TARGET_MIN_SAMPLES,
        )
        if value is None:
            return None
        return value, len(daily_totals)

    def _hot_water_state(
        self, now: datetime, solar_forecast: SolarForecast
    ) -> HotWaterState | None:
        """Estimate the hot-water reserve and whether a boost should be prompted.

        Open-loop energy balance anchored by the diverter's "Max temp reached" status
        (see hotwater.py). None unless both core sensors (green diversion + status) are set;
        any recorder failure degrades to None so the rest of the tick is unaffected.
        """
        green_sensor = self.config.get(CONF_HOTWATER_GREEN_SENSOR)
        status_sensor = self.config.get(CONF_HOTWATER_STATUS_SENSOR)
        if not green_sensor or not status_sensor:
            return None
        try:
            capacity = float(
                self.config.get(CONF_HOTWATER_CAPACITY_KWH, DEFAULT_HOTWATER_CAPACITY_KWH)
            )
            threshold = float(
                self.config.get(CONF_HOTWATER_THRESHOLD_PERCENT, DEFAULT_HOTWATER_THRESHOLD_PERCENT)
            )
            heater_kw = float(self.config.get(CONF_HOTWATER_HEATER_KW, DEFAULT_HOTWATER_HEATER_KW))
            depletion_fallback = float(
                self.config.get(CONF_HOTWATER_DEPLETION_KWH, DEFAULT_HOTWATER_DEPLETION_KWH)
            )
            max_temp_state = self.config.get(
                CONF_HOTWATER_MAX_TEMP_STATE, DEFAULT_HOTWATER_MAX_TEMP_STATE
            )

            last_full_at, full_dates = self._hot_water_full_events(
                now, status_sensor, max_temp_state
            )
            daily_green = self._hot_water_daily_green(now, green_sensor)
            depletion, depletion_source = learn_depletion(
                self._hot_water_steady_samples(daily_green, full_dates),
                percentile=HOTWATER_DEPLETION_PERCENTILE,
                min_samples=HOTWATER_DEPLETION_MIN_SAMPLES,
                fallback=depletion_fallback,
            )

            if last_full_at is None:
                # No confirmed-full event in the lookback window → assume depleted.
                reserve = 0.0
            else:
                green_since = self._hot_water_green_since(last_full_at, now, green_sensor)
                reserve = estimate_reserve(
                    elapsed_hours_since_full=(now - last_full_at).total_seconds() / 3600.0,
                    energy_in_since_full_kwh=green_since,
                    depletion_kwh_per_day=depletion,
                    capacity_kwh=capacity,
                )

            expected_refill = min(
                capacity, max(0.0, solar_forecast.total_kwh_forecast * HOTWATER_DIVERSION_FRACTION)
            )
            boost_recommended, hours = boost_recommendation(
                reserve_kwh=reserve,
                capacity_kwh=capacity,
                threshold_percent=threshold,
                expected_refill_kwh=expected_refill,
                depletion_kwh_per_day=depletion,
                heater_kw=heater_kw,
                min_hours=HOTWATER_MIN_BOOST_HOURS,
                max_hours=HOTWATER_MAX_BOOST_HOURS,
            )
            return HotWaterState(
                reserve_kwh=round(reserve, 2),
                capacity_kwh=capacity,
                reserve_percent=round(reserve / capacity * 100, 1) if capacity > 0 else 0.0,
                last_full_at=last_full_at,
                depletion_kwh_per_day=round(depletion, 2),
                depletion_source=depletion_source,
                boost_recommended=boost_recommended,
                suggested_boost_hours=hours,
            )
        except Exception:  # recorder/parse failures must not crash the tick
            _LOGGER.exception("Hot-water reserve estimate failed; skipping")
            return None

    def _hot_water_full_events(
        self, now: datetime, status_entity: str, max_temp_state: str
    ) -> tuple[datetime | None, set]:
        """Return (last 'Max temp reached' timestamp, set of local dates it occurred on)."""
        start = now - timedelta(days=HOTWATER_LOOKBACK_DAYS)
        states_by_entity = state_changes_during_period(
            self.hass,
            start,
            now,
            status_entity,
            no_attributes=True,  # status is very chatty; we only need state + last_changed
            include_start_time_state=False,
        )
        full_times = [
            s.last_changed
            for s in states_by_entity.get(status_entity, [])
            if s.state == max_temp_state
        ]
        if not full_times:
            return None, set()
        full_dates = {dt_util.as_local(ts).date() for ts in full_times}
        return max(full_times), full_dates

    def _hot_water_daily_green(self, now: datetime, green_entity: str) -> dict:
        """Map each complete local date in the lookback to that day's green diversion (kWh)."""
        start = now - timedelta(days=HOTWATER_LOOKBACK_DAYS)
        stats = statistics_during_period(
            self.hass, start, now, {green_entity}, "day", None, {"change"}
        )
        local_today = dt_util.as_local(now).date()
        daily: dict = {}
        for row in stats.get(green_entity, []):
            change = row.get("change")
            ts = row.get("start")
            if change is None or ts is None:
                continue
            ts = ts if isinstance(ts, datetime) else dt_util.utc_from_timestamp(ts)
            day = dt_util.as_local(ts).date()
            if day >= local_today:
                continue  # skip today's partial day
            daily[day] = float(change)
        return daily

    def _hot_water_steady_samples(self, daily_green: dict, full_dates: set) -> list[float]:
        """Green totals for days bracketed by 'Max temp reached' on both that day and the prior.

        On such full→full days net tank energy ≈ 0, so the day's green diversion ≈ depletion.
        """
        samples: list[float] = []
        for day, kwh in daily_green.items():
            prev = day - timedelta(days=1)
            if day in full_dates and prev in full_dates and 0 < kwh <= HOTWATER_DEPLETION_MAX_KWH:
                samples.append(kwh)
        return samples

    def _hot_water_green_since(
        self, last_full_at: datetime, now: datetime, green_entity: str
    ) -> float:
        """Green diversion energy (kWh) accumulated since the last full event."""
        stats = statistics_during_period(
            self.hass, last_full_at, now, {green_entity}, "hour", None, {"change"}
        )
        return sum(
            float(row["change"])
            for row in stats.get(green_entity, [])
            if row.get("change") is not None
        )

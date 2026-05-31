"""Constants and config keys for the energy_conductor HA integration."""

from datetime import time

DOMAIN = "energy_conductor"

# Coordinator
COORDINATOR_TICK_SECONDS = 30

# Config keys — battery
CONF_BATTERY_SOC_SENSOR = "battery_soc_sensor"
CONF_BATTERY_CHARGE_CONTROL = "battery_charge_control"
CONF_BATTERY_DISCHARGE_LIMIT = "battery_discharge_limit"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_BATTERY_RESERVE_PERCENT = "battery_reserve_percent"

# Config keys — tariff
CONF_CHEAP_RATE_SENSOR = "cheap_rate_sensor"
CONF_DISPATCHING_SENSOR = "dispatching_sensor"
CONF_OVERNIGHT_WINDOW_END_TIME = "overnight_window_end_time"

# Config keys — solar forecast
CONF_FORECAST_SOURCE = "forecast_source"
CONF_FORECAST_SOLCAST_SENSOR = "forecast_solcast_sensor"
CONF_FORECAST_DAILY_SENSOR = "forecast_daily_sensor"
CONF_SOLAR_GENERATION_SENSOR = "solar_generation_sensor"
CONF_WINTER_MIN_KWH = "winter_min_kwh"
CONF_SUMMER_MAX_KWH = "summer_max_kwh"
CONF_SOUTHERN_HEMISPHERE = "southern_hemisphere"

# Config keys — EV charger
CONF_EV_POWER_SENSOR = "ev_power_sensor"
CONF_EV_MIN_ACTIVATION_W = "ev_min_activation_power_w"

# Config keys — house load / managed loads
CONF_HOME_LOAD_SENSOR = "home_load_sensor"  # home-load power sensor (any semantics)
CONF_MANAGED_LOAD_SENSORS = "managed_load_sensors"  # list[str]; loads baked in, to net out

# Config keys — behaviour
CONF_WRITE_MODE = "write_mode"
CONF_NOTIFY_TARGET = "notify_target"
CONF_OVERNIGHT_PLAN_TIME = "overnight_plan_time"
CONF_DAILY_KWH_TARGET = "daily_kwh_target"
CONF_DEVICE_NAME = "device_name"

# Enum values
FORECAST_SOURCE_SOLCAST = "solcast"
FORECAST_SOURCE_DAILY = "daily_total_sensor"
FORECAST_SOURCE_NONE = "none"

WRITE_MODE_DRY_RUN = "dry_run"
WRITE_MODE_LIVE = "live"

# Defaults
DEFAULT_RESERVE_PERCENT = 10
DEFAULT_EV_MIN_ACTIVATION_W = 1400
DEFAULT_WINTER_MIN_KWH = 0.0
DEFAULT_SUMMER_MAX_KWH = 8.0
DEFAULT_DAILY_KWH_TARGET = 10.0
DEFAULT_OVERNIGHT_PLAN_TIME = time(21, 0)
DEFAULT_OVERNIGHT_WINDOW_END_TIME = time(5, 30)

# Stats fallback
STATS_LOOKBACK_DAYS = 365
STATS_CALENDAR_WINDOW_DAYS = 14
STATS_MIN_DATA_POINTS = 7
STATS_PERCENTILE = 0.25

# Baseline-load learning (filter-to-idle over a short trailing window)
DEFAULT_BASELINE_LOAD_W = 400.0  # used when no home-load sensor or insufficient data
BASELINE_LOOKBACK_DAYS = 14
BASELINE_MIN_SAMPLES = 48  # ~2 days of hourly buckets; sanity floor, not a warmup gate
BASELINE_PERCENTILE = 0.50  # p50 of idle-floor samples; biased up (see baseline.py)
BASELINE_IDLE_THRESHOLD_W = 50.0  # managed load <= this counts as "off" for a bucket

# Staleness thresholds
STALE_POWER_SECONDS = 5 * 60  # 5 minutes
STALE_FORECAST_SECONDS = 24 * 3600  # 24 hours

# Diagnostic sensor states
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_ERROR = "error"

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
# Optional: read the live minimum-SoC floor from an entity (e.g. GivEnergy
# battery_soc_reserve) instead of the static reserve percent above. May be a
# `number` or `sensor` entity. Falls back to CONF_BATTERY_RESERVE_PERCENT when unset.
CONF_RESERVE_SOC_SENSOR = "reserve_soc_sensor"

# Config keys — grid meter + actuation verification (all optional; feature inert when unset)
CONF_GRID_IMPORT_SENSOR = "grid_import_sensor"
CONF_GRID_EXPORT_SENSOR = "grid_export_sensor"
CONF_BATTERY_POWER_SENSOR = "battery_power_sensor"
# EC's convention is +ve = discharging; set this when the configured sensor reads +ve = charging.
CONF_BATTERY_POWER_POSITIVE_IS_CHARGING = "battery_power_positive_is_charging"

# Config keys — tariff
CONF_OFF_PEAK_SENSOR = "off_peak_sensor"
CONF_DISPATCHING_SENSOR = "dispatching_sensor"
CONF_OVERNIGHT_WINDOW_END_TIME = "overnight_window_end_time"
# Legacy key kept only for the v1→v2 config-entry migration; do not use elsewhere.
_LEGACY_CONF_CHEAP_RATE_SENSOR = "cheap_rate_sensor"

# Config keys — solar forecast
CONF_FORECAST_SOURCE = "forecast_source"
CONF_FORECAST_SOLCAST_SENSOR = "forecast_solcast_sensor"
CONF_FORECAST_SOLCAST_TODAY_SENSOR = "forecast_solcast_today_sensor"
CONF_FORECAST_DAILY_SENSOR = "forecast_daily_sensor"
CONF_SOLAR_GENERATION_SENSOR = "solar_generation_sensor"
CONF_SOLAR_POWER_SENSOR = "solar_power_sensor"
CONF_WINTER_MIN_KWH = "winter_min_kwh"
CONF_SUMMER_MAX_KWH = "summer_max_kwh"
CONF_SOUTHERN_HEMISPHERE = "southern_hemisphere"

# Config keys — EV charger
CONF_EV_POWER_SENSOR = "ev_power_sensor"
CONF_EV_MIN_ACTIVATION_W = "ev_min_activation_power_w"

# Config keys — hot water (myenergi Eddi diverter)
# Core: a green/diverted-energy sensor (kWh, total_increasing) drives the energy balance,
# and a status sensor whose "Max temp reached" state anchors the estimate to a full tank.
# Boost energy enters the model ONLY via that anchor, so green-only energy-in is correct
# whether or not a scheduled boost is running. Total-in is optional, display-only.
CONF_HOTWATER_GREEN_SENSOR = "hotwater_green_sensor"
CONF_HOTWATER_STATUS_SENSOR = "hotwater_status_sensor"
CONF_HOTWATER_ENERGY_SENSOR = "hotwater_energy_sensor"  # optional total-in, display only
CONF_HOTWATER_POWER_SENSOR = "hotwater_power_sensor"  # optional diverter power (W); tape rail
CONF_HOTWATER_CAPACITY_KWH = "hotwater_capacity_kwh"
CONF_HOTWATER_DEPLETION_KWH = "hotwater_depletion_kwh"  # fallback when not learned
CONF_HOTWATER_THRESHOLD_PERCENT = "hotwater_threshold_percent"
CONF_HOTWATER_HEATER_KW = "hotwater_heater_kw"
CONF_HOTWATER_MAX_TEMP_STATE = "hotwater_max_temp_state"

# Config keys — house load / managed loads
CONF_HOME_LOAD_SENSOR = "home_load_sensor"  # home-load power sensor (any semantics)
CONF_MANAGED_LOAD_SENSORS = "managed_load_sensors"  # list[str]; loads baked in, to net out
# Cumulative house-energy sensor (kWh, total_increasing). Optional; when set, the
# integration learns daily_kwh_target from recorder daily sums instead of using the
# static config value.
CONF_DAILY_ENERGY_SENSOR = "daily_energy_sensor"

# Config keys — costs / money (all optional; each money sensor is created only when its
# own source entities are configured, so any subset of this group is useful).
# Read-through entities (billing-grade where the supplier provides them):
CONF_IMPORT_COST_SENSOR = "import_cost_sensor"  # today's accumulative import cost
CONF_IMPORT_COST_OFF_PEAK_SENSOR = "import_cost_off_peak_sensor"
CONF_IMPORT_COST_PEAK_SENSOR = "import_cost_peak_sensor"
CONF_EXPORT_EARNINGS_SENSOR = "export_earnings_sensor"  # today's export earnings
CONF_STANDING_CHARGE_ELECTRICITY_SENSOR = "standing_charge_electricity_sensor"
CONF_STANDING_CHARGE_GAS_SENSOR = "standing_charge_gas_sensor"
CONF_GAS_COST_SENSOR = "gas_cost_sensor"  # today's accumulative gas cost
CONF_GAS_ENERGY_SENSOR = "gas_energy_sensor"  # today's gas kWh
# Rates (unit p/kWh or GBP/kWh, normalised at read time — see money.normalise_rate):
CONF_IMPORT_RATE_SENSOR = "import_rate_sensor"
CONF_EXPORT_RATE_SENSOR = "export_rate_sensor"
CONF_GAS_RATE_SENSOR = "gas_rate_sensor"
# Daily energy counters (kWh) feeding the modelled avoided-cost lines:
CONF_PV_ENERGY_SENSOR = "pv_energy_sensor"
CONF_GRID_IMPORT_ENERGY_SENSOR = "grid_import_energy_sensor"
CONF_GRID_EXPORT_ENERGY_SENSOR = "grid_export_energy_sensor"
CONF_BATTERY_DISCHARGE_ENERGY_SENSOR = "battery_discharge_energy_sensor"
CONF_EV_ENERGY_SENSOR = "ev_energy_sensor"
CONF_EV_GREEN_ENERGY_SENSOR = "ev_green_energy_sensor"  # EV energy charged from solar
# Scalars for the payback tracker and the EV comparator:
CONF_SYSTEM_CAPITAL_COST = "system_capital_cost_gbp"
CONF_SYSTEM_INSTALL_DATE = "system_install_date"
CONF_PUBLIC_CHARGING_RATE = "public_charging_rate_gbp_per_kwh"

# Internal: per-reference {platform, unique_id} anchors captured for every entity the
# config points at, so a referenced entity that is re-created with a new entity_id (e.g. the
# HA 2026.6 area-prefix convention, core #170560) is resolved back via its stable unique_id.
# Not user-facing; populated by the wizard on create and by the v2→v3 migration. See entity_ref.py.
CONF_ENTITY_REFS = "entity_refs"

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
DEFAULT_BATTERY_MAX_POWER_W = 3000  # fallback when entity lacks a 'max' attribute
DEFAULT_WINTER_MIN_KWH = 0.0
DEFAULT_SUMMER_MAX_KWH = 8.0
DEFAULT_DAILY_KWH_TARGET = 10.0
DEFAULT_OVERNIGHT_PLAN_TIME = time(21, 0)
DEFAULT_OVERNIGHT_WINDOW_END_TIME = time(5, 30)

# Hot water (Eddi) defaults
DEFAULT_HOTWATER_CAPACITY_KWH = 11.0  # 210 L cold-to-hot: ~210 kg * 4.186 kJ/kg.K * ~45 C
DEFAULT_HOTWATER_DEPLETION_KWH = 3.0  # lumped daily standing loss + draw, when not learned
DEFAULT_HOTWATER_THRESHOLD_PERCENT = 20  # lean: prompt late, tune up if caught cold
DEFAULT_HOTWATER_HEATER_KW = 2.7  # Eddi immersion element; sets suggested boost hours
DEFAULT_HOTWATER_MAX_TEMP_STATE = "Max temp reached"  # status value meaning "tank full"

# Solar forecast
# Solcast detailedForecast `pv_estimate` is AVERAGE POWER (kW) over each slot, not
# energy. Slots are 30 minutes, so energy_kwh = pv_estimate * 0.5h.
SOLCAST_SLOT_HOURS = 0.5
# Plausibility ceiling: warn if any forecast total exceeds the configured summer
# max by this margin (catches unit bugs like the kW-as-kWh 2x error). Headroom
# above the typical summer max for an exceptional clear day without false alarms.
FORECAST_PLAUSIBILITY_MARGIN = 1.5

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

# Daily-energy-target learning (median of recent daily consumption)
DAILY_TARGET_LOOKBACK_DAYS = 14
DAILY_TARGET_MIN_SAMPLES = 7
DAILY_TARGET_PERCENTILE = 0.50
DAILY_TARGET_MAX_KWH = 50.0  # reject outliers from sensor strategy changes or meter glitches

# Hot-water reserve learning (energy-balance estimate anchored by "Max temp reached")
HOTWATER_LOOKBACK_DAYS = 10  # bounded by raw state-history retention for the status anchor
HOTWATER_DEPLETION_MIN_SAMPLES = 5  # steady (full→full) days needed before learning kicks in
HOTWATER_DEPLETION_PERCENTILE = 0.50
HOTWATER_DEPLETION_MAX_KWH = 20.0  # reject implausible steady-day green totals
# Fraction of tomorrow's forecast generation assumed to reach the tank as diversion. Crude
# v1 heuristic for the refill projection; refine with a learned ratio or flow/temp probes.
HOTWATER_DIVERSION_FRACTION = 0.15
HOTWATER_MIN_BOOST_HOURS = 1
HOTWATER_MAX_BOOST_HOURS = 2
# A "Max temp reached" only anchors the reserve when real diversion flowed in the hours
# leading up to it — the diverter reports it falsely when the element is isolated at the
# safety switch (open circuit) or on a reconnect/restart republish.
HOTWATER_FULL_WINDOW_HOURS = 2  # hours before the event to check for diversion
HOTWATER_FULL_MIN_KWH = 0.05  # min diverted kWh in that window for the full to be genuine
# A genuine full trips to max temp while power is flowing, so only a "Max temp reached" whose
# immediately-prior status was active heating anchors the reserve. A trip from an idle origin
# (Stopped, a reconnect "unavailable" republish, or a supply-dip "Paused") on a cold/isolated
# tank is a phantom. "Paused" is excluded deliberately — every observed Paused→full was false.
HOTWATER_ACTIVE_DIVERSION_STATES = ("Diverting", "Boosting")

# Staleness thresholds
STALE_POWER_SECONDS = 5 * 60  # 5 minutes
STALE_FORECAST_SECONDS = 24 * 3600  # 24 hours

# Actuation verification (anti-drain check)
VERIFY_DISCHARGE_THRESHOLD_W = 150  # below this the battery is treated as effectively idle
VERIFY_MISMATCH_SECONDS = (
    90  # a mismatch must persist this long before flagging (settle + debounce)
)
# Write-readback: setpoint registers are integers (%/W); allow float/rounding slack only.
VERIFY_READBACK_TOLERANCE = 0.5

# Diagnostic sensor states
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_ERROR = "error"

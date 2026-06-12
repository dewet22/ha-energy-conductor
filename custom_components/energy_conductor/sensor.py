"""Diagnostic sensors exposing conductor's state to HA users."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    BASELINE_IDLE_THRESHOLD_W,
    BASELINE_LOOKBACK_DAYS,
    BASELINE_PERCENTILE,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_ENERGY_SENSOR,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DAILY_ENERGY_SENSOR,
    CONF_DEVICE_NAME,
    CONF_DISPATCHING_SENSOR,
    CONF_EV_ENERGY_SENSOR,
    CONF_EV_GREEN_ENERGY_SENSOR,
    CONF_EV_MIN_ACTIVATION_W,
    CONF_EV_POWER_SENSOR,
    CONF_EXPORT_EARNINGS_SENSOR,
    CONF_EXPORT_RATE_SENSOR,
    CONF_FORECAST_SOLCAST_SENSOR,
    CONF_FORECAST_SOURCE,
    CONF_GAS_COST_SENSOR,
    CONF_GAS_ENERGY_SENSOR,
    CONF_GAS_RATE_SENSOR,
    CONF_GRID_EXPORT_ENERGY_SENSOR,
    CONF_GRID_EXPORT_SENSOR,
    CONF_GRID_IMPORT_ENERGY_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_HOME_LOAD_SENSOR,
    CONF_HOTWATER_ENERGY_SENSOR,
    CONF_HOTWATER_GREEN_SENSOR,
    CONF_IMPORT_COST_OFF_PEAK_SENSOR,
    CONF_IMPORT_COST_PEAK_SENSOR,
    CONF_IMPORT_COST_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    CONF_MANAGED_LOAD_SENSORS,
    CONF_OFF_PEAK_SENSOR,
    CONF_OVERNIGHT_WINDOW_END_TIME,
    CONF_PV_ENERGY_SENSOR,
    CONF_RESERVE_SOC_SENSOR,
    CONF_SOLAR_GENERATION_SENSOR,
    CONF_STANDING_CHARGE_ELECTRICITY_SENSOR,
    CONF_STANDING_CHARGE_GAS_SENSOR,
    CONF_SYSTEM_CAPITAL_COST,
    CONF_SYSTEM_INSTALL_DATE,
    DAILY_TARGET_LOOKBACK_DAYS,
    DAILY_TARGET_PERCENTILE,
    DEFAULT_BATTERY_MAX_POWER_W,
    DEFAULT_EV_MIN_ACTIVATION_W,
    DEFAULT_RESERVE_PERCENT,
    DOMAIN,
)
from .coordinator import EnergyConductorCoordinator
from .money import CumulativeSavings, DailyCost
from .money_tracker import (
    ACC_COUNTERFACTUAL,
    ACC_EV_COST,
    ACC_EV_SOLAR,
    ACC_HOTWATER_GAS,
    ACC_PEAK_SHIFT,
    ACC_SELF_USE,
    MoneyTracker,
)
from .overnight import project_soc


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EnergyConductorCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        StatusSensor(coordinator, entry),
        OvernightPlanSensor(coordinator, entry),
        DischargeDecisionSensor(coordinator, entry),
        BatterySocSensor(coordinator, entry),
        BatteryReservePercentSensor(coordinator, entry),
        BatteryUsableEnergySensor(coordinator, entry),
        BatteryMaxChargeSensor(coordinator, entry),
        BatteryMaxDischargeSensor(coordinator, entry),
        SolarForecastSensor(coordinator, entry),
        CheapWindowEndSensor(coordinator, entry),
        NextOffPeakWindowStartSensor(coordinator, entry),
        EVChargerPowerSensor(coordinator, entry),
        BaselineLoadSensor(coordinator, entry),
        DailyKwhTargetSensor(coordinator, entry),
        HotWaterReserveSensor(coordinator, entry),
        LastSiteStateAtSensor(coordinator, entry),
    ]
    # Money sensors: each gated on its own sources (unlike the always-registered
    # hot-water pair, these stay absent rather than sit unknown forever — the
    # ledger view drops their lines the same way).
    config = coordinator.config
    if MoneyTracker.counterfactual_enabled(config):
        entities.append(CounterfactualCostSensor(coordinator, entry))
    if MoneyTracker.ev_cost_enabled(config):
        entities.append(EVChargeCostSensor(coordinator, entry))
    if MoneyTracker.savings_enabled(config):
        entities.append(SavingsTodaySensor(coordinator, entry))
        entities.append(CumulativeSavingsSensor(coordinator, entry))
    async_add_entities(entities)


class _BaseSensor(CoordinatorEntity[EnergyConductorCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id

    @property
    def device_info(self) -> DeviceInfo:
        # CONF_DEVICE_NAME overrides the *site* part only; "Energy Conductor" is
        # always the integration prefix so entity IDs follow the pattern
        # sensor.energy_conductor_<site>_<slug> — analogous to how GivEnergy exposes
        # sensor.givenergy_inverter_<model>_<slug>.
        site = (
            self.coordinator.config.get(CONF_DEVICE_NAME)
            or self.coordinator.hass.config.location_name
            or "Home"
        )
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=f"Energy Conductor {site}",
            manufacturer="Energy Conductor",
            model="v1",
        )


# Dashboard-facing names for the configured costs entities, exposed (resolved) on the
# status sensor's `money_sources` attribute so the bundled cards can find them without
# access to the config entry. Same pattern as the per-sensor `source_entity` attributes.
_MONEY_SOURCE_KEYS: tuple[tuple[str, str], ...] = (
    ("pv", CONF_PV_ENERGY_SENSOR),
    ("house", CONF_DAILY_ENERGY_SENSOR),
    ("grid_import", CONF_GRID_IMPORT_ENERGY_SENSOR),
    ("grid_export", CONF_GRID_EXPORT_ENERGY_SENSOR),
    ("ev", CONF_EV_ENERGY_SENSOR),
    ("ev_green", CONF_EV_GREEN_ENERGY_SENSOR),
    ("gas", CONF_GAS_ENERGY_SENSOR),
    ("battery_discharge", CONF_BATTERY_DISCHARGE_ENERGY_SENSOR),
    ("import_cost", CONF_IMPORT_COST_SENSOR),
    ("import_cost_off_peak", CONF_IMPORT_COST_OFF_PEAK_SENSOR),
    ("import_cost_peak", CONF_IMPORT_COST_PEAK_SENSOR),
    ("export_earnings", CONF_EXPORT_EARNINGS_SENSOR),
    ("standing_charge_electricity", CONF_STANDING_CHARGE_ELECTRICITY_SENSOR),
    ("standing_charge_gas", CONF_STANDING_CHARGE_GAS_SENSOR),
    ("gas_cost", CONF_GAS_COST_SENSOR),
    ("import_rate", CONF_IMPORT_RATE_SENSOR),
    ("export_rate", CONF_EXPORT_RATE_SENSOR),
    ("gas_rate", CONF_GAS_RATE_SENSOR),
)


# Feeds for the mission tape, exposed (resolved) on the status sensor alongside
# money_sources. The off-peak sensor is required config, so the map always exists.
_TAPE_SOURCE_KEYS: tuple[tuple[str, str], ...] = (
    ("solar_power", CONF_SOLAR_GENERATION_SENSOR),
    ("solar_forecast", CONF_FORECAST_SOLCAST_SENSOR),
    ("home_load", CONF_HOME_LOAD_SENSOR),
    ("off_peak", CONF_OFF_PEAK_SENSOR),
    ("dispatching", CONF_DISPATCHING_SENSOR),
    ("grid_import_w", CONF_GRID_IMPORT_SENSOR),
    ("grid_export_w", CONF_GRID_EXPORT_SENSOR),
)


def _tape_sources(config: dict[str, Any]) -> dict[str, str] | None:
    sources = {name: config[key] for name, key in _TAPE_SOURCE_KEYS if config.get(key)}
    return sources or None


def _money_sources(config: dict[str, Any]) -> dict[str, str] | None:
    sources = {name: config[key] for name, key in _MONEY_SOURCE_KEYS if config.get(key)}
    # Hot water heating displaces gas whether diverted or boosted: prefer the
    # total-in counter, fall back to the green/diverted one.
    hot_water = config.get(CONF_HOTWATER_ENERGY_SENSOR) or config.get(CONF_HOTWATER_GREEN_SENSOR)
    if hot_water:
        sources["hot_water"] = hot_water
    return sources or None


class StatusSensor(_BaseSensor):
    _attr_translation_key = "status"
    _attr_name = "Status"

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-status"

    @property
    def native_value(self) -> str:
        return self.coordinator.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.last_site_state
        grid = state.grid if state is not None else None
        return {
            "last_error": self.coordinator.last_error,
            "degraded_since": self.coordinator.degraded_since,
            "ticks_total": self.coordinator.ticks_total,
            "write_mode": self.coordinator.write_mode,
            "writes_sent": self.coordinator.writes_sent,
            "write_failures": self.coordinator.write_failures,
            "last_write_at": self.coordinator.last_write_at,
            "last_write_outcome": self.coordinator.last_write_outcome,
            "last_write_error": self.coordinator.last_write_error,
            "notifications_sent": self.coordinator.notifications_sent,
            "notify_failures": self.coordinator.notify_failures,
            "last_notify_error": self.coordinator.last_notify_error,
            # Meter view + actuation verification (None when the grid sensors aren't configured).
            "grid_import_w": grid.import_w if grid is not None else None,
            "grid_export_w": grid.export_w if grid is not None else None,
            "verification": self.coordinator.verification_status,
            "verification_detail": self.coordinator.last_verification_detail,
            "money_sources": _money_sources(self.coordinator.config),
            "tape_sources": _tape_sources(self.coordinator.config),
        }


class OvernightPlanSensor(_BaseSensor):
    _attr_translation_key = "overnight_plan"
    _attr_name = "Overnight plan target"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-overnight-plan"

    @property
    def native_value(self) -> int | None:
        plan = self.coordinator.last_overnight_plan
        return None if plan is None else plan.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        plan = self.coordinator.last_overnight_plan
        if plan is None:
            return {}
        attrs: dict[str, Any] = {
            "reason": plan.reason,
            "dedupe_key": plan.dedupe_key,
            "outcome": self.coordinator.last_overnight_outcome,
            "write_mode": self.coordinator.write_mode,
        }
        # SoC projection for the mission tape: served from EC's own plan model so
        # the dashboard never re-derives it client-side. Unmistakably a projection.
        state = self.coordinator.last_site_state
        if state is not None:
            attrs["soc_projection"] = [
                {"t": t.isoformat(), "soc": soc}
                for t, soc in project_soc(state, target_percent=float(plan.value))
            ]
        return attrs


class DischargeDecisionSensor(_BaseSensor):
    _attr_translation_key = "discharge_decision"
    _attr_name = "Discharge decision"
    _attr_native_unit_of_measurement = "W"

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-discharge-decision"

    @property
    def native_value(self) -> int | None:
        d = self.coordinator.last_discharge_decision
        return None if d is None else d.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.last_discharge_decision
        if d is None:
            return {}
        return {
            "reason": d.reason,
            "dedupe_key": d.dedupe_key,
            "outcome": self.coordinator.last_discharge_outcome,
            "write_mode": self.coordinator.write_mode,
        }


class BatterySocSensor(_BaseSensor):
    _attr_translation_key = "battery_soc"
    _attr_name = "Battery SoC"
    _attr_native_unit_of_measurement = PERCENTAGE
    # No device_class: SensorDeviceClass.BATTERY is for *device* batteries (remotes,
    # sensors) and triggers HA's low-battery scanners. House storage SoC is not that.
    _attr_icon = "mdi:battery-high"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-battery-soc"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.battery.soc_percent

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"source_entity": self.coordinator.config.get(CONF_BATTERY_SOC_SENSOR)}


class BatteryReservePercentSensor(_BaseSensor):
    _attr_translation_key = "battery_reserve"
    _attr_name = "Battery reserve"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery-low"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-battery-reserve"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.battery.reserve_percent

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        config = self.coordinator.config
        reserve_sensor = config.get(CONF_RESERVE_SOC_SENSOR)
        return {
            "source": "sensor" if reserve_sensor else "config",
            "source_entity": reserve_sensor,
            "configured_value_pct": config.get(
                CONF_BATTERY_RESERVE_PERCENT, DEFAULT_RESERVE_PERCENT
            ),
        }


class BatteryUsableEnergySensor(_BaseSensor):
    _attr_translation_key = "battery_usable_energy"
    _attr_name = "Battery usable energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    # Point-in-time stored energy (not cumulative). ENERGY device class requires
    # total/total_increasing state_class; omit it so MEASUREMENT stays valid.
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-battery-usable-energy"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        if state is None:
            return None
        battery = state.battery
        usable_percent = max(0.0, battery.soc_percent - battery.reserve_percent)
        return round(battery.capacity_kwh * usable_percent / 100, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.last_site_state
        if state is None:
            return {}
        b = state.battery
        return {
            "soc_pct": b.soc_percent,
            "reserve_pct": b.reserve_percent,
            "capacity_kwh": b.capacity_kwh,
            "calculation": (
                f"max(0, {b.soc_percent:.1f} - {b.reserve_percent:.1f})% x {b.capacity_kwh} kWh"
            ),
        }


class BatteryMaxChargeSensor(_BaseSensor):
    _attr_translation_key = "battery_max_charge"
    _attr_name = "Battery charge power limit"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-battery-max-charge"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.battery.max_charge_power_w

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source_entity": self.coordinator.config.get(CONF_BATTERY_CHARGE_CONTROL),
            "default_w": DEFAULT_BATTERY_MAX_POWER_W,
        }


class BatteryMaxDischargeSensor(_BaseSensor):
    _attr_translation_key = "battery_max_discharge"
    _attr_name = "Battery discharge power limit"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-battery-max-discharge"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.battery.max_discharge_power_w

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source_entity": self.coordinator.config.get(CONF_BATTERY_DISCHARGE_LIMIT),
            "default_w": DEFAULT_BATTERY_MAX_POWER_W,
        }


class SolarForecastSensor(_BaseSensor):
    _attr_translation_key = "solar_forecast"
    _attr_name = "Solar forecast tomorrow"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    # Daily forecast total is a point-in-time prediction (changes as the forecast
    # model updates), not a cumulative meter — MEASUREMENT.  ENERGY device class
    # requires total/total_increasing state_class; omit it so MEASUREMENT stays valid.
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-solar-forecast-today"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        return None if state is None else round(state.solar_forecast.total_kwh_forecast, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.last_site_state
        if state is None:
            return {}
        forecast = state.solar_forecast
        if forecast.slots:
            # Slot-based: source comes from config (solcast vs daily_total_sensor)
            source = self.coordinator.config.get(CONF_FORECAST_SOURCE, "unknown")
        else:
            # Fallback: source is whichever fallback path produced the value
            source = forecast.fallback_source or "unknown"
        attrs: dict[str, Any] = {
            "slot_count": len(forecast.slots),
            "source": source,
            "fallback_source": forecast.fallback_source,
            "planning_horizon": "tomorrow" if forecast.slots else "estimate",
        }
        if source == "daily_sensor":
            attrs["planning_note"] = (
                "daily_total_sensor reflects today's actual generation used as a "
                "proxy for tomorrow's. For overnight planning Solcast is more accurate."
            )
        return attrs


class CheapWindowEndSensor(_BaseSensor):
    _attr_translation_key = "off_peak_window_end"
    _attr_name = "Overnight off-peak tariff end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-cheap-window-end"

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.tariff.off_peak_window_end

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        config = self.coordinator.config
        return {
            "configured_end_time": config.get(CONF_OVERNIGHT_WINDOW_END_TIME),
            "source_sensor": config.get(CONF_OFF_PEAK_SENSOR),
            "note": (
                "Planning boundary for overnight charge decisions. "
                "Prefers next_end/current_end attributes on the off-peak sensor; "
                "falls back to the configured HH:MM time."
            ),
        }


class NextOffPeakWindowStartSensor(_BaseSensor):
    _attr_translation_key = "off_peak_window_start"
    _attr_name = "Next off-peak window start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-off-peak-window-start"

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.tariff.next_off_peak_window_start

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        config = self.coordinator.config
        return {
            "source_sensor": config.get(CONF_OFF_PEAK_SENSOR),
        }


class EVChargerPowerSensor(_BaseSensor):
    _attr_translation_key = "ev_charger_power"
    _attr_name = "EV charger power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-ev-charger-power"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        if state is None or state.ev_charger is None:
            return None
        return state.ev_charger.power_w

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        config = self.coordinator.config
        state = self.coordinator.last_site_state
        min_w = int(config.get(CONF_EV_MIN_ACTIVATION_W, DEFAULT_EV_MIN_ACTIVATION_W))
        attrs: dict[str, Any] = {
            "source_entity": config.get(CONF_EV_POWER_SENSOR),
            "min_activation_w": min_w,
        }
        if state is not None and state.ev_charger is not None:
            attrs["active"] = state.ev_charger.power_w >= min_w
        return attrs


class BaselineLoadSensor(_BaseSensor):
    _attr_translation_key = "baseline_load"
    _attr_name = "Calculated baseline load"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-baseline-load"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        return None if state is None else round(state.baseline_load_w, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.last_site_state
        config = self.coordinator.config
        attrs: dict[str, Any] = {
            "source": state.baseline_source if state is not None else None,
            "home_load_sensor": config.get(CONF_HOME_LOAD_SENSOR),
            "managed_load_sensors": config.get(CONF_MANAGED_LOAD_SENSORS) or [],
            "lookback_days": BASELINE_LOOKBACK_DAYS,
            "percentile": f"p{int(BASELINE_PERCENTILE * 100)}",
            "idle_threshold_w": int(BASELINE_IDLE_THRESHOLD_W),
        }
        if state is not None and state.baseline_qualifying_buckets is not None:
            attrs["qualifying_buckets"] = state.baseline_qualifying_buckets
        return attrs


class DailyKwhTargetSensor(_BaseSensor):
    _attr_translation_key = "daily_kwh_target"
    _attr_name = "Calculated daily target"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-daily-kwh-target"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        return None if state is None else round(state.daily_kwh_target, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.last_site_state
        config = self.coordinator.config
        attrs: dict[str, Any] = {
            "source": state.daily_kwh_target_source if state is not None else None,
            "daily_energy_sensor": config.get(CONF_DAILY_ENERGY_SENSOR),
            "lookback_days": DAILY_TARGET_LOOKBACK_DAYS,
            "percentile": f"p{int(DAILY_TARGET_PERCENTILE * 100)}",
        }
        if state is not None and state.daily_kwh_target_qualifying_days is not None:
            attrs["qualifying_days"] = state.daily_kwh_target_qualifying_days
        return attrs


class HotWaterReserveSensor(_BaseSensor):
    _attr_translation_key = "hot_water_reserve"
    _attr_name = "Hot water reserve"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:water-boiler"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-hot-water-reserve"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        if state is None or state.hot_water is None:
            return None
        return state.hot_water.reserve_percent

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.last_site_state
        if state is None or state.hot_water is None:
            return {}
        hw = state.hot_water
        return {
            "reserve_kwh": hw.reserve_kwh,
            "capacity_kwh": hw.capacity_kwh,
            "last_full_at": hw.last_full_at.isoformat() if hw.last_full_at else None,
            "depletion_kwh_per_day": hw.depletion_kwh_per_day,
            "depletion_source": hw.depletion_source,
            "boost_recommended": hw.boost_recommended,
            "suggested_boost_hours": hw.suggested_boost_hours,
        }


class _MoneySensorBase(_BaseSensor):
    """Shared shape for the GBP sensors. TOTAL + midnight last_reset puts the daily
    figures into long-term statistics, so month-to-date falls out of LTS for free."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    @property
    def _money(self) -> MoneyTracker | None:
        return self.coordinator.money


class _DailyMoneySensor(_MoneySensorBase, RestoreEntity):
    """A today-cost accumulator sensor backed by one MoneyTracker daily accumulator.

    RestoreEntity carries the running total across restarts: the restored value and
    its source-counter baseline are seeded back into the tracker (a restore from a
    previous day is discarded there — today starts at zero).
    """

    _acc: str  # MoneyTracker accumulator name; set by subclasses

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or self._money is None or last.state in ("unavailable", "unknown"):
            return
        try:
            restored = DailyCost(
                day=date.fromisoformat(last.attributes["day"]),
                last_counter_kwh=float(last.attributes["source_counter_kwh"]),
                cost_gbp=float(last.state),
            )
        except KeyError, TypeError, ValueError:
            return
        self._money.seed_daily(self._acc, restored)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # Unavailable during a tariff outage rather than freezing silently: the
        # accumulator holds underneath and resumes when the rate returns.
        if self._money is None or self._money.daily.get(self._acc) is None:
            return False
        return super().available and self._money.rate_available

    @property
    def native_value(self) -> float | None:
        return None if self._money is None else self._money.daily_cost(self._acc)

    @property
    def last_reset(self) -> datetime | None:
        acc = self._money.daily.get(self._acc) if self._money is not None else None
        if acc is None:
            return None
        return dt_util.start_of_local_day(datetime.combine(acc.day, time()))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        acc = self._money.daily.get(self._acc) if self._money is not None else None
        if acc is None:
            return {"modelled": True}
        return {
            "modelled": True,
            "day": acc.day.isoformat(),
            "source_counter_kwh": acc.last_counter_kwh,
        }


class CounterfactualCostSensor(_DailyMoneySensor):
    _attr_translation_key = "counterfactual_cost"
    _attr_name = "Counterfactual cost today"
    _acc = ACC_COUNTERFACTUAL

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-counterfactual-cost-today"


class EVChargeCostSensor(_DailyMoneySensor):
    _attr_translation_key = "ev_charge_cost"
    _attr_name = "EV charge cost today"
    _acc = ACC_EV_COST

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-ev-charge-cost-today"


class SavingsTodaySensor(_MoneySensorBase):
    """Modelled savings vs the no-PV/no-battery counterfactual.

    No restore needed: the headline recomputes from the counterfactual sensor (which
    restores) and the billing-grade read-throughs. Only the per-line breakdown
    attributes lose intraday history across a restart.
    """

    _attr_translation_key = "savings_today"
    _attr_name = "Savings today"

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-savings-today"

    @property
    def available(self) -> bool:
        return super().available and self._money is not None and self._money.rate_available

    @property
    def native_value(self) -> float | None:
        return None if self._money is None else self._money.savings_today

    @property
    def last_reset(self) -> datetime | None:
        return dt_util.start_of_local_day()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        money = self._money
        if money is None:
            return {"modelled": True}
        return {
            "modelled": True,
            "solar_self_use_gbp": money.daily_cost(ACC_SELF_USE),
            "battery_peak_shift_gbp": money.daily_cost(ACC_PEAK_SHIFT),
            "hot_water_gas_displacement_gbp": money.daily_cost(ACC_HOTWATER_GAS),
            "ev_solar_charge_gbp": money.daily_cost(ACC_EV_SOLAR),
        }


class CumulativeSavingsSensor(_MoneySensorBase, RestoreEntity):
    """Lifetime modelled savings — the payback tracker's source of truth."""

    _attr_translation_key = "cumulative_savings"
    _attr_name = "Cumulative savings"

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-cumulative-savings"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is None or self._money is None or last.state in ("unavailable", "unknown"):
            return
        try:
            restored = CumulativeSavings(
                day=date.fromisoformat(last.attributes["day"]),
                started=date.fromisoformat(last.attributes["started"]),
                base_gbp=float(last.attributes["banked_gbp"]),
                today_gbp=float(last.attributes["today_gbp"]),
            )
        except KeyError, TypeError, ValueError:
            return
        self._money.seed_cumulative(restored)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._money is not None
            and self._money.rate_available
            and self._money.cumulative is not None
        )

    @property
    def native_value(self) -> float | None:
        money = self._money
        if money is None or money.cumulative is None:
            return None
        return money.cumulative.total_gbp

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        money = self._money
        attrs: dict[str, Any] = {
            "modelled": True,
            "capital_cost_gbp": self.coordinator.config.get(CONF_SYSTEM_CAPITAL_COST),
            "install_date": self.coordinator.config.get(CONF_SYSTEM_INSTALL_DATE),
        }
        if money is None or money.cumulative is None:
            return attrs
        cumulative = money.cumulative
        attrs.update(
            {
                "day": cumulative.day.isoformat(),
                "started": cumulative.started.isoformat(),
                "banked_gbp": cumulative.base_gbp,
                "today_gbp": cumulative.today_gbp,
            }
        )
        payback = money.payback(cumulative.day)
        attrs.update(
            {
                "recovered_pct": None if payback is None else round(payback.recovered_pct, 2),
                "run_rate_gbp_per_year": (
                    None if payback is None else round(payback.run_rate_gbp_per_year, 2)
                ),
                "projected_breakeven": (
                    payback.projected_breakeven.isoformat()
                    if payback is not None and payback.projected_breakeven is not None
                    else None
                ),
            }
        )
        return attrs


class LastSiteStateAtSensor(_BaseSensor):
    _attr_translation_key = "last_state_read_at"
    _attr_name = "Last state read at"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Disabled by default: updates every coordinator tick (30 s), creating ~2 880
    # recorder entries/day. Enable manually for debugging tick-timing issues only.
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-last-state-read-at"

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.now

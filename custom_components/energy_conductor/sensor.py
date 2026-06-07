"""Diagnostic sensors exposing conductor's state to HA users."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BASELINE_IDLE_THRESHOLD_W,
    BASELINE_LOOKBACK_DAYS,
    BASELINE_PERCENTILE,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DAILY_ENERGY_SENSOR,
    CONF_DEVICE_NAME,
    CONF_EV_MIN_ACTIVATION_W,
    CONF_EV_POWER_SENSOR,
    CONF_FORECAST_SOURCE,
    CONF_HOME_LOAD_SENSOR,
    CONF_MANAGED_LOAD_SENSORS,
    CONF_OFF_PEAK_SENSOR,
    CONF_OVERNIGHT_WINDOW_END_TIME,
    CONF_RESERVE_SOC_SENSOR,
    DAILY_TARGET_LOOKBACK_DAYS,
    DAILY_TARGET_PERCENTILE,
    DEFAULT_BATTERY_MAX_POWER_W,
    DEFAULT_EV_MIN_ACTIVATION_W,
    DEFAULT_RESERVE_PERCENT,
    DOMAIN,
    PRE_OFF_PEAK_HOLD_MINUTES,
)
from .coordinator import EnergyConductorCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EnergyConductorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
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
            LastSiteStateAtSensor(coordinator, entry),
        ]
    )


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
        return {
            "last_error": self.coordinator.last_error,
            "ticks_total": self.coordinator.ticks_total,
            "notifications_sent": self.coordinator.notifications_sent,
            "notify_failures": self.coordinator.notify_failures,
            "last_notify_error": self.coordinator.last_notify_error,
        }


class OvernightPlanSensor(_BaseSensor):
    _attr_translation_key = "overnight_plan"
    _attr_name = "Overnight plan target"
    _attr_native_unit_of_measurement = "%"

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
        return {"reason": plan.reason, "dedupe_key": plan.dedupe_key}


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
        return {"reason": d.reason, "dedupe_key": d.dedupe_key}


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
            "hold_minutes": PRE_OFF_PEAK_HOLD_MINUTES,
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

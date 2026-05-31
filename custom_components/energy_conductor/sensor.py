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

from .const import CONF_DEVICE_NAME, CONF_FORECAST_SOURCE, DOMAIN
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
            EVChargerPowerSensor(coordinator, entry),
            BaselineLoadSensor(coordinator, entry),
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
        device_name = (
            self.coordinator.config.get(CONF_DEVICE_NAME)
            or self.coordinator.hass.config.location_name
            or "Energy Conductor"
        )
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=device_name,
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


class BatteryMaxChargeSensor(_BaseSensor):
    _attr_translation_key = "battery_max_charge"
    _attr_name = "Battery max charge"
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


class BatteryMaxDischargeSensor(_BaseSensor):
    _attr_translation_key = "battery_max_discharge"
    _attr_name = "Battery max discharge"
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


class SolarForecastSensor(_BaseSensor):
    _attr_translation_key = "solar_forecast"
    _attr_name = "Solar forecast today"
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
        return None if state is None else round(state.solar_forecast.total_kwh_today, 2)

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
        return {
            "slot_count": len(forecast.slots),
            "source": source,
            "fallback_source": forecast.fallback_source,
        }


class CheapWindowEndSensor(_BaseSensor):
    _attr_translation_key = "cheap_window_end"
    _attr_name = "Cheap window end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-cheap-window-end"

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.tariff.cheap_window_end


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


class BaselineLoadSensor(_BaseSensor):
    _attr_translation_key = "baseline_load"
    _attr_name = "Baseline load"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-baseline-load"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.baseline_load_w

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.last_site_state
        return {} if state is None else {"source": state.baseline_source}


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

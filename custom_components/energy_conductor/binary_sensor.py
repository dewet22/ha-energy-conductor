"""Binary sensor platform — tariff state flags."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_NAME, CONF_DISPATCHING_SENSOR, CONF_OFF_PEAK_SENSOR, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EnergyConductorCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EnergyConductorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TariffCheapNowBinarySensor(coordinator, entry),
            TariffDispatchingNowBinarySensor(coordinator, entry),
            HotWaterBoostRecommendedBinarySensor(coordinator, entry),
            ActuationMismatchBinarySensor(coordinator, entry),
        ]
    )


class _BaseBinarySensor(CoordinatorEntity["EnergyConductorCoordinator"], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id

    @property
    def device_info(self) -> DeviceInfo:
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


class TariffCheapNowBinarySensor(_BaseBinarySensor):
    _attr_translation_key = "tariff_off_peak_now"
    _attr_name = "Off-peak rate sensor active"
    # No device_class — HA's binary-sensor classes (POWER, RUNNING, PLUG, …)
    # all describe physical states and would be misleading for a tariff signal.
    # Note: this mirrors the off-peak rate sensor broadly (overnight tariff AND any
    # OI dispatch slots). See "Overnight off-peak tariff end" for the fixed overnight
    # window boundary used by the SoC projection, which is independent of why this
    # sensor is currently active.

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-tariff-cheap-now"

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.tariff.off_peak_now

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "monitored_sensor": self.coordinator.config.get(CONF_OFF_PEAK_SENSOR),
            "note": (
                "True whenever the monitored sensor is on — covers both the overnight "
                "off-peak rate window and intra-day dispatch slots (e.g. Octopus Intelligent). "
                "Battery discharge is blocked to 0 W whenever this is active."
            ),
        }


class HotWaterBoostRecommendedBinarySensor(_BaseBinarySensor):
    _attr_translation_key = "hot_water_boost_recommended"
    _attr_name = "Hot water boost recommended"
    _attr_icon = "mdi:water-boiler-alert"

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-hot-water-boost-recommended"

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.last_site_state
        if state is None or state.hot_water is None:
            return None
        return state.hot_water.boost_recommended

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.last_site_state
        if state is None or state.hot_water is None:
            return {}
        hw = state.hot_water
        return {
            "suggested_boost_hours": hw.suggested_boost_hours,
            "reserve_percent": hw.reserve_percent,
        }


class ActuationMismatchBinarySensor(_BaseBinarySensor):
    _attr_translation_key = "actuation_mismatch"
    _attr_name = "Actuation mismatch"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-actuation-mismatch"

    @property
    def is_on(self) -> bool:
        # PROBLEM device_class: on = a confirmed, persisted actuation mismatch (live mode only).
        return self.coordinator.verification_status == "mismatch"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "status": self.coordinator.verification_status,
            "detail": self.coordinator.last_verification_detail,
            "last_verified_at": self.coordinator.last_verification_at,
        }


class TariffDispatchingNowBinarySensor(_BaseBinarySensor):
    _attr_translation_key = "tariff_dispatching_now"
    _attr_name = "EV dispatching now"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-tariff-dispatching-now"

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.tariff.ev_dispatching_now

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        monitored = self.coordinator.config.get(CONF_DISPATCHING_SENSOR)
        return {
            "monitored_sensor": monitored,
            "note": (
                "True when the optional EV-dispatch sensor is on (e.g. Octopus Intelligent "
                "smart-charge active). Diagnostic only: the discharge guard keys off the "
                "off-peak rate signal, which already covers dispatch windows, so this no "
                "longer affects the battery discharge decision."
            )
            if monitored
            else "No EV dispatch sensor configured; always False.",
        }

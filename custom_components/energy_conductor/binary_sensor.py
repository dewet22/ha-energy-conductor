"""Binary sensor platform — tariff state flags."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_NAME, DOMAIN

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


class TariffCheapNowBinarySensor(_BaseBinarySensor):
    _attr_translation_key = "tariff_cheap_now"
    _attr_name = "Cheap window now"
    # No device_class — HA's binary-sensor classes (POWER, RUNNING, PLUG, …)
    # all describe physical states and would be misleading for a tariff signal.

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-tariff-cheap-now"

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.tariff.cheap_window_now


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

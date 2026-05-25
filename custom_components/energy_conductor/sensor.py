"""Diagnostic sensors exposing conductor's state to HA users."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
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
        ]
    )


class _BaseSensor(CoordinatorEntity[EnergyConductorCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id


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

"""Energy Conductor integration for Home Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from homeassistant.const import Platform

    from .coordinator import EnergyConductorCoordinator

    coordinator = EnergyConductorCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    try:
        await coordinator.async_config_entry_first_refresh()
        await coordinator.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
    except Exception:
        await coordinator.async_stop()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from homeassistant.const import Platform

    from .coordinator import EnergyConductorCoordinator

    coordinator: EnergyConductorCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR])
    if unloaded:
        if coordinator is not None:
            await coordinator.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)

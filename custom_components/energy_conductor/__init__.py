"""Energy Conductor integration for Home Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import _LEGACY_CONF_CHEAP_RATE_SENSOR, CONF_OFF_PEAK_SENSOR, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS = ["binary_sensor", "sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import EnergyConductorCoordinator

    coordinator = EnergyConductorCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    try:
        await coordinator.async_config_entry_first_refresh()
        await coordinator.async_start()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await coordinator.async_stop()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import EnergyConductorCoordinator

    coordinator: EnergyConductorCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        if coordinator is not None:
            await coordinator.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries from older versions."""
    if entry.version == 1:
        # v1 → v2: "cheap_rate_sensor" key renamed to "off_peak_sensor".
        new_data = {**entry.data}
        if _LEGACY_CONF_CHEAP_RATE_SENSOR in new_data:
            new_data[CONF_OFF_PEAK_SENSOR] = new_data.pop(_LEGACY_CONF_CHEAP_RATE_SENSOR)
        hass.config_entries.async_update_entry(entry, data=new_data, version=2)
    return True

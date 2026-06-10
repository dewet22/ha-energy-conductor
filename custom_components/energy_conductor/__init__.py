"""Energy Conductor integration for Home Assistant."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .const import (
    _LEGACY_CONF_CHEAP_RATE_SENSOR,
    CONF_ENTITY_REFS,
    CONF_OFF_PEAK_SENSOR,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["binary_sensor", "sensor"]

# Bundled dashboard-strategy frontend module. Bump _STRATEGY_VERSION on any
# change to ec-strategy.js so the browser cache doesn't serve a stale copy.
_STRATEGY_FILENAME = "ec-strategy.js"
_STRATEGY_URL = f"/{DOMAIN}/{_STRATEGY_FILENAME}"
_STRATEGY_VERSION = "6"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the bundled dashboard-strategy frontend module.

    Component scope, so the static-path registration happens exactly once
    regardless of how many config entries exist.
    """
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    if hass.http is None:
        # No web server (e.g. the test harness) — nothing to serve from anyway.
        return True
    try:
        module_path = Path(__file__).parent / "www" / _STRATEGY_FILENAME
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_STRATEGY_URL, str(module_path), False)]
        )
        add_extra_js_url(hass, f"{_STRATEGY_URL}?v={_STRATEGY_VERSION}")
    except Exception as exc:
        # The bundled module is cosmetic (dashboard frontend) — a failure here
        # must never take down the integration. Log and carry on.
        _LOGGER.warning("Could not register the dashboard strategy module: %s", exc)
    return True


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
    """Migrate config entries from older versions.

    Chained (not elif) so a v1 entry flows 1→2→3 in a single pass.
    """
    if entry.version == 1:
        # v1 → v2: "cheap_rate_sensor" key renamed to "off_peak_sensor".
        new_data = {**entry.data}
        if _LEGACY_CONF_CHEAP_RATE_SENSOR in new_data:
            new_data[CONF_OFF_PEAK_SENSOR] = new_data.pop(_LEGACY_CONF_CHEAP_RATE_SENSOR)
        hass.config_entries.async_update_entry(entry, data=new_data, version=2)
    if entry.version == 2:
        # v2 → v3: backfill {platform, unique_id} anchors for every referenced entity so
        # later area-prefix renames resolve via unique_id (see entity_ref.py). Entities not
        # yet in the registry at migrate time are simply skipped; they get anchored on the
        # next options save and fall back to entity_id meanwhile.
        from .entity_ref import capture_all

        new_data = {**entry.data}
        new_data[CONF_ENTITY_REFS] = capture_all(hass, new_data)
        hass.config_entries.async_update_entry(entry, data=new_data, version=3)
    return True

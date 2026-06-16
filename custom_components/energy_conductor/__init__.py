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

# Bundled dashboard frontend modules (the strategy + the custom cards). Bump
# _STRATEGY_VERSION on any change to any bundled JS so neither the browser
# cache nor the Lovelace resource entry serves a stale copy.
_STRATEGY_FILENAME = "ec-strategy.js"
_LONGTERM_FILENAME = "ec-longterm.js"
_TAPE_FILENAME = "ec-tape.js"
_LEDGER_FILENAME = "ec-ledger.js"
_STRATEGY_URL = f"/{DOMAIN}/{_STRATEGY_FILENAME}"
_LONGTERM_URL = f"/{DOMAIN}/{_LONGTERM_FILENAME}"
_TAPE_URL = f"/{DOMAIN}/{_TAPE_FILENAME}"
_LEDGER_URL = f"/{DOMAIN}/{_LEDGER_FILENAME}"
_MODULE_URLS = (_STRATEGY_URL, _LONGTERM_URL, _TAPE_URL, _LEDGER_URL)
_STRATEGY_VERSION = "21"


async def _async_register_lovelace_resources(hass: HomeAssistant) -> None:
    """Ensure the bundled modules are registered as Lovelace *resources*.

    ``add_extra_js_url`` modules are fire-and-forget: the frontend renders
    dashboards without awaiting them, so a panel view referencing a custom card
    from a module still in flight renders a permanent "Configuration error"
    (upstream frontend#52570; mechanism proven live in givenergy-hass). Lovelace
    resources, by contrast, are awaited before any dashboard renders. Storage
    mode only — a YAML-mode resource list is user-managed, so it is left alone.
    """
    try:
        lovelace = hass.data.get("lovelace")
        resources = getattr(lovelace, "resources", None)
        if resources is None or not hasattr(resources, "async_create_item"):
            return  # lovelace absent, or YAML mode: nothing to manage here
        if not resources.loaded:
            await resources.async_load()

        wanted = {
            url: {"res_type": "module", "url": f"{url}?v={_STRATEGY_VERSION}"}
            for url in _MODULE_URLS
        }
        for item in list(resources.async_items()):
            item_url = item.get("url") if isinstance(item, dict) else item.url
            base = str(item_url or "").split("?", 1)[0]
            target = wanted.pop(base, None)
            if target is None:
                continue
            if item_url != target["url"]:
                item_id = item.get("id") if isinstance(item, dict) else item.id
                await resources.async_update_item(item_id, target)
        for target in wanted.values():
            await resources.async_create_item(target)
    except Exception as exc:
        # Cosmetic feature — must never break setup.
        _LOGGER.warning("Could not register Lovelace resources: %s", exc)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the bundled dashboard frontend modules.

    Component scope, so the static-path registration happens exactly once
    regardless of how many config entries exist.
    """
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState

    if hass.http is None:
        # No web server (e.g. the test harness) — nothing to serve from anyway.
        return True
    try:
        www = Path(__file__).parent / "www"
        # cache_headers=True: serve the modules with a long Cache-Control so the browser
        # serves them from cache on reload instead of re-fetching every time. An uncached
        # re-fetch queues behind the browser's ~6-connection-per-origin limit and can lose
        # the frontend's fixed 5s custom-strategy registration race (get-strategy.ts
        # MAX_WAIT_STRATEGY_LOAD) -> "Timeout waiting for strategy element" (#27). The
        # ?v=_STRATEGY_VERSION stamp busts the cache on any JS change, so this is safe.
        await hass.http.async_register_static_paths(
            [StaticPathConfig(url, str(www / url.rsplit("/", 1)[1]), True) for url in _MODULE_URLS]
        )
        for url in _MODULE_URLS:
            add_extra_js_url(hass, f"{url}?v={_STRATEGY_VERSION}")
    except Exception as exc:
        # The bundled modules are cosmetic (dashboard frontend) — a failure here
        # must never take down the integration. Log and carry on.
        _LOGGER.warning("Could not register the dashboard modules: %s", exc)

    # Lovelace sets up its resource store during bootstrap; register ours once
    # HA has fully started (immediately when this is a reload of a running HA).
    async def _register_resources(_event: object = None) -> None:
        await _async_register_lovelace_resources(hass)

    if hass.state is CoreState.running:
        await _register_resources()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register_resources)
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

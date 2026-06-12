"""Resilience to entity_id renames.

Energy Conductor references entities owned by *other* integrations (GivEnergy, Octopus,
Solcast, myenergi, …) by entity_id. HA core #170560 ("Prefix area to entity ID", 2026.6)
gives newly-created entities an area-name prefix, so re-adding an integration or assigning a
device to an area effectively delete+recreates its entities: new entity_id, **same
unique_id**. Anchoring on the raw entity_id therefore breaks silently.

This module captures each reference's stable ``{platform, unique_id}`` (alongside the
entity_id, which is kept as a fallback) and resolves it back to the *current* entity_id at
runtime. Resolution happens once per coordinator build, so the adapter and the coordinator
watch-list keep reading plain entity_id strings unchanged.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_ENERGY_SENSOR,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DAILY_ENERGY_SENSOR,
    CONF_DISPATCHING_SENSOR,
    CONF_ENTITY_REFS,
    CONF_EV_ENERGY_SENSOR,
    CONF_EV_GREEN_ENERGY_SENSOR,
    CONF_EV_POWER_SENSOR,
    CONF_EXPORT_EARNINGS_SENSOR,
    CONF_EXPORT_RATE_SENSOR,
    CONF_FORECAST_DAILY_SENSOR,
    CONF_FORECAST_SOLCAST_SENSOR,
    CONF_FORECAST_SOLCAST_TODAY_SENSOR,
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
    CONF_HOTWATER_STATUS_SENSOR,
    CONF_IMPORT_COST_OFF_PEAK_SENSOR,
    CONF_IMPORT_COST_PEAK_SENSOR,
    CONF_IMPORT_COST_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    CONF_MANAGED_LOAD_SENSORS,
    CONF_NOTIFY_TARGET,
    CONF_OFF_PEAK_SENSOR,
    CONF_PV_ENERGY_SENSOR,
    CONF_RESERVE_SOC_SENSOR,
    CONF_SOLAR_GENERATION_SENSOR,
    CONF_SOLAR_POWER_SENSOR,
    CONF_STANDING_CHARGE_ELECTRICITY_SENSOR,
    CONF_STANDING_CHARGE_GAS_SENSOR,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# Scalar (single entity_id string) reference keys.
SCALAR_ENTITY_CONF_KEYS: frozenset[str] = frozenset(
    {
        CONF_BATTERY_SOC_SENSOR,
        CONF_BATTERY_CHARGE_CONTROL,
        CONF_BATTERY_DISCHARGE_LIMIT,
        CONF_BATTERY_POWER_SENSOR,
        CONF_GRID_IMPORT_SENSOR,
        CONF_GRID_EXPORT_SENSOR,
        CONF_RESERVE_SOC_SENSOR,
        CONF_OFF_PEAK_SENSOR,
        CONF_DISPATCHING_SENSOR,
        CONF_FORECAST_SOLCAST_SENSOR,
        CONF_FORECAST_SOLCAST_TODAY_SENSOR,
        CONF_FORECAST_DAILY_SENSOR,
        CONF_SOLAR_GENERATION_SENSOR,
        CONF_SOLAR_POWER_SENSOR,
        CONF_EV_POWER_SENSOR,
        CONF_HOME_LOAD_SENSOR,
        CONF_DAILY_ENERGY_SENSOR,
        CONF_HOTWATER_GREEN_SENSOR,
        CONF_HOTWATER_STATUS_SENSOR,
        CONF_HOTWATER_ENERGY_SENSOR,
        CONF_NOTIFY_TARGET,
        # Costs group (read-through, rates, energy counters):
        CONF_IMPORT_COST_SENSOR,
        CONF_IMPORT_COST_OFF_PEAK_SENSOR,
        CONF_IMPORT_COST_PEAK_SENSOR,
        CONF_EXPORT_EARNINGS_SENSOR,
        CONF_STANDING_CHARGE_ELECTRICITY_SENSOR,
        CONF_STANDING_CHARGE_GAS_SENSOR,
        CONF_GAS_COST_SENSOR,
        CONF_GAS_ENERGY_SENSOR,
        CONF_IMPORT_RATE_SENSOR,
        CONF_EXPORT_RATE_SENSOR,
        CONF_GAS_RATE_SENSOR,
        CONF_PV_ENERGY_SENSOR,
        CONF_GRID_IMPORT_ENERGY_SENSOR,
        CONF_GRID_EXPORT_ENERGY_SENSOR,
        CONF_BATTERY_DISCHARGE_ENERGY_SENSOR,
        CONF_EV_ENERGY_SENSOR,
        CONF_EV_GREEN_ENERGY_SENSOR,
    }
)

# List (list[entity_id]) reference keys.
LIST_ENTITY_CONF_KEYS: frozenset[str] = frozenset({CONF_MANAGED_LOAD_SENSORS})

ENTITY_REF_CONF_KEYS: frozenset[str] = SCALAR_ENTITY_CONF_KEYS | LIST_ENTITY_CONF_KEYS

_LOGGER = logging.getLogger(__name__)


def capture_ref(hass: HomeAssistant, entity_id: str) -> dict[str, str] | None:
    """Return the ``{platform, unique_id}`` anchor for ``entity_id``.

    ``None`` when the entity is not in the registry or has no unique_id (e.g. some legacy
    notify entities) — such references stay entity_id-only and fall back gracefully.
    """
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None or entry.unique_id is None:
        return None
    return {"platform": entry.platform, "unique_id": entry.unique_id}


def resolve_ref(hass: HomeAssistant, entity_id: str, ref: dict[str, str] | None) -> str:
    """Resolve a stored reference to the current entity_id.

    Prefer the registry lookup by ``(domain, platform, unique_id)``; fall back to the stored
    ``entity_id`` when there is no anchor or the lookup misses.
    """
    if not ref or not ref.get("unique_id"):
        return entity_id
    domain = entity_id.split(".", 1)[0]
    found = er.async_get(hass).async_get_entity_id(domain, ref["platform"], ref["unique_id"])
    if found and found != entity_id:
        # A rename (the intended self-heal) or, less benignly, a unique_id collision between
        # two instances — either way a write target has moved, so leave an audit trail (L-3).
        _LOGGER.info(
            "Entity reference redirected %s -> %s (platform=%s, unique_id=%s)",
            entity_id,
            found,
            ref["platform"],
            ref["unique_id"],
        )
    return found or entity_id


def capture_all(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Build the ``entity_refs`` anchor map for every entity reference present in ``data``.

    Scalar keys map to a single anchor; the managed-loads list maps to ``{entity_id: anchor}``.
    Keys absent from ``data`` or whose entities cannot be anchored are simply omitted.
    """
    refs: dict[str, Any] = {}
    for key in SCALAR_ENTITY_CONF_KEYS:
        entity_id = data.get(key)
        if not entity_id:
            continue
        anchor = capture_ref(hass, entity_id)
        if anchor is not None:
            refs[key] = anchor
    for key in LIST_ENTITY_CONF_KEYS:
        per_entity = {
            entity_id: anchor
            for entity_id in data.get(key) or []
            if (anchor := capture_ref(hass, entity_id)) is not None
        }
        if per_entity:
            refs[key] = per_entity
    return refs


def resolve_config(hass: HomeAssistant, merged: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``merged`` with every entity reference rewritten to its current id.

    No-ops cleanly when no ``entity_refs`` anchor map is present (e.g. entries that predate
    the migration, or test fixtures) — the original entity_ids pass through unchanged.
    """
    refs: dict[str, Any] = merged.get(CONF_ENTITY_REFS) or {}
    if not refs:
        return dict(merged)

    resolved = dict(merged)
    for key in SCALAR_ENTITY_CONF_KEYS:
        entity_id = merged.get(key)
        if entity_id:
            resolved[key] = resolve_ref(hass, entity_id, refs.get(key))
    for key in LIST_ENTITY_CONF_KEYS:
        entity_ids = merged.get(key)
        if entity_ids:
            per_entity = refs.get(key) or {}
            resolved[key] = [
                resolve_ref(hass, entity_id, per_entity.get(entity_id)) for entity_id in entity_ids
            ]
    return resolved

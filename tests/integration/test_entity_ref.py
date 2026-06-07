"""Tests for the unique_id-based entity reference resolution layer."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.energy_conductor import entity_ref
from custom_components.energy_conductor.const import (
    CONF_BATTERY_SOC_SENSOR,
    CONF_ENTITY_REFS,
    CONF_MANAGED_LOAD_SENSORS,
    CONF_OFF_PEAK_SENSOR,
)
from homeassistant.helpers import entity_registry as er


def _register(hass, domain, platform, unique_id, object_id):
    """Create a registry entry and return its entity_id."""
    return (
        er.async_get(hass)
        .async_get_or_create(domain, platform, unique_id, suggested_object_id=object_id)
        .entity_id
    )


async def test_capture_ref_found(hass):
    entity_id = _register(hass, "sensor", "givenergy", "soc-1", "battery_soc")
    assert entity_ref.capture_ref(hass, entity_id) == {
        "platform": "givenergy",
        "unique_id": "soc-1",
    }


async def test_capture_ref_not_in_registry(hass):
    assert entity_ref.capture_ref(hass, "sensor.nonexistent") is None


async def test_capture_ref_no_unique_id(hass, monkeypatch):
    fake = SimpleNamespace(async_get=lambda _eid: SimpleNamespace(platform="x", unique_id=None))
    monkeypatch.setattr(entity_ref.er, "async_get", lambda _hass: fake)
    assert entity_ref.capture_ref(hass, "sensor.weird") is None


async def test_resolve_ref_no_anchor_falls_back(hass):
    assert entity_ref.resolve_ref(hass, "sensor.x", None) == "sensor.x"
    assert entity_ref.resolve_ref(hass, "sensor.x", {"platform": "p"}) == "sensor.x"


async def test_resolve_ref_survives_rename(hass):
    """The core scenario: entity_id changes but unique_id is stable → resolve to new id."""
    entity_id = _register(hass, "sensor", "givenergy", "soc-1", "battery_soc")
    ref = entity_ref.capture_ref(hass, entity_id)

    new_id = (
        er.async_get(hass)
        .async_update_entity(entity_id, new_entity_id="sensor.loft_battery_soc")
        .entity_id
    )
    assert new_id == "sensor.loft_battery_soc"

    # Old stored id no longer exists, but the anchor resolves to the new one.
    assert entity_ref.resolve_ref(hass, entity_id, ref) == "sensor.loft_battery_soc"


async def test_resolve_ref_miss_falls_back(hass):
    """Anchor present but no registry match (entity gone entirely) → stored id."""
    ref = {"platform": "givenergy", "unique_id": "vanished"}
    assert entity_ref.resolve_ref(hass, "sensor.gone", ref) == "sensor.gone"


async def test_capture_all_mixed(hass):
    soc = _register(hass, "sensor", "givenergy", "soc-1", "battery_soc")
    load_a = _register(hass, "sensor", "myenergi", "zappi-1", "zappi_ev")
    data = {
        CONF_BATTERY_SOC_SENSOR: soc,
        CONF_OFF_PEAK_SENSOR: "binary_sensor.not_registered",  # not in registry → omitted
        CONF_MANAGED_LOAD_SENSORS: [load_a, "sensor.unregistered_load"],
    }
    refs = entity_ref.capture_all(hass, data)

    assert refs[CONF_BATTERY_SOC_SENSOR] == {"platform": "givenergy", "unique_id": "soc-1"}
    assert CONF_OFF_PEAK_SENSOR not in refs
    assert refs[CONF_MANAGED_LOAD_SENSORS] == {
        load_a: {"platform": "myenergi", "unique_id": "zappi-1"}
    }


async def test_resolve_config_noop_without_refs(hass):
    merged = {CONF_BATTERY_SOC_SENSOR: "sensor.battery_soc", "other": 5}
    assert entity_ref.resolve_config(hass, merged) == merged


async def test_resolve_config_rewrites_scalar_and_list(hass):
    soc = _register(hass, "sensor", "givenergy", "soc-1", "battery_soc")
    load = _register(hass, "sensor", "myenergi", "zappi-1", "zappi_ev")
    merged = {
        CONF_BATTERY_SOC_SENSOR: soc,
        CONF_MANAGED_LOAD_SENSORS: [load],
        CONF_ENTITY_REFS: entity_ref.capture_all(
            hass, {CONF_BATTERY_SOC_SENSOR: soc, CONF_MANAGED_LOAD_SENSORS: [load]}
        ),
        "write_mode": "dry_run",
    }

    er.async_get(hass).async_update_entity(soc, new_entity_id="sensor.loft_battery_soc")
    er.async_get(hass).async_update_entity(load, new_entity_id="sensor.outdoor_zappi_ev")

    resolved = entity_ref.resolve_config(hass, merged)
    assert resolved[CONF_BATTERY_SOC_SENSOR] == "sensor.loft_battery_soc"
    assert resolved[CONF_MANAGED_LOAD_SENSORS] == ["sensor.outdoor_zappi_ev"]
    assert resolved["write_mode"] == "dry_run"  # non-entity keys untouched

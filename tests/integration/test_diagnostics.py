"""Config-entry diagnostics dump (write-outcome observability)."""

from __future__ import annotations

import json
from unittest.mock import patch

from custom_components.energy_conductor.const import DOMAIN
from custom_components.energy_conductor.diagnostics import async_get_config_entry_diagnostics
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import MOCK_CONFIG


def _arrange_entities(hass: HomeAssistant) -> None:
    hass.states.async_set(MOCK_CONFIG["battery_soc_sensor"], "50", {})
    hass.states.async_set(MOCK_CONFIG["battery_charge_control"], "40", {"max": 100})
    hass.states.async_set(MOCK_CONFIG["battery_discharge_limit"], "40", {"max": 100})
    hass.states.async_set(MOCK_CONFIG["off_peak_sensor"], "off", {})


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    entry.add_to_hass(hass)
    with patch(
        "custom_components.energy_conductor.notifier.Notifier.notify",
        return_value=None,
    ):
        ok = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return ok


async def test_diagnostics_dump(hass: HomeAssistant) -> None:
    _arrange_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="d1")
    assert await _setup(hass, entry)

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert set(diag) >= {"config", "coordinator", "last_decisions", "last_site_state"}
    # Identifying entity references are redacted — they embed room/device/area names.
    assert diag["config"]["notify_target"] == "**REDACTED**"
    assert diag["config"]["battery_soc_sensor"] == "**REDACTED**"
    assert diag["config"]["battery_discharge_limit"] == "**REDACTED**"
    # The decision's target entity id is redacted too (the kind/outcome still identify it).
    discharge = diag["last_decisions"]["discharge"]
    assert discharge is not None
    assert discharge["target_entity"] == "**REDACTED**"
    assert discharge["kind"] == "set_discharge_limit"
    # Write-observability fields are present on the coordinator snapshot.
    coord = diag["coordinator"]
    assert coord["write_mode"] == "dry_run"
    for key in ("writes_sent", "write_failures", "last_write_outcome", "degraded_since", "status"):
        assert key in coord
    # A healthy tick populated the site-state snapshot.
    assert diag["last_site_state"] is not None
    # The whole dump must be JSON-serialisable (datetimes/enums coerced to primitives).
    json.dumps(diag)


async def test_diagnostics_redacts_error_strings(hass: HomeAssistant) -> None:
    """Error fields re-leak identifiers the structured redaction removed (entity ids in
    WriteFailure text, the notify target in notify errors), so they're redacted too — while
    None is preserved so "is there an error?" still shows (Codex review)."""
    _arrange_entities(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="d3")
    assert await _setup(hass, entry)

    coord = hass.data[DOMAIN][entry.entry_id]
    coord.last_error = "sensor.loft_battery_soc: stale (3600s)"
    coord.last_write_error = "number.loft_battery_discharge_limit: set_value failed"
    coord.last_notify_error = "notify.pixel_9a: boom"

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["coordinator"]["last_error"] == "**REDACTED**"
    assert diag["coordinator"]["last_write_error"] == "**REDACTED**"
    assert diag["coordinator"]["last_notify_error"] == "**REDACTED**"
    # No leaked identifiers anywhere in the serialised dump.
    blob = json.dumps(diag)
    assert "loft_battery" not in blob
    assert "pixel_9a" not in blob


async def test_diagnostics_without_coordinator(hass: HomeAssistant) -> None:
    """If setup failed there's no coordinator — diagnostics must still return the redacted
    config rather than KeyError-ing on hass.data (Gemini review)."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="d2")
    entry.add_to_hass(hass)  # not set up → not in hass.data[DOMAIN]

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["coordinator"] is None
    assert diag["config"]["battery_soc_sensor"] == "**REDACTED**"
    json.dumps(diag)


def test_decision_dict_none() -> None:
    """A missing decision serialises to None (e.g. before the first overnight plan)."""
    from custom_components.energy_conductor.diagnostics import _decision_dict

    assert _decision_dict(None, None) is None

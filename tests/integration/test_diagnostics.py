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
    # The notify target is the one personal-ish value — redacted.
    assert diag["config"]["notify_target"] == "**REDACTED**"
    # Write-observability fields are present on the coordinator snapshot.
    coord = diag["coordinator"]
    assert coord["write_mode"] == "dry_run"
    for key in ("writes_sent", "write_failures", "last_write_outcome", "degraded_since", "status"):
        assert key in coord
    # A healthy tick populated the site-state snapshot.
    assert diag["last_site_state"] is not None
    # The whole dump must be JSON-serialisable (datetimes/enums coerced to primitives).
    json.dumps(diag)

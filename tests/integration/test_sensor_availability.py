"""Guard: diagnostic sensors must publish a value after setup.

Sensors extend CoordinatorEntity. A first refresh where build_site_state raises
EntityProblem (e.g. the SoC sensor unavailable at boot) must still leave the
diagnostic sensors published (degraded status), not withheld as `unknown`.

(The "sensors never publish" symptom chased earlier was a false alarm — three
sensors have bare entity_ids — sensor.status / sensor.overnight_plan_target /
sensor.discharge_decision — not the sensor.blithe_* prefix the rest carry, so
querying sensor.blithe_status returned `unknown`. These tests lock in the real
behaviour so an actual availability regression would be caught.)
"""

from __future__ import annotations

from unittest.mock import patch

from custom_components.energy_conductor.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import MOCK_CONFIG


def _arrange_entities(hass: HomeAssistant, *, soc: str) -> None:
    """Set up the entities the adapter reads. soc='unavailable' simulates boot."""
    hass.states.async_set(MOCK_CONFIG["battery_soc_sensor"], soc, {})
    hass.states.async_set(MOCK_CONFIG["battery_charge_control"], "40", {"max": 100})
    hass.states.async_set(MOCK_CONFIG["battery_discharge_limit"], "40", {"max": 100})
    hass.states.async_set(MOCK_CONFIG["off_peak_sensor"], "off", {})


def _status_state(hass: HomeAssistant, entry: MockConfigEntry):
    """Look up the status sensor by unique_id (device naming varies by entry)."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}-status")
    assert entity_id is not None, "status sensor was never registered"
    return hass.states.get(entity_id)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    entry.add_to_hass(hass)
    # The notify service isn't registered in the harness; stub the notifier dispatch
    # so a degraded-but-emitting tick doesn't raise ServiceNotFound noise.
    with patch(
        "custom_components.energy_conductor.notifier.Notifier.notify",
        return_value=None,
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return result


async def test_sensors_publish_after_healthy_setup(hass: HomeAssistant) -> None:
    """Baseline: with all entities healthy, diagnostic sensors are available."""
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="t1")

    assert await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    status = _status_state(hass, entry)
    assert status is not None
    assert status.state not in ("unavailable", "unknown"), status.state


async def test_sensors_publish_when_soc_unavailable_at_boot(hass: HomeAssistant) -> None:
    """The bug: SoC sensor unavailable at first refresh.

    Diagnostic sensors must still publish (degraded status), not be withheld.
    """
    _arrange_entities(hass, soc="unavailable")
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="t2")

    assert await _setup(hass, entry)

    status = _status_state(hass, entry)
    assert status is not None
    assert status.state not in ("unavailable", "unknown"), (
        f"status sensor withheld at boot: {status.state}"
    )

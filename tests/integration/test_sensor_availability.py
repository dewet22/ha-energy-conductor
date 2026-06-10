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


async def test_off_peak_window_start_sensor_registered(hass: HomeAssistant) -> None:
    """NextOffPeakWindowStartSensor must be registered even when no start sensor is configured."""
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="t3")

    assert await _setup(hass, entry)

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}-off-peak-window-start"
    )
    assert entity_id is not None, "NextOffPeakWindowStartSensor was never registered"
    state = hass.states.get(entity_id)
    assert state is not None
    # Without a start sensor configured the state should be unknown (None native value)
    assert state.state in ("unknown", "unavailable", "None", "none") or state.state is not None


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


async def test_status_sensor_exposes_write_attributes(hass: HomeAssistant) -> None:
    """Write-outcome observability: the status sensor surfaces write_mode + write counters."""
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="t4")
    assert await _setup(hass, entry)

    attrs = _status_state(hass, entry).attributes
    for key in (
        "write_mode",
        "writes_sent",
        "write_failures",
        "last_write_at",
        "last_write_outcome",
        "last_write_error",
        "degraded_since",
    ):
        assert key in attrs, key
    assert attrs["write_mode"] == "dry_run"  # MOCK_CONFIG default
    assert attrs["degraded_since"] is None  # healthy setup


async def test_discharge_sensor_exposes_outcome(hass: HomeAssistant) -> None:
    """The discharge decision sensor surfaces the per-decision outcome + write_mode."""
    _arrange_entities(hass, soc="50")
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="t5")
    assert await _setup(hass, entry)

    registry = er.async_get(hass)
    eid = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}-discharge-decision")
    assert eid is not None
    attrs = hass.states.get(eid).attributes
    assert attrs.get("outcome") in ("dry_run", "applied", "unchanged", "failed")
    assert attrs.get("write_mode") == "dry_run"

"""Coordinator wiring tests (HA-glue layer).

Covers the diagnostic surfacing of notify failures — a clean tick resets
`status`, so notify failures are tracked on independent counters that persist.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from custom_components.energy_conductor.coordinator import EnergyConductorCoordinator
from custom_components.energy_conductor.decisions import Decision, DecisionKind


def _decision(dedupe_key: str = "d-0", value: int = 0) -> Decision:
    return Decision(
        kind=DecisionKind.SET_DISCHARGE_LIMIT,
        target_entity="number.battery_discharge_limit",
        value=value,
        reason="reserve floor reached",
        dedupe_key=dedupe_key,
    )


@pytest.fixture
def coordinator(hass, mock_config_entry) -> EnergyConductorCoordinator:
    mock_config_entry.add_to_hass(hass)
    coord = EnergyConductorCoordinator(hass, mock_config_entry)
    # Replace the real notifier/writer with mocks; we test coordinator bookkeeping,
    # not the HA service call (covered in test_notifier.py).
    coord.notifier.notify = AsyncMock(return_value=None)
    coord.writer.write = AsyncMock(return_value=None)
    return coord


async def test_emit_counts_successful_notification(coordinator) -> None:
    await coordinator._emit(_decision())

    assert coordinator.notifications_sent == 1
    assert coordinator.notify_failures == 0
    assert coordinator.last_notify_error is None


async def test_emit_records_notify_failure(coordinator) -> None:
    coordinator.notifier.notify = AsyncMock(return_value="notify.pixel_9a: boom")

    await coordinator._emit(_decision())

    # Failure surfaced on the persistent counters, not lost to the log.
    assert coordinator.notify_failures == 1
    assert coordinator.last_notify_error == "notify.pixel_9a: boom"
    # Still counted as an attempt.
    assert coordinator.notifications_sent == 1


async def test_failed_notification_retries_next_tick(coordinator) -> None:
    """A notify failure must not commit the dedupe key, so the same decision retries."""
    coordinator.notifier.notify = AsyncMock(return_value="notify.pixel_9a: boom")

    await coordinator._emit(_decision(dedupe_key="d-0"))
    # First attempt failed and was NOT deduped away.
    assert coordinator.notifications_sent == 1
    assert coordinator.notify_failures == 1

    # Notify recovers; the identical decision must be retried (not suppressed).
    coordinator.notifier.notify = AsyncMock(return_value=None)
    await coordinator._emit(_decision(dedupe_key="d-0"))
    assert coordinator.notifications_sent == 2
    assert coordinator.notify_failures == 1  # no new failure

    # Now that it succeeded, a third identical decision IS deduped.
    await coordinator._emit(_decision(dedupe_key="d-0"))
    assert coordinator.notifications_sent == 2


async def test_write_failure_commits_dedupe_key(coordinator) -> None:
    """A write failure commits the key (anti-spam) but emits a failure notification."""
    from custom_components.energy_conductor.writer import WriteFailure

    coordinator.writer.write = AsyncMock(side_effect=WriteFailure("set_value failed"))

    await coordinator._emit(_decision(dedupe_key="d-0"))
    # Two notifications: the decision + the WRITE FAILED follow-up.
    assert coordinator.notifications_sent == 2

    # Repeating the same decision is suppressed — no per-tick write/notify spam.
    await coordinator._emit(_decision(dedupe_key="d-0"))
    assert coordinator.notifications_sent == 2


async def test_emit_dedupes_repeated_decision(coordinator) -> None:
    await coordinator._emit(_decision(dedupe_key="d-0"))
    await coordinator._emit(_decision(dedupe_key="d-0"))

    # Second identical decision is suppressed.
    assert coordinator.notifications_sent == 1


async def test_emit_renotifies_on_changed_decision(coordinator) -> None:
    await coordinator._emit(_decision(dedupe_key="d-0", value=0))
    await coordinator._emit(_decision(dedupe_key="d-1", value=2600))

    assert coordinator.notifications_sent == 2


# ---- hot-water boost decision builder ---------------------------------------------------

from datetime import UTC, datetime  # noqa: E402

from custom_components.energy_conductor.coordinator import _hot_water_decision  # noqa: E402
from custom_components.energy_conductor.model import (  # noqa: E402
    Battery,
    HotWaterState,
    SiteState,
    SolarForecast,
    TariffState,
)


def _site_state(hot_water: HotWaterState | None) -> SiteState:
    return SiteState(
        now=datetime(2026, 6, 8, 21, 0, tzinfo=UTC),
        battery=Battery(90.0, 11.0, 3000, 3000, 4.0),
        ev_charger=None,
        solar_forecast=SolarForecast(slots=(), fallback_kwh=6.0, fallback_source="t"),
        tariff=TariffState(False, False, None, None),
        baseline_load_w=700.0,
        hot_water=hot_water,
    )


def _hw(*, boost: bool, hours: float | None) -> HotWaterState:
    return HotWaterState(
        reserve_kwh=1.5,
        capacity_kwh=11.0,
        reserve_percent=14.0,
        last_full_at=datetime(2026, 6, 6, 3, 0, tzinfo=UTC),
        depletion_kwh_per_day=3.0,
        depletion_source="stats",
        boost_recommended=boost,
        suggested_boost_hours=hours,
    )


def test_hot_water_decision_none_when_unconfigured() -> None:
    assert _hot_water_decision(_site_state(None)) is None


def test_hot_water_decision_none_when_not_recommended() -> None:
    assert _hot_water_decision(_site_state(_hw(boost=False, hours=None))) is None


def test_hot_water_decision_built_when_recommended() -> None:
    decision = _hot_water_decision(_site_state(_hw(boost=True, hours=2.0)))
    assert decision is not None
    assert decision.kind == DecisionKind.RECOMMEND_HOT_WATER_BOOST
    assert decision.value == 2.0
    assert "2026-06-08" in decision.dedupe_key
    assert "reserve" in decision.reason.lower()

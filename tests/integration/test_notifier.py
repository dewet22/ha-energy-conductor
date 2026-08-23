"""Tests for the HA notify dispatch seam.

Regression coverage for two bugs found this session:
- the notify entity_id was called as a legacy `notify.<name>` service (fixed to
  `notify.send_message` with an entity target);
- a dispatch failure was swallowed into the log, leaving the integration looking
  dead with no diagnostic signal.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from custom_components.energy_conductor.const import (
    WRITE_MODE_DRY_RUN,
    WRITE_MODE_LIVE,
)
from custom_components.energy_conductor.decisions import Decision, DecisionKind
from custom_components.energy_conductor.notifier import Notifier, render_message


def _decision() -> Decision:
    return Decision(
        kind=DecisionKind.SET_DISCHARGE_LIMIT,
        target_entity="number.battery_discharge_limit",
        value=0,
        reason="reserve floor reached",
        dedupe_key="d-0",
    )


@pytest.fixture
def hass() -> MagicMock:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    return hass


async def test_notify_uses_send_message_with_entity_target(hass: MagicMock) -> None:
    notifier = Notifier(hass, "notify.pixel_9a", WRITE_MODE_LIVE)

    result = await notifier.notify(_decision())

    assert result is None  # success
    hass.services.async_call.assert_awaited_once()
    args, kwargs = hass.services.async_call.call_args
    assert args[0] == "notify"
    assert args[1] == "send_message"
    # entity_id carried as a target, NOT mangled into the service name
    assert kwargs["target"] == {"entity_id": "notify.pixel_9a"}
    assert kwargs["blocking"] is True
    data = args[2]
    assert data["title"] == "Energy Conductor"
    assert "reserve floor reached" in data["message"]


async def test_notify_returns_error_string_on_failure(hass: MagicMock) -> None:
    hass.services.async_call.side_effect = RuntimeError("boom")
    notifier = Notifier(hass, "notify.pixel_9a", WRITE_MODE_DRY_RUN)

    # Must not raise — a notify failure must never crash a coordinator tick.
    result = await notifier.notify(_decision())

    assert result is not None
    assert "notify.pixel_9a" in result
    assert "boom" in result


def test_render_message_dry_run_prefix() -> None:
    msg = render_message(_decision(), WRITE_MODE_DRY_RUN)
    assert msg.startswith("[dry-run] ")
    assert "Discharge cap → 0W" in msg
    assert "reserve floor reached" in msg


def test_render_message_live_has_no_prefix() -> None:
    msg = render_message(_decision(), WRITE_MODE_LIVE)
    assert not msg.startswith("[dry-run]")


def test_render_message_charge_target_formats_percent() -> None:
    decision = Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity="number.battery_charge_target",
        value=80,
        reason="cheap energy — fill to 80%",
        dedupe_key="c-80",
    )
    msg = render_message(decision, WRITE_MODE_LIVE)
    assert "Battery SoC setpoint → 80%" in msg
    assert "cheap energy" in msg


def test_render_message_hot_water_boost_formats_hours() -> None:
    decision = Decision(
        kind=DecisionKind.RECOMMEND_HOT_WATER_BOOST,
        target_entity="hot_water",
        value=2.0,
        reason="reserve low",
        dedupe_key="hw-1",
    )
    msg = render_message(decision, WRITE_MODE_LIVE)
    assert "Hot water boost recommended → ~2.0h" in msg
    assert "reserve low" in msg


def test_render_message_verification_mismatch_formats_watts() -> None:
    decision = Decision(
        kind=DecisionKind.VERIFICATION_MISMATCH,
        target_entity="actuation",
        value=2000.0,
        reason="discharge capped at 0 but battery discharging 2000 W",
        dedupe_key="mismatch-2026-06-08",
    )
    msg = render_message(decision, WRITE_MODE_LIVE)
    assert "Actuation mismatch → 2000W" in msg
    assert "battery discharging" in msg

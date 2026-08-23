"""Writer tests — notify-only decisions must never trigger a service write."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from custom_components.energy_conductor.const import WRITE_MODE_DRY_RUN, WRITE_MODE_LIVE
from custom_components.energy_conductor.decisions import Decision, DecisionKind
from custom_components.energy_conductor.writer import WriteFailure, Writer


@pytest.fixture
def hass() -> MagicMock:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    return hass


def _decision(kind: DecisionKind, value) -> Decision:
    return Decision(kind=kind, target_entity="x", value=value, reason="r", dedupe_key="k")


async def test_writer_skips_notify_only_kind_in_live_mode(hass: MagicMock) -> None:
    writer = Writer(hass, WRITE_MODE_LIVE)
    await writer.write(_decision(DecisionKind.RECOMMEND_HOT_WATER_BOOST, 2.0))
    hass.services.async_call.assert_not_called()


async def test_writer_writes_number_kind_in_live_mode(hass: MagicMock) -> None:
    writer = Writer(hass, WRITE_MODE_LIVE)
    await writer.write(_decision(DecisionKind.SET_DISCHARGE_LIMIT, 0))
    hass.services.async_call.assert_awaited_once()


async def test_writer_non_numeric_value_raises_write_failure(hass: MagicMock) -> None:
    # Security audit L-2: Decision.value is Any — a non-numeric value must surface
    # as WriteFailure (handled by the coordinator), not as a raw TypeError/ValueError
    # escaping the failure boundary. No service call is attempted.
    writer = Writer(hass, WRITE_MODE_LIVE)
    with pytest.raises(WriteFailure, match="non-numeric"):
        await writer.write(_decision(DecisionKind.SET_DISCHARGE_LIMIT, "not-a-number"))
    hass.services.async_call.assert_not_called()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "-inf"])
async def test_writer_non_finite_value_raises_write_failure(hass: MagicMock, bad) -> None:
    # float("nan")/float("inf") pass the float() conversion — the writer is the last
    # boundary before a hardware write, so non-finite values must also be rejected
    # (Gemini review on the L-2 fix; same class as audit H-3).
    writer = Writer(hass, WRITE_MODE_LIVE)
    with pytest.raises(WriteFailure, match="non-finite"):
        await writer.write(_decision(DecisionKind.SET_DISCHARGE_LIMIT, bad))
    hass.services.async_call.assert_not_called()


# ---- slot pinning (time entities) --------------------------------------------------------


def _slot_decision(value) -> Decision:
    return Decision(
        kind=DecisionKind.SET_SLOT_TIME,
        target_entity="time.slot1_start",
        value=value,
        reason="Pin charge slot 1 always-on (setpoint regime)",
        dedupe_key=f"slot-pin-{value}",
    )


async def test_slot_time_write_calls_time_set_value(hass: MagicMock) -> None:
    writer = Writer(hass, WRITE_MODE_LIVE)
    await writer.write(_slot_decision("00:00:00"))
    hass.services.async_call.assert_awaited_once_with(
        "time",
        "set_value",
        {"entity_id": "time.slot1_start", "time": "00:00:00"},
        blocking=True,
    )


async def test_slot_time_write_dry_run_noop(hass: MagicMock) -> None:
    writer = Writer(hass, WRITE_MODE_DRY_RUN)
    await writer.write(_slot_decision("00:00:00"))
    hass.services.async_call.assert_not_called()


@pytest.mark.parametrize("bad", [0, None, ""])
async def test_slot_time_non_string_value_raises_write_failure(hass: MagicMock, bad) -> None:
    # Same failure boundary as the numeric kinds: Decision.value is Any, so a value that
    # isn't a usable time string must surface as WriteFailure, not reach the service call.
    writer = Writer(hass, WRITE_MODE_LIVE)
    with pytest.raises(WriteFailure, match="non-string"):
        await writer.write(_slot_decision(bad))
    hass.services.async_call.assert_not_called()

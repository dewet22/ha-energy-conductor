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


async def test_write_failure_retries_until_success(coordinator) -> None:
    """Audit M-4: a failed write must RETRY every tick (not be suppressed), while the
    notifications (primary + WRITE FAILED follow-up) fire only once each."""
    from custom_components.energy_conductor.writer import WriteFailure

    coordinator.writer.write = AsyncMock(side_effect=WriteFailure("set_value failed"))

    # Tick 1: primary + failure notification; write attempted.
    await coordinator._emit(_decision(dedupe_key="d-0"))
    assert coordinator.notifications_sent == 2
    assert coordinator.writer.write.await_count == 1

    # Tick 2: still failing — write RETRIED, but no notification spam.
    await coordinator._emit(_decision(dedupe_key="d-0"))
    assert coordinator.writer.write.await_count == 2
    assert coordinator.notifications_sent == 2

    # Tick 3: write recovers and lands; no new notifications.
    coordinator.writer.write = AsyncMock(return_value=None)
    await coordinator._emit(_decision(dedupe_key="d-0"))
    assert coordinator.writer.write.await_count == 1  # the recovered mock
    assert coordinator.notifications_sent == 2

    # Tick 4: fully handled now — identical decision is deduped (no write, no notify).
    await coordinator._emit(_decision(dedupe_key="d-0"))
    assert coordinator.writer.write.await_count == 1
    assert coordinator.notifications_sent == 2


async def test_emit_outcome_applied_in_live_mode(coordinator) -> None:
    """Write-outcome tracking: a live write reports 'applied' and increments writes_sent once."""
    from custom_components.energy_conductor.const import WRITE_MODE_LIVE

    coordinator.write_mode = WRITE_MODE_LIVE
    assert await coordinator._emit(_decision(dedupe_key="d-0")) == "applied"
    assert coordinator.writes_sent == 1
    assert coordinator.last_write_at is not None
    assert coordinator.last_write_outcome == "applied"

    # Re-emitting the identical decision is a no-op write: 'unchanged', no extra count.
    assert await coordinator._emit(_decision(dedupe_key="d-0")) == "unchanged"
    assert coordinator.writes_sent == 1


async def test_emit_outcome_dry_run(coordinator) -> None:
    """In dry-run the write is a no-op: outcome 'dry_run', writes_sent stays 0."""
    from custom_components.energy_conductor.const import WRITE_MODE_DRY_RUN

    assert coordinator.write_mode == WRITE_MODE_DRY_RUN  # fixture default
    assert await coordinator._emit(_decision(dedupe_key="d-0")) == "dry_run"
    assert coordinator.writes_sent == 0
    assert coordinator.last_write_outcome == "dry_run"


async def test_emit_outcome_failed_counts_and_records(coordinator) -> None:
    """A WriteFailure reports 'failed', increments write_failures, records the error."""
    from custom_components.energy_conductor.writer import WriteFailure

    coordinator.writer.write = AsyncMock(side_effect=WriteFailure("boom"))
    assert await coordinator._emit(_decision(dedupe_key="d-0")) == "failed"
    assert coordinator.write_failures == 1
    assert coordinator.last_write_error == "boom"
    assert coordinator.last_write_outcome == "failed"


async def test_successful_write_clears_stale_error(coordinator) -> None:
    """A recovered write must clear last_write_error, so the dashboard doesn't show a stale
    failure next to a successful outcome (Codex review)."""
    from custom_components.energy_conductor.const import WRITE_MODE_LIVE
    from custom_components.energy_conductor.writer import WriteFailure

    coordinator.write_mode = WRITE_MODE_LIVE
    coordinator.writer.write = AsyncMock(side_effect=WriteFailure("boom"))
    await coordinator._emit(_decision(dedupe_key="d-0"))
    assert coordinator.last_write_error == "boom"

    # Recovery: identical decision, write now succeeds → applied AND the stale error is cleared.
    coordinator.writer.write = AsyncMock(return_value=None)
    assert await coordinator._emit(_decision(dedupe_key="d-0")) == "applied"
    assert coordinator.last_write_error is None
    assert coordinator.last_write_outcome == "applied"


async def test_degraded_since_set_and_cleared(coordinator) -> None:
    """degraded_since stamps the first non-OK tick and clears on recovery, so a gap explains
    its own duration (audit observability)."""
    from custom_components.energy_conductor.adapter import EntityProblem
    from custom_components.energy_conductor.const import STATUS_DEGRADED, STATUS_OK

    coordinator.adapter.build_site_state = AsyncMock(side_effect=EntityProblem("sensor.x: gone"))
    await coordinator._async_update_data()
    assert coordinator.status == STATUS_DEGRADED
    first = coordinator.degraded_since
    assert first is not None

    # A second consecutive failure keeps the original timestamp.
    await coordinator._async_update_data()
    assert coordinator.degraded_since == first

    # A healthy tick clears it.
    coordinator.adapter.build_site_state = AsyncMock(return_value=_site_state(None))
    await coordinator._async_update_data()
    assert coordinator.status == STATUS_OK
    assert coordinator.degraded_since is None


# ---- per-tick SoC setpoint ---------------------------------------------------------------


def _written(coordinator, entity: str) -> list[float]:
    """Values written to `entity` across every writer.write call so far."""
    return [
        call.args[0].value
        for call in coordinator.writer.write.await_args_list
        if call.args[0].target_entity == entity
    ]


async def test_setpoint_written_every_regime(coordinator) -> None:
    """The SoC setpoint is steered on every tick alongside the discharge cap: 100% while
    energy is cheap, the charge control's own minimum through the peak."""
    charge_entity = coordinator.config[CONF_BATTERY_CHARGE_CONTROL]
    discharge_entity = coordinator.config[CONF_BATTERY_DISCHARGE_LIMIT]

    coordinator.adapter.build_site_state = AsyncMock(return_value=_site_state(None, off_peak=True))
    await coordinator._async_update_data()

    assert coordinator.last_setpoint_decision is not None
    assert coordinator.last_setpoint_decision.kind == DecisionKind.SET_CHARGE_TARGET
    assert coordinator.last_setpoint_decision.value == 100
    assert coordinator.last_setpoint_outcome == "dry_run"
    assert _written(coordinator, charge_entity) == [100]
    assert _written(coordinator, discharge_entity) == [0]

    # Peak: the setpoint drops to the control minimum and the discharge cap is released.
    coordinator.adapter.build_site_state = AsyncMock(return_value=_site_state(None))
    await coordinator._async_update_data()

    assert coordinator.last_setpoint_decision.value == _CHARGE_TARGET_MIN
    assert _written(coordinator, charge_entity) == [100, _CHARGE_TARGET_MIN]
    assert _written(coordinator, discharge_entity) == [0, 3000]


async def test_setpoint_transition_writes_once(coordinator) -> None:
    """Two consecutive ticks in the same regime write the setpoint once — the dedupe key
    is regime + value, so an unchanged setpoint isn't re-written every 30 s."""
    charge_entity = coordinator.config[CONF_BATTERY_CHARGE_CONTROL]
    coordinator.adapter.build_site_state = AsyncMock(return_value=_site_state(None, off_peak=True))

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert _written(coordinator, charge_entity) == [100]
    assert coordinator.last_setpoint_outcome == "unchanged"


async def test_dispatch_only_tick_sets_setpoint_100(coordinator) -> None:
    """A dispatch outside the off-peak window is cheap energy too — the regime tests it
    explicitly rather than relying on the tariff sensor flipping in lock-step."""
    coordinator.adapter.build_site_state = AsyncMock(
        return_value=_site_state(None, off_peak=False, dispatching=True)
    )
    await coordinator._async_update_data()

    assert coordinator.last_setpoint_decision.value == 100


async def test_hot_water_prompt_still_fires(coordinator) -> None:
    """The boost prompt moved off the nightly plan run onto the tick path; its per-day
    dedupe key still keeps it to one prompt."""
    coordinator.adapter.build_site_state = AsyncMock(
        return_value=_site_state(_hw(boost=True, hours=2.0))
    )
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    kinds = [call.args[0].kind for call in coordinator.notifier.notify.await_args_list]
    assert kinds.count(DecisionKind.RECOMMEND_HOT_WATER_BOOST) == 1


async def test_no_overnight_schedule_registered(coordinator) -> None:
    """async_start registers the state-change listeners and nothing else: with the setpoint
    emitted from every tick there is no planning schedule and no startup plan run."""
    from unittest.mock import patch

    coordinator.adapter.build_site_state = AsyncMock(return_value=_site_state(None))
    with patch(
        "custom_components.energy_conductor.coordinator.async_track_state_change_event",
        return_value=lambda: None,
    ) as track_state:
        await coordinator.async_start()
    try:
        track_state.assert_called_once()
        watched = track_state.call_args.args[1]
        assert coordinator.config[CONF_BATTERY_SOC_SENSOR] in watched
        # The state listener is the ONLY registration — no time-change schedules remain.
        assert len(coordinator._unsubs) == 1
        # ...and nothing plans at startup: the first tick emits the setpoint.
        coordinator.adapter.build_site_state.assert_not_awaited()
    finally:
        await coordinator.async_stop()


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
    GridState,
    HotWaterState,
    SiteState,
    SolarForecast,
    TariffState,
)

# The charge control's own minimum, distinct from the reserve so the self-consume
# setpoint can't be confused with it.
_CHARGE_TARGET_MIN = 5.0


def _site_state(
    hot_water: HotWaterState | None,
    *,
    now: datetime | None = None,
    off_peak: bool = False,
    dispatching: bool = False,
    battery_power: float | None = None,
    grid: GridState | None = None,
) -> SiteState:
    return SiteState(
        now=now or datetime(2026, 6, 8, 21, 0, tzinfo=UTC),
        battery=Battery(
            90.0,
            11.0,
            3000,
            3000,
            4.0,
            power_w=battery_power,
            charge_target_min_percent=_CHARGE_TARGET_MIN,
        ),
        ev_charger=None,
        solar_forecast=SolarForecast(slots=(), fallback_kwh=6.0, fallback_source="t"),
        tariff=TariffState(off_peak, dispatching, None, None),
        baseline_load_w=700.0,
        hot_water=hot_water,
        grid=grid,
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


# ---- actuation verification (anti-drain) -----------------------------------------------

from datetime import timedelta  # noqa: E402

from custom_components.energy_conductor.const import (  # noqa: E402
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_SOC_SENSOR,
    WRITE_MODE_LIVE,
)

_T0 = datetime(2026, 6, 8, 23, 0, tzinfo=UTC)


async def _tick(coordinator, *, secs: int, battery_power: float) -> None:
    """Run one live off-peak tick with the given battery power, `secs` after _T0."""
    coordinator.adapter.build_site_state = AsyncMock(
        return_value=_site_state(
            None, now=_T0 + timedelta(seconds=secs), off_peak=True, battery_power=battery_power
        )
    )
    await coordinator._async_update_data()


async def test_verification_flags_persistent_mismatch_and_notifies(coordinator) -> None:
    """Live mode: a discharge cap that doesn't idle the battery is flagged only after it
    persists VERIFY_MISMATCH_SECONDS, and notifies exactly once."""
    coordinator.write_mode = WRITE_MODE_LIVE

    # Tick 1: mismatch detected but within the debounce window → not yet confirmed.
    await _tick(coordinator, secs=0, battery_power=2000.0)
    assert coordinator.verification_status != "mismatch"
    n_before = coordinator.notifications_sent

    # Tick 2: still mismatching 95 s later → confirmed + notified once.
    await _tick(coordinator, secs=95, battery_power=2000.0)
    assert coordinator.verification_status == "mismatch"
    assert "discharging" in coordinator.last_verification_detail
    assert coordinator.notifications_sent == n_before + 1

    # Tick 3: still mismatching → no repeat notification (deduped per episode).
    await _tick(coordinator, secs=200, battery_power=2000.0)
    assert coordinator.notifications_sent == n_before + 1


async def test_verification_recovers_and_clears(coordinator) -> None:
    coordinator.write_mode = WRITE_MODE_LIVE
    await _tick(coordinator, secs=0, battery_power=2000.0)
    await _tick(coordinator, secs=95, battery_power=2000.0)
    assert coordinator.verification_status == "mismatch"

    # Battery goes idle → verdict clears.
    await _tick(coordinator, secs=130, battery_power=30.0)
    assert coordinator.verification_status == "ok"


async def test_verification_skipped_in_dry_run(coordinator) -> None:
    """Dry-run isn't actuating, so there's nothing to verify — status stays n/a."""
    assert coordinator.write_mode == "dry_run"  # fixture default
    await _tick(coordinator, secs=0, battery_power=2000.0)
    await _tick(coordinator, secs=95, battery_power=2000.0)
    assert coordinator.verification_status == "n/a"


# ---- write-readback verification ---------------------------------------------------------

_DISCHARGE_KEY = ("set_discharge_limit", "number.battery_discharge_limit")


async def test_write_readback_not_recorded_in_dry_run(coordinator) -> None:
    """Dry-run issues no real write — nothing to read back."""
    assert coordinator.write_mode == "dry_run"
    await _tick(coordinator, secs=0, battery_power=None)
    assert coordinator._commanded == {}


async def test_write_readback_ok_when_entity_reflects_command(coordinator, hass) -> None:
    """A judgeable commanded write whose entity reads the commanded value → status ok."""
    coordinator.write_mode = WRITE_MODE_LIVE
    await _tick(coordinator, secs=0, battery_power=None)  # live write records _commanded
    # Make it judgeable: pretend the write happened well before the tick clock (_T0).
    coordinator._commanded[_DISCHARGE_KEY].written_at = _T0 - timedelta(seconds=120)
    hass.states.async_set("number.battery_discharge_limit", "0")  # echo landed

    await _tick(coordinator, secs=0, battery_power=None)  # actuation n/a (no battery power)
    assert coordinator.verification_status == "ok"
    assert "as commanded" in coordinator.last_verification_detail


async def test_write_readback_pending_during_settle(coordinator, hass) -> None:
    """No verdict while the write-echo is still settling — an early read must not judge."""
    coordinator.write_mode = WRITE_MODE_LIVE
    await _tick(coordinator, secs=0, battery_power=None)
    coordinator._commanded[_DISCHARGE_KEY].written_at = _T0  # just written
    hass.states.async_set("number.battery_discharge_limit", "50")  # still the old value

    await _tick(coordinator, secs=30, battery_power=None)  # 30s < VERIFY_MISMATCH_SECONDS
    assert coordinator.verification_status == "n/a"
    assert coordinator._commanded[_DISCHARGE_KEY].retries == 0


async def test_write_readback_flip_back_retries_then_flags(coordinator, hass) -> None:
    """The flip-back signature: entity reverts to the original value. First confirmation
    re-issues the write once (self-heal); if the retry doesn't stick either, the mismatch
    persists through the debounce window → flagged + notified."""
    coordinator.write_mode = WRITE_MODE_LIVE
    await _tick(coordinator, secs=0, battery_power=None)  # write 1 (applied)
    coordinator._commanded[_DISCHARGE_KEY].written_at = _T0 - timedelta(seconds=120)
    hass.states.async_set("number.battery_discharge_limit", "50")  # flipped back

    # First confirmation → re-issue scheduled (emit bookkeeping cleared), not yet flagged.
    writes_before = coordinator.writer.write.await_count
    await _tick(coordinator, secs=30, battery_power=None)
    assert coordinator.verification_status != "mismatch"
    assert coordinator._commanded[_DISCHARGE_KEY].retries == 1

    # Next tick re-issues the same command (write 2) and starts a fresh settle window.
    await _tick(coordinator, secs=60, battery_power=None)
    assert coordinator.writer.write.await_count > writes_before

    # The retry doesn't stick either: make it judgeable, entity still shows the old value.
    coordinator._commanded[_DISCHARGE_KEY].written_at = _T0 - timedelta(seconds=120)
    await _tick(coordinator, secs=90, battery_power=None)
    assert coordinator.verification_status != "mismatch"  # mismatch persistence window starts
    n_before = coordinator.notifications_sent

    await _tick(coordinator, secs=190, battery_power=None)  # >90s persisted → confirmed
    assert coordinator.verification_status == "mismatch"
    assert "entity reads 50" in coordinator.last_verification_detail
    assert coordinator.notifications_sent == n_before + 1


async def test_readback_loop_processes_every_commanded_entry(coordinator, hass) -> None:
    """One stuck entity must not blind the readback loop to the streams behind it.

    With four commanded streams (discharge, setpoint, two slot pins) an early
    exhausted-budget mismatch used to return straight out of the loop, so every later
    entry lost its mismatch check, its self-heal and its budget re-arm.
    """
    coordinator.write_mode = WRITE_MODE_LIVE
    await _tick(coordinator, secs=0, battery_power=None)
    charge_key = ("set_charge_target", "number.battery_charge_target")
    assert list(coordinator._commanded) == [_DISCHARGE_KEY, charge_key]

    # First stream: mismatched with its self-heal budget already spent → flags.
    hass.states.async_set("number.battery_discharge_limit", "50")
    coordinator._commanded[_DISCHARGE_KEY].written_at = _T0 - timedelta(seconds=120)
    coordinator._commanded[_DISCHARGE_KEY].retries = 1
    # Second stream: a fresh drift that still has its self-heal available.
    hass.states.async_set("number.battery_charge_target", "4")
    coordinator._commanded[charge_key].written_at = _T0 - timedelta(seconds=120)

    result = coordinator._check_writes_landed(_T0 + timedelta(seconds=30))

    assert result is not None and result.ok is False
    assert "set_discharge_limit" in result.detail  # the first stream's verdict still wins
    # ...and the second stream was still processed: self-heal armed, re-issue unblocked.
    assert coordinator._commanded[charge_key].retries == 1
    assert coordinator._emit_state[charge_key].written is None


async def test_verification_renotifies_after_recovery(coordinator) -> None:
    """A second mismatch the same day, after recovery, is a fresh episode → notifies again
    (the per-episode dedupe key, not per-day; Codex review)."""
    coordinator.write_mode = WRITE_MODE_LIVE

    # Episode 1: confirm + notify once.
    await _tick(coordinator, secs=0, battery_power=2000.0)
    await _tick(coordinator, secs=95, battery_power=2000.0)
    assert coordinator.verification_status == "mismatch"
    n1 = coordinator.notifications_sent

    # Recover.
    await _tick(coordinator, secs=130, battery_power=30.0)
    assert coordinator.verification_status == "ok"

    # Episode 2 (same day): a fresh mismatch must notify again, not be deduped away.
    await _tick(coordinator, secs=200, battery_power=2000.0)
    await _tick(coordinator, secs=300, battery_power=2000.0)
    assert coordinator.verification_status == "mismatch"
    assert coordinator.notifications_sent == n1 + 1


# ---- charge-slot-1 pinning ---------------------------------------------------------------

from custom_components.energy_conductor.const import (  # noqa: E402
    CHARGE_SLOT_PIN_END,
    CHARGE_SLOT_PIN_START,
    CONF_CHARGE_SLOT_1_END_ENTITY,
    CONF_CHARGE_SLOT_1_START_ENTITY,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

_SLOT_START_ENTITY = "time.charge_slot_1_start"
_SLOT_END_ENTITY = "time.charge_slot_1_end"
_SLOT_START_KEY = ("set_slot_time", _SLOT_START_ENTITY)


@pytest.fixture
def slot_coordinator(hass, mock_config_entry) -> EnergyConductorCoordinator:
    """Coordinator whose config also carries both charge-slot-1 time entities."""
    entry = MockConfigEntry(
        domain=mock_config_entry.domain,
        data={
            **mock_config_entry.data,
            CONF_CHARGE_SLOT_1_START_ENTITY: _SLOT_START_ENTITY,
            CONF_CHARGE_SLOT_1_END_ENTITY: _SLOT_END_ENTITY,
        },
        entry_id="slot_entry",
        title="Energy Conductor",
    )
    entry.add_to_hass(hass)
    coord = EnergyConductorCoordinator(hass, entry)
    coord.notifier.notify = AsyncMock(return_value=None)
    coord.writer.write = AsyncMock(return_value=None)
    return coord


async def test_slot_pin_emitted_when_configured(slot_coordinator) -> None:
    """Both slot bounds are pinned on the first tick and left alone thereafter — the
    dedupe key is the pinned value, so a stable slot isn't re-written every 30 s."""
    await _tick(slot_coordinator, secs=0, battery_power=None)

    kinds = [call.args[0].kind for call in slot_coordinator.writer.write.await_args_list]
    assert kinds.count(DecisionKind.SET_SLOT_TIME) == 2
    assert _written(slot_coordinator, _SLOT_START_ENTITY) == [CHARGE_SLOT_PIN_START]
    assert _written(slot_coordinator, _SLOT_END_ENTITY) == [CHARGE_SLOT_PIN_END]

    await _tick(slot_coordinator, secs=30, battery_power=None)
    assert _written(slot_coordinator, _SLOT_START_ENTITY) == [CHARGE_SLOT_PIN_START]
    assert _written(slot_coordinator, _SLOT_END_ENTITY) == [CHARGE_SLOT_PIN_END]


async def test_slot_pin_skipped_when_unconfigured(coordinator) -> None:
    """Without both slot entities EC must not guess at a write target."""
    await _tick(coordinator, secs=0, battery_power=None)

    kinds = [call.args[0].kind for call in coordinator.writer.write.await_args_list]
    assert DecisionKind.SET_SLOT_TIME not in kinds


async def test_slot_pin_unconfigured_warns_once_in_live_mode(coordinator, caplog) -> None:
    """A live install without the pickers gets the setpoint writes but not the two-sided
    mechanics they assume — say so once, rather than skipping in silence."""
    coordinator.write_mode = WRITE_MODE_LIVE
    caplog.clear()
    await _tick(coordinator, secs=0, battery_power=None)
    await _tick(coordinator, secs=30, battery_power=None)

    warnings = [r for r in caplog.records if "charge slot 1" in r.message.lower()]
    assert len(warnings) == 1
    assert coordinator.slot_pin_status == "unconfigured"


async def test_slot_pin_unconfigured_silent_in_dry_run(coordinator, caplog) -> None:
    """Dry-run drives nothing, so the missing pickers aren't yet a problem to warn about."""
    caplog.clear()
    await _tick(coordinator, secs=0, battery_power=None)

    assert not [r for r in caplog.records if "charge slot 1" in r.message.lower()]


async def test_slot_pin_status_pinned_when_configured(slot_coordinator) -> None:
    assert slot_coordinator.slot_pin_status == "pinned"


async def test_slot_drift_heals_via_readback(slot_coordinator, hass) -> None:
    """An externally-edited slot is re-pinned by the readback loop, not the dedupe key:
    the string mismatch clears the emit bookkeeping and the per-tick re-emission rewrites."""
    slot_coordinator.write_mode = WRITE_MODE_LIVE
    await _tick(slot_coordinator, secs=0, battery_power=None)  # pin written (applied)
    assert slot_coordinator._commanded[_SLOT_START_KEY].value == CHARGE_SLOT_PIN_START

    # Someone edits the slot on the inverter app; make the write judgeable.
    hass.states.async_set(_SLOT_START_ENTITY, "23:30:00")
    slot_coordinator._commanded[_SLOT_START_KEY].written_at = _T0 - timedelta(seconds=120)

    # Readback mismatch → self-heal scheduled (emit bookkeeping cleared), not yet flagged.
    await _tick(slot_coordinator, secs=30, battery_power=None)
    assert slot_coordinator._commanded[_SLOT_START_KEY].retries == 1
    assert slot_coordinator.verification_status != "mismatch"

    # Next tick re-writes the same pin — the drift is healed.
    await _tick(slot_coordinator, secs=60, battery_power=None)
    assert _written(slot_coordinator, _SLOT_START_ENTITY) == [
        CHARGE_SLOT_PIN_START,
        CHARGE_SLOT_PIN_START,
    ]

    # The entity now echoes the pin → the verdict clears.
    hass.states.async_set(_SLOT_START_ENTITY, CHARGE_SLOT_PIN_START)
    slot_coordinator._commanded[_SLOT_START_KEY].written_at = _T0 - timedelta(seconds=120)
    await _tick(slot_coordinator, secs=90, battery_power=None)
    assert slot_coordinator.verification_status == "ok"


async def test_slot_drift_heals_again_after_recovery(slot_coordinator, hass) -> None:
    """The self-heal budget re-arms on a healthy readback, so the SECOND drift event heals too.

    The pin's value never changes, so the "same value keeps its retry budget" rule in _emit
    can't re-arm it the way a changing numeric setpoint does. Without a reset on the ok path
    the budget is spent for the coordinator's lifetime and a later drift is only flagged —
    leaving the slot on the user's value until HA restarts.
    """
    slot_coordinator.write_mode = WRITE_MODE_LIVE

    async def drift_and_heal(*, at: int) -> None:
        """One full drift → self-heal → healthy-readback cycle, starting at tick `at`."""
        hass.states.async_set(_SLOT_START_ENTITY, "23:30:00")
        slot_coordinator._commanded[_SLOT_START_KEY].written_at = _T0 - timedelta(seconds=120)
        await _tick(slot_coordinator, secs=at, battery_power=None)  # mismatch → re-issue armed
        assert slot_coordinator.verification_status != "mismatch"
        await _tick(slot_coordinator, secs=at + 30, battery_power=None)  # re-write lands
        hass.states.async_set(_SLOT_START_ENTITY, CHARGE_SLOT_PIN_START)
        slot_coordinator._commanded[_SLOT_START_KEY].written_at = _T0 - timedelta(seconds=120)
        await _tick(slot_coordinator, secs=at + 60, battery_power=None)
        assert slot_coordinator.verification_status == "ok"

    await _tick(slot_coordinator, secs=0, battery_power=None)  # initial pin
    await drift_and_heal(at=30)
    # A healthy readback means the heal stuck — the budget must be back to full.
    assert slot_coordinator._commanded[_SLOT_START_KEY].retries == 0

    await drift_and_heal(at=120)
    # Three writes: the initial pin plus one re-issue per drift event.
    assert _written(slot_coordinator, _SLOT_START_ENTITY) == [CHARGE_SLOT_PIN_START] * 3


# ---- rate-watch (warn-only fill-mode economics) ------------------------------------------

from custom_components.energy_conductor.const import (  # noqa: E402
    CONF_EXPORT_RATE_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
)
from homeassistant.const import STATE_UNAVAILABLE  # noqa: E402

_IMPORT_RATE_ENTITY = "sensor.import_rate"
_EXPORT_RATE_ENTITY = "sensor.export_rate"


@pytest.fixture
def rate_coordinator(hass, mock_config_entry) -> EnergyConductorCoordinator:
    """Coordinator whose config also carries the import/export rate sensors."""
    entry = MockConfigEntry(
        domain=mock_config_entry.domain,
        data={
            **mock_config_entry.data,
            CONF_IMPORT_RATE_SENSOR: _IMPORT_RATE_ENTITY,
            CONF_EXPORT_RATE_SENSOR: _EXPORT_RATE_ENTITY,
        },
        entry_id="rate_entry",
        title="Energy Conductor",
    )
    entry.add_to_hass(hass)
    coord = EnergyConductorCoordinator(hass, entry)
    coord.notifier.notify = AsyncMock(return_value=None)
    coord.writer.write = AsyncMock(return_value=None)
    return coord


def _set_rates(hass, import_rate, export_rate, *, unit: str = "GBP/kWh") -> None:
    for entity, value in ((_IMPORT_RATE_ENTITY, import_rate), (_EXPORT_RATE_ENTITY, export_rate)):
        if value is None:
            hass.states.async_set(entity, STATE_UNAVAILABLE)
        else:
            hass.states.async_set(entity, str(value), {"unit_of_measurement": unit})


async def _cheap_tick(coordinator, *, secs: int) -> None:
    coordinator.adapter.build_site_state = AsyncMock(
        return_value=_site_state(None, now=_T0 + timedelta(seconds=secs), off_peak=True)
    )
    await coordinator._async_update_data()


async def _peak_tick(coordinator, *, secs: int) -> None:
    coordinator.adapter.build_site_state = AsyncMock(
        return_value=_site_state(None, now=_T0 + timedelta(seconds=secs), off_peak=False)
    )
    await coordinator._async_update_data()


def _warnings(coordinator) -> list[Decision]:
    return [
        call.args[0]
        for call in coordinator.notifier.notify.await_args_list
        if call.args[0].kind == DecisionKind.RATE_ECONOMICS_WARNING
    ]


async def test_rate_watch_ok_at_current_tariff(rate_coordinator, hass) -> None:
    """6.9p import / 12p export at eta 0.9 still favours grid-filling: status ok, no warning."""
    _set_rates(hass, 0.069, 0.12)
    await _cheap_tick(rate_coordinator, secs=0)

    assert rate_coordinator.rate_watch_status == "ok"
    assert rate_coordinator.rate_watch_margin_gbp == pytest.approx(0.0433, abs=1e-4)
    assert _warnings(rate_coordinator) == []


async def test_rate_watch_inverted_warns_exactly_once_per_episode(rate_coordinator, hass) -> None:
    """A collapsed export rate inverts the premise: warn once, then stay quiet while inverted."""
    _set_rates(hass, 0.069, 0.05)
    await _cheap_tick(rate_coordinator, secs=0)

    assert rate_coordinator.rate_watch_status == "inverted"
    assert rate_coordinator.rate_watch_margin_gbp < 0
    warnings = _warnings(rate_coordinator)
    assert len(warnings) == 1
    # The reason interpolates only the margin — never an entity_id (redaction rule).
    assert "p/kWh" in warnings[0].reason
    assert "sensor." not in warnings[0].reason

    # Repeat ticks in the same episode: still exactly one notification.
    await _cheap_tick(rate_coordinator, secs=30)
    await _cheap_tick(rate_coordinator, secs=60)
    assert len(_warnings(rate_coordinator)) == 1
    # ...and the regime is untouched throughout: the check only ever warns.
    assert rate_coordinator.last_setpoint_decision.value == 100


async def test_rate_watch_rearms_above_threshold_and_warns_again(rate_coordinator, hass) -> None:
    """Recovery clear of the hysteresis band re-arms the latch, so a fresh inversion warns."""
    _set_rates(hass, 0.069, 0.05)
    await _cheap_tick(rate_coordinator, secs=0)
    assert len(_warnings(rate_coordinator)) == 1

    # Export recovers well clear of RATE_WATCH_REARM_GBP → ok and re-armed.
    _set_rates(hass, 0.069, 0.12)
    await _cheap_tick(rate_coordinator, secs=30)
    assert rate_coordinator.rate_watch_status == "ok"

    # A second inversion episode is a second warning.
    _set_rates(hass, 0.069, 0.05)
    await _cheap_tick(rate_coordinator, secs=60)
    assert rate_coordinator.rate_watch_status == "inverted"
    assert len(_warnings(rate_coordinator)) == 2


async def test_rate_watch_hysteresis_band_does_not_rearm(rate_coordinator, hass) -> None:
    """A margin barely positive reads ok but must NOT re-arm — that band is where a
    hovering rate would otherwise flap the notification once per tick."""
    _set_rates(hass, 0.069, 0.05)
    await _cheap_tick(rate_coordinator, secs=0)
    assert len(_warnings(rate_coordinator)) == 1

    # +0.004 GBP/kWh: positive, but inside the 0.005 re-arm band.
    _set_rates(hass, 0.09, 0.104)
    await _cheap_tick(rate_coordinator, secs=30)
    assert rate_coordinator.rate_watch_status == "ok"
    assert rate_coordinator.rate_watch_margin_gbp == pytest.approx(0.004, abs=1e-4)

    # Dipping back under zero is the same episode — no second warning.
    _set_rates(hass, 0.069, 0.05)
    await _cheap_tick(rate_coordinator, secs=60)
    assert rate_coordinator.rate_watch_status == "inverted"
    assert len(_warnings(rate_coordinator)) == 1


async def test_rate_watch_na_when_rates_unconfigured(coordinator) -> None:
    """No rate sensors configured → nothing to judge; the check must not guess."""
    await _cheap_tick(coordinator, secs=0)

    assert coordinator.rate_watch_status == "n/a"
    assert coordinator.rate_watch_margin_gbp is None
    assert _warnings(coordinator) == []


async def test_rate_watch_na_when_rates_unreadable(rate_coordinator, hass) -> None:
    """An unavailable rate sensor resets to n/a rather than leaving a stale verdict up."""
    _set_rates(hass, 0.069, 0.05)
    await _cheap_tick(rate_coordinator, secs=0)
    assert rate_coordinator.rate_watch_status == "inverted"

    _set_rates(hass, 0.069, None)
    await _cheap_tick(rate_coordinator, secs=30)
    assert rate_coordinator.rate_watch_status == "n/a"
    assert rate_coordinator.rate_watch_margin_gbp is None


async def test_rate_watch_na_when_rate_is_not_finite(rate_coordinator, hass) -> None:
    """A NaN parses as a float but compares false against zero — it must not read as
    an inversion and fire a "nanp/kWh" warning."""
    _set_rates(hass, "nan", 0.12)
    await _cheap_tick(rate_coordinator, secs=0)

    assert rate_coordinator.rate_watch_status == "n/a"
    assert rate_coordinator.rate_watch_margin_gbp is None
    assert _warnings(rate_coordinator) == []


async def test_rate_watch_na_when_rate_unit_is_foreign(rate_coordinator, hass) -> None:
    """An unpriceable unit (foreign currency) is n/a, never silently treated as sterling."""
    _set_rates(hass, 0.069, 0.05, unit="EUR/kWh")
    await _cheap_tick(rate_coordinator, secs=0)

    assert rate_coordinator.rate_watch_status == "n/a"
    assert _warnings(rate_coordinator) == []


async def test_rate_watch_not_evaluated_in_self_consume(rate_coordinator, hass) -> None:
    """Outside the cheap window the import sensor is reading the PEAK rate, so the
    inequality is meaningless — the last cheap-window verdict is left untouched."""
    _set_rates(hass, 0.069, 0.12)
    await _cheap_tick(rate_coordinator, secs=0)
    assert rate_coordinator.rate_watch_status == "ok"

    # Peak rates would invert the margin if it were (wrongly) evaluated here.
    _set_rates(hass, 0.30, 0.12)
    await _peak_tick(rate_coordinator, secs=30)
    assert rate_coordinator.rate_watch_status == "ok"
    assert rate_coordinator.rate_watch_margin_gbp == pytest.approx(0.0433, abs=1e-4)
    assert _warnings(rate_coordinator) == []


async def test_rate_watch_latch_survives_a_self_consume_gap(rate_coordinator, hass) -> None:
    """The episode latch is not reset by leaving the cheap regime: an inversion that is
    still live on the next cheap window must not re-notify."""
    _set_rates(hass, 0.069, 0.05)
    await _cheap_tick(rate_coordinator, secs=0)
    assert len(_warnings(rate_coordinator)) == 1

    await _peak_tick(rate_coordinator, secs=30)
    await _cheap_tick(rate_coordinator, secs=60)

    assert rate_coordinator.rate_watch_status == "inverted"
    assert len(_warnings(rate_coordinator)) == 1


async def test_rate_watch_normalises_pence_denominated_sensors(rate_coordinator, hass) -> None:
    """Rate sensors commonly report GBp/kWh; the margin attribute is named _gbp and must be."""
    _set_rates(hass, 6.9, 12.0, unit="GBp/kWh")
    await _cheap_tick(rate_coordinator, secs=0)

    assert rate_coordinator.rate_watch_status == "ok"
    assert rate_coordinator.rate_watch_margin_gbp == pytest.approx(0.0433, abs=1e-4)

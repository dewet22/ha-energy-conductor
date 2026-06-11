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


async def test_tick_retries_pending_overnight_charge_write(coordinator) -> None:
    """Audit M-4 (Codex): a failed charge-target write must retry on the next coordinator
    tick, not only at the hourly plan re-eval — `_async_update_data` re-emits the cached
    overnight plan each tick so it isn't stale for up to an hour."""
    from unittest.mock import AsyncMock

    from custom_components.energy_conductor.writer import WriteFailure

    plan = Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity="number.battery_charge_target",
        value=80,
        reason="overnight plan",
        dedupe_key="overnight-2026-06-08-80",
    )
    # The overnight plan emitted earlier; its write failed → still pending.
    coordinator.writer.write = AsyncMock(side_effect=WriteFailure("boom"))
    await coordinator._emit(plan)
    coordinator.last_overnight_plan = plan
    # _site_state's now is 2026-06-08 21:00, so a plan stamped now is fresh.
    coordinator.last_overnight_plan_at = datetime(2026, 6, 8, 21, 0, tzinfo=UTC)
    assert coordinator.writer.write.await_count == 1

    # Write recovers; a coordinator tick must retry the pending charge-target write.
    coordinator.writer.write = AsyncMock(return_value=None)
    coordinator.adapter.build_site_state = AsyncMock(return_value=_site_state(None))
    await coordinator._async_update_data()

    # The tick wrote the discharge decision AND retried the pending overnight write.
    written = [c.args[0].target_entity for c in coordinator.writer.write.await_args_list]
    assert "number.battery_charge_target" in written


async def test_first_healthy_tick_runs_missed_startup_plan(coordinator) -> None:
    """A startup plan that lost the race against slower-loading integrations (e.g. the
    GivEnergy entities register after EC on restart) must run on the first healthy tick
    — not leave the plan empty until the next hourly slot (live find 2026-06-11)."""
    from custom_components.energy_conductor.adapter import EntityProblem

    # Startup: dependencies not registered yet → the immediate plan run skips.
    coordinator.adapter.build_site_state = AsyncMock(
        side_effect=EntityProblem("sensor.soc: not found")
    )
    await coordinator._run_overnight_plan()
    assert coordinator.last_overnight_plan is None

    # Dependencies appear → the next tick (~30s) fills the plan in, reusing the
    # tick's own SiteState rather than rebuilding it (recorder queries are dear).
    coordinator.adapter.build_site_state = AsyncMock(return_value=_site_state(None))
    await coordinator._async_update_data()
    assert coordinator.last_overnight_plan is not None
    assert coordinator.last_overnight_plan.kind == DecisionKind.SET_CHARGE_TARGET
    assert coordinator.adapter.build_site_state.await_count == 1

    # Subsequent ticks leave refreshes to the scheduled evaluations.
    coordinator._run_overnight_plan = AsyncMock()
    await coordinator._async_update_data()
    coordinator._run_overnight_plan.assert_not_awaited()


async def test_tick_does_not_retry_stale_overnight_plan(coordinator) -> None:
    """Audit M-4 (Codex): a pending charge-target write from a PAST planning cycle must not
    be applied when the entity recovers. Once the cached plan is older than _PLAN_RETRY_MAX_AGE
    (planning has been failing to refresh it), the every-tick retry is gated out — so a
    recovered number entity never lands an obsolete command."""
    from unittest.mock import AsyncMock

    from custom_components.energy_conductor.writer import WriteFailure

    plan = Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity="number.battery_charge_target",
        value=80,
        reason="overnight plan",
        dedupe_key="overnight-2026-06-07-80",
    )
    # Yesterday's plan, write failed → still pending. Planning has since failed to refresh it.
    coordinator.writer.write = AsyncMock(side_effect=WriteFailure("boom"))
    await coordinator._emit(plan)
    coordinator.last_overnight_plan = plan
    # 24h before _site_state.now (06-08 21:00) — well past the 12h retry window.
    coordinator.last_overnight_plan_at = datetime(2026, 6, 7, 21, 0, tzinfo=UTC)

    # Entity recovers; a tick must NOT re-apply the stale (past-cycle) target.
    coordinator.writer.write = AsyncMock(return_value=None)
    coordinator.adapter.build_site_state = AsyncMock(return_value=_site_state(None))
    await coordinator._async_update_data()

    written = [c.args[0].target_entity for c in coordinator.writer.write.await_args_list]
    assert "number.battery_charge_target" not in written


async def test_tick_retries_pending_plan_across_midnight(coordinator) -> None:
    """Audit M-4 (Codex): a plan emitted just before midnight must keep retrying straight
    through the date rollover — age-based freshness has no calendar blackout. A plan stamped
    23:55 is only 10 min old at 00:05, so its still-pending write is retried inside the
    overnight charging window."""
    from unittest.mock import AsyncMock

    from custom_components.energy_conductor.writer import WriteFailure

    plan = Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity="number.battery_charge_target",
        value=80,
        reason="overnight plan",
        dedupe_key="overnight-2026-06-08-80",
    )
    coordinator.writer.write = AsyncMock(side_effect=WriteFailure("boom"))
    await coordinator._emit(plan)
    coordinator.last_overnight_plan = plan
    coordinator.last_overnight_plan_at = datetime(2026, 6, 8, 23, 55, tzinfo=UTC)

    # Tick fires at 00:05 the next day — past the date boundary but only 10 min after the plan.
    coordinator.writer.write = AsyncMock(return_value=None)
    after_midnight = datetime(2026, 6, 9, 0, 5, tzinfo=UTC)
    coordinator.adapter.build_site_state = AsyncMock(
        return_value=_site_state(None, now=after_midnight)
    )
    await coordinator._async_update_data()

    written = [c.args[0].target_entity for c in coordinator.writer.write.await_args_list]
    assert "number.battery_charge_target" in written


async def test_async_start_survives_malformed_plan_time(coordinator, caplog) -> None:
    """Audit L-1: a malformed stored overnight plan time must not abort setup. It falls back
    to the default time with a warning, still registering the schedule + state listeners."""
    import logging

    from custom_components.energy_conductor.const import CONF_OVERNIGHT_PLAN_TIME

    coordinator.config[CONF_OVERNIGHT_PLAN_TIME] = "not-a-time"
    coordinator.last_overnight_plan = _decision()  # truthy → skip the immediate plan run

    with caplog.at_level(logging.WARNING, logger="custom_components.energy_conductor.coordinator"):
        await coordinator.async_start()
    try:
        assert coordinator._unsubs  # listeners registered despite the bad time
        assert "malformed overnight plan time" in caplog.text.lower()
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


def _site_state(
    hot_water: HotWaterState | None,
    *,
    now: datetime | None = None,
    off_peak: bool = False,
    battery_power: float | None = None,
    grid: GridState | None = None,
) -> SiteState:
    return SiteState(
        now=now or datetime(2026, 6, 8, 21, 0, tzinfo=UTC),
        battery=Battery(90.0, 11.0, 3000, 3000, 4.0, power_w=battery_power),
        ev_charger=None,
        solar_forecast=SolarForecast(slots=(), fallback_kwh=6.0, fallback_source="t"),
        tariff=TariffState(off_peak, False, None, None),
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
    WRITE_MODE_LIVE,
)
from custom_components.energy_conductor.coordinator import (  # noqa: E402
    _PLAN_RETRY_MAX_AGE,
    _CommandedWrite,
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


async def test_write_readback_drops_stale_charge_target(coordinator, hass) -> None:
    """A charge-target command must stop being verified once the overnight plan goes stale
    (>_PLAN_RETRY_MAX_AGE) — EC no longer re-enforces it, so a later divergence on the entity
    must NOT raise a false mismatch (Gemini review)."""
    coordinator.write_mode = WRITE_MODE_LIVE
    charge_entity = coordinator.config[CONF_BATTERY_CHARGE_CONTROL]
    charge_key = ("set_charge_target", charge_entity)
    # EC commanded a charge target long ago; the entity has since diverged (window over).
    coordinator._commanded[charge_key] = _CommandedWrite(
        value=80.0, written_at=_T0 - timedelta(hours=2)
    )
    # The stale plan object itself is still cached (planning succeeded hours ago, then
    # stopped refreshing) — without it the tick's startup catch-up would plan afresh.
    coordinator.last_overnight_plan = Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity=charge_entity,
        value=80,
        reason="overnight plan",
        dedupe_key="overnight-stale-80",
    )
    coordinator.last_overnight_plan_at = _T0 - (_PLAN_RETRY_MAX_AGE + timedelta(hours=1))
    hass.states.async_set(charge_entity, "20")  # changed since EC's stale command

    await _tick(coordinator, secs=0, battery_power=None)
    assert charge_key not in coordinator._commanded  # dropped, not verified
    assert coordinator.verification_status == "n/a"


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

"""DataUpdateCoordinator that ties the adapter, core, notifier and writer together."""

from __future__ import annotations

import logging
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .adapter import Adapter, EntityProblem, parse_hh_mm
from .const import (
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DISPATCHING_SENSOR,
    CONF_EV_POWER_SENSOR,
    CONF_NOTIFY_TARGET,
    CONF_OFF_PEAK_SENSOR,
    CONF_OVERNIGHT_PLAN_TIME,
    CONF_WRITE_MODE,
    COORDINATOR_TICK_SECONDS,
    DEFAULT_OVERNIGHT_PLAN_TIME,
    DOMAIN,
    STATUS_DEGRADED,
    STATUS_ERROR,
    STATUS_OK,
    VERIFY_MISMATCH_SECONDS,
    WRITE_MODE_DRY_RUN,
    WRITE_MODE_LIVE,
)
from .decisions import Decision, DecisionKind
from .discharge_guard import discharge_limit
from .entity_ref import resolve_config
from .jitter import hourly_jitter_offset
from .model import SiteState
from .notifier import Notifier
from .overnight import plan_overnight
from .verify import check_actuation
from .writer import WriteFailure, Writer

_LOGGER = logging.getLogger(__name__)

# A pending charge-target write retries every tick until it lands, but only while the cached
# plan is still fresh. Planning runs hourly, so a healthy plan is never more than ~1h old;
# this cap bounds how long a write keeps retrying once planning itself has stalled, so a
# recovered entity can't apply a target from a past night's cycle. 12h comfortably covers any
# overnight charging window yet stays well short of the ~24h gap to the next cycle. Age-based
# (not calendar-date) freshness avoids a retry blackout for a plan emitted near midnight.
_PLAN_RETRY_MAX_AGE = timedelta(hours=12)

# Decision kinds that drive a hardware write (vs notify-only). Mirrors writer.py's routing.
_WRITE_KINDS = frozenset({DecisionKind.SET_CHARGE_TARGET, DecisionKind.SET_DISCHARGE_LIMIT})


@dataclass
class _EmitState:
    """Per-(kind, target) bookkeeping for _emit, tracking three independent facts so a
    failed write retries every tick while notifications fire only once each (audit M-4)."""

    notified: str | None = None  # dedupe_key whose primary notification was delivered
    written: str | None = None  # dedupe_key whose write succeeded
    notify_failed: str | None = None  # dedupe_key whose failure notification was delivered


def _hot_water_decision(state: SiteState) -> Decision | None:
    """Build a notify-only hot-water boost prompt from the SiteState, or None.

    Returns None when the diverter isn't configured or no boost is recommended.
    Dedupe is keyed per day + suggested hours so it prompts once a day and re-prompts
    only if the recommendation changes.
    """
    hw = state.hot_water
    if hw is None or not hw.boost_recommended:
        return None
    if hw.last_full_at is None:
        last_full = "no full reading in lookback"
    else:
        hours = (state.now - hw.last_full_at).total_seconds() / 3600.0
        last_full = f"last full {hours:.0f}h ago"
    forecast_kwh = state.solar_forecast.total_kwh_forecast
    reason = (
        f"Hot water reserve ~{hw.reserve_percent:.0f}% ({last_full}, depletion "
        f"{hw.depletion_kwh_per_day:.1f} kWh/d {hw.depletion_source}); forecast "
        f"{forecast_kwh:.1f} kWh won't refill — boost ~{hw.suggested_boost_hours:.0f}h on off-peak"
    )
    plan_date = state.now.date().isoformat()
    return Decision(
        kind=DecisionKind.RECOMMEND_HOT_WATER_BOOST,
        target_entity="hot_water",
        value=hw.suggested_boost_hours,
        reason=reason,
        dedupe_key=f"hot-water-{plan_date}-{hw.suggested_boost_hours}",
    )


class EnergyConductorCoordinator(DataUpdateCoordinator[None]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=COORDINATOR_TICK_SECONDS),
        )
        self.entry = entry
        # Resolve every entity reference to its current entity_id via the stored unique_id
        # anchors, so re-created/area-prefixed entities are found automatically. No-ops for
        # entries without anchors (pre-migration / tests). Touches only entity-id strings —
        # write_mode, Writer and Notifier are untouched, so dry-run behaviour is unchanged.
        self.config: dict[str, Any] = resolve_config(hass, {**entry.data, **entry.options})
        self.adapter = Adapter(hass, self.config)
        self.notifier = Notifier(
            hass,
            self.config[CONF_NOTIFY_TARGET],
            self.config.get(CONF_WRITE_MODE, WRITE_MODE_DRY_RUN),
        )
        self.writer = Writer(hass, self.config.get(CONF_WRITE_MODE, WRITE_MODE_DRY_RUN))

        self._emit_state: dict[tuple[str, str], _EmitState] = defaultdict(_EmitState)
        self.status: str = STATUS_OK
        self.last_error: str | None = None
        self.ticks_total: int = 0
        self.notifications_sent: int = 0
        # Notify-dispatch failures are tracked independently of `status`, which a clean
        # tick resets every 30s. These persist so a silently-failing notify target is
        # visible on the diagnostic sensor rather than lost to the log.
        self.notify_failures: int = 0
        self.last_notify_error: str | None = None
        # Write-outcome tracking, mirroring the notify counters above. EC actuates hardware
        # autonomously, so "did it write, when, did it succeed?" must be answerable at a glance.
        # writes_sent counts ACTUAL hardware writes (live only) — in dry-run it stays 0 and the
        # outcome reads "dry_run", so whether EC is really driving the inverter is unambiguous.
        self.write_mode: str = self.config.get(CONF_WRITE_MODE, WRITE_MODE_DRY_RUN)
        self.writes_sent: int = 0
        self.write_failures: int = 0
        self.last_write_at: datetime | None = None
        self.last_write_error: str | None = None
        self.last_write_outcome: str | None = None
        self.last_discharge_outcome: str | None = None
        self.last_overnight_outcome: str | None = None
        # When the coordinator first went non-OK (degraded/error), cleared on a healthy tick —
        # so a post-restart or upstream-entity gap explains its own duration.
        self.degraded_since: datetime | None = None
        self.last_overnight_plan: Decision | None = None
        # When last_overnight_plan was computed (state.now at plan time). Gates the every-tick
        # retry by elapsed age so a pending write from a past cycle is never applied once the
        # plan has gone stale — without a calendar-date blackout for plans made near midnight.
        self.last_overnight_plan_at: datetime | None = None
        self.last_discharge_decision: Decision | None = None
        self.last_site_state: SiteState | None = None
        # Actuation verification (live-mode only): does the meter reflect what EC commanded?
        # "ok" | "mismatch" (confirmed, persisted) | "n/a" (not applicable / dry-run).
        self.verification_status: str = "n/a"
        self.last_verification_detail: str | None = None
        self.last_verification_at: datetime | None = None
        self._mismatch_since: datetime | None = None
        self._mismatch_notified: bool = False

        self._unsubs: list = []

    async def async_start(self) -> None:
        # State-change listeners
        watched = [
            self.config[CONF_BATTERY_SOC_SENSOR],
            self.config[CONF_OFF_PEAK_SENSOR],
        ]
        if self.config.get(CONF_DISPATCHING_SENSOR):
            watched.append(self.config[CONF_DISPATCHING_SENSOR])
        if self.config.get(CONF_EV_POWER_SENSOR):
            watched.append(self.config[CONF_EV_POWER_SENSOR])

        self._unsubs.append(
            async_track_state_change_event(self.hass, watched, self._on_state_change)
        )

        # Scheduled overnight plan
        plan_time_raw = self.config.get(
            CONF_OVERNIGHT_PLAN_TIME, DEFAULT_OVERNIGHT_PLAN_TIME.isoformat()
        )
        parsed = parse_hh_mm(plan_time_raw)
        if parsed is None:
            _LOGGER.warning(
                "Malformed overnight plan time %r; falling back to %s",
                plan_time_raw,
                DEFAULT_OVERNIGHT_PLAN_TIME.isoformat(),
            )
            parsed = (DEFAULT_OVERNIGHT_PLAN_TIME.hour, DEFAULT_OVERNIGHT_PLAN_TIME.minute)
        hh, mm = parsed
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._run_overnight_plan, hour=hh, minute=mm, second=0
            )
        )

        # Hourly re-evaluation with startup-chosen jitter (HH:54..HH:56).
        # Spread across instances to avoid stampeding herd.
        jitter_minute, jitter_second = hourly_jitter_offset(random.randint(-60, 60))
        self._unsubs.append(
            async_track_time_change(
                self.hass,
                self._run_overnight_plan,
                minute=jitter_minute,
                second=jitter_second,
            )
        )
        _LOGGER.info(
            "Hourly plan re-evaluation scheduled for HH:%02d:%02d",
            jitter_minute,
            jitter_second,
        )

        # If we have no cached plan, run one immediately so the sensor isn't empty
        if self.last_overnight_plan is None:
            await self._run_overnight_plan()

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _on_state_change(self, event) -> None:
        # In HA 2026.5+, async_request_refresh is a coroutine (not a @callback).
        # Schedule it as a task so it actually runs instead of being silently discarded.
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_update_data(self) -> None:
        self.ticks_total += 1
        try:
            state = await self.adapter.build_site_state()
        except EntityProblem as exc:
            self.status = STATUS_DEGRADED
            self.last_error = str(exc)
            if self.degraded_since is None:
                self.degraded_since = dt_util.utcnow()
            _LOGGER.warning("Skipping tick: %s", exc)
            return
        except Exception as exc:
            self.status = STATUS_ERROR
            self.last_error = repr(exc)
            if self.degraded_since is None:
                self.degraded_since = dt_util.utcnow()
            _LOGGER.exception("Unexpected error building SiteState")
            raise UpdateFailed(str(exc)) from exc

        self.status = STATUS_OK
        self.last_error = None
        self.degraded_since = None
        self.last_site_state = state

        try:
            decision = discharge_limit(
                state, target_entity=self.config[CONF_BATTERY_DISCHARGE_LIMIT]
            )
        except Exception as exc:
            self.status = STATUS_ERROR
            self.last_error = repr(exc)
            if self.degraded_since is None:
                self.degraded_since = dt_util.utcnow()
            _LOGGER.exception("Discharge guard crashed")
            return

        self.last_discharge_outcome = await self._emit(decision)
        self.last_discharge_decision = decision

        # Retry a pending (failed) charge-target write on every tick too. The overnight
        # plan otherwise only re-emits at its scheduled time + hourly, so a transient
        # write failure would leave the target stale for up to an hour. _emit is a no-op
        # once the write has landed, so re-emitting the cached plan each tick is cheap.
        #
        # Only retry while the cached plan is still fresh (see _PLAN_RETRY_MAX_AGE). Planning
        # runs hourly, so a healthy plan is always recent; the age only grows when planning
        # itself has been failing — and in that degraded state a recovered entity must NOT
        # have a past cycle's target applied to it (audit M-4, Codex). The retry only acts on
        # a still-pending write anyway, so gating it out once the plan is stale costs nothing
        # in the healthy case.
        if (
            self.last_overnight_plan is not None
            and self.last_overnight_plan_at is not None
            and state.now - self.last_overnight_plan_at <= _PLAN_RETRY_MAX_AGE
        ):
            self.last_overnight_outcome = await self._emit(self.last_overnight_plan)

        # Actuation verification: did the meter reflect the discharge cap we just emitted?
        await self._verify_actuation(state)

    async def _verify_actuation(self, state: SiteState) -> None:
        """Check the last discharge actuation against the meter and flag a persistent mismatch.

        Live-mode only (dry-run isn't actuating). A mismatch must persist VERIFY_MISMATCH_SECONDS
        before it's confirmed — one timer serving as both settle window and debounce, robust to the
        variable tick cadence (state-change events trigger extra refreshes). Confirmed mismatches
        raise the actuation-mismatch binary sensor and notify once (per day, via _emit).
        """
        if self.write_mode != WRITE_MODE_LIVE:
            self.verification_status = "n/a"
            self.last_verification_detail = None
            self._mismatch_since = None
            self._mismatch_notified = False
            return

        result = check_actuation(state, self.last_discharge_decision, self.last_discharge_outcome)
        if result is None:
            self.verification_status = "n/a"
            self.last_verification_detail = None
            self._mismatch_since = None
            self._mismatch_notified = False
            return
        if result.ok:
            self.verification_status = "ok"
            self.last_verification_detail = result.detail
            self.last_verification_at = state.now
            self._mismatch_since = None
            self._mismatch_notified = False
            return

        # Mismatch — confirm only once it has persisted (settle + debounce in one window).
        if self._mismatch_since is None:
            self._mismatch_since = state.now
        if state.now - self._mismatch_since < timedelta(seconds=VERIFY_MISMATCH_SECONDS):
            return  # not yet persistent; leave the last confirmed verdict in place
        self.verification_status = "mismatch"
        self.last_verification_detail = result.detail
        self.last_verification_at = state.now
        if not self._mismatch_notified:
            mismatch = Decision(
                kind=DecisionKind.VERIFICATION_MISMATCH,
                target_entity="actuation",
                value=state.battery.power_w,
                reason=result.detail,
                dedupe_key=f"mismatch-{state.now.date().isoformat()}",
            )
            await self._emit(mismatch)
            self._mismatch_notified = True

    async def _run_overnight_plan(self, _now=None) -> None:
        try:
            state = await self.adapter.build_site_state()
        except EntityProblem as exc:
            _LOGGER.warning("Skipping overnight plan: %s", exc)
            return
        except Exception:
            self.status = STATUS_ERROR
            self.last_error = "Overnight plan failed (see logs)"
            _LOGGER.exception("Unexpected error building SiteState for overnight plan")
            return
        self.last_site_state = state
        try:
            decision = plan_overnight(
                state,
                target_entity=self.config[CONF_BATTERY_CHARGE_CONTROL],
                daily_kwh_target=state.daily_kwh_target,
            )
        except Exception:
            self.status = STATUS_ERROR
            self.last_error = "plan_overnight crashed (see logs)"
            _LOGGER.exception("plan_overnight crashed")
            return
        self.last_overnight_outcome = await self._emit(decision)
        self.last_overnight_plan = decision
        self.last_overnight_plan_at = state.now

        # Hot-water boost prompt — notify-only, evaluated alongside the overnight plan.
        hot_water_decision = _hot_water_decision(state)
        if hot_water_decision is not None:
            await self._emit(hot_water_decision)

    async def _emit(self, decision: Decision) -> str:
        """Notify + write a decision, returning the write outcome for observability:

        ``applied`` (live write landed) | ``dry_run`` (write-kind, dry-run, no-op) |
        ``unchanged`` (already written + notified, nothing to do) | ``failed`` (write error,
        will retry) | ``notified`` (notify-only kind, no hardware write).
        """
        key = (decision.kind.value, decision.target_entity)
        st = self._emit_state[key]
        dk = decision.dedupe_key
        is_write_kind = decision.kind in _WRITE_KINDS

        # Fully handled: written to hardware AND the user was told. Nothing to do.
        if st.written == dk and st.notified == dk:
            return "unchanged"

        # Primary notification — once per decision, retried only until delivered (so a
        # failed notify isn't suppressed forever; a retried write doesn't re-notify).
        if st.notified != dk and await self._notify(decision):
            st.notified = dk

        # Write — retried on each re-emission until it succeeds (number.set_value is
        # idempotent). Both writing decisions are re-emitted every coordinator tick: the
        # discharge limit directly, and the cached overnight plan via _async_update_data.
        # M-4: a failed write must NOT suppress the retry, or the actuator stays stale.
        # Only the failure notification is deduped.
        if st.written != dk:
            try:
                await self.writer.write(decision)
            except WriteFailure as exc:
                _LOGGER.warning("Write failed: %s", exc)
                self.write_failures += 1
                self.last_write_error = str(exc)
                if is_write_kind:
                    self.last_write_outcome = "failed"
                if st.notify_failed != dk:
                    failure_decision = Decision(
                        kind=decision.kind,
                        target_entity=decision.target_entity,
                        value=decision.value,
                        reason=f"WRITE FAILED — {exc}",
                        dedupe_key=f"{dk}-failed",
                    )
                    if await self._notify(failure_decision):
                        st.notify_failed = dk
                return "failed"  # NOT marked written → write retries on the next tick
            st.written = dk
            if not is_write_kind:
                return "notified"  # writer.write was a no-op (notify-only kind)
            # The write didn't fail, so any earlier failure is no longer current — clear it so
            # the dashboard doesn't show a stale "last write error" next to a healthy outcome.
            self.last_write_error = None
            if self.write_mode == WRITE_MODE_LIVE:
                # An actual hardware write landed.
                self.writes_sent += 1
                self.last_write_at = dt_util.utcnow()
                self.last_write_outcome = "applied"
                return "applied"
            self.last_write_outcome = "dry_run"
            return "dry_run"

        # Write was already applied earlier; this emission only (re)delivered a notification.
        return "unchanged"

    async def _notify(self, decision: Decision) -> bool:
        """Dispatch a notification; record any failure on the diagnostic counters.

        Returns True if the notification was delivered, False on failure.
        """
        error = await self.notifier.notify(decision)
        self.notifications_sent += 1
        if error is not None:
            self.notify_failures += 1
            self.last_notify_error = error
            return False
        return True

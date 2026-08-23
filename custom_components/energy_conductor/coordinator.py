"""DataUpdateCoordinator that ties the adapter, core, notifier and writer together."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .adapter import Adapter, EntityProblem
from .const import (
    CHARGE_SLOT_PIN_END,
    CHARGE_SLOT_PIN_START,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CHARGE_SLOT_1_END_ENTITY,
    CONF_CHARGE_SLOT_1_START_ENTITY,
    CONF_DISPATCHING_SENSOR,
    CONF_EV_POWER_SENSOR,
    CONF_EXPORT_RATE_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    CONF_NOTIFY_TARGET,
    CONF_OFF_PEAK_SENSOR,
    CONF_WRITE_MODE,
    COORDINATOR_TICK_SECONDS,
    DOMAIN,
    RATE_WATCH_EFFICIENCY,
    RATE_WATCH_REARM_GBP,
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
from .model import SiteState
from .money import normalise_rate
from .money_tracker import MoneyTracker
from .notifier import Notifier
from .rate_watch import fill_margin_gbp
from .regimes import REGIME_CHEAP_CHARGE, charge_setpoint, current_regime
from .verify import (
    VerificationResult,
    check_actuation,
    check_time_write_landed,
    check_write_landed,
)
from .writer import WriteFailure, Writer

_LOGGER = logging.getLogger(__name__)

# Decision kinds that drive a hardware write (vs notify-only). Mirrors writer.py's routing.
_WRITE_KINDS = frozenset(
    {
        DecisionKind.SET_CHARGE_TARGET,
        DecisionKind.SET_DISCHARGE_LIMIT,
        DecisionKind.SET_SLOT_TIME,
    }
)


@dataclass
class _CommandedWrite:
    """Bookkeeping for write-readback verification, per (kind, target) — the entity must come
    to reflect this value (the inverter's write-echo) and KEEP reflecting it on every tick."""

    value: float | str  # str for the "HH:MM:SS" slot pins
    written_at: datetime
    retries: int = 0  # re-issue budget consumed (retry once, then flag)


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
        self.last_setpoint_outcome: str | None = None
        # When the coordinator first went non-OK (degraded/error), cleared on a healthy tick —
        # so a post-restart or upstream-entity gap explains its own duration.
        self.degraded_since: datetime | None = None
        self.last_setpoint_decision: Decision | None = None
        self.last_discharge_decision: Decision | None = None
        self.last_site_state: SiteState | None = None
        # Actuation verification (live-mode only): does the meter reflect what EC commanded?
        # "ok" | "mismatch" (confirmed, persisted) | "n/a" (not applicable / dry-run).
        self.verification_status: str = "n/a"
        self.last_verification_detail: str | None = None
        self.last_verification_at: datetime | None = None
        self._mismatch_since: datetime | None = None
        # Rate-watch (warn-only): does grid-filling during the cheap window still beat
        # PV-filling? "n/a" until the rates are configured AND read in a cheap-charge tick.
        self.rate_watch_status: str = "n/a"
        self.rate_watch_margin_gbp: float | None = None
        # Start of the current inversion episode, or None while the premise holds. Doubles
        # as the warned-latch (not-None ⇒ already warned) and as the notification's dedupe
        # key, so a recovered-then-recurring inversion gets a fresh key and re-notifies.
        self._rate_watch_inverted_since: datetime | None = None
        # Write-readback: last successfully-commanded value per (kind, target), re-verified
        # against the entity every tick (catches flip-backs at any timescale).
        self._commanded: dict[tuple[str, str], _CommandedWrite] = {}
        # Money accumulators (ledger feature); None unless the costs options are set.
        self.money: MoneyTracker | None = (
            MoneyTracker(hass, self.config) if MoneyTracker.configured(self.config) else None
        )

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
        # Money accumulation runs first and unconditionally: its inputs are independent
        # of the control-loop entities, so a degraded control tick must not stall pricing.
        if self.money is not None:
            self.money.tick(dt_util.now())
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

        # The SoC setpoint is steered every tick, exactly like the discharge cap: the
        # regime is recomputed from the current state and re-asserted, so a failed write
        # retries on the next tick and drift on the entity is corrected within one.
        try:
            setpoint = charge_setpoint(
                state, target_entity=self.config[CONF_BATTERY_CHARGE_CONTROL]
            )
        except Exception as exc:
            self.status = STATUS_ERROR
            self.last_error = repr(exc)
            if self.degraded_since is None:
                self.degraded_since = dt_util.utcnow()
            _LOGGER.exception("Setpoint engine crashed")
            return

        self.last_setpoint_outcome = await self._emit(setpoint)
        self.last_setpoint_decision = setpoint

        if current_regime(state) == REGIME_CHEAP_CHARGE:
            await self._check_rate_economics(state)

        # Pin charge slot 1 always-on, so the charge target acts as a two-sided SoC setpoint
        # rather than a charge ceiling. Re-emitted every tick; the readback loop is what
        # re-writes an externally-edited slot (the dedupe key never changes).
        slot_start = self.config.get(CONF_CHARGE_SLOT_1_START_ENTITY)
        slot_end = self.config.get(CONF_CHARGE_SLOT_1_END_ENTITY)
        if slot_start and slot_end:
            for entity, value in (
                (slot_start, CHARGE_SLOT_PIN_START),
                (slot_end, CHARGE_SLOT_PIN_END),
            ):
                await self._emit(
                    Decision(
                        kind=DecisionKind.SET_SLOT_TIME,
                        target_entity=entity,
                        value=value,
                        reason="Pin charge slot 1 always-on (setpoint regime)",
                        dedupe_key=f"slot-pin-{value}",
                    )
                )

        # Hot-water boost prompt — notify-only; per-day dedupe keeps it to one prompt.
        hot_water_decision = _hot_water_decision(state)
        if hot_water_decision is not None:
            await self._emit(hot_water_decision)

        # Actuation verification: did the meter reflect the discharge cap we just emitted?
        await self._verify_actuation(state)

    async def _check_rate_economics(self, state: SiteState) -> None:
        """Warn (once per inversion episode) when cheap-window economics stop favouring
        fill-mode. Evaluated only in the cheap regime, when the import-rate sensor is by
        definition reading the cheap rate. Warn-only: the regime never changes."""
        import_rate = self._read_rate_state(CONF_IMPORT_RATE_SENSOR)
        export_rate = self._read_rate_state(CONF_EXPORT_RATE_SENSOR)
        if import_rate is None or export_rate is None:
            self.rate_watch_status = "n/a"
            self.rate_watch_margin_gbp = None
            return
        margin = fill_margin_gbp(import_rate, export_rate, efficiency=RATE_WATCH_EFFICIENCY)
        self.rate_watch_margin_gbp = round(margin, 4)
        if margin > 0:
            self.rate_watch_status = "ok"
            # Re-arm only once the margin is clear of the boundary, so a rate hovering
            # around break-even can't flap a fresh warning every tick.
            if margin > RATE_WATCH_REARM_GBP:
                self._rate_watch_inverted_since = None
            return

        self.rate_watch_status = "inverted"
        if self._rate_watch_inverted_since is None:
            self._rate_watch_inverted_since = state.now
        # Re-emitted every inverted tick with the episode-start dedupe key: _emit delivers
        # it once per episode and retries a *failed* notification, which an outer latch
        # would swallow (same reasoning as the actuation-mismatch path above).
        await self._emit(
            Decision(
                kind=DecisionKind.RATE_ECONOMICS_WARNING,
                target_entity="rate_watch",
                value=round(margin * 100, 2),  # pence, for the notification
                reason=(
                    "Cheap-window fill margin is "
                    f"{margin * 100:.2f}p/kWh — grid-filling no longer beats "
                    "PV-filling; review the setpoint strategy"
                ),
                dedupe_key=f"rate-watch-{self._rate_watch_inverted_since.isoformat()}",
            )
        )

    def _read_rate_state(self, conf_key: str) -> float | None:
        """Read a configured rate sensor as GBP/kWh, or None when it can't be priced.

        Unit-normalised via the same helper the money tracker uses: these sensors commonly
        report GBp/kWh, and mixed denominations across the two would otherwise invert the
        margin's sign and raise a false warning.
        """
        entity = self.config.get(conf_key)
        if not entity:
            return None
        raw = self.hass.states.get(entity)
        if raw is None or raw.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""):
            return None
        try:
            value = float(raw.state)
        except TypeError, ValueError:
            return None
        if not math.isfinite(value):
            return None  # a NaN would compare false against zero and read as "inverted"
        return normalise_rate(value, raw.attributes.get("unit_of_measurement"))

    def _check_writes_landed(self, now: datetime) -> VerificationResult | None:
        """Re-verify every commanded setpoint against its entity readback (write-landing).

        The entity reflects the inverter's write-echo within a tick, so after a short settle
        window (which absorbs the transient flip-back signature) a non-matching readback means
        the write was silently rejected or didn't persist. Self-heals once by clearing the
        emit bookkeeping so the M-4 every-tick retry re-issues the command; a second
        *consecutive* failure returns a mismatch verdict, and a healthy readback re-arms the
        budget. Returns ok when all judgeable writes match, None when
        nothing is judgeable (settling / unreadable entities / no commands yet).
        """
        verdict: VerificationResult | None = None
        for key, cmd in self._commanded.items():
            if (now - cmd.written_at).total_seconds() < VERIFY_MISMATCH_SECONDS:
                continue  # write-echo still settling — no verdict for this command yet
            kind, target = key  # kind is a non-identifying label; target is the entity_id
            raw = self.hass.states.get(target)
            state_str: str | None = None
            if raw is not None and raw.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""):
                state_str = raw.state
            # Pass the decision kind, NOT the entity_id, into the detail — it flows into
            # diagnostics + notifications, where the entity_id would re-leak room/device
            # names that the redaction strips (Codex review).
            if kind == DecisionKind.SET_SLOT_TIME.value:
                # Time entities read back an exact "HH:MM:SS" string — compare as strings.
                result = check_time_write_landed(kind, str(cmd.value), state_str)
            else:
                readback: float | None = None
                if state_str is not None:
                    try:
                        readback = float(state_str)
                    except TypeError, ValueError:
                        readback = None
                result = check_write_landed(kind, float(cmd.value), readback)
            if result is None:
                continue  # entity unreadable — no verdict either way
            if not result.ok:
                if cmd.retries < 1:
                    # Self-heal first: re-issue once via the M-4 retry path; flag only if
                    # the retry doesn't stick either.
                    cmd.retries += 1
                    self._emit_state[key].written = None
                    _LOGGER.warning("Write readback failed (%s); re-issuing once", result.detail)
                    continue
                return result
            # A healthy readback means the last re-issue stuck, so re-arm the budget: the next
            # drift gets its own self-heal. Without this the budget is spent for the
            # coordinator's lifetime whenever the commanded value never changes — as the slot
            # pins never do — and a later drift would only ever be flagged (review finding).
            cmd.retries = 0
            verdict = verdict or result
        return verdict

    async def _verify_actuation(self, state: SiteState) -> None:
        """Verify EC's commands took effect; flag a persistent mismatch.

        Two live-mode-only checks, folded into one verdict: write-readback (did each commanded
        setpoint value land — and stay — on its entity?) and the anti-drain actuation check
        (did the battery physically respond?). A landed-mismatch wins (the write didn't even
        stick); mismatches must persist VERIFY_MISMATCH_SECONDS before confirming — one timer
        serving as both settle window and debounce, robust to the variable tick cadence. A
        confirmed mismatch raises the actuation-mismatch binary sensor and notifies once per
        episode.
        """
        if self.write_mode != WRITE_MODE_LIVE:
            self.verification_status = "n/a"
            self.last_verification_detail = None
            self._mismatch_since = None
            return

        actuation = check_actuation(
            state, self.last_discharge_decision, self.last_discharge_outcome
        )
        landed = self._check_writes_landed(state.now)
        if landed is not None and not landed.ok:
            result = landed  # the write didn't even land — the most fundamental failure
        elif actuation is not None:
            result = actuation
        else:
            result = landed  # ok (all judgeable writes match) or None (nothing to verify)
        if result is None:
            self.verification_status = "n/a"
            self.last_verification_detail = None
            self._mismatch_since = None
            return
        if result.ok:
            self.verification_status = "ok"
            self.last_verification_detail = result.detail
            self.last_verification_at = state.now
            self._mismatch_since = None
            return

        # Mismatch — confirm only once it has persisted (settle + debounce in one window).
        if self._mismatch_since is None:
            self._mismatch_since = state.now
        if state.now - self._mismatch_since < timedelta(seconds=VERIFY_MISMATCH_SECONDS):
            return  # not yet persistent; leave the last confirmed verdict in place
        self.verification_status = "mismatch"
        self.last_verification_detail = result.detail
        self.last_verification_at = state.now
        # Notify via _emit, keyed on the episode start (_mismatch_since) — so a recovered-then-
        # recurring mismatch the same day gets a fresh key and re-notifies, and _emit's own
        # bookkeeping handles once-per-episode dedupe + notify-failure retry (no outer latch,
        # which would both block re-notification and swallow a failed delivery — Codex review).
        mismatch = Decision(
            kind=DecisionKind.VERIFICATION_MISMATCH,
            target_entity="actuation",
            # A landed-mismatch can fire without a battery-power sensor; the notifier
            # formats this as watts, so it must always be numeric.
            value=state.battery.power_w if state.battery.power_w is not None else 0,
            reason=result.detail,
            dedupe_key=f"mismatch-{self._mismatch_since.isoformat()}",
        )
        await self._emit(mismatch)

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
        # idempotent). Both writing decisions — the discharge limit and the SoC setpoint —
        # are recomputed and re-emitted on every coordinator tick.
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
                # Record for write-readback verification. A re-issue of the same command
                # keeps its retry budget; a new value resets it.
                prev = self._commanded.get(key)
                value: float | str = (
                    str(decision.value)
                    if decision.kind is DecisionKind.SET_SLOT_TIME
                    else float(decision.value)
                )
                self._commanded[key] = _CommandedWrite(
                    value=value,
                    written_at=self.last_write_at,
                    retries=prev.retries if prev is not None and prev.value == value else 0,
                )
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

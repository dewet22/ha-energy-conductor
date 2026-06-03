"""DataUpdateCoordinator that ties the adapter, core, notifier and writer together."""

from __future__ import annotations

import logging
import random
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .adapter import Adapter, EntityProblem
from .const import (
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_DAILY_KWH_TARGET,
    CONF_DISPATCHING_SENSOR,
    CONF_EV_POWER_SENSOR,
    CONF_MIN_TARGET_SOC_PERCENT,
    CONF_NOTIFY_TARGET,
    CONF_OFF_PEAK_SENSOR,
    CONF_OVERNIGHT_PLAN_TIME,
    CONF_WRITE_MODE,
    COORDINATOR_TICK_SECONDS,
    DEFAULT_DAILY_KWH_TARGET,
    DEFAULT_MIN_TARGET_SOC_PERCENT,
    DEFAULT_OVERNIGHT_PLAN_TIME,
    DOMAIN,
    STATUS_DEGRADED,
    STATUS_ERROR,
    STATUS_OK,
    WRITE_MODE_DRY_RUN,
)
from .decisions import Decision
from .discharge_guard import discharge_limit
from .jitter import hourly_jitter_offset
from .model import SiteState
from .notifier import Notifier
from .overnight import plan_overnight
from .writer import WriteFailure, Writer

_LOGGER = logging.getLogger(__name__)


class EnergyConductorCoordinator(DataUpdateCoordinator[None]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=COORDINATOR_TICK_SECONDS),
        )
        self.entry = entry
        self.config: dict[str, Any] = {**entry.data, **entry.options}
        self.adapter = Adapter(hass, self.config)
        self.notifier = Notifier(
            hass,
            self.config[CONF_NOTIFY_TARGET],
            self.config.get(CONF_WRITE_MODE, WRITE_MODE_DRY_RUN),
        )
        self.writer = Writer(hass, self.config.get(CONF_WRITE_MODE, WRITE_MODE_DRY_RUN))

        self._dedupe: dict[tuple[str, str], str] = {}
        self.status: str = STATUS_OK
        self.last_error: str | None = None
        self.ticks_total: int = 0
        self.notifications_sent: int = 0
        # Notify-dispatch failures are tracked independently of `status`, which a clean
        # tick resets every 30s. These persist so a silently-failing notify target is
        # visible on the diagnostic sensor rather than lost to the log.
        self.notify_failures: int = 0
        self.last_notify_error: str | None = None
        self.last_overnight_plan: Decision | None = None
        self.last_discharge_decision: Decision | None = None
        self.last_site_state: SiteState | None = None

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
        hh, mm, *_ = (int(p) for p in str(plan_time_raw).split(":"))
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
            _LOGGER.warning("Skipping tick: %s", exc)
            return
        except Exception as exc:
            self.status = STATUS_ERROR
            self.last_error = repr(exc)
            _LOGGER.exception("Unexpected error building SiteState")
            raise UpdateFailed(str(exc)) from exc

        self.status = STATUS_OK
        self.last_error = None
        self.last_site_state = state

        try:
            decision = discharge_limit(
                state, target_entity=self.config[CONF_BATTERY_DISCHARGE_LIMIT]
            )
        except Exception as exc:
            self.status = STATUS_ERROR
            self.last_error = repr(exc)
            _LOGGER.exception("Discharge guard crashed")
            return

        await self._emit(decision)
        self.last_discharge_decision = decision

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
                daily_kwh_target=float(
                    self.config.get(CONF_DAILY_KWH_TARGET, DEFAULT_DAILY_KWH_TARGET)
                ),
                min_target_soc_percent=float(
                    self.config.get(CONF_MIN_TARGET_SOC_PERCENT, DEFAULT_MIN_TARGET_SOC_PERCENT)
                ),
            )
        except Exception:
            self.status = STATUS_ERROR
            self.last_error = "plan_overnight crashed (see logs)"
            _LOGGER.exception("plan_overnight crashed")
            return
        await self._emit(decision)
        self.last_overnight_plan = decision

    async def _emit(self, decision: Decision) -> None:
        key = (decision.kind.value, decision.target_entity)
        if self._dedupe.get(key) == decision.dedupe_key:
            return
        notify_ok = await self._notify(decision)
        try:
            await self.writer.write(decision)
        except WriteFailure as exc:
            # Commit the dedupe key so the write (and its failure notification) does not
            # retry every tick. A fresh write attempt happens when the decision value changes.
            self._dedupe[key] = decision.dedupe_key
            _LOGGER.warning("Write failed: %s", exc)
            # Surface as a second notification (per spec §5)
            failure_decision = Decision(
                kind=decision.kind,
                target_entity=decision.target_entity,
                value=decision.value,
                reason=f"WRITE FAILED — {exc}",
                dedupe_key=f"{decision.dedupe_key}-failed",
            )
            await self._notify(failure_decision)
            return
        # Write succeeded. Only commit the dedupe key if the user was actually notified —
        # otherwise a failed notification for a recurring decision would be suppressed
        # forever. Leaving the key uncommitted retries the (idempotent) write + notify on
        # the next tick until the notification gets through.
        if notify_ok:
            self._dedupe[key] = decision.dedupe_key

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

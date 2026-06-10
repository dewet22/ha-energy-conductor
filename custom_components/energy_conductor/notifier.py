"""Decision → HA notify service call."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .const import WRITE_MODE_DRY_RUN
from .decisions import Decision, DecisionKind

_LOGGER = logging.getLogger(__name__)

_KIND_LABEL = {
    DecisionKind.SET_CHARGE_TARGET: "Overnight charge target",
    DecisionKind.SET_DISCHARGE_LIMIT: "Discharge cap",
    DecisionKind.RECOMMEND_HOT_WATER_BOOST: "Hot water boost recommended",
    DecisionKind.VERIFICATION_MISMATCH: "Actuation mismatch",
}


def _format_value(decision: Decision) -> str:
    if decision.kind == DecisionKind.SET_CHARGE_TARGET:
        return f"{decision.value}%"
    if decision.kind == DecisionKind.SET_DISCHARGE_LIMIT:
        return f"{decision.value}W"
    if decision.kind == DecisionKind.RECOMMEND_HOT_WATER_BOOST:
        return f"~{decision.value}h"
    if decision.kind == DecisionKind.VERIFICATION_MISMATCH:
        return f"{decision.value:.0f}W"
    return str(decision.value)  # pragma: no cover - defensive; all kinds handled above


def render_message(decision: Decision, write_mode: str) -> str:
    prefix = "[dry-run] " if write_mode == WRITE_MODE_DRY_RUN else ""
    label = _KIND_LABEL.get(decision.kind, decision.kind.value)
    return f"{prefix}{label} → {_format_value(decision)} ({decision.reason})"


class Notifier:
    def __init__(self, hass: HomeAssistant, notify_target_entity: str, write_mode: str) -> None:
        self.hass = hass
        self.notify_target = notify_target_entity
        self.write_mode = write_mode

    async def notify(self, decision: Decision) -> str | None:
        """Dispatch a notification.

        Returns None on success, or a short error string on failure. Never raises:
        a notification failure must not crash a coordinator tick. The caller surfaces
        the returned error to the diagnostic counters so it isn't lost (the original
        bug was a swallowed exception that left the integration looking dead).
        """
        message = render_message(decision, self.write_mode)
        # notify_target is a notify *entity_id* (EntitySelector(domain='notify')), e.g.
        # 'notify.pixel_9a'. Entity-platform notifiers are driven by the notify.send_message
        # action with the entity_id as target — there is no per-entity 'notify.<name>' service.
        try:
            await self.hass.services.async_call(
                "notify",
                "send_message",
                {"message": message, "title": "Energy Conductor"},
                blocking=True,
                target={"entity_id": self.notify_target},
            )
        except Exception as exc:
            _LOGGER.exception("Notification dispatch failed for decision %s", decision)
            return f"{self.notify_target}: {exc}"
        return None

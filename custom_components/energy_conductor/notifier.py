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
}


def _format_value(decision: Decision) -> str:
    if decision.kind == DecisionKind.SET_CHARGE_TARGET:
        return f"{decision.value}%"
    if decision.kind == DecisionKind.SET_DISCHARGE_LIMIT:
        return f"{decision.value}W"
    return str(decision.value)


def render_message(decision: Decision, write_mode: str) -> str:
    prefix = "[dry-run] " if write_mode == WRITE_MODE_DRY_RUN else ""
    label = _KIND_LABEL.get(decision.kind, decision.kind.value)
    return f"{prefix}{label} → {_format_value(decision)} ({decision.reason})"


class Notifier:
    def __init__(self, hass: HomeAssistant, notify_target_entity: str, write_mode: str) -> None:
        self.hass = hass
        self.notify_target = notify_target_entity
        self.write_mode = write_mode

    async def notify(self, decision: Decision) -> None:
        message = render_message(decision, self.write_mode)
        # Notify targets selected by EntitySelector(domain='notify') are like 'notify.mobile_app_x'
        # Strip the 'notify.' prefix for the service name.
        service_name = (
            self.notify_target.split(".", 1)[1] if "." in self.notify_target else self.notify_target
        )
        try:
            await self.hass.services.async_call(
                "notify",
                service_name,
                {"message": message, "title": "Energy Conductor"},
                blocking=True,
            )
        except Exception:
            _LOGGER.exception("Notification dispatch failed for decision %s", decision)

"""Decision → HA entity write (gated by write_mode)."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .const import WRITE_MODE_LIVE
from .decisions import Decision, DecisionKind

_LOGGER = logging.getLogger(__name__)


class WriteFailure(RuntimeError):
    """Raised when a service call to apply a decision fails."""


class Writer:
    def __init__(self, hass: HomeAssistant, write_mode: str) -> None:
        self.hass = hass
        self.write_mode = write_mode

    async def write(self, decision: Decision) -> None:
        """Apply a decision via HA service call. No-op in dry-run mode."""
        if decision.kind == DecisionKind.RECOMMEND_HOT_WATER_BOOST:
            return  # notify-only decision: surfaced via the Notifier, never written
        if self.write_mode != WRITE_MODE_LIVE:
            return
        if decision.kind in (DecisionKind.SET_CHARGE_TARGET, DecisionKind.SET_DISCHARGE_LIMIT):
            await self._set_number(decision.target_entity, float(decision.value))
        else:
            _LOGGER.warning("Unhandled decision kind: %s", decision.kind)

    async def _set_number(self, entity_id: str, value: float) -> None:
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
        except Exception as exc:
            raise WriteFailure(f"set_value failed for {entity_id}: {exc}") from exc

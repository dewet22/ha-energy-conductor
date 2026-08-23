"""Decision → HA entity write (gated by write_mode)."""

from __future__ import annotations

import logging
import math

from homeassistant.core import HomeAssistant

from .const import WRITE_MODE_LIVE
from .decisions import Decision, DecisionKind

_LOGGER = logging.getLogger(__name__)

# Decisions surfaced via the Notifier only — they never write to hardware.
_NOTIFY_ONLY_KINDS = frozenset(
    {DecisionKind.RECOMMEND_HOT_WATER_BOOST, DecisionKind.VERIFICATION_MISMATCH}
)


class WriteFailure(RuntimeError):
    """Raised when a service call to apply a decision fails."""


class Writer:
    def __init__(self, hass: HomeAssistant, write_mode: str) -> None:
        self.hass = hass
        self.write_mode = write_mode

    async def write(self, decision: Decision) -> None:
        """Apply a decision via HA service call. No-op in dry-run mode."""
        if decision.kind in _NOTIFY_ONLY_KINDS:
            return  # notify-only decision: surfaced via the Notifier, never written
        if self.write_mode != WRITE_MODE_LIVE:
            return
        if decision.kind is DecisionKind.SET_SLOT_TIME:
            # Same failure boundary as the numeric kinds: Decision.value is Any, so a value
            # that isn't a usable time string must surface as WriteFailure.
            if not isinstance(decision.value, str) or not decision.value:
                raise WriteFailure(
                    f"non-string slot time for {decision.target_entity}: {decision.value!r}"
                )
            await self._set_time(decision.target_entity, decision.value)
        elif decision.kind in (DecisionKind.SET_CHARGE_TARGET, DecisionKind.SET_DISCHARGE_LIMIT):
            # Convert inside the failure boundary: Decision.value is Any, and a
            # non-numeric value must surface as WriteFailure (handled upstream),
            # not as an unhandled TypeError/ValueError escaping the coordinator.
            try:
                value = float(decision.value)
            except (TypeError, ValueError) as exc:
                raise WriteFailure(
                    f"non-numeric decision value for {decision.target_entity}: {decision.value!r}"
                ) from exc
            # float("nan")/float("inf") parse fine — this is the last boundary
            # before a hardware write, so reject non-finite values here too
            # (defence-in-depth; the adapter already filters them at ingestion).
            if not math.isfinite(value):
                raise WriteFailure(
                    f"non-finite decision value for {decision.target_entity}: {decision.value!r}"
                )
            await self._set_number(decision.target_entity, value)
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

    async def _set_time(self, entity_id: str, value: str) -> None:
        try:
            await self.hass.services.async_call(
                "time",
                "set_value",
                {"entity_id": entity_id, "time": value},
                blocking=True,
            )
        except Exception as exc:
            raise WriteFailure(f"time.set_value failed for {entity_id}: {exc}") from exc

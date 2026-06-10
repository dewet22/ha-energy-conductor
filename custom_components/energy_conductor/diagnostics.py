"""Config-entry diagnostics — a redacted snapshot for issue reports.

Surfaces the coordinator's runtime state (status, write/notify counters, the last decisions
and their outcomes, and the last SiteState) via HA's one-click "Download diagnostics".
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_NOTIFY_TARGET, DOMAIN
from .decisions import Decision

# The notify service id isn't a secret, but it's the only personal-ish value here; redact it.
TO_REDACT = {CONF_NOTIFY_TARGET}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _json_safe(value: Any) -> Any:
    """Round-trip through JSON so datetimes/enums/tuples become serialisable primitives."""
    return json.loads(json.dumps(value, default=str))


def _decision_dict(decision: Decision | None, outcome: str | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    data = _json_safe(dataclasses.asdict(decision))
    data["outcome"] = outcome
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coord = hass.data[DOMAIN][entry.entry_id]
    return {
        "config": async_redact_data({**entry.data, **entry.options}, TO_REDACT),
        "coordinator": {
            "status": coord.status,
            "last_error": coord.last_error,
            "degraded_since": _iso(coord.degraded_since),
            "ticks_total": coord.ticks_total,
            "write_mode": coord.write_mode,
            "writes_sent": coord.writes_sent,
            "write_failures": coord.write_failures,
            "last_write_at": _iso(coord.last_write_at),
            "last_write_outcome": coord.last_write_outcome,
            "last_write_error": coord.last_write_error,
            "notifications_sent": coord.notifications_sent,
            "notify_failures": coord.notify_failures,
            "last_notify_error": coord.last_notify_error,
        },
        "last_decisions": {
            "discharge": _decision_dict(
                coord.last_discharge_decision, coord.last_discharge_outcome
            ),
            "overnight": _decision_dict(coord.last_overnight_plan, coord.last_overnight_outcome),
            "overnight_plan_at": _iso(coord.last_overnight_plan_at),
        },
        "last_site_state": (
            _json_safe(dataclasses.asdict(coord.last_site_state))
            if coord.last_site_state is not None
            else None
        ),
    }

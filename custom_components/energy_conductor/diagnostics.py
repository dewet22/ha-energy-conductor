"""Config-entry diagnostics — a redacted snapshot for issue reports.

Surfaces the coordinator's runtime state (status, write/notify counters, the last decisions
and their outcomes, and the last SiteState) via HA's one-click "Download diagnostics".
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_NAME, CONF_ENTITY_REFS, DOMAIN
from .decisions import Decision
from .entity_ref import ENTITY_REF_CONF_KEYS

# These dumps are meant to be attached to public issue reports. Entity IDs embed room/device/
# site names (the 2026.6 area-prefix convention bakes the area straight into them), so anything
# carrying an entity reference is identifying. Redact every entity-valued config key (this set
# already includes the notify target), plus the device name and the unique-id anchor map.
TO_REDACT = set(ENTITY_REF_CONF_KEYS) | {CONF_DEVICE_NAME, CONF_ENTITY_REFS}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _redact_error(text: str | None) -> str | None:
    """Error strings are free-form and re-leak identifiers the structured redaction removed —
    WriteFailure embeds the target entity_id, notify errors carry the notify target, and HA
    exception text can contain arbitrary entity IDs. Redact the text in the (publicly-attached)
    dump while preserving None, so "is there an error?" still shows; full detail stays in the logs.
    """
    return REDACTED if text else None


def _json_safe(value: Any) -> Any:
    """Round-trip through JSON so datetimes/enums/tuples become serialisable primitives."""
    return json.loads(json.dumps(value, default=str))


def _decision_dict(decision: Decision | None, outcome: str | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    data = _json_safe(dataclasses.asdict(decision))
    data["target_entity"] = REDACTED  # entity id embeds device/area names
    data["outcome"] = outcome
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    config = async_redact_data({**entry.data, **entry.options}, TO_REDACT)
    coord = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coord is None:
        # Setup failed, so there's no coordinator — still return the (redacted) config, which
        # is often exactly what a failed-setup bug report needs.
        return {"config": config, "coordinator": None}
    return {
        "config": config,
        "coordinator": {
            "status": coord.status,
            "last_error": _redact_error(coord.last_error),
            "degraded_since": _iso(coord.degraded_since),
            "ticks_total": coord.ticks_total,
            "write_mode": coord.write_mode,
            "writes_sent": coord.writes_sent,
            "write_failures": coord.write_failures,
            "last_write_at": _iso(coord.last_write_at),
            "last_write_outcome": coord.last_write_outcome,
            "last_write_error": _redact_error(coord.last_write_error),
            "notifications_sent": coord.notifications_sent,
            "notify_failures": coord.notify_failures,
            "last_notify_error": _redact_error(coord.last_notify_error),
            "verification_status": coord.verification_status,
            "last_verification_detail": coord.last_verification_detail,
            "last_verification_at": _iso(coord.last_verification_at),
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

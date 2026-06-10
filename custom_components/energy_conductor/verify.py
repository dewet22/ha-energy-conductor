"""Actuation verification — did EC's commands take effect? (pure core).

Two checks, both live-mode only:
- **anti-drain** (`check_actuation`): when EC caps discharge at 0 during off-peak (the
  protection that stops the battery feeding the house/EV on cheap grid), the battery must be
  idle. A battery that keeps discharging means the cap never took effect — the failure mode
  behind the EV-drain incident. Battery power is the direct signal; grid is context.
- **write-readback** (`check_write_landed`): for a write that *returned success*, did the
  commanded value actually land on the setpoint entity? The givenergy integration commits the
  inverter's write-echo straight to its cache (no optimistic echo), so the entity reflecting
  the commanded value IS an inverter ACK; the observable failure signature is a flip-back to
  the original value (seconds for a reject, the ~5-min full refresh for non-persistence).
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import VERIFY_DISCHARGE_THRESHOLD_W, VERIFY_READBACK_TOLERANCE
from .decisions import Decision, DecisionKind
from .model import SiteState


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    detail: str


def check_actuation(
    state: SiteState, decision: Decision | None, outcome: str | None
) -> VerificationResult | None:
    """Verify the last discharge actuation against the meter, or None when not applicable.

    Applicable only when EC has a *live* discharge cap of 0 during off-peak and battery power is
    known. A different decision, a dry-run/failed write, on-peak, or no battery-power signal all
    return None (nothing to assert).
    """
    if decision is None or decision.kind is not DecisionKind.SET_DISCHARGE_LIMIT:
        return None
    if decision.value != 0:
        return None  # not a cap-to-0 decision
    if outcome not in ("applied", "unchanged"):
        return None  # the cap isn't live on hardware (dry-run / failed / not yet emitted)
    if not state.tariff.off_peak_now:
        return None  # the cap only applies during off-peak
    power_w = state.battery.power_w
    if power_w is None:
        return None  # no battery-power signal configured

    grid_note = f", grid import {state.grid.import_w:.0f} W" if state.grid is not None else ""
    if power_w > VERIFY_DISCHARGE_THRESHOLD_W:
        return VerificationResult(
            ok=False,
            detail=f"discharge capped at 0 but battery discharging {power_w:.0f} W{grid_note}",
        )
    # Charging is fine under a discharge cap (the cap only forbids discharging) — but call it
    # what it is rather than "idle" when the battery is pulling meaningful power in (Gemini).
    if power_w < -VERIFY_DISCHARGE_THRESHOLD_W:
        return VerificationResult(
            ok=True, detail=f"battery charging at {-power_w:.0f} W{grid_note}"
        )
    return VerificationResult(ok=True, detail=f"battery idle at {power_w:.0f} W{grid_note}")


def check_write_landed(
    label: str, commanded: float, readback: float | None
) -> VerificationResult | None:
    """Compare a commanded setpoint write against the entity's current (read-back) value.

    ``label`` is a NON-identifying control name (the decision kind, e.g. "set_discharge_limit")
    — never the entity_id, which embeds room/device names and would re-leak into the
    (publicly-attached) diagnostics dump + notifications that detail strings flow into.

    ``None`` when the entity can't be read (unavailable/non-numeric) — no verdict either way.
    The caller owns the settle-window timing (don't judge before the write-echo has had time to
    land) and the per-tick re-evaluation that catches late flip-backs.
    """
    if readback is None:
        return None
    if abs(readback - commanded) <= VERIFY_READBACK_TOLERANCE:
        return VerificationResult(ok=True, detail=f"{label} reads {readback:g} as commanded")
    return VerificationResult(
        ok=False,
        detail=f"commanded {label}={commanded:g} but entity reads {readback:g}",
    )

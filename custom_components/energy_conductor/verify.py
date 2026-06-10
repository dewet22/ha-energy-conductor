"""Actuation verification — does the meter reflect what EC commanded? (pure core).

v1 is the anti-drain check. When EC caps discharge at 0 during off-peak (the protection that
stops the battery feeding the house/EV on cheap grid), the battery must be idle. A battery that
keeps discharging means the cap never took effect — the failure mode behind the EV-drain
incident. Battery power is the direct signal; grid import is corroborating context.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import VERIFY_DISCHARGE_THRESHOLD_W
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
    return VerificationResult(ok=True, detail=f"battery idle at {power_w:.0f} W{grid_note}")

"""Actuation verification — does the meter reflect what EC commanded? (pure core).

Two symmetric anti-mis-actuation checks, both off-peak + live-outcome + battery-power gated:
- **anti-drain** (discharge cap): when EC caps discharge at 0, the battery must not be
  discharging — a battery that keeps feeding the house/EV on cheap grid means the cap never
  took effect (the EV-drain incident).
- **charge** (overnight target): when the charge target is live and SoC is below it, the
  battery should be charging from the cheap grid — an idle/discharging battery means the
  charge command didn't take.

Battery power is the direct signal in both; grid import is corroborating context.
``check_actuation`` runs both and returns the worst verdict (a mismatch from either).
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


def _grid_note(state: SiteState) -> str:
    return f", grid import {state.grid.import_w:.0f} W" if state.grid is not None else ""


def check_discharge_actuation(
    state: SiteState, decision: Decision | None, outcome: str | None
) -> VerificationResult | None:
    """Anti-drain: a live discharge cap of 0 during off-peak must leave the battery not
    discharging. None when not applicable (other decision, dry-run/failed, on-peak, no power)."""
    if decision is None or decision.kind is not DecisionKind.SET_DISCHARGE_LIMIT:
        return None
    if decision.value != 0:
        return None  # not a cap-to-0 decision
    if outcome not in ("applied", "unchanged"):
        return None  # the cap isn't live on hardware
    if not state.tariff.off_peak_now:
        return None  # the cap only applies during off-peak
    power_w = state.battery.power_w
    if power_w is None:
        return None

    grid_note = _grid_note(state)
    if power_w > VERIFY_DISCHARGE_THRESHOLD_W:
        return VerificationResult(
            ok=False,
            detail=f"discharge capped at 0 but battery discharging {power_w:.0f} W{grid_note}",
        )
    # Charging is fine under a discharge cap (it only forbids discharging) — but name it.
    if power_w < -VERIFY_DISCHARGE_THRESHOLD_W:
        return VerificationResult(
            ok=True, detail=f"battery charging at {-power_w:.0f} W{grid_note}"
        )
    return VerificationResult(ok=True, detail=f"battery idle at {power_w:.0f} W{grid_note}")


def check_charge_actuation(
    state: SiteState, decision: Decision | None, outcome: str | None
) -> VerificationResult | None:
    """Charge: a live charge target during off-peak with SoC below it must see the battery
    charging. None when not applicable (other decision, dry-run/failed, on-peak, no power, or
    SoC already at/above target so no charging is expected)."""
    if decision is None or decision.kind is not DecisionKind.SET_CHARGE_TARGET:
        return None
    if outcome not in ("applied", "unchanged"):
        return None
    if not state.tariff.off_peak_now:
        return None
    power_w = state.battery.power_w
    if power_w is None:
        return None
    target = decision.value
    soc = state.battery.soc_percent
    if soc >= target:
        return None  # already at/above target — no charging expected

    grid_note = _grid_note(state)
    if power_w > -VERIFY_DISCHARGE_THRESHOLD_W:
        # Idle or discharging when it should be drawing charge from the cheap grid.
        return VerificationResult(
            ok=False,
            detail=(
                f"charge target {target}% but battery not charging "
                f"(SoC {soc:.0f}%, {power_w:.0f} W){grid_note}"
            ),
        )
    return VerificationResult(
        ok=True, detail=f"charging to {target}% (SoC {soc:.0f}%, {-power_w:.0f} W in){grid_note}"
    )


def check_actuation(
    state: SiteState,
    *,
    discharge: Decision | None,
    discharge_outcome: str | None,
    charge: Decision | None,
    charge_outcome: str | None,
) -> VerificationResult | None:
    """Combined verdict across both checks, or None when neither applies. A mismatch from
    either control wins (discharge reported first); otherwise an applicable ok verdict."""
    results = [
        check_discharge_actuation(state, discharge, discharge_outcome),
        check_charge_actuation(state, charge, charge_outcome),
    ]
    applicable = [r for r in results if r is not None]
    if not applicable:
        return None
    for r in applicable:
        if not r.ok:
            return r
    return applicable[0]

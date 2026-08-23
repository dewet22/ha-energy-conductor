"""Two-regime SoC-setpoint engine.

Spec: docs/superpowers/specs/2026-08-23-soc-setpoint-regime-design.md

With charge slot 1 pinned always-on, the inverter's charge-target control behaves as a
two-sided SoC setpoint: below target it grid-charges up, at target it holds (load moves
to grid), above target normal Eco discharge continues. EC steers that one number:

  cheap_charge (off-peak OR dispatch): setpoint 100 — fill; off_peak/eta < export makes
    grid-filling strictly cheaper than PV-filling (the discharge guard holds in parallel).
  self_consume (otherwise): setpoint = the control's own minimum — plain Eco down to the
    hardware reserve, nothing held back through the peak.

The dispatch test is deliberately explicit even though Octopus flips off_peak lock-step
during dispatches — the regime must not depend on that coupling.
"""

from __future__ import annotations

from .decisions import Decision, DecisionKind
from .model import SiteState

REGIME_CHEAP_CHARGE = "cheap_charge"
REGIME_SELF_CONSUME = "self_consume"


def current_regime(state: SiteState) -> str:
    if state.tariff.off_peak_now or state.tariff.ev_dispatching_now:
        return REGIME_CHEAP_CHARGE
    return REGIME_SELF_CONSUME


def charge_setpoint(state: SiteState, *, target_entity: str) -> Decision:
    regime = current_regime(state)
    if regime == REGIME_CHEAP_CHARGE:
        value: float = 100
        reason = "Cheap energy (off-peak/dispatch) — fill to 100%"
    else:
        value = state.battery.charge_target_min_percent
        reason = f"Self-consume — setpoint at control minimum ({value:g}%)"
    return Decision(
        kind=DecisionKind.SET_CHARGE_TARGET,
        target_entity=target_entity,
        value=value,
        reason=reason,
        # `:g` trims the float representation so 4.0 and 4 dedupe identically.
        dedupe_key=f"setpoint-{regime}-{value:g}",
    )

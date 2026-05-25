"""Three-regime discharge limit decision (spec §4.2).

Priority order:
  1. cheap whole-house window      → 0W
  2. EV smart-dispatch + EV drawing → baseline_load_w
  3. default                        → max_discharge_power_w
"""

from __future__ import annotations

from .decisions import Decision, DecisionKind
from .model import SiteState

_BUCKET_W = 100  # dedupe granularity; baseline jitter <100W doesn't re-notify


def _ev_drawing(state: SiteState) -> bool:
    ev = state.ev_charger
    return ev is not None and ev.power_w >= ev.min_activation_power_w


def discharge_limit(state: SiteState, *, target_entity: str) -> Decision:
    if state.tariff.cheap_window_now:
        limit_w = 0
        reason = "Cheap window active — battery idle"
    elif state.tariff.ev_dispatching_now and _ev_drawing(state):
        limit_w = max(0, min(round(state.baseline_load_w), state.battery.max_discharge_power_w))
        reason = f"EV dispatch active — capping discharge at house baseline ({limit_w}W)"
    else:
        limit_w = state.battery.max_discharge_power_w
        reason = "Unconstrained"

    return Decision(
        kind=DecisionKind.SET_DISCHARGE_LIMIT,
        target_entity=target_entity,
        value=limit_w,
        reason=reason,
        dedupe_key=f"discharge-{limit_w // _BUCKET_W}",
    )

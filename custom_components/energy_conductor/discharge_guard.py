"""Three-regime discharge limit decision (spec §4.2).

Priority order:
  1. off-peak rate active     → 0W
  2. pre-off-peak hold window → 0W
  3. default                  → max_discharge_power_w

A former "EV smart-dispatch + EV drawing → cap at baseline" regime was removed:
on a whole-house meter the off-peak rate blankets the *entire* supply during a
dispatch, so regime 1 already idles the battery whenever the EV is grid-charging.
The cap was therefore unreachable (dispatch always coincides with off_peak) and,
on this tariff, economically wrong (it would over-discharge). Dropping it also
means the limit is only ever 0 or max — both of which map cleanly onto a
percentage-based discharge control. See project memory for the full reasoning.
"""

from __future__ import annotations

from datetime import timedelta

from .const import PRE_OFF_PEAK_HOLD_MINUTES
from .decisions import Decision, DecisionKind
from .model import SiteState

_BUCKET_W = 100  # dedupe granularity; baseline jitter <100W doesn't re-notify


def _near_off_peak_start(state: SiteState) -> bool:
    start = state.tariff.next_off_peak_window_start
    if start is None:
        return False
    # Hold only when off-peak starts within the next PRE_OFF_PEAK_HOLD_MINUTES.
    # A past start (e.g. a stale tariff sensor) yields a negative delta — guard the
    # lower bound, else it would wrongly idle the battery during peak.
    delta = start - state.now
    return timedelta(0) <= delta <= timedelta(minutes=PRE_OFF_PEAK_HOLD_MINUTES)


def discharge_limit(state: SiteState, *, target_entity: str) -> Decision:
    if state.tariff.off_peak_now:
        limit_w = 0
        reason = "Off-peak rate active — battery idle"
    elif _near_off_peak_start(state):
        limit_w = 0
        reason = f"Pre-off-peak hold — off-peak starts within {PRE_OFF_PEAK_HOLD_MINUTES} min"
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

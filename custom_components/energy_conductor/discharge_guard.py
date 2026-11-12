"""Two-regime discharge limit decision (spec §4.2).

Priority order:
  1. off-peak rate active OR dispatch → 0W
  2. default                          → max_discharge_power_w

Regime 1's dispatch leg is explicit rather than inherited from Octopus's lock-step
off-peak flag: the guard must idle the battery during a dispatch even if the off-peak
sensor lags or the coupling changes.

Two regimes were removed once their premises didn't hold up:
- An "EV smart-dispatch → cap at baseline" regime: on a whole-house meter the
  off-peak rate blankets the entire supply during a dispatch, so regime 1 already
  idles the battery while the EV grid-charges. The cap was pointless (the battery
  is idle anyway) and would have over-discharged.
- A "pre-off-peak hold" that idled the battery for 30 min before off-peak "to
  enter the cheap period with more charge". But those minutes are still peak,
  where discharging to cover load saves the most (30p), and the held-back charge
  had no payoff — the battery idles through off-peak anyway. It traded the day's
  most valuable discharge for nothing, so we discharge through peak right up to
  off-peak. (If the battery runs out before off-peak, that's fine — the reserve
  floor handles the bottom.)

The limit is now only ever 0 or max — both map cleanly onto a percentage-based
discharge control. See project memory for the full reasoning.

Floor wake: within DISCHARGE_FLOOR_WAKE_MARGIN of the reserve, regime 1 leaves the limit at
max. A GivEnergy inverter at the floor sits in "waiting" and will not start the off-peak fill
while the limit is 0 — two weeks of empty nights in Nov 2026, once a power cut had let the
battery reach the floor before off-peak. There is nothing to protect at the floor, and once
the charge lifts SoC clear of the margin the cap resumes without interrupting it.
"""

from __future__ import annotations

from .const import DISCHARGE_FLOOR_WAKE_MARGIN
from .decisions import Decision, DecisionKind
from .model import SiteState


def discharge_limit(state: SiteState, *, target_entity: str) -> Decision:
    battery = state.battery
    off_peak = state.tariff.off_peak_now or state.tariff.ev_dispatching_now
    at_floor = battery.soc_percent <= battery.reserve_percent + DISCHARGE_FLOOR_WAKE_MARGIN
    if off_peak and at_floor:
        limit_w = battery.max_discharge_power_w
        reason = "Off-peak energy at the reserve floor — cap left open so the fill can start"
    elif off_peak:
        limit_w = 0
        reason = "Off-peak energy (window or dispatch) — battery idle"
    else:
        limit_w = state.battery.max_discharge_power_w
        reason = "Unconstrained"

    return Decision(
        kind=DecisionKind.SET_DISCHARGE_LIMIT,
        target_entity=target_entity,
        value=limit_w,
        reason=reason,
        # Dedupe on the raw limit. With regime 3 (the old variable baseline cap) gone, the
        # limit is only ever 0 or max — no jitter to bucket. Bucketing by //100 here was a
        # latent bug: on a %-based discharge control max reads as 50, so 0 and 50 collapsed
        # to the same bucket and every off-peak transition was deduped away (never written).
        dedupe_key=f"discharge-{limit_w}",
    )

# SoC-setpoint regime — design

**Date:** 2026-08-23
**Status:** approved (design review in chat, 2026-08-23)
**Supersedes:** the overnight just-enough planner as the battery actuation model. An earlier
two-setpoint redesign plan (June 2026) was lost with its plans directory; this spec was
re-derived from memory and went further — the provisioning problem it tried to refine is
dissolved instead.

## Problem

Three user-reported issues, June–August 2026:

1. **Manual override impossible** — EC heals any external write to the charge target within a
   tick, so overriding means disabling EC. *(Symptom of 2 and 3; a dedicated override
   mechanism is parked, likely unnecessary once they're fixed.)*
2. **Dispatches only hold the battery.** During an Octopus Intelligent dispatch the discharge
   guard idles the battery, but EC never *charges* it, forgoing cheap bulk energy.
3. **Overnight under-provisioning.** The just-enough planner trusts the central Solcast
   forecast with no error margin; overcast summer days regularly ran the battery out before
   the evening peak. The "Reserve SoC floor" config field looked like the fix but is (a) a
   hardware descriptor, not a control, and (b) silently overridden by the live reserve sensor
   when one is configured.

## The economic insight that reshaped the design

At the current tariff (off-peak import ~6.9p, export ~12p flat, peak ~30p, round-trip
efficiency ~0.9):

```
off_peak_import / η  ≈ 7.7p   <   export ≈ 12p
```

Every marginal kWh grid-charged during cheap windows is worth more than it costs, *even when
the battery "didn't need it"* — surplus PV that would have charged the battery exports at 12p
instead. Grid-filling the battery at night is strictly cheaper than PV-filling it by day.
The same inequality settled the June EV analysis ("serve the EV from off-peak grid, keep the
battery full to export solar"); it was never applied to the battery itself.

Consequence: the optimal cheap-window setpoint is **100%, every night**. Just-enough
provisioning — forecast, morning gap, deficit calc — is an artefact of tariffs where grid
charging is the expensive option. Issue 3 is not fixed but **dissolved**: the battery starts
every day full.

## Control model

### Firmware semantics (established from years of observed behaviour, not speculation)

On this inverter (GivEnergy hybrid SA2114G047), with a charge slot **active**:

- SoC below target → inverter grid-charges up to target (with built-in tail-off near 100%).
- SoC at target → holds: battery idles, load shifts to grid. No oscillation below target.
- SoC above target → normal Eco discharge continues serving load (multi-year pre-EC
  evidence: overnight slot active, summer SoC above target, battery still drained overnight).

So with an **always-on charge slot**, `charge_target_soc` behaves as a true two-sided SoC
setpoint — the thermostat analogue. `battery_pause_mode` remains unsupported on this
firmware and is not used.

### Regime table

Evaluated every coordinator tick from already-configured sensors:

| Regime | Condition (priority order) | Setpoint | Discharge limit |
|---|---|---|---|
| **Cheap charge** | off-peak sensor on OR dispatch sensor on | 100% | 0 |
| **Self-consume** | otherwise | control minimum (`native_min_value`, ~4%) | max |

- The setpoint is effectively binary: 100 or the control's own floor.
- **Actuation needs no reserve knowledge.** The self-consume setpoint is read from the
  charge-target control entity's own `native_min_value`, not from any reserve config. If the
  target sits below the inverter's actual reserve, the reserve governs anyway (Eco stops
  there) — writing the control to its own minimum is always safe. This removes the class of
  bug where a misdescribed static reserve would be *written* and silently reintroduce a floor.
- Self-consume is plain Eco behaviour, undisturbed: battery serves load down to the hardware
  reserve, discharging through the full peak. **No floor above the reserve** — under
  fill-mode a floor is actively harmful (idles charged battery during 30p peak; the next
  cheap window refills regardless) and would artificially curtail the battery's
  highest-value work.
- Discharge limit 0 during cheap windows prevents wasteful drain while SoC > setpoint is
  converging (spending 12p-worth of stored energy to avoid 6.9p grid).
- Anti-EV-drain semantics are subsumed unchanged (dispatch → discharge 0).
- Slot 1 is pinned **always-on** by EC (heal-once + drift check, not per-tick). Slots cease
  to be a scheduling mechanism. Slot 2 and `battery_soc_reserve` are never written.

### Write mechanics

All writes flow through the existing decision/dedupe/write-readback/heal-once machinery.
Setpoint decisions dedupe per (regime, value) — writes happen on regime transitions only.
Failure modes: stale/unavailable dispatch or off-peak sensor falls through to self-consume
(degrades to plain Eco — safe, never a stuck charge); failed writes surface via existing
readback flagging; an externally-unpinned slot is healed and flagged, worst case the battery
follows Eco defaults (no charge — degraded, not dangerous).

## Rate-watch (warn-only)

The fill-mode strategy is tariff-dependent. EC checks the inequality live and **warns** when
it breaks — it never flips strategy autonomously.

- Evaluated only while in the cheap-charge regime, when `CONF_IMPORT_RATE_SENSOR` is reading
  the cheap rate by definition (Octopus bills the whole supply off-peak during dispatches).
  Compares `import_rate / 0.9` against `CONF_EXPORT_RATE_SENSOR`. **No new config.**
- η = 0.9, constant, not a knob.
- Hysteresis band around the boundary so hovering rates don't flap the notification;
  delivered through the existing `_emit` notify path and surfaced as a status attribute.

## What is removed, kept, changed

**Removed from the actuation path:**
- `plan_overnight`'s target calculation and its nightly SET_CHARGE_TARGET decision.
- The planned p10-forecast + learned-bias planner improvements (fixes for provisioning;
  nothing left to fix).

**Kept:**
- Discharge guard (as the cheap-window hold lever — condition extended to dispatch-or-off-peak
  if not already equivalent).
- Baseline-load learning and solar-forecast plumbing (the tape's SoC projection needs both).
- All write observability, verify, and money-tracking machinery.

**Changed:**
- `project_soc` (tape SoC projection) rewritten to the regime model: charge toward 100%
  during cheap windows at the observed charge rate, discharge at baseline toward reserve
  otherwise. Should be *more* honest than the planner-based projection.
- Overnight-plan sensor reshaped into regime terms: current regime, setpoint, rate-watch
  verdict. The Tonight view's "Charge target tonight" row becomes a regime/setpoint row.
- `_STRATEGY_VERSION` bumps with the JS changes.

## Config surface

- **New:** charge-slot-1 start/end entity pickers (Battery section) so EC can pin and heal
  the slot. Schema + `_KEYS` whitelist + `en.json` — all three, per the standing lesson.
- **Demoted to display-only:** the reserve (live sensor preferred, static "Reserve SoC
  floor (%)" as fallback for installs whose ecosystem exposes no reserve entity) now feeds
  only usable-energy calcs and the projection floor — actuation never reads it. Help text
  states explicitly that the live sensor takes precedence and that this is *not* a
  minimum-SoC control. Migration note: the user's stored 40 should still be reset to the
  true hardware value (4) for hygiene, though it is now inert in every path on
  sensor-equipped installs.
- No floor knob. No dispatch-target knob (hardcoded 100). No η knob.

## Accepted costs and caveats (named, not hidden)

- **Inverter AC clipping:** 7 kWp array behind a ~5.x kW AC inverter. With the battery full
  from dawn, DC generation above the AC ceiling clips on clear summer middays (order
  1–3 kWh ≈ 12–36p on the best days) where battery headroom would have absorbed it.
  Accepted: fill-mode gains (~4.3p/kWh × several kWh nightly) dominate on all other days.
  *Possible future refinement:* cap the fill target below 100% when tomorrow's forecast peak
  exceeds the AC rating.
- **Tariff dependence:** the whole strategy rides on `off_peak/η < export`. The rate-watch
  makes a break visible; re-introducing provisioning would be a deliberate future project
  (this spec's Removed section is the map back).
- **Battery cycling:** nightly deep cycles to 100% are accepted by the owner (unlimited-cycle
  guarantee, vendor defunct, BMS tail-off observed working).
- **Recorder history:** regime changes take effect from deploy; tape projection honesty
  improves going forward only.

## Migration / cutover

1. **Dry-run A/B (a few days):** deploy with `write_mode: dry_run`. The regime engine
   logs/notifies intended setpoint + limit writes at each transition while the legacy givtcp
   automations keep running. Compare "EC would have done X" against observed behaviour.
2. **Cutover (an evening, user driving):** enumerate exactly what the legacy automations
   (`limit_givenergy_battery_discharge_during_octopus_intelligent_dispatching` +
   `restore_full_…` and the charge-window pair) write; confirm the regime engine covers each
   write; disable them; flip EC live; EC pins slot 1 always-on; watch one full off-peak
   cycle. Reset the stored Reserve-SoC-floor 40 → 4.
3. **Rollback:** re-enable the automations, EC back to dry-run. No state to unwind beyond
   slot times.

These automations are the last givtcp references in HA; their retirement completes the
de-givtcp migration (givtcp itself kept as a read-only safety blanket until separately
decided).

## Testing

- **Pure core:** regime-engine matrix (off-peak × dispatch × staleness — stale inputs must
  degrade to self-consume, never a stuck charge); setpoint/limit decision dedupe across
  regime transitions; rate-watch inequality + hysteresis; slot-pinning heal logic.
- **Config flow:** new slot-picker fields get the three-things guard tests (schema, `_KEYS`,
  `en.json`).
- **JS:** rewritten `project_soc`-fed tape projection (charge ramp, hold at 100, discharge to
  reserve; DST-pinned TZ as established).
- **Live checklist (cutover evening):** slot accepted as always-on; setpoint write lands
  (readback); charge starts on dispatch outside the fixed window; discharge resumes at
  window end; rate-watch attribute populated.

## Parked

- Manual override mechanism (likely unnecessary now; revisit only if a real need survives
  fill-mode).
- Sunny-day headroom refinement (see clipping caveat).
- Options-flow menu-return refactor (separate pre-existing thread, unaffected).

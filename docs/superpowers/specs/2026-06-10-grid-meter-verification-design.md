# Design: meter-side grid observability + active actuation verification

## Context

EC actuates the inverter autonomously (charge target, discharge limit) but has no view of what
actually happens **at the meter**. The motivating incident: during an off-peak EV dispatch the
battery drained ~2.65 kW into the EV despite EC computing a correct `discharge = 0` decision — the
write was silently deduped away, and nothing flagged that the actuator hadn't obeyed. The
write-outcome observability work (PR #14) made "did EC *issue* the write?" answerable; this closes
the loop with "did the write actually *take effect* in the real world?"

`givenergy-hass` now exposes grid flow as two always-positive sensors (`grid_power_import` /
`grid_power_export`), having hidden the combined signed `grid_power` (#151) — so the two-sensor form
is the supported, sign-unambiguous, future-proof input. EC has **no grid input today**; this adds one
for observation + verification, **not** for planning.

**Depends on PR #14** (write-outcome observability): reuses `last_write_at` (the settle signal), the
"Control status" dashboard card, and the diagnostics dump. Implement after #14 merges (or stacked on it).

## Goals / non-goals

- **Goal:** surface live grid import/export + battery power alongside EC's decisions, and actively
  verify that a `discharge = 0` actuation actually idled the battery — flagging persistent mismatches.
- **Non-goal:** no change to planning or discharge logic. EC observes, surfaces, and notifies; it does
  **not** re-act on a mismatch (the user can automate off the binary sensor). One assertion only for v1.

## Decisions (settled in brainstorming)

- **Two-sensor grid input** (import + export, always-positive), not the combined signed sensor.
- **Verification signal = battery power (direct)**, grid import as corroborating context. The cap is
  proven by the battery being idle, not by inference from grid-vs-load (murky when EV draw isn't in the
  home-load sensor — exactly the incident scenario).
- **Battery-power sign: an invert toggle.** EC's internal convention is `+ve = discharging`; a config
  boolean `battery_power_positive_is_charging` (default `False`) negates at the adapter so any sensor
  convention works — sign-safe by construction.
- **Escalation: binary sensor (`device_class=problem`) + one-shot notify** on persistent mismatch.

## Design

### 1. Inputs — config + model + adapter

New **optional** config keys (inert when unset, like EV/hot-water); the entity keys are added to
`entity_ref.py`'s `ENTITY_REF_CONF_KEYS` so they inherit unique-id rename-resilience **and** automatic
diagnostics redaction (`diagnostics.py` `TO_REDACT` already derives from that set):
- `grid_import_sensor`, `grid_export_sensor` — W, always-positive.
- `battery_power_sensor` — W.
- `battery_power_positive_is_charging` — bool, default `False` (non-entity; not anchored/redacted).

Model (`model.py`):
- New frozen `GridState(import_w: float, export_w: float)` with `net_w` property (`import_w − export_w`,
  **+ve = import**). `SiteState.grid: GridState | None = None`.
- `Battery.power_w: float | None = None` — EC convention **+ve = discharging**. Optional default keeps
  existing construction + `__post_init__` invariants unaffected.

Adapter (`adapter.py`):
- `_grid_state() -> GridState | None`: read both sensors via `_read_float`; `None` (feature off) if
  either is unconfigured/unavailable. Wired into `build_site_state`.
- Battery power: read `battery_power_sensor` via `_read_float` (optional); negate when
  `battery_power_positive_is_charging` is set; pass as `Battery(power_w=…)`. `None` when unconfigured.

### 2. Verification core — `verify.py` (NEW, pure, no HA imports, ≥90% cov)

`check_actuation(state, decision, outcome) -> VerificationResult | None`, where `VerificationResult`
is a frozen dataclass `(ok: bool, detail: str)`. v1 assertion — the **anti-drain check**:

> Applies when the last discharge decision capped discharge at **0**, its outcome was **`applied`**,
> and it is off-peak. Expectation: the battery must **not** be discharging — if
> `battery.power_w > VERIFY_DISCHARGE_THRESHOLD_W` (~150 W) → `ok=False`,
> detail `"discharge capped at 0 but battery discharging {x} W (grid import {y} W)"`.

Returns `None` when not applicable (cap not 0, not off-peak, not applied) or inputs missing
(grid/battery unconfigured). Pure and instantaneous — timing/debounce live in the coordinator.
Charge-target and peak-discharge assertions are **deferred**.

### 3. Timing & debounce — coordinator (`coordinator.py`)

The pure check is instantaneous; the coordinator owns the timing so transients don't cry wolf:
- **Settle gate:** only run once the write has had time to take effect —
  `now − last_write_at ≥ VERIFY_SETTLE_SECONDS` (~60 s). Reuses the PR-#14 `last_write_at`.
- **Debounce:** require `VERIFY_MISMATCH_TICKS` (~3) consecutive mismatches before escalating —
  absorbs sensor poll skew / momentary battery activity.
- New state: `verification_status` (`ok` / `mismatch` / `n/a`), `last_verification_detail`,
  `_consecutive_mismatches`, `last_verification_at`. On reaching the threshold: set the binary-sensor
  state and fire a one-shot notify (reuse Notifier + the existing dedupe, keyed per mismatch episode).

### 4. Surfacing

- **`binary_sensor.py`:** new `ActuationMismatchBinarySensor` (`device_class=problem`), state =
  persistent-mismatch latch; attrs = detail + last_verification_at. Idiomatic HA "something's wrong"
  entity the user can alert on.
- **Notify** once per mismatch episode (persistent), via the Notifier.
- **`diagnostics.py`:** grid state + battery power ride the `SiteState` snapshot automatically; add the
  verification fields to the coordinator block.
- **Dashboard (`www/ec-strategy.js`):** extend the "Control status" card — a "Grid now: importing X W"
  line and an "Actuation: OK / MISMATCH (detail)" line. Same validated-entity-id Jinja pattern; bump
  `_STRATEGY_VERSION` 5 → 6.

### 5. Config flow + translations

Add the three new fields to the wizard + options menu. `battery_power_sensor` +
`battery_power_positive_is_charging` fit the **Battery** group; the two grid sensors warrant a small
**Grid** group (or fold into Battery). Entity selectors get `entity_ref` anchoring for free.

## Files

**New:** `verify.py`; `tests/core/test_verify.py`; tests for the binary sensor + adapter grid reads.
**Modified:** `const.py` (3 keys + 3 thresholds), `model.py` (`GridState`, `Battery.power_w`),
`adapter.py`, `entity_ref.py` (anchor/redact the entity keys), `coordinator.py` (run + settle +
debounce + notify), `binary_sensor.py`, `diagnostics.py`, `www/ec-strategy.js` + `__init__.py`
(`_STRATEGY_VERSION`), `config_flow.py`, `translations/en.json`, `manifest.json` (→ 0.6.0).

## Tests

- **`test_verify.py`** (pure, full branch cov): cap=0 + off-peak + battery discharging → mismatch;
  battery idle → ok; not-applicable cases (cap≠0, not off-peak, outcome≠applied, grid/battery missing)
  → `None`.
- **coordinator:** settle gate suppresses the check before `VERIFY_SETTLE_SECONDS`; debounce requires K
  consecutive mismatches before the binary sensor latches + a single notify fires; recovery clears it.
- **adapter:** `GridState` built from two sensors; `None` when partial/unavailable; battery-power sign
  inversion honoured.
- **binary sensor:** reflects `verification_status`; attrs populated.
- **diagnostics/JS:** grid + verification appear in the dump; the Control status card renders the grid +
  actuation lines safely (validated id).

## Verification

`uv run ruff check --fix && uv run ruff format`; `uv run pytest --cov` (≥90%); `npx vitest run`.
Live (after deploy): configure the three sensors; confirm grid import/export + battery power flow into
the diagnostics dump and the Control status card. Force the failure mode (cap discharge to 0 off-peak
while the battery is discharging) and confirm the binary sensor latches + one notification after the
debounce window; confirm recovery clears it. Verify the invert toggle against the live battery sensor's
actual sign.

## Out of scope (deferred)

- Charge-target verification (SoC < target off-peak → expect grid-import elevated) and peak-discharge
  checks — add as further `verify.py` assertions once the anti-drain check is proven live.
- Any control/automatic reaction to a mismatch — surface + notify only.
- Re-exposing grid import/export as EC's own sensors (redundant with givenergy-hass) — surfaced in
  diagnostics + dashboard instead.

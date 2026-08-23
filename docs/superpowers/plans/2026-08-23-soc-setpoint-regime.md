# SoC-Setpoint Regime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the just-enough overnight planner with a two-regime SoC-setpoint controller: cheap windows (off-peak OR dispatch) fill the battery to 100% behind an always-on charge slot; otherwise plain Eco down to the charge control's own minimum. Plus a warn-only rate-watch and slot-1 pinning.

**Architecture:** Pure-core decision functions (`regimes.py`, extended `discharge_guard.py`, `rate_watch.py`) consumed by the coordinator every tick, exactly like today's discharge guard. The overnight planner (`plan_overnight`, its schedules, caching, and freshness machinery) is deleted from the actuation path. `project_soc` and baseline/forecast learning stay (tape projection). A new `SET_SLOT_TIME` decision kind writes `time.set_value` to pin charge slot 1 always-on, verified by the existing write-readback loop extended to string values.

**Tech Stack:** Python (HA custom integration, pytest + pytest-asyncio via `uv`), vanilla JS cards (vitest), voluptuous config flow.

**Spec:** `docs/superpowers/specs/2026-08-23-soc-setpoint-regime-design.md` — read it first; it carries the economic rationale, firmware-semantics evidence, and accepted caveats that justify every task below.

## Global Constraints

- Python: run `uv run ruff check --fix` AND `uv run ruff format` before every commit; run `uv run pytest` (full suite — it's fast) after each task; `tox` before the final commit.
- JS: `npm test` runs vitest; suite is TZ-pinned (Europe/London) — do not unpin.
- **Any bundled-JS change requires a `_STRATEGY_VERSION` bump in `custom_components/energy_conductor/__init__.py`** (one bump for the whole feature, done in Task 6; verify the current value there before incrementing).
- **A config field needs THREE things**: schema entry, `_KEYS` whitelist entry, `translations/en.json` labels — missing any one fails silently. New *entity* fields additionally need `entity_ref.py` `SCALAR_ENTITY_CONF_KEYS` membership for unique_id anchoring.
- Diagnostics privacy: detail/reason strings use decision kinds or generic labels, NEVER entity_ids.
- Commits: conventional commits (`feat:`/`fix:`/`refactor:`/`docs:`), one logical change each, `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer.
- Don't touch `.github/workflows/`.
- The Bash shell is zsh: don't name a variable `status`; quote glob-looking args.

---

### Task 1: Regime engine (pure core)

**Files:**
- Create: `custom_components/energy_conductor/regimes.py`
- Modify: `custom_components/energy_conductor/discharge_guard.py`
- Modify: `custom_components/energy_conductor/model.py` (add `Battery.charge_target_min_percent`)
- Test: `tests/core/test_regimes.py` (new), `tests/core/test_discharge_guard.py` (extend)

**Interfaces:**
- Consumes: `SiteState`, `Decision`, `DecisionKind.SET_CHARGE_TARGET` (all existing).
- Produces:
  - `regimes.current_regime(state: SiteState) -> str` returning `"cheap_charge"` when `state.tariff.off_peak_now or state.tariff.ev_dispatching_now`, else `"self_consume"`.
  - `regimes.charge_setpoint(state: SiteState, *, target_entity: str) -> Decision` — kind `SET_CHARGE_TARGET`, value `100` (cheap_charge) or `state.battery.charge_target_min_percent` (self_consume), dedupe_key `f"setpoint-{regime}-{value}"`.
  - `Battery.charge_target_min_percent: float = 4.0` (new field, validated 0–100).
  - Task 3 wires both functions into the coordinator; Task 2 populates the new field.

- [ ] **Step 1: Write the failing tests**

Check `tests/core/builders.py` first for the existing `SiteState`/`Battery`/`TariffState` builder helpers and use them (they exist — every core test does); the snippets below assume a `make_state(off_peak_now=..., ev_dispatching_now=..., ...)`-style builder, adapt names to what's actually there.

```python
# tests/core/test_regimes.py
"""Regime engine: cheap windows fill to 100, otherwise get out of the way."""

from custom_components.energy_conductor.decisions import DecisionKind
from custom_components.energy_conductor.regimes import charge_setpoint, current_regime

from .builders import make_state  # adapt to the real builder name/signature

TARGET = "number.charge_target"


def test_off_peak_is_cheap_charge():
    state = make_state(off_peak_now=True, ev_dispatching_now=False)
    assert current_regime(state) == "cheap_charge"


def test_dispatch_alone_is_cheap_charge():
    # Dispatch outside the fixed window: off_peak usually flips lock-step, but the
    # regime must not depend on that coupling.
    state = make_state(off_peak_now=False, ev_dispatching_now=True)
    assert current_regime(state) == "cheap_charge"


def test_neither_is_self_consume():
    state = make_state(off_peak_now=False, ev_dispatching_now=False)
    assert current_regime(state) == "self_consume"


def test_cheap_charge_setpoint_is_100():
    state = make_state(off_peak_now=True, ev_dispatching_now=False)
    d = charge_setpoint(state, target_entity=TARGET)
    assert d.kind is DecisionKind.SET_CHARGE_TARGET
    assert d.target_entity == TARGET
    assert d.value == 100
    assert d.dedupe_key == "setpoint-cheap_charge-100"


def test_self_consume_setpoint_is_control_minimum():
    state = make_state(
        off_peak_now=False, ev_dispatching_now=False, charge_target_min_percent=4.0
    )
    d = charge_setpoint(state, target_entity=TARGET)
    assert d.value == 4.0
    assert d.dedupe_key == "setpoint-self_consume-4"


def test_setpoint_reason_mentions_regime():
    state = make_state(off_peak_now=True, ev_dispatching_now=False)
    assert "cheap" in charge_setpoint(state, target_entity=TARGET).reason.lower()
```

Add to `tests/core/test_discharge_guard.py` (match its existing builder usage):

```python
def test_dispatch_alone_idles_battery():
    # A dispatch with off_peak somehow false must still idle the battery — the
    # regime no longer relies on Octopus's lock-step off-peak flag.
    state = make_state(off_peak_now=False, ev_dispatching_now=True)
    d = discharge_limit(state, target_entity="number.limit")
    assert d.value == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_regimes.py tests/core/test_discharge_guard.py -v`
Expected: `test_regimes` errors with `ModuleNotFoundError: regimes`; the new discharge test FAILS (value == max, not 0). If `make_state` lacks `charge_target_min_percent`/`ev_dispatching_now` parameters, extend the builder — `Battery.charge_target_min_percent` doesn't exist yet, so builder changes land with Step 3.

- [ ] **Step 3: Implement**

`custom_components/energy_conductor/model.py` — add to `Battery` (after `power_w`):

```python
    # The charge-target control entity's own minimum (its `min` attribute) — the lowest
    # setpoint the inverter accepts. The self-consume regime writes this value: "get out
    # of the way" expressed as the control's own floor. The hardware reserve governs the
    # actual discharge stop, so a setpoint at/below reserve is always safe. (Spec 2026-08-23.)
    charge_target_min_percent: float = 4.0
```

and in `Battery.__post_init__` append:

```python
        if not 0 <= self.charge_target_min_percent <= 100:
            raise ValueError(
                "Battery.charge_target_min_percent must be in [0, 100] "
                f"(got {self.charge_target_min_percent!r})"
            )
```

Create `custom_components/energy_conductor/regimes.py`:

```python
"""Two-regime SoC-setpoint engine (spec docs/superpowers/specs/2026-08-23-soc-setpoint-regime-design.md).

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
        dedupe_key=f"setpoint-{regime}-{value:g}",
    )
```

(`:g` renders `100` and `4.0` as `100`/`4`, matching the Step 1 assertions.)

`custom_components/energy_conductor/discharge_guard.py` — change the condition and reason:

```python
    if state.tariff.off_peak_now or state.tariff.ev_dispatching_now:
        limit_w = 0
        reason = "Cheap energy (off-peak/dispatch) — battery idle"
```

and update the module docstring's priority list to say "off-peak rate active OR dispatch → 0W" (the dispatch leg is now explicit rather than inherited from Octopus's lock-step off-peak flag).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/ -v`
Expected: new tests PASS; pre-existing discharge-guard tests still pass (their states have `ev_dispatching_now=False`). If an existing test asserted the old reason string "Off-peak rate active — battery idle", update it to the new string.

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff check --fix && uv run ruff format && uv run pytest -q
git add custom_components/energy_conductor/regimes.py custom_components/energy_conductor/discharge_guard.py custom_components/energy_conductor/model.py tests/core/
git commit -m "feat: two-regime SoC-setpoint engine (pure core)"
```

---

### Task 2: Adapter reads the charge control's minimum

**Files:**
- Modify: `custom_components/energy_conductor/adapter.py` (add `_min_attr`, wire into `Battery`)
- Test: `tests/integration/test_adapter_input_validation.py` (or a new `tests/integration/test_adapter_min_attr.py` if that file's fixtures don't fit)

**Interfaces:**
- Consumes: `_max_attr` pattern at `adapter.py:188`, `CONF_BATTERY_CHARGE_CONTROL`.
- Produces: `Battery.charge_target_min_percent` populated from the charge-control entity's `min` attribute, default `4.0`, clamped to [0, 100]. Task 3's coordinator relies on this being present on every built `SiteState`.

- [ ] **Step 1: Write the failing tests**

Follow the existing adapter-test fixture style (see `tests/integration/test_adapter_reserve.py` for how entities are seeded). Three cases:

```python
async def test_charge_target_min_read_from_control(hass, ...):
    # charge control entity has attributes {"min": 4, "max": 100}
    state = await adapter.build_site_state()
    assert state.battery.charge_target_min_percent == 4.0


async def test_charge_target_min_defaults_when_missing(hass, ...):
    # charge control entity has no "min" attribute
    state = await adapter.build_site_state()
    assert state.battery.charge_target_min_percent == 4.0


async def test_charge_target_min_clamped(hass, ...):
    # "min" attribute is -50 (bogus upstream) -> clamped to 0
    state = await adapter.build_site_state()
    assert state.battery.charge_target_min_percent == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/ -k min -v`
Expected: FAIL — `charge_target_min_percent` is the dataclass default regardless of attributes (first test passes by coincidence only if the fixture uses 4; make the first fixture use `"min": 10` and assert `10.0` so it genuinely fails).

- [ ] **Step 3: Implement**

In `adapter.py`, next to `_max_attr` (line ~188):

```python
def _min_attr(hass: HomeAssistant, entity_id: str, default: float) -> float:
    """The entity's `min` attribute as a percent, clamped to [0, 100]; `default` if absent."""
    state = hass.states.get(entity_id)
    if state is None:
        return default
    raw = state.attributes.get("min")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return max(0.0, min(100.0, value))
```

(`math` is already imported? Verify — `adapter.py` may not import it; add `import math` if absent.) Wire into `build_site_state`'s `Battery(...)` construction:

```python
            charge_target_min_percent=_min_attr(
                self.hass, self.config[CONF_BATTERY_CHARGE_CONTROL], default=4.0
            ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/ -q`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix && uv run ruff format && uv run pytest -q
git add custom_components/energy_conductor/adapter.py tests/integration/
git commit -m "feat: adapter reads the charge control's own minimum into Battery"
```

---

### Task 3: Coordinator — setpoint per tick, overnight planner removed

The largest task. The coordinator gains a per-tick setpoint emission (mirroring the discharge guard) and loses the entire overnight-planning apparatus: scheduled runs, hourly jitter re-evaluation, startup catch-up, plan caching, freshness-gated retry, and the stale-plan readback cleanup.

**Files:**
- Modify: `custom_components/energy_conductor/coordinator.py`
- Modify: `custom_components/energy_conductor/overnight.py` (delete `plan_overnight`, `_morning_gap_hours`, `_first_meaningful_slot_at_or_after`; keep `project_soc`, `_is_off_peak_at`, `_forecast_kw_at`)
- Modify: `custom_components/energy_conductor/const.py` (delete `MIN_OVERNIGHT_USABLE_KWH`, `MORNING_GAP_CAP_H`/`MISSING_FORECAST_GAP_H` move out with their functions — they live in overnight.py, delete there)
- Delete: `custom_components/energy_conductor/jitter.py` + `tests/core/test_jitter.py` (only consumer was the hourly re-plan)
- Modify: `custom_components/energy_conductor/notifier.py` (kind label)
- Test: `tests/integration/test_coordinator.py` (rework), `tests/core/test_overnight.py` (delete planner tests; keep/move nothing — projection tests live in `tests/core/test_projection.py`)

**Interfaces:**
- Consumes: `regimes.charge_setpoint`, `regimes.current_regime` (Task 1).
- Produces (coordinator attributes Task 4's sensors read):
  - `self.last_setpoint_decision: Decision | None` and `self.last_setpoint_outcome: str | None` — **replacing** `last_overnight_plan`/`last_overnight_outcome`/`last_overnight_plan_at`.
  - `regimes.current_regime(self.last_site_state)` is how sensors derive the regime string.
- Removed (Task 4 must not reference): `last_overnight_plan`, `last_overnight_plan_at`, `last_overnight_outcome`, `_PLAN_RETRY_MAX_AGE`, `_run_overnight_plan`.

- [ ] **Step 1: Write the failing coordinator tests**

Rework `tests/integration/test_coordinator.py` in place (read it first; reuse its fixtures/mocks). New/changed behaviours to cover — write these before touching the coordinator:

```python
async def test_setpoint_written_every_regime(hass, coordinator_fixture):
    # off-peak tick -> SET_CHARGE_TARGET value 100 emitted alongside discharge 0
    # peak tick -> SET_CHARGE_TARGET value == battery.charge_target_min_percent, discharge max


async def test_setpoint_transition_writes_once(hass, coordinator_fixture):
    # two consecutive off-peak ticks -> exactly one hardware write (dedupe "unchanged")


async def test_dispatch_only_tick_sets_setpoint_100(hass, coordinator_fixture):
    # off_peak False, dispatching True -> setpoint 100 (regime explicit, not lock-step)


async def test_hot_water_prompt_still_fires(hass, coordinator_fixture):
    # boost_recommended state -> RECOMMEND_HOT_WATER_BOOST notified from the tick path


async def test_no_overnight_schedule_registered(hass, coordinator_fixture):
    # async_start registers no time-change listeners for planning any more
    # (assert on the count/absence per the fixture's listener capture)
```

Also delete the tests in `test_coordinator.py` that exercise `_run_overnight_plan`, plan retries, `_PLAN_RETRY_MAX_AGE` gating, and the stale-plan `_commanded` cleanup — they test deleted machinery. In `tests/core/test_overnight.py`, delete tests of `plan_overnight`/`_morning_gap_hours` (the whole file if nothing else remains; `test_projection.py` covers `project_soc`).

- [ ] **Step 2: Run to verify the new tests fail**

Run: `uv run pytest tests/integration/test_coordinator.py -v`
Expected: new tests FAIL (no setpoint emission yet); deleted-machinery tests are gone.

- [ ] **Step 3: Implement the coordinator rewiring**

In `coordinator.py`:

1. **Imports:** drop `plan_overnight`, `hourly_jitter_offset`, `random`, `CONF_OVERNIGHT_PLAN_TIME`, `DEFAULT_OVERNIGHT_PLAN_TIME`, `parse_hh_mm` (verify no other consumer of `parse_hh_mm` in this file — `adapter` owns it); add `from .regimes import charge_setpoint`.
2. **`__init__`:** replace `last_overnight_outcome`/`last_overnight_plan`/`last_overnight_plan_at` with `self.last_setpoint_decision: Decision | None = None` and `self.last_setpoint_outcome: str | None = None`. Delete `_PLAN_RETRY_MAX_AGE` (module level).
3. **`async_start`:** delete the scheduled-plan and hourly-jitter registrations and the immediate-plan call; keep only the state-change listener block.
4. **`_async_update_data`:** after the discharge emission, add:

```python
        try:
            setpoint = charge_setpoint(
                state, target_entity=self.config[CONF_BATTERY_CHARGE_CONTROL]
            )
        except Exception as exc:
            self.status = STATUS_ERROR
            self.last_error = repr(exc)
            if self.degraded_since is None:
                self.degraded_since = dt_util.utcnow()
            _LOGGER.exception("Setpoint engine crashed")
            return

        self.last_setpoint_outcome = await self._emit(setpoint)
        self.last_setpoint_decision = setpoint

        # Hot-water boost prompt — notify-only; per-day dedupe keeps it to one prompt.
        hot_water_decision = _hot_water_decision(state)
        if hot_water_decision is not None:
            await self._emit(hot_water_decision)
```

   Delete the startup catch-up block (`if self.last_overnight_plan is None: ...`) and the freshness-gated plan retry block.
5. **Delete `_run_overnight_plan` entirely.**
6. **`_check_writes_landed`:** delete the stale-plan cleanup block at the top (the setpoint is re-asserted every tick now, exactly like the discharge limit — the readback self-heal covers drift with no freshness caveat).
7. **`notifier.py`:** `_KIND_LABEL[DecisionKind.SET_CHARGE_TARGET]` becomes `"Battery SoC setpoint"`.
8. **`overnight.py`:** delete `plan_overnight`, `_morning_gap_hours`, `_first_meaningful_slot_at_or_after`, and the three module constants `MEANINGFUL_SLOT_KWH`, `MORNING_GAP_CAP_H`, `MISSING_FORECAST_GAP_H`; update the module docstring ("SoC projection for the mission tape" is what remains). Delete `MIN_OVERNIGHT_USABLE_KWH` from `const.py`.
9. **Delete `jitter.py` and `tests/core/test_jitter.py`** (`git rm`).
10. **Grep for stragglers**: `grep -rn "last_overnight\|plan_overnight\|MIN_OVERNIGHT\|jitter\|OVERNIGHT_PLAN_TIME" custom_components/ tests/` — `diagnostics.py` and `sensor.py` will hit; `sensor.py` is Task 4's job (leave it failing there is NOT ok — if `sensor.py` references `last_overnight_plan`, do the minimal rename in this task: point `OvernightPlanSensor` at `last_setpoint_decision`/`last_setpoint_outcome` so the suite stays green; Task 4 does the real reshaping). Fix `diagnostics.py` references to the renamed fields in this task.

Behaviour note (expected, not a bug): the hot-water prompt now evaluates every tick instead of at the nightly plan time; its per-day dedupe key keeps it to one prompt per day, which may arrive earlier in the day than before.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. Deleted-machinery test count drops; no import errors anywhere (`sensor.py`/`diagnostics.py` compile against the renamed fields).

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix && uv run ruff format && uv run pytest -q
git add -A custom_components/energy_conductor tests
git commit -m "feat: steer the SoC setpoint every tick; retire the overnight planner"
```

---

### Task 4: Sensors and tape projection on the regime model

**Files:**
- Modify: `custom_components/energy_conductor/sensor.py` (`OvernightPlanSensor` → setpoint semantics; keep unique_id)
- Modify: `custom_components/energy_conductor/overnight.py` (`project_soc` docstring + default)
- Modify: `custom_components/energy_conductor/www/ec-strategy.js` (row label)
- Modify: `custom_components/energy_conductor/translations/en.json` (sensor name if translation-keyed)
- Test: `tests/integration/test_tape_attrs.py`, `tests/core/test_projection.py`, `tests/js/` (label snapshot if one exists)

**Interfaces:**
- Consumes: `coordinator.last_setpoint_decision`, `coordinator.last_setpoint_outcome` (Task 3), `regimes.current_regime` (Task 1).
- Produces: the `-overnight-plan` unique_id entity now reads: state = current setpoint (%), attrs `regime`, `reason`, `dedupe_key`, `outcome`, `write_mode`, `soc_projection` (projection toward 100). **The unique_id and translation_key stay** so recorder history, dashboards, and `tape_sources` wiring survive; only `_attr_name` changes to "Battery SoC setpoint". Task 6's rate-watch adds attrs here too.

- [ ] **Step 1: Write/adjust the failing tests**

In `tests/integration/test_tape_attrs.py` (and/or the sensor-availability test that covers this entity — grep for `overnight` there):

```python
async def test_setpoint_sensor_reflects_regime(hass, ...):
    # off-peak: state == 100, attrs["regime"] == "cheap_charge"
    # peak: state == 4, attrs["regime"] == "self_consume"


async def test_soc_projection_targets_full(hass, ...):
    # attrs["soc_projection"] present; during an off-peak window the projected soc
    # rises toward 100 (not toward an intermediate plan target)
```

In `tests/core/test_projection.py`: update any test that passes `target_percent=<plan value>` to use 100 where it models the new regime; keep the reserve-floor clamp tests as-is.

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/integration/test_tape_attrs.py tests/core/test_projection.py -v`
Expected: FAIL on `regime` attr (absent) and projection target.

- [ ] **Step 3: Implement**

`sensor.py` — reshape the class (keep class name to keep the diff honest about continuity, or rename to `SetpointSensor` with an alias comment; keep `unique_id` and `translation_key` VERBATIM):

```python
class OvernightPlanSensor(_BaseSensor):
    """Battery SoC setpoint (the repurposed overnight-plan entity — unique_id kept)."""

    _attr_translation_key = "overnight_plan"
    _attr_name = "Battery SoC setpoint"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int | float | None:
        d = self.coordinator.last_setpoint_decision
        return None if d is None else d.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.last_setpoint_decision
        if d is None:
            return {}
        attrs: dict[str, Any] = {
            "reason": d.reason,
            "dedupe_key": d.dedupe_key,
            "outcome": self.coordinator.last_setpoint_outcome,
            "write_mode": self.coordinator.write_mode,
        }
        state = self.coordinator.last_site_state
        if state is not None:
            attrs["regime"] = current_regime(state)
            attrs["soc_projection"] = [
                {"t": t.isoformat(), "soc": soc}
                for t, soc in project_soc(state, target_percent=100.0)
            ]
        return attrs
```

(import `current_regime` from `.regimes`.) `overnight.py`: give `project_soc` a default `target_percent: float = 100.0` and update its docstring — "inside a cheap window the setpoint engine charges toward 100% and the discharge guard holds; outside it the house draws baseline net of PV down to the reserve".

`www/ec-strategy.js`: change `row(acc("overnight-plan"), "Charge target tonight")` to `row(acc("overnight-plan"), "SoC setpoint")` (grep for other "overnight"/"tonight" copy in the JS while there; the Tonight tab title itself stays). If `translations/en.json` names this sensor under its translation key, update the name there too.

- [ ] **Step 4: Run py + JS suites**

Run: `uv run pytest -q && npm test`
Expected: PASS. If a JS test snapshots the row label, update it.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix && uv run ruff format
git add custom_components/energy_conductor tests
git commit -m "feat: setpoint sensor + regime-model tape projection"
```

---

### Task 5: Slot-1 pinning (config, decision kind, writer, readback)

**Files:**
- Modify: `custom_components/energy_conductor/const.py` (2 conf keys + 2 pin constants)
- Modify: `custom_components/energy_conductor/decisions.py` (`SET_SLOT_TIME`)
- Modify: `custom_components/energy_conductor/writer.py` (time.set_value branch)
- Modify: `custom_components/energy_conductor/coordinator.py` (`_WRITE_KINDS`, slot emission, string readback)
- Modify: `custom_components/energy_conductor/verify.py` (`check_time_write_landed`)
- Modify: `custom_components/energy_conductor/notifier.py` (kind label + value format)
- Modify: `custom_components/energy_conductor/config_flow.py` (battery schema + `BATTERY_KEYS`)
- Modify: `custom_components/energy_conductor/entity_ref.py` (`SCALAR_ENTITY_CONF_KEYS`)
- Modify: `custom_components/energy_conductor/translations/en.json` (field labels)
- Test: `tests/integration/test_writer.py`, `tests/core/test_verify.py`, `tests/integration/test_coordinator.py`, `tests/integration/test_config_flow.py`

**Interfaces:**
- Consumes: `_emit`/`_commanded` machinery (existing), `Decision`.
- Produces:
  - `const.CONF_CHARGE_SLOT_1_START_ENTITY = "charge_slot_1_start_entity"`, `const.CONF_CHARGE_SLOT_1_END_ENTITY = "charge_slot_1_end_entity"` (optional `time.` entity pickers).
  - `const.CHARGE_SLOT_PIN_START = "00:00:00"`, `const.CHARGE_SLOT_PIN_END = "23:59:00"`.
  - `DecisionKind.SET_SLOT_TIME = "set_slot_time"` — value is an `"HH:MM:SS"` string; writer calls `time.set_value`; `_WRITE_KINDS` includes it; `_commanded` stores the string and readback compares string-equality via `verify.check_time_write_landed(label: str, commanded: str, readback: str | None) -> VerificationResult | None`.
  - Coordinator emits two pin decisions per tick **only when both slot keys are configured** (dedupe key `f"slot-pin-{value}"` per entity — the readback loop, not the dedupe key, is what re-heals external drift).

- [ ] **Step 1: Write the failing tests**

`tests/integration/test_writer.py`:

```python
async def test_slot_time_write_calls_time_set_value(hass):
    # live mode, Decision(kind=SET_SLOT_TIME, target_entity="time.slot1_start",
    # value="00:00:00") -> service call time.set_value {"time": "00:00:00"}


async def test_slot_time_write_dry_run_noop(hass):
    # dry-run -> no service call
```

`tests/core/test_verify.py`:

```python
def test_time_write_landed_match():
    r = check_time_write_landed("set_slot_time", "00:00:00", "00:00:00")
    assert r is not None and r.ok


def test_time_write_landed_mismatch():
    r = check_time_write_landed("set_slot_time", "00:00:00", "23:30:00")
    assert r is not None and not r.ok
    assert "00:00:00" in r.detail and "23:30:00" in r.detail


def test_time_write_landed_unreadable():
    assert check_time_write_landed("set_slot_time", "00:00:00", None) is None
```

`tests/integration/test_coordinator.py`:

```python
async def test_slot_pin_emitted_when_configured(hass, coordinator_fixture):
    # config has both slot entities -> two SET_SLOT_TIME decisions per tick
    # (start "00:00:00", end "23:59:00"), written once, "unchanged" thereafter


async def test_slot_pin_skipped_when_unconfigured(hass, coordinator_fixture):
    # neither key set -> no SET_SLOT_TIME emissions


async def test_slot_drift_heals_via_readback(hass, coordinator_fixture):
    # after a landed pin, externally set the entity to "23:30:00"; after the settle
    # window the readback mismatch clears written-state and the pin re-writes once
```

`tests/integration/test_config_flow.py`: extend the existing whitelist/en.json guard tests — the new keys must appear in the battery schema, in `BATTERY_KEYS`, and have en.json labels (these guard tests exist; adding the keys to the fixtures may be all that's needed — read the test first).

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/integration/test_writer.py tests/core/test_verify.py -v`
Expected: FAIL/ERROR (`SET_SLOT_TIME` and `check_time_write_landed` don't exist).

- [ ] **Step 3: Implement**

`const.py` (battery section):

```python
# Charge slot 1 time entities (optional). When both are set, EC pins the slot always-on
# so the charge-target control behaves as a two-sided SoC setpoint (spec 2026-08-23).
CONF_CHARGE_SLOT_1_START_ENTITY = "charge_slot_1_start_entity"
CONF_CHARGE_SLOT_1_END_ENTITY = "charge_slot_1_end_entity"
CHARGE_SLOT_PIN_START = "00:00:00"
CHARGE_SLOT_PIN_END = "23:59:00"
```

`decisions.py`: add `SET_SLOT_TIME = "set_slot_time"`.

`writer.py`: extend `write` with a branch before the number branch:

```python
        if decision.kind is DecisionKind.SET_SLOT_TIME:
            if not isinstance(decision.value, str) or not decision.value:
                raise WriteFailure(
                    f"non-string slot time for {decision.target_entity}: {decision.value!r}"
                )
            try:
                await self.hass.services.async_call(
                    "time",
                    "set_value",
                    {"entity_id": decision.target_entity, "time": decision.value},
                    blocking=True,
                )
            except Exception as exc:
                raise WriteFailure(f"time.set_value failed for {decision.target_entity}: {exc}") from exc
            return
```

`verify.py`: add

```python
def check_time_write_landed(
    label: str, commanded: str, readback: str | None
) -> VerificationResult | None:
    """String-equality readback for time-entity writes (slot pinning). Same contract as
    check_write_landed; time entity states are exact 'HH:MM:SS' strings."""
    if readback is None:
        return None
    if readback == commanded:
        return VerificationResult(ok=True, detail=f"{label} reads {readback} as commanded")
    return VerificationResult(
        ok=False, detail=f"commanded {label}={commanded} but entity reads {readback}"
    )
```

`coordinator.py`:
- `_WRITE_KINDS` gains `DecisionKind.SET_SLOT_TIME`.
- `_CommandedWrite.value` type becomes `float | str`.
- In `_emit`'s live-write bookkeeping, replace `value = float(decision.value)` with:

```python
                value: float | str = (
                    str(decision.value)
                    if decision.kind is DecisionKind.SET_SLOT_TIME
                    else float(decision.value)
                )
```

- In `_check_writes_landed`, branch the readback read: for a `set_slot_time` key, `readback` is `raw.state` (string, `None` when unavailable/unknown) and the comparison uses `check_time_write_landed(kind, cmd.value, readback)`; numeric kinds keep the float path. Key the branch on `kind == DecisionKind.SET_SLOT_TIME.value`.
- In `_async_update_data`, after the setpoint emission:

```python
        slot_start = self.config.get(CONF_CHARGE_SLOT_1_START_ENTITY)
        slot_end = self.config.get(CONF_CHARGE_SLOT_1_END_ENTITY)
        if slot_start and slot_end:
            for entity, value in (
                (slot_start, CHARGE_SLOT_PIN_START),
                (slot_end, CHARGE_SLOT_PIN_END),
            ):
                await self._emit(
                    Decision(
                        kind=DecisionKind.SET_SLOT_TIME,
                        target_entity=entity,
                        value=value,
                        reason="Pin charge slot 1 always-on (setpoint regime)",
                        dedupe_key=f"slot-pin-{value}",
                    )
                )
```

`notifier.py`: `_KIND_LABEL[DecisionKind.SET_SLOT_TIME] = "Charge slot pinned"`; `_format_value` returns the raw string for this kind.

`config_flow.py`: add a `_time_entity_selector()` (`EntitySelector(EntitySelectorConfig(domain="time"))`), two `_marker(...)` optional entries in `battery_schema`, and both keys appended to `BATTERY_KEYS`.

`entity_ref.py`: add both keys to `SCALAR_ENTITY_CONF_KEYS`.

`translations/en.json`: labels in the battery step, e.g. "Charge slot 1 start (time entity)" / "Charge slot 1 end (time entity)" with descriptions "EC pins this always-on so the charge target acts as a SoC setpoint — see docs." Follow the file's existing structure for both `config` and `options` sections (grep how `reserve_soc_sensor` is declared and mirror it).

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, including the config-flow three-things guard tests.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix && uv run ruff format && uv run pytest -q
git add -A custom_components/energy_conductor tests
git commit -m "feat: pin charge slot 1 always-on via a time-entity write path"
```

---

### Task 6: Rate-watch (warn-only economics check) + strategy version bump

**Files:**
- Create: `custom_components/energy_conductor/rate_watch.py`
- Modify: `custom_components/energy_conductor/coordinator.py` (evaluation + latch), `decisions.py` (notify-only kind), `writer.py` (`_NOTIFY_ONLY_KINDS`), `notifier.py` (label/format), `sensor.py` (attrs on the setpoint sensor), `const.py` (constants), `__init__.py` (`_STRATEGY_VERSION` bump — covers Task 4's JS change too)
- Test: `tests/core/test_rate_watch.py` (new), `tests/integration/test_coordinator.py`

**Interfaces:**
- Consumes: `CONF_IMPORT_RATE_SENSOR`, `CONF_EXPORT_RATE_SENSOR` (existing costs group; £/kWh floats), `regimes.current_regime`.
- Produces:
  - `rate_watch.fill_margin_gbp(import_rate: float, export_rate: float, *, efficiency: float = 0.9) -> float` — `export - import/efficiency`; positive = fill-mode profitable.
  - `const.RATE_WATCH_EFFICIENCY = 0.9`, `const.RATE_WATCH_REARM_GBP = 0.005` (re-arm hysteresis: warn at margin ≤ 0, re-arm once margin > 0.005).
  - `DecisionKind.RATE_ECONOMICS_WARNING = "rate_economics_warning"` (notify-only).
  - Coordinator fields: `self.rate_watch_status: str` (`"n/a"`/`"ok"`/`"inverted"`), `self.rate_watch_margin_gbp: float | None`; setpoint-sensor attrs `rate_watch` + `rate_watch_margin_gbp`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_rate_watch.py
from custom_components.energy_conductor.rate_watch import fill_margin_gbp


def test_margin_positive_at_current_tariff():
    # 6.9p import, 12p export, eta 0.9 -> +4.33p
    assert fill_margin_gbp(0.069, 0.12) == pytest.approx(0.0433, abs=1e-4)


def test_margin_negative_when_export_collapses():
    assert fill_margin_gbp(0.069, 0.05) < 0


def test_efficiency_divides_import():
    assert fill_margin_gbp(0.09, 0.10, efficiency=1.0) == pytest.approx(0.01)
```

Coordinator tests: cheap-regime tick with rates configured 6.9p/12p → `rate_watch_status == "ok"`; with export 5p → `"inverted"` and exactly one `RATE_ECONOMICS_WARNING` notification (repeat ticks: still one); recovery above the re-arm threshold then re-inversion → a second notification; rates unconfigured → `"n/a"`, no evaluation; self-consume regime → status retains last value, no fresh evaluation.

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/core/test_rate_watch.py -v` — ModuleNotFoundError.

- [ ] **Step 3: Implement**

`rate_watch.py`:

```python
"""Fill-mode unit-economics check (spec 2026-08-23). Warn-only — never changes regime.

The setpoint regime's premise: grid-filling during cheap windows beats PV-filling while
off_peak_import / eta < export. This module computes the margin; the coordinator owns
the episode latch and notification.
"""

from __future__ import annotations


def fill_margin_gbp(import_rate: float, export_rate: float, *, efficiency: float = 0.9) -> float:
    """GBP/kWh margin of grid-filling: export value minus efficiency-adjusted import cost.

    Positive: fill-mode is profitable. Zero/negative: the strategy premise is broken and
    a human should reconsider (EC only warns).
    """
    return export_rate - import_rate / efficiency
```

Coordinator: init `self.rate_watch_status = "n/a"`, `self.rate_watch_margin_gbp = None`, `self._rate_watch_warned = False`. In `_async_update_data` after the setpoint emission:

```python
        if current_regime(state) == REGIME_CHEAP_CHARGE:
            await self._check_rate_economics(state)
```

with:

```python
    async def _check_rate_economics(self, state: SiteState) -> None:
        """Warn (once per inversion episode) when cheap-window economics stop favouring
        fill-mode. Evaluated only in the cheap regime, when the import-rate sensor is by
        definition reading the cheap rate. Warn-only: the regime never changes."""
        import_rate = self._read_rate_state(CONF_IMPORT_RATE_SENSOR)
        export_rate = self._read_rate_state(CONF_EXPORT_RATE_SENSOR)
        if import_rate is None or export_rate is None:
            self.rate_watch_status = "n/a"
            self.rate_watch_margin_gbp = None
            return
        margin = fill_margin_gbp(import_rate, export_rate, efficiency=RATE_WATCH_EFFICIENCY)
        self.rate_watch_margin_gbp = round(margin, 4)
        if margin <= 0:
            self.rate_watch_status = "inverted"
            if not self._rate_watch_warned:
                self._rate_watch_warned = True
                await self._emit(
                    Decision(
                        kind=DecisionKind.RATE_ECONOMICS_WARNING,
                        target_entity="rate_watch",
                        value=round(margin * 100, 2),  # pence, for the notification
                        reason=(
                            "Cheap-window fill margin is "
                            f"{margin * 100:.2f}p/kWh — grid-filling no longer beats "
                            "PV-filling; review the setpoint strategy"
                        ),
                        dedupe_key=f"rate-watch-{state.now.date().isoformat()}",
                    )
                )
        else:
            self.rate_watch_status = "ok"
            if margin > RATE_WATCH_REARM_GBP:
                self._rate_watch_warned = False

    def _read_rate_state(self, conf_key: str) -> float | None:
        entity = self.config.get(conf_key)
        if not entity:
            return None
        raw = self.hass.states.get(entity)
        if raw is None or raw.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, None, ""):
            return None
        try:
            value = float(raw.state)
        except (TypeError, ValueError):
            return None
        return value
```

`decisions.py`: add `RATE_ECONOMICS_WARNING = "rate_economics_warning"`. `writer.py`: add it to `_NOTIFY_ONLY_KINDS`. `notifier.py`: label `"Tariff economics changed"`, `_format_value` → `f"{decision.value:+.2f}p/kWh margin"`. `sensor.py` (setpoint sensor attrs): add `"rate_watch": self.coordinator.rate_watch_status` and `"rate_watch_margin_gbp": self.coordinator.rate_watch_margin_gbp`. `const.py`: the two constants. `__init__.py`: bump `_STRATEGY_VERSION` by one (single bump for this feature's JS change from Task 4).

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q && npm test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix && uv run ruff format && uv run pytest -q
git add -A custom_components/energy_conductor tests
git commit -m "feat: warn-only rate-watch on fill-mode unit economics"
```

---

### Task 7: Config/UX cleanup, docs, version

**Files:**
- Modify: `custom_components/energy_conductor/config_flow.py` (drop `CONF_OVERNIGHT_PLAN_TIME` from `BEHAVIOUR_KEYS` + behaviour schema)
- Modify: `custom_components/energy_conductor/translations/en.json` (drop plan-time labels; clarify reserve floor help text)
- Modify: `custom_components/energy_conductor/const.py` (delete `CONF_OVERNIGHT_PLAN_TIME`, `DEFAULT_OVERNIGHT_PLAN_TIME` if now unreferenced — grep first)
- Modify: `README.md` (behaviour description: regimes replace the overnight planner; slot-1 pinning setup; rate-watch)
- Modify: `custom_components/energy_conductor/manifest.json` (version → `0.9.0`)
- Test: `tests/integration/test_config_flow.py` (guards keep passing)

**Interfaces:**
- Consumes: everything landed in Tasks 1–6.
- Produces: a coherent 0.9.0 tree; stored `overnight_plan_time` values in existing entries are tolerated (extra dict keys are harmless) but no longer rendered or read.

- [ ] **Step 1: Make the changes**

- Behaviour schema + `BEHAVIOUR_KEYS`: remove `CONF_OVERNIGHT_PLAN_TIME`; grep `OVERNIGHT_PLAN_TIME` repo-wide — after Task 3 the coordinator no longer reads it; delete the const + default + en.json labels.
- en.json battery step: reserve-floor description becomes explicit: "Describes the inverter's reserve floor for energy calculations — NOT a minimum-SoC control. Ignored when the Reserve SoC floor sensor below is set (the live sensor always wins)."
- README: replace the overnight-planner description with the regime table (copy the spec's table), document the slot-1 picker setup and the always-on pin, the rate-watch warning, and the tariff-inequality premise with the caveat that the strategy suits `off_peak/η < export` tariffs. Keep the tone factual; note the legacy-automation retirement as a migration step for existing installs.
- manifest.json: `0.8.11` → `0.9.0`.

- [ ] **Step 2: Full local verification**

Run: `uv run pytest -q && npm test && uv run ruff check && uv run ruff format --check`
Then: `tox` (per repo convention before final commit).
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "chore: drop the overnight plan-time knob, document the setpoint regime, bump 0.9.0"
```

---

### Task 8: Self-review, branch, PR

- [ ] **Step 1: Spec-coverage pass** — reread `docs/superpowers/specs/2026-08-23-soc-setpoint-regime-design.md` top to bottom; check each section maps to landed code (regime table → Task 1/3; slot pinning → 5; rate-watch → 6; config → 5/7; sensors/tape → 4; removed planner → 3; testing list → all). Fix gaps before the PR.
- [ ] **Step 2: Fresh-eyes diff review** — `git diff main` in full; hunt for: leftover `last_overnight_*` references, en.json orphans, dead imports, reason strings leaking entity_ids, missed `_STRATEGY_VERSION` bump.
- [ ] **Step 3: Branch + PR** — work should have been on a feature branch (e.g. `feat/soc-setpoint-regime`) from Task 1; if not, branch now and reset main. Open the PR with a body summarising the spec (link it), the regime table, and the migration/cutover plan (dry-run A/B → cutover evening → rollback). PR creation uses plain `gh` (user's voice); reads/merges via `ghbot`. **Do not merge without explicit user sign-off.**
- [ ] **Step 4: Post-merge live plan (user-driven, NOT autonomous)** — deploy to Blithe in `dry_run`, observe transitions for a few days, then the cutover evening per the spec's checklist: audit + disable legacy automations, live mode, pin acceptance check (`23:59:00` end accepted? if the entity rejects it, try `23:59:59`/`00:00:00`-wrap and record what works), one full off-peak cycle watched, reset the stored reserve 40 → 4. HA restarts require explicit user confirmation, always.

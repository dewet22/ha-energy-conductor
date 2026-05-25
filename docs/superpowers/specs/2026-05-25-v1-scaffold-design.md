# ha-energy-conductor v1 — scaffold and behaviour design

*Spec written 2026-05-25. Captures the v1 scope, architecture, and explicitly-deferred items agreed during brainstorming.*

---

## Goal

A v1 useful for the author's own house: a Home Assistant integration that performs **overnight battery charge planning** and a **cross-device discharge guard** against the canonical motivating setup (GivEnergy hybrid inverter, Myenergi Zappi EV charger, Octopus Intelligent Go tariff, Solcast forecast).

The v1 surface is deliberately narrow. It exercises both control patterns conductor needs (scheduled planning, realtime coordination) without committing to features whose value is episodic (grid events) or whose design needs more iteration (solar surplus routing, advisory loads).

## Non-goals for v1

- Solar surplus routing to deferrable loads (Tier 3 in scoping; deferred to v3).
- Octopus saving sessions / power-up windows (Tier 4; deferred to v1.1).
- Advisory load notifications and confirmed deferred execution (deferred to v3).
- Managed-load planning awareness — reading Octopus Intelligent's *planned* dispatch slots and folding them into the overnight plan (v3; v1 only reads the *current* dispatching state).
- Multi-inverter / multi-site support.
- Storm pre-charge and resilience overrides (v2 — see §10).
- Forecast bias correction (v2; data plumbing is in v1).
- Learned baseline load profile (v2).
- Per-device adapters of any kind. The integration never names a hardware brand.

---

## 1. Architecture

**Layered: pure-Python core engine + thin HA adapter.**

Two packages in the repo:

- **`energy_conductor` (core)** under `src/energy_conductor/`. Pure Python, no `homeassistant` imports. Dataclasses describing site state; pure functions computing `Decision` values from that state.
- **`energy_conductor` (HA integration)** under `custom_components/energy_conductor/`. The HA shim: ConfigFlow, DataUpdateCoordinator, entity reads, decision execution. Imports the core package, hands it state, executes its decisions.

Both packages bear the same name in their respective sys.path roots — standard pattern for HA integrations with a separable library (`pyhaversion`, `python-miio`, etc.). The HA shim imports the core as `from energy_conductor import ...`.

**Why layered:** the Predbat code-quality assessment in `docs/predbat-code-quality.md` identifies AppDaemon/HA coupling and god-object inheritance as the main blockers to algorithm extraction. The layered architecture takes that lesson directly. Planning logic is fast to test (no HA fixtures), reuse-friendly, and reasoning is bounded by file. The cost is ~50 lines of dataclass boundary.

## 2. Repository layout

```
ha-energy-conductor/
├── custom_components/
│   └── energy_conductor/            # HA integration; DOMAIN = "energy_conductor"
│       ├── __init__.py              # async_setup_entry, coordinator wiring
│       ├── manifest.json
│       ├── config_flow.py           # ConfigFlow + OptionsFlow
│       ├── const.py
│       ├── coordinator.py           # DataUpdateCoordinator: HA state → SiteState
│       ├── adapter.py               # the ONLY file that knows both worlds
│       ├── notifier.py              # Decision → notify service call
│       ├── writer.py                # Decision → entity service call (gated)
│       ├── sensor.py                # Diagnostic sensors
│       └── translations/en.json
├── src/
│   └── energy_conductor/            # pure-Python core
│       ├── __init__.py
│       ├── model.py                 # SiteState, Battery, EVCharger, Tariff, ...
│       ├── tariff.py                # cheap-window helpers
│       ├── overnight.py             # plan_overnight()
│       ├── discharge_guard.py       # discharge_limit()
│       ├── fallback.py              # seasonal_fallback_kwh()
│       └── decisions.py             # Decision, DecisionKind
├── tests/
│   ├── core/                        # plain pytest, no HA
│   │   └── builders.py              # a_site_state(), a_battery(), ...
│   └── integration/                 # live HA smoke notes; NOT in CI
│       └── README.md                # install + manual checklist
├── docs/                            # existing design docs preserved
│   └── superpowers/specs/
├── pyproject.toml                   # uv, ruff, pytest config (single source)
├── hacs.json
├── README.md
└── .github/workflows/test.yml
```

**Architectural invariants enforced at lint time** (`ruff` `flake8-tidy-imports`):

- `src/energy_conductor/` must not import from `homeassistant`.
- `tests/core/` must not import from `custom_components`.

Catching architectural drift at lint time is cheaper than catching it in review.

## 3. Domain model (`energy_conductor.model`)

All dataclasses are frozen. `now` is always injected, never read from a clock — every core function takes a `SiteState` whose `now` was set by the adapter, so any scenario at any time is constructible in a test.

```python
@dataclass(frozen=True)
class Battery:
    soc_percent: float
    capacity_kwh: float
    max_charge_power_w: int           # from charge-control number's max attr
    max_discharge_power_w: int        # from discharge-limit number's max attr
    reserve_percent: float            # don't plan below this

@dataclass(frozen=True)
class EVCharger:
    power_w: float                    # instantaneous draw
    min_activation_power_w: int       # Zappi ~1400
    is_plugged_in: bool | None        # optional; None = unknown

@dataclass(frozen=True)
class ForecastSlot:
    start: datetime
    energy_kwh: float

@dataclass(frozen=True)
class SolarForecast:
    """Hourly or half-hourly blocks. Empty slots = no forecast available."""
    slots: list[ForecastSlot]
    fallback_kwh: float | None        # set only when slots is empty; else None
    fallback_source: str | None       # "seasonal" | "stats-2y" | etc; for notification

    @property
    def total_kwh_today(self) -> float: ...
    def kwh_between(self, start: datetime, end: datetime) -> float: ...

# Contract: exactly one of (non-empty slots) or (fallback_kwh is not None) holds.
# Core treats them as alternative inputs; adapter populates whichever it has.

@dataclass(frozen=True)
class TariffState:
    cheap_window_now: bool            # overnight whole-house cheap rate
    ev_dispatching_now: bool          # Octopus Intelligent smart dispatch active
    cheap_window_end: datetime | None
    next_cheap_window_start: datetime | None

@dataclass(frozen=True)
class SiteState:
    now: datetime                     # injected
    battery: Battery
    ev_charger: EVCharger | None      # None if not configured
    solar_forecast: SolarForecast
    tariff: TariffState
    baseline_load_w: float            # rolling average from recent power readings
```

`Decision` is data, not action. The core returns decisions; the adapter chooses whether to write or only notify. This is what makes dry-run trivial.

```python
class DecisionKind(StrEnum):
    SET_CHARGE_TARGET = "set_charge_target"
    SET_DISCHARGE_LIMIT = "set_discharge_limit"

@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    target_entity: str
    value: Any
    reason: str                       # human-readable; goes into the notification
    dedupe_key: str                   # adapter uses this to skip duplicate notifs
```

Dedupe lives in the adapter (a `(kind, target_entity) → last_key` map) — the core stays stateless. The dedupe key buckets continuous values (e.g. discharge limit to 100W) so baseline jitter doesn't spam notifications.

## 4. Behaviours

### 4.1 Overnight charge planning

**Runs:** daily at a configurable time (default 21:00, before Intelligent Go's 23:30 window opens). Also runs on integration startup if the last plan is >24h old.

**Algorithm:**

```
# Named constants (module-level in overnight.py)
MEANINGFUL_SOLAR_W = 500             # first slot at/above this counts as "solar arrived"
MORNING_GAP_CAP_H = 6                # absolute cap on morning_gap_hours
MISSING_FORECAST_GAP_H = 4           # used when no forecast slots are available

if solar_forecast.slots:
    first_solar = first slot with energy_kwh * 2 >= MEANINGFUL_SOLAR_W * 0.5
                  (i.e. half-hour slot equivalent to ≥500W average)
    morning_gap_hours = clamp(0, hours(cheap_window_end → first_solar), MORNING_GAP_CAP_H)
    forecast_kwh = solar_forecast.total_kwh_today
else:
    morning_gap_hours = MISSING_FORECAST_GAP_H
    forecast_kwh = solar_forecast.fallback_kwh    # adapter has populated this

morning_gap_kwh   = baseline_load_w * morning_gap_hours / 1000
forecast_deficit  = max(0, daily_kwh_target - forecast_kwh)

target_kwh        = morning_gap_kwh + forecast_deficit
target_percent    = clamp(reserve_percent,
                          round((target_kwh / capacity_kwh) * 100),
                          100)
```

**v1 caveats baked into the design:**

- `daily_kwh_target` is a config constant (default 10), not learned. v2 builds the rolling load profile that Predbat does. Documented in §10 roadmap.
- No export-price awareness; v1 assumes "any kWh not used overnight is fine to charge."
- No forecast bias correction; the plumbing for v2 exists via the optional `solar_generation_sensor` (see §7).

### 4.2 Discharge regime guard

The general control surface is the inverter's `discharge_power_limit` `number` entity. The decision is what value to write.

**Three-regime table for v1:**

| Priority | Regime | Discharge limit |
|---|---|---|
| 1 | `cheap_window_now` (overnight) | **0W** — battery idle; whole house cheap |
| 2 | `ev_dispatching_now` AND EV drawing | **baseline_load_w** — cover house, let EV pull grid |
| 3 | Default | **max_discharge_power_w** — unconstrained |

```python
def discharge_limit(state: SiteState) -> Decision:
    if state.tariff.cheap_window_now:
        limit_w, reason = 0, "Cheap window active — battery idle"
    elif state.tariff.ev_dispatching_now and _ev_drawing(state):
        limit_w = round(state.baseline_load_w)
        reason = f"EV dispatch active — capping discharge at house baseline ({limit_w}W)"
    else:
        limit_w = state.battery.max_discharge_power_w
        reason = "Unconstrained"

    return Decision(
        kind=DecisionKind.SET_DISCHARGE_LIMIT,
        target_entity=config.discharge_limit_entity,
        value=limit_w,
        reason=reason,
        dedupe_key=f"discharge-{limit_w // 100}",   # 100W bucket
    )
```

**Runs:** on the coordinator tick (every 30s) AND on state-change events for the EV power sensor, both tariff sensors, and battery SOC sensor (HA `async_track_state_change_event`). State-change wiring makes the guard feel realtime (typically subsecond after a sensor update fires on HA's event bus); the 30s heartbeat is the convergence safety net for missed events / restarts.

**v1 caveats:**
- The "baseline load" assumption is a rolling average; if the kettle goes on during a dispatch slot, the discharge cap is briefly below instant load and the battery under-supplies. The 30s tick catches up. An explicit `instant_load_w` field can be added later if this is observed to matter.
- Doesn't gate on SOC being above reserve; redundant suppression at reserve is a no-op for the inverter and the notification still reflects the actual decision.

## 5. Configuration (ConfigFlow)

One config entry per site. v1 assumes one inverter, one EV charger, one forecast source. Re-running the flow replaces the entry. `OptionsFlow` mirrors all steps so any field can be edited later.

**Step 1 — Battery (required):**
- `soc_sensor` — sensor entity selector (filtered to `device_class=battery` or unit `%`)
- `charge_power_control` — `number` entity selector
- `discharge_limit_entity` — `number` entity selector
- `capacity_kwh` — numeric input
- `reserve_percent` — default 10

**Step 2 — Tariff (required):**
- `cheap_rate_sensor` — `binary_sensor` selector (whole-house cheap window)
- `dispatching_sensor` — `binary_sensor` selector, **optional** (Octopus Intelligent smart dispatch). Absent → `ev_dispatching_now` is always `False`.
- `overnight_window_end_time` — time input (planning deadline reference)

**Step 3 — Solar forecast (required):**
- `forecast_source`: `solcast` | `daily_total_sensor` | `none`
- If `solcast`: pick the Solcast forecast sensor.
- If `daily_total_sensor`: any kWh sensor.
- If `none`: planning uses the seasonal/stats fallback; warn loudly.
- `solar_generation_sensor` — kWh total sensor, **optional**. If present, enables the stats-based seasonal fallback (§7) and reserves capability for v2 bias correction.
- `winter_min_kwh` (default 0), `summer_max_kwh` (default 8), `southern_hemisphere` (default false) — used by the cosine seasonal fallback.

**Step 4 — EV charger (optional):**
- `power_sensor` — `sensor` selector, unit W
- `min_activation_power_w` — default 1400 (Zappi)

Skipping this step means the discharge guard never fires regime 2; the integration still does overnight planning.

**Step 5 — Behaviour mode (required):**
- `write_mode`: `dry_run` (default) | `live`
- `notify_target` — notify service selector
- `overnight_plan_time` — default 21:00
- `daily_kwh_target` — default 10 (the v1 constant used in §4.1)

**Design choices:**

- **Entity selectors, not text inputs.** HA's `EntitySelector` shows a filterable dropdown — the right place to enforce the entity-type contract.
- **No "device" abstraction in config.** Users map roles to entities. The integration never has a list of supported hardware. This is the architectural promise; the ConfigFlow is where it gets tested.
- **Capacity is config, not auto-read.** Auto-discovery from device info is the kind of subtle coupling that grows into per-device adapters.

## 6. Decision execution

The adapter's per-tick flow:

```python
async def tick(self) -> None:
    state = await self._build_site_state()
    decisions = [discharge_limit(state)]
    # overnight plan runs on schedule, not every tick; cached result is replayed if changed
    for decision in decisions:
        if self._is_duplicate(decision):
            continue
        await self._notify(decision)
        if self._config.write_mode == "live":
            await self._write(decision)
        self._remember(decision)
```

**Notify always fires; write is gated.** This makes "dry-run with observability" the natural default. In live mode the notification still fires — the trust loop continues working after the switch is flipped. Dry-run prefixes notification text with `[dry-run]`.

**Notification shape** (one-line, mobile-friendly):

> `[dry-run] Discharge cap → 480W (EV dispatch active — capping at house baseline)`
>
> `Overnight charge target → 65% (Morning gap 4.2h, forecast 12.8 kWh)`

**Write failures** emit a second notification ("decision was X but writing to entity Y failed: unavailable") rather than retrying in a loop. Failed write does **not** invalidate the dedupe key; only a *changed* decision triggers a new write attempt.

## 7. Lifecycle, triggers, and error handling

### Triggers

A single `DataUpdateCoordinator` with three trigger sources:

1. **30s periodic tick** — heartbeat. Drives the discharge guard and ensures convergence after missed events / restarts.
2. **State-change events** on EV power, both tariff sensors, and battery SOC sensor → `coordinator.async_request_refresh()`. Makes the discharge guard feel realtime (~100ms after a sensor change).
3. **Scheduled overnight plan** at `overnight_plan_time` via `async_track_time_change`. Runs `plan_overnight()`, caches the result, emits the `SET_CHARGE_TARGET` decision through the same notify/write pipeline. Cached plan exposed as `sensor.energy_conductor_overnight_plan`.

### Startup

- `async_setup_entry`: build initial `SiteState`, emit `status = ok`.
- If most recent overnight plan is >24h old or absent, run one immediately.
- Discharge guard runs on first tick.

### Shutdown

`async_unload_entry` cancels timers and unsubscribes events. Does **not** restore any default values to control entities — we didn't touch them at install time; we don't touch them at uninstall.

### Failure modes and responses

| Condition | Response |
|---|---|
| Configured entity ID missing at setup | `ConfigEntryNotReady`; HA surfaces as repair issue |
| Entity state `unavailable`/`unknown` at tick | Skip affected decision; log once per `(error_kind, entity_id)` per hour |
| Sensor `last_updated` older than threshold (5min power, 24h forecast) | Treated as unavailable |
| Value fails to parse as expected type | Treated as unavailable |
| Forecast missing at overnight plan time | Use seasonal/stats fallback; notification explicitly flags fallback source |
| Write service call fails | Emit failure notification; do not retry until decision changes |
| Uncaught exception in `energy_conductor` core | Catch at adapter boundary; log full traceback once; set `status = error`; do not re-raise |

### Seasonal fallback (when forecast slots are empty)

Two strategies, opportunistic — pick the best available data source:

**If `solar_generation_sensor` is configured AND HA has sufficient long-term statistics:**

```
1. Query daily-sum statistics for the generation sensor over the trailing 365 days.
2. Filter to ±14 days around today's calendar date (across however many prior years exist).
3. If ≥7 data points: return the 25th percentile.
   Notification: "fallback 3.4 kWh from 2y stats"
4. Otherwise fall through to seasonal cosine.
```

**Seasonal cosine fallback (always available):**

```python
def seasonal_fallback_kwh(now, winter_min, summer_max, southern=False):
    peak_day = 172 if not southern else 355   # solstice
    phase = math.cos(2 * math.pi * (now.timetuple().tm_yday - peak_day) / 365)
    return winter_min + (summer_max - winter_min) * (phase + 1) / 2
```

**Properties:**
- Honest about what's known (sun angle is a function of date) and what isn't (cloud cover).
- Site-specific without parameter tuning when stats are available.
- 25th percentile, not mean — this is the *pessimistic* fallback.
- Calendar window, not trailing 30 days — avoids seasonal under/over-promising.
- Graceful degradation: day-1 install uses cosine; stats take over once data accumulates.
- The notification reflects the source so the user knows the basis of the decision.

**Architectural payoff:** the `solar_generation_sensor` is groundwork for v2 forecast bias correction. The actuals come from the same sensor; v2's data flow is already in place on day 1. The stats query is HA-side (in `adapter.py`); core receives a single number inside `SolarForecast.fallback_kwh`. The boundary holds.

### Diagnostic sensors

- `sensor.energy_conductor_status` — `ok` | `degraded` | `error`. Attributes: last decision, last error, counters.
- `sensor.energy_conductor_overnight_plan` — current cached overnight plan. Attributes: target_percent, morning_gap_hours, forecast_kwh, fallback_source (if used).
- `sensor.energy_conductor_discharge_decision` — most recent discharge decision. Attributes: limit_w, regime, reason.

## 8. Testing strategy

### Tier 1: pure pytest against `energy_conductor` core (the bulk)

`tests/core/`. Plain `pytest` + `pytest-asyncio` where needed. No HA harness, no fixtures heavier than a dataclass builder.

```python
def test_overnight_plan_covers_morning_gap():
    state = a_site_state(
        battery=a_battery(soc_percent=20, capacity_kwh=10),
        tariff=a_tariff(cheap_window_end=at("06:00")),
        solar_forecast=a_forecast(first_500w_slot=at("10:00")),
        baseline_load_w=400,
    )
    plan = plan_overnight(state, daily_kwh_target=10)
    assert plan.target_percent == 26
```

Builder helpers in `tests/core/builders.py` default every field; tests override only what they care about.

**Coverage:**
- `plan_overnight` — every morning-gap / forecast-deficit combination, every clamp boundary (reserve floor, 100% ceiling, missing forecast, DST transitions).
- `discharge_limit` — every regime-table cell, hysteresis around `min_activation_power_w`, dedupe key generation.
- `seasonal_fallback_kwh` — solstice / equinox values, both hemispheres.
- `TariffState` derivations — composite logic, window boundaries.
- `Decision.dedupe_key` — invariants (same inputs → same key; threshold-crossing changes key at expected granularity).

**Property tests** (`hypothesis`) for two narrow invariants:
1. `plan_overnight()` output is always in `[reserve_percent, 100]` for any valid input.
2. `discharge_limit()` is monotonic in EV power around the activation threshold (no oscillation regions).

**Coverage target:** 95%+ on `src/energy_conductor/`.

### Tier 2: live HA install (smoke, not CI)

`tests/integration/` as scripts and notes, not pytest tests. CI does not run these.

Contents:
- `README.md` documenting symlink install into a real HA config dir.
- Manual smoke checklist for each release: fresh ConfigFlow walkthrough; OptionsFlow edit; dry-run overnight plan fires at 21:00 with notification; discharge guard fires when Zappi crosses 1.4 kW; reload integration without restart; uninstall leaves no orphan entities.
- `replay.py` (stub for v1, fleshed out v1.1): takes a recorded HA state snapshot, feeds it to core, verifies the expected decision. Bridge between "I saw weird behaviour Tuesday" and "regression test that catches it."

**No `pytest-homeassistant-custom-component`.** Per the brainstorming decision: heavy harness, real maintenance overhead, and the actual integration risks (wrong entity ID, ConfigFlow UX, notification delivery, recorder query shape) only surface against a real instance anyway.

### CI

`.github/workflows/test.yml` on push and PR:

```yaml
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --group dev
      - run: uv run ruff check
      - run: uv run ruff format --check
      - run: uv run pytest --cov-fail-under=90
```

No HA installs in CI. Fast, deterministic, runs in under 30s.

## 9. Tooling and scaffolding

- **`uv`** for dependency management. Single `pyproject.toml` at the repo root covers both the core package and the HA component's dev workflow.
- **Python 3.12+** (matches HA 2024.6 baseline).
- **`ruff`** for lint + format. Rules baseline: `E, F, I, B, UP, SIM, RUF, TID`. `TID` enforces the architectural invariants (§2).
- **Line length 100.** No `# noqa` without an explanatory comment.
- **Dependency groups:** runtime for `energy_conductor` core is stdlib only; dev includes `pytest`, `pytest-asyncio`, `hypothesis`, `ruff`, `coverage[toml]`.
- **`manifest.json`:** `domain: energy_conductor`, `config_flow: true`, `iot_class: local_polling`, `requirements: []`, `version: 0.1.0`.
- **`hacs.json`:** installable as a custom HACS repository from day one. Submission to default list deferred.
- **`.gitignore`:** add `__pycache__/`, `.pytest_cache/`, `.coverage`, `htmlcov/`, `.venv/`, `dist/`, `*.egg-info/` at repo root.
- **Commit author** set to `dewet22@users.noreply.github.com` per GivEnergy projects convention.
- **Conventional commits** (`feat:`, `fix:`, `refactor:`, etc).
- **No pre-commit hook** — `ruff check --fix` and `ruff format` run manually per user preference.

## 10. Roadmap / explicitly-deferred items

### v1.1 — small follow-ups; regime model already supports them

- **Grid events** (saving sessions, power-up windows). Slot into the regime table at priorities 1–2. Adds `GridEvents` to `SiteState` and two optional binary_sensor config fields. Saving-session forced-export has an inverter-mode wrinkle on non-hybrids — to be documented as a known limitation rather than worked around in v1.1.
- **`replay.py`** for regression testing of recorded scenarios.
- **`hassfest` CI check** once `manifest.json` stabilises.

### v2 — features needing real-world data conductor accumulates during v1

- **Forecast bias correction.** Roll up `(forecast, actual)` pairs from the recorder using `solar_generation_sensor` (already wired in v1); compute rolling site-specific multiplicative correction. Infrastructure is in place.
- **Learned baseline load profile.** Replace the `daily_kwh_target` constant with a multi-day half-hourly profile from HA recorder, optionally EV-energy-filtered. After v1 reveals what the simpler heuristic gets wrong.
- **Storm pre-charge / resilience override.** When severe-weather warning sensors or wind-gust forecast thresholds indicate elevated outage risk, override cost-optimal planning with a maximum-SOC target — accepting non-cheap-rate charging if needed. Slots into the regime table at top priority. Exits when triggering sensors clear AND a cooldown elapses (default 6h). Needs more thought on (a) which warning sources are reliable enough, (b) interaction with grid-frequency anomalies during an unfolding event, (c) transfer-switch vs inverter-only-backup interaction.

### v3 — features needing new design work

- **Solar surplus routing** (EV / HWC during the day). Originally Tier 3 in scoping. Real model for managing multiple deferrable absorbers with hysteresis. Largest single addition.
- **Advisory load notifications.** Night-before recommendations for washing machine / dishwasher. Needs a surplus-window detector against the half-hourly forecast and a notification-with-confirmation pattern.
- **Multi-inverter support.** Lockstep vs load-sharing has its own design space.
- **Managed-load planning awareness.** Read Octopus Intelligent's *planned* dispatch slots from sensor attributes and fold them into the overnight plan as fixed constraints.
- **OpenADR / EEBUS reception** — the formal-provider-coordination path from the README.

### Explicitly never

- **Per-device adapters.** No `givenergy.py`, no `zappi.py`. If a feature requires per-device code, it's the wrong feature.
- **Cloud dependencies in the integration itself.** Forecast and tariff data flow through existing HA integrations. Conductor is local-polling.

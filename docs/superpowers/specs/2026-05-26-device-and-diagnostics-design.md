# Energy Conductor — Device grouping, diagnostics, continuous planning

**Status:** Approved 2026-05-26
**Builds on:** [v1 scaffold](./2026-05-25-v1-scaffold-design.md)

## Goal

Three improvements to the v1 integration:

1. **Device grouping** — bundle all entities under a single HA *device* per config entry so multiple plants are distinguishable and the integration presents as a coherent unit.
2. **Diagnostics** — expose the full `SiteState` snapshot the conductor used to make its last decision, so users can verify their entity mappings are wired correctly and decisions are correctly informed.
3. **Continuous planning** — re-evaluate the overnight plan hourly (with jitter) instead of only at the scheduled time, so the recommendation stays live and the system can react to mid-day input changes.

## Non-goals

- Storm-precharge or any new planning logic beyond v1.
- Restructuring `coordinator.py` beyond the minimal additions needed.
- Adding configuration knobs beyond the device-name override.
- New write paths or notification channels.

---

## Section 1: Device grouping

Every entity (existing and new) returns a `DeviceInfo` so HA groups them under one device per config entry. Multiple config entries → multiple devices, each with its own identifier and name.

### Device identity

| Field | Value |
|---|---|
| `identifiers` | `{(DOMAIN, entry.entry_id)}` |
| `name` | `config.get(CONF_DEVICE_NAME)` or `hass.config.location_name` |
| `manufacturer` | `"Energy Conductor"` |
| `model` | `"v1"` |

Using `entry.entry_id` as the identifier means the device is stable across restarts and uniquely tied to its config entry, so removing the entry cleanly removes the device.

### Device name configuration

A new `CONF_DEVICE_NAME` key is added to the **OptionsFlowHandler** (not initial config — the user doesn't need to think about it at setup time). When unset, runtime falls back to `hass.config.location_name` so a fresh install reads as "My Home" (or whatever the owner named their HA system).

### Implementation seam

`_BaseSensor` in `sensor.py` (and the analogous base in `binary_sensor.py`) gains:

```python
@property
def device_info(self) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, self._entry_id)},
        name=(
            self.coordinator.config.get(CONF_DEVICE_NAME)
            or self.coordinator.hass.config.location_name
        ),
        manufacturer="Energy Conductor",
        model="v1",
    )
```

All sensor classes inherit this with no per-entity work.

---

## Section 2: Coordinator changes

Two small additions to `EnergyConductorCoordinator`.

### 2.1 SiteState snapshot cache

Add `self.last_site_state: SiteState | None = None`. Populate it after every successful `build_site_state()` call in both `_async_update_data` (30-second tick) and `_run_overnight_plan` (scheduled and hourly).

**Set timing:** on the success path only, after the `try/except EntityProblem` block. A failed read leaves the previous snapshot in place — diagnostic sensors continue displaying the last-known-good values, while `StatusSensor` flips to `degraded` to signal staleness.

This snapshot is the single source of truth for all diagnostic sensors: they see exactly what the conductor saw when it last decided.

### 2.2 Hourly plan re-evaluation with jitter

In `async_start()`, register a second `async_track_time_change` alongside the existing scheduled-time one. To avoid all instances of the integration hammering at the same wall-clock second:

```python
import random

# Jitter chosen once per coordinator instance at startup.
# Base: HH:55:00 (5 minutes before the hour, so the plan rolls before
# any other hourly automations might key off it).
# Spread: ±60s → effective window HH:54:00 .. HH:56:00.
offset = random.randint(-60, 60)
total_seconds = 55 * 60 + offset
self._hourly_minute = total_seconds // 60   # 54, 55, or 56
self._hourly_second = total_seconds % 60

self._unsubs.append(
    async_track_time_change(
        self.hass,
        self._run_overnight_plan,
        minute=self._hourly_minute,
        second=self._hourly_second,
    )
)
_LOGGER.info(
    "Hourly plan re-evaluation scheduled for HH:%02d:%02d",
    self._hourly_minute, self._hourly_second,
)
```

Each instance picks once and sticks with it for the coordinator's lifetime. Across the user base, load is spread evenly across a ~2-minute window. The chosen time is logged at INFO at startup for debugging.

### 2.3 Behaviour: identical to scheduled run

`_run_overnight_plan` is the same code path for both triggers. The existing dedupe key (`decision.kind.value` + `target_entity`) suppresses redundant writes and notifications — if the recommended charge target hasn't changed, nothing is sent. If it has, the new value is written and notified, identical to the scheduled fire.

This matches the user's mental model that planning is becoming a continual task (e.g. when storm forecasting is added later), so the system is comfortable with that pattern from the start.

---

## Section 3: Diagnostic sensor catalogue

Two HA platforms: `sensor` (expanded) and `binary_sensor` (new). `PLATFORMS` in `__init__.py` becomes `["sensor", "binary_sensor"]`.

### 3.1 Sensor platform — decision entities (3 existing)

Behaviour unchanged; gain `device_info` via the shared base class.

| Entity | State | Unit | Device class | Notes |
|---|---|---|---|---|
| Status | `ok` / `degraded` / `error` | — | — | + attrs: last_error, ticks_total, notifications_sent |
| Overnight plan target | int | `%` | — | + attrs: reason, dedupe_key |
| Discharge decision | int | `W` | — | + attrs: reason, dedupe_key |

### 3.2 Sensor platform — site state diagnostics (10 new)

All `EntityCategory.DIAGNOSTIC`, all read from `coordinator.last_site_state`. If the snapshot is `None` or the relevant subtree is missing, `native_value` returns `None` and HA renders the entity as *Unavailable*.

| Entity | Source | Unit | Device class |
|---|---|---|---|
| Battery SoC | `state.battery.soc_percent` | `%` | `BATTERY` |
| Battery reserve | `state.battery.reserve_percent` | `%` | `BATTERY` |
| Battery usable energy | `capacity_kwh × max(0, soc − reserve) / 100` | `kWh` | `ENERGY` |
| Battery max charge | `state.battery.max_charge_power_w` | `W` | `POWER` |
| Battery max discharge | `state.battery.max_discharge_power_w` | `W` | `POWER` |
| Solar forecast today | `state.solar_forecast.total_kwh_today` | `kWh` | `ENERGY` |
| Cheap window end | `state.tariff.cheap_window_end` | — | `TIMESTAMP` |
| EV charger power | `state.ev_charger.power_w` (unavailable if no EV) | `W` | `POWER` |
| Baseline load | `state.baseline_load_w` | `W` | `POWER` |
| Last state read at | `state.now` | — | `TIMESTAMP` |

**Solar forecast today** carries extra attributes for full context: `source` (`solcast` / `daily_total_sensor` / `stats` / `seasonal`), `slot_count`, `fallback_source`. One glance tells you the value and which input path produced it.

### 3.3 Binary sensor platform — tariff flags (2 new)

Both `EntityCategory.DIAGNOSTIC`, both read from `coordinator.last_site_state.tariff`.

| Entity | Source | Device class |
|---|---|---|
| Cheap window now | `state.tariff.cheap_window_now` | `POWER` (on = cheap power available) |
| EV dispatching now | `state.tariff.ev_dispatching_now` | `RUNNING` |

### 3.4 Unique IDs

`f"{entry.entry_id}-{slug}"` for every entity. Slugs: `battery-soc`, `battery-reserve`, `battery-usable-energy`, `battery-max-charge`, `battery-max-discharge`, `solar-forecast-today`, `cheap-window-end`, `ev-charger-power`, `baseline-load`, `last-state-read-at`, `tariff-cheap-now`, `tariff-dispatching-now`. Stable across restarts.

---

## Files affected

**Modified:**
- `custom_components/energy_conductor/__init__.py` — `PLATFORMS` adds `"binary_sensor"`.
- `custom_components/energy_conductor/const.py` — new `CONF_DEVICE_NAME` key.
- `custom_components/energy_conductor/config_flow.py` — options flow gains the device-name field.
- `custom_components/energy_conductor/coordinator.py` — `last_site_state` field; populate after successful builds; hourly `async_track_time_change` with startup-jittered offset.
- `custom_components/energy_conductor/sensor.py` — `_BaseSensor.device_info`; 10 new diagnostic sensor classes.

**Created:**
- `custom_components/energy_conductor/binary_sensor.py` — platform module with `_BaseBinarySensor` (mirrors `_BaseSensor`) and 2 tariff binary sensors.

**Tests:**
- `tests/core/` is untouched — these changes are pure HA-layer surface; the core stays insulated.
- Integration tests (if/when added in a later round) would cover `device_info` shape, `last_site_state` caching behaviour, hourly trigger registration, and binary sensor states. Out of scope for this round — the v1 scaffold did not include HA-layer integration tests and adding the harness is its own project.

## Testing strategy for this round

Manual verification after install:

1. Reload the integration; confirm a single "Energy Conductor — \<location\>" device appears with all 15 entities grouped under it.
2. Edit device name via Options; confirm it updates without re-creating entities.
3. Confirm diagnostic entities show the same values you can read from the source HA entities (cross-check SoC, EV power, forecast).
4. Watch the overnight plan sensor through the day — confirm it updates at HH:54–56 each hour.
5. Restart HA — confirm jittered minute/second logged at startup (and may differ from previous run).
6. Stop a source sensor (e.g. SoC sensor unavailable) — confirm StatusSensor flips to `degraded` and diagnostic entities retain their last value.

Adding an HA-integration test harness is tracked for a future round, not this one.

## Risks and open questions

- **Entity-count growth (3 → 15)** — significant jump in the device's entity list. Mitigated by `EntityCategory.DIAGNOSTIC` keeping the new ones off the main entity list. User has accepted "more data over less for now" with intent to trim later.
- **Jitter at HH:54–56 means hourly plan doesn't fire on the hour** — by design. The log message at startup makes the actual fire-time discoverable. Documented in the README under v1 behaviour.
- **`hass.config.location_name`** can theoretically change after device creation. The device name read happens on every entity render, so HA updates without re-creation. Not an issue.
- **Existing tests** — none of the core tests should break (we don't touch `model.py`, `discharge_guard.py`, `overnight.py`, etc.). Coverage will dip slightly because `sensor.py` and `binary_sensor.py` are omitted from coverage measurement (per existing `pyproject.toml` config).

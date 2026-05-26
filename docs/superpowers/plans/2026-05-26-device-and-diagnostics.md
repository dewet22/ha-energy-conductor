# Device Grouping, Diagnostics & Continuous Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bundle all entities under one HA device per config entry, expose the full `SiteState` snapshot via diagnostic sensors, and re-evaluate the overnight plan hourly with startup-chosen jitter.

**Architecture:** Coordinator caches the last successful `SiteState` snapshot; new sensor/binary_sensor classes read directly from that cache. A pure jitter helper (in its own file) computes the hourly trigger offset, picked once at startup. Device grouping wired through `DeviceInfo` on a shared base class.

**Tech Stack:** Python 3.14, Home Assistant custom component APIs (`SensorEntity`, `BinarySensorEntity`, `DeviceInfo`, `async_track_time_change`), `uv` + `pytest` for tooling.

**Spec:** [docs/superpowers/specs/2026-05-26-device-and-diagnostics-design.md](../specs/2026-05-26-device-and-diagnostics-design.md)

---

## File map

**Modified:**
- `custom_components/energy_conductor/const.py` — add `CONF_DEVICE_NAME`
- `custom_components/energy_conductor/config_flow.py` — add device-name field to OptionsFlow behaviour step
- `custom_components/energy_conductor/coordinator.py` — add `last_site_state` cache; populate after successful builds; register hourly jittered trigger
- `custom_components/energy_conductor/sensor.py` — add `device_info` to `_BaseSensor`; add 10 diagnostic sensor classes
- `custom_components/energy_conductor/__init__.py` — `PLATFORMS` adds `"binary_sensor"`
- `pyproject.toml` — add `binary_sensor.py` to coverage omit list

**Created:**
- `custom_components/energy_conductor/jitter.py` — pure helper for jittered hourly offset
- `custom_components/energy_conductor/binary_sensor.py` — new platform with 2 tariff binary sensors
- `tests/core/test_jitter.py` — unit tests for the jitter helper

---

## Task 1: Add `CONF_DEVICE_NAME` constant

**Files:**
- Modify: `custom_components/energy_conductor/const.py`

- [ ] **Step 1: Add the constant**

Insert `CONF_DEVICE_NAME = "device_name"` after the existing `CONF_DAILY_KWH_TARGET` line in the "Config keys — behaviour" group:

```python
# Config keys — behaviour
CONF_WRITE_MODE = "write_mode"
CONF_NOTIFY_TARGET = "notify_target"
CONF_OVERNIGHT_PLAN_TIME = "overnight_plan_time"
CONF_DAILY_KWH_TARGET = "daily_kwh_target"
CONF_DEVICE_NAME = "device_name"
```

- [ ] **Step 2: Verify lint clean**

Run: `uv run ruff check custom_components/energy_conductor/const.py`
Expected: `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add custom_components/energy_conductor/const.py
git commit -m "feat: add CONF_DEVICE_NAME config key"
```

---

## Task 2: Add device-name field to OptionsFlow

**Files:**
- Modify: `custom_components/energy_conductor/config_flow.py:32-67` (imports) and `:276-312` (OptionsFlow behaviour step)

- [ ] **Step 1: Add the import**

In the import block from `.const`, add `CONF_DEVICE_NAME` (keep alphabetical order):

```python
from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CHARGE_CONTROL,
    CONF_BATTERY_DISCHARGE_LIMIT,
    CONF_BATTERY_RESERVE_PERCENT,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CHEAP_RATE_SENSOR,
    CONF_DAILY_KWH_TARGET,
    CONF_DEVICE_NAME,
    CONF_DISPATCHING_SENSOR,
    ...  # rest unchanged
)
```

- [ ] **Step 2: Add TextSelector import**

In the `homeassistant.helpers.selector` import block, add `TextSelector` and `TextSelectorConfig`:

```python
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TimeSelector,
)
```

- [ ] **Step 3: Add the field to the OptionsFlow schema**

In `EnergyConductorOptionsFlow.async_step_behaviour`, add a new field to the `vol.Schema` dict (place it last, after `CONF_DAILY_KWH_TARGET`):

```python
vol.Optional(
    CONF_DEVICE_NAME,
    description={"suggested_value": self._defaults.get(CONF_DEVICE_NAME, "")},
): TextSelector(TextSelectorConfig()),
```

- [ ] **Step 4: Persist the field**

In the same method's `async_create_entry` call, add the device-name key to the persisted `data` dict. Use `.get()` so an empty input doesn't overwrite a previous value with empty string — pass `None` instead and let the runtime fallback handle it:

```python
return self.async_create_entry(
    title="",
    data={
        CONF_WRITE_MODE: user_input[CONF_WRITE_MODE],
        CONF_NOTIFY_TARGET: user_input[CONF_NOTIFY_TARGET],
        CONF_DAILY_KWH_TARGET: user_input[CONF_DAILY_KWH_TARGET],
        CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME) or None,
    },
)
```

- [ ] **Step 5: Verify lint clean and existing tests pass**

Run: `uv run ruff check custom_components/energy_conductor/config_flow.py && uv run pytest`
Expected: lint passes; all existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/energy_conductor/config_flow.py
git commit -m "feat: add device-name field to OptionsFlow"
```

---

## Task 3: Create jitter helper module with tests

**Files:**
- Create: `custom_components/energy_conductor/jitter.py`
- Create: `tests/core/test_jitter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_jitter.py`:

```python
"""Unit tests for the jitter helper."""

from __future__ import annotations

import pytest

from energy_conductor.jitter import hourly_jitter_offset


def test_zero_offset_is_55_00():
    assert hourly_jitter_offset(0) == (55, 0)


def test_positive_60_offset_is_56_00():
    assert hourly_jitter_offset(60) == (56, 0)


def test_negative_60_offset_is_54_00():
    assert hourly_jitter_offset(-60) == (54, 0)


def test_positive_30_offset_is_55_30():
    assert hourly_jitter_offset(30) == (55, 30)


def test_negative_30_offset_is_54_30():
    assert hourly_jitter_offset(-30) == (54, 30)


def test_offset_below_range_raises():
    with pytest.raises(ValueError, match=r"-60, 60"):
        hourly_jitter_offset(-61)


def test_offset_above_range_raises():
    with pytest.raises(ValueError, match=r"-60, 60"):
        hourly_jitter_offset(61)


def test_minute_always_in_54_55_56():
    for off in range(-60, 61):
        minute, _ = hourly_jitter_offset(off)
        assert minute in (54, 55, 56)


def test_second_always_in_0_59():
    for off in range(-60, 61):
        _, second = hourly_jitter_offset(off)
        assert 0 <= second <= 59
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/core/test_jitter.py -v`
Expected: `ModuleNotFoundError: No module named 'energy_conductor.jitter'`

- [ ] **Step 3: Write the implementation**

Create `custom_components/energy_conductor/jitter.py`:

```python
"""Pure helpers for scheduling jitter (no HA dependencies).

Used to spread `async_track_time_change` callbacks across the user
base so many HA instances of the integration don't fire at the
same wall-clock second (stampeding herd).
"""

from __future__ import annotations


def hourly_jitter_offset(rand_offset_seconds: int) -> tuple[int, int]:
    """Return (minute, second) for an hourly trigger jittered around HH:55:00.

    Args:
        rand_offset_seconds: A value in [-60, 60]. Caller picks via
            ``random.randint(-60, 60)`` once at startup so the same
            instance fires at the same time every hour.

    Returns:
        ``(minute, second)`` tuple suitable for
        ``async_track_time_change(hass, cb, minute=m, second=s)``.

    The base is HH:55:00 (5 minutes before the hour) so the plan rolls
    before any other hourly automations might key off it. Jitter spreads
    the actual fire time across HH:54:00..HH:56:00 (inclusive both ends).
    """
    if not -60 <= rand_offset_seconds <= 60:
        raise ValueError(
            f"rand_offset_seconds must be in [-60, 60], got {rand_offset_seconds}"
        )
    total = 55 * 60 + rand_offset_seconds
    return divmod(total, 60)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/core/test_jitter.py -v`
Expected: all 9 tests pass.

- [ ] **Step 5: Format and lint**

Run: `uv run ruff format custom_components/energy_conductor/jitter.py tests/core/test_jitter.py && uv run ruff check custom_components/energy_conductor/jitter.py tests/core/test_jitter.py`
Expected: format leaves files unchanged or applies minor adjustments; lint passes.

- [ ] **Step 6: Commit**

```bash
git add custom_components/energy_conductor/jitter.py tests/core/test_jitter.py
git commit -m "feat: add jitter helper for hourly trigger spread"
```

---

## Task 4: Add `last_site_state` cache to coordinator

**Files:**
- Modify: `custom_components/energy_conductor/coordinator.py`

- [ ] **Step 1: Add the import**

In coordinator.py, add `SiteState` to the imports (it's not currently imported — coordinator only deals with decisions). Add this line under the existing model-free imports:

```python
from .model import SiteState
```

- [ ] **Step 2: Add the field to `__init__`**

In `EnergyConductorCoordinator.__init__`, alongside the existing `self.last_overnight_plan` and `self.last_discharge_decision` lines, add:

```python
self.last_site_state: SiteState | None = None
```

- [ ] **Step 3: Populate in `_async_update_data`**

In `_async_update_data`, after the existing `try/except` block that calls `self.adapter.build_site_state()` and before the `discharge_limit` call, add the snapshot assignment on the success path. The relevant section becomes:

```python
async def _async_update_data(self) -> None:
    self.ticks_total += 1
    try:
        state = await self.adapter.build_site_state()
    except EntityProblem as exc:
        self.status = STATUS_DEGRADED
        self.last_error = str(exc)
        _LOGGER.warning("Skipping tick: %s", exc)
        return
    except Exception as exc:
        self.status = STATUS_ERROR
        self.last_error = repr(exc)
        _LOGGER.exception("Unexpected error building SiteState")
        raise UpdateFailed(str(exc)) from exc

    self.status = STATUS_OK
    self.last_error = None
    self.last_site_state = state
    ...
```

(Add `self.last_site_state = state` — one new line directly after `self.last_error = None`.)

- [ ] **Step 4: Populate in `_run_overnight_plan`**

In `_run_overnight_plan`, after the existing `try/except` block that calls `self.adapter.build_site_state()` and before the `plan_overnight` call, add:

```python
self.last_site_state = state
```

The relevant section becomes:

```python
async def _run_overnight_plan(self, _now=None) -> None:
    try:
        state = await self.adapter.build_site_state()
    except EntityProblem as exc:
        _LOGGER.warning("Skipping overnight plan: %s", exc)
        return
    except Exception:
        self.status = STATUS_ERROR
        self.last_error = "Overnight plan failed (see logs)"
        _LOGGER.exception("Unexpected error building SiteState for overnight plan")
        return
    self.last_site_state = state
    try:
        decision = plan_overnight(
            ...
```

- [ ] **Step 5: Verify lint and existing tests pass**

Run: `uv run ruff check custom_components/energy_conductor/coordinator.py && uv run pytest`
Expected: lint passes; all existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/energy_conductor/coordinator.py
git commit -m "feat: cache last SiteState snapshot on coordinator"
```

---

## Task 5: Add hourly jittered trigger to coordinator

**Files:**
- Modify: `custom_components/energy_conductor/coordinator.py`

- [ ] **Step 1: Add imports**

At the top of coordinator.py, add the `random` import (standard library) and the jitter helper import:

```python
import random
```

And in the `from .` import block:

```python
from .jitter import hourly_jitter_offset
```

- [ ] **Step 2: Register the hourly trigger in `async_start`**

In `async_start`, after the existing scheduled-time `async_track_time_change` block (which uses `CONF_OVERNIGHT_PLAN_TIME`), add a second registration for the hourly trigger:

```python
# Hourly re-evaluation with startup-chosen jitter (HH:54..HH:56).
# Spread across instances to avoid stampeding herd.
jitter_minute, jitter_second = hourly_jitter_offset(random.randint(-60, 60))
self._unsubs.append(
    async_track_time_change(
        self.hass,
        self._run_overnight_plan,
        minute=jitter_minute,
        second=jitter_second,
    )
)
_LOGGER.info(
    "Hourly plan re-evaluation scheduled for HH:%02d:%02d",
    jitter_minute,
    jitter_second,
)
```

Place this block before the existing `# If we have no cached plan, run one immediately so the sensor isn't empty` line so the initial run still happens last.

- [ ] **Step 3: Verify lint and existing tests pass**

Run: `uv run ruff check custom_components/energy_conductor/coordinator.py && uv run pytest`
Expected: lint passes; all existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add custom_components/energy_conductor/coordinator.py
git commit -m "feat: hourly plan re-evaluation with startup-jittered offset"
```

---

## Task 6: Add `device_info` to `_BaseSensor`

**Files:**
- Modify: `custom_components/energy_conductor/sensor.py`

- [ ] **Step 1: Add imports**

Add the `DeviceInfo` and `CONF_DEVICE_NAME` imports at the top of sensor.py:

```python
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_DEVICE_NAME, DOMAIN
```

(The existing import block already has `DOMAIN`; ensure `CONF_DEVICE_NAME` is added there.)

- [ ] **Step 2: Add the property to `_BaseSensor`**

After the existing `__init__` of `_BaseSensor`, add:

```python
@property
def device_info(self) -> DeviceInfo:
    device_name = (
        self.coordinator.config.get(CONF_DEVICE_NAME)
        or self.coordinator.hass.config.location_name
    )
    return DeviceInfo(
        identifiers={(DOMAIN, self._entry_id)},
        name=device_name,
        manufacturer="Energy Conductor",
        model="v1",
    )
```

The full updated `_BaseSensor` class becomes:

```python
class _BaseSensor(CoordinatorEntity[EnergyConductorCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id

    @property
    def device_info(self) -> DeviceInfo:
        device_name = (
            self.coordinator.config.get(CONF_DEVICE_NAME)
            or self.coordinator.hass.config.location_name
        )
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=device_name,
            manufacturer="Energy Conductor",
            model="v1",
        )
```

- [ ] **Step 3: Verify lint and existing tests pass**

Run: `uv run ruff check custom_components/energy_conductor/sensor.py && uv run pytest`
Expected: lint passes; all existing tests pass.

- [ ] **Step 4: Commit**

```bash
git add custom_components/energy_conductor/sensor.py
git commit -m "feat: wire all sensors to a single HA device per config entry"
```

---

## Task 7: Add battery diagnostic sensors

**Files:**
- Modify: `custom_components/energy_conductor/sensor.py`

- [ ] **Step 1: Add imports**

Add these to the top of sensor.py:

```python
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy, UnitOfPower
```

- [ ] **Step 2: Add 5 battery sensor classes**

Append these classes after `DischargeDecisionSensor`:

```python
class BatterySocSensor(_BaseSensor):
    _attr_translation_key = "battery_soc"
    _attr_name = "Battery SoC"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-battery-soc"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.battery.soc_percent


class BatteryReservePercentSensor(_BaseSensor):
    _attr_translation_key = "battery_reserve"
    _attr_name = "Battery reserve"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-battery-reserve"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.battery.reserve_percent


class BatteryUsableEnergySensor(_BaseSensor):
    _attr_translation_key = "battery_usable_energy"
    _attr_name = "Battery usable energy"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-battery-usable-energy"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        if state is None:
            return None
        battery = state.battery
        usable_percent = max(0.0, battery.soc_percent - battery.reserve_percent)
        return round(battery.capacity_kwh * usable_percent / 100, 2)


class BatteryMaxChargeSensor(_BaseSensor):
    _attr_translation_key = "battery_max_charge"
    _attr_name = "Battery max charge"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-battery-max-charge"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.battery.max_charge_power_w


class BatteryMaxDischargeSensor(_BaseSensor):
    _attr_translation_key = "battery_max_discharge"
    _attr_name = "Battery max discharge"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-battery-max-discharge"

    @property
    def native_value(self) -> int | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.battery.max_discharge_power_w
```

- [ ] **Step 3: Register the new sensors in `async_setup_entry`**

Update the `async_add_entities` call to include the 5 new battery sensors:

```python
async_add_entities(
    [
        StatusSensor(coordinator, entry),
        OvernightPlanSensor(coordinator, entry),
        DischargeDecisionSensor(coordinator, entry),
        BatterySocSensor(coordinator, entry),
        BatteryReservePercentSensor(coordinator, entry),
        BatteryUsableEnergySensor(coordinator, entry),
        BatteryMaxChargeSensor(coordinator, entry),
        BatteryMaxDischargeSensor(coordinator, entry),
    ]
)
```

- [ ] **Step 4: Verify lint and tests pass**

Run: `uv run ruff check custom_components/energy_conductor/sensor.py && uv run pytest`
Expected: lint passes; all existing tests pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/energy_conductor/sensor.py
git commit -m "feat: add 5 battery diagnostic sensors"
```

---

## Task 8: Add solar forecast diagnostic sensor

**Files:**
- Modify: `custom_components/energy_conductor/sensor.py`

- [ ] **Step 1: Add import for `CONF_FORECAST_SOURCE`**

Add to the existing `from .const import (...)` block:

```python
from .const import CONF_DEVICE_NAME, CONF_FORECAST_SOURCE, DOMAIN
```

- [ ] **Step 2: Add the `SolarForecastSensor` class**

Append after `BatteryMaxDischargeSensor`:

```python
class SolarForecastSensor(_BaseSensor):
    _attr_translation_key = "solar_forecast"
    _attr_name = "Solar forecast today"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-solar-forecast-today"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        return None if state is None else round(state.solar_forecast.total_kwh_today, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.last_site_state
        if state is None:
            return {}
        forecast = state.solar_forecast
        if forecast.slots:
            # Slot-based: source comes from config (solcast vs daily_total_sensor)
            source = self.coordinator.config.get(CONF_FORECAST_SOURCE, "unknown")
        else:
            # Fallback: source is whichever fallback path produced the value
            source = forecast.fallback_source or "unknown"
        return {
            "slot_count": len(forecast.slots),
            "source": source,
            "fallback_source": forecast.fallback_source,
        }
```

- [ ] **Step 3: Register the sensor in `async_setup_entry`**

Add `SolarForecastSensor(coordinator, entry),` to the `async_add_entities` list (after the battery sensors).

- [ ] **Step 4: Verify lint and tests pass**

Run: `uv run ruff check custom_components/energy_conductor/sensor.py && uv run pytest`
Expected: lint passes; all existing tests pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/energy_conductor/sensor.py
git commit -m "feat: add solar forecast diagnostic sensor with source attribute"
```

---

## Task 9: Add tariff / EV / baseline / meta diagnostic sensors

**Files:**
- Modify: `custom_components/energy_conductor/sensor.py`

- [ ] **Step 1: Add the datetime import**

At the top of sensor.py, add:

```python
from datetime import datetime
```

- [ ] **Step 2: Add 4 sensor classes**

Append after `SolarForecastSensor`:

```python
class CheapWindowEndSensor(_BaseSensor):
    _attr_translation_key = "cheap_window_end"
    _attr_name = "Cheap window end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-cheap-window-end"

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.tariff.cheap_window_end


class EVChargerPowerSensor(_BaseSensor):
    _attr_translation_key = "ev_charger_power"
    _attr_name = "EV charger power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-ev-charger-power"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        if state is None or state.ev_charger is None:
            return None
        return state.ev_charger.power_w


class BaselineLoadSensor(_BaseSensor):
    _attr_translation_key = "baseline_load"
    _attr_name = "Baseline load"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-baseline-load"

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.baseline_load_w


class LastSiteStateAtSensor(_BaseSensor):
    _attr_translation_key = "last_state_read_at"
    _attr_name = "Last state read at"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-last-state-read-at"

    @property
    def native_value(self) -> datetime | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.now
```

- [ ] **Step 3: Register all 4 sensors in `async_setup_entry`**

Add to the `async_add_entities` list (after `SolarForecastSensor`):

```python
CheapWindowEndSensor(coordinator, entry),
EVChargerPowerSensor(coordinator, entry),
BaselineLoadSensor(coordinator, entry),
LastSiteStateAtSensor(coordinator, entry),
```

- [ ] **Step 4: Verify lint and tests pass**

Run: `uv run ruff check custom_components/energy_conductor/sensor.py && uv run pytest`
Expected: lint passes; all existing tests pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/energy_conductor/sensor.py
git commit -m "feat: add tariff/EV/baseline/meta diagnostic sensors"
```

---

## Task 10: Create `binary_sensor.py` platform

**Files:**
- Create: `custom_components/energy_conductor/binary_sensor.py`

- [ ] **Step 1: Write the platform module**

Create `custom_components/energy_conductor/binary_sensor.py`:

```python
"""Binary sensor platform — tariff state flags."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_NAME, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EnergyConductorCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EnergyConductorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TariffCheapNowBinarySensor(coordinator, entry),
            TariffDispatchingNowBinarySensor(coordinator, entry),
        ]
    )


class _BaseBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry_id = entry.entry_id

    @property
    def device_info(self) -> DeviceInfo:
        device_name = (
            self.coordinator.config.get(CONF_DEVICE_NAME)
            or self.coordinator.hass.config.location_name
        )
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry_id)},
            name=device_name,
            manufacturer="Energy Conductor",
            model="v1",
        )


class TariffCheapNowBinarySensor(_BaseBinarySensor):
    _attr_translation_key = "tariff_cheap_now"
    _attr_name = "Cheap window now"
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(
        self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-tariff-cheap-now"

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.tariff.cheap_window_now


class TariffDispatchingNowBinarySensor(_BaseBinarySensor):
    _attr_translation_key = "tariff_dispatching_now"
    _attr_name = "EV dispatching now"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self, coordinator: EnergyConductorCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}-tariff-dispatching-now"

    @property
    def is_on(self) -> bool | None:
        state = self.coordinator.last_site_state
        return None if state is None else state.tariff.ev_dispatching_now
```

- [ ] **Step 2: Verify lint passes**

Run: `uv run ruff check custom_components/energy_conductor/binary_sensor.py`
Expected: `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add custom_components/energy_conductor/binary_sensor.py
git commit -m "feat: add binary_sensor platform with 2 tariff flags"
```

---

## Task 11: Register `binary_sensor` platform & update coverage config

**Files:**
- Modify: `custom_components/energy_conductor/__init__.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Update PLATFORMS in `__init__.py`**

Change the `PLATFORMS` constant:

```python
PLATFORMS = ["sensor", "binary_sensor"]
```

- [ ] **Step 2: Verify both `async_setup_entry` and `async_unload_entry` already use `PLATFORMS`**

Read the file — both `async_forward_entry_setups` and `async_unload_platforms` should be passing `PLATFORMS` (this was fixed in PR #2). If they're still passing `[Platform.SENSOR]`, change them to `PLATFORMS`. No code change needed if already done.

- [ ] **Step 3: Add `binary_sensor.py` to coverage omit list**

In `pyproject.toml`, under `[tool.coverage.run]` `omit`, add the new file (keep alphabetical order):

```toml
omit = [
    "custom_components/energy_conductor/__init__.py",
    "custom_components/energy_conductor/adapter.py",
    "custom_components/energy_conductor/binary_sensor.py",
    "custom_components/energy_conductor/config_flow.py",
    "custom_components/energy_conductor/coordinator.py",
    "custom_components/energy_conductor/notifier.py",
    "custom_components/energy_conductor/sensor.py",
    "custom_components/energy_conductor/writer.py",
    "custom_components/energy_conductor/const.py",
]
```

- [ ] **Step 4: Run the full test suite with coverage**

Run: `uv run pytest --cov`
Expected: all tests pass; coverage report appears with `fail_under = 90` met. `binary_sensor.py` should not appear in the report (omitted).

- [ ] **Step 5: Run ruff over the whole project**

Run: `uv run ruff check && uv run ruff format --check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add custom_components/energy_conductor/__init__.py pyproject.toml
git commit -m "feat: register binary_sensor platform"
```

---

## Final verification

- [ ] **Run the full test suite one last time**

Run: `uv run pytest --cov -v`
Expected: all existing tests + 9 new jitter tests pass; coverage ≥ 90%.

- [ ] **Confirm git log shows clean, atomic commits**

Run: `git log --oneline main..HEAD`
Expected: ~10 commits, each one focused on one task.

- [ ] **Manual smoke checklist (for the developer after deploy to HA)**

1. Reload the integration; confirm a single device **Energy Conductor — \<location\>** appears with 15 entities grouped under it.
2. Open device → confirm 3 primary entities (Status, Overnight plan target, Discharge decision) visible.
3. Confirm 12 diagnostic entities (10 sensors + 2 binary sensors) visible under "Diagnostic" section.
4. Edit options → fill **Device name** → save → confirm device renames in HA without re-creating entities.
5. Cross-check 3 random diagnostic entities against their source HA entities (e.g. Battery SoC matches the configured SoC sensor).
6. Watch the **Overnight plan target** entity through the next two hours — confirm it updates at HH:54–HH:56 each hour.
7. Check HA logs at integration startup — `Hourly plan re-evaluation scheduled for HH:MM:SS` line should appear with the jittered time.
8. Trigger a failure on one source sensor (e.g. mark SoC sensor unavailable) → confirm **Status** flips to `degraded` and diagnostic entities retain their last value.

# Energy Conductor — agent guidance

Project-specific notes for working in this codebase. Global preferences (ruff, uv,
conventional commits, git workflow) are in `~/.claude-personal/CLAUDE.md`.

---

## Architecture: pure core / HA glue split

The most important structural rule in this codebase.

**Core modules** (`model.py`, `decisions.py`, `discharge_guard.py`, `regimes.py`,
`rate_watch.py`, `overnight.py`, `baseline.py`, `fallback.py`) must
**not import from `homeassistant`**.
This is enforced by a ruff `TID251` banned-api rule in `pyproject.toml`:

```
"homeassistant".msg = "energy_conductor core must not import from homeassistant"
```

Core modules are pure Python, fully unit-testable without an HA instance, and
covered by `tests/core/` which counts toward the 90% coverage gate.

**HA-glue modules** (`adapter.py`, `coordinator.py`, `notifier.py`, `sensor.py`,
`binary_sensor.py`, `config_flow.py`, `writer.py`) may import HA freely. They are
listed in the coverage `omit` section of `pyproject.toml` and tested (where
covered) via `tests/integration/` using `pytest-homeassistant-custom-component`.

When writing new logic, put the pure computation in a core module and keep the
HA-facing code a thin shim that calls it. See `baseline.py` + `adapter._compute_baseline`
as the canonical example of this pattern.

---

### Coordination inbox protocol

- **Shared directory:** `/tmp/givenergy-coordination`
- **Filename format:** `<unix-epoch>-<recipient>-<description>.md`
  - `recipient` is one of `cli`, `modbus`, or `hass`
  - `description` is a brief slug, optionally referencing an issue (e.g. `mock-pdu-logging-#42`)
  - Example: `1780409632-modbus-mock-pdu-logging.md`
- **Writing a message:** create a new file; never mutate an existing one
- **Replying:** create a new file with the current epoch, the original sender as addressee, and a description prefixed with `re-`. Only reply if actionable, save on pleasantries.
- **Content:** describe the expected outcome at the API boundary — not how to implement it; include enough context to act without this conversation's history. It does not need to be overly verbose since agents share a lot of common knowledge across these repos.

---

## Test layout

```
tests/core/          # Pure Python, no HA imports, fast, in the 90% gate
tests/integration/   # HA harness (hass fixture), for glue layer behaviour
```

Run everything: `uv run pytest --cov`

Core modules that don't yet have integration tests are in the `omit` list in
`pyproject.toml`. When you add integration tests for a module, remove it from
`omit` so the coverage gate starts enforcing it.

The `tests/integration/conftest.py` provides:
- `auto_enable_custom_integrations` (autouse)
- `mock_config_entry` — a minimal valid EC config entry (forecast source = none)

---

## HA version specifics (2026.5+)

Several HA APIs changed in 2026.5 and the codebase has been updated accordingly.
Do not revert these:

- **`async_request_refresh` is a coroutine** — schedule via
  `hass.async_create_task(self.async_request_refresh())` in `@callback` contexts.
  Calling it directly discards the coroutine silently.
- **`statistics_during_period` is synchronous** — call without `await`.
- **`OptionsFlow.__init__`** must not accept or assign `config_entry` — it is a
  read-only property injected by the framework. Build `defaults` inside the step
  handler via `self.config_entry.data` / `self.config_entry.options`.

---

## Config entry versioning and migrations

Config entry VERSION is tracked in `EnergyConductorConfigFlow.VERSION` in
`config_flow.py`. Migration logic lives in `async_migrate_entry` in `__init__.py`.

Current version: **2**. Migration history:
- v1→v2: renamed `cheap_rate_sensor` → `off_peak_sensor` in `entry.data`.

When bumping VERSION: increment the constant, add a migration branch in
`async_migrate_entry`, and add the old key as `_LEGACY_*` in `const.py` (used
only in the migration, never elsewhere).

---

## Terminology: off-peak, not cheap

The word "cheap" has been removed from the entire codebase (config keys, model
fields, display names, tests, comments). Use "off-peak" everywhere.
`CONF_OFF_PEAK_SENSOR` = `"off_peak_sensor"`, `TariffState.off_peak_now`, etc.

The only surviving `cheap` references are:
- `_LEGACY_CONF_CHEAP_RATE_SENSOR` — used only in the migration function
- Entity unique-IDs (`tariff-cheap-now`, `cheap-window-end`) — intentionally
  left unchanged for entity-registry stability

---

## Deployment cycle

1. `git push origin main`
2. HACS redownload: `ha_hacs_download(repository_id="1246088843")`
3. **Ask the user before restarting HA.** Restarts are disruptive. Stage the
   download and tell the user what's waiting; they'll trigger the restart.

Config-entry **reload** (triggered by options-flow changes) does not re-import
Python modules. A full HA restart is required for any code change to take effect.

---

## Sensor naming conventions

The EC device is named `"Energy Conductor {site}"` where `site` = `CONF_DEVICE_NAME`
(if set) or `hass.config.location_name` (e.g. "Blithe") or "Home". Entity IDs
follow `sensor.energy_conductor_<site>_<slug>`, e.g.
`sensor.energy_conductor_blithe_calculated_baseline_load`.

Display names use plain English without device prefix (the device name provides
context). Examples:
- "Calculated baseline load" (not "Blithe baseline load")
- "Off-peak rate sensor active" (binary sensor)
- "Overnight off-peak tariff end" (timestamp sensor)
- "Solar forecast tomorrow" (when Solcast slot-based)

---

## Solcast forecast integration notes

**Required sensor**: `sensor.solcast_pv_forecast_forecast_tomorrow`  
**Not**: `forecast_today`, `forecast_next_x_hours`, `Blithe` aggregate (no `detailedForecast` attr).

The `_slots_from_solcast` method converts Solcast's local-timezone datetimes
(Europe/London/BST) to UTC before constructing `ForecastSlot` objects. Do not
remove this conversion — `ForecastSlot.__post_init__` enforces UTC and will
silently drop all slots if they arrive in non-UTC timezone.

The full runtime compatibility picture is documented in `docs/integrations.md`.

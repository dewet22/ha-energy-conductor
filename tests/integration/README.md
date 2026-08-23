# Integration smoke notes

These are **manual** tests run against a real Home Assistant instance.
CI does not run them. They cover the things the unit suite can't —
ConfigFlow UX, notification delivery, recorder query shape, lifecycle.

## Installing the dev copy

1. From your HA config directory:
   ```bash
   ln -s /path/to/ha-energy-conductor/custom_components/energy_conductor custom_components/energy_conductor
   ```
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → "Energy Conductor".

## Pre-release checklist

Run all of these before tagging a release.

### ConfigFlow

- [ ] Fresh install: every step accepts entity selections from the dropdown
- [ ] Battery step: entering an entity that doesn't expose a `max` attribute still lets the flow proceed (default 3000W is used)
- [ ] Tariff step: leaving `dispatching_sensor` empty completes the flow
- [ ] Forecast step: each of `solcast` / `daily_total_sensor` / `none` works
- [ ] EV step: skipping it (no power sensor) completes the flow
- [ ] Behaviour step: defaults to `dry_run`
- [ ] Submitting the flow creates a config entry and a single device

### OptionsFlow

- [ ] Editing `write_mode` from `dry_run` → `live` takes effect on the next tick (verify in logs)
- [ ] Editing `notify_target` redirects subsequent notifications
- [ ] Editing `daily_kwh_target` (with no daily-energy history sensor configured) changes the `Calculated daily target` diagnostic sensor's value

### Runtime

- [ ] Within 30s of setup, `sensor.energy_conductor_status` reads `ok`
- [ ] `sensor.energy_conductor_overnight_plan` (now "Battery SoC setpoint") populates with a numeric % within 60s, with `regime` and `rate_watch` attributes set
- [ ] Toggling the off-peak or dispatch sensor on transitions the regime to off-peak-charge (setpoint 100%, discharge limit 0) and a notification arrives on the mobile target; toggling off returns to self-consume
- [ ] Discharge guard: temporarily set the off-peak binary sensor to `on` (e.g. via Developer Tools → States). Within seconds, a `Discharge cap → 0W` notification arrives. (This is the EV-protection path: EV smart-charging always lands inside an off-peak/dispatch window, so the battery idles and the car pulls grid.)
- [ ] Discharge guard dedupe: across an off-peak → peak → off-peak cycle, only one notification per regime transition (not 120/hour).

### Failure modes

- [ ] Rename the battery SOC entity in HA → `sensor.energy_conductor_status` becomes `degraded` and `last_error` describes the missing entity
- [ ] Restore the SOC entity → status returns to `ok` within 30s
- [ ] Set `write_mode = live` but use a non-existent `discharge_limit_entity` → a `WRITE FAILED` notification arrives once per regime transition (not on every tick)

### Lifecycle

- [ ] `Settings → Devices & Services → Energy Conductor → Reload` succeeds without HA restart
- [ ] Removing the integration leaves no orphan entities (search `energy_conductor` in Developer Tools → States)
- [ ] HA restart preserves the dedupe state freshly (first decision after restart re-notifies, then dedupes as normal)

## Reporting issues

If a smoke check fails, capture:
- HA core version
- The line from `home-assistant.log` with the relevant `[custom_components.energy_conductor.*]` tag
- A snapshot of the relevant state(s) from Developer Tools → States

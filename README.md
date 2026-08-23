# ha-energy-conductor

**Entity-driven energy coordination for Home Assistant.**

[![CI](https://github.com/dewet22/ha-energy-conductor/actions/workflows/test.yml/badge.svg)](https://github.com/dewet22/ha-energy-conductor/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/dewet22/ha-energy-conductor/branch/main/graph/badge.svg)](https://codecov.io/gh/dewet22/ha-energy-conductor)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/github/license/dewet22/ha-energy-conductor)](LICENSE)

`ha-energy-conductor` is a Home Assistant integration that coordinates energy flow across your home's generation, storage, and controllable loads — without knowing anything about the specific hardware involved. You point it at your existing HA entities (sensors and controls), describe your devices in abstract terms, and it makes dispatch decisions on your behalf.

---

## What v1 delivers

A single always-on SoC-setpoint regime governing the battery, plus a hot-water safety net that rides alongside it.

**Battery SoC-setpoint regime** (runs on every state change, ~30 s tick)

At a tariff where `off-peak import ÷ round-trip efficiency < export rate` (true of most current UK off-peak/export combinations, at roughly 90% efficiency), every marginal kWh grid-charged overnight is worth more than it costs — even surplus PV that would otherwise have charged the battery exports at the higher rate instead. So the battery's charge-target control is driven as a two-sided SoC setpoint, evaluated every tick from already-configured sensors:

| Regime | Condition (priority order) | Setpoint | Discharge limit |
|---|---|---|---|
| **Off-peak charge** | Off-peak sensor on, or dispatch sensor on | 100 % | 0 W |
| **Self-consume** | Otherwise | The charge control's own minimum (typically ~4 %) | Full rated discharge power |

- During an off-peak window (or dispatch) the inverter grid-charges to 100% and the discharge limit holds the battery at zero — no wasteful drain while the target converges. This also covers EV protection: on a whole-house meter, EV smart-charging always lands inside an off-peak/dispatch window, so the battery idles and the car pulls off-peak grid rather than the battery.
- Outside an off-peak window it's plain Eco: the battery discharges to serve load, all the way down to the inverter's hardware reserve. There's deliberately no floor above that reserve — a floor there would idle a charged battery during the peak rate for no benefit, since the next off-peak window refills the battery regardless.
- The self-consume setpoint is read from the charge-target control's own minimum value, not from any reserve configuration, so a misdescribed reserve can never be written back as a floor.
- A stale or unavailable off-peak/dispatch sensor falls through to self-consume — the failure mode is plain Eco, never a stuck charge.

**Always-on charge-slot pinning**

The two-sided setpoint depends on charge slot 1 being active around the clock — GivEnergy inverters otherwise only grid-charge inside a scheduled window. Map your inverter's slot-1 start and end time entities under **Battery** and EC pins the slot open once, healing it if something external changes it. The two pickers are optional in the schema: without both mapped, EC still steers the charge target, but the inverter only honours it inside whatever charge slots already exist (a charge ceiling, not a two-sided setpoint) — the integration logs a warning in live mode and the setpoint sensor's `slot_pin` attribute reads `unconfigured`. Slot 2 and the reserve-SoC number entity are never written.

**Rate-watch (warn-only)**

This strategy only pays while `off-peak import ÷ 0.9 < export rate`. EC checks that inequality live — only while in the off-peak-charge regime, where the import-rate sensor reads the off-peak rate by definition — and sends a "Tariff economics changed" notification if it breaks. It never changes strategy on its own; a hysteresis band around the boundary stops a hovering rate from flapping the notification.

**Hot-water reserve safety net** (optional; evaluated alongside the battery regime)

For a solar diverter such as the myenergi Eddi running on solar surplus alone, it estimates the tank's stored reserve from an open-loop energy balance — anchored by the diverter's "tank full" status event — and sends a notify-only prompt to add a short manual boost when a run of cloudy days is projected to leave the tank inadequate. It never controls the diverter; it only advises, so you can drop a scheduled overnight boost and rely on solar with a safety net.

All decisions are sent as mobile notifications before any write is made. A **dry-run mode** (the default) sends the notifications but skips the writes, so you can validate behaviour before going live.

---

## Installation

### HACS (recommended)

1. Open HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/dewet22/ha-energy-conductor` — category **Integration**
3. Install **Energy Conductor** and restart Home Assistant

### Manual

Copy `custom_components/energy_conductor/` into your HA `config/custom_components/` directory and restart.

---

## Configuration

**Settings → Devices & Services → Add Integration → Energy Conductor**

The config flow walks through these steps:

| Step | Required | What you configure |
|---|---|---|
| Battery | ✓ | SoC sensor, charge control entity, discharge limit entity, capacity, reserve % (display-only, see below), optional reserve-SoC sensor, charge slot-1 start/end time entities |
| Tariff | ✓ | Off-peak binary sensor, optional EV dispatching sensor, overnight window end time |
| Forecast source | ✓ | Solcast sensor, daily-total sensor, or none |
| Forecast details | ✓ | Optional live generation sensor, winter/summer fallback range, hemisphere |
| Loads & learning | ✓ | Optional home-load and managed-load sensors, optional daily-energy sensor, daily kWh target |
| EV charger | optional | Power sensor, minimum activation power |
| Hot water | optional | Eddi diverted-energy and status sensors, optional total-energy and diverter-power sensors (the latter draws the dashboard's diversion rail), tank capacity, heater power, reserve threshold, depletion fallback |
| Behaviour | ✓ | Write mode (dry-run / live), notify target, device name |

Every group can be changed at any time via the integration's **Configure** option — a menu of the same focused forms — without re-running the full flow.

**The battery reserve floor (%) is display-only.** It describes the inverter's reserve floor for energy calculations — it is not, and never was, a minimum-SoC control. If you configure the optional reserve-SoC sensor, that live entity always wins for actuation purposes; the static field only feeds usable-energy calculations and the projection floor when no live sensor is available. This field is never written to.

### Migrating from the overnight planner

If you're upgrading from a version that planned an overnight charge target, the regime engine replaces that decision outright rather than refining it. Practically, that means:

- The charge-slot-1 start/end pickers above are new and required for the regime to work — GivEnergy inverters only grid-charge inside a scheduled slot, and the regime needs that slot open around the clock.
- Any existing automations that set the charge window or cap discharge during dispatch (e.g. hand-rolled GivTCP automations) should be disabled once EC is pinning the slot and driving the setpoint — leaving both running risks fighting writes. A dry run (`write_mode: dry_run`) alongside your existing automations for a few days lets you compare what EC would have done before switching over.
- If your stored reserve floor was set to something other than your inverter's true hardware minimum (as a workaround for the old planner), reset it to the true value — it's inert now, but there's no reason to leave the wrong number in a display field.

### Adapting your sensors with template sensors

Conductor reads each input as a *role* — house load, solar power, diverter power, and so on — not a specific device. Where your hardware doesn't expose a clean entity for a role, compose one with a Home Assistant [template sensor](https://www.home-assistant.io/integrations/template/) and point conductor at it. The adaptation lives in your configuration, which is what keeps conductor itself device-agnostic.

The common case is **house load excluding a solar diverter**. Most house-load (or "consumption") power sensors sit upstream of the diverter, so when an Eddi soaks up surplus solar the reading spikes — which both jumps the dashboard's consumption line and inflates the learned baseline that feeds the mission tape's SoC projection. Netting the diverter out gives conductor a clean household floor. Create this as a UI Template helper, or in YAML:

```yaml
template:
  - sensor:
      - name: House load excluding diversion
        unit_of_measurement: W
        device_class: power
        state_class: measurement
        state: >
          {{ [ states('sensor.house_load_power')|float(0)
               - states('sensor.eddi_power')|float(0), 0 ] | max }}
        availability: "{{ has_value('sensor.house_load_power') }}"
```

Replace `sensor.house_load_power` with your inverter's load/consumption power sensor and `sensor.eddi_power` with the diverter's active-power entity, then set it as the house-load sensor under **Loads & learning**. The `device_class` and `state_class` lines matter — they let conductor read the sensor's history and learn the baseline from it.

One caveat: a freshly-created template sensor records only from the moment it exists, so the dashboard's consumption line backfills over the following ~12 hours and the baseline relearns over a couple of days. Both are one-time — the underlying inverter and diverter sensors keep their own full history.

---

## Dashboard

The integration bundles a Lovelace **dashboard strategy** that builds a single calm overview — battery and hot-water state, tonight's charge target, the plan's reasoning, and a couple of trend graphs. It's a glanceable "is everything in good hands tonight?" view.

To use it, create a new dashboard (**Settings → Dashboards → Add Dashboard**), open the **raw configuration editor** (top-right ⋮ menu → *Edit dashboard* → ⋮ → *Raw configuration editor*), and replace the whole config with:

```yaml
strategy:
  type: custom:energy-conductor
```

That's it — there are no entity IDs to edit. The strategy resolves every entity from the registry by its stable `unique_id` on each render, so it works regardless of your device name and survives entity renames (including the Home Assistant 2026.6 area-prefix convention). Cards whose backing entity is absent — for example the hot-water graph when no Eddi sensors are configured — are simply omitted.

If you run more than one Energy Conductor instance, pin the one you want:

```yaml
strategy:
  type: custom:energy-conductor
  device: blithe   # device name, or the config-entry id
```

> **Occasional "Error loading the dashboard strategy" on a cold load.** On a hard refresh, a first visit, or a load competing with many other custom frontend resources, the dashboard may briefly show *"Error loading the dashboard strategy: Timeout waiting for strategy element …"*. This is a Home Assistant limitation affecting all network-loaded dashboard strategies — HA gives the strategy module a fixed 5-second window to register and loads custom resources without awaiting them, so a contended load can lose that race. A normal reload serves the module from cache and isn't affected, so it doesn't bite in day-to-day use; if you hit it, just reload. Tracked upstream at [home-assistant/frontend#52570](https://github.com/home-assistant/frontend/issues/52570).

---

## The problem

A modern home energy system has several independently smart components that aren't coordinated with each other:

- A GivEnergy (or other) hybrid inverter happily discharges the battery into an EV charger drawing off-peak overnight grid power — wasteful.
- A solar diverter (Eddi, iBoost) and an EV charger (Zappi, Wallbox) compete for the same solar surplus without knowing each other's thresholds — the EV starves below its 1.4 kW minimum while the diverter absorbs everything.
- Overnight battery charging is set to a fixed SOC target that's too high on sunny days and too low on cloudy ones.
- A saving session fires at 5 pm and the battery is already half-depleted because no one turned off the dishwasher.
- There was 8 kWh of surplus solar on Tuesday and it all exported at 4p/kWh while the washing machine sat idle.

Each component makes locally rational decisions. The system as a whole does not.

---

## Design approach

### HA entities as the abstraction layer

`ha-energy-conductor` does not contain device-specific code. It has no GivEnergy adapter, no Zappi adapter, no Octopus adapter. Instead it uses the same model as the HA Energy Dashboard: you point it at entities that already exist in your HA instance, and it infers capability from entity type and attributes.

A battery charge rate limit is a `number` entity — the `min` and `max` attributes tell conductor what the hardware can do. An EV charger enable is a `switch` or `select`. A solar forecast is a `sensor`. The hardware brand is invisible.

This means conductor works with any inverter, any EV charger, any forecast service, and any tariff provider — as long as that device has a HA integration that exposes the right entity types. No per-device templates, no regex auto-discovery, no adapter layer.

### Capability degrades gracefully

Conductor operates at the level of sophistication your entity mapping supports:

- Map only an off-peak binary sensor → basic off-peak-window SoC-setpoint control
- Also map import/export rate sensors → the rate-watch validates the strategy's economics live
- Also map hourly forecast slots → hot-water diversion estimate and a slot-based mission-tape SoC projection
- Also map deferrable and advisory loads → full coordination across all devices

Each additional entity slot unlocks a richer strategy. None are required to get started.

---

## Roadmap

**Shipped** — SoC-setpoint regime with always-on charge-slot pinning; discharge guard; warn-only tariff rate-watch; baseline load and daily-target learned from recorder statistics; hot-water reserve safety net for solar diverters; entity reference resilience across renames; a registry-resolved dashboard strategy

**Planned** — saving-session precharge; active deferrable-load dispatch (EV, HWC); advisory load notifications; formal OpenADR/EEBUS grid event reception

---

## Relationship to existing projects

**[Predbat](https://github.com/springfall2008/batpred)** is the closest and most mature project in this space. It runs 48-hour LP optimisation for UK hybrid inverter users and has strong Octopus tariff support. Conductor differs in: being a native HA integration rather than an AppDaemon app; using purely entity-pointing configuration rather than per-inverter `apps.yaml` templates; targeting simpler condition-based coordination rather than full LP schedule optimisation; and treating EV, HWC, and advisory loads as first-class entities rather than secondary concerns. For users who want full 48-hour cost optimisation on Octopus Agile, Predbat is the better tool. Conductor's edge is setup simplicity, device-agnosticism, and the coordination layer between heterogeneous devices.

**[EMHASS](https://github.com/davidusb-geek/emhass)** is an LP optimisation engine that produces dispatch schedules and leaves execution to user automations. Conductor closes the control loop but operates at lower complexity — condition-based rules rather than constrained optimisation.

**[EV Smart Charging](https://github.com/jonasbkarlsson/ev_smart_charging)** handles EV charging scheduling against price windows but has no battery, HWC, or cross-device coordination awareness.

None of the existing projects combine battery + EV + thermal storage + advisory loads + grid events in a single entity-driven native HA integration. That is the gap conductor fills.

---

## Contributing

Bug reports, real-world device configurations, and feedback on the discharge and planning logic are all welcome. Particularly useful right now:

- Config examples for hardware not mentioned above (heat pumps, second batteries, smart appliances with HA integrations)
- Experience with managed load scenarios (Octopus Intelligent, other VPP programmes)
- Tariff structures from outside the UK where export economics change the dispatch priorities

---

## License

Apache License 2.0

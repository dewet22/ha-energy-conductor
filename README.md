# ha-energy-conductor

**Entity-driven energy coordination for Home Assistant.**

[![CI](https://github.com/dewet22/ha-energy-conductor/actions/workflows/test.yml/badge.svg)](https://github.com/dewet22/ha-energy-conductor/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/dewet22/ha-energy-conductor/branch/main/graph/badge.svg)](https://codecov.io/gh/dewet22/ha-energy-conductor)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/github/license/dewet22/ha-energy-conductor)](LICENSE)

`ha-energy-conductor` is a Home Assistant integration that coordinates energy flow across your home's generation, storage, and controllable loads — without knowing anything about the specific hardware involved. You point it at your existing HA entities (sensors and controls), describe your devices in abstract terms, and it makes dispatch decisions on your behalf.

---

## What v1 delivers

Two always-on coordination loops that run against entities you already have:

**Discharge guard** (runs on every state change, ~30 s tick)

Applies a simple limit to battery discharge:

| Condition | Discharge limit |
|---|---|
| Off-peak window active (incl. smart-dispatch slots), or about to open | 0 W — battery idles, grid fills demand |
| Otherwise | Full rated discharge power |

This is what stops a hybrid inverter from draining the battery into an EV charger: on a whole-house meter, EV smart-charging always lands inside an off-peak/dispatch window, so the battery idles and the car pulls cheap grid rather than the battery. (An earlier per-EV "cap discharge at house baseline" regime was removed once it was clear the off-peak signal already covers every case — see `discharge_guard.py` for the reasoning.)

**Overnight charge planning** (runs once per evening at a configured time)

Calculates a battery charge target based on tomorrow's conditions:

1. Reads tomorrow's solar forecast (Solcast, a daily-total sensor, or an automatic fallback)
2. Calculates the *morning gap*: hours from the end of the off-peak window until solar meaningfully contributes
3. Estimates the gap's energy cost: `gap_hours × baseline_load`
4. Adds any forecast deficit against your configured daily kWh target
5. Sets the battery charge target, clamped between your reserve level and 100 %

If no forecast source is configured, it falls back to historical recorder statistics (same ±14-day calendar window in prior years) and then to a seasonal cosine curve.

**Hot-water reserve safety net** (optional; evaluated alongside the evening plan)

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
| Battery | ✓ | SoC sensor, charge control entity, discharge limit entity, capacity, reserve %, optional reserve-SoC sensor |
| Tariff | ✓ | Off-peak binary sensor, optional EV dispatching sensor, overnight window end time |
| Forecast source | ✓ | Solcast sensor, daily-total sensor, or none |
| Forecast details | ✓ | Optional live generation sensor, winter/summer fallback range, hemisphere |
| Loads & learning | ✓ | Optional home-load and managed-load sensors, optional daily-energy sensor, daily kWh target |
| EV charger | optional | Power sensor, minimum activation power |
| Hot water | optional | Eddi diverted-energy and status sensors, optional total-energy sensor, tank capacity, heater power, reserve threshold, depletion fallback |
| Behaviour | ✓ | Write mode (dry-run / live), notify target, plan time, minimum target SoC, device name |

Every group can be changed at any time via the integration's **Configure** option — a menu of the same focused forms — without re-running the full flow.

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

---

## The problem

A modern home energy system has several independently smart components that aren't coordinated with each other:

- A GivEnergy (or other) hybrid inverter happily discharges the battery into an EV charger drawing cheap overnight grid power — wasteful.
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

- Map only an off-peak binary sensor → basic overnight charge scheduling
- Also map a price sensor → cost-threshold dispatch decisions
- Also map hourly forecast slots → morning-gap calculation and intraday routing
- Also map deferrable and advisory loads → full coordination across all devices

Each additional entity slot unlocks a richer strategy. None are required to get started.

---

## Roadmap

**Shipped** — discharge guard; overnight charge planning (Solcast / daily-total / seasonal forecast); baseline load and daily-target learned from recorder statistics; hot-water reserve safety net for solar diverters; entity reference resilience across renames; a registry-resolved dashboard strategy

**Planned** — saving-session precharge; forecast bias correction from (forecast, actual) pairs; active deferrable-load dispatch (EV, HWC); advisory load notifications; formal OpenADR/EEBUS grid event reception

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

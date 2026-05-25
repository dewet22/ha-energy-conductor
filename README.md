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

Applies a three-regime limit to battery discharge:

| Condition | Discharge limit |
|---|---|
| Cheap tariff window active | 0 W — battery idles, grid fills demand |
| EV smart-dispatch active *and* EV drawing power | House baseline load — battery covers the house but doesn't feed the EV |
| Otherwise | Full rated discharge power |

The guard prevents a hybrid inverter from depleting a battery into an EV charger that is simultaneously drawing cheap overnight grid power — a common source of wasted energy.

**Overnight charge planning** (runs once per evening at a configured time)

Calculates a battery charge target based on tomorrow's conditions:

1. Reads tomorrow's solar forecast (Solcast, a daily-total sensor, or an automatic fallback)
2. Calculates the *morning gap*: hours from the end of the cheap window until solar meaningfully contributes
3. Estimates the gap's energy cost: `gap_hours × baseline_load`
4. Adds any forecast deficit against your configured daily kWh target
5. Sets the battery charge target, clamped between your reserve level and 100 %

If no forecast source is configured, it falls back to historical recorder statistics (same ±14-day calendar window in prior years) and then to a seasonal cosine curve.

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

The config flow walks through six steps:

| Step | Required | What you configure |
|---|---|---|
| Battery | ✓ | SoC sensor, charge control entity, discharge limit entity, capacity, reserve % |
| Tariff | ✓ | Cheap-rate binary sensor, optional EV dispatching sensor, overnight window end time |
| Forecast source | ✓ | Solcast sensor, daily-total sensor, or none |
| Forecast details | ✓ | Optional live generation sensor, winter/summer fallback range, hemisphere |
| EV charger | optional | Power sensor, minimum activation power |
| Behaviour | ✓ | Write mode (dry-run / live), notify target, plan time, daily kWh target |

The **Behaviour** settings can be changed at any time via the integration's **Configure** option without re-running the full flow.

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

- Map only a cheap-rate binary sensor → basic overnight charge scheduling
- Also map a price sensor → cost-threshold dispatch decisions
- Also map hourly forecast slots → morning-gap calculation and intraday routing
- Also map deferrable and advisory loads → full coordination across all devices

Each additional entity slot unlocks a richer strategy. None are required to get started.

---

## Roadmap

**v1 (current)** — discharge guard + overnight charge planning + Solcast/daily/seasonal forecast

**v2** — baseline load from recorder stats (replaces the 400 W placeholder); saving-session precharge; forecast bias correction from (forecast, actual) pairs

**v3** — deferrable load dispatch (EV, HWC); advisory load notifications; formal OpenADR/EEBUS grid event reception

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

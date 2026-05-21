# Energy coordination ecosystem — prior art survey

*Surveyed May 2026. Project activity levels and feature sets change; treat as a snapshot.*

---

## Predbat / BatPred

**Repo:** [springfall2008/batpred](https://github.com/springfall2008/batpred)  
**Stars/forks:** 282 / 127 · ~3,000 commits · 689 releases · last release May 2026  
**Install:** AppDaemon app (requires the AppDaemon HA add-on)

The dominant open-source battery optimisation project for UK users. Runs every 5 minutes, forecasts 48 hours ahead in 30-minute slots using linear programming, and automatically programs the inverter to charge from grid or solar at optimal times. First-class support for Octopus Agile, Flux, Intelligent, Go and other dynamic UK tariffs. Solar forecast via Solcast, Open-Meteo, Forecast.solar.

**Supported inverters:** GivEnergy (via GivTCP or GE Cloud Direct), Solis Cloud, SolaX, SolarEdge, Huawei, FoxESS, Sofar, Sunsynk, LuxPower, SigEnergy, Tesla Powerwall, Solar Assistant (generic Modbus). EV coordination works via any charger that exposes a power/SOC sensor in HA (Wallbox, Ohme, Zappi).

**Architecture:** Entity-pointing for reads; per-inverter `apps.yaml` templates with regex auto-discovery for writes. The `inverter_type` config key selects the write-command profile, so there is per-device logic baked in at the template level even though the runtime wiring goes through HA entity IDs.

**Strengths:**
- Very large user community; active Facebook group, YouTube tutorials, community wiki (terravolt)
- Deep Octopus tariff integration including Intelligent dispatch awareness
- Handles export optimisation on Agile Export

**Limitations:**
- AppDaemon dependency — not a native HA integration
- Configuration is complex: `apps.yaml` has hundreds of parameters; per-inverter template files require ongoing maintenance as new hardware ships
- UK tariff-centric; carbon prediction UK-only
- 504 open issues reflects scale of use but also indicates surface area
- Multi-inverter support exists but all inverters must run in lockstep
- Occasionally triggers GivEnergy calibration cycles unexpectedly

**Commercial fork:** predbat.com offers a cloud-hosted managed version. Separate from the open-source project despite sharing the name.

---

## EMHASS — Energy Management for Home Assistant

**Repo:** [davidusb-geek/emhass](https://github.com/davidusb-geek/emhass)  
**Stars/forks:** 607 / 143 · 120 releases · last release May 2026 (v0.17.4)  
**Install:** HA add-on exposing a REST API; companion HACS integration [siku2/hass-emhass](https://github.com/siku2/hass-emhass) pulls schedules back into HA

LP optimisation engine (recently migrated from PuLP to CVXPY, ~4–5× faster solves) that computes an optimal daily dispatch schedule for batteries, controllable loads, and solar. Produces a plan and leaves execution to user automations — it deliberately does not close the control loop.

**Architecture:** Purely entity-pointing. Users declare HA entity IDs for solar power, battery SOC, grid prices, and controllable loads in a YAML config file. No device-specific adapters. Works with any inverter that exposes sensors in HA.

**Strengths:**
- Inverter-agnostic
- Full LP flexibility — can model complex multi-device households
- Popular in continental Europe and Australia where export economics differ from UK

**Limitations:**
- Does not close the control loop — user must write automations to execute the schedule
- Configuration requires understanding LP modelling concepts
- Less turnkey than Predbat for UK ToU users
- CVXPY migration is recent; some edge cases still being worked through

---

## EV Smart Charging

**Repo:** [jonasbkarlsson/ev_smart_charging](https://github.com/jonasbkarlsson/ev_smart_charging)  
**Stars/forks:** 294 / 45 · 69 releases · last release October 2025 (v2.5.1)  
**Install:** HACS

Schedules EV charging into the cheapest price windows within a configurable completion deadline. Supports continuous or split charging sessions.

**Architecture:** Entity-pointing: user supplies a price sensor, SOC sensor, and charger control entity. Auto-detects VW We Connect ID and OCPP integrations. Pricing: Nord Pool, Energi Data Service, GE-Spot, Entso-e, TGE, or any template sensor.

**Limitations:**
- EV charging only — no battery inverter, HWC, or cross-device coordination awareness
- Does not account for home battery SOC or available solar when deciding whether to charge from grid

---

## Solar Optimizer

**Repo:** [jmcollin78/solar_optimizer](https://github.com/jmcollin78/solar_optimizer)  
**Stars/forks:** 225 / 23 · 69 releases · last release September 2025 (v3.6.1)  
**Install:** HACS

Controls deferrable loads (water heaters, dishwashers, EV chargers, pumps) to absorb excess solar production rather than exporting it. Core logic is load-shifting against solar surplus, not battery charge/discharge scheduling.

**Architecture:** UI-configured, entity-pointing. Manages fixed-power and variable-power devices. Has some battery awareness (can account for battery state) but treats it as context rather than a first-class dispatchable device.

**Strengths:**
- Simple to configure for basic solar self-consumption maximisation
- Good model for threshold-based load control

**Limitations:**
- No tariff awareness
- No overnight scheduling or forecast integration
- Battery dispatch is out of scope

---

## PowerSync

**Repo:** [bolagnaise/PowerSync](https://github.com/bolagnaise/PowerSync)  
**Stars/forks:** 64 / 11 · 592 releases · last release May 2026  
**Install:** HACS · Licence: PolyForm (non-commercial)

Battery energy management with dynamic pricing, targeting primarily Australian (Amber Electric) and UK (Octopus) markets plus EU day-ahead EPEX pricing. Supports solar curtailment on AC-coupled inverters during negative price periods.

**Supported inverters:** Tesla Powerwall, FoxESS H1/H3, Sigenergy, Solax Hybrid, GoodWe, Sungrow SH-series, AlphaESS. GivEnergy not listed.

**Architecture:** Entity-based setup via HACS wizard for some devices; adapter-first for the listed hardware. Non-commercial licence restricts use.

**Limitations:**
- Adapter-first rather than truly entity-agnostic
- Non-commercial PolyForm licence
- Very high release count (592) relative to star count suggests heavy automated versioning rather than stability

---

## HAEO — Home Assistant Energy Optimiser

**Repo:** [hass-energy/haeo](https://github.com/hass-energy/haeo)  
**Stars/forks:** 52 / 14 · 24 open issues · 23 open PRs · last release February 2026  
**Install:** Early alpha

LP-based real-time energy optimiser that models batteries, grid, solar, and loads as a graph of nodes and connections, minimising cost over a 48-hour horizon. Designed as the optimiser backend in the `hass-energy` organisation.

**Architecture:** Entity-pointing. Exposes an optimisation API; users configure HA entity IDs for SOC, solar forecast, price sensors, and control targets.

**Limitations:** Alpha quality; 23 open PRs suggests active but incomplete development. Not recommended for production use yet.

---

## Octopus Energy HA integration

**Repo:** [BottlecapDave/HomeAssistant-OctopusEnergy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy)  
**Stars/forks:** 927 / 98 · 217 releases · last release April 2026 (v18.2.1)  
**Install:** HACS

Provides Octopus Energy account data as HA sensors: electricity and gas rates, consumption, Agile/Flux/Intelligent slot data, saving sessions, power-ups, and tariff comparison. Does not do battery dispatch — it is a data source consumed by Predbat, EMHASS, and user automations.

Exposes:
- `binary_sensor.octopus_intelligent_dispatching` — when Octopus Intelligent has control
- Planned intelligent charge slots as sensor attributes
- Saving session windows and active state
- Power-up windows
- Current and upcoming half-hourly rates for Agile

Effectively a required companion for any UK Octopus user running an energy management system.

---

## Solcast HA integration

**Repo:** [BJReplay/ha-solcast-solar](https://github.com/BJReplay/ha-solcast-solar)  
**Stars/forks:** 407 / 54 · 2,055 commits · actively maintained through 2026  
**Install:** HACS

Integrates Solcast's PV forecast API into HA. Provides sensors for estimated generation at 10th/50th/90th percentile confidence, 30-minute interval forecasts up to 14 days, dampening factors per array, and estimated actuals. Free API tier: 10 calls/day (sufficient for 30-minute refresh on one rooftop site).

This is a forecast data provider, not an optimiser. Predbat, EMHASS, and conductor all consume it as an input.

The built-in HA `forecast_solar` integration (official, no API key required) is a simpler alternative but notably less accurate for site-specific prediction.

---

## GivEnergy + Myenergi coordination

No dedicated "GivEnergy + Myenergi" HACS integration exists. In practice, users run both the `givenergy-hass` (or GivTCP) and `ha-myenergi` (CJNE, ~927-star adoption) integrations in parallel and write their own automations to coordinate: pause battery discharge when Zappi is drawing grid power, let Zappi know when battery is full and solar is surplus, etc.

The GivEnergy community forum has threads on coordinating the two but no turnkey solution. Predbat has EV charging awareness that partially addresses the problem, but within its AppDaemon/UK-tariff-centric architecture. The specific combination of GivEnergy battery + Zappi EV charger + Eddi HWC diverter is one of the clearest motivating use cases for ha-energy-conductor.

---

## Architectural pattern summary

| Pattern | Projects | Notes |
|---|---|---|
| Entity-pointing only | EMHASS, HAEO, EV Smart Charging, Solar Optimizer | Works with any hardware; no device-specific code |
| Entity-pointing + per-inverter templates | Predbat | Ships apps.yaml templates per inverter; write commands differ per type |
| Adapter-first + entity fallback | PowerSync | Integration-specific adapters for target hardware; entity fallback for others |
| Data provider only | Solcast, Octopus Energy | No dispatch; upstream data feed consumed by optimisers |
| **Entity-pointing, closed-loop, cross-device** | **ha-energy-conductor** | **Target architecture — the gap none of the above fills** |

---

## The gap

No existing project combines all of:

- Native HA integration (no AppDaemon)
- Purely entity-pointing for both reads *and* writes (no per-device adapter code)
- Battery + EV + thermal storage + deferrable loads + advisory loads in one consistent config-flow UX
- Grid events (saving sessions, power-ups, carbon intensity) as first-class concepts
- Managed load awareness (Octopus Intelligent, VPP operators)
- Cross-device coordination (e.g. don't discharge battery into EV charger)
- Advisory load notifications with human-confirmed deferred execution

That is the space ha-energy-conductor is designed to occupy.

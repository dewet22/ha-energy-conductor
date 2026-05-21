# ha-energy-conductor

**Entity-driven energy coordination for Home Assistant.**

`ha-energy-conductor` is a Home Assistant integration that coordinates energy flow across your home's generation, storage, and controllable loads — without knowing anything about the specific hardware involved. You point it at your existing HA entities (sensors and controls), describe your devices in abstract terms, and it makes dispatch decisions on your behalf.

> **Status: early design stage.** This repository captures the project's design intent and architecture. No functional code exists yet. Contributions, feedback, and real-world device configurations are welcome as the implementation takes shape.

---

## The problem

A modern home energy system has several independently smart components that aren't coordinated with each other:

- A GivEnergy (or other) hybrid inverter happily discharges the battery into an EV charger drawing cheap overnight grid power — wasteful.
- A solar diverter (Eddi, iBoost) and an EV charger (Zappi, Wallbox) compete for the same solar surplus without knowing each other's thresholds — the EV starves below its 1.4 kW minimum while the diverter absorbs everything.
- Overnight battery charging is set to a fixed SOC target that's too high on sunny days and too low on cloudy ones.
- A saving session fires at 5pm and the battery is already half-depleted because no one turned off the dishwasher.
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

## The abstract model

### Sources

| Concept | HA entity | Notes |
|---|---|---|
| Solar generation | `sensor` (W) | Instantaneous generation |
| Grid import/export | `sensor` (W) | Signed or separate import/export sensors |

### Storage devices

A storage device absorbs surplus and releases it later. Electrochemical batteries and hot water cylinders (thermal storage) share the same abstraction.

| Config field | Type | Example |
|---|---|---|
| SOC / level sensor | `sensor` | `sensor.battery_soc`, `sensor.hwc_temperature` |
| Charge rate control | `number` | `number.givenergy_charge_power_limit` |
| Max charge rate | W (config) | `2500` |
| Full threshold | % or °C (config) | `100`, `60` |
| Priority | int (config) | `0` (fills first) |

### Deferrable loads

A deferrable load has a minimum activation power — it won't run usefully below that threshold. The EV charger is the canonical example; heat pumps have the same characteristic.

| Config field | Type | Example |
|---|---|---|
| Power sensor | `sensor` | `sensor.zappi_power` |
| Enable control | `switch` / `select` | `switch.zappi_eco_mode` |
| Min activation power | W (config) | `1400` |
| Max power | W (config) | `7400` |
| Priority | int (config) | `1` |
| Min run duration | min (config) | `15` |

### Advisory loads

An advisory load cannot be automatically started — a human has to physically prepare it (load the washing machine, fill the dishwasher). Conductor monitors predicted surplus windows and notifies you in advance. If the device has a smart integration (SmartThings, LG ThinQ, etc.), an optional `start_control` entity enables **human-confirmed deferred execution**: conductor sends the night-before notification, you confirm and load the machine, conductor starts it at the right time.

| Config field | Type | Example |
|---|---|---|
| Typical power | W (config) | `2000` |
| Typical duration | min (config) | `90` |
| Start control (optional) | `button` / `switch` | `button.samsung_washer_start` |
| Notify target | HA notify service | `notify.mobile_app_phone` |
| Notify lead time | h (config) | `12` (notify the night before) |

### Export

Export is a configurable sink. When no export price sensor is mapped, conductor treats export as last resort (below all other loads). When a price sensor is present, export competes with storage and load dispatch based on current value. Negative export prices (Octopus Agile curtailment events) suppress export entirely.

---

## Tariff support

Conductor works with any tariff that can be expressed as HA entities:

| Tariff type | Entity needed | Unlocked strategy |
|---|---|---|
| Fixed rate | None | Static overnight window from config |
| Simple ToU (Go, Economy 7) | `binary_sensor` (is cheap now?) | Charge in cheap window, respect window end |
| Multi-rate ToU | `sensor` (price in p/kWh) | Threshold-based dispatch |
| Dynamic / Agile | `sensor` + time-series attributes | Look-ahead slot optimisation |

The provider is invisible. Octopus Go, Bulb, EDF, any utility with a HA integration — or a template sensor you write yourself — all look the same.

---

## Solar forecast

Conductor accepts two forecast input shapes:

**Daily total** — a single `sensor` with tomorrow's expected generation in kWh. Simpler; conductor uses a conservative assumption for morning gap timing.

**Hourly / half-hourly blocks** — a set of sensors (or a sensor with forecast attributes) giving per-slot expected generation. Solcast's HA integration exposes 30-minute blocks natively. With hourly data conductor can calculate:

1. **Morning gap duration** — how long after the cheap tariff ends before solar meaningfully contributes. On a day where the forecast shows generation arriving at noon rather than 8am, the overnight charge target needs to cover 4–5 extra hours of base load.
2. **Intraday routing** — whether predicted surplus will clear deferrable load thresholds (EV minimum 1.4 kW) or only threshold-free absorbers (HWC).

### Forecast bias correction *(v2)*

Generic forecast services cannot account for local factors: a neighbour's tree, panel soiling, a shading obstruction at certain sun angles. Over time, conductor accumulates (forecast, actual) pairs and computes a rolling site-specific correction factor — reducing systematic over- or under-prediction without requiring a different forecast provider. The correction infrastructure is built in from the start; the learning logic ships in v2.

---

## Grid events

Grid events are time-bounded periods where conductor temporarily overrides its normal dispatch strategy.

| Event type | Trigger | Conductor response |
|---|---|---|
| Saving Session | `binary_sensor` (session active) | Maximise battery discharge, defer non-essential loads, avoid grid import |
| Power-up / free window | `binary_sensor` (window active) | Opportunistically charge battery and run deferrable loads |
| Greener window | `sensor` (carbon intensity) | Prefer lower-carbon windows for charging even at slightly higher cost |

The Octopus Energy HA integration exposes saving sessions and power-ups natively. Carbon intensity is available via grid.watch and Electricity Maps integrations. Any provider whose events surface as HA binary sensors with time window attributes works without conductor knowing the provider's identity.

**Carbon intensity** is a first-class optimisation objective alongside cost — users can configure a weighting between the two.

---

## Managed loads

Some loads are controlled by an external party — an Octopus Intelligent dispatch, a VPP operator, a demand flexibility programme — and conductor should not issue competing commands to those devices during managed windows.

A managed load declares an external controller sensor. When that sensor indicates the controller is active, conductor defers all dispatch decisions for that device and reads (but does not write) its state.

For Octopus Intelligent specifically, the HA Octopus Energy integration exposes `binary_sensor.octopus_intelligent_dispatching` and the planned charge slots as attributes. Conductor reads the planned slots when building its own schedule and treats them as fixed constraints rather than override targets.

### Future: formal provider coordination *(v3)*

The tension between local energy management systems and VPP operators is an active area of industry work. Standards like **OpenADR** (US-led demand response signalling) and **EEBUS** (European home energy interoperability) are designed to let grid operators signal dispatch intent to home systems in a standardised way, eliminating the need for provider-specific workarounds. If conductor eventually implements OpenADR reception, it can receive Octopus's signals directly rather than inferring them from HA sensor state — and participate formally rather than politely stepping aside.

---

## Overnight planning cycle

Each evening, before the overnight cheap tariff window opens, conductor runs a planning cycle:

1. Reads tomorrow's solar forecast (daily total or hourly blocks)
2. Calculates the morning gap — time from tariff end to meaningful solar contribution
3. Computes the overnight charge target: enough to cover morning gap base load, plus any forecast deficit, minus expected daytime surplus
4. Applies site-specific forecast correction if available (v2)
5. Sets the battery charge target
6. Evaluates predicted daytime surplus after all storage and deferrable loads are satisfied
7. If surplus windows exist that match advisory load profiles, sends night-before notifications with recommended windows and confirmation prompts

The night-before timing for advisory notifications is deliberate — a human needs lead time to load the machine, not a real-time alert they'll miss.

---

## Relationship to existing projects

**[Predbat](https://github.com/springfall2008/batpred)** is the closest and most mature project in this space. It runs 48-hour LP optimisation for UK hybrid inverter users and has strong Octopus tariff support. Conductor differs in: being a native HA integration rather than an AppDaemon app; using purely entity-pointing configuration rather than per-inverter `apps.yaml` templates; targeting simpler condition-based coordination rather than full LP schedule optimisation; and treating EV, HWC, and advisory loads as first-class entities rather than secondary concerns. For users who want full 48-hour cost optimisation on Octopus Agile, Predbat is the better tool. Conductor's edge is setup simplicity, device-agnosticism, and the coordination layer between heterogeneous devices.

**[EMHASS](https://github.com/davidusb-geek/emhass)** is an LP optimisation engine that produces dispatch schedules and leaves execution to user automations. Conductor closes the control loop but operates at lower complexity — condition-based rules rather than constrained optimisation.

**[EV Smart Charging](https://github.com/jonasbkarlsson/ev_smart_charging)** handles EV charging scheduling against price windows but has no battery, HWC, or cross-device coordination awareness.

None of the existing projects combine battery + EV + thermal storage + advisory loads + grid events in a single entity-driven native HA integration. That is the gap conductor fills.

---

## Contributing

The project is at the design stage — issues, discussion, and real-world device configurations are the most valuable contributions right now. Particularly useful:

- Config examples for devices not mentioned above (heat pumps, second batteries, smart appliances)
- Experience with managed load scenarios (Octopus Intelligent, other VPP programmes)
- Tariff structures from outside the UK where export economics change the dispatch priorities

---

## License

Apache License 2.0

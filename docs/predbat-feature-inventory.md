# Predbat feature inventory

*Surveyed May 2026 against Predbat v8.39.4 documentation at https://springfall2008.github.io/batpred/ and the GitHub repository at https://github.com/springfall2008/batpred.*

*Purpose: clean-room reference only. Predbat is proprietary (personal use only). This document describes behaviour and feature surface; no source code is reproduced.*

---

## Core optimisation

**Objective:** Minimise total electricity cost (import spend minus export income) across a rolling 48-hour planning window, subject to battery and inverter constraints.

**Scheduling loop:** Runs every 5 minutes. Time granularity: 30-minute slots.

**Search algorithm:** Two-pass coarse-to-fine. Coarse pass evaluates a reduced set of charge/discharge window combinations; fine pass concentrates search near the coarse optima. Optimises: number and length of charge windows, discharge/export windows, and target SoC for each window.

**Objective function components:**
- Import cost minus export income
- Battery round-trip loss (separate configurable charge / discharge / inverter-conversion loss coefficients)
- Configurable virtual cycle-cost penalty per kWh cycled (discourages unnecessary cycling)
- Configurable self-sufficiency premium (biases toward grid independence without changing tariff model)
- Configurable carbon penalty (g/kWh equivalent cost, biases charging toward low-carbon grid periods)

**Operating modes:**
- Monitor only (no inverter writes)
- Control SoC only (adjusts charge target percentage; windows not moved)
- Control charge (manages charge windows and SoC targets; no forced export)
- Control charge and discharge (full control including forced grid export)

A global read-only flag prevents all writes regardless of mode.

**Loss modelling:** For hybrid inverters, DC solar charging bypasses the inverter-loss term. AC-coupled installations incur inverter loss on all paths.

**Calibration mode detection:** Detects inverter-triggered battery calibration cycles; suspends itself and configures the inverter for calibration completion (max charge rate, 100 % SoC target, minimum reserve).

---

## Charging

- Selects start/end times for one or more grid-charge windows during low-rate periods
- Calculates minimum SoC target per window to achieve lowest net cost; configurable improvement threshold prevents over-charging
- User-configurable minimum comfortable reserve going into next cheap period
- User-configurable absolute SoC floor below which discharge is never planned
- Learns battery charge curve (rate tapering near 100 %) from historical observations; auto or manual
- Low-power charge mode: reduces charge rate when only a small top-up is required
- Freeze charge: holds battery at current SoC while solar covers load; no grid draw or battery movement
- Solar reduces effective grid draw within charge windows; DC generation accounted for without inverter loss

---

## Discharging

- Schedules forced discharge-to-grid when export rate exceeds effective re-import cost (accounting for losses); configurable minimum margin threshold
- Export freeze: excess solar flows to grid but battery is not actively discharged
- Demand (eco) mode: battery discharges to cover home load; excess solar charges battery or exports if full
- Freeze discharge: holds battery SoC; all available solar is exported
- Configurable hardware reserve floor and ceiling
- Multiple-inverter balancing: detects SoC divergence; selectively suspends charge/discharge on one unit until parity restored

---

## EV integration

**Two planning modes:**

*Predbat-led:* Computes cheapest charging slots for the vehicle based on tariff, required kWh, and plugged-in window.

*Octopus-led (Intelligent Go):* Reads dispatch slots assigned by Octopus; plans home battery activity around them rather than recomputing them.

**Multi-vehicle support:** Per-vehicle sensors for planned state, current SoC, battery size, charge slots, and rate limits.

**Load accounting:** When charger is inside CT clamp measurement boundary, EV energy is stripped from historical load records to prevent distorting the baseline.

**Supported charger integrations:** Wallbox, Myenergi Zappi (Eco+ mode), Ohme (cloud + HA native, including legacy HACS), Hypervolt, PodPoint, Tesla Fleet API, and generic sensor-based configuration.

**Vehicle SoC integrations:** Renault, Toyota EU, Tesla for tracking without a dedicated charger integration.

---

## Solar forecast

**Supported providers:**
- Solcast — primary/recommended; reads half-hourly probabilistic forecast from the HA integration; uses central (p50) and pessimistic (p10) estimates
- Forecast.solar — less accurate; configured with array parameters
- Open-Meteo — free, no API key; fetches Global Tilted Irradiance and converts via PVWatts cell-temperature model; requires orientation, tilt, efficiency parameters

**Multiple arrays:** Supported on all providers; total forecast is summed across all configured arrays.

**Auto-calibration:** Builds a site-specific correction factor from historical (forecast, actual) pairs to compensate for systematic over- or under-prediction due to local obstructions, soiling, etc.

**Pessimism blending:** Configurable blend between p50 and p10 scenario; separate load pessimism scaling for worst-case runs.

**Forecast output:** Daily totals through day+3 plus half-hourly attributes with confidence bands, published as HA sensors.

---

## Tariff support

**Flat rates:** Single import/export rate, or multiple named time bands.

**Time-of-use (multi-band):** Any number of named rate bands; entire 24-hour period must be covered.

**Half-hourly dynamic pricing:**
- Octopus Agile (import and export)
- Octopus Tracker
- Czech Republic 15-minute spot
- Energi Data Service hourly rates
- Strømligning (Denmark) 15-minute interval
- Nord Pool

**Smart tariffs:** Octopus Intelligent Go (dispatch-slot aware).

**International:** Frank Energie (Netherlands), Energi Data Service, Nordpool, EDF and E.ON Next via Kraken API.

**Export tariffs:** Parallel to import rates; half-hourly dynamic export rates read directly from provider integrations.

**Negative pricing:** Handles negative import/export rates; adjusts behaviour when grid operators are paying consumers to import.

**Standing charges:** Included in total cost tracking.

---

## Grid events

**Octopus Saving Sessions:**
- Can auto-enrol account via Octopus Energy HA integration (requires Octoplus pre-registration)
- Raises effective export rate in the model during session window; optimiser schedules grid discharge
- Configurable load-scaling factor models reduced household consumption during the session

**Octopus free electricity / power-up windows:**
- Detects via Octopus integration sensor or website scrape fallback
- Maximises battery charging during free windows

**Axle Energy VPP:**
- Polls Axle API for upcoming demand-response events
- For export events: schedules forced discharge at the event rate (~£1/kWh)
- For import events: schedules additional charging
- Optional mode: Predbat enters read-only while Axle controls the inverter directly during active events

**Carbon intensity:**
- Fetches UK National Grid carbon data by postcode, or reads from a HA sensor
- Published as colour-coded display in plan view
- `carbon_metric` makes carbon intensity part of the objective function

**MeteoAlarm weather alerts:**
- Monitors MeteoAlarm feed for active weather warnings in user's region
- Appends "Alert" to status string; sensor exposes active warning details

---

## Load forecasting

**Historical baseline:** Builds a half-hourly load profile from HA sensor history; configurable number of past days and per-day weighting. EV charging and solar diverter energy are optionally stripped before computing the baseline.

**Machine-learning load predictor:**
- Optional neural-network component; fetches 7 days of 5-minute-interval load history
- ~900k parameter autoregressive model; produces 48-hour-ahead forecast in 576 single-step predictions
- Optionally incorporates Open-Meteo temperature data as additional input
- Blends with historical daily pattern rather than replacing it
- Trains on-device within the Predbat process

**Predheat (home heating simulation):**
- Models water-based central heating (gas boiler or heat pump)
- Simulates internal/external temperature dynamics, thermostat hysteresis, heat-loss rates
- Produces forecast of heating activation windows and estimated half-hourly energy consumption
- Feeds forecast directly into Predbat's battery plan; makes overnight charge targets aware of predicted heating demand on cold days

**Holiday mode:** When active, uses yesterday's actual consumption rather than the multi-day rolling average; adapts quickly to reduced-occupancy consumption patterns.

**Manual load adjustment:** Runtime API allows temporarily scaling load predictions up or down for known anomalous days (dinner parties, guests, etc.).

---

## Multi-device support

- `num_inverters` for multiple inverters; all run in lockstep (same windows and targets)
- SoC-balancing between inverters when divergence detected
- AC-coupled vs hybrid flag changes loss model for solar paths
- Multiple PV arrays across all forecast providers
- Battery capacity and rate scaling factors for inaccurate inverter-reported values
- Community-documented configurations for dissimilar inverter types running simultaneously

---

## Notifications and alerting

- `notify_devices` targets HA notification services (mobile app, Telegram, email, etc.) for error events
- `predbat.status` error flag and last-update timestamp enable watchdog automations
- Weather alerts propagate to status string
- VPP events exposed via binary sensors for user-written notification automations
- No built-in push channel — all notifications flow through HA `notify` domain

---

## Inverter write surface

Commands issued to inverters:

- Set charge mode: rate (W) + target SoC (%)
- Set discharge/export mode: rate (W)
- Set eco/demand mode (no forced charge or discharge)
- Freeze charge (hold SoC; solar covers load)
- Freeze discharge (hold SoC; solar exports)
- Set reserve SoC level
- Set charge rate
- Set discharge rate

Communication methods used: REST/HTTP, MQTT, HA service calls (shell/Modbus bridge), direct cloud APIs.

**Write-rate tracking:** Cumulative inverter register write counter with documented guidance on keeping writes to ~270/day or fewer for flash-memory-limited hardware.

---

## Distinctive / unusual features

**MCP server:** Built-in Model Context Protocol server exposes the system's state and controls to AI assistants (Claude, ChatGPT, etc.) via a bearer-token-secured endpoint. Natural language queries and overrides.

**Tariff comparison with battery modelling:** Simulates how the optimiser would behave under each comparison tariff — not a static rate-to-usage mapping, but a full re-run of the battery schedule — producing a cost prediction per tariff that accounts for dynamic dispatch.

**Gas vs electricity hot water decision:** When iBoost and gas rates are both configured, computes per-time-slot whether electric diversion or gas heating is cheaper and signals the diverter accordingly.

**Marginal cost matrix:** Publishes a matrix of effective cost of an additional kWh at various load levels and future time points. Can drive automations for deferrable high-power loads.

**Export trigger signals:** Notifies external automations when export rates exceed a configurable threshold, enabling high-power load activation or postponement based on export pricing.

**iBoost / solar diverter as a modelled load:** The diverter is planned as a variable load in the optimisation (configurable max power, daily energy cap, gas-rate comparison). `binary_sensor.predbat_iboost_active` can directly trigger an immersion heater.

**Plan card interactive slot override:** Clicking any 30-minute slot in the UI overrides that slot's planned activity without touching configuration files.

**Scenario benchmarking:** Offline mode for testing the optimisation algorithm against historical scenarios.

**Plugin component architecture:** Core and optional components (database, HA interface, web server, MCP, ML predictor, weather/carbon APIs, Octopus, cloud inverter APIs, VPP, Predheat, alert feed) individually enabled/disabled/restarted via the web interface.

**Predheat heating simulation:** Full standalone home heating model (boiler or heat pump) that produces a heating-energy forecast consumed by the battery plan. Makes the system aware of heating demand when planning overnight charge on cold days.

**Inverter flash-memory protection guidance:** Explicit register-write counter, documented configuration trade-offs, and a list of features that increase write frequency with recommended mitigation strategies.

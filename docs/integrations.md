# Integration compatibility notes

Runtime discoveries from running Energy Conductor in production. Not design
intent — things you only find out by doing it.

---

## Solar forecast

### Solcast (recommended for slot-based projection)

**Integration:** [HACS — Solcast PV Solar](https://github.com/BJReplay/ha-solcastpv-solar-forecast)

The Solcast integration exposes per-day sensors, each with a `detailedForecast`
attribute containing 48 half-hourly slots. EC reads this for the hot-water
diversion estimate and the mission-tape SoC projection.

**Required sensor:** `sensor.solcast_pv_forecast_forecast_tomorrow`

Configure EC's Solcast sensor to the **Forecast Tomorrow** sensor, **not**:
- `Forecast Today` — contains today's slots, not tomorrow's; use the separate
  "Forecast Today" picker (`forecast_solcast_today_sensor`) if you want today's
  slots stitched into the projection as well.
- `Forecast Next X Hours` / `Blithe` / aggregate sensors — no `detailedForecast`
  attribute; EC silently falls back to seasonal.

**Attribute format** (as of Solcast HA integration 4.x):
```python
detailedForecast = [
    {
        "period_start": datetime(2026, 6, 2, 1, 0, tzinfo=Europe/London),  # local tz
        "pv_estimate": 2.594,    # AVERAGE POWER (kW) over the slot (median) — NOT kWh
        "pv_estimate10": 1.9,    # pessimistic
        "pv_estimate90": 3.1,    # optimistic
    },
    ...  # 48 entries, covering the full day in local time
]
```

**`pv_estimate` is average power in kW, not energy.** Energy for a slot =
`pv_estimate * 0.5h`. Summing `pv_estimate` directly double-counts (the slot is
30 min, not 1 h). Confirmed against Solcast's own `peak_forecast_tomorrow` sensor:
it equals the highest single-slot `pv_estimate` to 4 sig figs, proving the value is
power, not energy. EC applies the `* 0.5` conversion in `_slots_from_solcast`.

Slot timestamps are in the HA instance's local timezone (e.g. BST/Europe/London),
**not UTC**. EC converts them to UTC on read.

**Accuracy vs forecast.solar:** In production, Solcast and forecast.solar typically
agree within 10–15% on daily totals. Solcast is preferred when available because
the slot data feeds a more detailed SoC projection; forecast.solar currently only
exposes a scalar daily total.

---

### Forecast.Solar (scalar daily total)

**Integration:** [HACS — Forecast.Solar](https://github.com/home-assistant/core/tree/dev/homeassistant/components/forecast_solar)

**Available sensor:** `sensor.energy_production_tomorrow` — scalar kWh total for
tomorrow, no per-slot breakdown.

Configure as `daily_total_sensor` forecast source and point at this sensor.
EC uses it as the day's kWh estimate for the hot-water diversion calculation.
It has no per-slot data, so the mission-tape SoC projection sees zero forecast
PV during the day under this source (no synthetic slots are invented).

**Upgrading to slot-based:** The forecast.solar integration computes hourly estimates
internally but does not currently expose them as a sensor attribute. A `detailedForecast`
attribute equivalent would make it a first-class alternative to Solcast for EC's
slot-based path.

---

## EV charger

### myenergi Zappi

**Integration:** [HACS — ha-myenergi](https://github.com/CJNE/ha-myenergi)

**Sensor for EC:** `sensor.myenergi_zappi_ev_internal_load_ct1` (Zappi EV Charge Power)

**Polling behaviour:** The myenergi integration polls the cloud relay on every
coordinator tick. At fast `scan_interval` values (≤ ~15 s), the volume of history
fetches (24-point hourly history on every tick) can trip the myenergi cloud relay's
rate limit, stranding all entities as `unavailable` — including after HA restarts
(the first refresh hits the same block). The [upstream issue is a patched local
copy](../README.md); a PR to decouple history from live refresh has been opened.

**`scan_interval` changes:** The integration's live options-change reload path is
broken upstream (leaked listener + hand-rolled unload/setup race). Changing
`scan_interval` via Configure requires an **HA restart** to take effect; the
integration will appear `loaded` but entities will strand unavailable otherwise.

**`last_updated` staleness:** The Zappi sensor only writes state on value change.
When charging at a steady rate, `last_updated` may be many minutes old even though
the device is active and the reading is valid. EC's EV sensor read deliberately
skips the time-based staleness check and relies solely on the `unavailable`/`unknown`
state for offline detection.

---

## Battery / inverter

### GivEnergy (via givenergy-local)

**Authoritative integration:** `givenergy_local` (local polling, not GivTCP cloud).
Use `sensor.givenergy_inverter_*` entities.

**GivTCP note:** GivTCP also exposes similar sensors (e.g. `givtcp_*`). Both are
present on this install; `givenergy_local` is preferred as the long-term stable
integration.

**Load power sensor:** `sensor.givenergy_inverter_sa2114g047_load_power` measures
**whole-house consumption including EV and Eddi** — GivEnergy cannot distinguish
managed loads from baseline. EC's baseline calculation accounts for this via the
managed-loads filter (idle-floor method).

**Number entities — the control surface (all SoC-% or power-%, no watts):**

| Entity | Range | Meaning | EC wiring |
|---|---|---|---|
| `charge_target_soc` | 4–100 % | **SoC setpoint** | regime engine writes here (100% cheap-charge, control minimum self-consume) |
| `battery_soc_reserve` | 4–100 % | **Minimum SoC floor** | optional reserve sensor reads here |
| `battery_charge_limit` | 0–50 % | Charge *power rate* (% of max) | not used |
| `battery_discharge_limit` | 0–50 % | Discharge *power rate* (% of max) | discharge guard — see below |
| `battery_discharge_min_power_reserve` | 4–100 % | Dynamic discharge reserve | not used |
| `inverter_max_output_active_power` | 0–100 % | Inverter output cap (%) | not used |

**True battery capacity:** `sensor…battery_nominal_capacity` (e.g. 17.7 kWh) — use
this for `CONF_BATTERY_CAPACITY_KWH`, not a guessed round number. A wrong capacity
scales every SoC%↔kWh conversion in the SoC projection and usable-energy calcs.

**Discharge guard live write is BLOCKED.** EC's discharge guard reasons in **watts**
(hardware-agnostic), but `givenergy_local` exposes only a 0–50 % power-*rate* knob
with no documented watt reference. There is no watt-valued discharge entity to write
to, so the discharge-guard live write is deferred. In dry-run the guard still
produces correct watt decisions for notification/inspection. Unblocking requires
`givenergy_local` to expose battery discharge (and charge) power limits as
watt-valued `number` entities — the Modbus registers are watt-valued, so the data
exists. (Cross-agent request dispatched via the givenergy-coordination inbox.)

The charge-target setpoint path is **not** blocked: `charge_target_soc` is a % SoC
value that matches what the regime engine writes.

---

## Hot water diverter

### myenergi Eddi

**Sensor for managed-loads exclusion:** `sensor.myenergi_eddi_hwc_internal_load_ct1`

**Seasonal behaviour:**
- Summer: diverts excess PV after battery charging capacity is exceeded; runs
  midday, self-funded. Does not affect baseline because baseline is computed from
  overnight/idle hours where Eddi is typically off.
- Winter: draws from overnight off-peak tariff when insufficient PV. Runs during
  the off-peak window, so the discharge guard (limit = 0 W) is active during the
  same period — battery does not discharge to cover Eddi's draw.

The filter-to-idle baseline method (exclude buckets where Eddi > 50 W) handles
both seasons correctly without seasonal logic.

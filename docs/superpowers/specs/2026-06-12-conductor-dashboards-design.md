# Conductor dashboards: mission, long-term, ledger — design

Brainstormed and approved 2026-06-12. The "is this paying for itself?"
question outgrew the GivEnergy integration's dashboard exploration
(givenergy-hass `feat/dashboard-mission`): costs and long-term energy flows
span every device the conductor coordinates — battery, PV, grid, EV (Zappi),
hot-water diversion (Eddi), and gas — so the broad views live here. That
branch is inspiration only: concepts port, code does not, except mechanisms
proven in live use and explicitly adopted (noted below).

## Decisions (locked during brainstorm)

- **One spec, phased build.** Four independently shippable phases:
  foundations -> long-term -> tape -> ledger. Foundations first because the
  payback accumulator only counts from the day it exists.
- **Conductor-first.** No changes to givenergy-hass; the fate of its
  overlapping views is decided later from real use.
- **Skeleton: Mission entry + real tabs.** Mission (glance strip + tape +
  summary tiles) becomes the dashboard entry; Long-term and Ledger are real,
  bookmarkable views; the existing bedtime view stays as a final "Tonight"
  tab, unchanged. Watch-point: the bedtime view may eventually merge into
  Mission — real use decides, the way the GE deep-Tape tab was deprecated.
- **Data sourcing: read-through + model the gaps.** Billing-grade numbers
  (Octopus accumulative cost / standing charges / gas / export, live via the
  Home Mini) are read from explicitly configured entities. The conductor
  creates Python sensors only for what exists nowhere: avoided-cost models,
  EV attribution, the payback accumulator. Every number that is not
  billing-grade carries a visible *modelled* tag.
- **Configuration is explicit.** Entity pickers in a new options group; no
  autodetection. Unconfigured features drop their lines/views silently.

## Views

1. **Mission** (entry) — glance strip (SoC, PV now + today, today net cost,
   conductor status tick); the rolling tape; summary tiles linking Ledger and
   Long-term.
2. **Long-term** — small-multiple calendar heatmaps, one per configured flow
   (PV, house load, grid in/out, EV, hot water, gas), annual kWh per card;
   selecting a flow opens the deep view: density heatmap (hour x day),
   calendar heatmap, weekly energy line. Data from long-term statistics.
3. **Ledger** — headline strip (today net / month-to-date / saved-today
   *modelled*); (A) whole-home actuals: import split by band, standing
   charges, gas, export credit; (B) avoided costs *modelled*: solar self-use,
   battery peak-shift, Eddi gas-displacement, EV solar charge; (C) EV
   breakdown with optional vs-public-charging comparator; (D) payback
   tracker: recovered vs configured capital cost, run-rate, projected
   break-even *modelled*.
4. **Tonight** — the existing bedtime view, unchanged.

## The tape (`custom:ec-tape`)

Rolling -12h -> +12h, now pinned centre. Layers, all in v1:

- **Context bands** — off-peak tariff tints (past + upcoming), planned EV
  dispatch windows from the Octopus `planned_dispatches` attribute (the
  committed plan, not inference), conductor plan blocks (charge-to-target
  window + target).
- **Energy curves** — solar actual area to now, forecast line across the
  whole window (the under/over-forecast gap over the past half is the
  performance-vs-forecast story); house load area behind.
- **SoC + projection** — history solid; projection dashed, served by the
  conductor's own overnight plan model via a sensor attribute
  (`soc_projection`), not client-side re-derivation.
- **Decision rail** — diamond markers: conductor writes (guard regime flips,
  plan target written, verification mismatches) plus events derived from
  threshold crossings in already-fetched history (export began/stopped,
  hot-water divert, SoC 100%/floor).

Isolated SoC samples disagreeing with both neighbours by >25 points are
rejected (plausibility filter). Each missing feed drops its layer with a
one-line legend note; a failed fetch costs one layer, never the card.

## Money sensors (Python)

Computed on the coordinator tick; created only when their source entities are
configured; RestoreSensor accumulators (restore from a previous day starts
`_today` sensors at 0); `device_class=MONETARY`, `state_class=TOTAL` with
midnight `last_reset` so month-to-date falls out of LTS. Rate units p/kWh or
GBP/kWh normalised from the rate entity's unit. Tariff entity unavailable ->
the dependent sensors go unavailable rather than accumulating priced-at-zero
garbage; they resume from the running total when rates return.

- `counterfactual-cost-today` — house consumption tick-priced at the import
  rate (today with no PV/battery). Modelled.
- `savings-today` — counterfactual - configured net import cost + export
  earnings; attributes break out solar self-use, battery peak-shift, Eddi
  gas-displacement (Eddi kWh x gas rate), EV solar-charge value. Modelled.
- `ev-charge-cost-today` — EV charger energy tick-priced at the rate in
  force.
- `cumulative-savings` — adds each day's savings at rollover; attributes:
  capital_cost, install_date, recovered_pct, run_rate, projected_breakeven.

Pure pricing/rollover/projection arithmetic lives in a new core module
(`money.py`, no homeassistant imports, counted by the coverage gate).

## Frontend registration

New card modules (`ec-longterm.js`, `ec-tape.js`, `ec-ledger.js`) are
registered as storage-mode Lovelace **resources** (created on first start,
version-bumped thereafter, registered once HA has started; YAML-mode resource
lists are user-managed and left alone) — `add_extra_js_url` alone is
fire-and-forget and loses the load race on panel views (upstream
frontend#52570; mechanism proven live in givenergy-hass, adopted by explicit
decision). The strategy additionally awaits bounded
`customElements.whenDefined` for its cards before emitting views that
reference them. ASCII-only JS source (the static path mangles multibyte
UTF-8).

## Degradation summary

| Missing | Effect |
| --- | --- |
| Cost/rate entities (options) | Money sensors not created; ledger lines drop |
| Tariff rate unavailable (outage) | Dependent money sensors unavailable, resume later |
| Solar forecast | Tape forecast line drops; projection uses plan + baseline |
| `planned_dispatches` attr | Dispatch bands drop; off-peak tints remain |
| Statistics for a flow | That flow omitted from Long-term with legend note |
| Any layer fetch failure | That layer drops, never the card |
| Registry fetch failure | Existing friendly-notice view |

## Testing

- **vitest**: strategy emission (view set per config, no dangling refs,
  bedtime view preserved); pure helpers per card (window arithmetic,
  downsampling, event detection, spike rejection, dispatch parsing, LTS
  bucketing, colour scaling, week aggregation, ledger line gating) against
  canned fixtures.
- **pytest**: money pricing across rate changes, midnight reset, restart
  restore, p-vs-GBP normalisation, tariff outage, counterfactual and savings
  arithmetic, payback rollover; options flow for the costs group;
  soc_projection attribute.

## Out of scope

No tariff/forecast autodetection; no inferred per-line attribution beyond the
tagged models; no recommendations engine; no changes to givenergy-hass; no
gas control (gas is a read-only cost/consumption line).

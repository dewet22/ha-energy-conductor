# Predbat code quality assessment

*Assessed May 2026 against the GitHub repository at https://github.com/springfall2008/batpred.*

*Purpose: inform the decision of whether to depend on Predbat as a library or pursue clean-room reimplementation of specific algorithms. No source code is reproduced.*

---

## TL;DR

**Depending on Predbat as a library is not viable** — blocked by both licence and architecture. **Clean-room reimplementation of specific algorithms is clearly the better path.** The algorithms are describable from public documentation and readable source; none are patented or secret.

---

## Licence

The licence is custom, proprietary, and restrictive:

- Personal, non-commercial use only, within the United Kingdom
- Redistribution or sublicensing outside the GitHub repository requires prior written permission from the author
- Contributors assign copyright to the licensor
- Can be revoked at any time

This is a hard legal blocker for any dependency relationship, regardless of technical merit. Even for personal use, any project intended to be shared or open-sourced cannot redistribute derived work. The licence rules out Predbat as a dependency entirely.

---

## Code organisation

The codebase is approximately 25,000–30,000 lines across ~50 Python files in `apps/predbat/`. The module decomposition looks reasonable at first glance, but the **architecture is composition via multiple inheritance**, not a library with a clean import boundary.

The top-level `PredBat` class inherits from ten base classes simultaneously — `hass.Hass`, `Octopus`, `Energidataservice`, `Stromligning`, `Fetch`, `Plan`, `Marginal`, `Execute`, `Output`, `UserInterface` — all of which are mixins that freely call methods on `self` defined in sibling mixins. There is no dependency injection, no protocol/interface boundary, and no separation between domain logic and HA/AppDaemon infrastructure.

Extracting any single mixin is extremely difficult: `Plan` calls into `Prediction` which is initialised from `self.base` state; `Fetch` calls HA data methods; `Output` publishes via HA dashboards. The only genuinely separable modules are `prediction.py`, `utils.py`, `load_predictor.py`, and `const.py`.

---

## Code style and quality

**Readability:** Generally reasonable for a project of this age and growth trajectory. Functions have descriptive names; inline comments frequently explain *why* decisions are made, not just what.

**Consistency:** Inconsistent. Black formatting and isort are configured, but with `line-length = 256` — a strong signal that functions are frequently very long and have not been refactored for readability. pylint is configured but suppressed via `# pylint: disable=...` at the top of nearly every file.

**Type annotations:** Essentially absent. No function return type annotations, no use of the `typing` module for parameters, no mypy/pyright configuration. This is significant for library suitability assessment.

**Docstrings:** `interrogate` is configured targeting 100% coverage, but enforcement state is unclear.

---

## Size and complexity

| Module | Approx. lines | Notes |
|---|---|---|
| `plan.py` | ~4,300 | God-object; single methods up to ~450 lines |
| `output.py` | ~3,400 | |
| `inverter.py` | ~3,200 | |
| `octopus.py` | ~3,100 | |
| `fetch.py` | ~2,400 | |
| `config.py` | ~2,200 | |
| `predbat.py` | ~1,800 | Entry point / mixin assembly |
| `load_predictor.py` | ~1,800 | |
| `load_ml_component.py` | ~1,100 | |
| `execute.py` | ~1,100 | |
| `prediction.py` | ~1,300 | Most extractable of the core modules |
| `utils.py` | ~1,200 | |
| `solcast.py` | ~1,300 | |

`plan.py` is the clearest god-object: a single mixin class with methods that individually span hundreds of lines, tightly coupled to internal data structures. The main simulation loop in `prediction.py` is similarly complex but more focused.

---

## Test coverage

A single test file (`unit_test.py`, 467 lines) acts as a custom runner over tests in a `coverage/` directory. The suite:

- Covers a broad functional surface (inverter operations, rate optimisation, API integrations, ML, OAuth, database)
- Is structured as functional/integration tests that construct a `PredBat` instance and drive it through scenarios — not unit tests of individual algorithms
- Has no standard pytest/coverage tooling integration visible in the main source tree
- Published HTML coverage reports are not present in the repository

The integration-test approach makes sense given the architecture, but it means algorithms cannot be tested in isolation.

---

## Dependencies

```
aiofiles, aiohttp, aiomqtt   async HTTP and MQTT
numpy==2.3.5                 numerical computation (pinned)
matplotlib                   chart generation
pvlib                        PV modelling (optional import in solcast.py)
prometheus-client            metrics export
protobuf                     GivEnergy protocol buffer
pyjwt                        JWT for OAuth
pytz                         timezone handling
requests                     synchronous HTTP
ruamel.yaml                  YAML config parsing
```

All are stable and well-maintained. Only numpy is pinned; the absence of other version constraints creates some reproducibility risk. There is also an implicit runtime dependency on Home Assistant / AppDaemon.

---

## Public API / importability

**There is no stable public API. The project is not designed to be imported as a library.**

- No `__init__.py` defining a public interface
- No versioned function signatures
- All meaningful state is carried on `self` of a god-object that inherits from an AppDaemon runtime class
- Even the most extractable modules (`prediction.py`) require populating ~30 state attributes normally populated by the HA runtime fetch layer before a `Prediction` instance is usable

---

## Versioning and stability

689 releases, versioned as a constant string in source (`THIS_VERSION = "v8.39.4"`), not managed through a packaging system. Not published to PyPI.

Internal data structures (`charge_window`, `charge_limit`, `rate_import` dicts) evolve without versioned interfaces. Commit history for `plan.py` shows multiple algorithm changes per month. There is no semantic versioning contract around internal APIs.

---

## Algorithm-level assessment: specific modules

| Algorithm | Source module | Self-contained? | Reimplementation effort |
|---|---|---|---|
| Minute-level battery simulation loop | `prediction.py` | Partially — simulation is clean, initialisation requires HA state | Medium |
| Charge curve extraction from history | `inverter.py` | No — requires HA sensor history | Low to reimplement |
| Solar forecast calibration (weighted scaling) | `solcast.py` | No — interleaved with HA fetching | Low (~80 lines of pure logic) |
| Historical load modal filter | `fetch.py` | No — embedded in HA sensor history data structures | Low |
| Load ML predictor | `load_ml_component.py`, `load_predictor.py` | **Yes** — `load_predictor.py` is numpy-only with no HA imports | Low (but licence still prevents direct use) |
| Price threshold grid search | `plan.py` | No — requires full PredBat state | Medium-high |
| Tariff rate fetching (Octopus API) | `octopus.py` | No — 3,100 lines of HA-coupled fetching | Medium (API is independently documented) |

`load_predictor.py` is the standout exception: a genuinely self-contained numpy-only MLP with AdamW, no HA dependencies. The licence still prohibits direct use, but it is useful as a reference for network architecture and training approach if reimplementing independently.

---

## Conclusion

Two compounding blockers prevent any dependency relationship:

1. **Licence**: Proprietary personal-use UK-only, no redistribution. Hard legal blocker regardless of technical merit.

2. **Architecture**: AppDaemon application with a god-object core and deep HA coupling. No stable public API, no type annotations for interface stability, no packaging. The algorithms of interest are not extractable without significant surgery.

The algorithms themselves — the minute-level battery simulation, calibrated solar forecast weighting, multi-day modal-filtered load averaging, coarse-fine price-threshold grid search — are all describable at pseudocode level from public documentation and readable source. None are patented. Clean-room reimplementation is both legally required and technically preferable: it produces code designed for ha-energy-conductor's architecture from the start, with proper type annotations, tests, and a stable API surface.

The prior art survey and feature inventory in `predbat-feature-inventory.md` capture the full behavioural specification needed to reimplement individual components without referencing the source code further.

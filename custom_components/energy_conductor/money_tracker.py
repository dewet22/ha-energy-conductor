"""HA-glue for the money sensors: reads configured entities, feeds the pure core.

One tracker per coordinator, advanced once per tick before the SiteState build (the
money inputs are independent of the control-loop entities, so a degraded control tick
must not stall pricing). All arithmetic lives in money.py; this module only reads
entity states and holds the accumulators.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN

from .const import (
    CONF_BATTERY_DISCHARGE_ENERGY_SENSOR,
    CONF_DAILY_ENERGY_SENSOR,
    CONF_EV_ENERGY_SENSOR,
    CONF_EV_GREEN_ENERGY_SENSOR,
    CONF_EXPORT_EARNINGS_SENSOR,
    CONF_GAS_RATE_SENSOR,
    CONF_GRID_EXPORT_ENERGY_SENSOR,
    CONF_HOTWATER_ENERGY_SENSOR,
    CONF_HOTWATER_GREEN_SENSOR,
    CONF_IMPORT_COST_SENSOR,
    CONF_IMPORT_RATE_SENSOR,
    CONF_PV_ENERGY_SENSOR,
    CONF_SYSTEM_CAPITAL_COST,
)
from .money import (
    CumulativeSavings,
    DailyCost,
    PaybackProjection,
    accumulate_daily_cost,
    normalise_rate,
    payback_projection,
    roll_cumulative,
    savings_today_gbp,
)

if TYPE_CHECKING:
    from datetime import date

    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Accumulator names (keys into MoneyTracker.daily).
ACC_COUNTERFACTUAL = "counterfactual"
ACC_EV_COST = "ev_cost"
ACC_SELF_USE = "self_use"
ACC_PEAK_SHIFT = "peak_shift"
ACC_HOTWATER_GAS = "hotwater_gas"
ACC_EV_SOLAR = "ev_solar"

_ALL_ACCS = (
    ACC_COUNTERFACTUAL,
    ACC_EV_COST,
    ACC_SELF_USE,
    ACC_PEAK_SHIFT,
    ACC_HOTWATER_GAS,
    ACC_EV_SOLAR,
)


class MoneyTracker:
    """Holds the money accumulators and advances them from entity states each tick."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config
        self.daily: dict[str, DailyCost | None] = {name: None for name in _ALL_ACCS}
        self.cumulative: CumulativeSavings | None = None
        self.rate_available: bool = False
        self._ticked = False

    # ---- feature gating (which sensors exist) ---------------------------------------

    @staticmethod
    def counterfactual_enabled(config: dict[str, Any]) -> bool:
        return bool(config.get(CONF_DAILY_ENERGY_SENSOR) and config.get(CONF_IMPORT_RATE_SENSOR))

    @staticmethod
    def ev_cost_enabled(config: dict[str, Any]) -> bool:
        return bool(config.get(CONF_EV_ENERGY_SENSOR) and config.get(CONF_IMPORT_RATE_SENSOR))

    @staticmethod
    def savings_enabled(config: dict[str, Any]) -> bool:
        return MoneyTracker.counterfactual_enabled(config) and bool(
            config.get(CONF_IMPORT_COST_SENSOR)
        )

    @classmethod
    def configured(cls, config: dict[str, Any]) -> bool:
        return cls.counterfactual_enabled(config) or cls.ev_cost_enabled(config)

    # ---- restore seeding -------------------------------------------------------------

    def seed_daily(self, name: str, restored: DailyCost) -> None:
        """Adopt a restored accumulator, but never clobber live accumulation.

        Restore runs at platform setup, which is *after* the coordinator's first
        refresh — so the live accumulator may already hold a fresh baseline (cost 0).
        In that case the restored running total is adopted and the down-time counter
        gap is priced at the current rate (same approximation as a rate outage). A
        restore from a previous day is ignored: today's accumulator starts at zero.
        """
        current = self.daily.get(name)
        if current is None:
            # No live baseline yet (first tick ran but this accumulator had no inputs).
            # Always adopt the restored snapshot so a post-outage restart doesn't lose today.
            self.daily[name] = restored
            return
        if restored.day != current.day or current.cost_gbp != 0.0:
            return
        rate_key = CONF_GAS_RATE_SENSOR if name == ACC_HOTWATER_GAS else CONF_IMPORT_RATE_SENSOR
        merged = accumulate_daily_cost(
            restored,
            day=current.day,
            counter_kwh=current.last_counter_kwh,
            rate_gbp_per_kwh=self._read_rate(rate_key),
        )
        self.daily[name] = merged if merged is not None else restored

    def seed_cumulative(self, restored: CumulativeSavings) -> None:
        """Adopt the restored lifetime bank (see seed_daily for the ordering caveat)."""
        if self.cumulative is None:
            self.cumulative = restored
            return
        if self.cumulative.base_gbp == 0.0:
            # Fresh boot: keep this boot's today-value, restore the bank and start date
            # (a restore from a previous day banks its final today-value in the roll).
            self.cumulative = roll_cumulative(
                restored, day=self.cumulative.day, savings_today_gbp=self.cumulative.today_gbp
            )

    # ---- entity reads ----------------------------------------------------------------

    def _read_float(self, conf_key: str) -> float | None:
        entity_id = self.config.get(conf_key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, "", None):
            return None
        try:
            value = float(state.state)
        except TypeError, ValueError:
            return None
        return value if math.isfinite(value) else None

    def _read_rate(self, conf_key: str) -> float | None:
        entity_id = self.config.get(conf_key)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, "", None):
            return None
        try:
            value = float(state.state)
        except TypeError, ValueError:
            return None
        if not math.isfinite(value):
            return None
        return normalise_rate(value, state.attributes.get("unit_of_measurement"))

    # ---- the tick --------------------------------------------------------------------

    def tick(self, now: datetime) -> None:
        """Advance every configured accumulator by one tick."""
        self._ticked = True
        day = now.date()
        import_rate = self._read_rate(CONF_IMPORT_RATE_SENSOR)
        gas_rate = self._read_rate(CONF_GAS_RATE_SENSOR)
        self.rate_available = import_rate is not None

        self._advance(
            ACC_COUNTERFACTUAL, day, self._read_float(CONF_DAILY_ENERGY_SENSOR), import_rate
        )
        self._advance(ACC_EV_COST, day, self._read_float(CONF_EV_ENERGY_SENSOR), import_rate)
        self._advance(ACC_EV_SOLAR, day, self._read_float(CONF_EV_GREEN_ENERGY_SENSOR), import_rate)

        # Self-used solar: cumulative PV that did not leave for the grid. Both counters
        # required; clamped at zero (battery export can legitimately outpace PV).
        pv = self._read_float(CONF_PV_ENERGY_SENSOR)
        exported = self._read_float(CONF_GRID_EXPORT_ENERGY_SENSOR)
        self_use = max(pv - exported, 0.0) if pv is not None and exported is not None else None
        self._advance(ACC_SELF_USE, day, self_use, import_rate)

        # Battery peak-shift: discharge priced at the import rate it displaced. The
        # discharge guard idles the battery off-peak, so discharge is peak-displacing
        # in practice; still a model, tagged as such downstream.
        self._advance(
            ACC_PEAK_SHIFT,
            day,
            self._read_float(CONF_BATTERY_DISCHARGE_ENERGY_SENSOR),
            import_rate,
        )

        # Hot-water heating displaces gas: prefer the total-in counter, fall back to
        # the green/diverted one (older configs may only have the core pair).
        hw = self._read_float(CONF_HOTWATER_ENERGY_SENSOR)
        if hw is None:
            hw = self._read_float(CONF_HOTWATER_GREEN_SENSOR)
        self._advance(ACC_HOTWATER_GAS, day, hw, gas_rate)

        savings = self.savings_today
        if savings is not None:
            self.cumulative = roll_cumulative(self.cumulative, day=day, savings_today_gbp=savings)

    def _advance(self, name: str, day: date, counter: float | None, rate: float | None) -> None:
        self.daily[name] = accumulate_daily_cost(
            self.daily[name], day=day, counter_kwh=counter, rate_gbp_per_kwh=rate
        )

    # ---- derived values --------------------------------------------------------------

    def daily_cost(self, name: str) -> float | None:
        state = self.daily.get(name)
        return None if state is None else state.cost_gbp

    @property
    def savings_today(self) -> float | None:
        if not self.rate_available:
            return None
        counterfactual = self.daily_cost(ACC_COUNTERFACTUAL)
        if not self.config.get(CONF_IMPORT_COST_SENSOR):
            return None
        return savings_today_gbp(
            counterfactual_gbp=counterfactual,
            import_cost_gbp=self._read_float(CONF_IMPORT_COST_SENSOR),
            export_earnings_gbp=self._read_float(CONF_EXPORT_EARNINGS_SENSOR),
        )

    def payback(self, today: date) -> PaybackProjection | None:
        if self.cumulative is None:
            return None
        return payback_projection(
            capital_cost_gbp=self.config.get(CONF_SYSTEM_CAPITAL_COST),
            recovered_gbp=self.cumulative.total_gbp,
            started=self.cumulative.started,
            today=today,
        )

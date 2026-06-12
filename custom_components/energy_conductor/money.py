"""Pure-core money arithmetic: tick pricing, daily rollover, payback projection.

Everything here is modelled (energy x rate), not billing-grade; the dashboard tags
the derived numbers accordingly. No homeassistant imports (TID251 core module).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

DAYS_PER_YEAR = 365.25

# A counter dropping below this fraction of its previous value (from a non-trivial
# previous value) is a reset; smaller dips are meter jitter / composite-counter
# wobble and are ignored rather than priced.
_RESET_FRACTION = 0.2
_RESET_MIN_KWH = 0.05


def normalise_rate(value: float | None, unit: str | None) -> float | None:
    """Return ``value`` as GBP/kWh, or None when it cannot be priced safely.

    Pence-denominated units are divided by 100; GBP-denominated pass through; a
    missing unit is assumed GBP/kWh (template sensors often omit it); any other
    unit returns None so a foreign currency can never be priced as sterling.

    The currency token is the part before the "/". "GBP" (pounds) and "GBp"
    (pence) differ only in the case of the final letter, so that one token is
    matched case-sensitively; everything else is case-insensitive.
    """
    if value is None:
        return None
    if unit is None:
        return value
    token = unit.split("/", 1)[0].strip()
    lowered = token.lower()
    if lowered == "gbp":
        return value / 100.0 if token == "GBp" else value
    if token == "£":
        return value
    if lowered in ("p", "pence"):
        return value / 100.0
    return None


@dataclass(frozen=True)
class DailyCost:
    """Running today-cost accumulator over a kWh counter (daily-reset or lifetime)."""

    day: date
    last_counter_kwh: float
    cost_gbp: float


def accumulate_daily_cost(
    state: DailyCost | None,
    *,
    day: date,
    counter_kwh: float | None,
    rate_gbp_per_kwh: float | None,
) -> DailyCost | None:
    """Advance the accumulator by one tick, pricing the counter delta at the rate in force.

    A new ``day`` rebaselines with zero cost (works for both daily-reset and lifetime
    counters). With either input unavailable the state is returned unchanged — the
    baseline is held, so energy that flows during a rate outage is priced at the
    resumed rate rather than dropped or priced at zero. A counter falling to below
    20% of its previous value mid-day is a source reset (priced from zero); smaller
    negative deltas are jitter and contribute nothing.
    """
    if counter_kwh is None:
        return state
    if rate_gbp_per_kwh is None:
        # Hold the baseline; if the calendar day changes during an outage, advance
        # the day (resetting cost to 0) so the overnight delta is priced at the
        # resumed rate rather than dropped when the day-change branch fires next.
        if state is not None and day != state.day:
            return DailyCost(day=day, last_counter_kwh=state.last_counter_kwh, cost_gbp=0.0)
        return state
    if state is None or day != state.day:
        return DailyCost(day=day, last_counter_kwh=counter_kwh, cost_gbp=0.0)
    delta = counter_kwh - state.last_counter_kwh
    if delta < 0:
        is_reset = (
            state.last_counter_kwh > _RESET_MIN_KWH
            and counter_kwh < _RESET_FRACTION * state.last_counter_kwh
        )
        if is_reset:
            delta = counter_kwh
        else:
            # Jitter: ignore and preserve the previous baseline so the rebound on
            # the next tick is not priced as fresh energy.
            return DailyCost(
                day=day, last_counter_kwh=state.last_counter_kwh, cost_gbp=state.cost_gbp
            )
    return DailyCost(
        day=day,
        last_counter_kwh=counter_kwh,
        cost_gbp=state.cost_gbp + delta * rate_gbp_per_kwh,
    )


def savings_today_gbp(
    *,
    counterfactual_gbp: float | None,
    import_cost_gbp: float | None,
    export_earnings_gbp: float | None,
) -> float | None:
    """Modelled savings: counterfactual minus actual net electricity position.

    Requires the counterfactual and the actual import cost; export earnings are
    optional (no export metering simply means no export credit).
    """
    if counterfactual_gbp is None or import_cost_gbp is None:
        return None
    return counterfactual_gbp - import_cost_gbp + (export_earnings_gbp or 0.0)


@dataclass(frozen=True)
class CumulativeSavings:
    """Lifetime savings: banked full days plus the running current day."""

    day: date
    started: date
    base_gbp: float
    today_gbp: float

    @property
    def total_gbp(self) -> float:
        return self.base_gbp + self.today_gbp


def roll_cumulative(
    state: CumulativeSavings | None, *, day: date, savings_today_gbp: float
) -> CumulativeSavings:
    """Update the cumulative accumulator: bank yesterday on a day change."""
    if state is None:
        return CumulativeSavings(day=day, started=day, base_gbp=0.0, today_gbp=savings_today_gbp)
    if day != state.day:
        return CumulativeSavings(
            day=day,
            started=state.started,
            base_gbp=state.base_gbp + state.today_gbp,
            today_gbp=savings_today_gbp,
        )
    return CumulativeSavings(
        day=day, started=state.started, base_gbp=state.base_gbp, today_gbp=savings_today_gbp
    )


@dataclass(frozen=True)
class PaybackProjection:
    recovered_pct: float
    run_rate_gbp_per_year: float
    projected_breakeven: date | None


def payback_projection(
    *,
    capital_cost_gbp: float | None,
    recovered_gbp: float,
    started: date,
    today: date,
) -> PaybackProjection | None:
    """Project break-even from the recovery run-rate since tracking started.

    The run-rate denominates over days *tracked* (not days since install): the
    accumulator only counts from the day it was created, so install-dated maths
    would understate the rate. None when no capital cost is configured.
    """
    if not capital_cost_gbp or capital_cost_gbp <= 0:
        return None
    days_tracked = max((today - started).days + 1, 1)
    per_day = recovered_gbp / days_tracked
    recovered_pct = recovered_gbp / capital_cost_gbp * 100.0
    if recovered_gbp >= capital_cost_gbp:
        breakeven: date | None = today
    elif per_day <= 0:
        breakeven = None
    else:
        breakeven = today + timedelta(days=round((capital_cost_gbp - recovered_gbp) / per_day))
    return PaybackProjection(
        recovered_pct=recovered_pct,
        run_rate_gbp_per_year=per_day * DAYS_PER_YEAR,
        projected_breakeven=breakeven,
    )

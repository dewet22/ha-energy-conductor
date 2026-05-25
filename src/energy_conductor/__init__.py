"""Pure-Python core for the energy_conductor integration.

This package MUST NOT import from `homeassistant`. The architectural
boundary is enforced by ruff (see pyproject.toml's TID rules).
"""

from .decisions import Decision, DecisionKind
from .discharge_guard import discharge_limit
from .fallback import seasonal_fallback_kwh
from .model import (
    Battery,
    EVCharger,
    ForecastSlot,
    SiteState,
    SolarForecast,
    TariffState,
)
from .overnight import plan_overnight

__all__ = [
    "Battery",
    "Decision",
    "DecisionKind",
    "EVCharger",
    "ForecastSlot",
    "SiteState",
    "SolarForecast",
    "TariffState",
    "discharge_limit",
    "plan_overnight",
    "seasonal_fallback_kwh",
]

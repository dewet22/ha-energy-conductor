"""Fill-mode unit-economics check (spec 2026-08-23). Warn-only — never changes regime.

The setpoint regime's premise: grid-filling during cheap windows beats PV-filling while
off_peak_import / eta < export. This module computes the margin; the coordinator owns
the episode latch and notification.
"""

from __future__ import annotations


def fill_margin_gbp(import_rate: float, export_rate: float, *, efficiency: float = 0.9) -> float:
    """GBP/kWh margin of grid-filling: export value minus efficiency-adjusted import cost.

    Positive: fill-mode is profitable. Zero/negative: the strategy premise is broken and
    a human should reconsider (EC only warns).
    """
    return export_rate - import_rate / efficiency

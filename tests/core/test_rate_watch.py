"""Fill-mode unit economics — the arithmetic behind the warn-only rate-watch."""

from __future__ import annotations

import pytest
from custom_components.energy_conductor.rate_watch import fill_margin_gbp


def test_margin_positive_at_current_tariff():
    # 6.9p import, 12p export, eta 0.9 -> +4.33p
    assert fill_margin_gbp(0.069, 0.12) == pytest.approx(0.0433, abs=1e-4)


def test_margin_negative_when_export_collapses():
    assert fill_margin_gbp(0.069, 0.05) < 0


def test_efficiency_divides_import():
    assert fill_margin_gbp(0.09, 0.10, efficiency=1.0) == pytest.approx(0.01)

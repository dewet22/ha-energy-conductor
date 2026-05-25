"""Tests for plan_overnight covering every algorithm branch.

Algorithm recap (spec §4.1):
  morning_gap_hours = clamp(0, hours(cheap_window_end → first_solar), 6)
                      where first_solar = first slot with energy >= 0.25 kWh (≈500W half-hour)
                      defaults to MISSING_FORECAST_GAP_H (4) when no slots
  morning_gap_kwh   = baseline_load_w * morning_gap_hours / 1000
  forecast_kwh      = total_kwh_today (slots) or fallback_kwh
  forecast_deficit  = max(0, daily_kwh_target - forecast_kwh)
  target_kwh        = morning_gap_kwh + forecast_deficit
  target_percent    = clamp(reserve_percent, round(target_kwh / capacity_kwh * 100), 100)
"""

from energy_conductor.decisions import DecisionKind
from energy_conductor.overnight import (
    MEANINGFUL_SLOT_KWH,
    MISSING_FORECAST_GAP_H,
    MORNING_GAP_CAP_H,
    plan_overnight,
)

from .builders import (
    a_battery,
    a_forecast_with_fallback,
    a_forecast_with_slots,
    a_site_state,
    a_tariff,
)
from .conftest import utc

CHARGE_ENTITY = "number.inverter_charge_target_soc"


def _state(**overrides):
    base = dict(
        now=utc(2026, 6, 1, 21, 0),  # 21:00 planning time
        battery=a_battery(soc_percent=20.0, capacity_kwh=10.0),
        tariff=a_tariff(
            cheap_window_end=utc(2026, 6, 2, 5, 30),  # Intelligent Go: 23:30-05:30
        ),
        baseline_load_w=400.0,
    )
    base.update(overrides)
    return a_site_state(**base)


class TestOvernightAlgorithm:
    def test_emits_set_charge_target_decision(self):
        state = _state()
        decision = plan_overnight(state, target_entity=CHARGE_ENTITY, daily_kwh_target=10.0)
        assert decision.kind == DecisionKind.SET_CHARGE_TARGET
        assert decision.target_entity == CHARGE_ENTITY

    def test_small_gap_with_sufficient_forecast(self):
        # gap 4h * 400W = 1.6 kWh; forecast 10 kWh covers 10 kWh target → deficit 0
        # target_kwh = 1.6 → 16% of 10 kWh
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 9, 30),  # 4h after cheap end at 05:30
            slot_count=20,
            kwh_per_slot=0.5,  # 10 kWh total
        )
        state = _state(solar_forecast=forecast)
        decision = plan_overnight(state, target_entity=CHARGE_ENTITY, daily_kwh_target=10.0)
        assert decision.value == 16

    def test_deficit_when_forecast_below_target(self):
        # gap 4h * 400W = 1.6 kWh; forecast 4 kWh, daily_kwh_target 10 → deficit 6
        # target_kwh = 1.6 + 6 = 7.6 → 76% of 10 kWh
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 9, 30),
            slot_count=8,
            kwh_per_slot=0.5,  # 4 kWh total
        )
        state = _state(solar_forecast=forecast)
        decision = plan_overnight(state, target_entity=CHARGE_ENTITY, daily_kwh_target=10.0)
        assert decision.value == 76

    def test_morning_gap_capped(self):
        # Solar doesn't arrive until 16:00 (10.5h after cheap end) — should clamp to 6h
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 16, 0),
            slot_count=4,
            kwh_per_slot=0.5,
        )
        state = _state(solar_forecast=forecast)
        decision = plan_overnight(state, target_entity=CHARGE_ENTITY, daily_kwh_target=2.0)
        # gap = 6h * 400W = 2.4 kWh; forecast 2.0 covers daily_kwh_target → deficit 0
        # target = 2.4 → 24%
        assert decision.value == 24

    def test_missing_forecast_uses_default_gap_and_fallback(self):
        state = _state(solar_forecast=a_forecast_with_fallback(kwh=3.0))
        decision = plan_overnight(state, target_entity=CHARGE_ENTITY, daily_kwh_target=10.0)
        # missing forecast → gap = MISSING_FORECAST_GAP_H (4) * 400 = 1.6 kWh
        # deficit = 10 - 3 = 7 → target 8.6 kWh → 86%
        assert decision.value == 86
        assert "fallback" in decision.reason.lower()

    def test_clamped_below_reserve(self):
        # tiny gap, surplus forecast → raw target below reserve
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 5, 31),  # 1 minute after cheap end
            slot_count=20,
            kwh_per_slot=2.0,  # massive surplus
        )
        state = _state(
            solar_forecast=forecast,
            battery=a_battery(reserve_percent=10.0, capacity_kwh=10.0),
        )
        decision = plan_overnight(state, target_entity=CHARGE_ENTITY, daily_kwh_target=10.0)
        assert decision.value == 10  # clamped to reserve

    def test_clamped_at_100(self):
        # huge deficit → target above 100%
        state = _state(
            solar_forecast=a_forecast_with_fallback(kwh=0.0),
            battery=a_battery(capacity_kwh=1.0),  # tiny battery
        )
        decision = plan_overnight(state, target_entity=CHARGE_ENTITY, daily_kwh_target=100.0)
        assert decision.value == 100

    def test_dedupe_key_includes_date_and_target(self):
        state = _state()
        decision = plan_overnight(state, target_entity=CHARGE_ENTITY, daily_kwh_target=10.0)
        assert "2026-06-01" in decision.dedupe_key
        assert str(decision.value) in decision.dedupe_key

    def test_reason_includes_morning_gap_and_forecast(self):
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 9, 30),
            slot_count=20,
            kwh_per_slot=0.5,
        )
        state = _state(solar_forecast=forecast)
        decision = plan_overnight(state, target_entity=CHARGE_ENTITY, daily_kwh_target=10.0)
        assert "Morning gap" in decision.reason
        assert "10.0" in decision.reason or "10 kWh" in decision.reason


class TestNamedConstants:
    def test_meaningful_slot_kwh_is_reasonable(self):
        # 500W average over a 30min slot = 0.25 kWh
        assert MEANINGFUL_SLOT_KWH == 0.25

    def test_morning_gap_cap_is_6h(self):
        assert MORNING_GAP_CAP_H == 6

    def test_missing_forecast_gap_default_is_4h(self):
        assert MISSING_FORECAST_GAP_H == 4

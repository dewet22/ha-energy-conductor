"""Tests for plan_overnight covering every algorithm branch.

Algorithm recap (reserve-aware):
  morning_gap_hours = clamp(0, hours(off_peak_window_end → first_solar), 6)
                      where first_solar = first slot with energy >= 0.25 kWh (≈500W half-hour)
                      defaults to MISSING_FORECAST_GAP_H (4) when no slots
  morning_gap_kwh   = baseline_load_w * morning_gap_hours / 1000
  forecast_kwh      = total_kwh_forecast (slots) or fallback_kwh
  forecast_deficit  = max(0, daily_kwh_target - forecast_kwh)
  usable_kwh        = morning_gap_kwh + forecast_deficit  (energy ABOVE the reserve floor)
  usable_percent    = usable_kwh / capacity_kwh * 100
  target_percent    = round(min(100, max(min_target, reserve, reserve + usable_percent)))

The +reserve term is the fix: the gap energy must sit ABOVE the dead reserve band,
so a 1.6 kWh gap on a 10 kWh battery with a 10% reserve targets 10 + 16 = 26%, not 16%.
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
        battery=a_battery(soc_percent=20.0, capacity_kwh=10.0, reserve_percent=10.0),
        tariff=a_tariff(
            off_peak_window_end=utc(2026, 6, 2, 5, 30),  # Intelligent Go: 23:30-05:30
        ),
        baseline_load_w=400.0,
    )
    base.update(overrides)
    return a_site_state(**base)


def _plan(state, *, daily_kwh_target=10.0, min_target=0.0):
    # Default min_target=0 so most tests isolate the reserve-aware gap math; the
    # min-target clamp gets its own dedicated tests.
    return plan_overnight(
        state,
        target_entity=CHARGE_ENTITY,
        daily_kwh_target=daily_kwh_target,
        min_target_soc_percent=min_target,
    )


class TestOvernightAlgorithm:
    def test_emits_set_charge_target_decision(self):
        decision = _plan(_state())
        assert decision.kind == DecisionKind.SET_CHARGE_TARGET
        assert decision.target_entity == CHARGE_ENTITY

    def test_small_gap_with_sufficient_forecast(self):
        # gap 4h * 400W = 1.6 kWh; forecast 10 kWh covers 10 kWh target → deficit 0
        # usable 1.6 kWh = 16% of 10 kWh; target = reserve 10 + 16 = 26%
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 9, 30),  # 4h after off-peak end at 05:30
            slot_count=20,
            kwh_per_slot=0.5,  # 10 kWh total
        )
        decision = _plan(_state(solar_forecast=forecast))
        assert decision.value == 26

    def test_deficit_when_forecast_below_target(self):
        # gap 4h * 400W = 1.6 kWh; forecast 4 kWh, target 10 → deficit 6
        # usable 7.6 kWh = 76%; target = reserve 10 + 76 = 86%
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 9, 30),
            slot_count=8,
            kwh_per_slot=0.5,  # 4 kWh total
        )
        decision = _plan(_state(solar_forecast=forecast))
        assert decision.value == 86

    def test_morning_gap_capped(self):
        # Solar doesn't arrive until 16:00 (10.5h after off-peak end) — clamp to 6h
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 16, 0),
            slot_count=4,
            kwh_per_slot=0.5,
        )
        decision = _plan(_state(solar_forecast=forecast), daily_kwh_target=2.0)
        # gap = 6h * 400W = 2.4 kWh; forecast 2.0 covers target → deficit 0
        # usable 2.4 kWh = 24%; target = reserve 10 + 24 = 34%
        assert decision.value == 34

    def test_missing_forecast_uses_default_gap_and_fallback(self):
        state = _state(solar_forecast=a_forecast_with_fallback(kwh=3.0))
        decision = _plan(state)
        # missing forecast → gap = MISSING_FORECAST_GAP_H (4) * 400 = 1.6 kWh
        # deficit = 10 - 3 = 7 → usable 8.6 kWh = 86%; target = 10 + 86 = 96%
        assert decision.value == 96
        assert "fallback" in decision.reason.lower()

    def test_surplus_forecast_clamps_to_reserve(self):
        # tiny gap, surplus forecast → usable ≈ 0 → target floors at reserve
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 5, 31),  # 1 minute after off-peak end
            slot_count=20,
            kwh_per_slot=2.0,  # massive surplus
        )
        state = _state(
            solar_forecast=forecast,
            battery=a_battery(reserve_percent=10.0, capacity_kwh=10.0),
        )
        decision = _plan(state)
        assert decision.value == 10  # gap ≈ 0 → just the reserve floor

    def test_min_target_clamp_raises_low_target(self):
        # Same near-zero-gap surplus case, but min_target 15% lifts the floor above reserve
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 5, 31),
            slot_count=20,
            kwh_per_slot=2.0,
        )
        state = _state(
            solar_forecast=forecast,
            battery=a_battery(reserve_percent=10.0, capacity_kwh=10.0),
        )
        decision = _plan(state, min_target=15.0)
        assert decision.value == 15  # clamped up to the min target

    def test_bms_reserve_above_min_target_warns(self):
        # Live BMS reserve (12%) exceeds the user's min target (10%): the inverter
        # won't supply down to where we'd plan. Surface a warning; still clamp up.
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 5, 31),
            slot_count=20,
            kwh_per_slot=2.0,
        )
        state = _state(
            solar_forecast=forecast,
            battery=a_battery(reserve_percent=12.0, capacity_kwh=10.0),
        )
        decision = _plan(state, min_target=10.0)
        assert decision.value == 12  # clamped up to BMS reserve
        assert "WARNING" in decision.reason
        assert "BMS reserve" in decision.reason

    def test_clamped_at_100(self):
        # huge deficit → target above 100%
        state = _state(
            solar_forecast=a_forecast_with_fallback(kwh=0.0),
            battery=a_battery(capacity_kwh=1.0, reserve_percent=10.0),  # tiny battery
        )
        decision = _plan(state, daily_kwh_target=100.0)
        assert decision.value == 100

    def test_dedupe_key_includes_date_and_target(self):
        decision = _plan(_state())
        assert "2026-06-01" in decision.dedupe_key
        assert str(decision.value) in decision.dedupe_key

    def test_reason_includes_morning_gap_and_forecast(self):
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 9, 30),
            slot_count=20,
            kwh_per_slot=0.5,
        )
        decision = _plan(_state(solar_forecast=forecast))
        assert "Morning gap" in decision.reason
        assert "reserve" in decision.reason.lower()


class TestDawnProjectionNote:
    def test_note_present_when_battery_on_track(self):
        # 86% SoC, 17.7 kWh capacity, 709W baseline, 8.5h until dawn
        # discharge ≈ 34% → expected ~52% > target → note appears
        state = _state(
            battery=a_battery(soc_percent=86.0, capacity_kwh=17.7, reserve_percent=4.0),
            baseline_load_w=709.0,
            solar_forecast=a_forecast_with_slots(
                first_slot_at=utc(2026, 6, 2, 9, 30),
                slot_count=20,
                kwh_per_slot=0.5,
            ),
        )
        decision = _plan(state, daily_kwh_target=10.0, min_target=10.0)
        assert "no charge needed" in decision.reason
        assert "at dawn" in decision.reason

    def test_note_absent_when_battery_will_fall_below_target(self):
        # 25% SoC, 10 kWh capacity, 400W baseline, 8.5h until dawn
        # discharge ≈ 34% → expected ≈ -9% < target → no note
        state = _state(
            battery=a_battery(soc_percent=25.0, capacity_kwh=10.0, reserve_percent=10.0),
        )
        decision = _plan(state)
        assert "no charge needed" not in decision.reason

    def test_note_absent_when_off_peak_end_unknown(self):
        state = _state(
            battery=a_battery(soc_percent=90.0, capacity_kwh=10.0, reserve_percent=10.0),
            tariff=a_tariff(off_peak_window_end=None),
        )
        decision = _plan(state)
        assert "no charge needed" not in decision.reason


class TestNamedConstants:
    def test_meaningful_slot_kwh_is_reasonable(self):
        # 500W average over a 30min slot = 0.25 kWh
        assert MEANINGFUL_SLOT_KWH == 0.25

    def test_morning_gap_cap_is_6h(self):
        assert MORNING_GAP_CAP_H == 6

    def test_missing_forecast_gap_default_is_4h(self):
        assert MISSING_FORECAST_GAP_H == 4

"""Tests for plan_overnight covering every algorithm branch.

Algorithm recap (reserve-aware):
  morning_gap_hours = clamp(0, hours(overnight_boundary → first_solar), 6)
                      where overnight_boundary = configured overnight_window_end, falling
                      back to the sensor-derived off_peak_window_end, and first_solar =
                      first slot with energy >= 0.25 kWh (≈500W half-hour)
                      defaults to MISSING_FORECAST_GAP_H (4) when no slots
  morning_gap_kwh   = baseline_load_w * morning_gap_hours / 1000
  forecast_kwh      = total_kwh_forecast (slots) or fallback_kwh
  forecast_deficit  = max(0, daily_kwh_target - forecast_kwh)
  usable_kwh        = max(morning_gap_kwh + forecast_deficit, MIN_OVERNIGHT_USABLE_KWH)
  usable_percent    = usable_kwh / capacity_kwh * 100
  target_percent    = round(min(100, reserve + usable_percent))

The +reserve term is the fix: the gap energy must sit ABOVE the dead reserve band,
so a 1.6 kWh gap on a 10 kWh battery with a 10% reserve targets 10 + 16 = 26%, not 16%.
MIN_OVERNIGHT_USABLE_KWH is a baked-in safety floor that only binds on the sunniest,
smallest-gap nights (replaces the old user-facing min_target_soc_percent knob).
"""

from datetime import timedelta

import pytest

from energy_conductor.const import MIN_OVERNIGHT_USABLE_KWH
from energy_conductor.decisions import DecisionKind
from energy_conductor.model import ForecastSlot, SolarForecast
from energy_conductor.overnight import (
    MEANINGFUL_SLOT_KWH,
    MISSING_FORECAST_GAP_H,
    MORNING_GAP_CAP_H,
    plan_overnight,
    project_soc,
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
            overnight_window_end=utc(2026, 6, 2, 5, 30),  # configured 05:30
        ),
        baseline_load_w=400.0,
    )
    base.update(overrides)
    return a_site_state(**base)


def _plan(state, *, daily_kwh_target=10.0):
    return plan_overnight(
        state,
        target_entity=CHARGE_ENTITY,
        daily_kwh_target=daily_kwh_target,
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

    def test_morning_gap_measured_from_configured_dawn_not_dispatch_end(self):
        # Codex review case: the sensor's next period is a dispatch slot ending
        # 22:30, so off_peak_window_end pairs hours before the real dawn. The gap
        # must measure from the configured 05:30 to first solar at 07:00 (1.5h),
        # not from 22:30 (8.5h → capped 6h) — that difference moves the WRITTEN
        # target, not just the advisory note.
        forecast = a_forecast_with_slots(
            first_slot_at=utc(2026, 6, 2, 7, 0),
            slot_count=8,
            kwh_per_slot=2.0,  # surplus → deficit 0, gap is the only driver
        )
        state = _state(
            solar_forecast=forecast,
            tariff=a_tariff(
                off_peak_window_end=utc(2026, 6, 1, 22, 30),  # dispatch slot end
                next_off_peak_window_start=utc(2026, 6, 1, 22, 0),
                overnight_window_end=utc(2026, 6, 2, 5, 30),
            ),
        )
        decision = _plan(state)
        assert "Morning gap 1.5h" in decision.reason
        # gap 1.5h * 400W = 0.6 kWh < 1.5 kWh floor → usable 15% → target 10 + 15
        assert decision.value == 25

    def test_missing_forecast_uses_default_gap_and_fallback(self):
        state = _state(solar_forecast=a_forecast_with_fallback(kwh=3.0))
        decision = _plan(state)
        # missing forecast → gap = MISSING_FORECAST_GAP_H (4) * 400 = 1.6 kWh
        # deficit = 10 - 3 = 7 → usable 8.6 kWh = 86%; target = 10 + 86 = 96%
        assert decision.value == 96
        assert "fallback" in decision.reason.lower()

    def test_tiny_gap_floors_at_baked_in_usable_margin(self):
        # Tiny gap + surplus forecast → morning need ≈ 0, but the baked-in safety
        # margin floors usable energy. On a 10 kWh battery, 1.5 kWh = 15% usable,
        # so target = reserve 10 + 15 = 25 (not just the bare reserve).
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
        expected = round(10 + MIN_OVERNIGHT_USABLE_KWH / 10.0 * 100)  # reserve + floor%
        assert decision.value == expected == 25

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
        decision = _plan(state, daily_kwh_target=10.0)
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

    def test_discharge_stops_at_off_peak_start(self):
        # The discharge guard idles the battery once off-peak begins, so only the
        # 2.5h from 21:00 until the 23:30 start drains it — not the 8.5h to dawn.
        # The hold is credited because the sensor period (ending 05:30) provably
        # reaches the configured dawn. 90% - (711W * 2.5h / 17.7 kWh / 10) ≈ 80%.
        state = _state(
            battery=a_battery(soc_percent=90.0, capacity_kwh=17.7, reserve_percent=4.0),
            baseline_load_w=711.0,
            tariff=a_tariff(
                off_peak_window_end=utc(2026, 6, 2, 5, 30),
                next_off_peak_window_start=utc(2026, 6, 1, 23, 30),
                overnight_window_end=utc(2026, 6, 2, 5, 30),
            ),
        )
        decision = _plan(state)
        assert "~80% at dawn" in decision.reason

    def test_no_discharge_when_already_off_peak(self):
        # Plan running inside the overnight window (period end == configured dawn):
        # battery is already idled, so the dawn projection is the current SoC.
        state = _state(
            now=utc(2026, 6, 2, 0, 30),
            battery=a_battery(soc_percent=85.0, capacity_kwh=17.7, reserve_percent=4.0),
            baseline_load_w=711.0,
            tariff=a_tariff(
                off_peak_now=True,
                off_peak_window_end=utc(2026, 6, 2, 5, 30),
                overnight_window_end=utc(2026, 6, 2, 5, 30),
            ),
        )
        decision = _plan(state)
        assert "~85% at dawn" in decision.reason

    def test_active_dispatch_slot_projects_full_drain(self):
        # Plan fires while a 30-min Intelligent dispatch is active: off_peak_now is
        # True but the period end (21:30) falls hours short of the configured dawn,
        # so the hold is NOT credited — drain is projected all the way to 05:30.
        # The real battery holds during the slot and the overnight window, so the
        # claim under-states dawn SoC: 90 - (711W * 8.5h / 17.7 / 10) ≈ 56%.
        state = _state(
            battery=a_battery(soc_percent=90.0, capacity_kwh=17.7, reserve_percent=4.0),
            baseline_load_w=711.0,
            tariff=a_tariff(
                off_peak_now=True,
                off_peak_window_end=utc(2026, 6, 1, 21, 30),  # slot end, 0.5h away
                overnight_window_end=utc(2026, 6, 2, 5, 30),
            ),
        )
        decision = _plan(state)
        assert "~56% at dawn" in decision.reason

    def test_next_short_slot_does_not_earn_the_hold(self):
        # The sensor's next period is a pre-window dispatch slot (22:30-23:00):
        # its end falls short of the configured dawn, so no hold is credited and
        # the projection drains to 05:30 — conservative, since the real battery
        # holds during the slot and the window. NOT the hold-credited ~84%.
        state = _state(
            battery=a_battery(soc_percent=90.0, capacity_kwh=17.7, reserve_percent=4.0),
            baseline_load_w=711.0,
            tariff=a_tariff(
                off_peak_window_end=utc(2026, 6, 1, 23, 0),
                next_off_peak_window_start=utc(2026, 6, 1, 22, 30),
                overnight_window_end=utc(2026, 6, 2, 5, 30),
            ),
        )
        decision = _plan(state)
        assert "~56% at dawn" in decision.reason

    def test_early_plan_with_future_dispatch_slot_stays_conservative(self):
        # Codex review case: a configurable 18:00 plan time with a 21:30-22:00
        # dispatch as the sensor's next period. The slot start is hours away but
        # its end (22:00) is not dawn, so the hold must not be credited — the
        # projection drains the full 11.5h to the configured 05:30:
        # 90 - (711W * 11.5h / 17.7 / 10) ≈ 44%, not the hold-credited ~76%.
        state = _state(
            now=utc(2026, 6, 1, 18, 0),
            battery=a_battery(soc_percent=90.0, capacity_kwh=17.7, reserve_percent=4.0),
            baseline_load_w=711.0,
            solar_forecast=a_forecast_with_slots(
                first_slot_at=utc(2026, 6, 2, 9, 30),
                slot_count=20,
                kwh_per_slot=0.5,
            ),
            tariff=a_tariff(
                off_peak_window_end=utc(2026, 6, 1, 22, 0),
                next_off_peak_window_start=utc(2026, 6, 1, 21, 30),
                overnight_window_end=utc(2026, 6, 2, 5, 30),
            ),
        )
        decision = _plan(state)
        assert "~44% at dawn" in decision.reason
        assert "~76%" not in decision.reason

    def test_stale_off_peak_start_falls_back_to_full_drain(self):
        # A next-start in the past (stale sensor attribute) must not zero the
        # projected drain — that would overstate dawn SoC. Fall back to the
        # conservative full-drain assumption instead (PR #18 review).
        state = _state(
            battery=a_battery(soc_percent=86.0, capacity_kwh=17.7, reserve_percent=4.0),
            baseline_load_w=709.0,
            tariff=a_tariff(
                off_peak_window_end=utc(2026, 6, 2, 5, 30),
                next_off_peak_window_start=utc(2026, 6, 1, 20, 0),  # before now=21:00
                overnight_window_end=utc(2026, 6, 2, 5, 30),
            ),
        )
        decision = _plan(state)
        assert "~52% at dawn" in decision.reason

    def test_full_drain_assumed_when_off_peak_start_unknown(self):
        # Without a known off-peak start, fall back to draining all the way to
        # dawn — the conservative direction (suppresses the note more often).
        # 86% - (709W * 8.5h / 17.7 kWh / 10) ≈ 86 - 34 = 52%.
        state = _state(
            battery=a_battery(soc_percent=86.0, capacity_kwh=17.7, reserve_percent=4.0),
            baseline_load_w=709.0,
            tariff=a_tariff(
                off_peak_window_end=utc(2026, 6, 2, 5, 30),
                overnight_window_end=utc(2026, 6, 2, 5, 30),
            ),
        )
        decision = _plan(state)
        assert "~52% at dawn" in decision.reason

    def test_note_absent_when_overnight_end_unknown(self):
        # No configured overnight end → no trustworthy dawn → no projection,
        # even when the sensor offers a period end.
        state = _state(
            battery=a_battery(soc_percent=90.0, capacity_kwh=17.7, reserve_percent=4.0),
            tariff=a_tariff(off_peak_window_end=utc(2026, 6, 2, 5, 30)),
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


class TestProjectSoc:
    def test_forecast_uses_fixed_thirty_minute_slot_duration(self):
        # If two forecast slots are separated by a 90-min gap (non-contiguous),
        # each slot still represents 30 min of generation, not the gap to the next.
        # Slot 0 at now: 2 kWh in 30 min = 4 kW. Baseline 400 W. Net = -3.6 kW.
        # SoC rise: 3.6 kW * 0.5h / 10 kWh * 100 = 18%.
        now = utc(2026, 6, 2, 10, 0)
        slots = [
            ForecastSlot(start=now, energy_kwh=2.0),
            ForecastSlot(start=now + timedelta(minutes=90), energy_kwh=1.0),
        ]
        forecast = SolarForecast(slots=slots, fallback_kwh=None, fallback_source=None)
        state = a_site_state(
            now=now,
            battery=a_battery(soc_percent=50.0, capacity_kwh=10.0, reserve_percent=10.0),
            baseline_load_w=400.0,
            solar_forecast=forecast,
            tariff=a_tariff(),
        )
        points = project_soc(state, target_percent=80, hours=0.5, step_minutes=30)
        # SoC should rise from 50% to 68% (18% absorbed from 3.6 kW net over 0.5h).
        # Buggy code uses 90-min gap → kW = 2/1.5 ≈ 1.33 → net ≈ -0.93 → rises only ~4.7%.
        assert len(points) == 2
        assert points[1][1] == pytest.approx(68.0, abs=0.1)

    def test_daytime_projection_uses_today_forecast_not_just_tomorrow(self):
        # The planner forecast (solar_forecast) carries only TOMORROW's slots, but
        # the projection runs over TODAY's daylight. Without a today-aware forecast
        # the midday sun is invisible and a full battery bleeds down at baseline.
        # projection_forecast carries today's slots so a sunny midday holds at 100%.
        now = utc(2026, 6, 24, 12, 0)
        tomorrow_only = SolarForecast(
            slots=[ForecastSlot(start=now + timedelta(days=1), energy_kwh=2.0)],
            fallback_kwh=None,
            fallback_source=None,
        )
        today = SolarForecast(
            slots=[
                ForecastSlot(start=now + timedelta(minutes=30 * i), energy_kwh=2.0)
                for i in range(4)
            ],
            fallback_kwh=None,
            fallback_source=None,
        )
        state = a_site_state(
            now=now,
            battery=a_battery(soc_percent=100.0, capacity_kwh=10.0, reserve_percent=10.0),
            baseline_load_w=500.0,
            solar_forecast=tomorrow_only,
            projection_forecast=today,
            tariff=a_tariff(),  # midday, not off-peak
        )
        points = project_soc(state, target_percent=80, hours=2, step_minutes=30)
        # 4 kW PV - 0.5 kW load = +3.5 kW net; already full, so it holds at 100%.
        assert all(p[1] == pytest.approx(100.0) for p in points)

    def test_projection_falls_back_to_planner_forecast_without_a_today_forecast(self):
        # No projection_forecast (today sensor unconfigured): behaviour is unchanged
        # - the projection reads the planner's solar_forecast slots as before.
        now = utc(2026, 6, 24, 12, 0)
        planner = SolarForecast(
            slots=[ForecastSlot(start=now, energy_kwh=2.0)],
            fallback_kwh=None,
            fallback_source=None,
        )
        state = a_site_state(
            now=now,
            battery=a_battery(soc_percent=50.0, capacity_kwh=10.0, reserve_percent=10.0),
            baseline_load_w=400.0,
            solar_forecast=planner,
            tariff=a_tariff(),
        )
        points = project_soc(state, target_percent=80, hours=0.5, step_minutes=30)
        # 4 kW - 0.4 kW = 3.6 kW net over 0.5h on a 10 kWh pack -> +18%.
        assert points[1][1] == pytest.approx(68.0, abs=0.1)

    def test_projection_charges_through_configured_dawn_not_dispatch_end(self):
        # Regression: off_peak_window_end can be a short Intelligent dispatch slot
        # (e.g. ending 22:30), while the overnight plan targets overnight_window_end
        # (05:30). The projection must charge through 05:30, not stop at 22:30.
        dispatch_end = utc(2026, 6, 1, 22, 30)  # brief dispatch slot
        overnight_end = utc(2026, 6, 2, 5, 30)  # configured overnight boundary
        state = a_site_state(
            now=utc(2026, 6, 1, 22, 0),
            battery=a_battery(soc_percent=50.0, capacity_kwh=10.0, reserve_percent=10.0),
            tariff=a_tariff(
                off_peak_now=True,
                off_peak_window_end=dispatch_end,
                overnight_window_end=overnight_end,
            ),
        )
        points = project_soc(state, target_percent=90.0, hours=2, step_minutes=30)
        # At t+30min (22:30, dispatch ends), projection should still be charging.
        # With the bug, it stops charging at dispatch_end and starts discharging.
        soc_at_22_00 = points[0][1]
        soc_at_22_30 = points[1][1]
        soc_at_23_00 = points[2][1]
        assert soc_at_22_30 >= soc_at_22_00, "should still charge past the dispatch end"
        assert soc_at_23_00 >= soc_at_22_30, "should still charge an hour past the dispatch end"

"""Tests for the pure-core hot-water reserve estimate.

Recap:
  reserve = clamp(capacity - depletion*elapsed_days + energy_since_full, 0, capacity)
  depletion learned from steady (full→full) days' delivered energy, else configured fallback
  boost recommended when reserve projected one day forward (- depletion + expected refill)
  falls below the comfort threshold; suggested hours = ceil(deficit/heater_kw), clamped 1..2
"""

from datetime import UTC, datetime

from energy_conductor.hotwater import (
    boost_recommendation,
    corroborated_full_events,
    estimate_reserve,
    estimate_reserve_cold_fill,
    learn_depletion,
    transition_gated_full_events,
)


def _hour(h: int, *, day: int = 13) -> datetime:
    return datetime(2026, 6, day, h, 0, 0, tzinfo=UTC)


def _corroborate(events, diversion):
    return corroborated_full_events(events, diversion, window_hours=2, min_kwh=0.05)


class TestCorroboratedFullEvents:
    """A 'Max temp reached' anchors the reserve only when real diversion flowed into
    the tank in the hours leading up to it — the isolated-element / reconnect-republished
    false events have no adjacent diversion and must be ignored."""

    def test_genuine_full_with_adjacent_diversion_is_kept(self):
        event = datetime(2026, 6, 13, 5, 52, tzinfo=UTC)  # real top-up, 0.16 kWh in its hour
        assert _corroborate([event], {_hour(5): 0.16}) == [event]

    def test_false_full_without_diversion_is_dropped(self):
        event = datetime(
            2026, 6, 15, 0, 10, tzinfo=UTC
        )  # isolated element / boot republish, zero diversion
        assert _corroborate([event], {}) == []

    def test_later_false_full_does_not_displace_earlier_genuine(self):
        genuine = datetime(2026, 6, 13, 5, 52, tzinfo=UTC)
        false_later = datetime(2026, 6, 15, 0, 10, tzinfo=UTC)
        # Only the genuine event has adjacent diversion; the anchor (max of the result)
        # must therefore be the earlier genuine event, not the later false one.
        result = _corroborate([genuine, false_later], {_hour(5): 0.16})
        assert result == [genuine]
        assert max(result) == genuine

    def test_keeps_all_corroborated_for_learning(self):
        early = datetime(2026, 6, 12, 14, 30, tzinfo=UTC)
        late = datetime(2026, 6, 13, 5, 52, tzinfo=UTC)
        result = _corroborate([early, late], {_hour(14, day=12): 1.0, _hour(5): 0.16})
        assert result == [early, late]
        assert max(result) == late

    def test_diversion_in_preceding_hour_still_corroborates(self):
        event = datetime(
            2026, 6, 13, 6, 3, tzinfo=UTC
        )  # event in hour 06; diversion was in hour 05
        assert _corroborate([event], {_hour(5): 0.20}) == [event]

    def test_diversion_below_threshold_is_dropped(self):
        event = datetime(2026, 6, 13, 5, 52, tzinfo=UTC)
        assert _corroborate([event], {_hour(5): 0.02}) == []  # below 0.05 — noise

    def test_no_events_returns_empty(self):
        assert _corroborate([], {}) == []


def _gate(events_with_prior):
    return transition_gated_full_events(events_with_prior, active_states=("Diverting", "Boosting"))


class TestTransitionGatedFullEvents:
    """A 'Max temp reached' anchors the reserve only when it follows active heating
    (Diverting/Boosting): a real full trips to max temp while power is flowing. A trip from
    an idle origin — Stopped, a reconnect 'unavailable' republish, or a supply-dip 'Paused' —
    on a cold/isolated tank is a phantom and must be dropped."""

    def test_full_from_diverting_is_kept(self):
        ts = _hour(10)
        assert _gate([(ts, "Diverting")]) == [ts]

    def test_full_from_boosting_is_kept(self):
        ts = _hour(10)
        assert _gate([(ts, "Boosting")]) == [ts]

    def test_full_from_stopped_is_dropped(self):
        # The live 2026-06-23 blip: Stopped → Max temp on a cold tank.
        assert _gate([(_hour(8), "Stopped")]) == []

    def test_full_from_unavailable_is_dropped(self):
        # Reconnect/restart status republish.
        assert _gate([(_hour(0), "unavailable")]) == []

    def test_full_from_paused_is_dropped(self):
        # The 06-13 phantom: trip during a supply-dip pause; only 0.16 kWh delivered all day.
        assert _gate([(_hour(5), "Paused")]) == []

    def test_full_with_unknown_prior_is_dropped(self):
        assert _gate([(_hour(5), None)]) == []

    def test_keeps_only_active_prior_in_order(self):
        genuine_a, phantom, genuine_b = _hour(10), _hour(11), _hour(12)
        events = [(genuine_a, "Diverting"), (phantom, "Stopped"), (genuine_b, "Boosting")]
        assert _gate(events) == [genuine_a, genuine_b]

    def test_no_events_returns_empty(self):
        assert _gate([]) == []


class TestEstimateReserveColdFill:
    """No trusted anchor: integrate measured delivered energy up from an assumed-empty tank,
    capped at capacity — the gradual rise while a cold tank fills."""

    def test_integrates_green_up_from_zero(self):
        assert estimate_reserve_cold_fill(3.0, 12.0) == 3.0

    def test_zero_green_is_zero(self):
        assert estimate_reserve_cold_fill(0.0, 12.0) == 0.0

    def test_caps_at_capacity(self):
        assert estimate_reserve_cold_fill(20.0, 12.0) == 12.0

    def test_negative_clamps_to_zero(self):
        assert estimate_reserve_cold_fill(-1.0, 12.0) == 0.0


CAPACITY = 11.0
THRESHOLD = 20.0
HEATER_KW = 2.7


class TestEstimateReserve:
    def test_recent_full_is_capacity(self):
        # Just reached max temp: full regardless of energy-in (clamped).
        assert (
            estimate_reserve(
                elapsed_hours_since_full=0.0,
                energy_in_since_full_kwh=0.0,
                depletion_kwh_per_day=3.0,
                capacity_kwh=CAPACITY,
            )
            == CAPACITY
        )

    def test_energy_in_cannot_exceed_capacity(self):
        assert (
            estimate_reserve(
                elapsed_hours_since_full=0.0,
                energy_in_since_full_kwh=5.0,
                depletion_kwh_per_day=3.0,
                capacity_kwh=CAPACITY,
            )
            == CAPACITY
        )

    def test_depletes_over_time(self):
        # 2 days since full, little diversion: 11 - 3*2 + 1 = 6.
        reserve = estimate_reserve(
            elapsed_hours_since_full=48.0,
            energy_in_since_full_kwh=1.0,
            depletion_kwh_per_day=3.0,
            capacity_kwh=CAPACITY,
        )
        assert reserve == 6.0

    def test_floors_at_zero(self):
        reserve = estimate_reserve(
            elapsed_hours_since_full=240.0,  # 10 days
            energy_in_since_full_kwh=2.0,
            depletion_kwh_per_day=3.0,
            capacity_kwh=CAPACITY,
        )
        assert reserve == 0.0

    def test_diversion_raises_reserve(self):
        common = dict(
            elapsed_hours_since_full=48.0, depletion_kwh_per_day=3.0, capacity_kwh=CAPACITY
        )
        low = estimate_reserve(energy_in_since_full_kwh=0.0, **common)
        high = estimate_reserve(energy_in_since_full_kwh=4.0, **common)
        assert high > low


class TestLearnDepletion:
    def test_learns_median_from_steady_days(self):
        samples = [2.0, 2.5, 3.0, 2.2, 2.8, 3.1]
        value, source = learn_depletion(samples, percentile=0.5, min_samples=5, fallback=3.0)
        assert source == "stats"
        assert min(samples) <= value <= max(samples)

    def test_falls_back_when_too_few_steady_days(self):
        value, source = learn_depletion([2.0, 3.0], percentile=0.5, min_samples=5, fallback=3.0)
        assert (value, source) == (3.0, "default")


class TestBoostRecommendation:
    def _rec(self, *, reserve, depletion=3.0, refill=0.0):
        return boost_recommendation(
            reserve_kwh=reserve,
            capacity_kwh=CAPACITY,
            threshold_percent=THRESHOLD,
            expected_refill_kwh=refill,
            depletion_kwh_per_day=depletion,
            heater_kw=HEATER_KW,
            min_hours=1,
            max_hours=2,
        )

    def test_no_prompt_when_projection_above_threshold(self):
        # reserve 8 - 3 + refill 2 = 7 >= 2.2 → no prompt
        assert self._rec(reserve=8.0, refill=2.0) == (False, None)

    def test_small_deficit_suggests_one_hour(self):
        # reserve 3 - 3 + 0 = 0; deficit 2.2; ceil(2.2/2.7) = 1
        assert self._rec(reserve=3.0) == (True, 1.0)

    def test_larger_deficit_suggests_two_hours(self):
        # reserve 2 - 3 + 0 = -1; deficit 3.2; ceil(3.2/2.7) = 2
        assert self._rec(reserve=2.0) == (True, 2.0)

    def test_hours_clamped_to_max(self):
        # reserve 0 - 4 + 0 = -4; deficit 6.2; ceil = 3 → clamped to 2
        assert self._rec(reserve=0.0, depletion=4.0) == (True, 2.0)

    def test_zero_heater_power_falls_back_to_max_hours(self):
        recommended, hours = boost_recommendation(
            reserve_kwh=1.0,
            capacity_kwh=CAPACITY,
            threshold_percent=THRESHOLD,
            expected_refill_kwh=0.0,
            depletion_kwh_per_day=3.0,
            heater_kw=0.0,
            min_hours=1,
            max_hours=2,
        )
        assert (recommended, hours) == (True, 2.0)

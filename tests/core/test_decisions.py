from energy_conductor.decisions import Decision, DecisionKind


class TestDecisionDedupe:
    def test_same_decision_same_key(self):
        a = Decision(
            kind=DecisionKind.SET_DISCHARGE_LIMIT,
            target_entity="number.inverter_discharge_power_limit",
            value=0,
            reason="off-peak",
            dedupe_key="discharge-0",
        )
        b = Decision(
            kind=DecisionKind.SET_DISCHARGE_LIMIT,
            target_entity="number.inverter_discharge_power_limit",
            value=0,
            reason="different reason text",
            dedupe_key="discharge-0",
        )
        assert a.dedupe_key == b.dedupe_key

    def test_frozen(self):
        d = Decision(
            kind=DecisionKind.SET_CHARGE_TARGET,
            target_entity="number.inverter_charge_target_soc",
            value=65,
            reason="test",
            dedupe_key="overnight-2026-06-01-65",
        )
        try:
            d.value = 70  # type: ignore[misc]
        except Exception as exc:
            msg = str(exc).lower()
            assert "frozen" in msg or "can't set" in msg or "cannot assign" in msg
        else:
            raise AssertionError("Decision should be frozen")

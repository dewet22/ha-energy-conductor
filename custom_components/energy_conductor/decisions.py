"""Decisions emitted by the core. The adapter is the only thing that acts on them."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DecisionKind(StrEnum):
    SET_CHARGE_TARGET = "set_charge_target"
    SET_DISCHARGE_LIMIT = "set_discharge_limit"
    SET_SLOT_TIME = "set_slot_time"  # value is an "HH:MM:SS" string, written via time.set_value
    RECOMMEND_HOT_WATER_BOOST = "recommend_hot_water_boost"  # notify-only; no write
    VERIFICATION_MISMATCH = "verification_mismatch"  # notify-only; actuation didn't take effect


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    target_entity: str
    value: Any
    reason: str
    dedupe_key: str

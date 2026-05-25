"""Decisions emitted by the core. The adapter is the only thing that acts on them."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class DecisionKind(StrEnum):
    SET_CHARGE_TARGET = "set_charge_target"
    SET_DISCHARGE_LIMIT = "set_discharge_limit"


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    target_entity: str
    value: Any
    reason: str
    dedupe_key: str

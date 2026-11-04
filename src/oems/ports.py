"""Ports: directed edges between a component and a bus."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .carrier import Bus


class Direction(StrEnum):
    IN = "in"    # bus -> component (withdrawal from the bus)
    OUT = "out"  # component -> bus (injection into the bus)


@dataclass(frozen=True, eq=False)
class Port:
    bus: Bus
    direction: Direction

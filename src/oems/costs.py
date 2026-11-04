"""Cost categories and generic flow costs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CostCategory(StrEnum):
    ENERGY = "ENERGY"
    NETWORK = "NETWORK"
    TAX = "TAX"
    PEAK = "PEAK"
    REVENUE = "REVENUE"
    OPERATION = "OPERATION"
    DEGRADATION = "DEGRADATION"
    FLEXIBILITY = "FLEXIBILITY"
    EMISSIONS = "EMISSIONS"
    FIXED = "FIXED"
    TIEBREAK = "TIEBREAK"
    SLACK = "SLACK"


#: Categories that are modelling artefacts and excluded from ``Results.total_cost``.
ARTIFICIAL = {CostCategory.TIEBREAK, CostCategory.SLACK}


def category(value) -> CostCategory:
    return value if isinstance(value, CostCategory) else CostCategory(str(value).upper())


@dataclass
class FlowCost:
    """A variable cost (EUR/MWh, scalar or series) on a component port.

    Negative values are refunds or subsidies.
    """

    rate: object
    category: CostCategory | str = CostCategory.OPERATION

    def __post_init__(self):
        self.category = category(self.category)

"""Energy carriers and buses (the nodes of the energy system graph)."""

from __future__ import annotations

from dataclasses import dataclass, field

CARRIER_KINDS = ("electricity", "heat", "cold", "fuel", "other")


@dataclass(eq=False)
class Carrier:
    """An energy type (electricity, heat, cold, gas, ...).

    ``kind`` gives the carrier semantics used for KPIs and connection checks.
    ``emission_factor`` ({"co2": t/MWh}) is the default factor applied to grid-connection imports
    of this carrier (emissions are counted at the system boundary).
    """

    name: str
    kind: str = "other"
    emission_factor: dict = field(default_factory=dict)
    description: str = ""

    def __post_init__(self):
        if self.kind not in CARRIER_KINDS:
            raise ValueError(f"Carrier kind must be one of {CARRIER_KINDS}, got {self.kind!r}")

    def __repr__(self) -> str:
        return f"Carrier({self.name!r}, kind={self.kind!r})"


@dataclass(eq=False)
class Bus:
    """A node with its own energy balance. Several buses may share a carrier
    (e.g. heat at 90 degC and at 45 degC).

    ``allow_slack`` adds penalised shortfall/excess variables so an otherwise infeasible
    balance can be diagnosed.
    """

    name: str
    carrier: Carrier
    allow_slack: bool = False
    slack_cost: float = 10_000.0

    def __repr__(self) -> str:
        return f"Bus({self.name!r}, carrier={self.carrier.name!r})"

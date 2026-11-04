"""Behind-the-meter production with an availability profile (PV, wind, waste heat)."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass

import numpy as np
import pyomo.environ as pyo

from ..costs import CostCategory
from ..exceptions import ValidationError
from ..ports import Direction, Port
from ..schema import Kind, param
from .base import Component, register_component


@register_component(group="Producer", icon="sun", label="Producer")
@dataclass(eq=False)
class Producer(Component):
    """Production limited by an availability profile.

    Availability is ``profile`` (MW) or ``capacity * cf``. A curtailable producer may produce less
    than available; the curtailed power is an explicit variable ``curtailment``.
    Port: ``out`` (OUT).
    """

    bus: object = param(Kind.BUS)
    _: KW_ONLY
    profile: object = param(Kind.SERIES, None, unit="MW", series="power", min=0, connector=True)
    capacity: float = param(Kind.SCALAR, None, unit="MW", min=0)
    cf: object = param(Kind.SERIES, None, series="ratio", min=0, connector=True, label="capacity factor")
    curtailable: bool = param(Kind.BOOL, True)
    marginal_cost: object = param(Kind.SERIES, 0.0, unit="EUR/MWh", series="rate")
    curtailment_cost: float = param(Kind.SCALAR, 0.0, unit="EUR/MWh", min=0)

    def resolve(self, grid, system, report=None, owner=None):
        super().resolve(grid, system, report, owner)
        r = self.r
        if r.profile is not None:
            r.available = r.profile
        elif r.capacity is not None and r.cf is not None:
            r.available = r.capacity * r.cf
        else:
            raise ValidationError(f"{self.name}: give either profile or capacity and cf")

    def ports(self):
        return {"out": Port(self.r.bus, Direction.OUT)}

    def port_bounds(self):
        av = self.r.available
        return {"out": (None, av) if self.curtailable else (av, av)}

    def build(self, blk, F, ctx):
        if not self.curtailable:
            return
        av = self.r.available
        out = F["out"]
        blk.curtailment = pyo.Var(ctx.T, within=pyo.NonNegativeReals)
        blk.available = pyo.Constraint(ctx.T, rule=lambda b, t: out[t] + b.curtailment[t] == float(av[t]))

    def cost_terms(self, blk, F, ctx):
        terms = {}
        cost = ctx.wsum(self.r.marginal_cost, F["out"])
        if self.curtailable and self.r.curtailment_cost:
            cost = cost + ctx.wsum(np.full(ctx.n, self.r.curtailment_cost), blk.curtailment)
        if not (isinstance(cost, float) and cost == 0.0):
            terms[CostCategory.OPERATION] = cost
        return terms

    def states(self, blk):
        return {"curtailment": blk.curtailment} if self.curtailable else {}

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

    With ``optimise_size`` (lifetime runs, see ``oems.sizing``) the capacity is a variable in
    [``capacity_min``, ``capacity_max``]: availability ``size * cf``, CAPEX ``capex * size`` once and OPEX
    ``opex * size`` per year.
    """

    bus: object = param(Kind.BUS)
    _: KW_ONLY
    profile: object = param(Kind.SERIES, None, unit="MW", series="power", min=0, connector=True)
    capacity: float = param(Kind.SCALAR, None, unit="MW", min=0)
    cf: object = param(Kind.SERIES, None, series="ratio", min=0, connector=True, label="capacity factor")
    curtailable: bool = param(Kind.BOOL, True, label="Allow curtailment",
                              help="Ticked: the optimiser may use less than the available profile (the rest is "
                                   "curtailed). Unticked: all available output must be used or exported")
    marginal_cost: object = param(Kind.SERIES, 0.0, unit="EUR/MWh", series="rate")
    curtailment_cost: float = param(Kind.SCALAR, 0.0, unit="EUR/MWh", min=0)
    optimise_size: bool = param(Kind.BOOL, False, group="Investment", label="Optimise capacity",
                                help="The capacity is chosen by the lifetime optimisation (needs cf, CAPEX and OPEX)")
    capacity_min: float = param(Kind.SCALAR, 0.0, unit="MW", min=0, group="Investment", label="Min capacity")
    capacity_max: float = param(Kind.SCALAR, None, unit="MW", min=0, group="Investment", label="Max capacity",
                                help="Empty = no limit")
    capex: float = param(Kind.SCALAR, None, unit="EUR/MW", min=0, group="Investment", label="CAPEX")
    opex: float = param(Kind.SCALAR, None, unit="EUR/MW/yr", min=0, group="Investment", label="OPEX")

    SIZED = ("capacity",)

    def resolve(self, grid, system, report=None, owner=None):
        super().resolve(grid, system, report, owner)
        r = self.r
        if self.optimise_size:
            if r.cf is None:
                raise ValidationError(f"{self.name}: an optimised capacity needs a capacity factor (cf), not a profile")
            r.available = None                  # size * cf, set by the solve
        elif r.profile is not None:
            r.available = r.profile
        elif r.capacity is not None and r.cf is not None:
            r.available = r.capacity * r.cf
        else:
            raise ValidationError(f"{self.name}: give either profile or capacity and cf")

    def ports(self):
        return {"out": Port(self.r.bus, Direction.OUT)}

    def port_bounds(self):
        av = self.r.available
        if av is None:
            return {}
        return {"out": (None, av) if self.curtailable else (av, av)}

    def check(self, grid):
        r = self.r
        if self.optimise_size:
            self.check_sizing(grid, {"CAPEX": r.capex, "OPEX": r.opex})
            if r.capacity_max is not None and r.capacity_max < r.capacity_min:
                raise ValidationError(f"{self.name}: max capacity is below min capacity")
        return []

    def build(self, blk, F, ctx):
        out = F["out"]
        if self.optimise_size:
            r = self.r
            cf = r.cf
            blk.size = pyo.Var(bounds=(r.capacity_min, r.capacity_max))
            if not self.curtailable:
                blk.available = pyo.Constraint(ctx.T, rule=lambda b, t: out[t] == float(cf[t]) * b.size)
                return
            blk.curtailment = pyo.Var(ctx.T, within=pyo.NonNegativeReals)
            blk.available = pyo.Constraint(
                ctx.T, rule=lambda b, t: out[t] + b.curtailment[t] == float(cf[t]) * b.size)
            return
        if not self.curtailable:
            return
        av = self.r.available
        blk.curtailment = pyo.Var(ctx.T, within=pyo.NonNegativeReals)
        blk.available = pyo.Constraint(ctx.T, rule=lambda b, t: out[t] + b.curtailment[t] == float(av[t]))

    def cost_terms(self, blk, F, ctx):
        terms = {}
        cost = ctx.wsum(self.r.marginal_cost, F["out"])
        if self.curtailable and self.r.curtailment_cost:
            cost = cost + ctx.wsum(np.full(ctx.n, self.r.curtailment_cost), blk.curtailment)
        if not (isinstance(cost, float) and cost == 0.0):
            terms[CostCategory.OPERATION] = cost
        if self.optimise_size:
            terms[CostCategory.INVESTMENT] = self.r.capex * blk.size
            terms[CostCategory.FIXED_OM] = self.r.opex * ctx.horizon.annuity * blk.size
        return terms

    def solved_sizes(self, blk):
        if not self.optimise_size:
            return {}
        size = max(0.0, float(pyo.value(blk.size)))
        self.r.capacity, self.r.available = size, size * self.r.cf
        return {"capacity": (size, "MW")}

    def states(self, blk):
        return {"curtailment": blk.curtailment} if self.curtailable else {}

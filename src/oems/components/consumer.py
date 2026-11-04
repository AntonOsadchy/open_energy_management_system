"""Consumers: fixed demand, flexible (shiftable) demand and sinks."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass

import numpy as np
import pyomo.environ as pyo

from ..costs import CostCategory
from ..exceptions import ValidationError
from ..ports import Direction, Port
from ..schema import Kind, param
from .base import Component, register_component


@register_component(group="Consumer", icon="plug", label="Demand")
@dataclass(eq=False)
class Demand(Component):
    """Inflexible demand: ``F_in[t] = profile[t]``. Port: ``in`` (IN)."""

    bus: object = param(Kind.BUS)
    _: KW_ONLY
    profile: object = param(Kind.SERIES, unit="MW", series="power", min=0, connector=True)

    def ports(self):
        return {"in": Port(self.r.bus, Direction.IN)}

    def port_bounds(self):
        return {"in": (self.r.profile, self.r.profile)}

    def build(self, blk, F, ctx):
        pass


@register_component(group="Consumer", icon="sink", label="Sink")
@dataclass(eq=False)
class Sink(Component):
    """Optional energy dump (heat rejection, dry cooler, dump load) with a cost per MWh.
    Port: ``in`` (IN)."""

    bus: object = param(Kind.BUS)
    _: KW_ONLY
    p_max: object = param(Kind.SERIES, float("inf"), unit="MW", series="power", min=0)
    cost: object = param(Kind.SERIES, 0.0, unit="EUR/MWh", series="rate")

    def ports(self):
        return {"in": Port(self.r.bus, Direction.IN)}

    def port_bounds(self):
        return {"in": (None, self.r.p_max)}

    def build(self, blk, F, ctx):
        pass

    def cost_terms(self, blk, F, ctx):
        cost = ctx.wsum(self.r.cost, F["in"])
        return {} if isinstance(cost, float) else {CostCategory.OPERATION: cost}


@register_component(group="Consumer", icon="shift", label="Flexible demand")
@dataclass(eq=False)
class FlexibleDemand(Component):
    """Shiftable demand modelled as a virtual storage of backlog energy.

    The baseline ``D`` may be deferred (backlog ``s_plus``, at most ``max_deferred`` MWh) or
    advanced (``s_minus``, at most ``max_advanced`` MWh). The backlog must be zero at the end of
    every ``window`` ("D", "W", "M" or None = horizon end), so energy is conserved per window.
    ``rebound`` (>= 0) adds extra energy per MWh deferred that must also be served.

    Port: ``in`` (IN). States: ``s_plus``, ``s_minus``.
    """

    bus: object = param(Kind.BUS)
    _: KW_ONLY
    baseline: object = param(Kind.SERIES, unit="MW", series="power", min=0, connector=True)
    p_min: object = param(Kind.SERIES, 0.0, unit="MW", series="power", min=0)
    p_max: object = param(Kind.SERIES, float("inf"), unit="MW", series="power", min=0)
    window: object = param(Kind.PERIOD, "D", help="Energy must balance within each window: D, W, M or None")
    max_deferred: float = param(Kind.SCALAR, None, unit="MWh", min=0)
    max_advanced: float = param(Kind.SCALAR, None, unit="MWh", min=0)
    rebound: float = param(Kind.SCALAR, 0.0, min=0, help="Extra MWh per MWh deferred")
    deviation_cost: float = param(Kind.SCALAR, 0.0, unit="EUR/MWh", min=0)

    def ports(self):
        return {"in": Port(self.r.bus, Direction.IN)}

    def port_bounds(self):
        return {"in": (self.r.p_min, self.r.p_max)}

    def check(self, grid):
        r = self.r
        if np.any(r.p_min > r.p_max):
            raise ValidationError(f"{self.name}: p_min > p_max")
        return []

    def build(self, blk, F, ctx):
        r = self.r
        D, dt = r.baseline, ctx.dt
        flow = F["in"]
        blk.s_plus = pyo.Var(ctx.T, within=pyo.NonNegativeReals, bounds=(0, r.max_deferred))
        blk.s_minus = pyo.Var(ctx.T, within=pyo.NonNegativeReals, bounds=(0, r.max_advanced))
        rho = r.rebound
        if rho > 0:
            blk.d = pyo.Var(ctx.T, within=pyo.NonNegativeReals, bounds=lambda b, t: (0, float(D[t])))
            blk.d_def = pyo.Constraint(ctx.T, rule=lambda b, t: b.d[t] >= float(D[t]) - flow[t])

        def backlog(b, t):
            prev = (b.s_plus[t - 1] - b.s_minus[t - 1]) if t > 0 else 0.0
            reb = rho * b.d[t] * float(dt[t]) if rho > 0 else 0.0
            return b.s_plus[t] - b.s_minus[t] == prev + (float(D[t]) - flow[t]) * float(dt[t]) + reb

        blk.backlog = pyo.Constraint(ctx.T, rule=backlog)
        ids = ctx.grid.groups(self.window)
        ends = [int(t) for t in np.flatnonzero(ctx.grid.last_of_group(ids))]
        blk.close_plus = pyo.Constraint(ends, rule=lambda b, t: b.s_plus[t] == 0)
        blk.close_minus = pyo.Constraint(ends, rule=lambda b, t: b.s_minus[t] == 0)
        if r.deviation_cost > 0:
            blk.u = pyo.Var(ctx.T, within=pyo.NonNegativeReals)
            blk.u_pos = pyo.Constraint(ctx.T, rule=lambda b, t: b.u[t] >= flow[t] - float(D[t]))
            blk.u_neg = pyo.Constraint(ctx.T, rule=lambda b, t: b.u[t] >= float(D[t]) - flow[t])

    def cost_terms(self, blk, F, ctx):
        out = {}
        if self.r.deviation_cost > 0:
            out[CostCategory.FLEXIBILITY] = ctx.wsum(np.full(ctx.n, self.r.deviation_cost), blk.u)
        if ctx.tie_break > 0:
            tb = np.full(ctx.n, ctx.tie_break)
            out[CostCategory.TIEBREAK] = ctx.wsum(tb, blk.s_plus) + ctx.wsum(tb, blk.s_minus)
        return out

    def states(self, blk):
        return {"s_plus": blk.s_plus, "s_minus": blk.s_minus}

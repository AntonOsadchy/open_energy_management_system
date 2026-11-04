"""Generic storage on one bus (battery, hot/cold water tank, steam accumulator, H2 tank)."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass

import numpy as np
import pyomo.environ as pyo

from ..costs import CostCategory
from ..exceptions import ValidationError
from ..ports import Direction, Port
from ..schema import Kind, param
from .base import Component, register_component

END_CONDITIONS = ("cyclic_geq", "cyclic_eq", "fixed", "free")


@register_component(group="Storage", icon="battery", label="Storage")
@dataclass(eq=False)
class Storage(Component):
    """Storage with charge/discharge efficiencies and self-discharge.

    ``e[t] = (1 - self_discharge)^dt * e[t-1] + (eta_ch * F_charge - F_discharge / eta_dis) * dt``

    Ports: ``charge`` (IN), ``discharge`` (OUT). State: ``e`` (MWh).
    ``self_discharge`` is the fraction lost per hour, so it does not depend on the resolution.
    """

    bus: object = param(Kind.BUS)
    _: KW_ONLY
    e_max: float = param(Kind.SCALAR, unit="MWh", min=0)
    p_ch_max: object = param(Kind.SERIES, unit="MW", series="power", min=0)
    p_dis_max: object = param(Kind.SERIES, unit="MW", series="power", min=0)
    eta_ch: float = param(Kind.SCALAR, 0.95, min=1e-6, max=1)
    eta_dis: float = param(Kind.SCALAR, 0.95, min=1e-6, max=1)
    self_discharge: float = param(Kind.SCALAR, 0.0, unit="1/h", min=0, max=1)
    soc_min: float = param(Kind.SCALAR, 0.0, min=0, max=1)
    soc_max: float = param(Kind.SCALAR, 1.0, min=0, max=1)
    soc_init: float = param(Kind.SCALAR, 0.5, min=0, max=1)
    end: str = param(Kind.ENUM, "cyclic_geq", choices=END_CONDITIONS)
    soc_end: float = param(Kind.SCALAR, None, min=0, max=1)
    exclusive: bool = param(Kind.BOOL, False, help="Discourage simultaneous charge and discharge (LP relaxation)")
    cycle_cost: float = param(Kind.SCALAR, 0.0, unit="EUR/MWh", min=0, help="Cost per MWh discharged")

    def ports(self):
        return {"charge": Port(self.r.bus, Direction.IN), "discharge": Port(self.r.bus, Direction.OUT)}

    def port_bounds(self):
        return {"charge": (None, self.r.p_ch_max), "discharge": (None, self.r.p_dis_max)}

    def check(self, grid):
        r = self.r
        if not r.soc_min <= r.soc_init <= r.soc_max:
            raise ValidationError(f"{self.name}: need soc_min <= soc_init <= soc_max")
        if r.end == "fixed" and r.soc_end is None:
            raise ValidationError(f"{self.name}: end='fixed' requires soc_end")
        if r.end == "fixed" and not r.soc_min <= r.soc_end <= r.soc_max:
            raise ValidationError(f"{self.name}: soc_end outside [soc_min, soc_max]")
        if self.exclusive and (np.any(~np.isfinite(r.p_ch_max)) or np.any(~np.isfinite(r.p_dis_max))):
            raise ValidationError(f"{self.name}: exclusive=True requires finite p_ch_max and p_dis_max")
        return []

    def build(self, blk, F, ctx):
        r = self.r
        E = r.e_max
        ch, dis = F["charge"], F["discharge"]
        keep = (1.0 - r.self_discharge) ** ctx.dt
        e0 = r.soc_init * E
        blk.e = pyo.Var(ctx.T, bounds=(r.soc_min * E, r.soc_max * E))

        def balance(b, t):
            prev = b.e[t - 1] if t > 0 else e0
            return b.e[t] == float(keep[t]) * prev + (r.eta_ch * ch[t] - dis[t] / r.eta_dis) * float(ctx.dt[t])

        blk.balance = pyo.Constraint(ctx.T, rule=balance)
        last = ctx.n - 1
        if r.end == "cyclic_geq":
            blk.end = pyo.Constraint(expr=blk.e[last] >= e0)
        elif r.end == "cyclic_eq":
            blk.end = pyo.Constraint(expr=blk.e[last] == e0)
        elif r.end == "fixed":
            blk.end = pyo.Constraint(expr=blk.e[last] == r.soc_end * E)
        if self.exclusive:
            pc, pd_ = r.p_ch_max, r.p_dis_max
            idx = [t for t in range(ctx.n) if pc[t] > 0 and pd_[t] > 0]
            blk.exclusive = pyo.Constraint(
                idx, rule=lambda b, t: ch[t] / float(pc[t]) + dis[t] / float(pd_[t]) <= 1)

    def cost_terms(self, blk, F, ctx):
        out = {}
        if self.r.cycle_cost:
            out[CostCategory.DEGRADATION] = ctx.wsum(np.full(ctx.n, self.r.cycle_cost), F["discharge"])
        if ctx.tie_break > 0:
            tb = np.full(ctx.n, ctx.tie_break)
            out[CostCategory.TIEBREAK] = ctx.wsum(tb, F["charge"]) + ctx.wsum(tb, F["discharge"])
        return out

    def states(self, blk):
        return {"e": blk.e}

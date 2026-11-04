"""Thermal zone (1R1C) for flexible space or process heating and cooling."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass

import numpy as np
import pyomo.environ as pyo

from ..costs import CostCategory
from ..exceptions import ValidationError
from ..ports import Direction, Port
from ..schema import Kind, param
from .base import Component, register_component


@register_component(group="Thermal", icon="thermometer", label="Thermal zone")
@dataclass(eq=False)
class ThermalZone(Component):
    """Lumped thermal mass kept inside a temperature band.

    ``theta[t] = theta[t-1] + dt/C * (F_heat - F_cold + gains - (theta[t-1] - theta_amb) / R)``

    ``C`` in MWh/K, ``R`` in K/MW. Ports ``heat`` and ``cold`` (both IN) exist only when the
    corresponding bus is given. With ``comfort_penalty`` (EUR per K*h) the band becomes soft.
    State: ``theta`` (degC).
    """

    _: KW_ONLY
    heat_bus: object = param(Kind.BUS, None, carrier_kind="heat")
    cold_bus: object = param(Kind.BUS, None, carrier_kind="cold")
    C: float = param(Kind.SCALAR, unit="MWh/K", min=1e-9)
    R: float = param(Kind.SCALAR, unit="K/MW", min=1e-9)
    theta_amb: object = param(Kind.SERIES, unit="degC", series="state", connector=True)
    theta_min: object = param(Kind.SERIES, unit="degC", series="state", connector=True)
    theta_max: object = param(Kind.SERIES, unit="degC", series="state", connector=True)
    theta_init: float = param(Kind.SCALAR, unit="degC")
    gains: object = param(Kind.SERIES, 0.0, unit="MW", series="power", connector=True)
    q_heat_max: object = param(Kind.SERIES, float("inf"), unit="MW", series="power", min=0)
    q_cold_max: object = param(Kind.SERIES, float("inf"), unit="MW", series="power", min=0)
    cyclic: bool = param(Kind.BOOL, False)
    comfort_penalty: float = param(Kind.SCALAR, None, unit="EUR/Kh", min=0)

    def ports(self):
        out = {}
        if self.r.heat_bus is not None:
            out["heat"] = Port(self.r.heat_bus, Direction.IN)
        if self.r.cold_bus is not None:
            out["cold"] = Port(self.r.cold_bus, Direction.IN)
        return out

    def port_bounds(self):
        out = {}
        if self.r.heat_bus is not None:
            out["heat"] = (None, self.r.q_heat_max)
        if self.r.cold_bus is not None:
            out["cold"] = (None, self.r.q_cold_max)
        return out

    def check(self, grid):
        r = self.r
        if r.heat_bus is None and r.cold_bus is None:
            raise ValidationError(f"{self.name}: needs heat_bus and/or cold_bus")
        if np.any(r.theta_min > r.theta_max):
            raise ValidationError(f"{self.name}: theta_min > theta_max")
        tau = r.R * r.C
        if grid.step_hours >= tau:
            raise ValidationError(f"{self.name}: time step {grid.step_hours} h >= R*C = {tau:g} h "
                                  "(explicit Euler unstable); use a finer resolution")
        if grid.step_hours > 0.2 * tau:
            return [f"{self.name}: time step is more than 20% of R*C = {tau:g} h; dynamics are coarse"]
        return []

    def build(self, blk, F, ctx):
        r = self.r
        heat = F["heat"] if r.heat_bus is not None else None
        cold = F["cold"] if r.cold_bus is not None else None
        blk.theta = pyo.Var(ctx.T)
        soft = r.comfort_penalty is not None

        def dyn(b, t):
            prev = b.theta[t - 1] if t > 0 else r.theta_init
            q = float(r.gains[t])
            if heat is not None:
                q = q + heat[t]
            if cold is not None:
                q = q - cold[t]
            return b.theta[t] == prev + float(ctx.dt[t]) / r.C * (q - (prev - float(r.theta_amb[t])) / r.R)

        blk.dynamics = pyo.Constraint(ctx.T, rule=dyn)
        if soft:
            blk.v_low = pyo.Var(ctx.T, within=pyo.NonNegativeReals)
            blk.v_high = pyo.Var(ctx.T, within=pyo.NonNegativeReals)
        blk.lower = pyo.Constraint(ctx.T, rule=lambda b, t: b.theta[t] + (b.v_low[t] if soft else 0)
                                   >= float(r.theta_min[t]))
        blk.upper = pyo.Constraint(ctx.T, rule=lambda b, t: b.theta[t] - (b.v_high[t] if soft else 0)
                                   <= float(r.theta_max[t]))
        if r.cyclic:
            blk.cyclic = pyo.Constraint(expr=blk.theta[ctx.n - 1] == r.theta_init)

    def cost_terms(self, blk, F, ctx):
        if self.r.comfort_penalty is None:
            return {}
        c = np.full(ctx.n, self.r.comfort_penalty)
        return {CostCategory.FLEXIBILITY: ctx.wsum(c, blk.v_low) + ctx.wsum(c, blk.v_high)}

    def states(self, blk):
        return {"theta": blk.theta}

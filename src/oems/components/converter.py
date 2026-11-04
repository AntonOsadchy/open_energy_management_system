"""Generic linear converter with any number of input and output buses."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass

import numpy as np
import pyomo.environ as pyo

from ..costs import CostCategory
from ..exceptions import ValidationError
from ..ports import Direction, Port
from ..schema import Kind, param
from .base import Component, register_component


@register_component(group="Converter", icon="convert", label="Converter")
@dataclass(eq=False)
class Converter(Component):
    """Linear conversion between carriers (heat pump, e-boiler, chiller, boiler, CHP, link).

    The flow on the reference input ``F_ref`` is the activity. Every other port is coupled to it:
    ``F_in_i = a_i / a_ref * F_ref`` and ``F_out_o = b_o / a_ref * F_ref``. Coefficients may be time
    series (e.g. a COP profile). ``p_max``/``p_min`` bound the flow on ``capacity_port`` (a bus name;
    default: the reference input). ``ramp`` limits the change of ``F_ref`` in MW per hour.

    Ports: ``in_<bus>`` (IN) per input, ``out_<bus>`` (OUT) per output.
    """

    inputs: dict = param(Kind.BUS_MAP, help="{bus: coefficient}")
    outputs: dict = param(Kind.BUS_MAP, help="{bus: coefficient}")
    _: KW_ONLY
    reference: str = param(Kind.STR, None, help="Input bus whose flow is the activity (default: first input)")
    capacity_port: str = param(Kind.STR, None, help="Bus whose flow p_max/p_min apply to (default: reference)")
    p_max: object = param(Kind.SERIES, float("inf"), unit="MW", series="power", min=0)
    p_min: object = param(Kind.SERIES, 0.0, unit="MW", series="power", min=0)
    ramp: float = param(Kind.SCALAR, None, unit="MW/h", min=0)
    marginal_cost: object = param(Kind.SERIES, 0.0, unit="EUR/MWh", series="rate",
                                  help="Cost per MWh of reference flow")

    def resolve(self, grid, system, report=None, owner=None):
        super().resolve(grid, system, report, owner)
        r = self.r
        if not r.inputs or not r.outputs:
            raise ValidationError(f"{self.name}: needs at least one input and one output bus")
        both = set(b.name for b in r.inputs) & set(b.name for b in r.outputs)
        if both:
            raise ValidationError(f"{self.name}: bus {sorted(both)} is both input and output")
        ref_bus = system.bus(r.reference) if r.reference is not None else next(iter(r.inputs))
        if ref_bus not in r.inputs:
            raise ValidationError(f"{self.name}: reference {ref_bus.name!r} is not an input")
        a_ref = r.inputs[ref_bus]
        if np.any(a_ref <= 0):
            raise ValidationError(f"{self.name}: reference input coefficient must be > 0")
        r.ref_port = f"in_{ref_bus.name}"
        # coefficients relative to the reference flow
        r.coef = {f"in_{b.name}": a / a_ref for b, a in r.inputs.items()}
        r.coef.update({f"out_{b.name}": c / a_ref for b, c in r.outputs.items()})
        r.bus_of = {f"in_{b.name}": b for b in r.inputs} | {f"out_{b.name}": b for b in r.outputs}
        if r.capacity_port is None:
            r.cap_port = r.ref_port
        else:
            cap_bus = system.bus(r.capacity_port)
            if cap_bus in r.inputs:
                r.cap_port = f"in_{cap_bus.name}"
            elif cap_bus in r.outputs:
                r.cap_port = f"out_{cap_bus.name}"
            else:
                raise ValidationError(f"{self.name}: capacity_port {cap_bus.name!r} is not connected")

    def ports(self):
        r = self.r
        return {p: Port(b, Direction.IN if p.startswith("in_") else Direction.OUT) for p, b in r.bus_of.items()}

    def port_bounds(self):
        r = self.r
        if np.any(r.p_min > r.p_max):
            raise ValidationError(f"{self.name}: p_min > p_max")
        lb = r.p_min if np.any(r.p_min > 0) else None
        return {r.cap_port: (lb, r.p_max)}

    def build(self, blk, F, ctx):
        r = self.r
        ref = F[r.ref_port]
        coupled = [p for p in r.coef if p != r.ref_port]
        blk.couple = pyo.Constraint(
            coupled, ctx.T, rule=lambda b, p, t: F[p][t] == float(r.coef[p][t]) * ref[t])
        if r.ramp is not None:
            idx = range(1, ctx.n)
            blk.ramp_up = pyo.Constraint(idx, rule=lambda b, t: ref[t] - ref[t - 1] <= r.ramp * float(ctx.dt[t]))
            blk.ramp_dn = pyo.Constraint(idx, rule=lambda b, t: ref[t - 1] - ref[t] <= r.ramp * float(ctx.dt[t]))

    def cost_terms(self, blk, F, ctx):
        out = {}
        ref = F[self.r.ref_port]
        cost = ctx.wsum(self.r.marginal_cost, ref)
        if not isinstance(cost, float):
            out[CostCategory.OPERATION] = cost
        if ctx.tie_break > 0:
            out[CostCategory.TIEBREAK] = ctx.wsum(np.full(ctx.n, ctx.tie_break), ref)
        return out

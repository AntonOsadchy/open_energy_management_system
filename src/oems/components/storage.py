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

    With ``optimise_size`` (lifetime runs, see ``oems.sizing``) energy ``E`` (MWh) and power ``P`` (MW, the same
    for charge and discharge) are variables within their min/max, optionally tied by ``duration`` (E = duration *
    P): CAPEX ``capex_energy * E + capex_power * P`` once and OPEX ``opex * E`` per year. ``e_max``, ``p_ch_max`` and
    ``p_dis_max`` are then not used (only the size limits ``e_size_*`` / ``p_size_*``).
    """

    bus: object = param(Kind.BUS)
    _: KW_ONLY
    # required, unless optimise_size makes them decision variables (None default only for that case)
    e_max: float = param(Kind.SCALAR, None, unit="MWh", min=0, required=True, optional=False)
    p_ch_max: object = param(Kind.SERIES, None, unit="MW", series="power", min=0, required=True, optional=False)
    p_dis_max: object = param(Kind.SERIES, None, unit="MW", series="power", min=0, required=True, optional=False)
    eta_ch: float = param(Kind.SCALAR, 0.95, min=1e-6, max=1)
    eta_dis: float = param(Kind.SCALAR, 0.95, min=1e-6, max=1)
    self_discharge: float = param(Kind.SCALAR, 0.0, unit="1/h", min=0, max=1)
    soc_min: float = param(Kind.SCALAR, 0.0, min=0, max=1)
    soc_max: float = param(Kind.SCALAR, 1.0, min=0, max=1)
    soc_init: float = param(Kind.SCALAR, 0.5, min=0, max=1)
    end: str = param(Kind.ENUM, "cyclic_geq", choices=END_CONDITIONS)
    soc_end: float = param(Kind.SCALAR, None, min=0, max=1)
    exclusive: bool = param(Kind.BOOL, False, help="Discourage simultaneous charge and discharge (LP relaxation)")
    cycle_cost: float = param(Kind.SCALAR, 0.0, unit="EUR/MWh", min=0, help="Cost per MWh discharged",
                              group="Cycling", label="Cycle cost")
    max_cycles: float = param(Kind.SCALAR, None, min=0, optional=True, group="Cycling", label="Max cycles",
                              help="Max equivalent full cycles per period, counted on stored energy: a cycle = "
                                   "(eta_ch * charged + discharged / eta_dis) / 2 / e_max, e.g. 1 per day or 365 per "
                                   "year. Empty = no limit")
    cycle_period: str = param(Kind.ENUM, "D", choices=("D", "W", "M", "Y"), group="Cycling", label="per",
                              help="Calendar period of Max cycles (local time): D day, W week, M month, Y year")
    degradation: object = param(Kind.CURVES, None, connector=True, group="Degradation", label="SoH curves",
                                help="State-of-health curves by cycling rate (e.g. a BESS IQ degradation profile). "
                                     "Used by multi-year runs: capacity falls with age and cumulative cycles. "
                                     "Not connected = the fixed SoH, else 100 % every year")
    soh: float = param(Kind.SCALAR, None, min=0, max=1, optional=True, group="Degradation", label="Fixed SoH",
                       help="State of health instead of SoH curves: the usable energy is SoH x e_max (e.g. 0.8 = "
                            "80 %), the same in every run and every year; power is unchanged. Empty = 100 % (or the "
                            "wired curves)")
    optimise_size: bool = param(Kind.BOOL, False, group="Investment", label="Optimise size",
                                help="Energy and power are chosen by the lifetime optimisation (needs CAPEX and OPEX)")
    e_size_min: float = param(Kind.SCALAR, 0.0, unit="MWh", min=0, group="Investment", label="Min energy")
    e_size_max: float = param(Kind.SCALAR, None, unit="MWh", min=0, group="Investment", label="Max energy",
                              help="Empty = no limit")
    p_size_min: float = param(Kind.SCALAR, 0.0, unit="MW", min=0, group="Investment", label="Min power")
    p_size_max: float = param(Kind.SCALAR, None, unit="MW", min=0, group="Investment", label="Max power",
                              help="Empty = no limit")
    duration: float = param(Kind.SCALAR, None, unit="h", min=0, group="Investment", label="Duration",
                            help="Fixed energy-to-power ratio E = duration x P. Empty = free")
    capex_energy: float = param(Kind.SCALAR, None, unit="EUR/MWh", min=0, group="Investment", label="CAPEX energy")
    capex_power: float = param(Kind.SCALAR, None, unit="EUR/MW", min=0, group="Investment", label="CAPEX power")
    opex: float = param(Kind.SCALAR, None, unit="EUR/MWh/yr", min=0, group="Investment", label="OPEX")

    SIZED = ("e_max", "p_ch_max", "p_dis_max")

    def ports(self):
        return {"charge": Port(self.r.bus, Direction.IN), "discharge": Port(self.r.bus, Direction.OUT)}

    def port_bounds(self):
        return {"charge": (None, self.r.p_ch_max), "discharge": (None, self.r.p_dis_max)}

    def check(self, grid):
        r = self.r
        if self.optimise_size:
            self.check_sizing(grid, {"CAPEX energy": r.capex_energy, "CAPEX power": r.capex_power, "OPEX": r.opex})
            for lo, hi, what in ((r.e_size_min, r.e_size_max, "energy"), (r.p_size_min, r.p_size_max, "power")):
                if hi is not None and hi < lo:
                    raise ValidationError(f"{self.name}: max {what} is below min {what}")
        if r.soh is not None:
            if r.soh <= 0:
                raise ValidationError(f"{self.name}: fixed SoH must be above 0 (e.g. 0.8 for 80 %)")
            if r.degradation:
                raise ValidationError(f"{self.name}: use either SoH curves or a fixed SoH, not both")
        if not r.soc_min <= r.soc_init <= r.soc_max:
            raise ValidationError(f"{self.name}: need soc_min <= soc_init <= soc_max")
        if r.end == "fixed" and r.soc_end is None:
            raise ValidationError(f"{self.name}: end='fixed' requires soc_end")
        if r.end == "fixed" and not r.soc_min <= r.soc_end <= r.soc_max:
            raise ValidationError(f"{self.name}: soc_end outside [soc_min, soc_max]")
        if self.exclusive and not self.optimise_size and (np.any(~np.isfinite(r.p_ch_max))
                                                          or np.any(~np.isfinite(r.p_dis_max))):
            raise ValidationError(f"{self.name}: exclusive=True requires finite p_ch_max and p_dis_max")
        return []

    def build(self, blk, F, ctx):
        r = self.r
        ch, dis = F["charge"], F["discharge"]
        keep = (1.0 - r.self_discharge) ** ctx.dt
        soh = self.usable()
        if self.optimise_size:
            blk.E = pyo.Var(bounds=(r.e_size_min, r.e_size_max))
            blk.P = pyo.Var(bounds=(r.p_size_min, r.p_size_max))
            if r.duration is not None:
                blk.duration = pyo.Constraint(expr=blk.E == r.duration * blk.P)
            E = soh * blk.E                 # usable energy (CAPEX and OPEX are on the nominal blk.E)
            blk.e = pyo.Var(ctx.T, within=pyo.NonNegativeReals)
            blk.e_max = pyo.Constraint(ctx.T, rule=lambda b, t: b.e[t] <= r.soc_max * E)
            if r.soc_min > 0:
                blk.e_min = pyo.Constraint(ctx.T, rule=lambda b, t: b.e[t] >= r.soc_min * E)
            if self.exclusive:      # a shared power rating: charge + discharge <= P covers both limits
                blk.p_max = pyo.Constraint(ctx.T, rule=lambda b, t: ch[t] + dis[t] <= b.P)
            else:
                blk.p_ch = pyo.Constraint(ctx.T, rule=lambda b, t: ch[t] <= b.P)
                blk.p_dis = pyo.Constraint(ctx.T, rule=lambda b, t: dis[t] <= b.P)
        else:
            E = soh * r.e_max
            blk.e = pyo.Var(ctx.T, bounds=(r.soc_min * E, r.soc_max * E))
        e0 = r.soc_init * E

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
        if r.max_cycles is not None:
            # stored-energy throughput per calendar period:
            # sum (eta_ch * charge + discharge / eta_dis) * dt / 2 <= max_cycles * e_max
            gid = ctx.grid.groups(r.cycle_period)
            periods = sorted(set(int(g) for g in gid))
            steps = {k: np.flatnonzero(gid == k) for k in periods}
            blk.cycles = pyo.Constraint(periods, rule=lambda b, k: pyo.quicksum(
                (r.eta_ch * ch[int(t)] + dis[int(t)] / r.eta_dis) * float(ctx.dt[t]) for t in steps[k]) / 2
                <= r.max_cycles * E)
        if self.exclusive and not self.optimise_size:
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
        if self.optimise_size:
            r = self.r
            out[CostCategory.INVESTMENT] = r.capex_energy * blk.E + r.capex_power * blk.P
            out[CostCategory.FIXED_OM] = r.opex * ctx.horizon.annuity * blk.E
        return out

    def solved_sizes(self, blk):
        if not self.optimise_size:
            return {}
        e, p = (max(0.0, float(pyo.value(v))) for v in (blk.E, blk.P))
        self.r.e_max, self.r.p_ch_max, self.r.p_dis_max = e, p, p
        return {"energy": (e, "MWh"), "power": (p, "MW")}

    def states(self, blk):
        return {"e": blk.e}

    def usable(self) -> float:
        """Fraction of the nominal energy that can be stored: the fixed SoH (1 when not set)."""
        return 1.0 if self.r.soh is None else float(self.r.soh)

    def derived_states(self, values):
        """State of charge: stored energy / usable capacity (0…1, comparable with ``soc_min`` / ``soc_max``)."""
        cap = np.broadcast_to(np.asarray(self.r.e_max, dtype=float) * self.usable(), values["e"].shape)
        return {"soc": np.divide(values["e"], cap, out=np.zeros_like(values["e"]), where=cap > 0)}

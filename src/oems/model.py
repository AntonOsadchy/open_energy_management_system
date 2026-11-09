"""Assembly of the Pyomo model.

Model structure (see README for the full formulation)::

    m.T                  time steps 0..N-1
    m.P                  port ids "component.port"
    m.F[p, t] >= 0       explicit flow on every component-bus edge [MW]
    m.c_<component>      one block per component (its own variables and constraints)
    m.balance[b, t]      sum(OUT flows) - sum(IN flows) + slack_pos - slack_neg == 0   per bus
    m.C[c, k]            cost of component c in category k [EUR]   (variable, defined by equality)
    m.E[c, g]            emissions of component c of gas g [t]     (variable, defined by equality)
    m.Et[g, t]           system emissions per step [t]             (expression)
    m.obj                min sum(m.C)
    m.hurdle[i]          investment hurdles of a lifetime optimisation (sizing.hurdle_expr)
    m.self_sufficiency   grid import <= (1 - target) * demand of a lifetime optimisation (electricity buses)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pyomo.environ as pyo

from .components.base import BuildContext, PortFlows, safe_name
from .costs import CostCategory
from .ports import Direction
from .timeseries import TimeGrid


@dataclass
class PortInfo:
    pid: str
    component: str
    port: str
    bus: str
    direction: Direction


@dataclass
class BuildMeta:
    grid: TimeGrid
    ports: list[PortInfo] = field(default_factory=list)
    blocks: dict = field(default_factory=dict)            # component name -> block
    bus_ports: dict = field(default_factory=dict)         # bus -> (out pids, in pids)
    slack_buses: list = field(default_factory=list)
    emission_factors: dict = field(default_factory=dict)  # component -> port -> gas -> array
    gases: list = field(default_factory=list)
    cap_gases: list = field(default_factory=list)
    build_time: float = 0.0
    tie_break: float = 0.0
    cost_exprs: dict = field(default_factory=dict)         # (component, category) -> objective cost expression


SLACK_PREFIX = "bus:"


def _merge_bounds(a, b):
    """Intersect two (lb, ub) pairs of arrays/None."""
    lo = [x for x in (a[0], b[0]) if x is not None]
    hi = [x for x in (a[1], b[1]) if x is not None]
    return (np.maximum.reduce(lo) if lo else None, np.minimum.reduce(hi) if hi else None)


def build_model(system, grid: TimeGrid, resolved, *, tie_break: float = 0.01,
                slack_all: bool = False) -> tuple[pyo.ConcreteModel, BuildMeta]:
    """Build the LP for an already validated ``system`` (``resolved`` comes from ``system.validate``)."""
    t0 = time.perf_counter()
    n = grid.n
    meta = BuildMeta(grid=grid, tie_break=tie_break)
    m = pyo.ConcreteModel(name=system.name)
    m.T = pyo.RangeSet(0, n - 1)

    # ---- ports and bounds
    lb: dict[str, np.ndarray | None] = {}
    ub: dict[str, np.ndarray | None] = {}
    for comp in system.components.values():
        pb = comp.port_bounds()
        gb = resolved.bounds[comp.name]
        for pname, port in comp.ports().items():
            pid = f"{comp.name}.{pname}"
            meta.ports.append(PortInfo(pid, comp.name, pname, port.bus.name, port.direction))
            lo, hi = _merge_bounds(pb.get(pname, (None, None)), gb.get(pname, (None, None)))
            lb[pid], ub[pid] = lo, hi
    pids = [p.pid for p in meta.ports]
    m.P = pyo.Set(initialize=pids, ordered=True)

    def bounds(_m, p, t):
        lo, hi = lb[p], ub[p]
        low = max(float(lo[t]), 0.0) if lo is not None else 0.0
        high = float(hi[t]) if hi is not None and np.isfinite(hi[t]) else None
        return (low, high)

    m.F = pyo.Var(m.P, m.T, within=pyo.NonNegativeReals, bounds=bounds)

    # ---- components
    ctx = BuildContext(m, grid, tie_break)
    flows = {}
    for comp in system.components.values():
        blk = pyo.Block()
        m.add_component(f"c_{safe_name(comp.name)}", blk)
        F = PortFlows(m, comp.name)
        comp.build(blk, F, ctx)
        meta.blocks[comp.name] = blk
        flows[comp.name] = F

    # ---- bus balances
    for bus in system.buses.values():
        meta.bus_ports[bus.name] = (
            [p.pid for p in meta.ports if p.bus == bus.name and p.direction == Direction.OUT],
            [p.pid for p in meta.ports if p.bus == bus.name and p.direction == Direction.IN],
        )
    meta.slack_buses = [b.name for b in system.buses.values() if b.allow_slack or slack_all]
    m.SB = pyo.Set(initialize=meta.slack_buses, ordered=True)
    m.slack_pos = pyo.Var(m.SB, m.T, within=pyo.NonNegativeReals)  # shortfall covered by slack
    m.slack_neg = pyo.Var(m.SB, m.T, within=pyo.NonNegativeReals)  # excess removed by slack
    m.B = pyo.Set(initialize=list(system.buses), ordered=True)
    slack_set = set(meta.slack_buses)

    def balance(_m, b, t):
        outs, ins = meta.bus_ports[b]
        expr = pyo.quicksum(m.F[p, t] for p in outs) - pyo.quicksum(m.F[p, t] for p in ins)
        if b in slack_set:
            expr = expr + m.slack_pos[b, t] - m.slack_neg[b, t]
        return expr == 0

    m.balance = pyo.Constraint(m.B, m.T, rule=balance)

    # ---- costs
    terms: dict[tuple[str, str], list] = {}

    def add(cname, cat, expr):
        if isinstance(expr, (int, float)) and expr == 0:
            return
        terms.setdefault((cname, CostCategory(cat).value), []).append(expr)

    for comp in system.components.values():
        F = flows[comp.name]
        for cat, expr in comp.cost_terms(meta.blocks[comp.name], F, ctx).items():
            add(comp.name, cat, expr)
        for port, (rate, cat) in resolved.flow_costs[comp.name].items():
            add(comp.name, cat, ctx.wsum(rate, F[port]))

    for b in meta.slack_buses:
        c = system.buses[b].slack_cost if b in system.buses else 1e4
        cost = np.full(n, float(c))
        add(SLACK_PREFIX + b, CostCategory.SLACK,
            ctx.wsum(cost, _Index2(m.slack_pos, b)) + ctx.wsum(cost, _Index2(m.slack_neg, b)))

    # ---- emissions
    factors = {c: resolved.emission_factors[c] for c in system.components}
    meta.emission_factors = factors
    gases = sorted({g for fac in factors.values() for pf in fac.values() for g in pf} | set(system.policies))
    meta.gases = gases
    ekeys = []
    eexpr = {}
    for comp in system.components.values():
        F = flows[comp.name]
        for gas in gases:
            parts = [ctx.wsum(f[gas], F[port], physical=True) for port, f in factors[comp.name].items() if gas in f]
            parts = [p for p in parts if not isinstance(p, float)]
            if parts:
                ekeys.append((comp.name, gas))
                eexpr[(comp.name, gas)] = pyo.quicksum(parts)
            policy = system.policies.get(gas)
            price = comp.emission_prices().get(gas)
            if price is None and policy is not None and policy.price is not None:
                price = resolved.policy_prices[gas]
            if parts and price is not None and not (policy is not None and policy.report_only):
                cost = pyo.quicksum(ctx.wsum(price * f[gas], F[port])
                                    for port, f in factors[comp.name].items() if gas in f)
                add(comp.name, CostCategory.EMISSIONS, cost)
    m.EK = pyo.Set(initialize=ekeys, dimen=2, ordered=True)
    m.E = pyo.Var(m.EK, within=pyo.Reals)
    m.E_def = pyo.Constraint(m.EK, rule=lambda _m, c, g: m.E[c, g] == eexpr[(c, g)])
    m.G = pyo.Set(initialize=gases, ordered=True)

    def et(_m, g, t):
        parts = []
        for comp in system.components.values():
            for port, f in factors[comp.name].items():
                if g in f and f[g][t] != 0:
                    parts.append(float(f[g][t] * grid.dt[t]) * m.F[f"{comp.name}.{port}", t])
        return pyo.quicksum(parts) if parts else 0.0

    m.Et = pyo.Expression(m.G, m.T, rule=et)
    # a cap on a gas nothing emits would be the constant constraint 0 <= cap: met anyway, so left out
    meta.cap_gases = [g for g, p in system.policies.items() if p.cap is not None and any(gg == g for _, gg in ekeys)]
    m.CG = pyo.Set(initialize=meta.cap_gases, ordered=True)
    m.emission_cap = pyo.Constraint(
        m.CG, rule=lambda _m, g: pyo.quicksum(m.E[c, gg] for (c, gg) in ekeys if gg == g)
        <= float(system.policies[g].cap))

    # ---- minimum self-sufficiency of a lifetime optimisation: grid import <= (1 - target) * demand (electricity)
    target = getattr(grid.horizon, "self_sufficiency", None)
    if target is not None:
        from .exceptions import ValidationError
        from .sizing import self_sufficiency_ports

        imports, demands = self_sufficiency_ports(system)
        if not demands:
            raise ValidationError("a self-sufficiency target needs a Demand on an electricity bus")

        def energy(cps):
            return pyo.quicksum(ctx.wsum(1.0, flows[c][p], physical=True) for c, p in cps)
        m.self_sufficiency = pyo.Constraint(expr=energy(imports) <= (1.0 - target) * energy(demands))

    # ---- accounting variables and objective
    ckeys = list(terms)
    m.CK = pyo.Set(initialize=ckeys, dimen=2, ordered=True)
    m.C = pyo.Var(m.CK, within=pyo.Reals)
    meta.cost_exprs = {ck: pyo.quicksum(terms[ck]) for ck in ckeys}
    m.C_def = pyo.Constraint(m.CK, rule=lambda _m, c, k: m.C[c, k] == meta.cost_exprs[(c, k)])
    m.obj = pyo.Objective(expr=pyo.quicksum(m.C[k] for k in ckeys) if ckeys else 0.0, sense=pyo.minimize)

    # ---- investment hurdles of a lifetime optimisation (minimum IRR, maximum payback; see sizing.hurdle_expr)
    limits = getattr(grid.horizon, "hurdle_limits", None) or []
    if limits:
        from .sizing import hurdle_expr

        m.H = pyo.RangeSet(0, len(limits) - 1)
        # the bound is the reference case's own value: a hair of slack for the solver's tolerance
        m.hurdle = pyo.Constraint(m.H, rule=lambda _m, i: hurdle_expr(m, meta, np.asarray(limits[i]["weights"]))
                                  <= limits[i]["limit"] + 1e-7 * abs(limits[i]["limit"]) + 1e-6)
    meta.build_time = time.perf_counter() - t0
    return m, meta


class _Index2:
    """View ``var[b, t]`` as ``view[t]``."""

    def __init__(self, var, b):
        self.var, self.b = var, b

    def __getitem__(self, t):
        return self.var[self.b, t]

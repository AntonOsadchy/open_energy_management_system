"""Shared helpers for the test suite.

``assert_invariants`` is applied to every solved validation case. It checks properties that must
hold for *any* correct solution, independent of the specific case:

1. every edge flow is non-negative,
2. every bus balance closes (sum OUT - sum IN + slack = 0) at every step,
3. the reported flows are exactly the values of the model variables ``m.F``,
4. the cost accounting variables add up to the solver objective,
5. storage energy stays within [soc_min, soc_max] * e_max,
6. the emission accounting variables equal sum(factor * flow * dt * w).
"""

from __future__ import annotations

import numpy as np
import pyomo.environ as pyo

import oems

TOL = 1e-6


def grid_h(n, start="2025-01-01", tz=None, **kw):
    return oems.TimeGrid(start=start, periods=n, freq="1h", tz=tz, **kw)


def grid_15(n, start="2025-01-01", tz=None, **kw):
    return oems.TimeGrid(start=start, periods=n, freq="15min", tz=tz, **kw)


def el_system(grid):
    es = oems.EnergySystem(grid)
    el = es.add_bus("el", oems.Carrier("electricity", kind="electricity"))
    return es, el


def heat_el_system(grid):
    es, el = el_system(grid)
    heat = es.add_bus("heat", oems.Carrier("heat", kind="heat"))
    return es, el, heat


def assert_invariants(res: oems.Results, tol: float = TOL) -> None:
    m, meta = res.model, res.meta
    n = res.grid.n
    # 1. non-negative flows
    assert (res.flows.to_numpy() >= -tol).all(), "negative edge flow"
    # 2. bus balances close
    for b in res.system.buses:
        bal = res.balance(b).sum(axis=1).to_numpy()
        assert np.abs(bal).max(initial=0) < tol * 10, f"balance of bus {b} does not close: {bal}"
    # 3. flows equal model variables
    for p in meta.ports:
        col = res.flows[(p.component, p.port, p.bus, p.direction.value)].to_numpy()
        var = np.array([m.F[p.pid, t].value or 0.0 for t in range(n)])
        assert np.allclose(col, var, atol=tol), f"flow table differs from model variable {p.pid}"
    # 4. accounting adds up to the objective
    total = res._cost_rows["EUR"].sum()
    assert abs(total - res.objective) <= tol * max(1.0, abs(res.objective)), (total, res.objective)
    # 5. storage energy within bounds
    for name, comp in res.system.components.items():
        if isinstance(comp, oems.Storage):
            e = res.states[(name, "e")].to_numpy()
            assert e.min() >= comp.r.soc_min * comp.r.e_max - tol
            assert e.max() <= comp.r.soc_max * comp.r.e_max + tol
    # 6. emissions equal factor * flow
    wdt = res.grid.w * res.grid.dt
    for (c, g) in m.EK:
        expected = 0.0
        for port, gases in meta.emission_factors[c].items():
            if g in gases:
                f = np.array([m.F[f"{c}.{port}", t].value or 0.0 for t in range(n)])
                expected += float(np.sum(gases[g] * f * wdt))
        assert abs(pyo.value(m.E[c, g]) - expected) < tol * max(1.0, abs(expected))


def flow(res, component, port):
    """Flow time series (MW) of one port as a numpy array."""
    return res.flows.xs((component, port), level=("component", "port"), axis=1).iloc[:, 0].to_numpy()


def state(res, component, name):
    return res.states[(component, name)].to_numpy()

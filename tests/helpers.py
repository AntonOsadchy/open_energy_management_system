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


FAKE_CURVES = {1.0: [0.95, 0.9, 0.8], 2.0: [0.9, 0.8]}


def fake_bess_db(db, series=None, tariffs=None):
    """Make a ``BessDb`` answer its fixed queries from in-memory data instead of Postgres.

    ``series``: {(zone, series_type_code): pd.Series with UTC index}; ``tariffs``: {(zone, connection_type, column): …}.
    Default data: DK_1 day-ahead price that is hourly until 2025-01-01 12:00 UTC and 15-min afterwards (as after the
    market moved to 15-min products), 2m temperature in K, an N1 A-high consumption tariff of 40 €/MWh, and a
    modelled 2 MWp PV system ``SITE`` producing 0.8 MW from 10:00 to 14:00 UTC each day (capacity factor 0.4), and
    the degradation profile ``short`` (``FAKE_CURVES``: 3 years at 1 cycle/day, 2 at 2 cycles/day, ending at 0.8).
    """
    import pandas as pd

    if series is None:
        h = pd.date_range("2024-12-20", "2025-01-01 11:00", freq="1h", tz="UTC")
        q = pd.date_range("2025-01-01 12:00", "2025-01-15", freq="15min", tz="UTC", inclusive="left")
        price = pd.concat([pd.Series(50.0 + 10 * (h.hour >= 12), index=h),
                           pd.Series(50.0 + 10 * (q.hour >= 12) + 4 * (q.minute // 15), index=q)])
        temp_idx = pd.date_range("2024-12-20", "2025-01-15", freq="1h", tz="UTC", inclusive="left")
        series = {("DK_1", "da_price"): price, ("DK_1", "temperature_2m_k"): pd.Series(278.15, index=temp_idx)}
    if tariffs is None:
        idx = pd.date_range("2024-12-20", "2025-01-15", freq="1h", tz="UTC", inclusive="left")
        tariffs = {("DK_1", "n1_a_high", "consumption_tariff_eur_mwh"): pd.Series(40.0, index=idx)}
    empty = pd.Series(dtype=float, index=pd.DatetimeIndex([], tz="UTC"))
    pv_idx = pd.date_range("2024-12-20", "2025-01-15", freq="1h", tz="UTC", inclusive="left")
    pv = pd.Series(np.where((pv_idx.hour >= 10) & (pv_idx.hour < 14), 0.8, 0.0), index=pv_idx)

    def rows(sql, params=(), database=None):
        if sql == "select 1":
            return [(1,)]
        if "from pg_database" in sql:
            return [("postgres",), ("staging",)]
        if "from bidding_zones" in sql:
            return [("DK_1", "Denmark West", "Europe/Copenhagen"), ("DE_LU", "Germany-Luxembourg", "Europe/Berlin")]
        if "from series_types" in sql:
            return [("gas_ttf_front_month", "commodity", "EUR/MWh", "Gas: TTF front month"),
                    ("gen_solar", "generation", "MW", "Generation: Solar"),
                    ("da_price", "price", "EUR/MWh", "Day-ahead market price"),
                    ("temperature_2m_k", "weather", "K", "Temperature: 2m air temperature")]
        if "from tariff_dso_connection_types" in sql:
            return [("no_tariffs", "No tariffs"), ("n1_a_high", "N1 A-high")]
        if "from bess_degradation_curves c" in sql:
            return [("short", "Short test profile", [1.0, 2.0], 3)]
        if "from bess_degradation_curves where" in sql:
            return ([(r, y + 1, v) for r, vals in FAKE_CURVES.items() for y, v in enumerate(vals)]
                    if params == ("short",) else [])
        if "from solar_pv_scenarios" in sql:
            return [("SITE", "DK_1", "Site roof", 2.0, 25.0, 180.0, None, True, 55.8, 11.4, "test system")]
        if "'gen_solar_modeled'" in sql:
            cap, zone, source, lo, hi = params
            s = (pv if (zone, source) == ("DK_1", "SITE") else empty) / cap
        elif "from market_timeseries" in sql:
            zone, code, lo, hi = params
            s = series.get((zone, code), empty)
        elif "from tariffs" in sql:
            zone, ct, lo, hi = params
            col = sql.split("select ts, ")[1].split(" ")[0]
            s = tariffs.get((zone, ct, col), empty)
        else:
            raise AssertionError(f"unexpected query {sql}")
        s = s[(s.index >= pd.Timestamp(lo)) & (s.index < pd.Timestamp(hi))]
        return list(zip(s.index.to_pydatetime(), s.tolist(), strict=True))

    db._rows = rows
    db._options = None
    return db


def use_fake_bess_db(app, **kw):
    """Route every DB connection of a UI app to :func:`fake_bess_db`."""
    from oems.ui.dbsource import BessDb

    app.state.db_factory = lambda conn: fake_bess_db(BessDb(conn, "pw"), **kw)

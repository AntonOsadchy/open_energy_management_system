"""Validation: 15-min vs hourly equivalence, weights, explicit flows and diagnostics."""

import numpy as np
import pandas as pd
import pytest
from helpers import assert_invariants, el_system, flow, grid_h

import oems
from oems import presets as P


def _site(grid, prices: pd.Series, demand: pd.Series, loss_per_h=0.01):
    es = oems.EnergySystem(grid)
    el = es.add_bus("el", oems.Carrier("electricity", kind="electricity"))
    heat = es.add_bus("heat", oems.Carrier("heat", kind="heat"))
    es.add(oems.GridConnection("dso", el, import_price=prices, export_price=prices, export_fee=5, max_export=2),
           P.battery("bess", el, e_max=2, p_max=1, eta_rt=0.9),
           P.heat_pump("hp", el, heat, cop=3, p_th_max=1.5),
           P.hot_water_tank("tes", heat, e_max=4, p_max=2, loss_per_h=loss_per_h),
           oems.Demand("hd", heat, profile=demand), oems.Demand("ed", el, profile=0.5))
    return es


def _hourly_inputs(n_hours):
    idx = pd.date_range("2025-01-01", periods=n_hours, freq="1h")
    rng = np.random.default_rng(3)
    return pd.Series(rng.uniform(20, 150, n_hours), idx), pd.Series(rng.uniform(0.2, 1.2, n_hours), idx)


def test_same_system_same_cost_at_15min_and_hourly():
    """With hourly-constant inputs and no standing losses, a 15-min run has exactly the same optimum
    as the hourly run: every hourly schedule is feasible at 15 min, and the 15-min optimum averaged
    per hour is feasible hourly (linear dynamics, per-hour parameters). Objectives agree to 1e-6."""
    prices, demand = _hourly_inputs(24)
    r60 = _site(oems.TimeGrid(start="2025-01-01", periods=24, freq="1h"), prices, demand, 0.0).solve(tie_break=0)
    r15 = _site(oems.TimeGrid(start="2025-01-01", periods=96, freq="15min"), prices, demand, 0.0).solve(tie_break=0)
    assert_invariants(r60)
    assert_invariants(r15)
    assert r15.objective == pytest.approx(r60.objective, rel=1e-6, abs=1e-6)
    # energy totals per edge agree after aggregation
    e15 = r15.resample("1h").energy.sum()
    e60 = r60.energy().sum()
    assert e15[("hd", "in")].sum() == pytest.approx(e60[("hd", "in")].sum())


def test_resolutions_agree_closely_with_standing_losses():
    """With self-discharge the resolutions differ slightly: energy charged early in an hour already
    decays during that hour at 15-min resolution, but not in the hourly model. The difference is a
    second-order effect (here < 1e-4 relative)."""
    prices, demand = _hourly_inputs(24)
    r60 = _site(oems.TimeGrid(start="2025-01-01", periods=24, freq="1h"), prices, demand).solve(tie_break=0)
    r15 = _site(oems.TimeGrid(start="2025-01-01", periods=96, freq="15min"), prices, demand).solve(tie_break=0)
    assert r15.objective == pytest.approx(r60.objective, rel=1e-4)


def test_15min_price_variation_never_costs_more():
    """Inside-the-hour price variation gives extra arbitrage options, so the 15-min optimum is at
    most the hourly optimum (computed with hourly mean prices)."""
    idx15 = pd.date_range("2025-01-01", periods=96, freq="15min")
    p15 = pd.Series(np.tile([10.0, 150.0, 10.0, 150.0], 24), idx15)
    _, demand = _hourly_inputs(24)
    r15 = _site(oems.TimeGrid(idx15), p15, demand).solve()
    r60 = _site(oems.TimeGrid(start="2025-01-01", periods=24, freq="1h"), p15, demand).solve()  # mean-aligned
    assert r15.total_cost <= r60.total_cost + 1e-6


def test_resampled_energy_is_conserved():
    """Aggregating 15-min results to hourly keeps the total energy on every edge."""
    prices, demand = _hourly_inputs(6)
    r15 = _site(oems.TimeGrid(start="2025-01-01", periods=24, freq="15min"), prices, demand).solve()
    agg = r15.resample("1h")
    assert np.isclose(agg.energy.to_numpy().sum(), r15.energy().to_numpy().sum())


def test_weights_scale_costs_not_physics():
    """Doubling all weights doubles the objective and leaves the dispatch unchanged."""
    def run(w):
        es, el = el_system(grid_h(4, weights=w))
        es.add(oems.GridConnection("dso", el, import_price=[10, 50, 10, 50]), oems.Demand("d", el, profile=1),
               P.battery("b", el, e_max=1, p_max=1, eta_rt=0.9))
        return es.solve()
    r1, r2 = run(1.0), run(2.0)
    assert r2.objective == pytest.approx(2 * r1.objective)
    assert np.allclose(flow(r1, "b", "charge"), flow(r2, "b", "charge"))


def test_flows_are_model_variables_and_costs_add_up():
    """res.flows equals value(m.F) for every edge and the accounting variables sum to the objective."""
    prices, demand = _hourly_inputs(12)
    res = _site(oems.TimeGrid(start="2025-01-01", periods=12, freq="1h"), prices, demand).solve()
    assert_invariants(res)
    assert len(res.flows.columns) == len(res.meta.ports)
    fm = res.flow_matrix("heat")
    assert set(fm["component"]) == {"hp", "tes", "hd"}


def test_infeasible_model_reports_where():
    """Demand 2 MW but grid limited to 1 MW: infeasible. The diagnosis re-solves with slack and reports
    a 1 MWh shortfall on bus 'el' in each of the 3 hours."""
    es, el = el_system(grid_h(3))
    es.add(oems.GridConnection("dso", el, import_price=10, max_import=1), oems.Demand("d", el, profile=2))
    with pytest.raises(oems.InfeasibleModelError) as exc:
        es.solve()
    rep = exc.value.report
    assert list(rep["bus"].unique()) == ["el"]
    assert rep["shortfall_MWh"].sum() == pytest.approx(3.0)


def test_unbounded_model_raises():
    """A sink with negative cost and no limit fed by an unlimited free grid is unbounded."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("dso", el, import_price=0), oems.Sink("dump", el, cost=-1),
           oems.Demand("d", el, profile=0))
    with pytest.raises(oems.UnboundedModelError):
        es.solve()


def test_bus_slack_keeps_model_feasible():
    """With allow_slack the same shortfall is priced at slack_cost and visible in res.slack."""
    g = grid_h(2)
    es = oems.EnergySystem(g)
    el = es.add_bus("el", oems.Carrier("electricity", kind="electricity"), allow_slack=True, slack_cost=1000)
    es.add(oems.GridConnection("dso", el, import_price=10, max_import=1), oems.Demand("d", el, profile=2))
    res = es.solve()
    assert res.slack["shortfall_MWh"].sum() == pytest.approx(2.0)
    assert res.total_cost == pytest.approx(20.0)  # slack cost excluded


def test_results_export(tmp_path):
    """to_csv writes all result tables and metadata; the heat-electrification KPI is 1 for an all-electric heat supply."""
    prices, demand = _hourly_inputs(4)
    res = _site(oems.TimeGrid(start="2025-01-01", periods=4, freq="1h"), prices, demand).solve()
    out = res.to_csv(tmp_path / "run")
    for f in ("flows.csv", "states.csv", "costs.csv", "shadow_prices.csv", "kpis.csv", "metadata.json"):
        assert (out / f).exists()
    kpis = res.kpis().set_index("kpi")
    assert kpis.loc["heat_electrification_share", "value"] == pytest.approx(1.0)

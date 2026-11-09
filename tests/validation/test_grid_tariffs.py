"""Validation: grid connection and tariffs against hand-calculated costs."""

import numpy as np
import pandas as pd
import pytest
from helpers import assert_invariants, el_system, flow, grid_15, grid_h

import oems


def test_grid_only_cost():
    """0.1 MW for 4 h at 200 EUR/MWh = 0.4 MWh * 200 = 80 EUR, all in category ENERGY."""
    es, el = el_system(grid_h(4))
    es.add(oems.GridConnection("dso", el, import_price=200), oems.Demand("load", el, profile=0.1))
    res = es.solve()
    assert_invariants(res)
    assert res.objective == pytest.approx(80.0)
    assert res.cost_by_category["ENERGY"] == pytest.approx(80.0)


def test_fee_is_reported_as_network_cost():
    """Import fee 50 EUR/MWh on 0.4 MWh = 20 EUR in NETWORK; price part 80 EUR in ENERGY."""
    es, el = el_system(grid_h(4))
    es.add(oems.GridConnection("dso", el, import_price=200, import_fee=50), oems.Demand("load", el, profile=0.1))
    res = es.solve()
    assert res.cost_by_category["NETWORK"] == pytest.approx(20.0)
    assert res.total_cost == pytest.approx(100.0)


@pytest.mark.parametrize("fee, exported", [(40.0, 0.0), (10.0, 4.0)])
def test_export_only_when_credit_exceeds_fee(fee, exported):
    """1 MW of curtailable PV, no load, export-only grid (max_import=0), export price 30 EUR/MWh.

    fee 40 > price 30: exporting loses 10 EUR/MWh -> PV is curtailed, nothing exported.
    fee 10 < price 30: export all 4 MWh, objective = -4 * (30 - 10) = -80 EUR.
    """
    es, el = el_system(grid_h(4))
    es.add(oems.GridConnection("dso", el, export_price=30, export_fee=fee, max_export=5, max_import=0),
           oems.Producer("pv", el, profile=1.0))
    res = es.solve(tie_break=0)
    assert_invariants(res)
    assert flow(res, "dso", "export").sum() == pytest.approx(exported)
    assert res.objective == pytest.approx(-exported * (30 - fee) if exported else 0.0)


def test_peak_charge_without_flexibility():
    """Fixed demand [1, 3, 1, 1] MW, peak charge 1000 EUR/MW: billed peak 3 MW -> 3000 EUR."""
    es, el = el_system(grid_h(4))
    es.add(oems.GridConnection("dso", el, peak_charge=1000), oems.Demand("load", el, profile=[1, 3, 1, 1]))
    res = es.solve()
    assert res.cost_by_category["PEAK"] == pytest.approx(3000.0)
    assert res.periodic["value"].max() == pytest.approx(3.0)


def test_peak_shaving_with_storage():
    """Demand [1, 1, 1, 3] MW (6 MWh). A lossless 3 MWh / 2 MW battery that starts with 1 MWh and must
    end with >= 1 MWh can flatten imports to 6 MWh / 4 h = 1.5 MW (it charges 0.5 MWh in each of the
    first three hours up to 2.5 MWh, then discharges 1.5 MW). Peak cost = 1.5 * 1000 EUR.
    (With only 2 MWh of capacity the best achievable peak would be 2 MW.)"""
    es, el = el_system(grid_h(4))
    es.add(oems.GridConnection("dso", el, peak_charge=1000), oems.Demand("load", el, profile=[1, 1, 1, 3]),
           oems.Storage("bess", el, e_max=3, p_ch_max=2, p_dis_max=2, eta_ch=1, eta_dis=1, soc_init=1 / 3))
    res = es.solve(tie_break=0)
    assert_invariants(res)
    assert res.objective == pytest.approx(1500.0)
    assert flow(res, "dso", "import").max() == pytest.approx(1.5)


def test_peak_measurement_hourly_mean_on_15min_grid():
    """15-min demand [4, 0, 0, 0, 1, 1, 1, 1] MW. Per-step metering bills 4 MW; hourly metering bills
    the highest hourly mean = max(1, 1) = 1 MW."""
    demand = [4, 0, 0, 0, 1, 1, 1, 1]
    costs = {}
    for measurement in ("step", "1h"):
        es, el = el_system(grid_15(8))
        es.add(oems.GridConnection("dso", el, peak_charge=100, peak_measurement=measurement),
               oems.Demand("load", el, profile=demand))
        costs[measurement] = es.solve().cost_by_category["PEAK"]
    assert costs["step"] == pytest.approx(400.0)
    assert costs["1h"] == pytest.approx(100.0)


def test_peak_window_only_counts_window_steps():
    """Demand [5, 1, 2, 1] MW, peak window only steps 1-3: billed peak = 2 MW (the 5 MW is outside)."""
    es, el = el_system(grid_h(4))
    window = pd.Series([0, 1, 1, 1], index=es.grid.index)
    es.add(oems.GridConnection("dso", el, tariffs=[oems.PeakCharge(100, window=window)]),
           oems.Demand("load", el, profile=[5, 1, 2, 1]))
    assert es.solve().cost_by_category["PEAK"] == pytest.approx(200.0)


def test_stacked_daily_and_annual_peaks():
    """48 h, demand peak 2 MW on day 1 and 3 MW on day 2.
    Daily charge 10 EUR/MW: 10 * (2 + 3) = 50; annual charge 5 EUR/MW: 5 * 3 = 15; total 65 EUR."""
    d = np.ones(48)
    d[5], d[30] = 2.0, 3.0
    es, el = el_system(grid_h(48))
    es.add(oems.GridConnection("dso", el, tariffs=[oems.PeakCharge(10, period="D"), oems.PeakCharge(5, period="Y")]),
           oems.Demand("load", el, profile=d))
    assert es.solve().cost_by_category["PEAK"] == pytest.approx(65.0)


def test_hard_capacity_limit_uses_backup_supply():
    """Demand 2 MW for 1 h. Cheap grid (100 EUR/MWh) limited to 1.5 MW, expensive backup (1000 EUR/MWh).
    Cost = 1.5 * 100 + 0.5 * 1000 = 650 EUR."""
    es, el = el_system(grid_h(1))
    es.add(oems.GridConnection("cheap", el, import_price=100, tariffs=[oems.CapacityLimit(1.5)]),
           oems.GridConnection("backup", el, import_price=1000),
           oems.Demand("load", el, profile=2))
    res = es.solve()
    assert_invariants(res)
    assert res.objective == pytest.approx(650.0)


def test_soft_capacity_limit_overrun_penalty():
    """Demand 2 MW for 1 h, limit 1.5 MW with overrun penalty 100 EUR/MWh: overrun 0.5 MWh -> 50 EUR."""
    es, el = el_system(grid_h(1))
    es.add(oems.GridConnection("dso", el, tariffs=[oems.CapacityLimit(1.5, penalty=100)]),
           oems.Demand("load", el, profile=2))
    assert es.solve().cost_by_category["PEAK"] == pytest.approx(50.0)


def test_fixed_charge_counts_periods():
    """12 EUR per day over 48 h = 24 EUR in FIXED; dispatch unaffected."""
    es, el = el_system(grid_h(48))
    es.add(oems.GridConnection("dso", el, import_price=10, tariffs=[oems.FixedCharge(12, period="D")]),
           oems.Demand("load", el, profile=1))
    res = es.solve()
    assert res.cost_by_category["FIXED"] == pytest.approx(24.0)
    assert res.cost_by_category["ENERGY"] == pytest.approx(480.0)


def test_exclusive_limits_simultaneous_import_export():
    """Export credit 100 > import price 50 creates an arbitrage loop (import and export together).
    Without `exclusive` both flows hit their 1 MW limits. With `exclusive` the relaxation
    F_imp/1 + F_exp/1 <= 1 holds - it halves but (being an LP relaxation) does not remove the loop."""
    kw = dict(import_price=50, export_price=100, max_import=1, max_export=1)
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("dso", el, **kw), oems.Demand("load", el, profile=0))
    r1 = es.solve(tie_break=0)
    assert flow(r1, "dso", "import").max() == pytest.approx(1.0)
    es2, el2 = el_system(grid_h(2))
    es2.add(oems.GridConnection("dso", el2, exclusive=True, **kw), oems.Demand("load", el2, profile=0))
    r2 = es2.solve(tie_break=0)
    s = flow(r2, "dso", "import") + flow(r2, "dso", "export")
    assert np.all(s <= 1.0 + 1e-9)


def test_no_simultaneous_import_and_export_at_equal_prices():
    """With import and export at the same price, importing and exporting the same MW is cost-neutral; the default
    tie-break removes this degenerate optimum. A 2 MWh battery alone behind the grid only imports to charge and only
    exports to discharge (arbitrage 10 -> 50 EUR/MWh)."""
    es, el = el_system(grid_h(4))
    price = [10, 10, 50, 50]
    es.add(oems.GridConnection("dso", el, import_price=price, export_price=price, max_import=2, max_export=2),
           oems.Storage("bess", el, e_max=2, p_ch_max=1, p_dis_max=1, eta_ch=1, eta_dis=1, soc_init=0, end="free"))
    res = es.solve()
    imp, exp = flow(res, "dso", "import"), flow(res, "dso", "export")
    assert np.allclose(np.minimum(imp, exp), 0, atol=1e-9)
    assert res.total_cost == pytest.approx(10 * 2 - 50 * 2)


def test_grid_has_no_power_limits_by_default():
    """max_import and max_export default to no limit: a 3 MW PV surplus is exported in full (at 20 EUR/MWh)."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("dso", el, import_price=50, export_price=20),
           oems.Producer("pv", el, profile=[3, 0], curtailable=True), oems.Demand("load", el, profile=1))
    res = es.solve()
    assert flow(res, "dso", "export")[0] == pytest.approx(2.0) and flow(res, "dso", "import")[1] == pytest.approx(1.0)
    assert res.total_cost == pytest.approx(-2 * 20 + 1 * 50)


def test_unlimited_grid_with_export_above_import_cost_is_rejected():
    """Unlimited import and export with an export price above the import cost would let the model buy and sell
    without bound: validation stops it with a clear message (limits make it a normal, bounded case)."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("dso", el, import_price=10, export_price=[5, 30]), oems.Demand("load", el, profile=1))
    with pytest.raises(oems.ValidationError, match="earn without bound"):
        es.validate()
    es2, el2 = el_system(grid_h(2))
    es2.add(oems.GridConnection("dso", el2, import_price=10, export_price=[5, 30], max_export=1),
            oems.Demand("load", el2, profile=1))
    assert es2.validate(raise_on_error=False).ok


def test_storage_with_nothing_to_discharge_into_is_warned():
    """A battery alone behind a grid without an export price can only charge: validation says why it stays idle;
    an export price (or a demand on the bus) removes the warning."""
    def warnings(**grid_kw):
        es, el = el_system(grid_h(2))
        es.add(oems.GridConnection("dso", el, import_price=10, **grid_kw),
               oems.Storage("bess", el, e_max=1, p_ch_max=1, p_dis_max=1))
        return es.validate(raise_on_error=False).warnings
    assert any("nothing to discharge into" in w for w in warnings())
    assert not any("nothing to discharge into" in w for w in warnings(export_price=8))


@pytest.mark.parametrize("curtailable, cost", [(True, 10.0), (False, 15.0)])
def test_curtailment_can_be_switched_off(curtailable, cost):
    """PV [2, 0] MW, 1 MW load, import 10 EUR/MWh, export pays nothing but costs a 5 EUR/MWh fee. Allowed to
    curtail, the 1 MWh PV surplus is curtailed: cost = 10 (second hour). Not allowed, the surplus must be
    exported: cost = 10 + 5 = 15 EUR, and the producer has no curtailment state."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("dso", el, import_price=10, export_price=0, export_fee=5, max_export=2),
           oems.Producer("pv", el, profile=[2, 0], curtailable=curtailable), oems.Demand("load", el, profile=1))
    res = es.solve(tie_break=0)
    assert_invariants(res)
    assert res.total_cost == pytest.approx(cost)
    assert flow(res, "pv", "out")[0] == pytest.approx(1.0 if curtailable else 2.0)
    assert (("pv", "curtailment") in res.states.columns) is curtailable

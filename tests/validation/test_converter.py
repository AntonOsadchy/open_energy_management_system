"""Validation: converters (heat pump, e-boiler, CHP, links) against energy balances."""

import numpy as np
import pytest
from helpers import assert_invariants, flow, grid_h, heat_el_system

import oems
from oems import presets as P


def test_heat_pump_cop_balance():
    """COP 3: 0.3 MW heat needs 0.1 MW el. 4 h at 100 EUR/MWh -> 0.4 MWh el -> 40 EUR."""
    es, el, heat = heat_el_system(grid_h(4))
    es.add(oems.GridConnection("dso", el, import_price=100), P.heat_pump("hp", el, heat, cop=3, p_th_max=1),
           oems.Demand("hd", heat, profile=0.3))
    res = es.solve(tie_break=0)
    assert_invariants(res)
    assert np.allclose(flow(res, "hp", "in_el"), 0.1)
    assert res.objective == pytest.approx(40.0)


def test_relative_coefficients():
    """inputs {el: 2}, outputs {heat: 6} is the same machine as COP 3 (coefficients are relative)."""
    es, el, heat = heat_el_system(grid_h(2))
    es.add(oems.GridConnection("dso", el, import_price=100), oems.Converter("hp", {el: 2}, {heat: 6}),
           oems.Demand("hd", heat, profile=0.3))
    assert np.allclose(flow(es.solve(), "hp", "in_el"), 0.1)


def test_time_varying_cop_with_storage_shifts_production():
    """COP [4, 2], heat demand [0, 1] MWh, lossless heat storage. Producing in hour 0 (COP 4) and
    storing costs 0.25 MWh el instead of 0.5 MWh in hour 1. At 100 EUR/MWh -> 25 EUR."""
    es, el, heat = heat_el_system(grid_h(2))
    es.add(oems.GridConnection("dso", el, import_price=100), P.heat_pump("hp", el, heat, cop=[4, 2], p_th_max=2),
           P.hot_water_tank("tes", heat, e_max=2, p_max=2, soc_init=0), oems.Demand("hd", heat, profile=[0, 1]))
    res = es.solve(tie_break=0)
    assert_invariants(res)
    assert res.objective == pytest.approx(25.0)
    assert flow(res, "hp", "out_heat")[0] == pytest.approx(1.0)


def test_capacity_on_output_port():
    """HP limited to 0.5 MW heat (COP 3) plus e-boiler (eta 1) for 0.8 MW heat demand:
    HP 0.5 MW heat (0.1667 MW el), e-boiler 0.3 MW. Total el = 0.4667 MW."""
    es, el, heat = heat_el_system(grid_h(1))
    es.add(oems.GridConnection("dso", el, import_price=100), P.heat_pump("hp", el, heat, cop=3, p_th_max=0.5),
           P.e_boiler("eb", el, heat, p_max=5, eta=1.0), oems.Demand("hd", heat, profile=0.8))
    res = es.solve(tie_break=0)
    assert flow(res, "hp", "out_heat")[0] == pytest.approx(0.5)
    assert flow(res, "dso", "import")[0] == pytest.approx(0.5 / 3 + 0.3)


def test_ramp_limit():
    """Link el->heat with ramp 0.5 MW/h, heat demand [0, 1, 1]; an expensive heat source covers the
    rest. The link can reach only 0.5 MW in hour 1, so the backup supplies 0.5 MWh (1000 EUR/MWh)."""
    es, el, heat = heat_el_system(grid_h(3))
    es.add(oems.GridConnection("dso", el, import_price=10),
           oems.GridConnection("backup", heat, import_price=1000, max_export=0),   # import-only: no heat dumping
           P.link("eb", el, heat, ramp=0.5), oems.Demand("hd", heat, profile=[0, 1, 1]))
    res = es.solve(tie_break=0)
    assert np.allclose(flow(res, "eb", "in_el"), [0, 0.5, 1.0])
    assert flow(res, "backup", "import").sum() == pytest.approx(0.5)


def test_chp_fixed_ratios():
    """CHP: 1 MWh gas -> 0.4 MWh el + 0.5 MWh heat. All three edges keep these ratios."""
    g = grid_h(3)
    es = oems.EnergySystem(g)
    el = es.add_bus("el", oems.Carrier("electricity", kind="electricity"))
    heat = es.add_bus("heat", oems.Carrier("heat", kind="heat"))
    gas = es.add_bus("gas", oems.Carrier("gas", kind="fuel"))
    es.add(oems.GridConnection("gas_grid", gas, import_price=30),
           oems.GridConnection("dso", el, import_price=200, export_price=50, max_export=10),
           P.chp("chp", gas, el, heat, eta_el=0.4, eta_th=0.5, p_fuel_max=2),
           oems.Demand("hd", heat, profile=0.5), oems.Demand("ed", el, profile=1))
    res = es.solve()
    assert_invariants(res)
    fuel = flow(res, "chp", "in_gas")
    assert np.allclose(flow(res, "chp", "out_el"), 0.4 * fuel)
    assert np.allclose(flow(res, "chp", "out_heat"), 0.5 * fuel)


def test_heat_cascade_link():
    """Heat at 45 degC demand served from the 90 degC bus via an explicit link (eta 1)."""
    es, el, h90 = heat_el_system(grid_h(2))
    h45 = es.add_bus("heat_45", oems.Carrier("heat45", kind="heat"))
    es.add(oems.GridConnection("dso", el, import_price=100), P.e_boiler("eb", el, h90, p_max=5, eta=1.0),
           P.link("cascade", h90, h45), oems.Demand("d45", h45, profile=0.7))
    res = es.solve()
    assert np.allclose(flow(res, "cascade", "out_heat_45"), 0.7)
    assert np.allclose(flow(res, "eb", "in_el"), 0.7)


def test_min_load_is_must_run():
    """p_min = 0.3 MW forces the converter on even with no useful demand (excess heat to a sink)."""
    es, el, heat = heat_el_system(grid_h(2))
    es.add(oems.GridConnection("dso", el, import_price=100),
           oems.Converter("eb", {el: 1}, {heat: 1}, p_min=0.3, p_max=1),
           oems.Sink("cooler", heat), oems.Demand("hd", heat, profile=0))
    res = es.solve(tie_break=0)
    assert np.allclose(flow(res, "eb", "in_el"), 0.3)

"""Validation: emissions, CO2 pricing/caps and shadow (marginal) prices."""

import numpy as np
import pytest
from helpers import assert_invariants, el_system, flow, grid_h, heat_el_system

import oems
from oems import presets as P

C_EL, C_GAS, COP, ETA = 120.0, 30.0, 3.0, 0.9
EPS_EL, EPS_GAS = 0.3, 0.2


def _hp_vs_boiler(co2_price=None, cap=None, report_only=False, n=2):
    g = grid_h(n)
    es = oems.EnergySystem(g)
    el = es.add_bus("el", oems.Carrier("electricity", kind="electricity", emission_factor={"co2": EPS_EL}))
    gas = es.add_bus("gas", oems.Carrier("gas", kind="fuel", emission_factor={"co2": EPS_GAS}))
    heat = es.add_bus("heat", oems.Carrier("heat", kind="heat"))
    es.add(oems.GridConnection("dso", el, import_price=C_EL), oems.GridConnection("gas_grid", gas, import_price=C_GAS),
           P.heat_pump("hp", el, heat, cop=COP, p_th_max=5), P.gas_boiler("gb", gas, heat, eta=ETA, p_th_max=5),
           oems.Demand("hd", heat, profile=1.0))
    if co2_price is not None or cap is not None or report_only:
        es.set_emission_policy("co2", price=co2_price, cap=cap, report_only=report_only)
    return es


#: break-even CO2 price where both technologies cost the same per MWh of heat
PI_STAR = (C_EL / COP - C_GAS / ETA) / (EPS_GAS / ETA - EPS_EL / COP)


@pytest.mark.parametrize("delta, winner", [(-5.0, "gb"), (5.0, "hp")])
def test_co2_breakeven_price(delta, winner):
    """Heat cost per MWh: HP = c_el/COP + pi*eps_el/COP, boiler = c_gas/eta + pi*eps_gas/eta.
    They are equal at pi* = (c_el/COP - c_gas/eta) / (eps_gas/eta - eps_el/COP) = 54.5 EUR/t.
    Below pi* the boiler is used, above pi* the heat pump."""
    res = _hp_vs_boiler(co2_price=PI_STAR + delta).solve(tie_break=0)
    assert_invariants(res)
    other = "hp" if winner == "gb" else "gb"
    assert flow(res, winner, "out_heat").sum() == pytest.approx(2.0)
    assert flow(res, other, "out_heat").sum() == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("delta, winner", [(-5.0, "gb"), (5.0, "hp")])
def test_grid_co2_price_matches_system_policy(delta, winner):
    """A co2_price on each grid connection prices emissions like the system policy: same break-even pi*,
    same objective; a grid price replaces (does not add to) the policy price."""
    pi = PI_STAR + delta
    es = _hp_vs_boiler()
    for name in ("dso", "gas_grid"):
        es.components[name].co2_price = pi
    res = es.solve(tie_break=0)
    assert_invariants(res)
    assert flow(res, winner, "out_heat").sum() == pytest.approx(2.0)
    assert res.objective == pytest.approx(_hp_vs_boiler(co2_price=pi).solve(tie_break=0).objective)
    es.set_emission_policy("co2", price=1000)
    assert es.solve(tie_break=0).objective == pytest.approx(res.objective)


def test_emissions_are_counted_at_the_boundary():
    """Without a policy the boiler is cheaper; 2 MWh heat / 0.9 = 2.222 MWh gas * 0.2 t/MWh = 0.444 t."""
    res = _hp_vs_boiler(report_only=True).solve(tie_break=0)
    assert res.emissions.loc["gas_grid", "co2"] == pytest.approx(2 / ETA * EPS_GAS)
    assert "EMISSIONS" not in res.cost_by_category  # report_only: not priced


def test_report_only_does_not_change_objective():
    """An emission policy with report_only=True leaves the optimum unchanged (emissions are only reported)."""
    a = _hp_vs_boiler().solve(tie_break=0).objective
    b = _hp_vs_boiler(co2_price=100, report_only=True).solve(tie_break=0).objective
    assert a == pytest.approx(b)


def test_emission_cap_dual_matches_finite_difference():
    """The dual of the CO2 cap equals the marginal abatement cost: (obj(cap) - obj(cap + d)) / d."""
    cap, d = 0.35, 0.01
    r1 = _hp_vs_boiler(cap=cap).solve(tie_break=0)
    r2 = _hp_vs_boiler(cap=cap + d).solve(tie_break=0)
    fd = (r1.objective - r2.objective) / d
    assert r1.emission_shadow_price["co2"] == pytest.approx(fd, rel=1e-4)
    assert r1.emissions["co2"].sum() == pytest.approx(cap)


def test_shadow_price_equals_import_cost():
    """On a bus fed only by the grid, the marginal price is import price + fee in every step."""
    es, el = el_system(grid_h(3))
    es.add(oems.GridConnection("dso", el, import_price=[10, 20, 30], import_fee=5), oems.Demand("d", el, profile=1))
    res = es.solve()
    assert np.allclose(res.shadow_prices["el"], [15, 25, 35])


def test_shadow_price_of_heat_from_eboiler():
    """Heat from an e-boiler with eta 0.9 at 90 EUR/MWh el costs 100 EUR/MWh heat (+ tiny tie-break)."""
    es, el, heat = heat_el_system(grid_h(2))
    es.add(oems.GridConnection("dso", el, import_price=90), P.e_boiler("eb", el, heat, p_max=5, eta=0.9),
           oems.Demand("hd", heat, profile=1))
    res = es.solve(tie_break=0)
    assert np.allclose(res.shadow_prices["heat"], 100.0)


def test_shadow_prices_scale_with_resolution():
    """Shadow prices are per MWh, so they are the same on 15-min and hourly grids."""
    for grid in (grid_h(2), oems.TimeGrid(start="2025-01-01", periods=8, freq="15min")):
        es, el = el_system(grid)
        es.add(oems.GridConnection("dso", el, import_price=42), oems.Demand("d", el, profile=1))
        assert np.allclose(es.solve().shadow_prices["el"], 42.0)


def test_flow_cost_refund_category():
    """A negative flow cost (tax refund) on the HP input appears as negative TAX."""
    es, el, heat = heat_el_system(grid_h(1))
    es.add(oems.GridConnection("dso", el, import_price=100),
           P.heat_pump("hp", el, heat, cop=2, p_th_max=5, flow_costs={"in_el": oems.FlowCost(-10, "TAX")}),
           oems.Demand("hd", heat, profile=1))
    res = es.solve(tie_break=0)
    assert res.cost_by_category["TAX"] == pytest.approx(-5.0)


def test_a_cap_on_a_gas_nothing_emits_is_met_and_ignored():
    """A CO2 cap on a site without any CO2 factor (0 t <= cap) solves as without the cap, with no cap dual."""
    es, el = el_system(grid_h(2))
    es.add(oems.GridConnection("dso", el, import_price=50), oems.Demand("load", el, profile=1.0))
    es.set_emission_policy("co2", cap=10)
    res = es.solve()
    assert res.total_cost == pytest.approx(100) and res.emission_shadow_price == {}

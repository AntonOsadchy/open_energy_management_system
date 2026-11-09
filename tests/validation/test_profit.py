"""Validation: system profit per time step, split from the optimiser's own cost expressions."""

import numpy as np
import pytest
from helpers import el_system, flow, grid_h

import oems


def _site():
    """Grid (energy price, export credit, monthly peak charge 100 EUR/MW, fixed 7 EUR/month) + battery with cycle
    cost + PV + demand over 4 hours."""
    es, el = el_system(grid_h(4))
    price = [10.0, 50.0, 20.0, 80.0]
    es.add(oems.GridConnection("dso", el, import_price=price, export_price=[p - 5 for p in price], peak_charge=100,
                               tariffs=[oems.FixedCharge(7, period="M")]),
           oems.Storage("bess", el, e_max=2, p_ch_max=1, p_dis_max=1, eta_ch=1, eta_dis=1, soc_init=0, end="free",
                        cycle_cost=3),
           oems.Producer("pv", el, profile=[0, 2, 0, 0], curtailable=True),
           oems.Demand("load", el, profile=[1, 1, 1, 1]))
    return es.solve(), price


def test_step_costs_add_up_to_component_costs():
    """For every component and category, the per-step parts plus the period charges equal the optimiser's cost."""
    res, _ = _site()
    per_step, period = res.step_costs()
    total = per_step.sum()
    for (c, k), v in period.set_index(["component", "category"])["EUR"].items():
        total[(c, k)] = total.get((c, k), 0.0) + v
    for r in res._cost_rows.itertuples():
        assert total[(r.component, r.category)] == pytest.approx(r.EUR, abs=1e-6)
    # peak and fixed charges are per billing period, not per step
    assert set(period["category"]) == {"PEAK", "FIXED"} and period.query("category == 'FIXED'")["EUR"].iloc[0] == 7


def test_profit_per_step_matches_flows_and_prices():
    """Each step's profit = export income - import cost - battery cycle cost at that step, and summed with the
    period charges it equals -total_cost."""
    res, price = _site()
    p = res.profit()
    imp, exp, dis = flow(res, "dso", "import"), flow(res, "dso", "export"), flow(res, "bess", "discharge")
    expected = exp * (np.array(price) - 5) - imp * np.array(price) - 3 * dis
    assert np.allclose(p["profit"], expected, atol=1e-6)
    assert np.allclose(p["revenue"], exp * (np.array(price) - 5), atol=1e-6)
    period = res.step_costs()[1]
    period_real = period.loc[~period["category"].isin(["TIEBREAK", "SLACK"]), "EUR"].sum()
    assert p["profit"].sum() - period_real == pytest.approx(-res.total_cost, abs=1e-6)
    assert p["profit_cumulative"].iloc[-1] == pytest.approx(p["profit"].sum())

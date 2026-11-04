"""Validation: storage dispatch and energy accounting."""

import numpy as np
import pytest
from helpers import assert_invariants, el_system, flow, grid_15, grid_h, state

import oems


def _arbitrage(prices, eta, **kw):
    es, el = el_system(grid_h(len(prices)))
    es.add(oems.GridConnection("dso", el, import_price=prices), oems.Demand("load", el, profile=1),
           oems.Storage("bess", el, e_max=1, p_ch_max=1, p_dis_max=1, eta_ch=eta, eta_dis=eta, soc_init=0, **kw))
    return es.solve(tie_break=0)


def test_two_price_arbitrage_lossless():
    """Prices [10, 100], 1 MW demand. A lossless 1 MWh battery charges 1 MWh at 10 and serves the
    second hour: cost = 2 MWh * 10 = 20 EUR (vs 110 EUR without storage)."""
    res = _arbitrage([10, 100], 1.0)
    assert_invariants(res)
    assert res.objective == pytest.approx(20.0)
    assert flow(res, "bess", "charge")[0] == pytest.approx(1.0)


def test_arbitrage_with_losses():
    """eta_ch = eta_dis = 0.9. Charging 1 MWh at 50 stores 0.9 MWh and delivers 0.81 MWh at 100.
    Cost = 50 * 2 + 100 * (1 - 0.81) = 119 EUR."""
    res = _arbitrage([50, 100], 0.9)
    assert_invariants(res)
    assert res.objective == pytest.approx(119.0)


def test_no_cycling_when_spread_below_losses():
    """Prices [50, 55], round-trip 0.81: 1 MWh at 50 yields 0.81 MWh worth 44.55 < 50 -> no cycling."""
    res = _arbitrage([50, 55], 0.9)
    assert res.objective == pytest.approx(105.0)
    assert flow(res, "bess", "discharge").sum() == pytest.approx(0.0, abs=1e-9)


def test_cycle_cost_prevents_cycling():
    """Spread 20 EUR/MWh but cycle cost 30 EUR per MWh discharged -> battery stays idle."""
    res = _arbitrage([10, 30], 1.0, cycle_cost=30)
    assert flow(res, "bess", "discharge").sum() == pytest.approx(0.0, abs=1e-9)
    assert res.objective == pytest.approx(40.0)


@pytest.mark.parametrize("grid, step_after_1h", [(grid_h(2), 0), (grid_15(8), 3)])
def test_self_discharge_is_per_hour(grid, step_after_1h):
    """10 %/h self-discharge leaves 0.9 MWh of 1 MWh after one hour at any resolution."""
    es, el = el_system(grid)
    es.add(oems.GridConnection("dso", el, import_price=1), oems.Demand("load", el, profile=0),
           oems.Storage("s", el, e_max=1, p_ch_max=1, p_dis_max=1, soc_init=1.0, self_discharge=0.1, end="free"))
    res = es.solve(tie_break=0.01)
    assert state(res, "s", "e")[step_after_1h] == pytest.approx(0.9)


def test_energy_accounting_exact():
    """e[t] = keep * e[t-1] + (eta_ch * ch - dis / eta_dis) * dt holds exactly for every step."""
    rng = np.random.default_rng(0)
    prices = rng.uniform(0, 100, 24)
    es, el = el_system(grid_15(24))
    es.add(oems.GridConnection("dso", el, import_price=prices, export_price=prices * 0.9, max_export=2),
           oems.Demand("load", el, profile=0.5),
           oems.Storage("s", el, e_max=2, p_ch_max=1, p_dis_max=1, eta_ch=0.95, eta_dis=0.9,
                        self_discharge=0.02, soc_init=0.3))
    res = es.solve()
    assert_invariants(res)
    e, ch, dis = state(res, "s", "e"), flow(res, "s", "charge"), flow(res, "s", "discharge")
    prev = np.concatenate(([0.6], e[:-1]))
    keep = (1 - 0.02) ** 0.25
    assert np.allclose(e, keep * prev + (0.95 * ch - dis / 0.9) * 0.25, atol=1e-7)


@pytest.mark.parametrize("end, kw, expected", [
    ("cyclic_eq", {}, 0.5),
    ("fixed", {"soc_end": 0.8}, 0.8),
])
def test_end_conditions(end, kw, expected):
    """cyclic_eq returns to soc_init; fixed ends at soc_end (as a fraction of e_max = 1 MWh)."""
    es, el = el_system(grid_h(4))
    es.add(oems.GridConnection("dso", el, import_price=[10, 50, 10, 50]), oems.Demand("load", el, profile=0.5),
           oems.Storage("s", el, e_max=1, p_ch_max=1, p_dis_max=1, soc_init=0.5, end=end, **kw))
    res = es.solve()
    assert state(res, "s", "e")[-1] == pytest.approx(expected)


def test_cyclic_geq_end_condition():
    """Default end condition: final energy >= initial energy."""
    res = _arbitrage([100, 10], 1.0)
    assert state(res, "bess", "e")[-1] >= -1e-9


def test_tie_break_prevents_simultaneous_charge_discharge():
    """With equal prices, any cycling is pointless; the tie-break keeps the battery idle, and the
    tie-break cost is excluded from total_cost."""
    es, el = el_system(grid_h(4))
    es.add(oems.GridConnection("dso", el, import_price=50), oems.Demand("load", el, profile=1),
           oems.Storage("s", el, e_max=1, p_ch_max=1, p_dis_max=1, eta_ch=1, eta_dis=1, soc_init=0.5))
    res = es.solve(tie_break=0.01)
    assert np.all(flow(res, "s", "charge") * flow(res, "s", "discharge") < 1e-9)
    assert res.total_cost == pytest.approx(200.0)

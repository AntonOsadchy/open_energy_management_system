"""Validation: flexible demand and thermal zones."""

import numpy as np
import pytest
from helpers import assert_invariants, el_system, flow, grid_h, state

import oems


def _flex(prices, **kw):
    es, el = el_system(grid_h(len(prices)))
    defaults = dict(baseline=1.0, p_max=3, window=None)
    defaults.update(kw)
    es.add(oems.GridConnection("dso", el, import_price=prices), oems.FlexibleDemand("flex", el, **defaults))
    return es.solve(tie_break=0.0)


def test_load_shifts_to_cheap_hours():
    """Baseline 1 MW for 4 h, prices [100, 100, 10, 10], no shift limit, p_max 3:
    all 4 MWh move to the cheap hours -> cost 4 * 10 = 40 EUR."""
    res = _flex([100, 100, 10, 10])
    assert_invariants(res)
    assert res.objective == pytest.approx(40.0)


def test_max_deferred_limits_shifted_energy():
    """Same case with max_deferred = 1 MWh and p_max = 2: only one of the two expensive hours can be
    deferred, e.g. consumption [0, 1, 2, 1] -> cost 100 + 20 + 10 = 130 EUR. (Deferring hour 1
    instead of hour 0 costs the same - the test checks the cost and the backlog bound only.)"""
    res = _flex([100, 100, 10, 10], max_deferred=1, p_max=2)
    assert res.objective == pytest.approx(130.0)
    assert state(res, "flex", "s_plus").max() <= 1.0 + 1e-9
    assert flow(res, "flex", "in")[:2].sum() == pytest.approx(1.0)


def test_rebound_adds_energy():
    """rebound 0.1: deferring 2 MWh requires 2.2 MWh later. Cost = 10 * 4.2 = 42 EUR."""
    res = _flex([100, 100, 10, 10], rebound=0.1)
    assert res.objective == pytest.approx(42.0)


def test_zero_flexibility_equals_fixed_demand():
    """max_deferred = max_advanced = 0 reproduces the baseline exactly."""
    res = _flex([100, 10, 100, 10], max_deferred=0, max_advanced=0)
    assert np.allclose(flow(res, "flex", "in"), 1.0)


def test_daily_window_conserves_energy():
    """With window='D' the energy consumed each day equals the baseline energy of that day."""
    rng = np.random.default_rng(1)
    prices = rng.uniform(0, 100, 48)
    baseline = rng.uniform(0.5, 1.5, 48)
    es, el = el_system(grid_h(48))
    es.add(oems.GridConnection("dso", el, import_price=prices),
           oems.FlexibleDemand("flex", el, baseline=baseline, p_max=3, window="D"))
    res = es.solve()
    f = flow(res, "flex", "in")
    assert f[:24].sum() == pytest.approx(baseline[:24].sum())
    assert f[24:].sum() == pytest.approx(baseline[24:].sum())
    assert state(res, "flex", "s_plus")[23] == pytest.approx(0.0, abs=1e-9)


def test_deviation_cost_reduces_shifting():
    """A deviation cost of 60 EUR/MWh (on |F - D|) exceeds half the spread (90/2): no shifting."""
    res = _flex([100, 10], deviation_cost=60)
    assert np.allclose(flow(res, "flex", "in"), 1.0)


# ------------------------------------------------------------------ thermal zone
def _zone_system(n, prices, **zone_kw):
    g = grid_h(n)
    es = oems.EnergySystem(g)
    el = es.add_bus("el", oems.Carrier("electricity", kind="electricity"))
    cold = es.add_bus("cold", oems.Carrier("cold", kind="cold"))
    es.add(oems.GridConnection("dso", el, import_price=prices),
           oems.presets.chiller("ch", el, cold, eer=3.0, p_cold_max=zone_kw.pop("p_cold_max", 5)),
           oems.ThermalZone("store", cold_bus=cold, **zone_kw))
    return es


def test_free_floating_zone_matches_analytic_decay():
    """Without cooling (chiller limited to 0) and wide bounds, theta follows the discrete solution
    theta[t] - theta_amb = (1 - dt/(R*C))^(t+1) * (theta_init - theta_amb)."""
    C, R, amb, th0 = 2.0, 5.0, 20.0, 0.0
    es = _zone_system(6, 10, p_cold_max=0, C=C, R=R, theta_amb=amb, theta_min=-100, theta_max=100, theta_init=th0)
    res = es.solve()
    expected = amb + (1 - 1 / (R * C)) ** np.arange(1, 7) * (th0 - amb)
    assert np.allclose(state(res, "store", "theta"), expected)


def test_precooling_before_price_spike():
    """Cold store between -22 and -18 degC, price spike in hour 3. The optimum cools harder before
    the spike so the chiller can be idle during it, and it costs less than without the band."""
    kw = dict(C=1.0, R=10.0, theta_amb=10.0, theta_max=-18.0, theta_init=-20.0)
    prices = [50, 50, 50, 500, 50, 50]
    flexible = _zone_system(6, prices, theta_min=-22.0, **kw).solve()
    assert_invariants(flexible)
    assert flow(flexible, "ch", "in_el")[3] == pytest.approx(0.0, abs=1e-7)
    th = state(flexible, "store", "theta")
    assert np.all(th >= -22 - 1e-7) and np.all(th <= -18 + 1e-7)
    tight = _zone_system(6, prices, theta_min=-18.0 - 1e-3, **dict(kw, theta_init=-18.0)).solve()
    assert flexible.total_cost < tight.total_cost


def test_comfort_penalty_makes_band_soft():
    """A band that cannot be held (no cooling) is infeasible unless a comfort penalty is set; then
    the violation is paid for (FLEXIBILITY cost > 0)."""
    kw = dict(p_cold_max=0, C=1.0, R=10.0, theta_amb=30.0, theta_min=-22.0, theta_max=-18.0, theta_init=-18.0)
    with pytest.raises(oems.InfeasibleModelError):
        _zone_system(3, 10, **dict(kw)).solve(diagnose=False)
    res = _zone_system(3, 10, comfort_penalty=100.0, **dict(kw)).solve()
    assert res.cost_by_category["FLEXIBILITY"] > 0

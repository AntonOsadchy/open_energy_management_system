"""Performance: a one-shot lifetime optimisation of PV + battery sizes over 15 hourly years (131,400 steps)."""

import time

import numpy as np
import pandas as pd
import pytest

from oems.sizing import optimise_lifetime


def _site_cfg(years=15):
    """A full calendar year of hourly data (evening price peak, midday PV, 1 MW load) repeated for ``years``."""
    idx = pd.date_range("2025-01-01", periods=8760, freq="1h", tz="Europe/Copenhagen")
    h, d = idx.hour, idx.dayofyear
    rng = np.random.default_rng(1)
    price = 80 + 60 * ((h >= 17) & (h < 21)) - 40 * ((h >= 10) & (h < 15)) + rng.normal(0, 15, len(idx))
    cf = np.clip(np.sin((h - 6) / 12 * np.pi), 0, None) * (0.45 + 0.35 * np.sin((d - 80) / 365 * 2 * np.pi))
    frames = {"site": pd.DataFrame({"price": price, "pv_cf": np.clip(cf, 0, 1), "load": 1.0}, index=idx)}
    cfg = {"time": {"start": "2025-01-01", "periods": 8760, "freq": "1h", "tz": "Europe/Copenhagen"},
           "carriers": [{"name": "electricity", "kind": "electricity"}],
           "buses": [{"name": "el", "carrier": "electricity"}],
           "components": [
               {"type": "GridConnection", "name": "grid", "bus": "el", "import_price": {"series": "site:price"},
                "export_price": {"series": "site:price"}, "max_import": 5, "max_export": 5},
               {"type": "Demand", "name": "load", "bus": "el", "profile": {"series": "site:load"}},
               {"type": "Producer", "name": "pv", "bus": "el", "cf": {"series": "site:pv_cf"}, "optimise_size": True,
                "capacity_max": 5, "capex": 600_000, "opex": 8_000},
               {"type": "Storage", "name": "bess", "bus": "el", "optimise_size": True, "e_size_max": 10,
                "p_size_max": 5, "capex_energy": 250_000, "capex_power": 60_000, "opex": 3_000}],
           "lifetime": {"mode": "optimise", "years": years, "discount_rate": 0.07, "model": "full"}}
    return cfg, frames


@pytest.mark.slow
def test_fifteen_hourly_years_in_one_model():
    """15 x 8760 hourly steps, PV and battery sized, plus the reference case: two solves (interior point, the default
    for lifetime horizons), each within 15 minutes. Times and the result are printed."""
    from oems.config import from_config

    cfg, frames = _site_cfg()

    def solve(c):
        es, _ = from_config(c, frames=frames)
        return es.solve()

    t0 = time.perf_counter()
    sr = optimise_lifetime(cfg, solve=solve)
    total = time.perf_counter() - t0
    for label, res in (("sized", sr.results), ("reference", sr.reference)):
        print(f"\n{label}: {res.grid.n} steps, build {res.build_time:.1f}s solve {res.solve_time:.1f}s")
        assert res.status == "optimal"
        assert res.build_time < 300 and res.solve_time < 900
    print(f"total {total:.0f}s; sizes {[(s['component'], s['quantity'], round(s['value'], 2)) for s in sr.sizes]}; "
          f"NPV {sr.kpis['npv']:,.0f} IRR {sr.kpis['irr']} payback {sr.kpis['payback']}")
    assert sr.results.grid.n == 15 * 8760
    assert sr.kpis["npv"] == pytest.approx(sr.kpis["npv_check"], rel=1e-6)

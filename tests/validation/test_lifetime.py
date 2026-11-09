"""Validation: multi-year runs with battery degradation (oems.degradation.simulate_lifetime)."""

import pytest

from oems.config import from_config
from oems.degradation import simulate_lifetime


def _cfg(curves=None, prices=(10, 100, 10, 100), eta=1.0):
    """A 4-hour 'year': prices alternate 10 / 100 EUR/MWh, 1 MW demand, a 1 MWh / 1 MW battery starting empty.
    A battery of capacity c charges c twice at 10 and discharges it twice at 100: cost = 220 - 180 c EUR, and it
    makes (2c + 2c) / 2 / 1 = 2c cycles per year, i.e. 2c / (4/24) = 12c cycles/day."""
    store = {"type": "Storage", "name": "bess", "bus": "el", "e_max": 1, "p_ch_max": 1, "p_dis_max": 1,
             "eta_ch": eta, "eta_dis": eta, "soc_init": 0}
    if curves:
        store["degradation"] = {"curves": curves, "source": "test"}
    return {"time": {"start": "2025-01-01", "periods": 4, "freq": "1h"},
            "carriers": [{"name": "electricity", "kind": "electricity"}],
            "buses": [{"name": "el", "carrier": "electricity"}],
            "components": [{"type": "GridConnection", "name": "grid", "bus": "el", "import_price": list(prices),
                            "max_export": 0},
                           {"type": "Demand", "name": "load", "bus": "el", "profile": 1}, store]}


def _counting_solver():
    calls = []

    def solve(c):
        calls.append(c)
        es, _ = from_config(c)
        return es.solve()
    return solve, calls


def test_capacity_follows_a_single_curve_and_the_battery_retires_below_end_of_life():
    """Single curve SoH 0.9 / 0.8 / 0.7 (end of life 0.7). Year k runs with e_max = SoH after year k-1:
    1, 0.9, 0.8, 0.7 -> cost 220 - 180 e_max = 40, 58, 76, 94 EUR. After year 4 the extended curve reads 0.6 < 0.7,
    so the battery retires: years 5 and 6 have no capacity and no flows (cost 220 EUR each)."""
    solve, calls = _counting_solver()
    lt = simulate_lifetime(_cfg({"12": [0.9, 0.8, 0.7]}), 6, solve=solve)
    t = lt.table.set_index("year")
    assert list(t["capacity_start_mwh"]) == pytest.approx([1, 0.9, 0.8, 0.7, 0, 0])
    assert list(t["soh_end"]) == pytest.approx([0.9, 0.8, 0.7, 0.6, 0, 0])
    assert list(-t["profit"]) == pytest.approx([40, 58, 76, 94, 220, 220])
    assert lt.retired == {"bess": 4} and lt.eol == {"bess": pytest.approx(0.7)}
    assert list(t["cycles"]) == pytest.approx([2, 1.8, 1.6, 1.4, 0, 0])
    assert list(t["cumulative_cycles"]) == pytest.approx([2, 3.8, 5.4, 6.8, 6.8, 6.8])
    assert [c["components"][2]["e_max"] for c in calls] == pytest.approx([1, 0.9, 0.8, 0.7, 0])
    assert lt.years[5].reused and lt.years[5].results is lt.years[4].results     # dead years solved once
    assert "retires" in t.loc[4, "notes"]


def test_soh_interpolates_at_the_average_cycling_rate():
    """Curves at 6 and 18 cycles/day; the battery makes 12 cycles/day in year 1, halfway: SoH = (0.95 + 0.85) / 2."""
    lt = simulate_lifetime(_cfg({"6": [0.95, 0.9], "18": [0.85, 0.7]}), 1)
    assert lt.table["soh_end"].iloc[0] == pytest.approx(0.9)
    assert lt.table["avg_cycles_per_day"].iloc[0] == pytest.approx(12)


def test_retired_battery_cannot_burn_energy_at_negative_prices():
    """A negative price (-50 EUR/MWh) and a lossy battery (eta 0.9): a zero-capacity storage that kept its power
    could charge and discharge at once to burn energy; once retired (zero capacity *and* power) every flow is zero. Curve SoH 0.5 after year 1 = end of life (still operating); the
    extended curve reads 0 after year 2, so it retires then and year 3 is dead."""
    lt = simulate_lifetime(_cfg({"1": [0.5]}, prices=(-50, 100, 10, 100), eta=0.9), 4)
    assert lt.retired == {"bess": 2}
    assert float(lt.years[0].results.flows["bess"].to_numpy().max()) > 0              # active while alive
    for y in lt.years[2:]:
        f = y.results.flows["bess"]
        assert float(f.abs().to_numpy().max()) == 0.0


def test_without_degradation_every_year_is_the_same_and_solved_once():
    solve, calls = _counting_solver()
    lt = simulate_lifetime(_cfg(), 5, solve=solve)
    assert len(calls) == 1 and list(-lt.table["profit"]) == pytest.approx([40] * 5)
    assert list(lt.table["profit_cumulative"]) == pytest.approx([-40, -80, -120, -160, -200])
    assert "soh_start" not in lt.table.columns


def test_discounting_gives_the_npv():
    """Profit -40 EUR a year for 3 years at 10 %: NPV = -40 (1/1.1 + 1/1.1^2 + 1/1.1^3) = -99.47 EUR."""
    lt = simulate_lifetime(_cfg(), 3, discount_rate=0.1)
    assert lt.npv == pytest.approx(-40 * (1 / 1.1 + 1 / 1.1 ** 2 + 1 / 1.1 ** 3))
    assert lt.table["discount_factor"].iloc[1] == pytest.approx(1 / 1.21)


def test_inflation_scales_every_years_profit():
    """10 % inflation: the profit of -40 EUR in year 1 is -44 in year 2 and -48.4 in year 3 (nominal)."""
    lt = simulate_lifetime(_cfg(), 3, discount_rate=0.1, inflation=0.1)
    assert list(lt.table["profit"]) == pytest.approx([-40, -44, -48.4])
    assert list(lt.table["price_index"]) == pytest.approx([1, 1.1, 1.21])
    assert lt.npv == pytest.approx(-40 * 3 / 1.1)


def test_one_year_with_curves_equals_an_ordinary_run():
    lt = simulate_lifetime(_cfg({"12": [0.9]}), 1)
    es, _ = from_config(_cfg())
    assert lt.years[0].results.total_cost == pytest.approx(es.solve().total_cost)

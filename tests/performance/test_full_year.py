"""Performance: full-year runs of the electrification example with timing budgets."""

import importlib
import sys
from pathlib import Path

import pytest

import oems

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.fixture(scope="module")
def example():
    sys.path.insert(0, str(EXAMPLES))
    if not (EXAMPLES / "data" / "site_hourly.csv").exists():
        importlib.import_module("make_data").main()
    return importlib.import_module("02_site_electrification")


@pytest.mark.slow
@pytest.mark.parametrize("freq, steps, build_budget, solve_budget", [
    ("1h", 8760, 30, 120),
    ("15min", 35040, 60, 600),
])
def test_full_year_electrified(example, freq, steps, build_budget, solve_budget):
    """A full year of the electrified site (13 components, 5 buses) builds and solves within budget:
    hourly < 30 s build / 120 s solve, 15-min < 60 s build / 600 s solve. Times are printed."""
    grid = oems.TimeGrid(start="2025-01-01", periods=steps, freq=freq, tz="Europe/Copenhagen")
    res = example.build(grid, True).solve()
    print(f"\n{freq}: build {res.build_time:.1f}s solve {res.solve_time:.1f}s")
    assert res.status == "optimal"
    assert res.build_time < build_budget
    assert res.solve_time < solve_budget

"""Unit tests of the one-shot lifetime horizon (oems.sizing): discounting, cost weights and tiling of the inputs."""

import numpy as np
import pandas as pd
import pytest

from oems.config import SeriesStore
from oems.exceptions import ValidationError
from oems.sizing import Horizon, irr, payback, stack_grid, tile_list
from oems.timeseries import TimeGrid


def test_horizon_discounts_years_and_scales_a_short_base_period_to_a_year():
    hz = Horizon(3, 0.1, n_base=24, step_hours=1.0)          # a 1-day base period stands for a 365-day year
    assert hz.year_scale == pytest.approx(365)
    assert list(hz.df) == pytest.approx([1 / 1.1, 1 / 1.1**2, 1 / 1.1**3])
    assert hz.annuity == pytest.approx(sum(hz.df))
    assert list(hz.year_of[[0, 23, 24, 71]]) == [0, 0, 1, 2]
    assert hz.cost_weights[30] == pytest.approx(365 / 1.1**2)


def test_a_calendar_year_base_is_not_scaled():
    assert Horizon(2, 0, n_base=8760, step_hours=1.0).year_scale == 1.0
    assert Horizon(2, 0, n_base=366 * 96, step_hours=0.25).year_scale == 1.0


def test_horizon_rejects_bad_inputs():
    with pytest.raises(ValidationError):
        Horizon(0, 0.05, 24, 1.0)
    with pytest.raises(ValidationError):
        Horizon(10, 7, 24, 1.0)                               # 7 = 700 %, a percentage given as a fraction


def test_stacked_grid_repeats_the_base_period_back_to_back():
    base = TimeGrid(start="2025-03-29", periods=48, freq="1h", tz="Europe/Copenhagen")   # spans the DST switch
    grid = stack_grid(base, 3, 0.05)
    hz = grid.horizon
    assert grid.n == 144 and grid.index[0] == base.index[0]
    assert grid.index[48] == base.index[0] + pd.Timedelta(hours=48)
    assert list(grid.cw) == pytest.approx(list(hz.cost_weights))
    assert np.all(grid.w == 1)                                # physics and emissions are not weighted


def test_series_are_tiled_from_the_base_window_and_line_up_with_the_stacked_grid():
    base = TimeGrid(start="2025-01-01", periods=4, freq="1h")
    grid = stack_grid(base, 3, 0.0)
    idx = pd.date_range("2024-12-31 22:00", periods=10, freq="1h")      # wider than the base period
    df = pd.DataFrame({"price": np.arange(10.0)}, index=idx)
    s = SeriesStore(frames={"ds": df}, horizon=grid.horizon).get("ds:price")
    assert list(s.index) == list(grid.index)
    assert list(s) == [2, 3, 4, 5] * 3                       # the 4 base-period hours, every year


def test_series_at_another_resolution_are_tiled_too():
    base = TimeGrid(start="2025-01-01", periods=2, freq="1h")
    grid = stack_grid(base, 2, 0.0)
    idx = pd.date_range("2025-01-01", periods=8, freq="15min")
    s = SeriesStore(frames={"ds": pd.DataFrame({"p": np.arange(8.0)}, index=idx)}, horizon=grid.horizon).get("ds:p")
    assert len(s) == 16 and s.index[8] == pd.Timestamp("2025-01-01 02:00")


def test_a_series_without_data_in_the_base_period_is_an_error():
    grid = stack_grid(TimeGrid(start="2025-01-01", periods=4, freq="1h"), 2, 0.0)
    df = pd.DataFrame({"p": [1.0, 2.0]}, index=pd.date_range("2023-01-01", periods=2, freq="1h"))
    with pytest.raises(ValidationError, match="no data in the base period"):
        SeriesStore(frames={"ds": df}, horizon=grid.horizon).get("ds:p")


def test_inline_lists_of_one_base_period_are_tiled():
    hz = Horizon(3, 0.0, n_base=2, step_hours=1.0)
    assert tile_list([1, 2], hz) == [1, 2, 1, 2, 1, 2]
    assert tile_list([1, 2, 3], hz) == [1, 2, 3]              # not one value per step: left alone


def test_irr_and_payback():
    rate = irr([-100, 60, 60])                                # 60 x + 60 x^2 = 100 with x = 1 / (1 + i)
    x = (-1 + np.sqrt(1 + 4 * 100 / 60)) / 2
    assert rate == pytest.approx(1 / x - 1, abs=1e-8)
    assert irr([0, 10, 10]) is None and irr([-100, -1, -1]) is None
    flows = np.array([-100.0, 40, 40, 40])
    assert payback(np.cumsum(flows), flows) == pytest.approx(2.5)
    assert payback(np.cumsum([-100.0, 10, 10]), [-100.0, 10, 10]) is None

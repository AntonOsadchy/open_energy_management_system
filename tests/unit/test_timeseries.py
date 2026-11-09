"""Unit tests for the time grid and time-series alignment (oems.timeseries)."""

import numpy as np
import pandas as pd
import pytest

import oems
from oems.config import from_config, to_config
from oems.timeseries import DailyProfile, TimeGrid, as_param, energy_to_power, infer_resolution, load_csv


# ---------------------------------------------------------------- TimeGrid
def test_hourly_grid_step_and_length():
    """An hourly grid has dt = 1 h for every step; `end` is exclusive."""
    g = TimeGrid(start="2025-01-01", end="2025-01-02", freq="1h")
    assert g.n == 24 and np.all(g.dt == 1.0) and g.freq == "1h"


def test_15min_grid_step():
    """A 15-min grid has dt = 0.25 h."""
    g = TimeGrid(start="2025-01-01", periods=8, freq="15min")
    assert np.all(g.dt == 0.25) and g.freq == "15min"


@pytest.mark.parametrize("day, steps", [("2025-03-30", 92), ("2025-10-26", 100)])
def test_dst_days_have_92_or_100_quarter_hours(day, steps):
    """On DST change days a local-time 15-min grid has 92 (spring) or 100 (autumn) steps, all 0.25 h."""
    start = pd.Timestamp(day, tz="Europe/Copenhagen")
    g = TimeGrid(start=start, end=start + pd.DateOffset(days=1), freq="15min", tz="Europe/Copenhagen")
    assert g.n == steps and np.allclose(g.dt, 0.25)


def test_irregular_index_rejected():
    """Grids must be regular; a gap in the index raises."""
    idx = pd.DatetimeIndex(["2025-01-01 00:00", "2025-01-01 01:00", "2025-01-01 03:00"])
    with pytest.raises(oems.ValidationError):
        TimeGrid(idx)


def test_unsupported_resolution_rejected():
    """Only 15min/30min/1h resolutions are supported."""
    with pytest.raises(oems.ValidationError):
        TimeGrid(start="2025-01-01", periods=4, freq="5min")


def test_range_index_needs_dt():
    """A non-datetime grid needs an explicit dt."""
    with pytest.raises(oems.ValidationError):
        TimeGrid(pd.RangeIndex(4))
    assert TimeGrid(pd.RangeIndex(4), dt=1.0).n == 4


def test_negative_weights_rejected():
    """Negative cost weights are rejected."""
    with pytest.raises(oems.ValidationError):
        TimeGrid(start="2025-01-01", periods=2, freq="1h", weights=[1, -1])


def test_monthly_groups_follow_local_time():
    """Billing months are taken in local time: 31 Jan 23:30 local (22:30 UTC) is still January."""
    g = TimeGrid(start=pd.Timestamp("2025-01-31 23:00", tz="Europe/Copenhagen"), periods=4, freq="15min",
                 tz="Europe/Copenhagen")
    ids = g.groups("M")
    assert list(ids) == [0, 0, 0, 0]
    g2 = TimeGrid(start=pd.Timestamp("2025-01-31 23:30", tz="Europe/Copenhagen"), periods=4, freq="15min",
                  tz="Europe/Copenhagen")
    assert list(g2.groups("M")) == [0, 0, 1, 1]


def test_measurement_groups_hourly_on_15min_grid():
    """An hourly metering interval groups four 15-min steps."""
    g = TimeGrid(start="2025-01-01", periods=8, freq="15min")
    ids, exact = g.measurement_groups("1h")
    assert exact and list(ids) == [0, 0, 0, 0, 1, 1, 1, 1]


def test_measurement_finer_than_step_is_flagged():
    """A 15-min metering interval cannot be resolved on an hourly grid (exact=False)."""
    g = TimeGrid(start="2025-01-01", periods=3, freq="1h")
    ids, exact = g.measurement_groups("15min")
    assert not exact and list(ids) == [0, 1, 2]


# ---------------------------------------------------------------- as_param
def test_scalar_and_list_coercion():
    """Scalars are broadcast to all steps; lists of the right length are taken as is."""
    g = TimeGrid(start="2025-01-01", periods=3, freq="1h")
    assert np.all(as_param(2.5, g) == 2.5)
    assert list(as_param([1, 2, 3], g)) == [1, 2, 3]


def test_wrong_length_and_nan_rejected():
    """A list of the wrong length or containing NaN raises a ValidationError naming the problem."""
    g = TimeGrid(start="2025-01-01", periods=3, freq="1h")
    with pytest.raises(oems.ValidationError):
        as_param([1, 2], g)
    with pytest.raises(oems.ValidationError, match="NaN"):
        as_param([1, np.nan, 3], g)


def test_upsample_rate_holds_value():
    """An hourly price on a 15-min grid is held constant within the hour."""
    g = TimeGrid(start="2025-01-01", periods=8, freq="15min")
    s = pd.Series([10.0, 20.0], index=pd.date_range("2025-01-01", periods=2, freq="1h"))
    assert list(as_param(s, g, kind="rate")) == [10] * 4 + [20] * 4


def test_upsample_state_interpolates():
    """An hourly temperature on a 15-min grid is linearly interpolated (last hour held)."""
    g = TimeGrid(start="2025-01-01", periods=8, freq="15min")
    s = pd.Series([0.0, 4.0], index=pd.date_range("2025-01-01", periods=2, freq="1h"))
    assert np.allclose(as_param(s, g, kind="state"), [0, 1, 2, 3, 4, 4, 4, 4])


def test_downsample_power_is_mean():
    """15-min power averaged to hourly conserves energy: mean(1,2,3,4) = 2.5 MW."""
    g = TimeGrid(start="2025-01-01", periods=2, freq="1h")
    s = pd.Series([1.0, 2, 3, 4, 0, 0, 0, 8], index=pd.date_range("2025-01-01", periods=8, freq="15min"))
    assert list(as_param(s, g, kind="power")) == [2.5, 2.0]


def test_downsample_bool_is_any():
    """A 15-min mask aggregated to hourly is 1 if any quarter-hour in the hour is 1."""
    g = TimeGrid(start="2025-01-01", periods=2, freq="1h")
    s = pd.Series([0, 0, 1, 0, 0, 0, 0, 0], index=pd.date_range("2025-01-01", periods=8, freq="15min"))
    assert list(as_param(s, g, kind="bool")) == [1.0, 0.0]


def test_partial_coverage_rejected():
    """A series that does not cover the whole horizon raises instead of extrapolating."""
    g = TimeGrid(start="2025-01-01", periods=4, freq="1h")
    s = pd.Series([1.0, 2.0], index=pd.date_range("2025-01-01", periods=2, freq="1h"))
    with pytest.raises(oems.ValidationError, match="covers"):
        as_param(s, g)


def test_strict_mode_rejects_resampling():
    """With strict=True a series at another resolution raises instead of being resampled."""
    g = TimeGrid(start="2025-01-01", periods=4, freq="15min")
    s = pd.Series([1.0], index=pd.date_range("2025-01-01", periods=1, freq="1h"))
    with pytest.raises(oems.ValidationError, match="strict"):
        as_param(s, g, strict=True)


def test_timezone_mismatch_rejected():
    """A naive series on a tz-aware grid raises (no silent time shifts)."""
    g = TimeGrid(start="2025-01-01", periods=2, freq="1h", tz="UTC")
    s = pd.Series([1.0, 2.0], index=pd.date_range("2025-01-01", periods=2, freq="1h"))
    with pytest.raises(oems.ValidationError, match="time zone"):
        as_param(s, g)


def test_alignment_is_reported():
    """Every resample is logged with source/target resolution and method."""
    g = TimeGrid(start="2025-01-01", periods=4, freq="15min")
    s = pd.Series([1.0], index=pd.date_range("2025-01-01", periods=1, freq="1h"))
    rep = []
    with pytest.raises(oems.ValidationError):  # single stamp: no step info, coverage 15 min only
        as_param(s, g, report=rep)
    s2 = pd.Series([1.0, 1.0], index=pd.date_range("2025-01-01", periods=2, freq="1h"))
    as_param(s2, g, report=rep, owner="x", name="price")
    assert rep and rep[-1].method == "hold" and rep[-1].source_hours == 1.0 and rep[-1].target_hours == 0.25


def test_energy_to_power():
    """0.5 MWh per 15 min is 2 MW."""
    s = pd.Series([0.5, 0.5], index=pd.date_range("2025-01-01", periods=2, freq="15min"))
    assert list(energy_to_power(s)) == [2.0, 2.0]


def test_load_csv_and_infer_resolution(tmp_path):
    """CSV with an ISO time column is parsed into a tz-aware DatetimeIndex."""
    p = tmp_path / "d.csv"
    p.write_text("time,price,load\n2025-01-01 00:00,10,1\n2025-01-01 00:15,11,2\n2025-01-01 00:30,12,3\n")
    df = load_csv(p, tz="Europe/Copenhagen")
    assert list(df.columns) == ["price", "load"] and str(df.index.tz) == "Europe/Copenhagen"
    assert infer_resolution(df.index) == "15min"


# ---------------------------------------------------------------- daily profiles
def test_daily_profile_repeats_on_every_day_by_local_time():
    """A 24-value profile gives step h of every day the value of hour h of the local clock; on the spring DST day
    (23 h) the 02:00 value is skipped and the next midnight is still value 0."""
    g = TimeGrid(start="2025-03-29", periods=72, freq="1h", tz="Europe/Copenhagen")
    a = as_param(DailyProfile(range(24)), g)
    assert a[:24].tolist() == list(range(24)) and a[24:26].tolist() == [0, 1]
    assert a[24 + 2] == 3 and a[24 + 23] == 0           # 30 Mar: 01:00 -> 03:00, then midnight after 23 steps


def test_daily_profile_holds_on_finer_and_averages_on_coarser_grids():
    """An hourly profile holds for the four quarter hours of each hour; a 96-value (15-min) profile is averaged
    over the four quarter hours of each hourly step."""
    a = as_param(DailyProfile(range(24)), TimeGrid(start="2025-01-01", periods=8, freq="15min"))
    assert a.tolist() == [0, 0, 0, 0, 1, 1, 1, 1]
    b = as_param(DailyProfile(range(96)), TimeGrid(start="2025-01-01", periods=3, freq="1h"))
    assert b.tolist() == [1.5, 5.5, 9.5]


@pytest.mark.parametrize("values, msg", [([1.0] * 25, "split a day"), ([], "split a day"), (["x"] * 24, "numbers"),
                                         ([float("nan")] * 24, "finite")])
def test_bad_daily_profiles_are_rejected(values, msg):
    """A profile needs a whole number of equal steps per day (24, 48, 96 …) of finite numbers."""
    with pytest.raises(oems.ValidationError, match=msg):
        DailyProfile(values)


def test_daily_profile_in_a_config_round_trips():
    """`{"daily": [...]}` in a config sets a series parameter (the demand follows the profile on both days) and
    `to_config` writes it back in the same form."""
    day = [1.0] * 8 + [2.0] * 12 + [1.0] * 4
    cfg = {"name": "s", "time": {"start": "2025-01-01", "periods": 48, "freq": "1h"},
           "carriers": [{"name": "electricity", "kind": "electricity"}], "buses": [{"name": "el", "carrier": "electricity"}],
           "components": [{"type": "GridConnection", "name": "g", "bus": "el", "import_price": 50},
                          {"type": "Demand", "name": "d", "bus": "el", "profile": {"daily": day}}]}
    es, _ = from_config(cfg)
    res = es.solve()
    assert np.allclose(res.flows[("d", "in", "el", "in")].to_numpy(), day * 2)
    out, series = to_config(es)
    assert next(c for c in out["components"] if c["name"] == "d")["profile"] == {"daily": day} and not series

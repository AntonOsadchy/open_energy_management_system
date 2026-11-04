"""Time grid and time-series handling.

The model runs on a regular :class:`TimeGrid` (15 min, 30 min or hourly). Every time-varying
input is passed through :func:`as_param`, which turns scalars, sequences or pandas Series into a
``numpy`` array aligned to the grid. Series at a different resolution are resampled according to
what the numbers *mean* (see ``SERIES_KINDS``), so the same system definition runs at 15-min and
hourly resolution without changing its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from numbers import Number
from pathlib import Path

import numpy as np
import pandas as pd

from .exceptions import ValidationError

#: Supported model resolutions and their step length in hours.
SUPPORTED_FREQS = {"15min": 0.25, "30min": 0.5, "1h": 1.0, "h": 1.0, "60min": 1.0}

#: Meaning of a time series, which decides how it is resampled.
#:  - rate:  intensive value per MWh/MW (prices, fees, emission factors) - hold / mean
#:  - power: average MW over the step (loads, profiles, limits)            - hold / mean
#:  - ratio: dimensionless (COP, efficiency, capacity factor)             - hold / mean
#:  - state: instantaneous value (temperatures)                           - interpolate / mean
#:  - bool:  masks and windows                                            - hold / any
SERIES_KINDS = ("rate", "power", "ratio", "state", "bool")

_PERIOD_ALIASES = {"D": "D", "W": "W", "M": "M", "Y": "Y", "A": "Y", "Q": "Q"}


def _in_tz(x, tz) -> pd.Timestamp:
    ts = pd.Timestamp(x)
    return ts.tz_convert(tz) if ts.tz is not None else ts.tz_localize(tz)


def _freq_hours(freq: str) -> float:
    return pd.Timedelta(pd.tseries.frequencies.to_offset(freq)).total_seconds() / 3600.0


@dataclass(frozen=True)
class AlignmentRecord:
    """One entry of the alignment report: which input was resampled and how."""

    owner: str
    field: str
    source_hours: float
    target_hours: float
    method: str


class TimeGrid:
    """Regular model time grid.

    Parameters
    ----------
    index:
        A ``DatetimeIndex`` (tz-aware or naive) or a ``RangeIndex``. Alternatively give
        ``start`` + (``end`` | ``periods``) + ``freq``.
    freq:
        ``"15min"``, ``"30min"`` or ``"1h"``. Inferred from ``index`` when omitted.
    dt:
        Step length in hours. Only needed for a ``RangeIndex``.
    weights:
        Cost/emission weight per step (default 1). Weights scale costs but not physics.

    ``end`` is exclusive: ``TimeGrid(start="2025-01-01", end="2025-01-02", freq="1h")`` has 24 steps.
    """

    def __init__(
        self,
        index: pd.Index | None = None,
        *,
        start=None,
        end=None,
        periods: int | None = None,
        freq: str | None = None,
        tz: str | None = None,
        dt: float | None = None,
        weights=None,
    ):
        if index is None:
            if start is None or freq is None or (end is None and periods is None):
                raise ValidationError("TimeGrid needs an index or start + freq + (end or periods)")
            if tz is not None:  # accept timestamps with a UTC offset together with a named zone
                start, end = (None if x is None else _in_tz(x, tz) for x in (start, end))
            index = pd.date_range(start=start, end=end, periods=periods, freq=freq, tz=tz,
                                  inclusive="left" if end is not None else "both")
        if not isinstance(index, pd.Index) or len(index) == 0:
            raise ValidationError("TimeGrid index must be a non-empty pandas Index")
        self.index = index
        self.n = len(index)

        if isinstance(index, pd.DatetimeIndex):
            if self.n > 1:
                diffs = _diff_hours(index)
                if np.any(diffs <= 0):
                    raise ValidationError("TimeGrid index must be strictly increasing")
                if not np.allclose(diffs, diffs[0]):
                    raise ValidationError("TimeGrid index must be regular (constant step length)")
                step = float(diffs[0])
            elif freq is not None:
                step = _freq_hours(freq)
            elif dt is not None:
                step = float(dt)
            else:
                raise ValidationError("Single-step TimeGrid needs freq or dt")
            if freq is not None and not np.isclose(_freq_hours(freq), step):
                raise ValidationError(f"freq={freq!r} does not match the index step of {step} h")
        else:
            if dt is None:
                raise ValidationError("A non-datetime TimeGrid index needs an explicit dt (hours)")
            step = float(dt)

        if not any(np.isclose(step, h) for h in SUPPORTED_FREQS.values()):
            raise ValidationError(f"Unsupported resolution of {step} h; use 15min, 30min or 1h")
        self.step_hours = step
        self.dt = np.full(self.n, step)
        self.w = as_param(1.0 if weights is None else weights, self, kind="ratio", name="weights")
        if np.any(self.w < 0):
            raise ValidationError("TimeGrid weights must be non-negative")

    # ------------------------------------------------------------------ properties
    @property
    def is_datetime(self) -> bool:
        return isinstance(self.index, pd.DatetimeIndex)

    @property
    def tz(self):
        return self.index.tz if self.is_datetime else None

    @property
    def freq(self) -> str:
        return {0.25: "15min", 0.5: "30min", 1.0: "1h"}[self.step_hours]

    @property
    def unit_weights(self) -> bool:
        return bool(np.allclose(self.w, 1.0))

    def __len__(self) -> int:
        return self.n

    def __repr__(self) -> str:
        span = f"{self.index[0]} .. {self.index[-1]}" if self.n else "empty"
        return f"TimeGrid(n={self.n}, freq={self.freq}, {span})"

    # ------------------------------------------------------------------ groupings
    def _local_naive(self) -> pd.DatetimeIndex:
        return self.index.tz_localize(None) if self.tz is not None else self.index

    def groups(self, period) -> np.ndarray:
        """Integer group id per step for a calendar period ("D", "W", "M", "Q", "Y").

        ``None`` puts the whole horizon in one group; an array-like of ids is passed through.
        Calendar groups follow local time when the index is tz-aware.
        """
        if period is None:
            return np.zeros(self.n, dtype=int)
        if not isinstance(period, str):
            ids = np.asarray(period)
            if ids.shape != (self.n,):
                raise ValidationError(f"group ids must have length {self.n}")
            return pd.factorize(ids)[0]
        if not self.is_datetime:
            raise ValidationError(f"Calendar period {period!r} needs a DatetimeIndex grid")
        key = _PERIOD_ALIASES.get(period.upper())
        if key is None:
            raise ValidationError(f"Unknown period {period!r}; use D, W, M, Q or Y")
        return pd.factorize(self._local_naive().to_period(key))[0]

    def measurement_groups(self, measurement: str) -> tuple[np.ndarray, bool]:
        """Group ids of metering intervals ("step", "15min", "30min", "1h").

        Returns ``(ids, exact)``. ``exact`` is False when the interval is shorter than a model
        step, in which case each step is its own group (the finer peak cannot be resolved).
        """
        if measurement in (None, "step"):
            return np.arange(self.n), True
        hours = _freq_hours(measurement)
        if hours < self.step_hours - 1e-9:
            return np.arange(self.n), False
        if not self.is_datetime:
            per = int(round(hours / self.step_hours))
            return np.arange(self.n) // per, True
        utc = self.index.tz_convert("UTC") if self.tz is not None else self.index
        return pd.factorize(utc.floor(measurement))[0], True

    def last_of_group(self, ids: np.ndarray) -> np.ndarray:
        """Boolean mask marking the last step of every group."""
        mask = np.zeros(self.n, dtype=bool)
        mask[-1] = True
        mask[:-1] = ids[1:] != ids[:-1]
        return mask


# ---------------------------------------------------------------------- coercion
def _diff_hours(index: pd.DatetimeIndex) -> np.ndarray:
    """Step lengths in hours (computed in UTC, independent of the index time unit)."""
    utc = index.tz_convert("UTC") if index.tz is not None else index
    return np.diff(utc.as_unit("ns").asi8) / 3.6e12


def _series_step_hours(index: pd.DatetimeIndex) -> float | None:
    if len(index) < 2:
        return None
    return float(np.median(_diff_hours(index)))


def as_param(
    value,
    grid: TimeGrid,
    *,
    kind: str = "rate",
    name: str = "value",
    owner: str = "",
    strict: bool = False,
    report: list | None = None,
) -> np.ndarray:
    """Coerce ``value`` to a float array aligned to ``grid``.

    Accepts a scalar, a sequence/array of length ``grid.n`` or a pandas Series. A Series with a
    DatetimeIndex at another resolution is resampled according to ``kind`` (see
    ``SERIES_KINDS``) unless ``strict`` is set. Every resample is appended to ``report``.
    """
    label = f"{owner}.{name}" if owner else name
    if kind not in SERIES_KINDS:
        raise ValueError(f"unknown series kind {kind!r}")
    if value is None:
        raise ValidationError(f"{label}: value is required")
    if isinstance(value, (bool, np.bool_)):
        arr = np.full(grid.n, float(value))
    elif isinstance(value, Number):
        arr = np.full(grid.n, float(value))
    elif isinstance(value, pd.DataFrame):
        if value.shape[1] != 1:
            raise ValidationError(f"{label}: DataFrame input must have exactly one column")
        return as_param(value.iloc[:, 0], grid, kind=kind, name=name, owner=owner, strict=strict, report=report)
    elif isinstance(value, pd.Series):
        arr = _align_series(value, grid, kind, label, strict, report, owner, name)
    else:
        arr = np.asarray(value, dtype=float)
        if arr.shape != (grid.n,):
            raise ValidationError(f"{label}: expected {grid.n} values, got shape {arr.shape}")
    arr = arr.astype(float, copy=True)
    if np.any(np.isnan(arr)):
        bad = np.flatnonzero(np.isnan(arr))[:5]
        stamps = [str(grid.index[i]) for i in bad]
        raise ValidationError(f"{label}: contains NaN at {stamps}")
    return arr


def _align_series(s: pd.Series, grid: TimeGrid, kind, label, strict, report, owner, name) -> np.ndarray:
    if kind == "bool":
        s = s.astype(float)
    else:
        s = pd.to_numeric(s, errors="coerce")
    if not grid.is_datetime or not isinstance(s.index, pd.DatetimeIndex):
        if len(s) != grid.n:
            raise ValidationError(f"{label}: expected {grid.n} values, got {len(s)}")
        return s.to_numpy(dtype=float)

    if s.index.equals(grid.index):
        return s.to_numpy(dtype=float)

    if (s.index.tz is None) != (grid.tz is None):
        raise ValidationError(
            f"{label}: time zone mismatch (series tz={s.index.tz}, grid tz={grid.tz}); "
            "localize the series or use load_csv(..., tz=...)"
        )
    if strict:
        raise ValidationError(f"{label}: index does not match the model grid (strict=True)")

    s = s.sort_index()
    if s.index.has_duplicates:
        raise ValidationError(f"{label}: duplicate timestamps in input series")
    src = s.copy()
    gidx = grid.index
    if grid.tz is not None:
        src.index = src.index.tz_convert("UTC")
        gidx = gidx.tz_convert("UTC")

    src_h = _series_step_hours(src.index) or grid.step_hours
    # coverage: the series must span the full horizon
    g_end = gidx[-1] + pd.Timedelta(hours=grid.step_hours)
    s_end = src.index[-1] + pd.Timedelta(hours=src_h)
    if src.index[0] > gidx[0] or s_end < g_end:
        raise ValidationError(
            f"{label}: series covers {s.index[0]} .. {s.index[-1]} but the model needs "
            f"{grid.index[0]} .. {grid.index[-1]}"
        )

    if np.isclose(src_h, grid.step_hours):
        out = src.reindex(gidx)
        method = "reindex"
    elif src_h > grid.step_hours:  # upsample
        if kind == "state":
            union = src.index.union(gidx)
            out = src.reindex(union).interpolate(method="time").ffill().reindex(gidx)
            method = "interpolate"
        else:
            union = src.index.union(gidx)
            out = src.reindex(union).ffill().reindex(gidx)
            method = "hold"
    else:  # downsample: bin source stamps into model steps
        pos = gidx.searchsorted(src.index, side="right") - 1
        valid = (pos >= 0) & (src.index < g_end)
        binned = pd.Series(src.to_numpy()[valid], index=pos[valid])
        agg = binned.groupby(level=0).max() if kind == "bool" else binned.groupby(level=0).mean()
        out = pd.Series(np.nan, index=range(grid.n))
        out.loc[agg.index] = agg.to_numpy()
        method = "any" if kind == "bool" else "mean"

    if report is not None and method != "reindex":
        report.append(AlignmentRecord(owner, name, src_h, grid.step_hours, method))
    arr = out.to_numpy(dtype=float)
    if np.any(np.isnan(arr)):
        bad = np.flatnonzero(np.isnan(arr))[:5]
        raise ValidationError(f"{label}: missing values at {[str(grid.index[i]) for i in bad]}")
    return arr


# ---------------------------------------------------------------------- loading helpers
def load_csv(path, *, time_col: str | None = None, tz: str | None = None, sep: str | None = None) -> pd.DataFrame:
    """Load a CSV of time series into a DataFrame with a DatetimeIndex.

    The time column is ``time_col`` or the first column that parses as datetimes. Timestamps
    with an offset are converted to ``tz``; naive timestamps are localized to ``tz`` when given.
    """
    df = pd.read_csv(Path(path), sep=sep, engine="python" if sep is None else "c")
    col = time_col
    if col is None:
        for c in df.columns:
            if not pd.api.types.is_numeric_dtype(df[c]):
                try:
                    pd.to_datetime(df[c].head(20), utc=False, format="mixed")
                    col = c
                    break
                except (ValueError, TypeError):
                    continue
    if col is None:
        raise ValidationError(f"{path}: could not find a datetime column")
    try:
        idx = pd.to_datetime(df[col], format="mixed")
    except ValueError:  # several UTC offsets (e.g. +01:00/+02:00 across DST): parse via UTC
        idx = pd.to_datetime(df[col], format="mixed", utc=True)
    if getattr(idx.dt, "tz", None) is not None:
        idx = idx.dt.tz_convert(tz) if tz else idx
    elif tz:
        idx = idx.dt.tz_localize(tz, ambiguous="infer", nonexistent="shift_forward")
    out = df.drop(columns=[col])
    out.index = pd.DatetimeIndex(idx, name="time")
    return out.apply(pd.to_numeric, errors="coerce")


def energy_to_power(series: pd.Series) -> pd.Series:
    """Convert energy per interval (MWh per step) to average power (MW)."""
    h = _series_step_hours(series.index)
    if h is None:
        raise ValidationError("energy_to_power needs at least two timestamps")
    return series / h


def infer_resolution(index: pd.DatetimeIndex) -> str | None:
    """Return "15min", "30min", "1h" (or None) for a datetime index."""
    h = _series_step_hours(index)
    if h is None:
        return None
    for name, hours in (("15min", 0.25), ("30min", 0.5), ("1h", 1.0)):
        if np.isclose(h, hours):
            return name
    return None

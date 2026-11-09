"""Grid tariffs, attached to a :class:`~oems.components.grid.GridConnection`.

Each tariff acts on the import or export flow of its grid connection (``direction``) and may add
its own variables (peaks, overruns) to a sub-block of the grid block.
"""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass

import numpy as np
import pyomo.environ as pyo

from ..costs import CostCategory
from ..exceptions import ValidationError
from ..schema import Kind, param
from .base import BuildContext, Resolvable

TARIFF_REGISTRY: dict[str, type] = {}


def register_tariff(cls):
    TARIFF_REGISTRY[cls.__name__] = cls
    return cls


MEASUREMENTS = ("step", "15min", "30min", "1h")


@dataclass(eq=False)
class Tariff(Resolvable):
    _: KW_ONLY
    direction: str = param(Kind.ENUM, "import", choices=("import", "export"))

    def check(self, grid) -> list[str]:
        return []

    def build(self, blk: pyo.Block, flow, ctx: BuildContext) -> dict:
        """Add variables/constraints to ``blk``; return {CostCategory: expression}."""
        raise NotImplementedError


@register_tariff
@dataclass(eq=False)
class EnergyCharge(Tariff):
    """Volumetric charge paid per MWh (spot price, network energy fee, tax, export fee)."""

    rate: object = param(Kind.SERIES, unit="EUR/MWh", series="rate", connector=True)
    _: KW_ONLY
    category: str = param(Kind.ENUM, "ENERGY", choices=("ENERGY", "NETWORK", "TAX"))

    def build(self, blk, flow, ctx):
        return {CostCategory(self.category): ctx.wsum(self.r.rate, flow)}


@register_tariff
@dataclass(eq=False)
class EnergyCredit(Tariff):
    """Volumetric revenue received per MWh (spot export, feed-in tariff)."""

    rate: object = param(Kind.SERIES, unit="EUR/MWh", series="rate", connector=True)
    _: KW_ONLY
    direction: str = param(Kind.ENUM, "export", choices=("import", "export"))

    def build(self, blk, flow, ctx):
        return {CostCategory.REVENUE: -ctx.wsum(self.r.rate, flow)}


def _intervals(grid, period, measurement, mask):
    """Metering intervals as contiguous runs of steps with the same billing group and metering
    interval. Returns ([(billing group, step indices)], number of billing groups); intervals with
    no step inside ``mask`` are dropped."""
    k_ids = grid.groups(period)
    h_ids, _exact = grid.measurement_groups(measurement)
    change = (k_ids[1:] != k_ids[:-1]) | (h_ids[1:] != h_ids[:-1])
    starts = np.concatenate(([0], np.flatnonzero(change) + 1))
    runs = np.split(np.arange(grid.n), starts[1:])
    result = [(int(k_ids[steps[0]]), steps) for steps in runs
              if mask is None or np.any(mask[steps] > 0.5)]
    return result, int(k_ids.max()) + 1


def _group_weights(grid, ids: np.ndarray) -> np.ndarray:
    """Cost weight of each billing group (``ids`` from ``grid.groups``): the weight of its first step, so a charge
    per period is discounted with the year it falls in (all 1 outside lifetime runs)."""
    _, first = np.unique(ids, return_index=True)
    return grid.cw[first]


def _scaled_period_warning(grid, what: str) -> list[str]:
    hz = grid.horizon
    if hz is None or hz.year_scale == 1:
        return []
    return [f"{what} per billing period: the base period is {hz.base_days:g} days, scaled by {hz.year_scale:.3g} to a "
            "year, so charges per month or day are scaled with it; use a full calendar year as base period"]


@register_tariff
@dataclass(eq=False)
class PeakCharge(Tariff):
    """Capacity charge on the highest metered power in each billing period (EUR/MW per period).

    ``measurement`` is the metering interval: the peak is taken over interval means
    ("1h" on a 15-min grid bills the hourly average). ``window`` (bool series) restricts which
    steps count, e.g. weekday peak hours. Several PeakCharges can be stacked (monthly + annual).
    """

    rate: float = param(Kind.SCALAR, unit="EUR/MW/period", min=0)
    _: KW_ONLY
    period: str = param(Kind.PERIOD, "M", help="Billing period: D, W, M, Q, Y")
    measurement: str = param(Kind.ENUM, "step", choices=MEASUREMENTS)
    window: object = param(Kind.SERIES, None, series="bool", connector=True)

    def check(self, grid):
        warnings = []
        if not grid.unit_weights:
            raise ValidationError("PeakCharge requires unit time weights (chronological full periods)")
        _, exact = grid.measurement_groups(self.measurement)
        if not exact:
            warnings.append(f"PeakCharge measurement {self.measurement!r} is finer than the model step "
                            f"({grid.freq}); peaks are measured per step and may be underestimated")
        return warnings + _scaled_period_warning(grid, "PeakCharge")

    def build(self, blk, flow, ctx):
        intervals, n_k = _intervals(ctx.grid, self.period, self.measurement, self.r.window)
        dt = ctx.dt
        blk.K = pyo.RangeSet(0, n_k - 1)
        blk.pk = pyo.Var(blk.K, within=pyo.NonNegativeReals)
        blk.I = pyo.RangeSet(0, len(intervals) - 1) if intervals else pyo.Set(initialize=[])

        def rule(b, i):
            k, steps = intervals[i]
            dur = float(dt[steps].sum())
            return b.pk[k] >= pyo.quicksum(float(dt[t] / dur) * flow[int(t)] for t in steps)

        blk.peak = pyo.Constraint(blk.I, rule=rule)
        gw = _group_weights(ctx.grid, ctx.grid.groups(self.period))
        return {CostCategory.PEAK: pyo.quicksum(self.r.rate * float(gw[k]) * blk.pk[k] for k in blk.K)}


@register_tariff
@dataclass(eq=False)
class CapacityLimit(Tariff):
    """Contracted capacity. Hard limit by default; with ``penalty`` (EUR/MWh of overrun energy)
    the limit can be exceeded at a cost."""

    limit: object = param(Kind.SERIES, unit="MW", series="power", min=0, connector=True)
    _: KW_ONLY
    penalty: float = param(Kind.SCALAR, None, unit="EUR/MWh", min=0)
    measurement: str = param(Kind.ENUM, "step", choices=MEASUREMENTS)

    def build(self, blk, flow, ctx):
        intervals, _ = _intervals(ctx.grid, None, self.measurement, None)
        lim = self.r.limit
        dt, w = ctx.dt, ctx.cw
        data = []
        for _k, steps in intervals:
            dur = float(dt[steps].sum())
            L = float(np.mean(lim[steps]))
            if np.isfinite(L):
                data.append((steps, dur, L))
        blk.I = pyo.RangeSet(0, len(data) - 1) if data else pyo.Set(initialize=[])
        soft = self.r.penalty is not None
        if soft:
            blk.o = pyo.Var(blk.I, within=pyo.NonNegativeReals)

        def rule(b, i):
            steps, dur, L = data[i]
            mean = pyo.quicksum(float(dt[t] / dur) * flow[int(t)] for t in steps)
            return mean <= L + (b.o[i] if soft else 0)

        blk.limit = pyo.Constraint(blk.I, rule=rule)
        if not soft:
            return {}
        cost = pyo.quicksum(self.r.penalty * float(np.mean(w[data[i][0]])) * data[i][1] * blk.o[i] for i in blk.I)
        return {CostCategory.PEAK: cost}


@register_tariff
@dataclass(eq=False)
class FixedCharge(Tariff):
    """Constant charge per billing period (reported, does not affect dispatch)."""

    amount: float = param(Kind.SCALAR, unit="EUR/period")
    _: KW_ONLY
    period: str = param(Kind.PERIOD, "M")

    def check(self, grid):
        return _scaled_period_warning(grid, "FixedCharge")

    def build(self, blk, flow, ctx):
        gw = _group_weights(ctx.grid, ctx.grid.groups(self.period))
        return {CostCategory.FIXED: float(self.r.amount) * float(gw.sum())}

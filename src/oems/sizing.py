"""Lifetime capacity optimisation: the base period repeated year after year in ONE model, sizes as decision variables.

The time horizon of a config is its *base period* (typically a calendar year of input data). A one-shot lifetime run
(``config["lifetime"] = {"mode": "optimise", "years": N, "discount_rate": r}``) stacks N copies of it back to back
into one chronological grid (every series tiled, see :class:`Horizon`) and solves it once. Every step carries a cost
weight ``year_scale * (1 + r) ** -y`` (y = 1..N, end-of-year discounting as in ``degradation.lifetime_table``), so the
objective is the present value of all costs. With an inflation rate i (``lifetime.inflation``) every cost of year y
(energy prices, tariffs, OPEX) is the base period's times ``(1 + i) ** (y - 1)`` (year 1 at base-period prices,
CAPEX at year 0 not inflated), so the weight is ``year_scale * (1 + i) ** (y - 1) * (1 + r) ** -y`` and r is a nominal
discount rate; the cash flows, NPV, IRR and payback are nominal. Components with ``optimise_size`` get their size as
a variable, CAPEX once at the start (category INVESTMENT) and OPEX every year (FIXED_OM, times the annuity factor).

Minimising the present value of the site's costs is the same as maximising the NPV of the investment, because the
cost of the site without the new assets is a constant. To report the investment's cash flows (NPV, IRR, payback) a
*reference* case is solved as well: the same horizon with every optimised size pinned to its minimum
(:func:`reference_config`), or a *baseline* system of its own (``lifetime.reference``: a config, e.g. the site as it
is today; solved on the same horizon). Savings per year = reference operating cost - operating cost with the optimal
sizes.

Investment hurdles (``lifetime.hurdles``: a minimum IRR and/or a maximum payback) are linear constraints on the
same cash flows: IRR >= h is NPV at rate h >= 0, payback <= P is the cumulative cash flow at P >= 0. Both are a
weighted sum of the yearly cash flows (:func:`hurdle_weights`), so the reference case is solved first and its
weighted cost is the bound of the sized model's (:func:`hurdle_expr`).

A minimum self-sufficiency (``lifetime.self_sufficiency``: share of the site's electricity demand not bought from
the grid, 1 - grid import / demand) is one more linear constraint of the sized model (:func:`self_sufficiency_ports`);
the reference case is solved without it.

Capacities are constant over the lifetime (no degradation, no augmentation), so every modelled year is the same
year: same inputs, same sizes, only discounted differently. By default (``lifetime.model: weighted``) the model
therefore holds the base period ONCE, its costs weighted by ``year_scale * annuity`` (the sum of the yearly discount
factors) and its emissions by the number of years: the same optimal sizes, NPV, cash flows and hurdles as the full
chronology, N times smaller. Only the storage level at the year boundaries is simplified (each year ends where it
began, the storage's own end condition). ``lifetime.model: full`` stacks all N years (kept for degradation).
"""

from __future__ import annotations

import copy
import gc
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import ARTIFICIAL, CostCategory
from .exceptions import InfeasibleModelError, ValidationError
from .timeseries import TimeGrid

#: Days of a modelled year when the base period is not a calendar year (its costs are scaled to a year).
DAYS_PER_YEAR = 365.0

#: Cost categories of the investment itself (not operating costs of the site).
INVESTMENT_CATEGORIES = (CostCategory.INVESTMENT.value, CostCategory.FIXED_OM.value)


class Horizon:
    """``years`` years of a base period of ``n_base`` steps in one grid: ``years`` copies stacked (``weighted=False``)
    or one copy standing for every year (``weighted=True``, see the module doc).

    ``copies`` is the number of base periods in the grid, ``year_of[t]`` the copy (0-based) of step t and
    ``copy_of_year[y-1]`` the copy that models year y; ``year_scale`` scales one base period to a year (1 for a
    calendar year of 365/366 days, else 365 / base days); ``df[y-1]`` is the discount factor of year y,
    ``escalation[y-1]`` = (1 + inflation) ** (y - 1) its price level and ``annuity`` the sum of their products (the
    present value of one base-period EUR per year); ``copy_weight[k]`` the weight of copy k (df * escalation of its
    year, or the annuity); ``cost_weights`` = ``year_scale * copy_weight[year_of]``; ``represents`` = years per copy
    (emission weight).
    """

    def __init__(self, years: int, discount_rate: float, n_base: int, step_hours: float, weighted: bool = False,
                 inflation: float = 0.0):
        years = int(years)
        if years < 1:
            raise ValidationError("the lifetime must be at least 1 year")
        if not -1 < float(discount_rate) < 1:
            raise ValidationError(f"discount rate {discount_rate!r} is not a fraction (e.g. 0.07 for 7 %)")
        if not -1 < float(inflation) < 1:
            raise ValidationError(f"inflation {inflation!r} is not a fraction (e.g. 0.02 for 2 %)")
        self.years = years
        self.discount_rate = float(discount_rate)
        self.inflation = float(inflation)
        self.n_base = int(n_base)
        self.step_hours = float(step_hours)
        self.base_days = self.n_base * self.step_hours / 24.0
        full_year = any(np.isclose(self.base_days, d) for d in (365.0, 366.0))
        self.year_scale = 1.0 if full_year else DAYS_PER_YEAR / self.base_days
        self.df = (1.0 + self.discount_rate) ** -np.arange(1, years + 1, dtype=float)
        self.escalation = (1.0 + self.inflation) ** np.arange(0, years, dtype=float)
        self.annuity = float((self.df * self.escalation).sum())
        self.weighted = bool(weighted)
        self.copies = 1 if self.weighted else years
        self.year_of = np.repeat(np.arange(self.copies), self.n_base)
        self.copy_of_year = np.zeros(years, dtype=int) if self.weighted else np.arange(years)
        self.copy_weight = np.array([self.annuity]) if self.weighted else self.df * self.escalation
        self.represents = years / self.copies
        self.cost_weights = self.year_scale * self.copy_weight[self.year_of]
        self.length = pd.Timedelta(hours=self.n_base * self.step_hours)
        self.hurdle_limits: list[dict] = []     # [{name, weights, limit}]: constraints of the sized model
        self.self_sufficiency: float | None = None      # minimum share of the demand not from the grid

    @property
    def n(self) -> int:
        return self.copies * self.n_base

    def per_year(self, step_values, per_copy_weight=None) -> np.ndarray:
        """Per year 1..N: the sum of ``step_values`` over the year's copy, divided by ``per_copy_weight`` (e.g.
        ``copy_weight``, to undo the discounting of the cost weights)."""
        sums = np.bincount(self.year_of, weights=np.asarray(step_values, dtype=float), minlength=self.copies)
        if per_copy_weight is not None:
            sums = sums / per_copy_weight
        return sums[self.copy_of_year]

    def __repr__(self) -> str:
        how = "1 weighted copy" if self.weighted else f"{self.copies} copies"
        return (f"Horizon({self.years} years as {how} x {self.n_base} steps, r={self.discount_rate:g}, "
                f"i={self.inflation:g}, "
                f"year_scale={self.year_scale:g})")


def stack_grid(base: TimeGrid, years: int, discount_rate: float, weighted: bool = False,
               inflation: float = 0.0) -> TimeGrid:
    """The lifetime grid: ``base`` repeated ``years`` times (or once, weighted for every year), with the discounting
    cost weights and its Horizon."""
    if not base.is_datetime:
        raise ValidationError("a lifetime optimisation needs a dated time grid (time: start, periods, freq)")
    hz = Horizon(years, discount_rate, base.n, base.step_hours, weighted=weighted, inflation=inflation)
    grid = TimeGrid(start=base.index[0], periods=hz.n, freq=base.freq, tz=base.tz, cost_weights=hz.cost_weights)
    grid.horizon = hz
    hz.base_start, hz.base_end = base.index[0], base.index[0] + hz.length
    return grid


def tile_series(s: pd.Series, hz: Horizon, ref: str = "") -> pd.Series:
    """The part of ``s`` inside the base period, repeated for every year of the horizon (shifted by whole base
    periods, so it lines up with the stacked grid step by step)."""
    if not isinstance(s.index, pd.DatetimeIndex):
        return s
    start, end = hz.base_start, hz.base_end
    if (s.index.tz is None) != (start.tz is None):       # let as_param report the time-zone mismatch
        return s
    window = s[(s.index >= start) & (s.index < end)]
    if window.empty:
        raise ValidationError(f"{ref or 'series'}: no data in the base period {start} .. {end}")
    return pd.concat([window.set_axis(window.index + k * hz.length) for k in range(hz.copies)])


def tile_list(values: list, hz: Horizon) -> list:
    """An inline list of one value per base-period step, repeated for every year (other lists unchanged)."""
    if len(values) == hz.n_base and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return list(values) * hz.copies
    return values


# ------------------------------------------------------------------ the reference case
def optimised(cfg: dict) -> list[dict]:
    """Components of a config whose size is optimised."""
    return [c for c in cfg.get("components", []) if c.get("optimise_size")]


def without_reference(cfg: dict) -> dict:
    """``cfg`` without its baseline system (``lifetime.reference``), as the sized model is solved."""
    if "reference" not in (cfg.get("lifetime") or {}):
        return cfg
    out = dict(cfg, lifetime=dict(cfg["lifetime"]))
    out["lifetime"].pop("reference")
    return out


def reference_config(cfg: dict) -> dict:
    """The case without the investment: the baseline system of ``lifetime.reference`` if there is one (solved on
    ``cfg``'s horizon: its time and lifetime sections are ``cfg``'s), else ``cfg`` itself. Every optimised size of it
    is pinned to its minimum."""
    life = cfg["lifetime"]
    if life.get("reference") is not None:
        ref = copy.deepcopy(life["reference"])
        ref["time"] = copy.deepcopy(cfg["time"])
        ref["lifetime"] = copy.deepcopy(without_reference(cfg)["lifetime"])
    else:
        ref = copy.deepcopy(cfg)
    ref["lifetime"].pop("self_sufficiency", None)      # a target of the investment, not of the site without it
    ref["lifetime"].pop("reference_name", None)
    for c in optimised(ref):
        if c.get("type") == "Producer":
            c["capacity_max"] = c.get("capacity_min") or 0.0
        elif c.get("type") == "Storage":
            c["e_size_max"] = c.get("e_size_min") or 0.0
            c["p_size_max"] = c.get("p_size_min") or 0.0
    return ref


# ------------------------------------------------------------------ self-sufficiency
def self_sufficiency_target(cfg: dict) -> float | None:
    """``lifetime.self_sufficiency`` as a fraction (None when not set)."""
    v = (cfg.get("lifetime") or {}).get("self_sufficiency")
    if v in (None, ""):
        return None
    v = float(v)
    if not 0 <= v <= 1:
        raise ValidationError(f"self-sufficiency {v!r} is not a fraction (e.g. 0.4 for 40 %)")
    return v


def self_sufficiency_ports(system) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """``(grid imports, demands)`` on electricity buses as (component, port): self-sufficiency = 1 - the energy of
    the first / the energy of the second."""
    from .components.consumer import Demand
    from .components.grid import GridConnection

    el = {b for b, bus in system.buses.items() if bus.carrier.kind == "electricity"}
    imports, demands = [], []
    for name, comp in system.components.items():
        for port, kind, out in (("import", GridConnection, imports), ("in", Demand, demands)):
            if isinstance(comp, kind) and comp.ports()[port].bus.name in el:
                out.append((name, port))
    return imports, demands


def self_sufficiency(res) -> float | None:
    """Share of the electricity demand not bought from the grid in a solved run (None without demand)."""
    imports, demands = self_sufficiency_ports(res.system)
    energy = res.energy()

    def total(cps):
        return sum(float(energy.xs(cp, level=("component", "port"), axis=1).sum().sum()) for cp in cps)

    demand = total(demands)
    return None if demand <= 1e-9 else 1.0 - total(imports) / demand


# ------------------------------------------------------------------ investment hurdles
def hurdles(cfg: dict) -> list[tuple[str, float]]:
    """The hurdles of a config's ``lifetime.hurdles`` as ``[(kind, target)]``, kind ``irr`` (target a fraction),
    ``payback`` or ``payback_discounted`` (target in years)."""
    h = (cfg.get("lifetime") or {}).get("hurdles") or {}
    out = []
    if h.get("min_irr") not in (None, ""):
        rate = float(h["min_irr"])
        if not -1 < rate < 10:
            raise ValidationError(f"minimum IRR {rate!r} is not a fraction (e.g. 0.1 for 10 %)")
        out.append(("irr", rate))
    if h.get("max_payback") not in (None, ""):
        years = float(h["max_payback"])
        if years <= 0:
            raise ValidationError(f"maximum payback {years!r} must be more than 0 years")
        kind = h.get("payback", "simple")
        if kind not in ("simple", "discounted"):
            raise ValidationError(f"payback {kind!r}: use 'simple' or 'discounted'")
        out.append(("payback" if kind == "simple" else "payback_discounted", years))
    return out


def hurdle_weights(kind: str, target: float, hz: Horizon) -> np.ndarray:
    """Weights W[y-1] of the yearly net cash flows (years 1..N; year 0, the CAPEX, has weight 1) such that the hurdle
    holds when sum(W * net cash flow) - CAPEX >= 0: the NPV at the minimum IRR, or the cumulative (discounted) cash
    flow at the maximum payback (the part year interpolated as in :func:`payback`)."""
    y = np.arange(1, hz.years + 1, dtype=float)
    if kind == "irr":
        return (1.0 + target) ** -y
    w = np.clip(target - (y - 1), 0.0, 1.0)
    return w * hz.df if kind == "payback_discounted" else w


def _hurdle_scale(hz: Horizon, w: np.ndarray) -> tuple[np.ndarray, float]:
    """Factors that turn the lifetime model's cost weights into the hurdle weights of the nominal cash flows: per
    step (its yearly cost is inflated and discounted, so W[y] * escalation[y] / (df[y] * escalation[y]); a weighted
    copy stands for every year: sum(W * escalation) / annuity) and for charges per billing period and OPEX
    (levelised over the years as in :func:`year_table`: sum(W * escalation) / annuity)."""
    w = w * hz.escalation
    per_copy = np.bincount(hz.copy_of_year, weights=w, minlength=hz.copies) / hz.copy_weight
    return per_copy[hz.year_of], float(w.sum() / hz.annuity)


def hurdle_value(res, w: np.ndarray) -> float:
    """The site's cost weighted by the hurdle weights ``w`` (operating cost and OPEX by year, CAPEX once) of a solved
    lifetime run: the reference case's is the bound of the sized model's (:func:`hurdle_expr`)."""
    hz = res.grid.horizon
    per_step, period = res.step_costs()
    per_t, levelised = _hurdle_scale(hz, w)
    cats = per_step.columns.get_level_values("category")
    keep = ~cats.isin([c.value for c in ARTIFICIAL]) & ~cats.isin(INVESTMENT_CATEGORIES)
    total = float(per_step.loc[:, keep].sum(axis=1).to_numpy() @ per_t)
    real = period[~period["category"].isin([c.value for c in ARTIFICIAL])]
    capex = real["category"] == CostCategory.INVESTMENT.value
    return total + float(real.loc[capex, "EUR"].sum()) + levelised * float(real.loc[~capex, "EUR"].sum())


def hurdle_expr(model, meta, w: np.ndarray):
    """:func:`hurdle_value` as an expression of the model's variables: every cost term reweighted (per time step,
    per period or CAPEX) - linear, like the objective."""
    import pyomo.environ as pyo
    from pyomo.repn import generate_standard_repn

    from .results import _step_of

    hz = meta.grid.horizon
    per_t, levelised = _hurdle_scale(hz, w)
    artificial = {c.value for c in ARTIFICIAL}
    terms = []
    for (_c, k), expr in meta.cost_exprs.items():
        if k in artificial:
            continue
        repn = generate_standard_repn(expr, compute_values=True)
        flat = 1.0 if k == CostCategory.INVESTMENT.value else levelised
        if repn.constant:
            terms.append(flat * float(pyo.value(repn.constant)))
        for coef, var in zip(repn.linear_coefs, repn.linear_vars, strict=True):
            t = None if k in INVESTMENT_CATEGORIES else _step_of(var, model.T)
            terms.append(float(coef) * (flat if t is None else float(per_t[t])) * var)
    return pyo.quicksum(terms)


def _hurdle_name(kind: str, target: float) -> str:
    if kind == "irr":
        return f"IRR >= {100 * target:g} %"
    return f"{'discounted ' if kind == 'payback_discounted' else ''}payback <= {target:g} years"


# ------------------------------------------------------------------ cash flows
def _annual_costs(res) -> tuple[np.ndarray, float, float, float]:
    """Operating cost of the site per year (at base-period prices, undiscounted, EUR), plus the present values of
    CAPEX, OPEX and the charges per billing period (peak, fixed) of a solved lifetime run."""
    hz = res.grid.horizon
    per_step, period = res.step_costs()
    cats = per_step.columns.get_level_values("category")
    keep = ~cats.isin([c.value for c in ARTIFICIAL]) & ~cats.isin(INVESTMENT_CATEGORIES)
    step = per_step.loc[:, keep].sum(axis=1).to_numpy()
    annual = hz.per_year(step, hz.copy_weight)
    real = period[~period["category"].isin([c.value for c in ARTIFICIAL])]
    capex = float(real.loc[real["category"] == CostCategory.INVESTMENT.value, "EUR"].sum())
    opex = float(real.loc[real["category"] == CostCategory.FIXED_OM.value, "EUR"].sum())
    charges = float(real.loc[~real["category"].isin(INVESTMENT_CATEGORIES), "EUR"].sum())
    return annual, capex, opex, charges


def year_table(res, ref) -> pd.DataFrame:
    """Cash flows of the investment, one row per year (0 = the investment, 1..N operation).

    Every year is in nominal EUR: the base period's costs times the year's price level (``escalation``, inflation).
    Operating cost includes the charges per billing period (peak, fixed), levelised over the years (their present
    value / annuity). Savings = reference operating cost - operating cost; net cash flow = savings - OPEX (year 0:
    - CAPEX). Both runs' CAPEX/OPEX are counted, so a non-zero minimum size cancels out.
    """
    hz = res.grid.horizon
    op, capex, opex, charges = _annual_costs(res)
    op_ref, capex_ref, opex_ref, charges_ref = _annual_costs(ref)
    op = (op + charges / hz.annuity) * hz.escalation
    op_ref = (op_ref + charges_ref / hz.annuity) * hz.escalation
    opex_y = (opex - opex_ref) / hz.annuity * hz.escalation
    years = np.arange(0, hz.years + 1)
    df = np.concatenate(([1.0], hz.df))
    savings = np.concatenate(([0.0], op_ref - op))
    om = np.concatenate(([0.0], opex_y))
    invest = np.concatenate(([capex - capex_ref], np.zeros(hz.years)))
    net = savings - om - invest
    out = pd.DataFrame({"year": years, "price_index": np.concatenate(([1.0], hz.escalation)),
                        "discount_factor": df,
                        "operating_cost": np.concatenate(([0.0], op)),
                        "operating_cost_reference": np.concatenate(([0.0], op_ref)),
                        "savings": savings, "opex": om, "capex": invest, "net_cash_flow": net,
                        "net_cash_flow_cumulative": np.cumsum(net), "discounted": net * df,
                        "discounted_cumulative": np.cumsum(net * df)})
    energy = res.energy()
    for name, comp in res.system.components.items():
        if not getattr(comp, "optimise_size", False):
            continue
        port = "out" if "out" in comp.ports() else "discharge"
        mwh = energy.xs((name, port), level=("component", "port"), axis=1).sum(axis=1).to_numpy()
        per_year = hz.per_year(mwh) * hz.year_scale
        out[f"{name} {'production' if port == 'out' else 'discharged'} MWh"] = np.concatenate(([0.0], per_year))
    return out


def irr(cash_flows) -> float | None:
    """Internal rate of return of yearly cash flows (year 0 first); None when NPV(rate) has no root in (-99 %,
    1000 %) (e.g. no investment, or never paid back)."""
    cf = np.asarray(cash_flows, dtype=float)
    t = np.arange(len(cf))

    def npv(rate):
        return float(np.sum(cf / (1.0 + rate) ** t))

    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if not np.isfinite(f_lo) or np.sign(f_lo) == np.sign(f_hi) or abs(cf[0]) < 1e-9:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if np.sign(f_mid) == np.sign(f_lo):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2


def payback(cumulative, flows) -> float | None:
    """Years until the cumulative cash flow turns non-negative (interpolated within the year); None if never."""
    cum = np.asarray(cumulative, dtype=float)
    if len(cum) < 2 or cum[0] >= -1e-9:
        return None
    for y in range(1, len(cum)):
        if cum[y] >= -1e-9:
            step = float(flows[y])
            return float(y - 1 + (-cum[y - 1] / step if step > 0 else 1.0))
    return None


@dataclass
class SizingResults:
    results: object                                 # Results of the lifetime run with the optimal sizes
    reference: object                               # Results with every optimised size at its minimum
    table: pd.DataFrame                             # year_table
    sizes: list[dict]                               # [{component, quantity, value, unit, min, max}]
    kpis: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def horizon(self) -> Horizon:
        return self.results.grid.horizon


def investment_kpis(table: pd.DataFrame, hz: Horizon, tol: float = 1e-9) -> dict:
    """Headline figures of the investment from its year table. A CAPEX within ``tol`` EUR is no investment (solver
    noise): no IRR or payback."""
    capex = float(table["capex"].sum())
    none = abs(capex) <= tol
    return {"npv": float(table["discounted"].sum()), "irr": None if none else irr(table["net_cash_flow"]),
            "payback": None if none else payback(table["net_cash_flow_cumulative"], table["net_cash_flow"]),
            "payback_discounted": None if none else payback(table["discounted_cumulative"], table["discounted"]),
            "capex": capex, "opex_per_year": float(table["opex"].iloc[1:].mean()),
            "savings_year1": float(table["savings"].iloc[1]), "savings_total": float(table["savings"].sum()),
            "years": hz.years, "discount_rate": hz.discount_rate, "inflation": hz.inflation,
            "year_scale": hz.year_scale,
            "steps": hz.n, "base_days": hz.base_days}


def optimise_lifetime(cfg: dict, *, solve=None, on_stage=None, solve_both=None) -> SizingResults:
    """Solve the one-shot lifetime model of ``cfg`` (``lifetime.mode == "optimise"``) and its reference case.

    ``solve(cfg) -> Results`` solves one config (default ``from_config`` + ``EnergySystem.solve``); ``on_stage(k, n,
    label)`` reports progress before each solve. ``solve_both(sized, reference) -> [Results, Results]`` solves the
    two at once instead (the cloud: two machines in parallel); progress is then one stage (1 of 1).

    With hurdles (``lifetime.hurdles``) the reference case is solved first and the sized model gets one constraint
    per hurdle (``lifetime.hurdle_limits``: weights and bound, see :func:`hurdle_expr`); ``solve_both`` is not used.
    """
    from .config import from_config

    life = cfg.get("lifetime") or {}
    if life.get("mode") != "optimise":
        raise ValidationError("not a lifetime optimisation (config lifetime.mode must be 'optimise')")
    if not optimised(cfg):
        raise ValidationError("no component has optimise_size set: nothing to size")
    goals = hurdles(cfg)
    target = self_sufficiency_target(cfg)
    reference, cfg = reference_config(cfg), without_reference(cfg)
    if target is not None:
        solve, solve_both = _explain_infeasible(solve, target), _explain_infeasible(solve_both, target)
    if solve_both is not None and not goals:
        if on_stage:
            on_stage(1, 1, "optimal sizes and reference case in parallel")
        res, ref = solve_both(cfg, reference)
        return _sizing_results(cfg, res, ref)
    if solve is None:
        def plain(c):
            es, opts = from_config(c)
            return es.solve(time_limit=opts.get("time_limit"), tie_break=opts.get("tie_break", 0.01))
        solve = _explain_infeasible(plain, target) if target is not None else plain
    if goals:       # the hurdles are bounded by the reference case's cash flows: it goes first
        if on_stage:
            on_stage(1, 2, "reference case (without the investment)")
        ref = solve(reference)
        ref.release_model()
        gc.collect()
        hz = ref.grid.horizon
        limits = []
        for kind, target in goals:
            w = hurdle_weights(kind, target, hz)
            limits.append({"name": _hurdle_name(kind, target), "weights": w.tolist(), "limit": hurdle_value(ref, w)})
        sized = copy.deepcopy(cfg)
        sized["lifetime"]["hurdle_limits"] = limits
        if on_stage:
            on_stage(2, 2, "optimal sizes meeting the hurdles")
        res = solve(sized)
        res.release_model()
        return _sizing_results(cfg, res, ref)
    if on_stage:
        on_stage(1, 2, "sizing model")
    res = solve(cfg)
    res.release_model()         # before the reference is built: two lifetime models at once may not fit in memory
    gc.collect()
    if on_stage:
        on_stage(2, 2, "reference case (without the investment)")
    ref = solve(reference)
    ref.release_model()
    return _sizing_results(cfg, res, ref)


def _explain_infeasible(fn, target: float):
    """``fn`` (a solve) that reports an infeasible sized model as the self-sufficiency target out of reach
    (None stays None)."""
    if fn is None:
        return None

    def run(*args):
        try:
            return fn(*args)
        except InfeasibleModelError as e:
            if any("self_sufficiency" in (a.get("lifetime") or {}) for a in args if isinstance(a, dict)):
                raise ValidationError(
                    f"a self-sufficiency of {100 * target:g} % cannot be reached within the max sizes of the "
                    "optimised assets: raise their max sizes or lower the target") from e
            raise
    return run


def _sizing_results(cfg: dict, res, ref) -> SizingResults:
    """Sizes, cash flows and KPIs of a solved lifetime optimisation and its reference case."""
    hz = res.grid.horizon
    table = year_table(res, ref)
    sizes = []
    by_name = {c["name"]: c for c in optimised(cfg)}
    for name, quantities in res.sizes.items():
        c = by_name.get(name, {})
        for q, (value, unit) in quantities.items():
            lo, hi = {"capacity": ("capacity_min", "capacity_max"), "energy": ("e_size_min", "e_size_max"),
                      "power": ("p_size_min", "p_size_max")}.get(q, (None, None))
            sizes.append({"component": name, "quantity": q, "value": value, "unit": unit,
                          "min": c.get(lo) or 0.0, "max": c.get(hi)})
    noise = 1e-7 * max(1.0, abs(ref.total_cost))        # EUR: below this a cash flow is solver noise
    kpis = investment_kpis(table, hz, tol=noise)
    kpis["npv_check"] = float(ref.total_cost - res.total_cost)
    kpis["reference"] = (cfg.get("lifetime") or {}).get("reference_name")   # the baseline system, if one was chosen
    warnings = []
    target = self_sufficiency_target(cfg)
    kpis["self_sufficiency"], kpis["self_sufficiency_reference"] = self_sufficiency(res), self_sufficiency(ref)
    if target is not None:
        kpis["self_sufficiency_target"] = target
        kpis["self_sufficiency_binding"] = (kpis["self_sufficiency"] is not None
                                            and kpis["self_sufficiency"] <= target + 1e-5)
    goals = hurdles(cfg)
    if goals:
        kpis["hurdles"] = _hurdle_report(goals, table, kpis, hz, noise)
        invested = any(abs(s["value"] - s["min"]) > 1e-6 * max(1.0, abs(s["value"])) for s in sizes)
        if not invested:
            warnings.append("no investment meets the hurdles (" + ", ".join(h["name"] for h in kpis["hurdles"])
                            + "): every optimised size stays at its minimum")
    for s in sizes:
        if s["max"] is not None and s["value"] >= float(s["max"]) - 1e-6 * max(1.0, abs(float(s["max"]))):
            warnings.append(f"{s['component']}: the optimal {s['quantity']} is at its maximum "
                            f"({s['value']:,.3g} {s['unit']}); a larger limit could pay off more")
    return SizingResults(res, ref, table, sizes, kpis, warnings)


def _hurdle_report(goals, table: pd.DataFrame, kpis: dict, hz: Horizon, noise: float) -> list[dict]:
    """Per hurdle: target, achieved value, met, and binding (the investment's weighted cash flow is ~0, so the
    hurdle limited the sizes). Cash flows within ``noise`` EUR count as zero."""
    net = table["net_cash_flow"].to_numpy()
    invested = abs(kpis["capex"]) > noise
    scale = max(noise, 1e-6 * abs(kpis["capex"]))
    out = []
    for kind, target in goals:
        w = hurdle_weights(kind, target, hz)
        margin = float(net[0] + w @ net[1:])
        achieved = kpis[kind]
        out.append({"kind": kind, "name": _hurdle_name(kind, target), "target": target, "achieved": achieved,
                    "met": margin >= -scale, "margin": margin,
                    "binding": invested and abs(margin) <= 10 * scale})
    return out

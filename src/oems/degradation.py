"""Multi-year runs with battery degradation: the base period is repeated year after year and every storage with
state-of-health (SoH) curves loses capacity according to how hard it was cycled.

The simulation is myopic, year by year: each year is an ordinary solve (the battery is dispatched for maximum
profit, with no degradation cost in the objective). After the year its throughput is counted, the cumulative
cycles give the SoH at the end of that year, and the next year runs with ``e_max = nominal * SoH``.

SoH is read from curves per cycling rate (equivalent full cycles per day), as in BESS IQ's
``bess_degradation_curves``: at age ``a`` years with ``N`` cycles since commissioning, the average rate is
``N / (days_per_year * a)`` and SoH is interpolated linearly between the two curves that bracket that rate. All
curves end at the end-of-life (EOL) SoH. When a year ends below EOL the battery retires: from the next year on it
has no capacity and no power (SoH 0), while the rest of the site keeps running for the full simulation length.

A cycle is ``(charged + discharged) / 2 / nominal e_max`` - the convention of the curves and of the
``cycles_throughput`` KPI.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .exceptions import ValidationError

#: Largest allowed spread between the curves' final SoH values before a warning (fraction of nominal).
EOL_SPREAD = 0.005


class DegradationCurves:
    """SoH curves by cycling rate: ``{cycles_per_day: [SoH after year 1, after year 2, ...]}``.

    Age 0 (SoH 1.0) is added. A curve may end earlier than others (hard cycling ends life sooner); past its end
    it is continued along its last-year slope, so interpolating near it keeps falling below EOL.
    """

    def __init__(self, curves: dict, name: str = ""):
        if not curves:
            raise ValidationError(f"{name or 'degradation'}: no SoH curves")
        self.name = name
        self.rates: list[float] = []
        self.soh: list[np.ndarray] = []
        for rate, values in sorted(((float(r), v) for r, v in curves.items()), key=lambda x: x[0]):
            vals = [float(x) for x in values]
            label = f"{name or 'degradation'} ({rate:g} cycles/day)"
            if rate <= 0:
                raise ValidationError(f"{label}: cycling rate must be > 0")
            if not vals:
                raise ValidationError(f"{label}: empty curve")
            if any(not 0 <= x <= 1 for x in vals):
                raise ValidationError(f"{label}: SoH must be between 0 and 1")
            arr = np.array([1.0, *vals])
            if np.any(np.diff(arr) > 1e-12):
                raise ValidationError(f"{label}: SoH increases with age")
            self.rates.append(rate)
            self.soh.append(arr)
        ends = [a[-1] for a in self.soh]
        self.eol = float(max(ends))
        self.warnings = ([f"{name or 'degradation'}: the curves end at different SoH ({min(ends):.3f}-"
                          f"{max(ends):.3f}); {self.eol:.3f} is used as end of life"]
                         if max(ends) - min(ends) > EOL_SPREAD else [])

    @classmethod
    def from_config(cls, value, name: str = "") -> DegradationCurves:
        """From a storage's ``degradation`` field: ``{"curves": {rate: [...]}, "source": ...}`` or the bare map."""
        curves = value.get("curves", value) if isinstance(value, dict) else None
        if not isinstance(curves, dict):
            raise ValidationError(f"{name}: degradation must be {{'curves': {{cycles_per_day: [SoH, ...]}}}}")
        return cls(curves, name)

    @property
    def single(self) -> bool:
        return len(self.rates) == 1

    def curve_at(self, i: int, age: float) -> float:
        """SoH of curve ``i`` at ``age`` years: linear between years, extended along the last slope."""
        s = self.soh[i]
        last = len(s) - 1
        if age <= last:
            return float(np.interp(age, np.arange(len(s)), s))
        slope = s[-1] - s[-2]
        return max(0.0, float(s[-1] + slope * (age - last)))

    def at(self, age: float, cycles: float, days_per_year: float = 365.0) -> tuple[float, str | None]:
        """SoH after ``age`` years with ``cycles`` equivalent full cycles since commissioning, and a note when the
        average rate is outside the curves' range (then the nearest curve is used)."""
        if age <= 0:
            return 1.0, None
        if self.single:
            return self.curve_at(0, age), None
        rate = cycles / (days_per_year * age)
        note = None
        if rate < self.rates[0] or rate > self.rates[-1]:
            note = (f"{self.name}: average {rate:.2f} cycles/day after {age:g} years is outside the curves "
                    f"({self.rates[0]:g}-{self.rates[-1]:g}/day); the nearest curve is used")
            rate = min(max(rate, self.rates[0]), self.rates[-1])
        j = int(np.searchsorted(self.rates, rate))
        if self.rates[min(j, len(self.rates) - 1)] == rate:
            return self.curve_at(min(j, len(self.rates) - 1), age), note
        r1, r2 = self.rates[j - 1], self.rates[j]
        s1, s2 = self.curve_at(j - 1, age), self.curve_at(j, age)
        return float(s1 + (s2 - s1) * (rate - r1) / (r2 - r1)), note


@dataclass
class YearResult:
    year: int
    results: object                                     # Results of this year (shared between identical years)
    soh_start: dict[str, float]
    soh_end: dict[str, float]
    cycles: dict[str, float]
    cumulative: dict[str, float]
    notes: list[str] = field(default_factory=list)
    reused: bool = False                                # identical to an earlier year, not solved again


@dataclass
class LifetimeResults:
    years: list[YearResult]
    table: pd.DataFrame                                 # one row per year (and storage, if several degrade)
    eol: dict[str, float]
    retired: dict[str, int | None]                      # last operating year per degrading storage
    discount_rate: float = 0.0
    inflation: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def npv(self) -> float:
        return float(self.table.groupby("year")["profit_discounted"].first().sum())


def degrading_storages(cfg: dict) -> dict[str, DegradationCurves]:
    """Storages of a config that have SoH curves wired in: ``{name: curves}``."""
    out = {}
    for c in cfg.get("components", []):
        if c.get("type") == "Storage" and c.get("degradation"):
            out[c["name"]] = DegradationCurves.from_config(c["degradation"], c["name"])
    return out


def _year_cycles(res, name: str, nominal: float) -> float:
    if nominal <= 0:
        return 0.0
    e = res.energy()
    thr = sum(float(e.xs((name, port), level=("component", "port"), axis=1).to_numpy().sum())
              for port in ("charge", "discharge")) / 2
    return thr / nominal


def simulate_lifetime(cfg: dict, years: int, *, discount_rate: float = 0.0, inflation: float = 0.0, solve=None,
                      on_year=None) -> LifetimeResults:
    """Run ``cfg`` (its time horizon is the base period) for ``years`` consecutive years with degradation.

    ``solve(cfg) -> Results`` solves one year (default: ``from_config`` + ``EnergySystem.solve``); ``on_year(k,
    years, soh)`` is called before year ``k`` is solved (progress). Years whose storage capacities equal an
    earlier year's reuse its results instead of solving again (no degradation, or after retirement). With
    ``inflation`` every year's revenue and costs are the base period's times (1 + inflation) ** (year - 1) (a uniform
    price level does not change the dispatch, so it only scales the table).
    """
    from .config import from_config

    years = int(years)
    if years < 1:
        raise ValidationError("the simulation length must be at least 1 year")
    if solve is None:
        def solve(c):
            es, opts = from_config(c)
            return es.solve(**opts)
    curves = degrading_storages(cfg)
    comps = {c["name"]: c for c in cfg.get("components", [])}
    nominal = {n: float(comps[n].get("e_max", 0) or 0) for n in curves}
    soh = {n: 1.0 for n in curves}
    alive = {n: True for n in curves}
    cum = {n: 0.0 for n in curves}
    retired: dict[str, int | None] = {n: None for n in curves}
    warnings = [w for c in curves.values() for w in c.warnings]
    cache: dict[tuple, object] = {}
    out: list[YearResult] = []
    days = None
    for k in range(1, years + 1):
        if on_year:
            on_year(k, years, dict(soh))
        key = tuple((n, round(soh[n], 12), alive[n]) for n in sorted(curves))
        reused = key in cache
        if reused:
            res = cache[key]
        else:
            c = copy.deepcopy(cfg)
            for comp in c.get("components", []):
                n = comp.get("name")
                if n in curves:
                    comp.pop("degradation", None)
                    if alive[n]:
                        comp["e_max"] = nominal[n] * soh[n]
                    else:                       # retired: no capacity and no power (else it could burn energy)
                        comp["e_max"] = 0.0
                        comp["p_ch_max"] = 0.0
                        comp["p_dis_max"] = 0.0
            c.pop("lifetime", None)
            res = solve(c)
            res.release_model()                 # every year's results are kept: not every year's model
            cache[key] = res
        if days is None:
            days = float(np.sum(res.grid.dt)) / 24.0
        start = dict(soh)
        cyc, notes = {}, []
        for n, cv in curves.items():
            cyc[n] = _year_cycles(res, n, nominal[n]) if alive[n] else 0.0
            cum[n] += cyc[n]
            if alive[n]:
                value, note = cv.at(k, cum[n], days)
                if note:
                    notes.append(f"year {k}: {note}")
                soh[n] = min(soh[n], value)
                if soh[n] < cv.eol - 1e-12:
                    alive[n] = False
                    retired[n] = k
                    notes.append(f"year {k}: {n} reaches end of life (SoH {soh[n]:.3f} < {cv.eol:.3f}) and "
                                 f"retires; from year {k + 1} it has no capacity")
            else:
                soh[n] = 0.0
        out.append(YearResult(k, res, start, dict(soh), cyc, dict(cum), notes, reused))
        for n in curves:
            if not alive[n]:
                soh[n] = 0.0
    table = lifetime_table(out, nominal, discount_rate, days or 365.0, inflation)
    return LifetimeResults(out, table, {n: c.eol for n, c in curves.items()}, retired, discount_rate, inflation,
                           warnings)


def lifetime_table(years: list[YearResult], nominal: dict[str, float], discount_rate: float,
                   days: float, inflation: float = 0.0) -> pd.DataFrame:
    """One row per year (per degrading storage): SoH, capacity, cycles and the year's economics (nominal: the base
    period's times the year's price index (1 + inflation) ** (year - 1))."""
    rows = []
    cum_profit = 0.0
    for y in years:
        prof = y.results.profit()
        index = (1.0 + inflation) ** (y.year - 1)
        profit = -float(y.results.total_cost) * index
        revenue = (float(prof["revenue"].sum()) if "revenue" in prof else 0.0) * index
        cum_profit += profit
        df = 1.0 / (1.0 + discount_rate) ** y.year
        base = {"year": y.year, "price_index": index, "revenue": revenue, "costs": revenue - profit, "profit": profit,
                "profit_cumulative": cum_profit, "discount_factor": df, "profit_discounted": profit * df,
                "notes": "; ".join(y.notes)}
        stores = sorted(nominal) or [None]
        for n in stores:
            row = {"year": y.year, "storage": n}
            if n is not None:
                row.update({"soh_start": y.soh_start[n], "soh_end": y.soh_end[n],
                            "capacity_start_mwh": nominal[n] * y.soh_start[n], "cycles": y.cycles[n],
                            "cumulative_cycles": y.cumulative[n],
                            "avg_cycles_per_day": y.cumulative[n] / (days * y.year)})
            row.update({k: v for k, v in base.items() if k != "year"})
            rows.append(row)
    return pd.DataFrame(rows)

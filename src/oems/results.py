"""Solved-model results.

Everything here is read directly from model variables (``m.F``, block variables, ``m.C``,
``m.E``) and constraint duals; nothing is recomputed from the inputs.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyomo.environ as pyo

from .components.consumer import Demand, FlexibleDemand
from .components.converter import Converter
from .components.grid import GridConnection
from .components.producer import Producer
from .components.storage import Storage
from .components.thermal_zone import ThermalZone
from .costs import ARTIFICIAL, CostCategory
from .exceptions import OEMSError
from .model import SLACK_PREFIX


def _values(var_view, n) -> np.ndarray:
    out = np.empty(n)
    for t in range(n):
        v = var_view[t].value
        out[t] = 0.0 if v is None else v
    return out


class Results:
    """Results of one solve. Power in MW, energy in MWh, money in EUR, emissions in t."""

    def __init__(self, system, model, meta, info, validation):
        self.system = system
        self.model = model
        self.meta = meta
        self.info = info
        self.validation = validation
        self.grid = meta.grid
        self.index = self.grid.index
        n = self.grid.n
        m = model

        # flows on every edge
        cols, data = [], []
        for p in meta.ports:
            cols.append((p.component, p.port, p.bus, p.direction.value))
            data.append(_values(_PidView(m.F, p.pid), n))
        self.flows = pd.DataFrame(np.column_stack(data) if data else np.empty((n, 0)), index=self.index,
                                  columns=pd.MultiIndex.from_tuples(cols, names=["component", "port", "bus",
                                                                                 "direction"]))
        # optimised sizes (lifetime runs), written back into the components so the reporting below can use them
        self.sizes = {}
        for name, comp in system.components.items():
            solved = comp.solved_sizes(meta.blocks[name])
            if solved:
                self.sizes[name] = solved
        # states
        scols, sdata = [], []
        for name, comp in system.components.items():
            values = {sname: _values(var, n) for sname, var in comp.states(meta.blocks[name]).items()}
            values.update(comp.derived_states(values))          # e.g. storage soc = e / e_max
            for sname, v in values.items():
                scols.append((name, sname))
                sdata.append(v)
        self.states = pd.DataFrame(np.column_stack(sdata) if sdata else np.empty((n, 0)), index=self.index,
                                   columns=pd.MultiIndex.from_tuples(scols, names=["component", "state"]))
        # costs
        rows = [(c, k, pyo.value(m.C[c, k])) for (c, k) in m.CK]
        costs = pd.DataFrame(rows, columns=["component", "category", "EUR"])
        self.costs = (costs.pivot_table(index="component", columns="category", values="EUR", aggfunc="sum",
                                        fill_value=0.0) if len(costs) else pd.DataFrame())
        self._cost_rows = costs
        self.objective = info.objective
        # emissions
        erows = [(c, g, pyo.value(m.E[c, g])) for (c, g) in m.EK]
        em = pd.DataFrame(erows, columns=["component", "gas", "t"])
        self.emissions = (em.pivot_table(index="component", columns="gas", values="t", aggfunc="sum",
                                         fill_value=0.0) if len(em) else pd.DataFrame())
        self.emissions_t = pd.DataFrame(
            {g: [pyo.value(m.Et[g, t]) for t in range(n)] for g in meta.gases}, index=self.index)
        # duals
        wdt = self.grid.cw * self.grid.dt         # cost weights: undiscounted prices in a lifetime run
        duals = info.duals
        sp = {}
        for b in m.B:
            arr = np.array([duals.get(m.balance[b, t], np.nan) for t in range(n)], dtype=float)
            with np.errstate(divide="ignore", invalid="ignore"):
                sp[b] = np.where(wdt > 0, arr / wdt, np.nan)
        self.shadow_prices = pd.DataFrame(sp, index=self.index)
        self.emission_shadow_price = {
            g: -duals.get(m.emission_cap[g], np.nan) for g in meta.cap_gases}
        # slack
        srows = []
        for b in meta.slack_buses:
            pos = _values(_B2View(m.slack_pos, b), n)
            neg = _values(_B2View(m.slack_neg, b), n)
            for t in np.flatnonzero((pos > 1e-7) | (neg > 1e-7)):
                srows.append((b, self.index[t], pos[t] * self.grid.dt[t], neg[t] * self.grid.dt[t]))
        self.slack = pd.DataFrame(srows, columns=["bus", "time", "shortfall_MWh", "excess_MWh"])
        # billing-period variables (peaks)
        prow = []
        for name, comp in system.components.items():
            if hasattr(comp, "periodic"):
                for vname, var in comp.periodic(meta.blocks[name]).items():
                    for k in var:
                        prow.append((name, vname, k, var[k].value or 0.0))
        self.periodic = pd.DataFrame(prow, columns=["component", "variable", "period", "value"])

    # ------------------------------------------------------------------ summaries
    @property
    def status(self) -> str:
        return self.info.status.value

    @property
    def solve_time(self) -> float:
        return self.info.solve_time

    @property
    def build_time(self) -> float:
        return self.meta.build_time

    @property
    def total_cost(self) -> float:
        """Objective without the artificial TIEBREAK and SLACK categories."""
        rows = self._cost_rows
        return float(rows.loc[~rows["category"].isin([c.value for c in ARTIFICIAL]), "EUR"].sum())

    def step_costs(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """The objective's cost terms split by time step, read from the solved model's own cost expressions.

        Returns ``(per_step, period)``: ``per_step`` is time x (component, category) in EUR (revenue negative), built
        from every term whose variable belongs to a time step (energy charges and credits, flow, operation,
        degradation, flexibility and CO2 costs, tie-break, slack); ``period`` lists the rest (component, category,
        EUR): charges per billing period or interval such as peak, capacity-overrun and fixed charges. For every
        (component, category): ``per_step.sum() + period == costs`` (up to rounding).
        """
        if getattr(self, "_step_costs", None) is not None:
            return self._step_costs
        from pyomo.repn import generate_standard_repn

        m, n = self.model, self.grid.n
        cols, data, period = [], [], []
        for (c, k), expr in self.meta.cost_exprs.items():
            per = np.zeros(n)
            rest = 0.0
            repn = generate_standard_repn(expr, compute_values=True)
            rest += float(pyo.value(repn.constant)) if repn.constant is not None else 0.0
            for coef, var in zip(repn.linear_coefs, repn.linear_vars, strict=True):
                v = (var.value or 0.0) * float(coef)
                t = _step_of(var, m.T)
                if t is None:
                    rest += v
                else:
                    per[t] += v
            cols.append((c, k))
            data.append(per)
            if abs(rest) > 1e-9:
                period.append((c, k, rest))
        per_step = pd.DataFrame(np.column_stack(data) if data else np.empty((n, 0)), index=self.index,
                                columns=pd.MultiIndex.from_tuples(cols, names=["component", "category"]))
        self._step_costs = (per_step, pd.DataFrame(period, columns=["component", "category", "EUR"]))
        return self._step_costs

    def profit(self) -> pd.DataFrame:
        """System profit per time step [EUR] = revenue - costs, consistent with ``total_cost``: the per-step part
        of every real cost category (the artificial TIEBREAK and SLACK left out). Columns: ``revenue``, one
        ``cost <CATEGORY>`` per other category with step-level terms, ``profit`` and ``profit_cumulative``.
        Charges per billing period (peak, fixed) are not in the steps: ``step_costs()[1]``; so
        ``profit.sum() - period charges == -total_cost``."""
        per_step, _ = self.step_costs()
        real = per_step.loc[:, ~per_step.columns.get_level_values("category").isin([c.value for c in ARTIFICIAL])]
        by_cat = real.T.groupby(level="category").sum().T
        out = pd.DataFrame(index=self.index)
        if "REVENUE" in by_cat:
            out["revenue"] = -by_cat.pop("REVENUE")
        for cat in by_cat.columns:
            out[f"cost {cat}"] = by_cat[cat]
        out["profit"] = -real.sum(axis=1)
        out["profit_cumulative"] = out["profit"].cumsum()
        return out + 0.0

    #: Port pairs that move energy in opposite directions; their ``exclusive`` option is an LP relaxation, so both
    #: can be non-zero in the same step when that lowers cost (e.g. burning energy at negative prices)
    OPPOSITE_PORTS = ((GridConnection, ("import", "export")), (Storage, ("charge", "discharge")))

    def simultaneous(self, tol: float = 1e-6) -> pd.DataFrame:
        """Overlap of opposite flows per step [MW] = min(import, export) for grid connections and min(charge,
        discharge) for storages; time x (component, "a & b"). Zero where only one direction flows (below ``tol``)."""
        cols, data = [], []
        flows = self.flows.sort_index(axis=1)
        for name, comp in self.system.components.items():
            for cls, (a, b) in self.OPPOSITE_PORTS:
                if isinstance(comp, cls):
                    ov = np.minimum(flows[(name, a)].iloc[:, 0].to_numpy(), flows[(name, b)].iloc[:, 0].to_numpy())
                    cols.append((name, f"{a} & {b}"))
                    data.append(np.where(ov > tol, ov, 0.0))
        return pd.DataFrame(np.column_stack(data) if data else np.empty((self.grid.n, 0)), index=self.index,
                            columns=pd.MultiIndex.from_tuples(cols, names=["component", "ports"]))

    @property
    def cost_by_category(self) -> pd.Series:
        return self._cost_rows.groupby("category")["EUR"].sum()

    def energy(self) -> pd.DataFrame:
        """Energy per edge per step (MWh) = flow * dt."""
        return self.flows.mul(self.grid.dt, axis=0)

    @property
    def edges(self) -> pd.DataFrame:
        """Tidy long table: time, component, port, bus, carrier, direction, MW, MWh."""
        long = self.flows.stack(level=[0, 1, 2, 3], future_stack=True).rename("MW").reset_index()
        long = long.rename(columns={long.columns[0]: "time"})
        dt = pd.Series(self.grid.dt, index=self.index)
        long["MWh"] = long["MW"] * long["time"].map(dt).to_numpy()
        long["carrier"] = long["bus"].map({b: bus.carrier.name for b, bus in self.system.buses.items()})
        return long[["time", "component", "port", "bus", "carrier", "direction", "MW", "MWh"]]

    def flow_matrix(self, bus: str | None = None) -> pd.DataFrame:
        """Horizon energy (MWh) on every edge, optionally only edges of one bus."""
        e = self.energy().sum()
        df = e.rename("MWh").reset_index()
        return df if bus is None else df[df["bus"] == bus].reset_index(drop=True)

    def component(self, name: str) -> dict:
        if name not in self.system.components:
            raise KeyError(name)
        states = self.states[name] if name in self.states.columns.get_level_values(0) else pd.DataFrame()
        costs = self._cost_rows[self._cost_rows["component"] == name].set_index("category")["EUR"]
        return {"flows": self.flows[name], "states": states, "costs": costs}

    def balance(self, bus: str) -> pd.DataFrame:
        """Signed injections into ``bus`` per component (+ slack); rows sum to zero."""
        sub = self.flows.xs(bus, level="bus", axis=1)
        out = {}
        for (comp, port, direction) in sub.columns:
            sign = 1.0 if direction == "out" else -1.0
            key = f"{comp}.{port}"
            out[key] = sign * sub[(comp, port, direction)]
        df = pd.DataFrame(out, index=self.index)
        if bus in self.meta.slack_buses:
            df["slack"] = self._slack_net(bus)
        return df

    def _slack_net(self, bus: str) -> np.ndarray:
        kept = getattr(self, "_slack", None)
        if kept is not None:
            return kept[bus]
        n = self.grid.n
        return _values(_B2View(self.model.slack_pos, bus), n) - _values(_B2View(self.model.slack_neg, bus), n)

    def release_model(self) -> None:
        """Drop the Pyomo model, which holds most of the memory of a large run (about 18 kB per time step: 3 GB for
        20 hourly years), once the reporting has what it reads from it: the per-step costs and the slack per bus.
        The solver's duals go too - they are keyed by the model's constraints and would keep it alive."""
        if self.model is None:
            return
        self.step_costs()
        self._slack = {b: self._slack_net(b) for b in self.meta.slack_buses}
        self.model = None
        self.meta.cost_exprs = {}
        self.meta.blocks = {}
        self.info.duals = {}

    def kpis(self) -> pd.DataFrame:
        """Key indicators as a tidy table (kpi, scope, value, unit)."""
        rows = []
        energy = self.energy().sum().sort_index()
        flows = self.flows.sort_index(axis=1)
        sys = self.system
        for name, comp in sys.components.items():
            if isinstance(comp, GridConnection):
                imp, exp = energy[(name, "import")].sum(), energy[(name, "export")].sum()
                rows += [("import_energy", name, imp, "MWh"), ("export_energy", name, exp, "MWh"),
                         ("max_import", name, flows[(name, "import")].max().max(), "MW")]
                pk = self.periodic[self.periodic["component"] == name]
                if len(pk):
                    rows.append(("max_billed_peak", name, pk["value"].max(), "MW"))
            elif isinstance(comp, Producer):
                prod = energy[(name, "out")].sum()
                rows.append(("production", name, prod, "MWh"))
                if (name, "curtailment") in self.states.columns:
                    rows.append(("curtailment", name, float((self.states[(name, "curtailment")]
                                                             * self.grid.dt).sum()), "MWh"))
            elif isinstance(comp, Storage):
                dis = energy[(name, "discharge")].sum()
                rows.append(("discharged", name, dis, "MWh"))
                if comp.r.e_max > 0:
                    rows.append(("equivalent_cycles", name, dis / comp.r.e_max, "-"))
                    # cycles as max_cycles counts them, on stored energy:
                    # (eta_ch * charged + discharged / eta_dis) / 2 / e_max, total and busiest period
                    steps = self.energy()
                    port_mwh = {port: steps.xs((name, port), level=("component", "port"), axis=1).sum(axis=1)
                                for port in ("charge", "discharge")}
                    thr = (comp.r.eta_ch * port_mwh["charge"] + port_mwh["discharge"] / comp.r.eta_dis) / 2
                    rows.append(("cycles_throughput", name, float(thr.sum()) / comp.r.e_max, "-"))
                    period = comp.r.cycle_period or "D"
                    per = thr.groupby(self.grid.groups(period)).sum() / comp.r.e_max
                    rows.append((f"max_cycles_per_{ {'D': 'day', 'W': 'week', 'M': 'month', 'Y': 'year'}[period]}",
                                 name, float(per.max()), "-"))
                soc = self.states[(name, "soc")].to_numpy() * 100
                for kpi, v in (("soc_min", soc.min()), ("soc_mean", soc.mean()), ("soc_max", soc.max()),
                               ("soc_end", soc[-1])):
                    rows.append((kpi, name, float(v), "%"))
            elif isinstance(comp, Converter):
                cap = flows[(name, comp.r.cap_port)].iloc[:, 0]
                pmax = np.max(comp.r.p_max)
                e_cap = float((cap * self.grid.dt).sum())
                rows.append(("energy_on_capacity_port", name, e_cap, "MWh"))
                if np.isfinite(pmax) and pmax > 0:
                    rows.append(("full_load_hours", name, e_cap / pmax, "h"))
        # steps with opposite flows at once (import & export, charge & discharge)
        for (name, _ports), ov in self.simultaneous().items():
            rows += [("simultaneous_steps", name, int((ov > 0).sum()), "steps"),
                     ("simultaneous_energy", name, float((ov * self.grid.dt).sum()), "MWh")]
        # electricity self-consumption / self-sufficiency
        el_buses = [b for b, bus in sys.buses.items() if bus.carrier.kind == "electricity"]
        prod = sum(energy[(c, "out")].sum() for c, comp in sys.components.items()
                   if isinstance(comp, Producer) and comp.r.bus.name in el_buses)
        exp = sum(energy[(c, "export")].sum() for c, comp in sys.components.items()
                  if isinstance(comp, GridConnection) and comp.r.bus.name in el_buses)
        imp = sum(energy[(c, "import")].sum() for c, comp in sys.components.items()
                  if isinstance(comp, GridConnection) and comp.r.bus.name in el_buses)
        if prod > 0:
            used = max(prod - exp, 0.0)
            rows.append(("self_consumption", "electricity", used / prod, "-"))
            if used + imp > 0:
                rows.append(("self_sufficiency", "electricity", used / (used + imp), "-"))
        # heat electrification share
        heat_buses = {b for b, bus in sys.buses.items() if bus.carrier.kind == "heat"}
        if heat_buses:
            total, electric = 0.0, 0.0
            for (c, _port, bus, direction), val in energy.items():
                if bus not in heat_buses or direction != "out":
                    continue
                comp = sys.components[c]
                if isinstance(comp, Storage):
                    continue  # storage shifts heat, it does not generate it
                if isinstance(comp, Converter) and all(b.name in heat_buses for b in comp.r.inputs):
                    continue  # heat-to-heat links (cascades)
                total += val
                if isinstance(comp, Converter) and any(b.carrier.kind == "electricity" for b in comp.r.inputs):
                    electric += val
            if total > 0:
                rows.append(("heat_electrification_share", "heat", electric / total, "-"))
        # delivered energy and emission intensity
        delivered = 0.0
        for c, comp in sys.components.items():
            if isinstance(comp, (Demand, FlexibleDemand, ThermalZone)):
                delivered += energy[c].sum()
        for gas in self.meta.gases:
            tot = float(self.emissions[gas].sum()) if gas in self.emissions.columns else 0.0
            rows.append((f"emissions_{gas}", "system", tot, "t"))
            if delivered > 0:
                rows.append((f"{gas}_intensity", "delivered", tot / delivered, "t/MWh"))
        rows.append(("delivered_energy", "system", delivered, "MWh"))
        rows.append(("total_cost", "system", self.total_cost, "EUR"))
        for b in self.shadow_prices:
            outs = self.flows.xs(b, level="bus", axis=1)
            w = outs.loc[:, outs.columns.get_level_values("direction") == "in"].sum(axis=1)
            sp = self.shadow_prices[b]
            if w.sum() > 0 and sp.notna().all():
                rows.append(("avg_marginal_price", b, float((sp * w).sum() / w.sum()), "EUR/MWh"))
        return pd.DataFrame(rows, columns=["kpi", "scope", "value", "unit"])

    # ------------------------------------------------------------------ resampling and export
    def resample(self, freq: str = "1h") -> SimpleResampled:
        """Aggregate results to a coarser resolution for reporting/comparison."""
        if not self.grid.is_datetime:
            raise OEMSError("resample needs a DatetimeIndex grid")
        dt = pd.Series(self.grid.dt, index=self.index)
        energy = self.energy().resample(freq).sum()
        hours = dt.resample(freq).sum()
        return SimpleResampled(
            flows=energy.div(hours, axis=0),
            energy=energy,
            states=self.states.resample(freq).last(),
            shadow_prices=self.shadow_prices.resample(freq).mean(),
        )

    def to_csv(self, folder) -> Path:
        """Write tidy CSV tables and metadata.json to ``folder``."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self.edges.to_csv(folder / "flows.csv", index=False)
        st = self.states.stack(level=[0, 1], future_stack=True).rename("value").reset_index()
        st.columns = ["time", "component", "state", "value"]
        st.to_csv(folder / "states.csv", index=False)
        self._cost_rows.to_csv(folder / "costs.csv", index=False)
        self.shadow_prices.rename_axis("time").to_csv(folder / "shadow_prices.csv")
        if len(self.emissions):
            self.emissions.stack().rename("t").reset_index().to_csv(folder / "emissions.csv", index=False)
        self.emissions_t.rename_axis("time").to_csv(folder / "emissions_t.csv")
        self.kpis().to_csv(folder / "kpis.csv", index=False)
        self.periodic.to_csv(folder / "periodic.csv", index=False)
        self.slack.to_csv(folder / "slack.csv", index=False)
        meta = {
            "status": self.status,
            "objective": self.objective,
            "total_cost": self.total_cost,
            "build_time_s": self.build_time,
            "solve_time_s": self.solve_time,
            "steps": self.grid.n,
            "resolution": self.grid.freq,
            "start": str(self.index[0]),
            "end": str(self.index[-1]),
            "warnings": self.validation.warnings,
        }
        (folder / "metadata.json").write_text(json.dumps(meta, indent=2, default=str))
        return folder

    def summary(self) -> str:
        lines = [f"status={self.status} objective={self.objective:,.2f} EUR total_cost={self.total_cost:,.2f} EUR",
                 f"build {self.build_time:.2f}s solve {self.solve_time:.2f}s steps={self.grid.n} ({self.grid.freq})"]
        for cat, val in self.cost_by_category.items():
            lines.append(f"  {cat:<12} {val:>14,.2f} EUR")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"Results(status={self.status}, objective={self.objective})"


class SimpleResampled:
    def __init__(self, flows, energy, states, shadow_prices):
        self.flows = flows
        self.energy = energy
        self.states = states
        self.shadow_prices = shadow_prices


class _PidView:
    def __init__(self, F, pid):
        self.F, self.pid = F, pid

    def __getitem__(self, t):
        return self.F[self.pid, t]


class _B2View:
    def __init__(self, var, b):
        self.var, self.b = var, b

    def __getitem__(self, t):
        return self.var[self.b, t]


__all__ = ["Results", "CostCategory", "SLACK_PREFIX"]


def _step_of(var, T) -> int | None:
    """Time step of a model variable indexed by the time set (``x[t]`` or ``x[..., t]``), else None."""
    iset = var.parent_component().index_set()
    if iset is T:
        return int(var.index())
    subsets = list(iset.subsets()) if hasattr(iset, "subsets") else []
    if len(subsets) > 1 and subsets[-1] is T:
        return int(var.index()[-1])
    return None

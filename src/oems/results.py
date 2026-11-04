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
        # states
        scols, sdata = [], []
        for name, comp in system.components.items():
            for sname, var in comp.states(meta.blocks[name]).items():
                scols.append((name, sname))
                sdata.append(_values(var, n))
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
        wdt = self.grid.w * self.grid.dt
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
            n = self.grid.n
            df["slack"] = (_values(_B2View(self.model.slack_pos, bus), n)
                           - _values(_B2View(self.model.slack_neg, bus), n))
        return df

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
            elif isinstance(comp, Converter):
                cap = flows[(name, comp.r.cap_port)].iloc[:, 0]
                pmax = np.max(comp.r.p_max)
                e_cap = float((cap * self.grid.dt).sum())
                rows.append(("energy_on_capacity_port", name, e_cap, "MWh"))
                if np.isfinite(pmax) and pmax > 0:
                    rows.append(("full_load_hours", name, e_cap / pmax, "h"))
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

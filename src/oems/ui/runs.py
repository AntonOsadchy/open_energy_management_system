"""Background solve runs for the UI.

A local run is solved in its own worker process (:func:`_run_in_process`): **Stop** ends that process at once, wherever
the run is (building or solving), and all of its memory goes back when it ends. The process reports status and
progress through a pipe and finally sends the run's results (pickled, Pyomo models released). A cloud run
(``compute: cloud``) waits on the cloud machines from a thread here; Stop cancels them
(``cloud.solve_remote(cancel=)``).
"""

from __future__ import annotations

import io
import json
import multiprocessing as mp
import pickle
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import from_config
from ..degradation import simulate_lifetime
from ..exceptions import InfeasibleModelError, OEMSError, RunCancelled, ValidationError
from ..schema import specs
from ..sizing import optimise_lifetime
from .graph import map_errors


@dataclass
class Run:
    id: str
    status: str = "queued"          # queued | building | solving | done | failed | cancelled
    message: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    results: object = None
    node_errors: dict = field(default_factory=dict)
    name_to_node: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)
    lifetime: object = None         # degradation.LifetimeResults of a multi-year run (results = year 1)
    year: int = 1                   # the year this view shows (see year_view)
    progress: dict = field(default_factory=lambda: {"year": 1, "years": 1})   # year being solved (run status)
    sizing: object = None           # sizing.SizingResults of a lifetime optimisation (results = the sized run)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)   # set by RunManager.cancel
    process: object = field(default=None, repr=False)      # the worker process of a local run

    #: what a worker process sends back at the end
    FINAL = ("status", "message", "node_errors", "results", "lifetime", "sizing", "progress", "finished")

    def __getstate__(self):             # sent to / from worker processes without the Event and the process
        return {k: v for k, v in self.__dict__.items() if k not in ("cancel", "process")}

    def __setstate__(self, state):
        self.__dict__.update(state, cancel=threading.Event(), process=None)


class RunManager:
    def __init__(self, keep: int = 5, processes: bool = True):
        self.cloud_runner = None        # replaces the cloud call (tests: cloud.run_locally)
        self.processes = processes      # local runs in a worker process (False: in a thread here, cannot be stopped)
        self.runs: dict[str, Run] = {}
        self.keep = keep
        self._lock = threading.Lock()

    def start(self, cfg: dict, name_to_node: dict) -> Run:
        run = Run(id=uuid.uuid4().hex[:10], name_to_node=name_to_node, config=cfg)
        with self._lock:
            self.runs[run.id] = run
            for old in list(self.runs)[:-self.keep]:
                self.runs.pop(old, None)
        local = cfg.get("compute") != "cloud" and self.processes
        threading.Thread(target=self._run_in_process if local else self._work, args=(run, cfg), daemon=True).start()
        return run

    def cancel(self, run_id: str) -> Run | None:
        """Stop a run: its worker process ends now; a cloud run's machines are cancelled."""
        run = self.runs.get(run_id)
        if run is None or run.status in ("done", "failed", "cancelled"):
            return run
        run.cancel.set()
        run.message = "stopping"
        if run.process is not None and run.process.is_alive():
            run.process.terminate()
        return run

    def _run_in_process(self, run: Run, cfg: dict):
        """Solve a local run in a worker process, mirroring its status here until it sends its results."""
        ctx = mp.get_context("spawn")
        receiver, sender = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=_worker, args=(cfg, run.name_to_node, sender), daemon=True, name=f"oems-run-{run.id}")
        run.status, run.process = "building", proc
        proc.start()
        sender.close()
        final = None
        try:
            while final is None:
                if receiver.poll(0.3):
                    msg = receiver.recv()
                    if msg[0] == "progress":
                        run.status, run.message, run.progress = msg[1], msg[2], msg[3]
                    else:
                        final = pickle.loads(msg[1])
                elif not proc.is_alive() and not receiver.poll(0):
                    break
        except (EOFError, OSError):         # the process ended (stopped) while sending
            pass
        proc.join(timeout=5)
        if final is not None:
            for k, v in final.items():
                setattr(run, k, v)
        elif run.cancel.is_set():
            run.status, run.message = "cancelled", "stopped by user"
        else:
            run.status = "failed"
            run.message = f"the solver process ended unexpectedly (exit code {proc.exitcode}; out of memory?)"
        run.process = None
        run.finished = run.finished or time.time()

    def _work(self, run: Run, cfg: dict):
        try:
            run.status = "building"
            if cfg.get("compute") == "cloud":
                self._work_cloud(run, cfg)
                return
            if (cfg.get("lifetime") or {}).get("mode") == "optimise":
                self._work_sizing(run, cfg)
                return
            if cfg.get("lifetime"):
                self._work_lifetime(run, cfg)
                return
            es, opts = from_config(cfg)
            run.status = "solving"
            run.results = es.solve(time_limit=opts.get("time_limit"), tie_break=opts.get("tie_break", 0.01))
            run.status = "done"
            run.message = f"optimal, total cost {run.results.total_cost:,.0f} EUR"
        except ValidationError as e:
            run.status, run.message = "failed", "validation failed"
            run.node_errors = map_errors(e.errors, run.name_to_node)
        except InfeasibleModelError as e:
            run.status, run.message = "failed", str(e)
            if e.report is not None and len(e.report):
                for bus, grp in e.report.groupby("bus"):
                    nid = run.name_to_node.get(bus, "_global")
                    run.node_errors.setdefault(nid, []).append(
                        f"balance fails in {len(grp)} steps (short {grp['shortfall_MWh'].sum():.3g} MWh, "
                        f"excess {grp['excess_MWh'].sum():.3g} MWh), first at {grp['time'].iloc[0]}")
        except RunCancelled:
            run.status, run.message = "cancelled", "stopped by user"
        except OEMSError as e:
            run.status, run.message = "failed", str(e)
        except Exception as e:  # pragma: no cover - unexpected
            run.status, run.message = "failed", f"{type(e).__name__}: {e}"
            run.node_errors["_global"] = [traceback.format_exc(limit=3)]
        finally:
            run.finished = time.time()

    def _work_lifetime(self, run: Run, cfg: dict):
        """Multi-year run: the base period repeated ``lifetime.years`` times with battery degradation."""
        life = cfg["lifetime"]

        def solve(c):
            es, opts = from_config(c)
            return es.solve(time_limit=opts.get("time_limit"), tie_break=opts.get("tie_break", 0.01))

        def progress(k, n, soh):
            run.status = "solving"
            run.progress = {"year": k, "years": n}
            worn = ", ".join(f"{name} SoH {v:.1%}" for name, v in soh.items())
            run.message = f"year {k} of {n}" + (f" ({worn})" if worn else "")

        run.progress = {"year": 1, "years": int(life["years"])}
        lt = simulate_lifetime(cfg, life["years"], discount_rate=float(life.get("discount_rate") or 0),
                               inflation=float(life.get("inflation") or 0), solve=solve, on_year=progress)
        run.lifetime, run.results = lt, lt.years[0].results
        run.status = "done"
        total = float(lt.table.groupby("year")["profit"].first().sum())
        retired = "; ".join(f"{n} retires after year {y}" for n, y in lt.retired.items() if y)
        run.message = (f"{len(lt.years)} years, profit {total:,.0f} EUR"
                       + (f", NPV {lt.npv:,.0f} EUR" if lt.discount_rate else "") + (f"; {retired}" if retired else ""))

    def _work_sizing(self, run: Run, cfg: dict):
        """Lifetime optimisation: the base period repeated ``lifetime.years`` times in one model, optimised sizes,
        then the reference case without the investment (for NPV, IRR and payback)."""
        life = cfg["lifetime"]

        def stage(k, n, label):
            run.status = "solving"
            run.progress = {"stage": k, "stages": n, "years": int(life["years"])}   # one model, not year by year
            run.message = f"{label} ({k} of {n})"

        sr = optimise_lifetime(cfg, on_stage=stage)
        self._sizing_done(run, sr)

    @staticmethod
    def _sizing_done(run: Run, sr):
        run.sizing, run.results = sr, sr.results
        run.status = "done"
        k = sr.kpis
        run.message = (f"{k['years']} years, NPV {k['npv']:,.0f} EUR"
                       + (f" vs '{k['reference']}'" if k.get("reference") else "")
                       + (f", IRR {100 * k['irr']:.1f} %" if k["irr"] is not None else "")
                       + (f", payback {k['payback']:.1f} years" if k["payback"] is not None else ", no payback")
                       + (f", self-sufficiency {100 * k['self_sufficiency']:.1f} %"
                          f" ({100 * (k.get('self_sufficiency_reference') or 0):.1f} % without the investment)"
                          if k.get("self_sufficiency") is not None else ""))
        binding = [h["name"] for h in k.get("hurdles", []) if h["binding"]]
        if k.get("self_sufficiency_binding"):
            binding.insert(0, f"self-sufficiency >= {100 * k['self_sufficiency_target']:g} %")
        if binding:
            run.message += f"; limited by {', '.join(binding)}"
        elif any(w.startswith("no investment meets") for w in sr.warnings):
            run.message += "; no investment meets the hurdles"

    def _work_cloud(self, run: Run, cfg: dict):
        """The solve on a cloud machine (``cloud.solve_remote``); single period or lifetime optimisation."""
        from ..cloud import solve_remote

        life = cfg.get("lifetime") or {}
        sizing = life.get("mode") == "optimise"
        run.status = "solving"
        run.progress = {"year": 1, "years": int(life.get("years", 1)), "cloud": True}
        run.message = "starting a cloud machine"

        def stage(k, n, label):
            if sizing:      # both models on two machines at once (one stage), or one after the other (hurdles)
                run.progress = {"stage": k, "stages": n, "years": int(life["years"]), "cloud": True,
                                "parallel": n == 1}
            run.message = f"{label} · cloud" if sizing else "solving · cloud"

        out = solve_remote(cfg, on_stage=stage, run=self.cloud_runner, cancel=run.cancel)
        stats = getattr(out, "cloud_stats", None) or {}
        peak = max((s.get("peak_mb") or 0) for s in stats) if isinstance(stats, list) else stats.get("peak_mb")
        where = f" (cloud, peak {peak / 1024:.1f} GB)" if peak else " (cloud)"
        if sizing:
            self._sizing_done(run, out)
            run.message += where
            return
        run.results = out
        run.status = "done"
        run.message = f"optimal, total cost {out.total_cost:,.0f} EUR{where}"

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)


def _worker(cfg: dict, name_to_node: dict, conn) -> None:
    """A local run in its own process: solves ``cfg`` as :meth:`RunManager._work` does, sends ``("progress", status,
    message, progress)`` whenever they change and finally ``("final", pickled Run fields)``."""
    run = Run(id="worker", name_to_node=name_to_node, config=cfg)
    stop = threading.Event()

    def report():
        last = None
        while not stop.wait(0.3):
            now = (run.status, run.message, dict(run.progress))
            if now != last:
                conn.send(("progress", *now))
                last = now

    reporter = threading.Thread(target=report, daemon=True)
    reporter.start()
    RunManager(processes=False)._work(run, cfg)
    stop.set()
    reporter.join()
    if run.results is not None:
        run.results.release_model()     # the model stays here; the results go back
    conn.send(("final", pickle.dumps({k: getattr(run, k) for k in Run.FINAL})))
    conn.close()


def years_of(run: Run) -> int:
    return len(run.lifetime.years) if run.lifetime else 1


def year_view(run: Run, year: int | None) -> Run:
    """The run as seen for one year of a multi-year run (same run for an ordinary one); KeyError for no such year."""
    if not year or run.lifetime is None:
        if year not in (None, 1):
            raise KeyError(year)
        return run
    if not 1 <= int(year) <= years_of(run):
        raise KeyError(year)
    return replace(run, results=run.lifetime.years[int(year) - 1].results, year=int(year))


def sizing_summary(run: Run) -> dict | None:
    """Optimal sizes, investment KPIs and the yearly cash flows of a lifetime optimisation (None otherwise)."""
    sr = run.sizing
    if sr is None:
        return None
    table = sr.table.replace({np.nan: None})
    return {"kpis": {k: (_json_num(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                     for k, v in sr.kpis.items()},
            "sizes": [{**s, "value": _json_num(s["value"]), "node": run.name_to_node.get(s["component"])}
                      for s in sr.sizes],
            "warnings": sr.warnings, "columns": list(table.columns),
            "rows": [[_json_num(v) if isinstance(v, (int, float, np.floating, np.integer)) and not isinstance(v, bool)
                      else v for v in row] for row in table.itertuples(index=False)]}


def lifetime_summary(run: Run) -> dict | None:
    """The per-year table and headline figures of a multi-year run (None for an ordinary run)."""
    lt = run.lifetime
    if lt is None:
        return None
    table = lt.table.replace({np.nan: None})
    return {"years": len(lt.years), "discount_rate": lt.discount_rate, "inflation": lt.inflation,
            "npv": _json_num(lt.npv),
            "profit": _json_num(lt.table.groupby("year")["profit"].first().sum()), "eol": lt.eol,
            "retired": lt.retired, "warnings": lt.warnings,
            "notes": [n for y in lt.years for n in y.notes],
            "columns": list(table.columns),
            "rows": [[_json_num(v) if isinstance(v, (int, float, np.floating, np.integer)) and not isinstance(v, bool)
                      else v for v in row] for row in table.itertuples(index=False)]}


def summary_rows(summary: dict) -> pd.DataFrame:
    """The Summary tab as a table: profit, costs by category, per-period charges and KPIs (item, scope, value, unit)."""
    p = summary.get("profit") or {}
    rows = [("Profit", "", p.get("total"), "EUR"), ("Revenue", "", p.get("revenue"), "EUR"),
            ("Total cost", "", summary.get("total_cost"), "EUR")]
    rows += [(f"cost {k}", "", v, "EUR") for k, v in (summary.get("by_category") or {}).items()]
    rows += [(f"{c['category'].lower()} charge (per period)", c["component"], c["EUR"], "EUR")
             for c in p.get("period_charges", [])]
    rows += [(k["kpi"], k["scope"], k["value"], k["unit"]) for k in summary.get("kpis", [])]
    return pd.DataFrame(rows, columns=["item", "scope", "value", "unit"])


EXPORT_VIEWS = ("summary", "costs", "lifetime", "sizing")


def export_tables(summary: dict, view: str) -> dict[str, pd.DataFrame]:
    """A results tab built from the summary (``EXPORT_VIEWS``) as {sheet name: table}; the first is the CSV one."""
    if view == "summary":
        return {"Summary": summary_rows(summary)}
    if view == "costs":
        costs = pd.DataFrame(summary["costs"], columns=["component", "category", "EUR"])
        return {"Costs": costs[costs["category"] != "TIEBREAK"]}
    if view == "sizing":
        sz = summary.get("sizing")
        if not sz:
            raise KeyError(view)
        sizes = pd.DataFrame(sz["sizes"]).drop(columns=["node"], errors="ignore")
        kpis = pd.DataFrame([(k, v) for k, v in sz["kpis"].items()], columns=["item", "value"])
        return {"Cash flows": pd.DataFrame(sz["rows"], columns=sz["columns"]), "Sizes": sizes, "Investment": kpis}
    life = summary.get("lifetime")
    if view != "lifetime" or not life:
        raise KeyError(view)
    rows = [("years", life["years"], ""), ("discount rate", life["discount_rate"], ""),
            ("inflation", life.get("inflation", 0), ""),
            ("profit (sum of years)", life["profit"], "EUR")]
    if life["discount_rate"]:
        rows.append(("NPV", life["npv"], "EUR"))
    rows += [(f"{n} end of life SoH", v, "") for n, v in (life.get("eol") or {}).items()]
    rows += [(f"{n} retires after year", v, "") for n, v in (life.get("retired") or {}).items() if v]
    return {"Lifetime": pd.DataFrame(life["rows"], columns=life["columns"]),
            "Summary": pd.DataFrame(rows, columns=["item", "value", "unit"])}


def tables_file(sheets: dict[str, pd.DataFrame], format: str) -> tuple[bytes, str]:
    """``export_tables`` as CSV (first table) or Excel (one sheet each): (file bytes, media type)."""
    if format == "csv":
        return next(iter(sheets.values())).to_csv(index=False, float_format="%.10g").encode(), "text/csv"
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for name, df in sheets.items():
            df.to_excel(xl, sheet_name=name, index=False)
            xl.sheets[name].freeze_panes = "A2"
            xl.sheets[name].column_dimensions["A"].width = 28
    return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _json_num(x):
    if x is None:
        return None
    x = float(x)
    return None if not np.isfinite(x) else round(x, 6)


def summary(run: Run) -> dict:
    res = run.results
    edges = []
    for (comp, port, bus, direction), mwh in res.energy().sum().items():
        edges.append({"component": comp, "port": port, "bus": bus, "direction": direction, "MWh": _json_num(mwh),
                      "node": run.name_to_node.get(comp), "bus_node": run.name_to_node.get(bus)})
    rows = res._cost_rows
    by_comp = rows.groupby("component")["EUR"].sum()
    costs = [{"component": r.component, "category": r.category, "EUR": _json_num(r.EUR)} for r in rows.itertuples()]
    badges = {}
    for name, nid in run.name_to_node.items():
        if name in by_comp.index:
            badges[nid] = {"cost": _json_num(by_comp[name])}
    k = res.kpis()
    for r in k.itertuples():
        nid = run.name_to_node.get(r.scope)
        if nid and r.kpi in ("full_load_hours", "equivalent_cycles", "production", "max_billed_peak", "soc_min",
                             "soc_max"):
            badges.setdefault(nid, {})[r.kpi] = _json_num(r.value)
    notices = []
    sim = res.simultaneous()
    for (comp, ports), ov in sim.items():
        steps = ov > 0
        if not steps.any():
            continue
        a, b = ports.split(" & ")
        nid = run.name_to_node.get(comp)
        if nid:
            badges.setdefault(nid, {})["simultaneous"] = int(steps.sum())
        notices.append(f"{comp}: {a} and {b} at the same time in {int(steps.sum())} of {res.grid.n} steps "
                       f"({float((ov * res.grid.dt).sum()):.3g} MWh, up to {float(ov.max()):.3g} MW; first "
                       f"{str(ov.index[steps.to_numpy().argmax()])[:16]}). The 'exclusive' option is a linear "
                       f"relaxation, so the optimiser does this when it lowers cost, e.g. to burn energy at negative "
                       f"prices. See 🔍 Inspect → Checks.")
    if run.sizing is not None:
        for s in run.sizing.sizes:
            nid = run.name_to_node.get(s["component"])
            if nid:
                badges.setdefault(nid, {}).setdefault("size", []).append(f"{s['value']:,.3g} {s['unit']}")
        hz = run.sizing.horizon
        how = "one base period weighted for each of" if hz.weighted else "the base period repeated for"
        notices.append(f"Lifetime optimisation: {how} {hz.years} years in one model; costs and profit in "
                       f"this summary are present values at {100 * hz.discount_rate:g} %"
                       + (f" with {100 * hz.inflation:g} % inflation" if hz.inflation else "")
                       + " (see the Sizing tab for the yearly cash flows)")
        notices += run.sizing.warnings
    if run.lifetime is not None:
        y = run.lifetime.years[run.year - 1]
        for name, v in y.soh_start.items():
            nid = run.name_to_node.get(name)
            if nid:
                badges.setdefault(nid, {})["soh"] = _json_num(100 * v)
        notices += y.notes
    per_step, period = res.step_costs()
    real = ~period["category"].isin(["TIEBREAK", "SLACK"])
    prof = res.profit()
    profit = {"total": _json_num(-res.total_cost), "steps": _json_num(prof["profit"].sum()),
              "revenue": _json_num(prof["revenue"].sum()) if "revenue" in prof else 0.0,
              "period_charges": [{"component": p.component, "category": p.category, "EUR": _json_num(p.EUR)}
                                 for p in period[real].itertuples()],
              "best_step": str(prof["profit"].idxmax()), "worst_step": str(prof["profit"].idxmin()),
              "best": _json_num(prof["profit"].max()), "worst": _json_num(prof["profit"].min())}
    return {
        "status": res.status, "objective": _json_num(res.objective), "total_cost": _json_num(res.total_cost),
        "profit": profit,
        "build_time": res.build_time, "solve_time": res.solve_time, "steps": res.grid.n,
        "resolution": res.grid.freq, "costs": costs,
        "by_category": {k: _json_num(v) for k, v in res.cost_by_category.items()},
        "kpis": [{"kpi": r.kpi, "scope": r.scope, "value": _json_num(r.value), "unit": r.unit} for r in k.itertuples()],
        "edges": edges, "badges": badges, "warnings": res.validation.warnings, "notices": notices,
        "year": run.year, "lifetime": lifetime_summary(run), "sizing": sizing_summary(run),
        "buses": list(res.system.buses), "components": list(res.system.components),
    }


def _downsample(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    if len(df) <= max_points:
        return df
    step = int(np.ceil(len(df) / max_points))
    grp = np.arange(len(df)) // step
    out = df.groupby(grp).mean()
    out.index = df.index[::step][: len(out)]
    return out


def series(run: Run, name: str, max_points: int = 5000) -> dict:
    res = run.results
    if name in res.system.buses:
        df = res.balance(name)
        df["shadow_price"] = res.shadow_prices[name]
    elif name in res.system.components:
        c = res.component(name)
        df = c["flows"].copy()
        df.columns = [f"{p} ({d}) [MW]" for (p, _b, d) in df.columns]
        st = c["states"]
        for col in st.columns:
            df[f"{col}"] = st[col]
    else:
        raise KeyError(name)
    df = _downsample(df, max_points)
    return {"time": [str(t) for t in df.index],
            "series": {str(c): [_json_num(v) for v in df[c].to_numpy()] for c in df.columns}}


STATE_UNITS = {"e": "MWh", "soc": "0-1", "theta": "°C", "s_plus": "MWh", "s_minus": "MWh", "curtailment": "MW"}


def _is_ref(v) -> bool:
    return isinstance(v, dict) and set(v) in ({"series"}, {"daily"})


def timeseries(run: Run, labels: dict[str, str] | None = None) -> pd.DataFrame:
    """One row per time step: every wire's flow, component states, bus marginal prices and the input series the
    optimiser used (data-wired parameters after alignment to the model grid).

    Column groups (prefix): ``flow`` (MW, ``component.port → bus`` or ``bus → component.port``), ``state``,
    ``price`` (bus marginal price, EUR/MWh), ``check`` (MW flowing both ways at once on a grid connection or
    storage; only where that happens), ``econ`` (EUR) and ``input`` (with unit and data source). ``labels`` maps
    dataset ids to readable names for the source.
    """
    res, labels = run.results, labels or {}
    cols: dict[str, np.ndarray] = {"step_h": np.asarray(res.grid.dt, dtype=float)}
    for (comp, port, bus, direction) in res.flows.columns:
        name = f"flow {comp}.{port} → {bus}" if direction == "out" else f"flow {bus} → {comp}.{port}"
        cols[f"{name} [MW]"] = res.flows[(comp, port, bus, direction)].to_numpy()
    for col in res.states.columns:
        label = ".".join(map(str, col)) if isinstance(col, tuple) else str(col)
        unit = STATE_UNITS.get(col[-1] if isinstance(col, tuple) else col)
        cols[f"state {label}{f' [{unit}]' if unit else ''}"] = res.states[col].to_numpy()
    for bus in res.shadow_prices.columns:
        cols[f"price {bus} [EUR/MWh]"] = res.shadow_prices[bus].to_numpy()
    # opposite flows at once (only components where it happens): min(import, export), min(charge, discharge)
    for (comp, ports), ov in res.simultaneous().items():
        if (ov > 0).any():
            cols[f"check {comp} {ports} at once [MW]"] = ov.to_numpy()
    # system profit per step, split from the optimiser's cost expressions (billing-period charges are not in it)
    for name, v in res.profit().items():
        cols[f"econ {name.replace('_', ' ')} [EUR]"] = v.to_numpy()
    n = res.grid.n

    def source(ref):
        if "daily" in ref:
            return "daily profile"
        ds, _, col = ref["series"].partition(":")
        name = labels.get(ds, ds)
        return name if col == "value" else f"{name}:{col}"

    for c in run.config.get("components", []):
        comp = res.system.components.get(c["name"])
        if comp is None or not hasattr(comp, "r"):
            continue
        sp = specs(comp)
        for fname, v in c.items():
            if _is_ref(v):
                unit = sp[fname].unit if fname in sp else None
                label = f"input {c['name']}.{fname}{f' [{unit}]' if unit else ''} ({source(v)})"
                cols[label] = np.broadcast_to(np.asarray(getattr(comp.r, fname), dtype=float), (n,))
            elif fname in ("inputs", "outputs") and isinstance(v, dict):
                resolved = {b.name: a for b, a in getattr(comp.r, fname).items()}
                for bus, coef in v.items():
                    if _is_ref(coef):
                        label = f"input {c['name']}.{fname}[{bus}] ({source(coef)})"
                        cols[label] = np.broadcast_to(np.asarray(resolved[bus], dtype=float), (n,))
    df = pd.DataFrame(cols, index=res.index) + 0.0      # + 0.0 turns solver -0.0 into 0.0
    df.index.name = "time"
    return df


def timeseries_years(run: Run, labels: dict[str, str] | None = None, year="1") -> pd.DataFrame:
    """The per-step table of one year (``year`` = 1, 2, ...) or of every year (``"all"``: one block per year, the
    base period's timestamps repeated, with a first column ``year``)."""
    if str(year) != "all":
        return timeseries(year_view(run, int(year)), labels)
    frames, done = [], {}
    for k in range(1, years_of(run) + 1):
        view = year_view(run, k)
        key = id(view.results)
        if key not in done:
            done[key] = timeseries(view, labels)
        df = done[key].copy()
        df.insert(0, "year", k)
        frames.append(df)
    return pd.concat(frames)


def timeseries_page(df: pd.DataFrame, offset: int, limit: int, cols: list[str] | None = None) -> dict:
    """A page of the per-time-step table (optionally only ``cols``) for the results drawer and inspector."""
    if cols:
        df = df[[c for c in cols if c in df.columns]]
    offset = max(0, offset)
    page = df.iloc[offset: offset + max(1, min(limit, 1000))]
    return {"rows": len(df), "offset": offset, "columns": list(df.columns), "time": [str(t) for t in page.index],
            "data": [[_json_num(v) for v in row] for row in page.to_numpy()]}


def timeseries_xlsx(df: pd.DataFrame, summary: dict | None = None) -> bytes:
    """The per-step table as an Excel workbook (local time without offset, as Excel has no time zones); with a
    ``summary`` also a Summary sheet (costs by category, KPIs, profit)."""
    out = df.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(out.index, utc=True))
    tz = getattr(df.index, "tz", None)
    out.index = (idx.tz_convert(tz) if tz is not None else idx).tz_localize(None)
    out.index.name = f"time ({tz})" if tz is not None else "time (UTC)"
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        out.to_excel(xl, sheet_name="Time series")
        ws = xl.sheets["Time series"]
        ws.freeze_panes = "B2"
        ws.column_dimensions["A"].width = 19
        if summary:
            summary_rows(summary).to_excel(xl, sheet_name="Summary", index=False)
            xl.sheets["Summary"].column_dimensions["A"].width = 28
            life = summary.get("lifetime")
            if life:
                pd.DataFrame(life["rows"], columns=life["columns"]).to_excel(xl, sheet_name="Lifetime", index=False)
    return buf.getvalue()


def timeseries_series(df: pd.DataFrame, cols: list[str], max_points: int = 5000) -> dict:
    """Whole series of the chosen columns for the inspector chart (bin means when longer than ``max_points``)."""
    unknown = [c for c in cols if c not in df.columns]
    if unknown:
        raise KeyError(unknown[0])
    out = _downsample(df[cols], max_points)
    return {"rows": len(df), "points": len(out), "time": [str(t) for t in out.index],
            "series": {c: [_json_num(v) for v in out[c].to_numpy()] for c in cols}}


def results_zip(run: Run, labels: dict[str, str] | None = None) -> bytes:
    tmp = Path(tempfile.mkdtemp())
    try:
        run.results.to_csv(tmp / "results")
        timeseries(run, labels).to_csv(tmp / "results" / "timeseries.csv")
        if run.lifetime is not None:
            run.lifetime.table.to_csv(tmp / "results" / "lifetime.csv", index=False)
            timeseries_years(run, labels, "all").to_csv(tmp / "results" / "timeseries_all_years.csv")
        # the solved system (scalar parameters; data-wired series are the "input" columns of timeseries.csv)
        (tmp / "results" / "config.json").write_text(json.dumps(run.config, indent=1, default=str))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in (tmp / "results").iterdir():
                z.write(f, f.name)
        return buf.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

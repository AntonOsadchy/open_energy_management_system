"""Background solve runs for the UI."""

from __future__ import annotations

import io
import shutil
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import from_config
from ..exceptions import InfeasibleModelError, OEMSError, ValidationError
from .graph import map_errors


@dataclass
class Run:
    id: str
    status: str = "queued"          # queued | building | solving | done | failed
    message: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    results: object = None
    node_errors: dict = field(default_factory=dict)
    name_to_node: dict = field(default_factory=dict)


class RunManager:
    def __init__(self, keep: int = 5):
        self.runs: dict[str, Run] = {}
        self.keep = keep
        self._lock = threading.Lock()

    def start(self, cfg: dict, name_to_node: dict) -> Run:
        run = Run(id=uuid.uuid4().hex[:10], name_to_node=name_to_node)
        with self._lock:
            self.runs[run.id] = run
            for old in list(self.runs)[:-self.keep]:
                self.runs.pop(old, None)
        threading.Thread(target=self._work, args=(run, cfg), daemon=True).start()
        return run

    def _work(self, run: Run, cfg: dict):
        try:
            run.status = "building"
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
        except OEMSError as e:
            run.status, run.message = "failed", str(e)
        except Exception as e:  # pragma: no cover - unexpected
            run.status, run.message = "failed", f"{type(e).__name__}: {e}"
            run.node_errors["_global"] = [traceback.format_exc(limit=3)]
        finally:
            run.finished = time.time()

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)


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
        if nid and r.kpi in ("full_load_hours", "equivalent_cycles", "production", "max_billed_peak"):
            badges.setdefault(nid, {})[r.kpi] = _json_num(r.value)
    return {
        "status": res.status, "objective": _json_num(res.objective), "total_cost": _json_num(res.total_cost),
        "build_time": res.build_time, "solve_time": res.solve_time, "steps": res.grid.n,
        "resolution": res.grid.freq, "costs": costs,
        "by_category": {k: _json_num(v) for k, v in res.cost_by_category.items()},
        "kpis": [{"kpi": r.kpi, "scope": r.scope, "value": _json_num(r.value), "unit": r.unit} for r in k.itertuples()],
        "edges": edges, "badges": badges, "warnings": res.validation.warnings,
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


def results_zip(run: Run) -> bytes:
    tmp = Path(tempfile.mkdtemp())
    try:
        run.results.to_csv(tmp / "results")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in (tmp / "results").iterdir():
                z.write(f, f.name)
        return buf.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

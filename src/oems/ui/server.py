"""FastAPI server for the drag-and-drop UI. Start with ``oems-app`` (opens http://127.0.0.1:8765)."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import threading
import time
import webbrowser
import zipfile
from pathlib import Path

import pandas as pd
from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config import from_config
from ..exceptions import ValidationError
from . import report as report_mod
from . import runs as runs_mod
from . import simulations as sims_mod
from . import tutorial
from .accounts import SESSION_DAYS, Accounts, AuthError
from .catalog import catalog
from .datasets import DatasetStore
from .dbsource import BessDb, DbError, Secrets, conn_of, curves_for, dataset_for, mask, materialize
from .graph import GraphError, graph_to_config, map_errors

STATIC = Path(__file__).parent / "static"
SESSION_COOKIE = "oems_session"


def create_app(workspace: str | Path | None = None) -> FastAPI:
    ws = Path(workspace or os.environ.get("OEMS_WORKSPACE", "oems_workspace")).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    accounts = Accounts(ws)
    datasets = DatasetStore(ws)
    runs = runs_mod.RunManager()
    secrets = Secrets(ws)
    sims: sims_mod.SimulationStore = sims_mod.LocalSimulationStore(ws)
    app = FastAPI(title="Microgrid Sizing Tool")
    app.state.workspace = ws
    app.state.runs = runs                       # tests set runs.cloud_runner (a cloud run in this process)
    app.state.db_factory = lambda conn: BessDb(conn, secrets.password(conn))   # replaced by a fake in tests
    dbs: dict[str, BessDb] = {}

    def db_for(conn: dict) -> BessDb:
        """One BessDb per connection (it caches the zone/series catalogue)."""
        key = json.dumps(conn_of(conn), sort_keys=True)
        if key not in dbs:
            dbs[key] = app.state.db_factory(conn)
        return dbs[key]

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        """Make browsers re-check the page and scripts on every load, so an updated app is never mixed with
        cached old scripts (unchanged files still come back as a cheap 304)."""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/catalog")
    def get_catalog():
        return catalog()

    # ------------------------------------------------------------- datasets
    @app.get("/api/datasets")
    def list_datasets():
        return datasets.list()

    @app.post("/api/datasets")
    async def upload(file: UploadFile = File(...), tz: str | None = Form(None)):
        try:
            return datasets.add(file.filename or "data.csv", await file.read(), tz=tz or None)
        except (ValidationError, ValueError) as e:
            raise HTTPException(400, f"could not read CSV: {e}") from e

    @app.get("/api/datasets/{ds_id}")
    def dataset_meta(ds_id: str):
        if ds_id not in datasets.index:
            raise HTTPException(404, "unknown dataset")
        return {**datasets.index[ds_id], "preview": datasets.preview(ds_id)}

    @app.get("/api/datasets/{ds_id}/{col}")
    def dataset_series(ds_id: str, col: str):
        if ds_id not in datasets.index:
            raise HTTPException(404, "unknown dataset")
        return datasets.series(ds_id, col)

    @app.post("/api/tutorial/data")
    def tutorial_data():
        """Register the tutorial dataset (once) and return its metadata."""
        for meta in datasets.list():
            if meta["name"] == tutorial.NAME:
                return {**meta, "preview": datasets.preview(meta["id"])}
        return datasets.add(f"{tutorial.NAME}.csv", tutorial.tutorial_csv(), tz=tutorial.TZ)

    # ------------------------------------------------------------- BESS IQ database (DB connection / DB input blocks)
    def default_conn() -> dict | None:
        """The .env / environment login, used by DB inputs when the canvas has no DB connection block."""
        d = secrets.defaults()
        return {k: d[k] for k in ("host", "port", "database", "user", "sslmode")} if d["host"] else None

    @app.get("/api/db/defaults")
    def db_defaults():
        """Prefill for a new DB connection block (from $OEMS_DB_URL / $DATABASE_URL / the BESS IQ .env file)."""
        d = secrets.defaults()
        return {**d, "has_password": bool(d["host"]) and secrets.has_password(d)}

    @app.post("/api/db/connect")
    def db_connect(body: dict):
        """Store the password (if given) for the block's login, test it and list the server's databases."""
        conn = conn_of(body.get("conn") or {})
        if body.get("password"):
            secrets.set_password(conn, body["password"])
        dbs.pop(json.dumps(conn, sort_keys=True), None)
        out = {"target": mask(conn), "has_password": secrets.has_password(conn), "connected": False}
        try:
            return {**out, "connected": True, "databases": db_for(conn).databases()}
        except DbError as e:
            return {**out, "error": str(e)}

    @app.post("/api/db/options")
    def db_options(body: dict):
        try:
            return db_for(body.get("conn") or {}).options()
        except DbError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/db/preview")
    def db_preview(body: dict):
        """Fetch one DB input's series for the current settings; returns its stats and a sparkline (a degradation
        input: its SoH curves)."""
        spec = body.get("spec") or {}
        if spec.get("source") == "degradation":
            try:
                return curves_for(spec, body.get("conn") or {}, db_for)
            except DbError as e:
                raise HTTPException(400, str(e)) from e
        try:
            ds_id, info = dataset_for(body.get("spec") or {}, body.get("conn") or {}, body.get("settings") or {},
                                      datasets, db_for, refresh=bool(body.get("refresh")))
        except DbError as e:
            raise HTTPException(400, str(e)) from e
        s = datasets.frame(ds_id)["value"]
        return {**info, "dataset": ds_id, "preview": datasets.preview(ds_id)["value"],
                "min": float(s.min()), "mean": float(s.mean()), "max": float(s.max()), "steps": len(s)}

    # ------------------------------------------------------------- validate & run
    def _translate(graph: dict):
        graph, db_errors = materialize(graph, datasets, db_for, default_conn())
        if db_errors:
            return None, db_errors
        try:
            return graph_to_config(graph, datasets.paths())
        except GraphError as e:
            return None, e.errors

    @app.get("/api/cloud")
    def cloud_status():
        """Whether runs can go to a cloud machine, and the RAM here and there (for the Run pop-up's estimate)."""
        from .. import cloud
        ok, why = cloud.available()
        try:
            ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
        except (ValueError, OSError, AttributeError):
            ram = None
        return {"available": ok, "reason": why, "local_gb": ram, "cloud_gb": cloud.MEMORY_MB / 1024}

    @app.post("/api/validate")
    def validate(graph: dict):
        cfg, extra = _translate(graph)
        if cfg is None:
            return {"ok": False, "errors": extra, "warnings": {}}
        name_to_node = extra
        try:
            es, _ = from_config(cfg)
            rep = es.validate(raise_on_error=False)
        except ValidationError as e:
            return {"ok": False, "errors": map_errors(e.errors, name_to_node), "warnings": {}}
        return {"ok": rep.ok, "errors": map_errors(rep.errors, name_to_node),
                "warnings": map_errors(rep.warnings, name_to_node),
                "alignment": [f"{a.owner}.{a.field}: {a.source_hours}h -> {a.target_hours}h ({a.method})"
                              for a in rep.alignment]}

    def _baseline(graph: dict, cfg: dict) -> dict | None:
        """A lifetime optimisation compared with another variant (``graph.baseline``: ``{name, nodes, edges}``): that
        variant becomes the reference case (``lifetime.reference``), with this run's settings and Run settings block
        (the same horizon, discount rate and CO₂ cap). Returns the errors, keyed ``_global``, or None."""
        base = graph.get("baseline")
        if not base or (cfg.get("lifetime") or {}).get("mode") != "optimise":
            return None
        name = base.get("name") or "baseline"
        mine = [{**n, "id": "_run_settings"} for n in graph.get("nodes", []) if n.get("kind") == "invest"]   # no id clash
        nodes = [n for n in base.get("nodes", []) if n.get("kind") not in ("invest", "note")] + mine
        bcfg, extra = _translate({"settings": {**graph.get("settings", {}), "baseline": True}, "nodes": nodes,
                                  "edges": base.get("edges", [])})
        if bcfg is None:
            msgs = [m for ms in extra.values() for m in ms]
            return {"_global": [f"baseline '{name}': {m}" for m in msgs] or [f"baseline '{name}' has errors"]}
        cfg["lifetime"].update(reference=bcfg, reference_name=name)
        return None

    @app.post("/api/runs")
    def start_run(graph: dict):
        cfg, extra = _translate(graph)
        if cfg is None:
            return {"run_id": None, "status": "failed", "errors": extra}
        errors = _baseline(graph, cfg)
        if errors:
            return {"run_id": None, "status": "failed", "errors": errors}
        run = runs.start(cfg, extra)
        return {"run_id": run.id, "status": run.status}

    def _run(run_id):
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        return run

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        """Stop a run (local: its worker process ends; cloud: its machines are cancelled)."""
        run = runs.cancel(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        return {"run_id": run.id, "status": run.status}

    @app.get("/api/runs/{run_id}")
    def run_status(run_id: str):
        run = _run(run_id)
        return {"run_id": run.id, "status": run.status, "message": run.message, "errors": run.node_errors,
                "elapsed": (run.finished or time.time()) - run.started, **run.progress}

    def _done(run_id: str, year=None):
        """A finished run, as seen for ``year`` of a multi-year run (default: year 1)."""
        run = _run(run_id)
        if run.status != "done":
            raise HTTPException(409, f"run is {run.status}")
        try:
            return runs_mod.year_view(run, int(year) if year not in (None, "") else None)
        except (KeyError, ValueError) as e:
            raise HTTPException(404, f"no year {year!r} in this run") from e

    @app.get("/api/runs/{run_id}/summary")
    def run_summary(run_id: str, year: int | None = None):
        return runs_mod.summary(_done(run_id, year))

    @app.get("/api/runs/{run_id}/series")
    def run_series(run_id: str, name: str, year: int | None = None):
        run = _done(run_id, year)
        try:
            return runs_mod.series(run, name)
        except KeyError as e:
            raise HTTPException(404, f"unknown bus or component {name!r}") from e

    def _labels() -> dict[str, str]:
        return {k: v["name"] for k, v in datasets.index.items()}

    @app.get("/api/runs/{run_id}/timeseries")
    def run_timeseries(run_id: str, offset: int = 0, limit: int = 100, format: str = "json", cols: str = "",
                       year: str = "1"):
        """Per-time-step table of flows, states, marginal prices and inputs: a page as JSON (``format=json``), whole
        series of some columns for a chart (``format=series``, ``cols`` = JSON list) or all of it as CSV / Excel.
        ``year`` picks a year of a multi-year run (``all``: every year, with a ``year`` column)."""
        run = _done(run_id, None if year == "all" else year)
        return _timeseries_response(_ts_frame(run_id, run, year), run_id, offset, limit, format, cols,
                                    lambda: runs_mod.summary(run))

    @app.post("/api/runs/{run_id}/report")
    def run_report(run_id: str, body: dict):
        """Downloadable HTML report of a finished run; ``body.project`` is the canvas it was run from (``body.year``:
        the year of a multi-year run it shows in detail)."""
        year = str(body.get("year") or 1)
        run = _done(run_id, year)
        project = body.get("project") or {}
        name = body.get("name") or (project.get("settings") or {}).get("name") or "simulation"
        page = report_mod.build_report(name=name, summary=runs_mod.summary(run), ts=_ts_frame(run_id, run, year),
                                       config=run.config, project=project, notes=body.get("notes") or "")
        return _html_download(page, name)

    def _html_download(page: str, name: str):
        safe = re.sub(r"[^\w\-]", "_", name).strip("_") or "simulation"
        return Response(page, media_type="text/html",
                        headers={"Content-Disposition": f'attachment; filename="oems_report_{safe}.html"'})

    ts_cache: dict[str, pd.DataFrame] = {}     # run id[:year] -> table (the inspector asks for it many times)

    def _ts_frame(run_id: str, run, year="1") -> pd.DataFrame:
        key = f"{run_id}:{year}"
        if key not in ts_cache:
            if len(ts_cache) >= 4:
                ts_cache.pop(next(iter(ts_cache)))
            ts_cache[key] = runs_mod.timeseries_years(run, _labels(), year)
        return ts_cache[key]

    def _timeseries_response(df, name: str, offset: int, limit: int, format: str, cols: str = "",
                             summary=None):
        chosen = json.loads(cols) if cols else None
        if format == "xlsx":
            part = df[[c for c in chosen if c in df.columns]] if chosen else df
            data = runs_mod.timeseries_xlsx(part, None if chosen or summary is None else summary())
            return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": f'attachment; filename="oems_timeseries_{name}.xlsx"'})
        if format == "csv":
            if chosen:
                df = df[[c for c in chosen if c in df.columns]]
            return Response(df.to_csv(float_format="%.10g"), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="oems_timeseries_{name}.csv"'})
        if format == "series":
            try:
                return runs_mod.timeseries_series(df, chosen or [])
            except KeyError as e:
                raise HTTPException(404, f"unknown column {e.args[0]!r}") from e
        return runs_mod.timeseries_page(df, offset, limit, chosen)

    def _export(get_summary, name: str, view: str, format: str):
        if view not in runs_mod.EXPORT_VIEWS or format not in ("csv", "xlsx"):
            raise HTTPException(400, f"view must be one of {', '.join(runs_mod.EXPORT_VIEWS)}; format csv or xlsx")
        try:
            sheets = runs_mod.export_tables(get_summary(), view)
        except KeyError as e:
            raise HTTPException(404, "not a multi-year run") from e
        data, media = runs_mod.tables_file(sheets, format)
        return Response(data, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="oems_{view}_{name}.{format}"'})

    @app.get("/api/runs/{run_id}/export")
    def run_export(run_id: str, view: str, format: str = "csv", year: int | None = None):
        """A results tab built from the summary (``view`` = summary, costs or lifetime) as CSV or Excel; the
        per-step views (bus balance, component, time series) download from ``/timeseries``."""
        return _export(lambda: runs_mod.summary(_done(run_id, year)), run_id, view, format)

    @app.get("/api/runs/{run_id}/download")
    def run_download(run_id: str):
        run = _run(run_id)
        if run.status != "done":
            raise HTTPException(409, f"run is {run.status}")
        return Response(runs_mod.results_zip(run, _labels()), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="oems_results_{run_id}.zip"'})

    @app.post("/api/config")
    def export_config(graph: dict):
        """The system config (YAML-ready dict) for a graph, to run headless with `python -m oems run`."""
        cfg, extra = _translate(graph)
        if cfg is None:
            raise HTTPException(400, json.dumps(extra))
        return cfg

    # ------------------------------------------------------------- saved simulations
    def _sim(sim_id: str, part: str):
        try:
            return sims.get(sim_id, part)
        except KeyError as e:
            raise HTTPException(404, "unknown simulation") from e

    @app.get("/api/simulations")
    def list_simulations():
        return sims.list()

    @app.post("/api/simulations")
    def save_simulation(body: dict, oems_session: str | None = Cookie(None)):
        """Save a finished run: ``{run_id, name, notes, project}`` (project = the canvas the run was started from).

        Each project or variant keeps one set of results: the run replaces the results saved before for the same
        project name, and the user's saved project of that name (if any) points at the new ones."""
        run = _run(body.get("run_id") or "")
        if run.status != "done":
            raise HTTPException(409, f"run is {run.status}")
        snap = sims_mod.snapshot(run, name=body.get("name") or "", notes=body.get("notes") or "",
                                 project=body.get("project"), labels=_labels())
        meta = sims.save(snap)
        name = meta.get("project_name")
        if name:
            for old in sims.list():
                if old.get("project_name") == name and old["id"] != meta["id"]:
                    sims.delete(old["id"])
            user = accounts.user_of(oems_session)
            path = accounts.project_path(user, name) if user else None
            if path is not None and path.exists():
                project = json.loads(path.read_text())
                project["results"] = meta["id"]
                path.write_text(json.dumps(project, indent=1))
        return meta

    @app.get("/api/simulations/{sim_id}")
    def load_simulation(sim_id: str):
        """Metadata and canvas of a saved simulation (summary and series are fetched separately)."""
        return {"meta": _sim(sim_id, "meta"), "project": _sim(sim_id, "project")}

    @app.patch("/api/simulations/{sim_id}")
    def update_simulation(sim_id: str, body: dict):
        try:
            return sims.update(sim_id, name=body.get("name"), notes=body.get("notes"), project_name=body.get("project_name"))
        except KeyError as e:
            raise HTTPException(404, "unknown simulation") from e

    @app.delete("/api/simulations/{sim_id}")
    def delete_simulation(sim_id: str):
        try:
            sims.delete(sim_id)
        except KeyError as e:
            raise HTTPException(404, "unknown simulation") from e
        return {"deleted": sim_id}

    @app.get("/api/simulations/{sim_id}/summary")
    def simulation_summary(sim_id: str, year: int | None = None):
        """The stored summary; ``year`` picks a year of a saved multi-year run."""
        if not year or year == 1:
            return _sim(sim_id, "summary")
        try:
            years = sims.get(sim_id, "years") or []
        except KeyError:
            years = []
        if not 1 <= year <= len(years):
            raise HTTPException(404, f"no year {year} in this simulation")
        return years[year - 1]

    @app.get("/api/simulations/{sim_id}/export")
    def simulation_export(sim_id: str, view: str, format: str = "csv", year: int | None = None):
        """A results tab of a saved simulation as CSV or Excel (see ``/api/runs/{id}/export``)."""
        safe = re.sub(r"[^\w\-]", "_", _sim(sim_id, "meta")["name"]).strip("_") or sim_id
        return _export(lambda: simulation_summary(sim_id, year), safe, view, format)

    @app.get("/api/simulations/{sim_id}/config")
    def simulation_config(sim_id: str):
        return _sim(sim_id, "config")

    @app.get("/api/simulations/{sim_id}/series")
    def simulation_series(sim_id: str, name: str):
        series = _sim(sim_id, "series")
        if name not in series:
            raise HTTPException(404, f"unknown bus or component {name!r}")
        return series[name]

    @app.get("/api/simulations/{sim_id}/timeseries")
    def simulation_timeseries(sim_id: str, offset: int = 0, limit: int = 100, format: str = "json", cols: str = "",
                              year: str = "1"):
        """The per-time-step table of a saved simulation (from its results zip); ``year`` picks a year of a saved
        multi-year run (``all``: every year, with a ``year`` column)."""
        _sim(sim_id, "meta")
        key = "sim:" + sim_id + ("" if year == "1" else f":{year}")
        if key not in ts_cache:
            with zipfile.ZipFile(io.BytesIO(sims.download(sim_id))) as z:
                names = z.namelist()
                if "timeseries.csv" not in names:
                    raise HTTPException(404, "this simulation was saved before per-time-step tables existed; "
                                             "run it again")
                if year == "1":
                    df = pd.read_csv(z.open("timeseries.csv"), index_col=0)
                elif "timeseries_all_years.csv" in names:
                    df = pd.read_csv(z.open("timeseries_all_years.csv"), index_col=0)
                    if year != "all":
                        df = df[df["year"] == int(year)].drop(columns="year")
                    if not len(df):
                        raise HTTPException(404, f"no year {year} in this simulation")
                else:
                    raise HTTPException(404, "this simulation keeps year 1 only; run it again to keep every year")
                if len(ts_cache) >= 4:
                    ts_cache.pop(next(iter(ts_cache)))
                ts_cache[key] = df
        return _timeseries_response(ts_cache[key], sim_id, offset, limit, format, cols,
                                    lambda: _sim(sim_id, "summary"))

    @app.get("/api/simulations/{sim_id}/report")
    def simulation_report(sim_id: str):
        """The report of a saved simulation, from its stored canvas, summary, config and per-step table."""
        meta = _sim(sim_id, "meta")
        simulation_timeseries(sim_id, limit=1)                          # loads the per-step table (or 404)
        page = report_mod.build_report(name=meta["name"], summary=_sim(sim_id, "summary"),
                                       ts=ts_cache["sim:" + sim_id], config=_sim(sim_id, "config"),
                                       project=_sim(sim_id, "project"), notes=meta.get("notes") or "")
        return _html_download(page, meta["name"])

    @app.get("/api/simulations/{sim_id}/download")
    def simulation_download(sim_id: str):
        meta = _sim(sim_id, "meta")
        safe = re.sub(r"[^\w\-]", "_", meta["name"]).strip("_") or sim_id
        return Response(sims.download(sim_id), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="oems_{safe}.zip"'})

    # ------------------------------------------------------------- logins
    def _signed_in(response: Response, token: str, user: str) -> dict:
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="strict")
        return {"user": user}

    def current_user(oems_session: str | None = Cookie(None)) -> str:
        user = accounts.user_of(oems_session)
        if user is None:
            raise HTTPException(401, "please log in to see your saved cases")
        return user

    @app.get("/api/auth/me")
    def whoami(oems_session: str | None = Cookie(None)):
        return {"user": accounts.user_of(oems_session)}

    @app.post("/api/auth/register")
    def register(body: dict, response: Response):
        user = (body.get("user") or "").strip()
        try:
            return _signed_in(response, accounts.register(user, body.get("password") or ""), user)
        except AuthError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/auth/login")
    def login(body: dict, response: Response):
        user = (body.get("user") or "").strip()
        try:
            return _signed_in(response, accounts.login(user, body.get("password") or ""), user)
        except AuthError as e:
            raise HTTPException(401, str(e)) from e

    @app.post("/api/auth/logout")
    def logout(response: Response, oems_session: str | None = Cookie(None)):
        accounts.logout(oems_session)
        response.delete_cookie(SESSION_COOKIE)
        return {"user": None}

    # ------------------------------------------------------------- projects ("cases", per user)
    def _existing(user: str, name: str) -> Path:
        path = accounts.project_path(user, name)
        if not path.exists():
            raise HTTPException(404, "unknown project")
        return path

    @app.get("/api/projects")
    def list_projects(user: str = Depends(current_user)):
        return accounts.list_projects(user)

    @app.post("/api/projects/{name}")
    def save_project(name: str, project: dict, user: str = Depends(current_user)):
        path = accounts.project_path(user, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(project, indent=1))
        return {"saved": path.stem}

    @app.get("/api/projects/{name}")
    def load_project(name: str, user: str = Depends(current_user)):
        return json.loads(_existing(user, name).read_text())

    @app.patch("/api/projects/{name}")
    def rename_project(name: str, body: dict, user: str = Depends(current_user)):
        """Rename a project: ``{name}``; also changes the project name in its settings."""
        path, new = _existing(user, name), (body.get("name") or "").strip()
        if not new:
            raise HTTPException(400, "the new name is empty")
        target = accounts.project_path(user, new)
        if target != path and target.exists():
            raise HTTPException(409, f"a project named '{new}' already exists")
        project = json.loads(path.read_text())
        old = project.get("settings", {}).get("name") or path.stem
        project.setdefault("settings", {})["name"] = new
        target.write_text(json.dumps(project, indent=1))
        if target != path:
            path.unlink()
        accounts.relink_variants(user, old, new)
        return {"renamed": target.stem}

    @app.delete("/api/projects/{name}")
    def delete_project(name: str, user: str = Depends(current_user)):
        path = _existing(user, name)
        project = json.loads(path.read_text())
        path.unlink()   # its variants move up to its parent
        accounts.relink_variants(user, project.get("settings", {}).get("name") or path.stem, project.get("variant_of"))
        return {"deleted": path.stem}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main(argv=None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(prog="oems-app", description="Microgrid Sizing Tool drag-and-drop UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--workspace", default=os.environ.get("OEMS_WORKSPACE", "oems_workspace"))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Microgrid Sizing Tool UI on {url} (workspace: {Path(args.workspace).resolve()})")
    uvicorn.run(create_app(args.workspace), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

"""FastAPI server for the drag-and-drop UI. Start with ``oems-app`` (opens http://127.0.0.1:8765)."""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import webbrowser
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config import from_config
from ..exceptions import ValidationError
from . import runs as runs_mod
from . import tutorial
from .catalog import catalog
from .datasets import DatasetStore
from .graph import GraphError, graph_to_config, map_errors

STATIC = Path(__file__).parent / "static"


def create_app(workspace: str | Path | None = None) -> FastAPI:
    ws = Path(workspace or os.environ.get("OEMS_WORKSPACE", "oems_workspace")).resolve()
    (ws / "projects").mkdir(parents=True, exist_ok=True)
    datasets = DatasetStore(ws)
    runs = runs_mod.RunManager()
    app = FastAPI(title="Open EMS")
    app.state.workspace = ws

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

    # ------------------------------------------------------------- validate & run
    def _translate(graph: dict):
        try:
            return graph_to_config(graph, datasets.paths())
        except GraphError as e:
            return None, e.errors

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

    @app.post("/api/runs")
    def start_run(graph: dict):
        cfg, extra = _translate(graph)
        if cfg is None:
            return {"run_id": None, "status": "failed", "errors": extra}
        run = runs.start(cfg, extra)
        return {"run_id": run.id, "status": run.status}

    def _run(run_id):
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        return run

    @app.get("/api/runs/{run_id}")
    def run_status(run_id: str):
        run = _run(run_id)
        return {"run_id": run.id, "status": run.status, "message": run.message, "errors": run.node_errors,
                "elapsed": (run.finished or time.time()) - run.started}

    @app.get("/api/runs/{run_id}/summary")
    def run_summary(run_id: str):
        run = _run(run_id)
        if run.status != "done":
            raise HTTPException(409, f"run is {run.status}")
        return runs_mod.summary(run)

    @app.get("/api/runs/{run_id}/series")
    def run_series(run_id: str, name: str):
        run = _run(run_id)
        if run.status != "done":
            raise HTTPException(409, f"run is {run.status}")
        try:
            return runs_mod.series(run, name)
        except KeyError as e:
            raise HTTPException(404, f"unknown bus or component {name!r}") from e

    @app.get("/api/runs/{run_id}/download")
    def run_download(run_id: str):
        run = _run(run_id)
        if run.status != "done":
            raise HTTPException(409, f"run is {run.status}")
        return Response(runs_mod.results_zip(run), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="oems_results_{run_id}.zip"'})

    @app.post("/api/config")
    def export_config(graph: dict):
        """The system config (YAML-ready dict) for a graph, to run headless with `python -m oems run`."""
        cfg, extra = _translate(graph)
        if cfg is None:
            raise HTTPException(400, json.dumps(extra))
        return cfg

    # ------------------------------------------------------------- projects
    def _project_path(name: str) -> Path:
        safe = re.sub(r"[^\w\-]", "_", name).strip("_") or "project"
        return ws / "projects" / f"{safe}.json"

    @app.get("/api/projects")
    def list_projects():
        return sorted(p.stem for p in (ws / "projects").glob("*.json"))

    @app.post("/api/projects/{name}")
    def save_project(name: str, project: dict):
        path = _project_path(name)
        path.write_text(json.dumps(project, indent=1))
        return {"saved": path.stem}

    @app.get("/api/projects/{name}")
    def load_project(name: str):
        path = _project_path(name)
        if not path.exists():
            raise HTTPException(404, "unknown project")
        return json.loads(path.read_text())

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main(argv=None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(prog="oems-app", description="Open EMS drag-and-drop UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--workspace", default=os.environ.get("OEMS_WORKSPACE", "oems_workspace"))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Open EMS UI on {url} (workspace: {Path(args.workspace).resolve()})")
    uvicorn.run(create_app(args.workspace), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

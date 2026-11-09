"""Saved simulations: a finished run frozen together with the canvas it was run from.

A saved simulation is self-contained, so it can be reopened (canvas, summary, plots, CSV download) without solving
again and after the server has dropped the run from memory. It consists of

* ``meta``    - one small record per simulation (id, name, notes, created, total cost, horizon); the list view;
* ``project`` - the canvas and settings as they were when the run started (the same format as a saved project);
* ``config``  - the system config that was solved (to rerun headless with ``python -m oems run``);
* ``summary`` - the results summary the UI shows (costs, KPIs, wire energies, block badges);
* ``series``  - the plot series of every bus and component (as served by ``/api/runs/{id}/series``);
* ``years``   - for a multi-year run: the summary of every year (``null`` otherwise);
* ``results.zip`` - the full-resolution result CSVs, including ``timeseries.csv`` (flows, states, prices and
  inputs per time step; year 1) and, for a multi-year run, ``timeseries_all_years.csv`` and ``lifetime.csv``.

The storage backend is behind :class:`SimulationStore`. :class:`LocalSimulationStore` keeps everything in the
workspace (``<workspace>/simulations/<id>/``). A hosted backend (e.g. Supabase) maps ``meta`` + ``project`` +
``config`` + ``summary`` to a table row and ``series`` / ``results.zip`` to a storage bucket; an ``owner`` column
can be added once there are user logins.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from . import runs as runs_mod

_ID = re.compile(r"^[0-9a-f]{12}$")


def snapshot(run: runs_mod.Run, *, name: str, notes: str = "", project: dict | None = None,
             labels: dict[str, str] | None = None) -> dict:
    """Everything that is stored for a finished run: ``{meta, project, config, summary, series, zip}``."""
    summary = runs_mod.summary(run)
    series = {n: runs_mod.series(run, n) for n in summary["buses"] + summary["components"]}
    years = None
    if run.lifetime is not None:          # keep every year, so a saved multi-year run can be browsed by year
        years = [runs_mod.summary(runs_mod.year_view(run, k)) for k in range(1, runs_mod.years_of(run) + 1)]
        summary["lifetime"]["saved_years"] = True
    settings = (project or {}).get("settings") or {}
    meta = {
        "id": uuid.uuid4().hex[:12], "name": name.strip() or "simulation", "notes": notes,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "run_id": run.id,
        "total_cost": summary["total_cost"], "steps": summary["steps"], "resolution": summary["resolution"],
        "start": settings.get("start"), "days": settings.get("days"), "project_name": settings.get("name"),
    }
    return {"meta": meta, "project": project or {}, "config": run.config, "summary": summary, "series": series,
            "years": years, "zip": runs_mod.results_zip(run, labels)}


class SimulationStore(ABC):
    """Where saved simulations live. Implementations raise ``KeyError`` for an unknown id."""

    @abstractmethod
    def save(self, snap: dict) -> dict:
        """Store a :func:`snapshot`; returns its ``meta``."""

    @abstractmethod
    def list(self) -> list[dict]:
        """``meta`` of every saved simulation, newest first."""

    @abstractmethod
    def get(self, sim_id: str, part: str) -> dict:
        """One JSON part of a simulation: ``meta``, ``project``, ``config``, ``summary``, ``series`` or ``years``."""

    @abstractmethod
    def download(self, sim_id: str) -> bytes:
        """The result CSVs as a zip."""

    @abstractmethod
    def update(self, sim_id: str, **fields) -> dict:
        """Change ``name``, ``notes`` and/or ``project_name``; returns the new ``meta``."""

    @abstractmethod
    def delete(self, sim_id: str) -> None: ...


class LocalSimulationStore(SimulationStore):
    """One folder per simulation under ``<workspace>/simulations``, one JSON file per part."""

    PARTS = ("meta", "project", "config", "summary", "series", "years")

    def __init__(self, workspace: str | Path):
        self.root = Path(workspace) / "simulations"
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, sim_id: str) -> Path:
        d = self.root / sim_id
        if not _ID.match(sim_id) or not (d / "meta.json").exists():
            raise KeyError(sim_id)
        return d

    def save(self, snap: dict) -> dict:
        d = self.root / snap["meta"]["id"]
        tmp = d.with_name(d.name + ".tmp")
        tmp.mkdir(parents=True)
        for part in self.PARTS:
            (tmp / f"{part}.json").write_text(json.dumps(snap.get(part), separators=(",", ":")))
        (tmp / "results.zip").write_bytes(snap["zip"])
        tmp.rename(d)        # appears in the list only once complete
        return snap["meta"]

    def list(self) -> list[dict]:
        metas = [json.loads(p.read_text()) for p in self.root.glob("*/meta.json") if _ID.match(p.parent.name)]
        return sorted(metas, key=lambda m: m["created"], reverse=True)

    def get(self, sim_id: str, part: str) -> dict:
        if part not in self.PARTS:
            raise KeyError(part)
        path = self._dir(sim_id) / f"{part}.json"
        if not path.exists():           # saved before this part existed
            raise KeyError(part)
        return json.loads(path.read_text())

    def download(self, sim_id: str) -> bytes:
        return (self._dir(sim_id) / "results.zip").read_bytes()

    def update(self, sim_id: str, **fields) -> dict:
        path = self._dir(sim_id) / "meta.json"
        meta = json.loads(path.read_text())
        meta.update({k: v for k, v in fields.items() if k in ("name", "notes", "project_name") and v is not None})
        path.write_text(json.dumps(meta))
        return meta

    def delete(self, sim_id: str) -> None:
        shutil.rmtree(self._dir(sim_id))

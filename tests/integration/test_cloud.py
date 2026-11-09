"""Cloud runs (oems.cloud) with the worker run in this process instead of on Modal: the job is packed, solved and
unpacked exactly as in the cloud, so the results, the progress and the errors must equal a local run."""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from oems import cloud
from oems.exceptions import ValidationError
from oems.sizing import optimise_lifetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validation"))
from test_sizing import _cfg  # noqa: E402


def test_lifetime_optimisation_in_the_cloud_equals_the_local_run():
    cfg = _cfg(rate=0.05, store={"capex_energy": 600_000, "opex": 10_000})
    stages = []
    remote = cloud.solve_remote(cfg, run=cloud.run_locally, on_stage=lambda k, n, label: stages.append((k, n)))
    local = optimise_lifetime(cfg)
    assert stages == [(1, 1)]                       # sized and reference model solved at once
    assert remote.kpis["npv"] == pytest.approx(local.kpis["npv"], rel=1e-9)
    assert [s["value"] for s in remote.sizes] == pytest.approx([s["value"] for s in local.sizes])
    assert remote.results.model is None and remote.results.kpis().shape == local.results.kpis().shape
    assert len(remote.cloud_stats) == 2 and all(s["peak_mb"] > 0 for s in remote.cloud_stats)


def test_input_series_travel_with_the_job(tmp_path):
    """The job carries the series it uses (loaded here), not the dataset paths: the worker cannot read them."""
    idx = pd.date_range("2025-01-01", periods=4, freq="1h")
    pd.DataFrame({"price": [10.0, 100, 10, 100], "unused": 0.0}, index=idx).rename_axis("time").to_csv(tmp_path / "p.csv")
    cfg = _cfg(store={"capex_energy": 700_000})
    cfg["datasets"] = {"prices": str(tmp_path / "p.csv"), "other": str(tmp_path / "missing.csv")}
    cfg["components"][0]["import_price"] = {"series": "prices:price"}
    assert cloud.series_refs(cfg) == {"prices"}
    def run(payload):           # packed: from here on the CSV is gone, as it would be on the cloud machine
        (tmp_path / "p.csv").unlink(missing_ok=True)
        return cloud.run_locally(payload)

    sr = cloud.solve_remote(cfg, run=run)
    assert sr.kpis["npv"] == pytest.approx(788_400 - 700_000, rel=1e-6)


def test_errors_come_back_as_the_same_errors():
    cfg = _cfg(store={"capex_energy": 700_000})
    del cfg["components"][2]["opex"]
    with pytest.raises(ValidationError) as e:
        cloud.solve_remote(cfg, run=cloud.run_locally)
    assert any("OPEX required" in m for m in e.value.errors)
    multi = _cfg(store={"capex_energy": 1})
    multi["lifetime"] = {"years": 3}
    with pytest.raises(ValidationError, match="multi-year"):
        cloud.solve_remote(multi, run=cloud.run_locally)


def test_single_period_run_in_the_cloud():
    cfg = _cfg(store={"capex_energy": 1})
    del cfg["lifetime"]
    cfg["components"][2].update(optimise_size=False, e_max=1, p_ch_max=1, p_dis_max=1)
    res = cloud.solve_remote(cfg, run=cloud.run_locally)
    assert res.total_cost == pytest.approx(220 - 180, abs=1e-3)       # see test_lifetime: 220 - 180 * e_max


def test_availability_is_reported_with_a_reason():
    ok, why = cloud.available()
    assert isinstance(ok, bool) and (ok or why)


def test_ui_run_with_compute_cloud(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from oems.ui.server import create_app

    app = create_app(tmp_path / "ws")
    client = TestClient(app)
    app.state.runs.cloud_runner = cloud.run_locally
    idx = pd.date_range("2025-01-01", periods=24, freq="1h")
    csv = pd.DataFrame({"price": 50 + 40 * np.sin(np.arange(24) / 3), "load": 0.5}, index=idx).rename_axis("time").to_csv()
    ds = client.post("/api/datasets", files={"file": ("d.csv", csv.encode(), "text/csv")}).json()
    graph = {"settings": {"start": "2025-01-01", "days": 1, "freq": "1h", "tz": None, "run_mode": "optimise",
                          "sim_years": 2, "compute": "cloud"},
             "nodes": [{"id": "1", "kind": "bus", "name": "el", "carrier": "electricity", "carrier_kind": "electricity"},
                       {"id": "2", "kind": "data", "dataset": ds["id"]},
                       {"id": "3", "kind": "component", "type": "GridConnection", "name": "dso", "params": {}},
                       {"id": "4", "kind": "component", "type": "Storage", "name": "bess", "params": {},
                        "sizing": {"optimise": True, "e_size_max": 2, "capex_energy": 1000, "capex_power": 100,
                                   "opex": 10}},
                       {"id": "5", "kind": "component", "type": "Demand", "name": "load", "params": {}},
                       {"id": "9", "kind": "invest", "discount_rate": 5}],
             "edges": [{"from": ["3", "link"], "to": ["1", "port"]}, {"from": ["4", "link"], "to": ["1", "port"]},
                       {"from": ["5", "in"], "to": ["1", "port"]}, {"from": ["2", "price"], "to": ["3", "import_price"]},
                       {"from": ["2", "load"], "to": ["5", "profile"]}]}
    run_id = client.post("/api/runs", json=graph).json()["run_id"]
    for _ in range(200):
        st = client.get(f"/api/runs/{run_id}").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    assert "(cloud, peak" in st["message"] and st["cloud"] is True
    s = client.get(f"/api/runs/{run_id}/summary").json()
    assert s["sizing"]["kpis"]["years"] == 2 and s["steps"] == 24          # one base day, weighted for 2 years
    status = client.get("/api/cloud").json()
    assert set(status) == {"available", "reason", "local_gb", "cloud_gb"}


def test_a_cloud_run_can_be_stopped():
    """Setting the cancel event stops the jobs that have not finished (in the cloud: their machines) - RunCancelled."""
    import threading

    from oems.exceptions import RunCancelled

    cancel = threading.Event()

    def run(payload):                   # the first model finishes, then the user presses Stop
        out = cloud.run_locally(payload)
        cancel.set()
        return out

    with pytest.raises(RunCancelled):
        cloud.solve_remote(_cfg(store={"capex_energy": 700_000}), run=run, cancel=cancel)


def test_with_hurdles_the_cloud_solves_the_reference_first():
    """The sized model is bounded by the reference case's result: two jobs one after the other, same as locally."""
    cfg = _cfg(years=4, rate=0.05, store={"capex_energy": 400_000, "e_size_max": 0.5},
               pv={"capex": 500_000, "capacity_max": 5})
    cfg["lifetime"]["hurdles"] = {"min_irr": 0.6}
    stages, jobs = [], []

    def run(payload):
        import pickle
        jobs.append(pickle.loads(payload)[0]["lifetime"].get("hurdle_limits"))
        return cloud.run_locally(payload)

    remote = cloud.solve_remote(cfg, run=run, on_stage=lambda k, n, label: stages.append((k, n)))
    local = optimise_lifetime(cfg)
    assert stages == [(1, 2), (2, 2)] and jobs[0] is None and len(jobs[1]) == 1
    assert remote.kpis["irr"] == pytest.approx(0.6, abs=1e-4)
    assert remote.kpis["npv"] == pytest.approx(local.kpis["npv"], rel=1e-6)

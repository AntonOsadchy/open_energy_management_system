"""Integration: UI backend (FastAPI) - catalog, datasets, graph translation, validation and runs."""

import time

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from oems.ui.graph import GraphError, graph_to_config  # noqa: E402
from oems.ui.server import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(tmp_path / "ws"))


def _csv(freq="1h", n=48, tz=None):
    idx = pd.date_range("2025-01-01", periods=n, freq=freq, tz=tz)
    df = pd.DataFrame({"price": 50 + 30 * np.sin(np.arange(n) / 3), "load": 0.5 + 0.1 * np.cos(np.arange(n))}, index=idx)
    return df.rename_axis("time").to_csv().encode()


def _graph(ds_id, days=2, freq="1h"):
    """Bus el + grid (price wired from data) + battery + demand (profile wired from data)."""
    return {
        "settings": {"start": "2025-01-01", "days": days, "freq": freq, "tz": None, "co2_price": ""},
        "nodes": [
            {"id": "1", "kind": "bus", "name": "el", "carrier": "electricity", "carrier_kind": "electricity"},
            {"id": "2", "kind": "data", "dataset": ds_id},
            {"id": "3", "kind": "component", "type": "GridConnection", "name": "dso", "params": {"max_export": 1}},
            {"id": "4", "kind": "component", "type": "Storage", "name": "bess",
             "params": {"e_max": 1, "p_ch_max": 0.5, "p_dis_max": 0.5}},
            {"id": "5", "kind": "component", "type": "Demand", "name": "load", "params": {}},
        ],
        "edges": [
            {"from": ["3", "import"], "to": ["1", "supply"]}, {"from": ["1", "withdraw"], "to": ["3", "export"]},
            {"from": ["4", "discharge"], "to": ["1", "supply"]}, {"from": ["1", "withdraw"], "to": ["4", "charge"]},
            {"from": ["1", "withdraw"], "to": ["5", "in"]},
            {"from": ["2", "price"], "to": ["3", "import_price"]}, {"from": ["2", "price"], "to": ["3", "export_price"]},
            {"from": ["2", "load"], "to": ["5", "profile"]},
        ],
    }


def test_catalog_lists_components_with_connectors(client):
    """The palette catalog lists every component with its energy connectors (direction, carrier kind), data connectors, presets and partner ports."""
    cat = client.get("/api/catalog").json()
    types = {c["type"]: c for c in cat["components"]}
    assert {"GridConnection", "Storage", "Converter", "ThermalZone", "FlexibleDemand"} <= set(types)
    storage_ports = {e["port"]: e["direction"] for e in types["Storage"]["energy"]}
    assert storage_ports == {"charge": "in", "discharge": "out"}
    assert "import_price" in types["GridConnection"]["data_connectors"]
    assert types["ThermalZone"]["energy"][1]["carrier_kind"] == "cold"
    assert any(p["id"] == "heat_pump" for p in cat["presets"])


@pytest.mark.parametrize("freq", ["1h", "15min"])
def test_dataset_upload_detects_resolution(client, freq):
    """An uploaded CSV is parsed, its resolution (1h / 15min) detected and a sparkline preview returned."""
    r = client.post("/api/datasets", files={"file": ("prices.csv", _csv(freq), "text/csv")})
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["resolution"] == freq and meta["columns"] == ["price", "load"]
    assert len(meta["preview"]["price"]) > 0


def test_bad_csv_rejected(client):
    """A CSV without a datetime column is rejected with HTTP 400."""
    r = client.post("/api/datasets", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 400


def test_graph_to_config_maps_wires(client):
    """Energy wires set the components' bus fields, data wires become `{series: dataset:column}` references, days x resolution gives the step count."""
    ds = client.post("/api/datasets", files={"file": ("d.csv", _csv(), "text/csv")}).json()
    cfg, name_to_node = graph_to_config(_graph(ds["id"]), {ds["id"]: "x.csv"})
    comps = {c["name"]: c for c in cfg["components"]}
    assert comps["dso"]["bus"] == "el" and comps["bess"]["bus"] == "el"
    assert comps["dso"]["import_price"] == {"series": f"{ds['id']}:price"}
    assert comps["load"]["profile"] == {"series": f"{ds['id']}:load"}
    assert cfg["time"]["periods"] == 48
    assert name_to_node == {"el": "1", "dso": "3", "bess": "4", "load": "5"}


def test_graph_errors_are_keyed_by_node():
    """Storage ports wired to two different buses, and an unconnected demand, are reported per node."""
    g = {"settings": {}, "nodes": [
        {"id": "1", "kind": "bus", "name": "a", "carrier_kind": "electricity"},
        {"id": "2", "kind": "bus", "name": "b", "carrier_kind": "electricity"},
        {"id": "3", "kind": "component", "type": "Storage", "name": "s", "params": {}},
        {"id": "4", "kind": "component", "type": "Demand", "name": "d", "params": {}}],
        "edges": [{"from": ["3", "discharge"], "to": ["1", "supply"]}, {"from": ["2", "withdraw"], "to": ["3", "charge"]}]}
    with pytest.raises(GraphError) as exc:
        graph_to_config(g, {})
    assert "different buses" in " ".join(exc.value.errors["3"])
    assert "not connected" in " ".join(exc.value.errors["4"])


def test_converter_graph():
    """Converter connectors in1/out1 map to inputs/outputs by the wired bus; capacity follows the selection."""
    g = {"settings": {"days": 1}, "nodes": [
        {"id": "1", "kind": "bus", "name": "el", "carrier_kind": "electricity"},
        {"id": "2", "kind": "bus", "name": "heat", "carrier_kind": "heat"},
        {"id": "3", "kind": "component", "type": "Converter", "name": "hp", "params": {"p_max": 2},
         "converter": {"inputs": [{"key": "in1", "coef": 1}], "outputs": [{"key": "out1", "coef": 3}], "capacity": "out1"}}],
        "edges": [{"from": ["1", "withdraw"], "to": ["3", "in1"]}, {"from": ["3", "out1"], "to": ["2", "supply"]}]}
    cfg, _ = graph_to_config(g, {})
    hp = cfg["components"][0]
    assert hp["inputs"] == {"el": 1} and hp["outputs"] == {"heat": 3} and hp["capacity_port"] == "heat"


def test_validate_endpoint_reports_missing_parameter(client):
    """A missing required parameter (storage e_max) is reported by /api/validate under the block's node id."""
    ds = client.post("/api/datasets", files={"file": ("d.csv", _csv(), "text/csv")}).json()
    g = _graph(ds["id"])
    g["nodes"][3]["params"] = {"p_ch_max": 0.5, "p_dis_max": 0.5}  # e_max missing
    r = client.post("/api/validate", json=g).json()
    assert not r["ok"] and "4" in r["errors"]


def test_run_end_to_end(client):
    """Run via the API; the wire energies in the summary equal the flow matrix of the results."""
    ds = client.post("/api/datasets", files={"file": ("d.csv", _csv(), "text/csv")}).json()
    run = client.post("/api/runs", json=_graph(ds["id"])).json()
    assert run["run_id"]
    for _ in range(200):
        st = client.get(f"/api/runs/{run['run_id']}").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    s = client.get(f"/api/runs/{run['run_id']}/summary").json()
    assert s["steps"] == 48 and s["total_cost"] is not None
    load_in = [e for e in s["edges"] if e["component"] == "load"][0]
    expected = pd.read_csv(pd.io.common.BytesIO(_csv()))["load"].sum()
    assert load_in["MWh"] == pytest.approx(expected, rel=1e-6)
    ser = client.get(f"/api/runs/{run['run_id']}/series", params={"name": "el"}).json()
    assert "shadow_price" in ser["series"]
    z = client.get(f"/api/runs/{run['run_id']}/download")
    assert z.status_code == 200 and z.content[:2] == b"PK"


def test_run_at_15min_with_hourly_data(client):
    """Hourly data drives a 15-min model (data are aligned automatically)."""
    ds = client.post("/api/datasets", files={"file": ("d.csv", _csv("1h"), "text/csv")}).json()
    run = client.post("/api/runs", json=_graph(ds["id"], days=1, freq="15min")).json()
    for _ in range(200):
        st = client.get(f"/api/runs/{run['run_id']}").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    assert client.get(f"/api/runs/{run['run_id']}/summary").json()["steps"] == 96


def test_infeasible_run_points_at_bus(client):
    """An infeasible run (grid too small, no storage) fails and the diagnosis is attached to the bus block where the balance fails."""
    ds = client.post("/api/datasets", files={"file": ("d.csv", _csv(), "text/csv")}).json()
    g = _graph(ds["id"])
    g["nodes"][2]["params"]["max_import"] = 0.01
    g["nodes"][3]["params"]["e_max"] = 0.0
    run = client.post("/api/runs", json=g).json()
    for _ in range(200):
        st = client.get(f"/api/runs/{run['run_id']}").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "failed" and "1" in st["errors"]


def test_projects_save_and_load(client):
    """Projects (settings + canvas layout) are saved to and loaded from the workspace."""
    client.post("/api/projects/my site", json={"settings": {"name": "my site"}, "drawflow": {}})
    assert "my_site" in client.get("/api/projects").json()
    assert client.get("/api/projects/my site").json()["settings"]["name"] == "my site"


def test_index_and_static(client):
    """The UI page and all static scripts/styles are served."""
    assert client.get("/").status_code == 200
    for f in ("app.js", "blocks.js", "panel.js", "results.js", "style.css"):
        assert client.get(f"/static/{f}").status_code == 200


def test_tutorial_dataset_is_registered_once(client):
    """POST /api/tutorial/data adds the tutorial dataset (48 hourly rows, price/pv_cf/heat_load) and returns the
    same dataset on a second call instead of a duplicate."""
    a = client.post("/api/tutorial/data").json()
    b = client.post("/api/tutorial/data").json()
    assert a["id"] == b["id"]
    assert a["columns"] == ["price", "pv_cf", "heat_load"] and a["rows"] == 48 and a["resolution"] == "1h"
    assert [d["name"] for d in client.get("/api/datasets").json()].count("tutorial_data") == 1


def test_page_and_scripts_are_revalidated(client):
    """The page and static scripts are sent with Cache-Control: no-cache, so after an update the browser never
    runs old cached scripts against the new page."""
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"


def test_single_link_wire_equals_two_port_wires(client):
    """A grid or storage wired with its single ⇄ `link` connector gives exactly the same config as wiring
    import/export (charge/discharge) separately, so old two-wire projects and new one-wire projects agree."""
    ds = client.post("/api/datasets", files={"file": ("d.csv", _csv(), "text/csv")}).json()
    old = _graph(ds["id"])
    new = _graph(ds["id"])
    new["edges"] = [e for e in new["edges"] if e["from"][1] not in ("import", "discharge")
                    and e["to"][1] not in ("export", "charge")]
    new["edges"] += [{"from": ["3", "link"], "to": ["1", "supply"]}, {"from": ["1", "withdraw"], "to": ["4", "link"]}]
    assert len(new["edges"]) == len(old["edges"]) - 2
    assert graph_to_config(new, {ds["id"]: "x.csv"}) == graph_to_config(old, {ds["id"]: "x.csv"})


def test_catalog_declares_link_connectors(client):
    """Grid connection and storage declare one ⇄ link connector for both ports; other components have none."""
    comps = {c["type"]: c for c in client.get("/api/catalog").json()["components"]}
    assert comps["GridConnection"]["link"]["ports"] == ["import", "export"]
    assert comps["Storage"]["link"]["ports"] == ["charge", "discharge"]
    assert comps["Demand"]["link"] is None

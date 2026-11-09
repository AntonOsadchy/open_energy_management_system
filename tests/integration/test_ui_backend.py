"""Integration: UI backend (FastAPI) - catalog, datasets, graph translation, validation and runs."""

import io
import json
import time
import zipfile

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
            {"from": ["3", "import"], "to": ["1", "port"]}, {"from": ["3", "export"], "to": ["1", "port"]},
            {"from": ["4", "discharge"], "to": ["1", "port"]}, {"from": ["4", "charge"], "to": ["1", "port"]},
            {"from": ["5", "in"], "to": ["1", "port"]},
            {"from": ["2", "price"], "to": ["3", "import_price"]}, {"from": ["2", "price"], "to": ["3", "export_price"]},
            {"from": ["2", "load"], "to": ["5", "profile"]},
        ],
    }


def test_catalog_lists_components_with_connectors(client):
    """The palette catalog lists every component with its energy connectors (direction, carrier kind), data connectors and presets."""
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


def test_note_blocks_are_ignored_by_the_run(client):
    """A note block only holds text: the config is the same with or without it."""
    ds = client.post("/api/datasets", files={"file": ("d.csv", _csv(), "text/csv")}).json()
    g = _graph(ds["id"])
    with_note = {**g, "nodes": g["nodes"] + [{"id": "99", "kind": "note", "text": "check the tariff", "ins": [], "outs": []}]}
    assert graph_to_config(with_note, {ds["id"]: "x.csv"}) == graph_to_config(g, {ds["id"]: "x.csv"})


def test_graph_errors_are_keyed_by_node():
    """Storage ports wired to two different buses, and an unconnected demand, are reported per node."""
    g = {"settings": {}, "nodes": [
        {"id": "1", "kind": "bus", "name": "a", "carrier_kind": "electricity"},
        {"id": "2", "kind": "bus", "name": "b", "carrier_kind": "electricity"},
        {"id": "3", "kind": "component", "type": "Storage", "name": "s", "params": {}},
        {"id": "4", "kind": "component", "type": "Demand", "name": "d", "params": {}}],
        "edges": [{"from": ["3", "discharge"], "to": ["1", "port"]}, {"from": ["3", "charge"], "to": ["2", "port"]}]}
    with pytest.raises(GraphError) as exc:
        graph_to_config(g, {})
    assert "different buses" in " ".join(exc.value.errors["3"])
    assert "not connected" in " ".join(exc.value.errors["4"])


def test_port_wires_take_their_direction_from_the_port():
    """Energy wires have no direction: component port -> bus port is an inflow or outflow by the port's meaning, a
    `link` port is both, and an unknown port is an error."""
    g = {"settings": {"days": 1}, "nodes": [
        {"id": "1", "kind": "bus", "name": "el", "carrier_kind": "electricity"},
        {"id": "2", "kind": "component", "type": "GridConnection", "name": "dso", "params": {}},
        {"id": "3", "kind": "component", "type": "Storage", "name": "bess", "params": {"e_max": 1}},
        {"id": "4", "kind": "component", "type": "Demand", "name": "load", "params": {"profile": 1}},
        {"id": "5", "kind": "component", "type": "Producer", "name": "pv", "params": {"capacity": 1}}],
        "edges": [{"from": [n, p], "to": ["1", "port"]} for n, p in (("2", "link"), ("3", "link"), ("4", "in"), ("5", "out"))]}
    cfg, _ = graph_to_config(g, {})
    assert all(c["bus"] == "el" for c in cfg["components"])
    g["edges"].append({"from": ["4", "nope"], "to": ["1", "port"]})
    with pytest.raises(GraphError) as exc:
        graph_to_config(g, {})
    assert "unknown energy port 'nope'" in exc.value.errors["4"]


def test_converter_ports_take_direction_from_its_definition():
    """A converter's in/out keys decide the direction of its port wires, even when both go to one bus side."""
    g = {"settings": {"days": 1}, "nodes": [
        {"id": "1", "kind": "bus", "name": "a", "carrier_kind": "other"},
        {"id": "2", "kind": "bus", "name": "b", "carrier_kind": "other"},
        {"id": "3", "kind": "component", "type": "Converter", "name": "lnk", "params": {},
         "converter": {"inputs": [{"key": "in1", "coef": 1}], "outputs": [{"key": "out1", "coef": 0.9}], "capacity": "in1"}}],
        "edges": [{"from": ["3", "out1"], "to": ["1", "port"]}, {"from": ["3", "in1"], "to": ["2", "port"]}]}
    cfg, _ = graph_to_config(g, {})
    assert cfg["components"][0]["inputs"] == {"b": 1} and cfg["components"][0]["outputs"] == {"a": 0.9}


def test_legacy_supply_withdraw_wires_still_map():
    """Graphs from older projects (component -> bus 'supply', bus 'withdraw' -> component) give the same config."""
    g = {"settings": {"days": 1}, "nodes": [
        {"id": "1", "kind": "bus", "name": "el", "carrier_kind": "electricity"},
        {"id": "2", "kind": "component", "type": "GridConnection", "name": "dso", "params": {}},
        {"id": "3", "kind": "component", "type": "Demand", "name": "load", "params": {"profile": 1}}],
        "edges": [{"from": ["2", "link"], "to": ["1", "supply"]}, {"from": ["1", "withdraw"], "to": ["3", "in"]}]}
    cfg, _ = graph_to_config(g, {})
    assert [c["bus"] for c in cfg["components"]] == ["el", "el"]


def test_converter_graph():
    """Converter connectors in1/out1 map to inputs/outputs by the wired bus; capacity follows the selection."""
    g = {"settings": {"days": 1}, "nodes": [
        {"id": "1", "kind": "bus", "name": "el", "carrier_kind": "electricity"},
        {"id": "2", "kind": "bus", "name": "heat", "carrier_kind": "heat"},
        {"id": "3", "kind": "component", "type": "Converter", "name": "hp", "params": {"p_max": 2},
         "converter": {"inputs": [{"key": "in1", "coef": 1}], "outputs": [{"key": "out1", "coef": 3}], "capacity": "out1"}}],
        "edges": [{"from": ["3", "in1"], "to": ["1", "port"]}, {"from": ["3", "out1"], "to": ["2", "port"]}]}
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
    """Projects (settings + canvas) are saved to and loaded from the logged-in user's workspace folder."""
    client.post("/api/auth/register", json={"user": "ana", "password": "secret1"})
    client.post("/api/projects/my site", json={"settings": {"name": "my site"}, "drawflow": {}})
    assert [p["id"] for p in client.get("/api/projects").json()] == ["my_site"]
    assert client.get("/api/projects/my site").json()["settings"]["name"] == "my site"


def test_projects_need_login_and_are_per_user(tmp_path):
    """Cases are only visible to the user who saved them; without a login the project API answers 401."""
    ana, bo = TestClient(create_app(tmp_path / "ws")), TestClient(create_app(tmp_path / "ws"))
    assert ana.get("/api/projects").status_code == 401
    assert ana.post("/api/projects/x", json={}).status_code == 401
    assert ana.get("/api/auth/me").json() == {"user": None}
    assert ana.post("/api/auth/register", json={"user": "ana", "password": "short"}).status_code == 400
    assert ana.post("/api/auth/register", json={"user": "..", "password": "secret1"}).status_code == 400
    assert ana.post("/api/auth/register", json={"user": "ana", "password": "secret1"}).json() == {"user": "ana"}
    assert ana.post("/api/auth/register", json={"user": "ANA", "password": "secret1"}).status_code == 400
    assert bo.post("/api/auth/register", json={"user": "bo", "password": "secret2"}).status_code == 200
    ana.post("/api/projects/plant", json={"settings": {"name": "plant", "start": "2025-01-01", "days": 7},
                                          "drawflow": {"drawflow": {"Home": {"data": {"1": {}, "2": {}}}}}})
    [meta] = ana.get("/api/projects").json()
    assert meta | {"saved": 0} == {"id": "plant", "name": "plant", "start": "2025-01-01", "days": 7, "freq": None,
                                    "blocks": 2, "saved": 0, "parent": None}
    assert bo.get("/api/projects").json() == []
    assert bo.get("/api/projects/plant").status_code == 404
    assert bo.delete("/api/projects/plant").status_code == 404

    # log out and back in (wrong password first); the session survives a server restart
    ana.post("/api/auth/logout")
    assert ana.get("/api/projects").status_code == 401
    assert ana.post("/api/auth/login", json={"user": "ana", "password": "nope00"}).status_code == 401
    ana.post("/api/auth/login", json={"user": "ana", "password": "secret1"})
    again = TestClient(create_app(tmp_path / "ws"), cookies=dict(ana.cookies))
    assert again.get("/api/auth/me").json() == {"user": "ana"}

    # rename (also the name in the settings), refuse renaming onto another case, delete
    ana.post("/api/projects/other", json={"settings": {"name": "other"}})
    r = ana.patch("/api/projects/plant", json={"name": "plant v2"})
    assert r.json() == {"renamed": "plant_v2"}
    assert ana.get("/api/projects/plant").status_code == 404
    assert ana.get("/api/projects/plant v2").json()["settings"]["name"] == "plant v2"
    assert ana.patch("/api/projects/other", json={"name": "plant v2"}).status_code == 409
    assert ana.delete("/api/projects/other").json() == {"deleted": "other"}
    assert [p["name"] for p in ana.get("/api/projects").json()] == ["plant v2"]


def test_project_variants_keep_their_parent_through_rename_and_delete(client):
    """A project saved with "variant_of" is listed with its parent (the sidebar project tree nests it there); renaming
    the parent follows it, and deleting a project moves its variants up to that project's own parent."""
    client.post("/api/auth/register", json={"user": "ana", "password": "secret1"})
    client.post("/api/projects/base", json={"settings": {"name": "base"}})
    client.post("/api/projects/bigger bess", json={"settings": {"name": "bigger bess"}, "variant_of": "base"})
    client.post("/api/projects/bess + pv", json={"settings": {"name": "bess + pv"}, "variant_of": "bigger bess"})
    parents = lambda: {p["name"]: p["parent"] for p in client.get("/api/projects").json()}  # noqa: E731
    assert parents() == {"base": None, "bigger bess": "base", "bess + pv": "bigger bess"}
    client.patch("/api/projects/base", json={"name": "site 2026"})
    assert parents() == {"site 2026": None, "bigger bess": "site 2026", "bess + pv": "bigger bess"}
    assert client.delete("/api/projects/bigger_bess").status_code == 200   # by its id, as the Open window does
    assert parents() == {"site 2026": None, "bess + pv": "site 2026"}


def test_first_account_adopts_projects_saved_before_logins(tmp_path):
    (tmp_path / "ws" / "projects").mkdir(parents=True)
    (tmp_path / "ws" / "projects" / "old.json").write_text('{"settings": {"name": "old"}}')
    c = TestClient(create_app(tmp_path / "ws"))
    c.post("/api/auth/register", json={"user": "ana", "password": "secret1"})
    assert [p["name"] for p in c.get("/api/projects").json()] == ["old"]


def _finished_run(client, graph):
    run_id = client.post("/api/runs", json=graph).json()["run_id"]
    for _ in range(200):
        st = client.get(f"/api/runs/{run_id}").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    return run_id


def test_daily_profile_block_repeats_on_every_day(client):
    """A daily-profile block wired to the demand becomes `{daily: [...]}` in the config; the run draws that day on both
    days, and the per-step table lists it as a 'daily profile' input. A profile that does not split a day is an
    error on its block."""
    ds = client.post("/api/datasets", files={"file": ("site.csv", _csv(), "text/csv")}).json()
    day = [0.2] * 7 + [0.8] * 12 + [0.4] * 5
    g = _graph(ds["id"])
    g["nodes"].append({"id": "6", "kind": "profile", "name": "office", "values": day})
    g["edges"] = [e for e in g["edges"] if e["to"] != ["5", "profile"]] + [{"from": ["6", "value"], "to": ["5", "profile"]}]
    cfg, _ = graph_to_config(g, {ds["id"]: "x.csv"})
    assert next(c for c in cfg["components"] if c["name"] == "load")["profile"] == {"daily": day}
    run_id = _finished_run(client, g)
    t = client.get(f"/api/runs/{run_id}/timeseries?limit=48").json()
    df = pd.DataFrame(t["data"], columns=t["columns"])
    assert np.allclose(df["flow el → load.in [MW]"], day * 2)
    assert np.allclose(df["input load.profile [MW] (daily profile)"], day * 2)
    g["nodes"][-1]["values"] = [1.0] * 25
    with pytest.raises(GraphError) as e:
        graph_to_config(g, {ds["id"]: "x.csv"})
    assert "24, 48 or 96 values" in " ".join(e.value.errors["6"])


def test_simulation_save_reopen_and_delete(client, tmp_path):
    """A finished run saved as a simulation keeps its canvas, config, summary, plot series and CSV zip; it is
    listed, served unchanged after a server restart (new app on the same workspace), can be renamed and deleted."""
    ds = client.post("/api/datasets", files={"file": ("d.csv", _csv(), "text/csv")}).json()
    run_id = _finished_run(client, _graph(ds["id"]))
    project = {"settings": {"name": "site", "start": "2025-01-01", "days": 2}, "drawflow": {"drawflow": {"Home": {"data": {}}}}}
    meta = client.post("/api/simulations", json={"run_id": run_id, "name": "base case", "project": project}).json()
    live = client.get(f"/api/runs/{run_id}/summary").json()
    assert meta["name"] == "base case" and meta["total_cost"] == live["total_cost"] and meta["steps"] == 48

    client2 = TestClient(create_app(tmp_path / "ws"))       # restart: the run is gone, the simulation is not
    assert client2.get(f"/api/runs/{run_id}").status_code == 404
    assert [m["id"] for m in client2.get("/api/simulations").json()] == [meta["id"]]
    base = f"/api/simulations/{meta['id']}"
    assert client2.get(base).json()["project"] == project
    assert client2.get(f"{base}/summary").json() == live
    assert client2.get(f"{base}/series", params={"name": "el"}).json() == \
        client.get(f"/api/runs/{run_id}/series", params={"name": "el"}).json()
    assert client2.get(f"{base}/series", params={"name": "nope"}).status_code == 404
    assert client2.get(f"{base}/config").json()["time"]["periods"] == 48
    assert client2.get(f"{base}/download").content[:2] == b"PK"

    assert client2.patch(base, json={"name": "renamed", "notes": "n"}).json()["name"] == "renamed"
    assert client2.get("/api/simulations").json()[0]["notes"] == "n"
    assert client2.patch(base, json={"project_name": "site v2"}).json()["project_name"] == "site v2"   # project renamed
    assert client2.delete(base).status_code == 200
    assert client2.get(base).status_code == 404 and client2.get("/api/simulations").json() == []


def test_simulation_save_needs_finished_run(client):
    """Saving an unknown run is a 404; an unknown or malformed simulation id is a 404 (no path traversal)."""
    assert client.post("/api/simulations", json={"run_id": "nope", "name": "x"}).status_code == 404
    assert client.get("/api/simulations/0123456789ab/summary").status_code == 404
    assert client.get("/api/simulations/..%2Fprojects/summary").status_code == 404


def test_index_and_static(client):
    """The UI page and all static scripts/styles are served."""
    assert client.get("/").status_code == 200
    for f in ("app.js", "blocks.js", "panel.js", "results.js", "simulations.js", "style.css"):
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
    new["edges"] = [e for e in new["edges"] if e["from"][1] not in ("import", "export", "charge", "discharge")]
    new["edges"] += [{"from": ["3", "link"], "to": ["1", "port"]}, {"from": ["4", "link"], "to": ["1", "port"]}]
    assert len(new["edges"]) == len(old["edges"]) - 2
    assert graph_to_config(new, {ds["id"]: "x.csv"}) == graph_to_config(old, {ds["id"]: "x.csv"})


def test_catalog_declares_link_connectors(client):
    """Grid connection and storage declare one ⇄ link connector for both ports; other components have none."""
    comps = {c["type"]: c for c in client.get("/api/catalog").json()["components"]}
    assert comps["GridConnection"]["link"]["ports"] == ["import", "export"]
    assert comps["Storage"]["link"]["ports"] == ["charge", "discharge"]
    assert comps["Demand"]["link"] is None


def test_per_step_table_of_flows_states_prices_and_inputs(client):
    """The time-series table has one row per step with every wire's flow, storage states, bus marginal prices and
    the input series the optimiser used (named after their dataset); each step balances the bus; it pages as JSON,
    downloads as CSV, is in the results zip and stays available from a saved simulation."""
    ds = client.post("/api/datasets", files={"file": ("site.csv", _csv(), "text/csv")}).json()
    run_id = _finished_run(client, _graph(ds["id"]))
    t = client.get(f"/api/runs/{run_id}/timeseries?offset=5&limit=10").json()
    assert t["rows"] == 48 and t["offset"] == 5 and len(t["data"]) == 10 and len(t["time"]) == 10
    cols = t["columns"]
    assert {"flow dso.import → el [MW]", "flow el → dso.export [MW]", "flow bess.discharge → el [MW]",
            "flow el → bess.charge [MW]", "flow el → load.in [MW]", "state bess.e [MWh]", "state bess.soc [0-1]",
            "price el [EUR/MWh]",
            "input dso.import_price [EUR/MWh] (site:price)", "input load.profile [MW] (site:load)"} <= set(cols)
    df = pd.DataFrame(t["data"], columns=cols)
    supply = df["flow dso.import → el [MW]"] + df["flow bess.discharge → el [MW]"]
    demand = df["flow el → dso.export [MW]"] + df["flow el → bess.charge [MW]"] + df["flow el → load.in [MW]"]
    assert np.allclose(supply, demand, atol=1e-6)
    assert np.allclose(df["flow el → load.in [MW]"], df["input load.profile [MW] (site:load)"])
    src = pd.read_csv(io.BytesIO(_csv()), index_col=0)
    assert np.allclose(df["input dso.import_price [EUR/MWh] (site:price)"], src["price"].iloc[5:15])

    # system profit per step (from the optimiser's cost expressions) adds up to the run's profit
    full = pd.read_csv(io.StringIO(client.get(f"/api/runs/{run_id}/timeseries?format=csv").text), index_col=0)
    summary = client.get(f"/api/runs/{run_id}/summary").json()
    period = sum(c["EUR"] for c in summary["profit"]["period_charges"])
    assert full["econ profit [EUR]"].sum() - period == pytest.approx(-summary["total_cost"], abs=1e-4)
    assert summary["profit"]["total"] == pytest.approx(-summary["total_cost"])
    assert full["econ profit cumulative [EUR]"].iloc[-1] == pytest.approx(full["econ profit [EUR]"].sum(), abs=1e-4)
    csv = pd.read_csv(io.StringIO(client.get(f"/api/runs/{run_id}/timeseries?format=csv").text), index_col=0)
    assert csv.shape == (48, len(cols)) and not np.signbit(csv.to_numpy()[csv.to_numpy() == 0]).any()
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/runs/{run_id}/download").content)) as z:
        assert "timeseries.csv" in z.namelist()
    meta = client.post("/api/simulations", json={"run_id": run_id, "name": "with table"}).json()
    saved = client.get(f"/api/simulations/{meta['id']}/timeseries?offset=5&limit=10").json()
    assert saved["columns"] == cols and np.allclose(np.array(saved["data"], float), np.array(t["data"], float))


def test_inspector_series_and_column_filter(client):
    """The inspector gets whole series of chosen columns (thinned to bin means only above its point limit), a table
    page and a CSV restricted to the chosen columns; an unknown column is a 404."""
    ds = client.post("/api/datasets", files={"file": ("site.csv", _csv(), "text/csv")}).json()
    run_id = _finished_run(client, _graph(ds["id"]))
    base = f"/api/runs/{run_id}/timeseries"
    cols = ["price el [EUR/MWh]", "flow el → load.in [MW]"]
    q = json.dumps(cols)
    s = client.get(base, params={"format": "series", "cols": q}).json()
    assert (s["rows"], s["points"], len(s["time"])) == (48, 48, 48) and set(s["series"]) == set(cols)
    page = client.get(base, params={"cols": q, "offset": 40, "limit": 100}).json()
    assert page["columns"] == cols and len(page["data"]) == 8
    assert [r[0] for r in page["data"]] == s["series"][cols[0]][40:]
    csv = client.get(base, params={"format": "csv", "cols": q}).text.splitlines()
    assert csv[0] == "time," + ",".join(cols) and len(csv) == 49
    assert client.get(base, params={"format": "series", "cols": json.dumps(["nope"])}).status_code == 404


def test_excel_downloads_and_report(client):
    """Inspector downloads: the per-step table as Excel (all columns plus a Summary sheet, or only the selected
    columns) reads back to the same numbers as the CSV. The report is one HTML page with the system picture, key
    inputs, key outputs and charts, for a live run and for the saved simulation."""
    ds = client.post("/api/datasets", files={"file": ("site.csv", _csv(), "text/csv")}).json()
    run_id = _finished_run(client, _graph(ds["id"]))
    base = f"/api/runs/{run_id}/timeseries"
    csv = pd.read_csv(io.StringIO(client.get(base, params={"format": "csv"}).text), index_col=0)
    r = client.get(base, params={"format": "xlsx"})
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    sheets = pd.read_excel(io.BytesIO(r.content), sheet_name=None, index_col=0)
    assert set(sheets) == {"Time series", "Summary"}
    assert np.allclose(sheets["Time series"].to_numpy(dtype=float), csv.to_numpy(dtype=float), atol=1e-9)
    cols = ["price el [EUR/MWh]", "econ profit [EUR]"]
    sel = pd.read_excel(io.BytesIO(client.get(base, params={"format": "xlsx", "cols": json.dumps(cols)}).content),
                        sheet_name=None, index_col=0)
    assert list(sel) == ["Time series"] and list(sel["Time series"].columns) == cols
    # ⤓ Download of the Summary / Costs tabs; Lifetime only exists for a multi-year run
    exp = f"/api/runs/{run_id}/export"
    summ = pd.read_excel(io.BytesIO(client.get(exp, params={"view": "summary", "format": "xlsx"}).content))
    assert {"Profit", "Total cost"} <= set(summ["item"])
    costs = pd.read_csv(io.StringIO(client.get(exp, params={"view": "costs"}).text))
    assert list(costs.columns) == ["component", "category", "EUR"] and "TIEBREAK" not in set(costs["category"])
    assert client.get(exp, params={"view": "lifetime"}).status_code == 404
    assert client.get(exp, params={"view": "kpis"}).status_code == 400

    canvas = {"drawflow": {"Home": {"data": {
        "1": {"id": 1, "pos_x": 400, "pos_y": 150, "data": {"kind": "bus", "name": "el", "carrier_kind": "electricity",
              "ins": [{"name": "port", "type": "bus"}], "outs": []}, "outputs": {}},
        "3": {"id": 3, "pos_x": 100, "pos_y": 100, "data": {"kind": "component", "type": "GridConnection",
              "name": "dso", "ins": [], "outs": [{"name": "link", "type": "energy"}]},
              "outputs": {"output_1": {"connections": [{"node": "1", "output": "input_1"}]}}},
        "4": {"id": 4, "pos_x": 700, "pos_y": 150, "data": {"kind": "component", "type": "Storage", "name": "bess",
              "ins": [], "outs": [{"name": "link", "type": "energy"}]},
              "outputs": {"output_1": {"connections": [{"node": "1", "output": "input_1"}]}}}}}}}
    project = {"settings": {"name": "report test", "start": "2025-01-01", "days": 2}, "drawflow": canvas}
    page = client.post(f"/api/runs/{run_id}/report", json={"project": project, "name": "report test"})
    assert page.headers["content-disposition"].endswith('report_test.html"')
    text = page.text
    for part in ("<h2>System</h2>", "<h2>Key inputs</h2>", "<h2>Key outputs</h2>", "<h2>Results</h2>",
                 'class="diagram"', ">dso<", ">bess<", "Energy flows on bus el", "Storage state of charge",
                 "Cumulative profit", "dso.import_price", "site:price"):
        assert part in text, part
    assert text.count("<svg") >= 4 and "<script" not in text           # self-contained, no scripts
    meta = client.post("/api/simulations", json={"run_id": run_id, "name": "saved case", "project": project}).json()
    saved = client.get(f"/api/simulations/{meta['id']}/report")
    assert saved.status_code == 200 and "<h2>Key outputs</h2>" in saved.text and "saved case" in saved.text
    sx = client.get(f"/api/simulations/{meta['id']}/timeseries", params={"format": "xlsx"})
    assert set(pd.read_excel(io.BytesIO(sx.content), sheet_name=None)) == {"Time series", "Summary"}


def test_simultaneous_flows_are_highlighted(client):
    """When a storage charges and discharges in the same step (here: burning energy at a negative price), the run
    summary carries a notice, the block a badge, and the per-step table a ``check`` column with the overlap."""
    idx = pd.date_range("2025-01-01", periods=48, freq="1h")
    price = np.where((np.arange(48) >= 6) & (np.arange(48) < 18), -50.0, 50.0)   # 12 h: more than the battery holds
    csv = pd.DataFrame({"price": price, "load": 0.5}, index=idx).rename_axis("time").to_csv().encode()
    ds = client.post("/api/datasets", files={"file": ("neg.csv", csv, "text/csv")}).json()
    graph = _graph(ds["id"])
    graph["nodes"][2]["params"] = {"max_export": 0}
    graph["nodes"][3]["params"].update(soc_init=0.9)
    run_id = _finished_run(client, graph)
    s = client.get(f"/api/runs/{run_id}/summary").json()
    assert len(s["notices"]) == 1 and s["notices"][0].startswith("bess: charge and discharge at the same time in")
    assert s["badges"]["4"]["simultaneous"] >= 1
    k = {(r["kpi"], r["scope"]): r["value"] for r in s["kpis"]}
    assert k[("simultaneous_steps", "bess")] >= 1 and k[("simultaneous_steps", "dso")] == 0
    t = pd.read_csv(io.StringIO(client.get(f"/api/runs/{run_id}/timeseries?format=csv").text), index_col=0)
    col = "check bess charge & discharge at once [MW]"
    assert col in t.columns and "check dso import & export at once [MW]" not in t.columns
    assert (t[col].iloc[6:18] > 0).any() and (t[col].iloc[:6] == 0).all()
    assert np.allclose(t[col], np.minimum(t["flow el → bess.charge [MW]"], t["flow bess.discharge → el [MW]"])
                       .where(lambda x: x > 1e-6, 0.0))


def _invest_graph(ds_id, sizing, years=3, rate=5):
    """``sizing``: {component node id: its ``sizing`` (variable size when ``optimise``)}"""
    g = _graph(ds_id, days=1)
    g["settings"].update({"run_mode": "optimise", "sim_years": years})
    for n in g["nodes"]:
        if n["id"] in sizing:
            n["sizing"] = sizing[n["id"]]
    g["nodes"].append({"id": "9", "kind": "invest", "discount_rate": rate})
    return g


def _bess(g):
    return next(n for n in g["nodes"] if n["id"] == "4")


def test_variable_size_merges_into_the_sized_components():
    g = _invest_graph("ds", {"4": {"optimise": True, "e_size_max": 4, "capex_energy": 2e5, "capex_power": 5e4,
                                   "opex": 3000, "duration": ""}})
    cfg, _ = graph_to_config(g, {"ds": "ds.csv"})
    bess = next(c for c in cfg["components"] if c["name"] == "bess")
    assert bess["optimise_size"] is True and bess["capex_energy"] == 2e5 and "duration" not in bess
    assert cfg["lifetime"] == {"mode": "optimise", "years": 3, "discount_rate": 0.05, "inflation": 0,
                               "model": "weighted"}
    g["nodes"][-1]["inflation"] = 2.5                         # %/yr in the Run settings block
    assert graph_to_config(g, {"ds": "ds.csv"})[0]["lifetime"]["inflation"] == pytest.approx(0.025)
    g["settings"]["lifetime_model"] = "full"                  # every year in sequence (full chronology)
    assert graph_to_config(g, {"ds": "ds.csv"})[0]["lifetime"]["model"] == "full"
    g["settings"].pop("lifetime_model")
    g["settings"]["run_mode"] = "single"            # other run modes use the fixed sizes
    g["settings"]["sim_years"] = 1
    cfg, _ = graph_to_config(g, {"ds": "ds.csv"})
    assert "optimise_size" not in next(c for c in cfg["components"] if c["name"] == "bess")
    assert "lifetime" not in cfg


def test_variable_size_without_run_settings_block_and_from_older_projects():
    g = _invest_graph("ds", {"4": {"optimise": True, "capex_energy": 1, "capex_power": 0, "opex": 0}})
    g["nodes"].pop()                                # no Run settings block: the default discount rate
    assert graph_to_config(g, {"ds": "ds.csv"})[0]["lifetime"]["discount_rate"] == pytest.approx(0.07)
    sizing = _bess(g).pop("sizing")                 # older projects: the assets of the Run settings block
    g["nodes"].append({"id": "9", "kind": "invest", "discount_rate": 5, "assets": {"4": sizing, "77": sizing}})
    cfg, _ = graph_to_config(g, {"ds": "ds.csv"})   # 77: a deleted block
    assert next(c for c in cfg["components"] if c["name"] == "bess")["optimise_size"] is True
    _bess(g)["sizing"] = {"optimise": False}        # the component's own setting wins
    with pytest.raises(GraphError):
        graph_to_config(g, {"ds": "ds.csv"})


def test_investment_hurdles_go_into_the_lifetime_config():
    g = _invest_graph("ds", {"4": {"optimise": True, "capex_energy": 1, "capex_power": 0, "opex": 0}})
    g["nodes"][-1].update(min_irr=12, max_payback=7.5, payback_kind="discounted")
    cfg, _ = graph_to_config(g, {"ds": "ds.csv"})
    assert cfg["lifetime"]["hurdles"] == {"min_irr": pytest.approx(0.12), "max_payback": 7.5, "payback": "discounted"}
    g["nodes"][-1].update(min_irr=None, max_payback="")             # blank: no hurdle
    cfg, _ = graph_to_config(g, {"ds": "ds.csv"})
    assert "hurdles" not in cfg["lifetime"] and "self_sufficiency" not in cfg["lifetime"]
    g["nodes"][-1]["min_self_sufficiency"] = 40
    assert graph_to_config(g, {"ds": "ds.csv"})[0]["lifetime"]["self_sufficiency"] == pytest.approx(0.4)


def test_variable_size_errors():
    g = _invest_graph("ds", {"4": {"optimise": False}})
    with pytest.raises(GraphError) as e:
        graph_to_config(g, {"ds": "ds.csv"})
    assert "at least one" in e.value.errors["_global"][0]
    g = _invest_graph("ds", {"5": {"optimise": True}})                     # a Demand cannot be sized
    g["nodes"].append({"id": "10", "kind": "invest"})
    with pytest.raises(GraphError) as e:
        graph_to_config(g, {"ds": "ds.csv"})
    assert "cannot be sized" in e.value.errors["5"][0] and "only one" in e.value.errors["10"][0]


def test_lifetime_optimisation_run_end_to_end(client):
    ds = client.post("/api/datasets", files={"file": ("d.csv", _csv(n=24), "text/csv")}).json()
    g = _invest_graph(ds["id"], {"4": {"optimise": True, "e_size_max": 4, "capex_energy": 1000,
                                       "capex_power": 100, "opex": 10}})
    run_id = _finished_run(client, g)
    s = client.get(f"/api/runs/{run_id}/summary").json()
    sz = s["sizing"]
    assert s["steps"] == 24 and sz["kpis"]["years"] == 3                  # one day, weighted for 3 years
    assert {x["quantity"] for x in sz["sizes"]} == {"energy", "power"} and sz["sizes"][0]["node"] == "4"
    assert sz["kpis"]["npv"] == pytest.approx(sz["kpis"]["npv_check"], rel=1e-6, abs=1e-3)
    assert sz["columns"][0] == "year" and len(sz["rows"]) == 4
    assert "size" in s["badges"]["4"]
    xl = client.get(f"/api/runs/{run_id}/export", params={"view": "sizing", "format": "xlsx"})
    assert xl.status_code == 200
    # missing OPEX is reported on the battery block
    _bess(g)["sizing"].pop("opex")
    run_id = client.post("/api/runs", json=g).json()["run_id"]
    for _ in range(100):
        st = client.get(f"/api/runs/{run_id}").json()
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "failed" and "OPEX required" in st["errors"]["4"][0]


def test_co2_cap_comes_from_the_run_settings_block():
    g = _graph("ds", days=1)
    g["nodes"].append({"id": "9", "kind": "invest", "co2_cap": 12.5})
    cfg, _ = graph_to_config(g, {"ds": "ds.csv"})
    assert cfg["emission_policy"] == {"co2": {"cap": 12.5}} and "lifetime" not in cfg
    g["nodes"].pop()
    g["settings"]["co2_cap"] = 7                    # projects saved before the cap moved off the top bar
    cfg, _ = graph_to_config(g, {"ds": "ds.csv"})
    assert cfg["emission_policy"] == {"co2": {"cap": 7.0}}


def test_a_running_local_run_can_be_stopped(client):
    """A long multi-year run (60 years of a 30-day base period, one solve each) is stopped while it runs: its worker
    process ends at once and the run reports 'cancelled'; stopping a finished run changes nothing."""
    idx = pd.date_range("2025-01-01", periods=24 * 30, freq="1h")
    csv = pd.DataFrame({"price": 50 + 30 * np.sin(np.arange(len(idx)) / 3), "load": 0.5}, index=idx)
    ds = client.post("/api/datasets", files={"file": ("d.csv", csv.rename_axis("time").to_csv().encode(), "text/csv")}).json()
    g = _graph(ds["id"], days=30)
    g["settings"].update({"run_mode": "multi_year", "sim_years": 60, "discount_rate": 5})
    g["nodes"][3]["params"]["degradation"] = None
    run_id = client.post("/api/runs", json=g).json()["run_id"]
    for _ in range(300):                                     # wait until it is solving
        st = client.get(f"/api/runs/{run_id}").json()
        if st["status"] == "solving":
            break
        time.sleep(0.05)
    assert st["status"] == "solving", st
    t0 = time.time()
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
    for _ in range(100):
        st = client.get(f"/api/runs/{run_id}").json()
        if st["status"] in ("cancelled", "done", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "cancelled" and st["message"] == "stopped by user" and time.time() - t0 < 5
    assert client.get(f"/api/runs/{run_id}/summary").status_code == 409             # no results
    assert client.post("/api/runs/nope/cancel").status_code == 404


def _sized_graph(ds_id):
    """_graph with the battery switched to variable size and a lifetime optimisation over 2 years."""
    g = _graph(ds_id)
    g["settings"].update(run_mode="optimise", years=2, sim_years=2)
    g["nodes"][3]["sizing"] = {"optimise": True, "capex_energy": 1000, "capex_power": 0, "opex": 0, "e_size_max": 2}
    g["nodes"].append({"id": "9", "kind": "invest", "discount_rate": 0, "inflation": 0})
    return g


def _finish(client, run):
    assert run["run_id"], run
    for _ in range(600):
        st = client.get(f"/api/runs/{run['run_id']}").json()
        if st["status"] in ("done", "failed"):
            return st
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_lifetime_optimisation_against_a_baseline_variant(client):
    """graph.baseline (another variant's canvas) is the reference case of the investment, run with this run's
    settings: here the site with its existing 1 MWh battery, so the new battery saves less than against the default
    reference (this model without it). A baseline with errors stops the run with them."""
    ds = client.post("/api/datasets", files={"file": ("d.csv", _csv(), "text/csv")}).json()
    plain = _finish(client, client.post("/api/runs", json=_sized_graph(ds["id"])).json())
    assert plain["status"] == "done", plain
    base = _graph(ds["id"])
    g = _sized_graph(ds["id"])
    g["baseline"] = {"name": "site today", "nodes": base["nodes"], "edges": base["edges"]}
    st = _finish(client, client.post("/api/runs", json=g).json())
    assert st["status"] == "done", st
    assert "vs 'site today'" in st["message"]
    k = client.get(f"/api/runs/{st['run_id']}/summary").json()["sizing"]["kpis"]
    k0 = client.get(f"/api/runs/{plain['run_id']}/summary").json()["sizing"]["kpis"]
    assert k["reference"] == "site today" and k0["reference"] is None
    assert k["savings_year1"] < k0["savings_year1"] and k["npv"] < k0["npv"]
    assert k["npv"] == pytest.approx(k["npv_check"], rel=1e-6)

    g["baseline"]["edges"] = base["edges"][:4]                          # the demand is not wired
    bad = client.post("/api/runs", json=g).json()
    assert bad["run_id"] is None and any(m.startswith("baseline 'site today':") for m in bad["errors"]["_global"])

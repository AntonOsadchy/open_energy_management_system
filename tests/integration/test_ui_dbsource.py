"""Integration: DB connection + DB input blocks - BESS IQ series fetched, aligned to the model grid and used like
uploaded inputs.

The database is replaced by ``helpers.fake_bess_db``, which answers the fixed queries from memory.
"""

import time

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from helpers import use_fake_bess_db  # noqa: E402

from oems.ui.dbsource import DbError, Secrets, horizon, parse_url, to_grid  # noqa: E402
from oems.ui.server import create_app  # noqa: E402

SETTINGS = {"start": "2025-01-01", "days": 2, "freq": "1h", "tz": "UTC"}
CONN = {"host": "db.example.com", "port": 5432, "database": "postgres", "user": "tester", "sslmode": "require"}


@pytest.fixture(autouse=True)
def no_real_login(tmp_path, monkeypatch):
    """Keep the developer's own .env / environment logins out of the tests."""
    for var in ("OEMS_DB_URL", "DATABASE_URL", "SUPABASE_DB_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("BESS_DB_ENV", str(tmp_path / "none.env"))


@pytest.fixture()
def client(tmp_path):
    c = TestClient(create_app(tmp_path / "ws"))
    use_fake_bess_db(c.app)
    return c


def _graph(db_node, settings=SETTINGS):
    """Bus el + grid whose import price comes from a DB input (through DB connection block 9) + 0.5 MW demand."""
    return {"settings": settings,
            "nodes": [{"id": "1", "kind": "bus", "name": "el", "carrier": "electricity", "carrier_kind": "electricity"},
                      {"id": "2", "kind": "db", **db_node},
                      {"id": "3", "kind": "component", "type": "GridConnection", "name": "dso", "params": {}},
                      {"id": "4", "kind": "component", "type": "Demand", "name": "load", "params": {"profile": 0.5}},
                      {"id": "9", "kind": "dbconn", "name": "bess_iq", **CONN}],
            "edges": [{"from": ["3", "link"], "to": ["1", "supply"]}, {"from": ["1", "withdraw"], "to": ["4", "in"]},
                      {"from": ["2", "value"], "to": ["3", "import_price"]}]}


def test_to_grid_averages_finer_and_holds_coarser_points():
    """15-min points are averaged into hourly steps; hourly points are held over 15-min steps."""
    q = pd.Series([1.0, 2.0, 3.0, 6.0, 10.0, 10.0, 10.0, 10.0],
                  index=pd.date_range("2025-01-01", periods=8, freq="15min", tz="UTC"))
    hourly, info = to_grid(q, horizon({"start": "2025-01-01", "days": 1 / 12, "freq": "1h", "tz": "UTC"}))
    assert hourly.tolist() == [3.0, 10.0] and info["source_resolution"] == "15min"
    h = pd.Series([5.0, 7.0], index=pd.date_range("2025-01-01", periods=2, freq="1h", tz="UTC"))
    quarter, info = to_grid(h, horizon({"start": "2025-01-01", "days": 2 / 24, "freq": "15min", "tz": "UTC"}))
    assert quarter.tolist() == [5.0] * 4 + [7.0] * 4 and info["held_steps"] == 6


def test_to_grid_refuses_to_hold_across_long_gaps():
    """A horizon beyond the end of the data is an error, not a silently repeated last value."""
    h = pd.Series(1.0, index=pd.date_range("2025-01-01", periods=24, freq="1h", tz="UTC"))
    with pytest.raises(DbError, match="no data around 2025-01-03"):
        to_grid(h, horizon({"start": "2025-01-01", "days": 3, "freq": "1h", "tz": "UTC"}))


def test_db_input_feeds_a_parameter(client):
    """A DB input wired to the grid's import price runs like an uploaded input; the cost matches the price data
    (hourly 50 €/MWh before 12:00 UTC on Jan 1, then hourly means of the 15-min prices: 56 / 66 €/MWh)."""
    g = _graph({"connection": "9", "source": "market", "zone": "DK_1", "series": "da_price"})
    assert client.post("/api/validate", json=g).json()["ok"]
    cfg = client.post("/api/config", json=g).json()
    ref = next(c for c in cfg["components"] if c["name"] == "dso")["import_price"]["series"]
    assert ref.startswith("db_") and ref.endswith(":value")
    assert ref.split(":")[0] not in {d["id"] for d in client.get("/api/datasets").json()}   # cache stays internal
    run = client.post("/api/runs", json=g).json()
    for _ in range(200):
        s = client.get(f"/api/runs/{run['run_id']}").json()
        if s["status"] in ("done", "failed"):
            break
        time.sleep(0.05)       # the run's worker process needs a moment to start
    assert s["status"] == "done", s
    price = np.r_[np.full(12, 50.0), np.full(12, 66.0), np.full(12, 56.0), np.full(12, 66.0)]
    total = client.get(f"/api/runs/{run['run_id']}/summary").json()["total_cost"]
    assert total == pytest.approx(0.5 * price.sum(), rel=1e-6)


def test_db_preview_tariff_and_transform(client):
    """Preview returns the aligned series' stats; the tariff source and the K → °C transform are applied."""
    tariff = {"source": "tariff", "zone": "DK_1", "connection_type": "n1_a_high", "series": "consumption"}
    r = client.post("/api/db/preview", json={"settings": SETTINGS, "conn": CONN, "spec": tariff}).json()
    assert r["steps"] == 48 and r["min"] == r["max"] == 40.0
    temp = {"source": "market", "zone": "DK_1", "series": "temperature_2m_k", "transform": "kelvin_to_celsius", "scale": 2}
    r = client.post("/api/db/preview", json={"settings": SETTINGS, "conn": CONN, "spec": temp}).json()
    assert r["mean"] == pytest.approx(10.0)


def test_solar_pv_profile_is_a_capacity_factor(client):
    """The solar source divides the modelled PV output by the system's MWp (0.8 MW / 2 MWp = 0.4) and feeds a PV
    producer's cf: a 1 MW PV then covers 0.4 of a 0.5 MW demand from 10:00 to 14:00 UTC; the rest is bought at the
    DB tariff of 40 €/MWh."""
    o = client.post("/api/db/options", json={"conn": CONN}).json()
    assert o["solar_scenarios"][0]["code"] == "SITE" and o["solar_scenarios"][0]["capacity_mwp"] == 2.0
    spec = {"source": "solar", "scenario": "SITE"}
    r = client.post("/api/db/preview", json={"settings": SETTINGS, "conn": CONN, "spec": spec}).json()
    assert (r["min"], r["max"], r["label"]) == (0.0, 0.4, "SITE PV capacity factor")
    g = _graph({"source": "tariff", "zone": "DK_1", "connection_type": "n1_a_high", "series": "consumption"})
    g["nodes"] += [{"id": "5", "kind": "db", **spec},
                   {"id": "6", "kind": "component", "type": "Producer", "name": "pv", "params": {"capacity": 1}}]
    g["edges"] += [{"from": ["6", "out"], "to": ["1", "supply"]}, {"from": ["5", "value"], "to": ["6", "cf"]}]
    run = client.post("/api/runs", json=g).json()
    for _ in range(200):
        s = client.get(f"/api/runs/{run['run_id']}").json()
        if s["status"] in ("done", "failed"):
            break
        time.sleep(0.05)       # the run's worker process needs a moment to start
    assert s["status"] == "done", s
    bought = 48 * 0.5 - 2 * 4 * 0.4                      # MWh over 2 days
    assert client.get(f"/api/runs/{run['run_id']}/summary").json()["total_cost"] == pytest.approx(40 * bought)
    g = _graph({"source": "solar", "scenario": "NOPE"})
    assert client.post("/api/validate", json=g).json()["errors"]["2"] == ["choose a solar PV scenario"]


def test_db_errors_are_attached_to_the_block(client):
    """Missing data, incomplete settings and a missing connection show up as errors on the DB input block."""
    g = _graph({"source": "market", "zone": "DE_LU", "series": "da_price"})
    assert client.post("/api/validate", json=g).json()["errors"] == {"2": [
        "no data for DE_LU da_price between 2025-01-01 and 2025-01-03"]}
    g = _graph({"source": "tariff", "zone": "DK_1", "series": "consumption"})
    assert "connection type" in client.post("/api/validate", json=g).json()["errors"]["2"][0]
    g = _graph({"source": "market", "zone": "DK_1", "series": "da_price"})
    g["nodes"] = [n for n in g["nodes"] if n["kind"] != "dbconn"]
    assert "add a DB connection block" in client.post("/api/validate", json=g).json()["errors"]["2"][0]


def test_db_input_without_connection_block_uses_env_login(tmp_path, monkeypatch):
    """With no DB connection block on the canvas, DB inputs use the login in the project's .env
    (SUPABASE_DB_URL), so a project needs no connection set-up."""
    env = tmp_path / "project.env"
    env.write_text("ENTSOE_API_KEY=x\nSUPABASE_DB_URL=postgresql://postgres.ref:p%2Fw@pool.example.com:5432/postgres\n")
    monkeypatch.setenv("BESS_DB_ENV", str(env))
    c = TestClient(create_app(tmp_path / "ws"))
    seen = []
    use_fake_bess_db(c.app)
    factory = c.app.state.db_factory
    c.app.state.db_factory = lambda conn: seen.append(conn) or factory(conn)
    d = c.get("/api/db/defaults").json()
    assert (d["host"], d["user"], d["has_password"]) == ("pool.example.com", "postgres.ref", True)
    assert "p/w" not in str(d) and "p%2Fw" not in str(d)
    g = _graph({"source": "market", "zone": "DK_1", "series": "da_price"})
    g["nodes"] = [n for n in g["nodes"] if n["kind"] != "dbconn"]
    assert c.post("/api/validate", json=g).json()["ok"]
    assert seen[0]["host"] == "pool.example.com" and seen[0]["user"] == "postgres.ref"


def test_connect_lists_databases_and_options(client):
    """Connecting a DB connection block lists the server's databases; the DB input's dropdowns come from the
    database catalogue. The password is stored by login, never returned."""
    r = client.post("/api/db/connect", json={"conn": CONN, "password": "secret"}).json()
    assert r["connected"] and r["databases"] == ["postgres", "staging"] and r["has_password"]
    assert "secret" not in str(r)
    o = client.post("/api/db/options", json={"conn": CONN}).json()
    assert [z["code"] for z in o["zones"]] == ["DK_1", "DE_LU"]
    assert {"da_price", "temperature_2m_k"} <= {s["code"] for s in o["series"]}


def test_passwords_are_stored_privately_per_login(tmp_path, monkeypatch):
    """Passwords are kept per user@host:port, owner-readable only; the BESS IQ .env login is used as a default."""
    monkeypatch.setenv("OEMS_DB_URL", "postgresql://env_user:env%40pw@env-host:6543/postgres?sslmode=require")
    sec = Secrets(tmp_path)
    d = sec.defaults()
    assert (d["host"], d["port"], d["user"], d["origin"]) == ("env-host", 6543, "env_user", "$OEMS_DB_URL")
    assert sec.password(d) == "env@pw"                      # same login as the environment URL
    assert sec.password(CONN) is None
    sec.set_password(CONN, "s3cret")
    assert sec.password(CONN) == "s3cret" and (sec.path.stat().st_mode & 0o777) == 0o600
    assert "s3cret" not in str(Secrets(tmp_path).defaults())
    assert parse_url("postgres://u:p@h/db")[0]["port"] == 5432
    conn, pw = parse_url("postgresql://u:a/b#c?d@e:f@host.x:6543/postgres?sslmode=require")   # urllib misreads this
    assert (conn["host"], conn["port"], pw) == ("host.x", 6543, "a/b#c?d@e:f")


def _wait(client, run_id):
    for _ in range(400):
        s = client.get(f"/api/runs/{run_id}").json()
        if s["status"] in ("done", "failed"):
            return s
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_degradation_profile_feeds_a_multi_year_run(client):
    """A degradation DB input (profile 'short' of the fake DB) wired to a battery's SoH curves, with a 4-year
    simulation of the 2-day base period: the summary has one Lifetime row per year with falling capacity, each year
    can be inspected, the Excel export has a Lifetime sheet, the results zip a lifetime.csv and the report a Lifetime
    section; a saved simulation keeps every year (summary and per-step table)."""
    import io
    import zipfile

    o = client.post("/api/db/options", json={"conn": CONN}).json()
    assert o["degradation_profiles"] == [{"code": "short", "name": "Short test profile", "rates": [1.0, 2.0],
                                          "years": 3}]
    spec = {"source": "degradation", "profile": "short"}
    r = client.post("/api/db/preview", json={"settings": SETTINGS, "conn": CONN, "spec": spec}).json()
    assert r["curves"] == {"1": [0.95, 0.9, 0.8], "2": [0.9, 0.8]} and r["eol"] == pytest.approx(0.8)

    g = _graph({"connection": "9", "source": "market", "zone": "DK_1", "series": "da_price"},
               {**SETTINGS, "sim_years": 4})
    g["nodes"] += [{"id": "5", "kind": "db", "connection": "9", **spec},
                   {"id": "6", "kind": "component", "type": "Storage", "name": "bess",
                    "params": {"e_max": 1, "p_ch_max": 1, "p_dis_max": 1}}]
    g["edges"] += [{"from": ["1", "withdraw"], "to": ["6", "link"]}, {"from": ["5", "value"], "to": ["6", "degradation"]}]
    cfg = client.post("/api/config", json=g).json()
    store = next(c for c in cfg["components"] if c["name"] == "bess")
    assert store["degradation"]["curves"]["2"] == [0.9, 0.8] and cfg["lifetime"] == {"years": 4, "discount_rate": 0, "inflation": 0}

    run_id = client.post("/api/runs", json=g).json()["run_id"]
    st = _wait(client, run_id)
    assert st["status"] == "done" and (st["year"], st["years"]) == (4, 4)          # progress: year being solved
    s = client.get(f"/api/runs/{run_id}/summary").json()
    life = s["lifetime"]
    rows = [dict(zip(life["columns"], r, strict=True)) for r in life["rows"]]
    assert [r["year"] for r in rows] == [1, 2, 3, 4] and life["years"] == 4
    caps = [r["capacity_start_mwh"] for r in rows]
    assert caps[0] == 1 and all(a >= b for a, b in zip(caps, caps[1:], strict=False)) and caps[1] < 1
    assert s["badges"]["6"]["soh"] == 100 and s["year"] == 1
    s2 = client.get(f"/api/runs/{run_id}/summary?year=2").json()
    assert s2["year"] == 2 and s2["badges"]["6"]["soh"] == pytest.approx(100 * rows[1]["soh_start"])
    assert client.get(f"/api/runs/{run_id}/summary?year=9").status_code == 404

    t2 = client.get(f"/api/runs/{run_id}/timeseries?year=2&limit=5").json()
    assert t2["rows"] == 48
    every = pd.read_csv(io.StringIO(client.get(f"/api/runs/{run_id}/timeseries?year=all&format=csv").text))
    assert len(every) == 4 * 48 and list(every["year"].unique()) == [1, 2, 3, 4]
    xl = pd.read_excel(io.BytesIO(client.get(f"/api/runs/{run_id}/timeseries?format=xlsx").content), sheet_name=None)
    assert list(xl["Lifetime"]["year"]) == [1, 2, 3, 4]
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/runs/{run_id}/download").content)) as z:
        assert "lifetime.csv" in z.namelist()
    exp = f"/api/runs/{run_id}/export"                                   # ⤓ Download of the selected results tab
    csv = client.get(f"{exp}?view=lifetime&format=csv")
    assert "oems_lifetime_" in csv.headers["content-disposition"]
    assert pd.read_csv(io.StringIO(csv.text))["capacity_start_mwh"].tolist() == pytest.approx(caps)
    lx = pd.read_excel(io.BytesIO(client.get(f"{exp}?view=lifetime&format=xlsx").content), sheet_name=None)
    assert list(lx["Lifetime"]["year"]) == [1, 2, 3, 4] and "years" in list(lx["Summary"]["item"])
    summ = pd.read_csv(io.StringIO(client.get(f"{exp}?view=summary&year=2").text))
    assert summ.set_index("item").loc["Profit", "value"] == pytest.approx(s2["profit"]["total"])
    assert set(pd.read_csv(io.StringIO(client.get(f"{exp}?view=costs").text)).columns) == {"component", "category", "EUR"}
    assert client.get(f"{exp}?view=lifetime&format=pdf").status_code == 400
    report = client.post(f"/api/runs/{run_id}/report", json={"name": "life", "year": 2}).text
    assert "Lifetime" in report and "(year 2 in detail)" in report
    meta = client.post("/api/simulations", json={"run_id": run_id, "name": "4 years"}).json()   # saved: every year
    base = f"/api/simulations/{meta['id']}"
    saved = client.get(f"{base}/summary").json()["lifetime"]
    assert saved["rows"] == life["rows"] and saved["saved_years"]
    assert client.get(f"{base}/summary?year=3").json()["badges"]["6"]["soh"] == pytest.approx(100 * rows[2]["soh_start"])
    t3 = client.get(f"{base}/timeseries?year=3&limit=48").json()
    live3 = client.get(f"/api/runs/{run_id}/timeseries?year=3&limit=48").json()
    assert t3["rows"] == 48 and t3["columns"] == live3["columns"]
    assert np.allclose(np.array(t3["data"], float), np.array(live3["data"], float), atol=1e-9)
    assert len(pd.read_csv(io.StringIO(client.get(f"{base}/timeseries?year=all&format=csv").text))) == 4 * 48
    assert client.get(f"{base}/summary?year=9").status_code == 404
    saved_csv = client.get(f"{base}/export?view=lifetime")
    assert "oems_lifetime_4_years.csv" in saved_csv.headers["content-disposition"]
    assert len(pd.read_csv(io.StringIO(saved_csv.text))) == len(rows)


def test_degradation_curves_only_feed_soh_curves(client):
    g = _graph({"connection": "9", "source": "degradation", "profile": "short"})
    errs = client.post("/api/validate", json=g).json()["errors"]
    assert "can only feed a storage's SoH curves" in errs["2"][0]

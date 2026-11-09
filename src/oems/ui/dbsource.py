"""Time series from a BESS IQ database (Supabase / Postgres) as UI inputs.

Two block kinds on the canvas:

* **DB connection** (``kind: "dbconn"``): host, port, user, SSL mode and the database chosen from the server's list.
  The password is never part of the project; it is kept per ``user@host:port`` in ``<workspace>/db.json`` (readable
  by the owner only) or taken from the BESS IQ ``.env`` file when that names the same login.
* **DB input** (``kind: "db"``): one series pulled through a connection, e.g.
  ``{"connection": "<conn node id>", "source": "market", "zone": "DK_1", "series": "da_price"}`` or
  ``{"source": "tariff", "zone": "DK_1", "connection_type": "n1_a_high", "series": "consumption"}`` or
  ``{"source": "solar", "scenario": "DK1"}``: the modelled PV output of a ``solar_pv_scenarios`` system
  (``gen_solar_modeled``) divided by its ``capacity_mwp``, i.e. a 0…1 capacity factor for a producer's ``cf``.
  ``{"source": "degradation", "profile": "standard"}`` is not a time series but a battery's SoH curves by cycling
  rate (``bess_degradation_curves``), for a storage's ``degradation`` field in multi-year runs.

Before a graph is validated or run, :func:`materialize` fetches every DB input for the project horizon, lines it up
with the model grid and caches it as an ordinary dataset; the block then behaves exactly like an uploaded input.

Alignment (:func:`to_grid`): each model step takes the mean of the database points inside it; steps without a point
hold the last earlier value (hourly data on a 15-min grid, daily commodity prices). Holding longer than the series'
maximum hold is an error, so a horizon beyond the end of the data is never filled silently. The database labels
day-ahead prices ``resolution_minutes = 60`` also after the move to 15-min products, so that column is not used.

Connections are read-only (session default plus fixed, parameterised SELECTs).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote

import numpy as np
import pandas as pd

SOURCES = {"market": "Market data (prices, generation, load, weather, commodities)", "tariff": "Grid tariff",
           "solar": "Solar PV profile per MWp (capacity factor)",
           "degradation": "Battery degradation (SoH curves by cycling rate)"}
TARIFF_COLUMNS = {"consumption": "consumption_tariff_eur_mwh", "feedin": "feedin_tariff_eur_mwh"}
TRANSFORMS = {"none": "as stored", "kelvin_to_celsius": "K → °C", "normalize": "÷ maximum over the horizon (0…1)"}
SSL_MODES = ["require", "prefer", "disable"]
MAX_HOLD = {"commodity": pd.Timedelta(days=7)}          # by series category
DEFAULT_HOLD = pd.Timedelta(hours=24)
LOOKBACK = pd.Timedelta(days=14)                        # fetched before the start, so a held value exists
DEFAULT_ENV = Path.home() / "Documents" / "Projects" / "linkedin-carousels" / ".env"
CONN_KEYS = ("host", "port", "database", "user", "sslmode")
ENV_VARS = ("OEMS_DB_URL", "DATABASE_URL", "SUPABASE_DB_URL")


class DbError(Exception):
    """A database problem the user can fix (no connection, unknown series, no data for the horizon)."""


# ------------------------------------------------------------------ connection details and passwords
def conn_of(node: dict) -> dict:
    """The connection fields of a DB connection block (no password)."""
    c = {k: node.get(k) for k in CONN_KEYS}
    c["port"] = int(c["port"] or 5432)
    c["sslmode"] = c["sslmode"] or "require"
    c["database"] = c["database"] or "postgres"
    return c


def login_key(conn: dict) -> str:
    return f"{conn.get('user')}@{conn.get('host')}:{int(conn.get('port') or 5432)}"


def parse_url(url: str) -> tuple[dict, str | None]:
    """``(connection fields, password)`` of a ``postgresql://user:password@host:port/db?sslmode=…`` URL."""
    # Split by hand at the *last* '@': passwords often contain characters that urllib misreads (/, #, ?, @).
    # Never put the URL into an error message, it carries the password.
    m = re.match(r"^postgres(?:ql)?(?:\+\w+)?://(?:(?P<user>[^:@]*)(?::(?P<pw>.*))?@)?(?P<host>[^:/?@]*)"
                 r"(?::(?P<port>\d+))?(?:/(?P<db>[^?]*))?(?:\?(?P<q>.*))?$", url.strip(), re.S)
    if not m:
        return {"host": "", "port": 5432, "database": "postgres", "user": "", "sslmode": "require"}, None
    q = dict(p.split("=", 1) for p in (m["q"] or "").split("&") if "=" in p)
    conn = {"host": m["host"], "port": int(m["port"] or 5432), "database": unquote(m["db"] or "") or "postgres",
            "user": unquote(m["user"] or ""), "sslmode": q.get("sslmode", "require")}
    return conn, (unquote(m["pw"]) if m["pw"] else None)


class Secrets:
    """Passwords per ``user@host:port``: stored in the workspace, else from the BESS IQ ``.env`` file / environment."""

    def __init__(self, workspace: Path):
        self.path = Path(workspace) / "db.json"
        # .env files searched for a login: $BESS_DB_ENV, else the project's own (next to the workspace / in the
        # working directory), else the BESS IQ project's
        named = os.environ.get("BESS_DB_ENV")
        self.env_files = ([Path(os.path.expanduser(named))] if named else
                          list(dict.fromkeys([Path(workspace).resolve().parent / ".env", Path.cwd() / ".env",
                                              DEFAULT_ENV])))

    def _stored(self) -> dict:
        return json.loads(self.path.read_text()).get("passwords", {}) if self.path.exists() else {}

    def env_url(self) -> tuple[str | None, str]:
        """The default login: ``$OEMS_DB_URL`` / ``$DATABASE_URL`` / ``$SUPABASE_DB_URL``, else the first ``.env``
        file (see ``env_files``) that defines one of them."""
        for var in ENV_VARS:
            if os.environ.get(var):
                return os.environ[var], f"${var}"
        for env in self.env_files:
            if env.exists():
                values = {}
                for line in env.read_text().splitlines():
                    k, _, v = line.strip().partition("=")
                    values[k.strip()] = v.strip().strip('"').strip("'")
                for var in ENV_VARS:
                    if values.get(var):
                        return values[var], f"{env} ({var})"
        return None, ""

    def defaults(self) -> dict:
        """Prefill for a new DB connection block (from the environment / .env file), without the password."""
        url, origin = self.env_url()
        if not url:
            return {"host": "", "port": 5432, "database": "postgres", "user": "", "sslmode": "require", "origin": ""}
        conn, _ = parse_url(url)
        return {**conn, "origin": origin}

    def password(self, conn: dict) -> str | None:
        key = login_key(conn)
        if key in self._stored():
            return self._stored()[key]
        url, _ = self.env_url()
        if url:
            env_conn, pw = parse_url(url)
            if login_key(env_conn) == key:
                return pw
        return None

    def has_password(self, conn: dict) -> bool:
        return self.password(conn) is not None

    def set_password(self, conn: dict, password: str | None) -> None:
        """Store (or with an empty password forget) the password of a login; the file is owner-readable only."""
        stored = self._stored()
        if password:
            stored[login_key(conn)] = password
        else:
            stored.pop(login_key(conn), None)
        self.path.write_text(json.dumps({"passwords": stored}))
        self.path.chmod(0o600)


def mask(conn: dict) -> str:
    return f"{conn.get('user')}@{conn.get('host')}:{conn.get('port')}/{conn.get('database')}"


class BessDb:
    """Read-only access, through one connection, to the BESS IQ tables used by DB input blocks."""

    def __init__(self, conn: dict, password: str | None):
        self.conn, self.password = conn_of(conn), password
        self._options: dict | None = None

    def connect(self, database: str | None = None):
        try:
            import psycopg
        except ImportError as e:
            raise DbError("the database driver is missing: pip install 'open-ems[db]'") from e
        c = self.conn
        if not c["host"] or not c["user"]:
            raise DbError("the DB connection needs a host and a user")
        if not self.password:
            raise DbError(f"no password stored for {login_key(c)}: enter it in the DB connection block")
        try:
            conn = psycopg.connect(host=c["host"], port=c["port"], dbname=database or c["database"], user=c["user"],
                                   password=self.password, sslmode=c["sslmode"], autocommit=True, connect_timeout=10)
        except psycopg.Error as e:
            raise DbError(f"cannot connect to {mask(c)}: {str(e).strip().splitlines()[-1]}") from e
        conn.execute("set session characteristics as transaction read only")
        conn.execute("set statement_timeout = '60s'")
        return conn

    def _rows(self, sql: str, params=(), database: str | None = None) -> list[tuple]:
        with self.connect(database) as conn:
            return conn.execute(sql, params).fetchall()

    # ---------------------------------------------------------------- metadata
    def databases(self) -> list[str]:
        """Databases on the server the login may connect to (for the connection block's dropdown)."""
        return [r[0] for r in self._rows("select datname from pg_database where not datistemplate and datallowconn "
                                         "and has_database_privilege(datname, 'connect') order by 1")]

    def options(self) -> dict:
        """Zones, series and tariff connection types for the DB input's dropdowns."""
        if self._options is None:
            try:
                zones = self._rows("select zone_code, display_name, timezone from bidding_zones where is_active "
                                   "order by 1")
                series = self._rows("select series_type_code, category, default_unit, description from series_types "
                                    "order by category, series_type_code")
                cts = self._rows("select connection_type_code, display_name from tariff_dso_connection_types "
                                 "order by sort_order")
                solar = self._solar_scenarios()
                degradation = self._degradation_profiles()
            except DbError:
                raise
            except Exception as e:  # tables missing: not a BESS IQ database
                raise DbError(f"database {self.conn['database']!r} has no BESS IQ tables "
                              f"({str(e).strip().splitlines()[0]})") from e
            self._options = {
                "sources": SOURCES, "transforms": TRANSFORMS,
                "zones": [{"code": z, "name": n, "tz": tz} for z, n, tz in zones],
                "series": [{"code": c, "category": cat, "unit": u, "description": d} for c, cat, u, d in series],
                "connection_types": [{"code": c, "name": n} for c, n in cts],
                "solar_scenarios": solar, "degradation_profiles": degradation}
        return self._options

    def _solar_scenarios(self) -> list[dict]:
        """Modelled PV systems (an older database without the table simply has none)."""
        try:
            rows = self._rows("select scenario_code, zone_code, display_name, capacity_mwp, slope_deg, azimuth_deg, "
                              "tracking, bifacial, lat, lon, description from solar_pv_scenarios order by 1")
        except DbError:
            raise
        except Exception:
            return []
        keys = ("code", "zone", "name", "capacity_mwp", "slope_deg", "azimuth_deg", "tracking", "bifacial", "lat",
                "lon", "description")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def _degradation_profiles(self) -> list[dict]:
        """Battery SoH curve sets (an older database without the tables has none)."""
        try:
            rows = self._rows("select c.profile_code, coalesce(p.display_name, c.profile_code), "
                              "array_agg(distinct c.cycles_per_day order by c.cycles_per_day), max(c.year) "
                              "from bess_degradation_curves c "
                              "left join bess_degradation_profiles p using (profile_code) group by 1, 2 order by 1")
        except DbError:
            raise
        except Exception:
            return []
        return [{"code": c, "name": n, "rates": [float(r) for r in rates], "years": int(y)}
                for c, n, rates, y in rows]

    def curves(self, profile: str) -> dict[float, list[float]]:
        """SoH curves of a degradation profile: ``{cycles_per_day: [SoH after year 1, 2, ...]}``."""
        if not profile:
            raise DbError("choose a degradation profile")
        rows = self._rows("select cycles_per_day, year, soh from bess_degradation_curves where profile_code = %s "
                          "order by 1, 2", (profile,))
        if not rows:
            raise DbError(f"no degradation curves for profile {profile!r}")
        out: dict[float, list[float]] = {}
        for rate, year, soh in rows:
            vals = out.setdefault(float(rate), [])
            if int(year) != len(vals) + 1:
                raise DbError(f"degradation profile {profile!r}: years of the {float(rate):g}/day curve must run "
                              "1, 2, 3, ... without gaps")
            vals.append(float(soh))
        return out

    def _category(self, code: str) -> str | None:
        return next((s["category"] for s in self.options()["series"] if s["code"] == code), None)

    # ---------------------------------------------------------------- data
    def fetch(self, spec: dict, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
        """Raw points (UTC index) of one DB input's series in ``[start - LOOKBACK, end)``."""
        lo, hi = (start - LOOKBACK).to_pydatetime(), end.to_pydatetime()
        zone = spec.get("zone")
        if not zone and spec.get("source") != "solar":
            raise DbError("choose a zone")
        if spec.get("source") == "solar":
            sc = next((x for x in self.options()["solar_scenarios"] if x["code"] == spec.get("scenario")), None)
            if sc is None:
                raise DbError("choose a solar PV scenario")
            if not sc["capacity_mwp"] or sc["capacity_mwp"] <= 0:
                raise DbError(f"solar scenario {sc['code']!r} has no capacity_mwp")
            rows = self._rows("select ts, value / %s from market_timeseries where zone_code = %s "
                              "and series_type_code = 'gen_solar_modeled' and source = %s and ts >= %s and ts < %s "
                              "order by ts", (float(sc["capacity_mwp"]), sc["zone"], sc["code"], lo, hi))
        elif spec.get("source", "market") == "tariff":
            col = TARIFF_COLUMNS.get(spec.get("series") or "consumption")
            if col is None or not spec.get("connection_type"):
                raise DbError("choose a connection type and consumption or feed-in")
            rows = self._rows(f"select ts, {col} from tariffs where zone_code = %s and connection_type_code = %s "
                              "and ts >= %s and ts < %s order by ts", (zone, spec["connection_type"], lo, hi))
        else:
            if not spec.get("series"):
                raise DbError("choose a series")
            rows = self._rows("select ts, value from market_timeseries where zone_code = %s and series_type_code = %s "
                              "and ts >= %s and ts < %s order by ts", (zone, spec["series"], lo, hi))
        if not rows:
            raise DbError(f"no data for {describe(spec)} between {start:%Y-%m-%d} and {end:%Y-%m-%d}")
        s = pd.Series([float(v) if v is not None else np.nan for _, v in rows],
                      index=pd.DatetimeIndex([t for t, _ in rows]).tz_convert("UTC"), dtype=float)
        return s.dropna().groupby(level=0).mean()   # a series may be stored by several sources

    def max_hold(self, spec: dict) -> pd.Timedelta:
        if spec.get("source") == "tariff":
            return DEFAULT_HOLD
        return MAX_HOLD.get(self._category(spec.get("series")), DEFAULT_HOLD)


def describe(spec: dict) -> str:
    if spec.get("source") == "degradation":
        return f"{spec.get('profile')} SoH curves"
    if spec.get("source") == "solar":
        return f"{spec.get('scenario')} PV capacity factor"
    if spec.get("source") == "tariff":
        return f"{spec.get('zone')} {spec.get('connection_type')} {spec.get('series') or 'consumption'} tariff"
    return f"{spec.get('zone')} {spec.get('series')}"


# ------------------------------------------------------------------ alignment
def horizon(settings: dict) -> pd.DatetimeIndex:
    """The model time index of the UI settings (same rule as ``graph_to_config``)."""
    from .graph import base_period

    freq = settings.get("freq", "1h")
    start, days = base_period(settings)
    periods = int(days * {"15min": 96, "30min": 48, "1h": 24}[freq])
    return pd.date_range(start, periods=periods, freq=freq, tz=settings.get("tz") or None)


def to_grid(raw: pd.Series, index: pd.DatetimeIndex, max_hold: pd.Timedelta = DEFAULT_HOLD) -> tuple[pd.Series, dict]:
    """Average the points of ``raw`` (UTC, period-start stamps) into the steps of ``index``; hold the last value over
    steps without points. Returns ``(series on index, info)``; raises DbError on gaps longer than ``max_hold``."""
    utc = index.tz_convert("UTC") if index.tz is not None else index.tz_localize("UTC")
    step = utc[1] - utc[0] if len(utc) > 1 else pd.Timedelta(index.freq or "1h")
    edges = utc.append(pd.DatetimeIndex([utc[-1] + step]))
    pos = np.searchsorted(edges.asi8, raw.index.asi8, side="right") - 1
    inside = (pos >= 0) & (pos < len(utc))
    sums = np.bincount(pos[inside], weights=raw.to_numpy()[inside], minlength=len(utc))
    counts = np.bincount(pos[inside], minlength=len(utc))
    values = np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)

    before = raw[raw.index < utc[0]]
    last_t, last_v = (before.index[-1], float(before.iloc[-1])) if len(before) else (None, np.nan)
    held = 0
    for i, t in enumerate(utc):
        if counts[i] > 0:
            last_t, last_v = t, values[i]
            continue
        if last_t is None or t - last_t > max_hold:
            loc = raw.index.tz_convert(index.tz) if index.tz is not None else raw.index
            have = f"{loc[0]:%Y-%m-%d %H:%M} … {loc[-1]:%Y-%m-%d %H:%M}" if len(raw) else "nothing"
            raise DbError(f"no data around {index[i]:%Y-%m-%d %H:%M} (database has {have}); "
                          "move the start date or shorten the horizon")
        values[i] = last_v
        held += 1
    src_step = raw.index.to_series().diff().median() if len(raw) > 1 else None
    info = {"points": int(inside.sum()), "held_steps": held,
            "source_resolution": _fmt_step(src_step), "first": str(raw.index[0]), "last": str(raw.index[-1])}
    return pd.Series(values, index=index), info


def _fmt_step(td) -> str | None:
    if td is None or pd.isna(td):
        return None
    minutes = int(td.total_seconds() // 60)
    return f"{minutes // 60}h" if minutes % 60 == 0 and minutes >= 60 else f"{minutes}min"


def transform(s: pd.Series, spec: dict) -> pd.Series:
    kind = spec.get("transform") or "none"
    if kind == "kelvin_to_celsius":
        s = s - 273.15
    elif kind == "normalize":
        peak = s.max()
        if not peak > 0:
            raise DbError("cannot normalise a series whose maximum is not positive")
        s = s / peak
    elif kind != "none":
        raise DbError(f"unknown transform {kind!r}")
    scale = spec.get("scale")
    return s * float(scale) if scale not in (None, "") else s


# ------------------------------------------------------------------ graph integration
SPEC_KEYS = ("source", "zone", "series", "connection_type", "scenario", "transform", "scale", "profile")


def curves_for(spec: dict, conn: dict, db_for) -> dict:
    """A degradation DB input's SoH curves: ``{"curves": {rate: [...]}, "label": ..., "eol": ..., "rates": [...]}``
    (validated, so a bad profile is reported on its block)."""
    from ..degradation import DegradationCurves
    from ..exceptions import ValidationError

    curves = db_for(conn).curves(spec.get("profile"))
    try:
        dc = DegradationCurves(curves, spec.get("profile") or "")
    except ValidationError as e:
        raise DbError("; ".join(e.errors)) from e
    return {"curves": {f"{r:g}": v for r, v in curves.items()}, "label": describe(spec), "eol": dc.eol,
            "rates": dc.rates, "years": max(len(v) for v in curves.values()), "warnings": dc.warnings}


def dataset_for(spec: dict, conn: dict, settings: dict, store, db_for, refresh: bool = False) -> tuple[str, dict]:
    """Fetch one DB input's series through ``db_for(conn)`` for the project horizon into the dataset store (cached
    per connection + spec + horizon). Returns ``(dataset id, info)``."""
    index = horizon(settings)
    key = json.dumps({**{k: spec.get(k) for k in SPEC_KEYS}, "conn": conn_of(conn), "start": str(index[0]),
                      "n": len(index), "freq": settings.get("freq", "1h"), "tz": settings.get("tz")},
                     sort_keys=True, default=str)
    ds_id = "db_" + hashlib.sha1(key.encode()).hexdigest()[:10]
    if ds_id in store.index and not refresh:
        return ds_id, store.index[ds_id].get("db", {})
    db = db_for(conn)
    end = index[-1] + (index[1] - index[0] if len(index) > 1 else pd.Timedelta(settings.get("freq", "1h")))
    utc = (lambda t: t.tz_convert("UTC")) if index.tz is not None else (lambda t: t.tz_localize("UTC"))
    raw = db.fetch(spec, utc(index[0]), utc(end))
    series, info = to_grid(raw, index, db.max_hold(spec))
    series = transform(series, spec)
    info = {**info, "spec": {k: spec.get(k) for k in SPEC_KEYS}, "label": describe(spec)}
    store.add_frame(ds_id, describe(spec), series.to_frame("value"), tz=settings.get("tz"), origin="db", db=info)
    return ds_id, info


def connection_for(node: dict, nodes: list[dict], default: dict | None = None) -> dict:
    """The connection a DB input uses: the DB connection block it names, else the only one on the canvas, else the
    default login (from ``.env`` / the environment) when the canvas has no connection block."""
    conns = {str(n["id"]): n for n in nodes if n.get("kind") == "dbconn"}
    chosen = conns.get(str(node.get("connection") or ""))
    if chosen is None and len(conns) == 1:
        chosen = next(iter(conns.values()))
    if chosen is None and not conns and default:
        chosen = default
    if chosen is None:
        raise DbError("choose a DB connection" if conns else
                      "no database login: add a DB connection block, or put SUPABASE_DB_URL in the project's .env")
    return chosen


def materialize(graph: dict, store, db_for, default: dict | None = None) -> tuple[dict, dict[str, list[str]]]:
    """Replace every DB input node by a data node on its cached dataset (a degradation input by a ``curves`` node
    holding its SoH curves) and drop the connection blocks. Returns ``(graph, errors by node id)``."""
    all_nodes = graph.get("nodes", [])
    nodes, errors = [], {}
    for n in all_nodes:
        if n.get("kind") == "dbconn":
            continue
        if n.get("kind") != "db":
            nodes.append(n)
            continue
        try:
            conn = connection_for(n, all_nodes, default)
            if n.get("source") == "degradation":
                c = curves_for(n, conn, db_for)
                nodes.append({"id": n["id"], "kind": "curves", "curves": c["curves"], "label": c["label"]})
                continue
            ds_id, _ = dataset_for(n, conn, graph.get("settings", {}), store, db_for)
            nodes.append({"id": n["id"], "kind": "data", "dataset": ds_id})
        except DbError as e:
            errors[str(n["id"])] = [str(e)]
    return {**graph, "nodes": nodes}, errors

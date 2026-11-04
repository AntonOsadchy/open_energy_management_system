"""Serialisation of an EnergySystem to/from a plain dict (YAML/JSON).

Time series are never written inline. A series value is a reference ``{"series": "dataset:column"}``
into a CSV listed under ``datasets``. :func:`to_config` writes every pandas Series it meets into a
dataset folder and replaces it by such a reference, so ``from_config(to_config(es))`` round-trips.

Format::

    name: site
    time: {start: 2025-01-01, periods: 8760, freq: 1h, tz: Europe/Copenhagen}
    datasets: {site: data/site_hourly.csv, prices: data/prices_15min.csv}
    carriers: [{name: electricity, kind: electricity, emission_factor: {co2: 0.0}}]
    buses: [{name: el, carrier: electricity}]
    components:
      - {type: GridConnection, name: dso, bus: el, import_price: {series: "prices:da_price"},
         tariffs: [{type: PeakCharge, rate: 4500, period: M}]}
      - {type: Converter, name: hp, inputs: {el: 1}, outputs: {heat: {series: "site:cop90"}}}
    emission_policy: {co2: {price: 85}}
    solver: {time_limit: 600, tie_break: 0.01}
"""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from .carrier import Bus, Carrier
from .components.base import REGISTRY
from .components.tariffs import TARIFF_REGISTRY
from .costs import FlowCost
from .exceptions import ValidationError
from .schema import SPEC_KEY, Kind
from .system import EnergySystem
from .timeseries import TimeGrid, load_csv


# ------------------------------------------------------------------ loading
class SeriesStore:
    """Resolves ``dataset:column`` references, loading CSVs lazily."""

    def __init__(self, datasets: dict | None = None, base_dir: Path | None = None, tz=None, frames=None):
        self.paths = dict(datasets or {})
        self.base = Path(base_dir or ".")
        self.tz = tz
        self.frames: dict[str, pd.DataFrame] = dict(frames or {})

    def get(self, ref: str) -> pd.Series:
        if ":" not in ref:
            raise ValidationError(f"series reference {ref!r} must be 'dataset:column'")
        ds, col = ref.split(":", 1)
        if ds not in self.frames:
            if ds not in self.paths:
                raise ValidationError(f"unknown dataset {ds!r} in reference {ref!r}")
            path = Path(self.paths[ds])
            self.frames[ds] = load_csv(path if path.is_absolute() else self.base / path, tz=self.tz)
        df = self.frames[ds]
        if col not in df.columns:
            raise ValidationError(f"dataset {ds!r} has no column {col!r}")
        return df[col].dropna()  # columns of mixed-resolution datasets have gaps


def _load_value(v, store: SeriesStore):
    if isinstance(v, dict) and set(v) == {"series"}:
        return store.get(v["series"])
    if isinstance(v, str) and v in ("inf", "+inf"):
        return float("inf")
    return v


def _load_fields(cls, data: dict, store: SeriesStore, buses: dict) -> dict:
    out = {}
    fields = {f.name: f for f in dataclasses.fields(cls)}
    for key, val in data.items():
        if key == "type":
            continue
        f = fields.get(key)
        if f is None:
            raise ValidationError(f"{cls.__name__}: unknown parameter {key!r}")
        spec = f.metadata.get(SPEC_KEY)
        kind = spec.kind if spec else None
        if kind == Kind.BUS_MAP:
            out[key] = {b: _load_value(c, store) for b, c in (val or {}).items()}
        elif kind == Kind.TARIFFS:
            out[key] = [_make_tariff(t, store, buses) for t in val or []]
        elif kind == Kind.PORT_MAP:
            out[key] = _load_port_map(key, val or {}, store)
        else:
            out[key] = _load_value(val, store)
    return out


def _load_port_map(field: str, val: dict, store):
    if field == "flow_costs":
        return {p: FlowCost(_load_value(v["rate"], store), v.get("category", "OPERATION")) if isinstance(v, dict)
                and "rate" in v else _load_value(v, store) for p, v in val.items()}
    if field == "emission_factors":
        return {p: {g: _load_value(x, store) for g, x in gases.items()} for p, gases in val.items()}
    if field == "bounds":
        return {p: tuple(_load_value(x, store) for x in pair) for p, pair in val.items()}
    return val


def _make_tariff(d: dict, store, buses):
    cls = TARIFF_REGISTRY.get(d.get("type"))
    if cls is None:
        raise ValidationError(f"unknown tariff type {d.get('type')!r}; known: {sorted(TARIFF_REGISTRY)}")
    return cls(**_load_fields(cls, d, store, buses))


def grid_from_config(t: dict) -> TimeGrid:
    return TimeGrid(start=t.get("start"), end=t.get("end"), periods=t.get("periods"), freq=t.get("freq", "1h"),
                    tz=t.get("tz"))


def from_config(cfg: dict | str | Path, base_dir: str | Path | None = None,
                frames: dict | None = None) -> tuple[EnergySystem, dict]:
    """Build an EnergySystem from a config dict or YAML/JSON file.

    Returns ``(system, solve_options)``. ``frames`` may supply already loaded datasets
    ({name: DataFrame}) instead of CSV paths.
    """
    if not isinstance(cfg, dict):
        path = Path(cfg)
        base_dir = base_dir or path.parent
        cfg = yaml.safe_load(path.read_text())
    time = cfg.get("time") or {}
    grid = grid_from_config(time) if time else None
    store = SeriesStore(cfg.get("datasets"), Path(base_dir or "."), tz=time.get("tz"), frames=frames)
    es = EnergySystem(grid, name=cfg.get("name", "oems"))
    carriers = {}
    for c in cfg.get("carriers", []):
        carriers[c["name"]] = Carrier(c["name"], c.get("kind", "other"), dict(c.get("emission_factor") or {}),
                                      c.get("description", ""))
    for b in cfg.get("buses", []):
        cname = b["carrier"]
        if cname not in carriers:
            raise ValidationError(f"bus {b['name']!r}: unknown carrier {cname!r}")
        es.add_bus(Bus(b["name"], carriers[cname], bool(b.get("allow_slack", False)),
                       float(b.get("slack_cost", 10_000.0))))
    errors = []
    for d in cfg.get("components", []):
        cls = REGISTRY.get(d.get("type"))
        if cls is None:
            errors.append(f"unknown component type {d.get('type')!r}; known: {sorted(REGISTRY)}")
            continue
        try:
            es.add(cls(**_load_fields(cls, d, store, es.buses)))
        except TypeError as e:
            errors.append(f"{d.get('name', '?')}: {e}")
        except ValidationError as e:
            errors.extend(e.errors)
    if errors:
        raise ValidationError(errors)
    for gas, p in (cfg.get("emission_policy") or {}).items():
        es.set_emission_policy(gas, price=_load_value(p.get("price"), store), cap=p.get("cap"),
                               report_only=bool(p.get("report_only", False)))
    return es, dict(cfg.get("solver") or {})


# ------------------------------------------------------------------ dumping
class _SeriesSink:
    def __init__(self):
        self.series: dict[str, pd.Series] = {}

    def add(self, key: str, s: pd.Series) -> dict:
        key = key.replace(".", "_").replace("[", "_").replace("]", "")
        self.series[key] = s
        return {"series": f"{{ds}}:{key}"}


def _dump_value(v, key, sink: _SeriesSink):
    if isinstance(v, pd.Series):
        return sink.add(key, v)
    if isinstance(v, Bus):
        return v.name
    if isinstance(v, np.ndarray):
        return [float(x) for x in v]
    if isinstance(v, (np.floating, np.integer)):
        v = v.item()
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    return v


def _dump_fields(obj, prefix, sink) -> dict:
    out = {}
    for f in dataclasses.fields(obj):
        spec = f.metadata.get(SPEC_KEY)
        if spec is None:
            continue
        val = getattr(obj, f.name)
        default = f.default if f.default is not dataclasses.MISSING else (
            f.default_factory() if f.default_factory is not dataclasses.MISSING else dataclasses.MISSING)
        if default is not dataclasses.MISSING and not isinstance(val, pd.Series) and _equal(val, default):
            continue
        key = f"{prefix}.{f.name}"
        if spec.kind == Kind.BUS_MAP:
            out[f.name] = {(b.name if isinstance(b, Bus) else b): _dump_value(c, f"{key}.{getattr(b, 'name', b)}", sink)
                           for b, c in val.items()}
        elif spec.kind == Kind.TARIFFS:
            out[f.name] = [{"type": type(t).__name__, **_dump_fields(t, f"{key}{i}", sink)} for i, t in enumerate(val)]
        elif spec.kind == Kind.PORT_MAP:
            out[f.name] = _dump_port_map(f.name, val, key, sink)
        else:
            out[f.name] = _dump_value(val, key, sink)
    return out


def _dump_port_map(field, val, key, sink):
    if field == "flow_costs":
        return {p: ({"rate": _dump_value(v.rate, f"{key}.{p}", sink), "category": v.category.value}
                    if isinstance(v, FlowCost) else _dump_value(v, f"{key}.{p}", sink)) for p, v in val.items()}
    if field == "emission_factors":
        return {p: {g: _dump_value(x, f"{key}.{p}.{g}", sink) for g, x in gases.items()} for p, gases in val.items()}
    if field == "bounds":
        return {p: [_dump_value(x, f"{key}.{p}.{i}", sink) for i, x in enumerate(pair)] for p, pair in val.items()}
    return val


def _equal(a, b) -> bool:
    try:
        if isinstance(a, float) and isinstance(b, float) and math.isinf(a) and math.isinf(b):
            return True
        return bool(a == b)
    except (ValueError, TypeError):
        return False


def to_config(es: EnergySystem, *, dataset_name: str = "inputs") -> tuple[dict, dict[str, pd.Series]]:
    """Convert ``es`` to a config dict. Returns ``(config, series)`` where ``series`` maps column names
    of the generated dataset to the pandas Series found in the system."""
    sink = _SeriesSink()
    cfg: dict = {"name": es.name}
    if es.grid is not None and es.grid.is_datetime:
        start = es.grid.index[0]
        cfg["time"] = {"start": str(start.tz_localize(None) if start.tz is not None else start),
                       "periods": es.grid.n, "freq": es.grid.freq,
                       "tz": str(es.grid.tz) if es.grid.tz else None}
    cfg["carriers"] = [{"name": c.name, "kind": c.kind, "emission_factor": dict(c.emission_factor)}
                       for c in es.carriers.values()]
    cfg["buses"] = [{"name": b.name, "carrier": b.carrier.name, "allow_slack": b.allow_slack,
                     "slack_cost": b.slack_cost} for b in es.buses.values()]
    cfg["components"] = [{"type": type(c).type_name, "name": c.name, **_dump_fields(c, c.name, sink)}
                         for c in es.components.values()]
    if es.policies:
        cfg["emission_policy"] = {g: {"price": _dump_value(p.price, f"policy.{g}", sink), "cap": p.cap,
                                      "report_only": p.report_only} for g, p in es.policies.items()}
    text = yaml.safe_dump(cfg, sort_keys=False)
    cfg = yaml.safe_load(text.replace("{ds}", dataset_name))
    if sink.series:
        cfg["datasets"] = {dataset_name: f"{dataset_name}.csv"}
    return cfg, sink.series


def save_config(es: EnergySystem, folder: str | Path, *, dataset_name: str = "inputs") -> Path:
    """Write ``system.yaml`` and the referenced series (``<dataset_name>.csv``) into ``folder``.

    Series at different resolutions are written on the union of their timestamps; the loader
    drops the gaps again because each column is aligned separately.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cfg, series = to_config(es, dataset_name=dataset_name)
    if series:
        df = pd.DataFrame(series)
        df.index.name = "time"
        df.to_csv(folder / f"{dataset_name}.csv")
    path = folder / "system.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path

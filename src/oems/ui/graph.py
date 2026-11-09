"""Translation of the UI graph (blocks + wires) into a system config.

Graph format (produced by the frontend)::

    {"settings": {"start": "2025-01-01", "days": 7, "freq": "1h", "tz": "Europe/Copenhagen",
                  "co2_cap": null, "time_limit": 600, "tie_break": 0.01},
     "nodes": [{"id": "1", "kind": "bus", "name": "el", "carrier": "electricity", "carrier_kind": "electricity"},
               {"id": "2", "kind": "data", "dataset": "ds_ab12"},
               {"id": "3", "kind": "component", "type": "Storage", "name": "bess", "params": {...},
                "converter": {"inputs": [{"key": "in1", "coef": 1}], "outputs": [...], "capacity": "out1"}}],
     "edges": [{"from": ["3", "link"], "to": ["1", "port"]},          # storage (charge + discharge) <-> bus
               {"from": ["4", "out"], "to": ["1", "port"]},           # component port -> bus
               {"from": ["2", "da_price"], "to": ["3", "import_price"]}]}  # data column -> parameter

Energy wires have no direction: a component port is wired to a bus ``port`` and whether it is an inflow or an
outflow comes from the port itself (``catalog.ENERGY_CONNECTORS``, a converter's inputs / outputs). Graphs from older
projects, with ``component -> bus "supply"`` and ``bus "withdraw" -> component`` wires, are read the same way.
Energy wires decide the bus fields of components; data wires become ``{"series": "dataset:column"}``; a wire from a
daily-profile block (``kind: "profile"``, ``values``: one day of 24, 48 or 96 values typed in the UI) becomes
``{"daily": values}``, repeated on every day of the run (``oems.timeseries.DailyProfile``); a wire from
a degradation-curve block (``kind: "curves"``, made from a BESS IQ profile by ``dbsource.materialize``) puts its SoH
curves into the field (``{"curves": {cycles_per_day: [SoH, ...]}, "source": ...}``).

Settings ``base_year`` (a calendar year, or empty for a custom ``start`` / ``days``) and ``sim_years`` (default 1)
make a multi-year run: ``config["lifetime"] = {"years": N, "discount_rate": r, "inflation": i}`` (from
``settings.discount_rate`` and ``settings.inflation`` in %) repeats the base year N times (see ``oems.degradation``).

A PV (Producer) or storage node switched to variable size carries ``"sizing": {"optimise": true, "capex": ...,
"opex": ..., ...}`` (the component's ``Investment`` group, see ``catalog.SIZING_FIELDS``); older projects keep these
per component node id in ``assets`` of the Run settings block, which is still read for nodes without ``sizing``.
The Run settings block (``kind: "invest"``, not wired, at most one) holds the CO₂ cap (``co2_cap`` t, used by every
run; older projects keep it in ``settings.co2_cap``), the discount rate (``discount_rate`` %, default 7 without the
block), the inflation of prices and OPEX (``inflation`` %/yr, default 0) and optional investment hurdles
``min_irr`` (%), ``max_payback`` (years) and ``payback_kind`` (simple|discounted) and a minimum self-sufficiency
``min_self_sufficiency`` (%, share of the electricity demand not from the grid). With
``settings.run_mode == "optimise"`` every variable-size component gets its sizing fields and ``optimise_size``
and the run becomes a one-shot lifetime optimisation over ``sim_years`` years:
``config["lifetime"] = {"mode": "optimise", "model": settings.lifetime_model (weighted | full), ...}`` (see
``oems.sizing``). Other run modes use the fixed sizes.
The single ``link`` connector of grid connections and storages (see ``catalog.LINKS``) stands for all their
ports; wiring the ports one by one (``import``/``export``, ``charge``/``discharge``) is also accepted.
"""

from __future__ import annotations

from collections import defaultdict

from .catalog import ENERGY_CONNECTORS, LINKS, SIZING_FIELDS

NUMERIC_KINDS = {"scalar", "series"}


class GraphError(Exception):
    def __init__(self, errors: dict[str, list[str]]):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, vs in errors.items() for v in vs))


def graph_to_config(graph: dict, datasets: dict[str, str]) -> tuple[dict, dict[str, str]]:
    """Return ``(config, name_to_node_id)``. Raises GraphError with errors keyed by node id."""
    nodes = {str(n["id"]): n for n in graph.get("nodes", [])}
    edges = graph.get("edges", [])
    settings = graph.get("settings", {})
    errors: dict[str, list[str]] = defaultdict(list)

    buses = {nid: n for nid, n in nodes.items() if n.get("kind") == "bus"}
    comps = {nid: n for nid, n in nodes.items() if n.get("kind") == "component"}
    data = {nid: n for nid, n in nodes.items() if n.get("kind") == "data"}
    profiles = {nid: n for nid, n in nodes.items() if n.get("kind") == "profile"}
    curves = {nid: n for nid, n in nodes.items() if n.get("kind") == "curves"}
    invest = {nid: n for nid, n in nodes.items() if n.get("kind") == "invest"}
    optimise = settings.get("run_mode") == "optimise"
    for nid in list(invest)[1:]:
        errors[nid].append("only one Run settings block per project")
    sized = _sized_assets(next(iter(invest.values()), None), comps, errors) if optimise else {}
    if optimise and not sized and not settings.get("baseline"):    # a baseline system need not size anything
        errors["_global"].append("lifetime optimisation: switch at least one PV or storage to variable size")

    # names
    seen: dict[str, str] = {}
    for nid, n in {**buses, **comps}.items():
        name = (n.get("name") or "").strip()
        if not name or "." in name or ":" in name:
            errors[nid].append(f"invalid name {name!r}")
        elif name in seen:
            errors[nid].append(f"name {name!r} is used twice")
        seen[name] = nid

    # carriers and buses
    carriers: dict[str, dict] = {}
    cfg_buses = []
    for nid, b in buses.items():
        cname = b.get("carrier") or b.get("carrier_kind") or "other"
        kind = b.get("carrier_kind") or "other"
        if cname in carriers and carriers[cname]["kind"] != kind:
            errors[nid].append(f"carrier {cname!r} is used with two different kinds")
        ef = b.get("emission_factor")
        carriers.setdefault(cname, {"name": cname, "kind": kind,
                                    "emission_factor": {"co2": float(ef)} if ef not in (None, "") else {}})
        cfg_buses.append({"name": b["name"], "carrier": cname, "allow_slack": bool(b.get("allow_slack", False))})

    # wires
    energy_in: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))   # comp -> port -> [bus]
    energy_out: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    data_in: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))    # comp -> field -> [value]
    curves_in: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))  # comp -> field -> [curves]
    for e in edges:
        (src, sport), (dst, dport) = (str(e["from"][0]), e["from"][1]), (str(e["to"][0]), e["to"][1])
        if src in comps and dst in buses:            # component port -> bus port
            _add_energy(comps[src], src, sport, buses[dst]["name"], energy_out, energy_in, errors)
        elif src in buses and dst in comps:          # older projects: bus 'withdraw' -> component
            _add_energy(comps[dst], dst, dport, buses[src]["name"], energy_out, energy_in, errors)
        elif src in curves and dst in comps:
            if dport != "degradation":
                errors[src].append(f"degradation curves can only feed a storage's SoH curves, not {dport!r}")
            curves_in[dst][dport].append({"curves": curves[src].get("curves"), "source": curves[src].get("label")})
        elif src in data and dst in comps:
            ds = data[src].get("dataset")
            if ds not in datasets:
                errors[src].append(f"dataset {ds!r} is not uploaded")
            data_in[dst][dport].append({"series": f"{ds}:{sport}"})
        elif src in profiles and dst in comps:
            values = profiles[src].get("values") or []
            if not values or 1440 % len(values):
                errors[src].append(f"a daily profile needs 24, 48 or 96 values, not {len(values)}")
            data_in[dst][dport].append({"daily": values})
        else:
            errors[dst].append(f"invalid wire {src}.{sport} -> {dst}.{dport}")

    components = []
    name_to_node = {n["name"]: nid for nid, n in {**buses, **comps}.items() if n.get("name")}
    for nid, c in comps.items():
        ctype = c.get("type")
        params = {k: v for k, v in (c.get("params") or {}).items() if v not in (None, "")}
        if not params.get("tariffs"):
            params.pop("tariffs", None)
        d: dict = {"type": ctype, "name": c.get("name"), **params, **sized.get(nid, {})}
        for field, refs in data_in[nid].items():
            if len(refs) > 1:
                errors[nid].append(f"parameter {field!r} has more than one data wire")
            if not field.startswith("coef:"):
                d[field] = refs[0]
        for field, items in curves_in[nid].items():
            if len(items) > 1:
                errors[nid].append(f"parameter {field!r} has more than one degradation-curve wire")
            d[field] = items[0]
        if ctype == "Converter":
            conv = c.get("converter") or {}
            ins, outs = {}, {}
            key_bus = {}
            for side, store, wires in (("inputs", ins, energy_in[nid]), ("outputs", outs, energy_out[nid])):
                for item in conv.get(side, []):
                    key = item["key"]
                    bus_list = wires.get(key, [])
                    if len(bus_list) != 1:
                        errors[nid].append(f"connector {key!r} must be wired to exactly one bus")
                        continue
                    ref = data_in[nid].get(f"coef:{key}")
                    coef = ref[0] if ref else item.get("coef", 1.0)
                    if bus_list[0] in store:
                        errors[nid].append(f"bus {bus_list[0]!r} is connected twice on the {side} side")
                    store[bus_list[0]] = coef
                    key_bus[key] = bus_list[0]
            d["inputs"], d["outputs"] = ins, outs
            cap = conv.get("capacity")
            if cap and cap in key_bus:
                d["capacity_port"] = key_bus[cap]
        else:
            conns = ENERGY_CONNECTORS.get(ctype, {})
            field_bus: dict[str, set] = defaultdict(set)
            for port, (direction, field) in conns.items():
                wires = (energy_out if direction == "out" else energy_in)[nid].get(port, [])
                if len(wires) > 1:
                    errors[nid].append(f"connector {port!r} has more than one wire")
                field_bus[field].update(wires)
            for field, bset in field_bus.items():
                if len(bset) > 1:
                    errors[nid].append(f"ports of {field!r} are wired to different buses {sorted(bset)}")
                elif len(bset) == 1:
                    d[field] = next(iter(bset))
                elif field == "bus":
                    errors[nid].append("not connected to a bus")
        components.append(d)

    if errors:
        raise GraphError(dict(errors))

    s = settings
    start, days = base_period(s)
    time = {"start": start, "freq": s.get("freq", "1h"), "tz": s.get("tz") or None}
    steps_per_day = {"15min": 96, "30min": 48, "1h": 24}[time["freq"]]
    time["periods"] = int(days * steps_per_day)
    cfg = {"name": s.get("name", "ui"), "time": time, "datasets": dict(datasets),
           "carriers": list(carriers.values()), "buses": cfg_buses, "components": components,
           "solver": {"time_limit": s.get("time_limit") or None, "tie_break": float(s.get("tie_break", 0.01))}}
    block = next(iter(invest.values()), {})
    co2_cap = block.get("co2_cap") if block.get("co2_cap") not in (None, "") else s.get("co2_cap")
    if co2_cap not in (None, ""):
        cfg["emission_policy"] = {"co2": {"cap": float(co2_cap)}}
    if s.get("compute") == "cloud":                 # solve on a cloud machine (oems.cloud)
        cfg["compute"] = "cloud"
    years = sim_years(s)
    if optimise:
        rate = block.get("discount_rate", 7)
        cfg["lifetime"] = {"mode": "optimise", "years": years, "discount_rate": float(rate or 0) / 100,
                           "inflation": float(block.get("inflation") or 0) / 100,
                           "model": "full" if s.get("lifetime_model") == "full" else "weighted"}
        hurdles = {}
        if block.get("min_irr") not in (None, ""):
            hurdles["min_irr"] = float(block["min_irr"]) / 100
        if block.get("max_payback") not in (None, ""):
            hurdles.update(max_payback=float(block["max_payback"]), payback=block.get("payback_kind") or "simple")
        if hurdles:
            cfg["lifetime"]["hurdles"] = hurdles
        if block.get("min_self_sufficiency") not in (None, ""):
            cfg["lifetime"]["self_sufficiency"] = float(block["min_self_sufficiency"]) / 100
    elif years > 1:
        cfg["lifetime"] = {"years": years, "discount_rate": float(s.get("discount_rate") or 0) / 100,
                           "inflation": float(s.get("inflation") or 0) / 100}
    return cfg, name_to_node


def _sized_assets(block: dict | None, comps: dict, errors) -> dict[str, dict]:
    """The variable-size components as component params: ``{component node id: {"optimise_size": True, field:
    value}}``, from each node's ``sizing`` (else from ``assets`` of an older project's Run settings block). Missing
    CAPEX/OPEX are left to the model's validation (it names the component)."""
    legacy = (block or {}).get("assets") or {}
    out = {}
    for cid, comp in comps.items():
        a = comp.get("sizing") or legacy.get(cid) or {}
        if not a.get("optimise"):
            continue
        fields = SIZING_FIELDS.get(comp.get("type"))
        if fields is None:
            errors[cid].append(f"a {comp.get('type')} cannot be sized")
            continue
        out[cid] = {"optimise_size": True,
                    **{f: a[f] for f in fields if f != "optimise_size" and a.get(f) not in (None, "")}}
    return out


def base_period(settings: dict) -> tuple[str, float]:
    """``(start, days)`` of the run's base period: the calendar year ``base_year`` (365 or 366 days), else the
    custom ``start`` / ``days``."""
    year = settings.get("base_year")
    if year not in (None, "", "custom"):
        y = int(year)
        return f"{y}-01-01", 366.0 if (y % 4 == 0 and y % 100 != 0) or y % 400 == 0 else 365.0
    return settings.get("start", "2025-01-01"), float(settings.get("days", 7))


def sim_years(settings: dict) -> int:
    """Simulation length in years (1 = an ordinary run of the base period)."""
    try:
        return max(1, int(float(settings.get("sim_years") or 1)))
    except (TypeError, ValueError):
        return 1


def _add_energy(comp: dict, nid: str, connector: str, bus: str, energy_out, energy_in, errors) -> None:
    """Record an energy wire as an inflow or outflow of the component, by the meaning of its port; a ``link``
    connector stands for all ports of the component's link."""
    link = LINKS.get(comp.get("type"))
    ports = link["ports"] if link and connector == link["connector"] else [connector]
    for port in ports:
        direction = port_direction(comp, port)
        if direction is None:
            errors[nid].append(f"unknown energy port {port!r}")
        else:
            (energy_out if direction == "out" else energy_in)[nid][port].append(bus)


def port_direction(comp: dict, port: str) -> str | None:
    """``"in"`` if energy flows from the bus into the component through ``port``, ``"out"`` if it flows to the bus."""
    if comp.get("type") == "Converter":
        conv = comp.get("converter") or {}
        for direction, side in (("in", "inputs"), ("out", "outputs")):
            if any(item.get("key") == port for item in conv.get(side, [])):
                return direction
        return None
    entry = ENERGY_CONNECTORS.get(comp.get("type"), {}).get(port)
    return entry[0] if entry else None


def map_errors(messages: list[str], name_to_node: dict[str, str]) -> dict[str, list[str]]:
    """Attach validation messages to node ids by the component/bus name they start with or mention."""
    out: dict[str, list[str]] = defaultdict(list)
    names = sorted(name_to_node, key=len, reverse=True)
    for msg in messages:
        target = "_global"
        for n in names:
            if msg.startswith(f"{n}.") or msg.startswith(f"{n}:") or f"'{n}'" in msg or msg.startswith(f"{n} "):
                target = name_to_node[n]
                break
        out[target].append(msg)
    return dict(out)

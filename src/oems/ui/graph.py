"""Translation of the UI graph (blocks + wires) into a system config.

Graph format (produced by the frontend)::

    {"settings": {"start": "2025-01-01", "days": 7, "freq": "1h", "tz": "Europe/Copenhagen",
                  "co2_price": 85, "co2_cap": null, "time_limit": 600, "tie_break": 0.01},
     "nodes": [{"id": "1", "kind": "bus", "name": "el", "carrier": "electricity", "carrier_kind": "electricity"},
               {"id": "2", "kind": "data", "dataset": "ds_ab12"},
               {"id": "3", "kind": "component", "type": "Storage", "name": "bess", "params": {...},
                "converter": {"inputs": [{"key": "in1", "coef": 1}], "outputs": [...], "capacity": "out1"}}],
     "edges": [{"from": ["1", "withdraw"], "to": ["3", "link"]},      # bus -> storage (charge + discharge)
               {"from": ["4", "out"], "to": ["1", "supply"]},          # component OUT -> bus
               {"from": ["2", "da_price"], "to": ["3", "import_price"]}]}  # data column -> parameter

Energy wires decide the bus fields of components; data wires become ``{"series": "dataset:column"}``.
The single ``link`` connector of grid connections and storages (see ``catalog.LINKS``) stands for all their
ports; wiring the ports one by one (``import``/``export``, ``charge``/``discharge``) is also accepted.
"""

from __future__ import annotations

from collections import defaultdict

from .catalog import ENERGY_CONNECTORS, LINKS

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
    data_in: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))     # comp -> field -> [ref]
    for e in edges:
        (src, sport), (dst, dport) = (str(e["from"][0]), e["from"][1]), (str(e["to"][0]), e["to"][1])
        if src in comps and dst in buses and dport == "supply":
            _add_energy(comps[src], src, sport, buses[dst]["name"], energy_out, energy_in, "out")
        elif src in buses and sport == "withdraw" and dst in comps:
            _add_energy(comps[dst], dst, dport, buses[src]["name"], energy_out, energy_in, "in")
        elif src in data and dst in comps:
            ds = data[src].get("dataset")
            if ds not in datasets:
                errors[src].append(f"dataset {ds!r} is not uploaded")
            data_in[dst][dport].append(f"{ds}:{sport}")
        else:
            errors[dst].append(f"invalid wire {src}.{sport} -> {dst}.{dport}")

    components = []
    name_to_node = {n["name"]: nid for nid, n in {**buses, **comps}.items() if n.get("name")}
    for nid, c in comps.items():
        ctype = c.get("type")
        params = {k: v for k, v in (c.get("params") or {}).items() if v not in (None, "")}
        if not params.get("tariffs"):
            params.pop("tariffs", None)
        d: dict = {"type": ctype, "name": c.get("name"), **params}
        for field, refs in data_in[nid].items():
            if len(refs) > 1:
                errors[nid].append(f"parameter {field!r} has more than one data wire")
            if not field.startswith("coef:"):
                d[field] = {"series": refs[0]}
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
                    coef = {"series": ref[0]} if ref else item.get("coef", 1.0)
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
    time = {"start": s.get("start", "2025-01-01"), "freq": s.get("freq", "1h"), "tz": s.get("tz") or None}
    steps_per_day = {"15min": 96, "30min": 48, "1h": 24}[time["freq"]]
    time["periods"] = int(float(s.get("days", 7)) * steps_per_day)
    cfg = {"name": s.get("name", "ui"), "time": time, "datasets": dict(datasets),
           "carriers": list(carriers.values()), "buses": cfg_buses, "components": components,
           "solver": {"time_limit": s.get("time_limit") or None, "tie_break": float(s.get("tie_break", 0.01))}}
    if s.get("co2_price") not in (None, "") or s.get("co2_cap") not in (None, ""):
        cfg["emission_policy"] = {"co2": {
            "price": float(s["co2_price"]) if s.get("co2_price") not in (None, "") else None,
            "cap": float(s["co2_cap"]) if s.get("co2_cap") not in (None, "") else None}}
    return cfg, name_to_node


def _add_energy(comp: dict, nid: str, connector: str, bus: str, energy_out, energy_in, side: str) -> None:
    """Record an energy wire; a ``link`` connector stands for all ports of the component's link."""
    link = LINKS.get(comp.get("type"))
    if link and connector == link["connector"]:
        for port in link["ports"]:
            direction = ENERGY_CONNECTORS[comp["type"]][port][0]
            (energy_out if direction == "out" else energy_in)[nid][port].append(bus)
    else:
        (energy_out if side == "out" else energy_in)[nid][connector].append(bus)


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

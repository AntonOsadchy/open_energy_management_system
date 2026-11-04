"""Component catalog for the UI palette, generated from the registry and the field specs."""

from __future__ import annotations

import dataclasses

from ..components.base import REGISTRY
from ..components.tariffs import TARIFF_REGISTRY
from ..schema import SPEC_KEY, Kind, spec_to_dict

#: Energy connectors of each component type: port -> (direction, bus field).
#: Converter connectors are dynamic (one per input/output added in the UI).
ENERGY_CONNECTORS = {
    "GridConnection": {"import": ("out", "bus"), "export": ("in", "bus")},
    "Producer": {"out": ("out", "bus")},
    "Demand": {"in": ("in", "bus")},
    "FlexibleDemand": {"in": ("in", "bus")},
    "Sink": {"in": ("in", "bus")},
    "Storage": {"charge": ("in", "bus"), "discharge": ("out", "bus")},
    "ThermalZone": {"heat": ("in", "heat_bus"), "cold": ("in", "cold_bus")},
    "Converter": {},
}

#: Bidirectional components get ONE energy connector in the UI that wires all listed ports to the same bus:
#: an output (to a bus ``supply``) or an input (from a bus ``withdraw``).
LINKS = {"GridConnection": {"connector": "link", "direction": "out", "ports": ["import", "export"],
                            "label": "⇄ import / export"},
         "Storage": {"connector": "link", "direction": "in", "ports": ["charge", "discharge"],
                     "label": "⇄ charge / discharge"}}

#: Partner ports that must connect to the same bus (auto-wired by the UI for projects that wire them separately).
PARTNERS = {"GridConnection": {"import": "export", "export": "import"},
            "Storage": {"charge": "discharge", "discharge": "charge"}}

HIDDEN_FIELDS = {"name", "bus", "heat_bus", "cold_bus", "inputs", "outputs", "reference", "capacity_port", "bounds"}

PRESETS = [
    {"id": "heat_pump", "label": "Heat pump", "type": "Converter", "icon": "hp",
     "converter": {"inputs": [{"kind": "electricity", "coef": 1}], "outputs": [{"kind": "heat", "coef": 3}],
                   "capacity": "out1"}, "params": {"p_max": 1.0}},
    {"id": "e_boiler", "label": "E-boiler", "type": "Converter", "icon": "boiler",
     "converter": {"inputs": [{"kind": "electricity", "coef": 1}], "outputs": [{"kind": "heat", "coef": 0.99}],
                   "capacity": "in1"}, "params": {"p_max": 1.0}},
    {"id": "chiller", "label": "Chiller", "type": "Converter", "icon": "chiller",
     "converter": {"inputs": [{"kind": "electricity", "coef": 1}], "outputs": [{"kind": "cold", "coef": 4}],
                   "capacity": "out1"}, "params": {"p_max": 1.0}},
    {"id": "gas_boiler", "label": "Gas boiler", "type": "Converter", "icon": "flame",
     "converter": {"inputs": [{"kind": "fuel", "coef": 1}], "outputs": [{"kind": "heat", "coef": 0.92}],
                   "capacity": "out1"}, "params": {"p_max": 1.0}},
    {"id": "chp", "label": "CHP", "type": "Converter", "icon": "chp",
     "converter": {"inputs": [{"kind": "fuel", "coef": 1}],
                   "outputs": [{"kind": "electricity", "coef": 0.38}, {"kind": "heat", "coef": 0.45}],
                   "capacity": "in1"}, "params": {"p_max": 1.0}},
    {"id": "link", "label": "Link / cascade", "type": "Converter", "icon": "link",
     "converter": {"inputs": [{"kind": None, "coef": 1}], "outputs": [{"kind": None, "coef": 1}],
                   "capacity": "in1"}, "params": {}},
    {"id": "battery", "label": "Battery", "type": "Storage", "icon": "battery",
     "params": {"e_max": 2.0, "p_ch_max": 1.0, "p_dis_max": 1.0, "eta_ch": 0.95, "eta_dis": 0.95}},
    {"id": "tes", "label": "Heat storage", "type": "Storage", "icon": "tank",
     "params": {"e_max": 10.0, "p_ch_max": 3.0, "p_dis_max": 3.0, "eta_ch": 1.0, "eta_dis": 1.0,
                "self_discharge": 0.003}},
    {"id": "pv", "label": "PV", "type": "Producer", "icon": "sun", "params": {"capacity": 1.0}},
]


def _fields(cls) -> list[dict]:
    out = []
    for f in dataclasses.fields(cls):
        spec = f.metadata.get(SPEC_KEY)
        if spec is None or f.name in HIDDEN_FIELDS:
            continue
        default = f.default if f.default is not dataclasses.MISSING else dataclasses.MISSING
        out.append(spec_to_dict(f.name, spec, default))
    return out


def catalog() -> dict:
    comps = []
    for name, cls in REGISTRY.items():
        fields = _fields(cls)
        bus_kinds = {}
        for f in dataclasses.fields(cls):
            spec = f.metadata.get(SPEC_KEY)
            if spec is not None and spec.kind == Kind.BUS:
                bus_kinds[f.name] = spec.carrier_kind
        energy = [{"port": p, "direction": d, "field": fld, "carrier_kind": bus_kinds.get(fld)}
                  for p, (d, fld) in ENERGY_CONNECTORS.get(name, {}).items()]
        comps.append({
            "type": name, "label": getattr(cls, "label", name), "group": cls.palette_group, "icon": cls.icon,
            "doc": (cls.__doc__ or "").strip().split("\n\n")[0],
            "fields": fields, "energy": energy, "dynamic": name == "Converter",
            "data_connectors": [f["name"] for f in fields if f["connector"]],
            "partners": PARTNERS.get(name, {}), "link": LINKS.get(name),
        })
    tariffs = [{"type": n, "fields": _fields(c)} for n, c in TARIFF_REGISTRY.items()]
    return {"components": comps, "tariffs": tariffs, "presets": PRESETS,
            "carrier_kinds": ["electricity", "heat", "cold", "fuel", "other"],
            "resolutions": ["15min", "30min", "1h"]}

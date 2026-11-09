"""Declarative parameter specifications.

Each component field is declared once with :func:`param`. The attached :class:`Spec` drives
validation and coercion now, and config serialisation and auto-generated UI forms later.
"""

from __future__ import annotations

import dataclasses
from dataclasses import MISSING, dataclass
from enum import StrEnum


class Kind(StrEnum):
    SCALAR = "scalar"      # one number
    SERIES = "series"      # number or time series (aligned via as_param)
    BUS = "bus"            # reference to a Bus (object or name)
    BUS_MAP = "bus_map"    # {bus: coefficient (number or series)}
    ENUM = "enum"          # one of `choices`
    BOOL = "bool"
    STR = "str"
    PERIOD = "period"      # calendar period "D"/"W"/"M"/"Q"/"Y" or None
    TARIFFS = "tariffs"    # list of Tariff objects
    PORT_MAP = "port_map"  # {port name: value}
    CURVES = "curves"      # SoH curves {"curves": {cycles_per_day: [SoH, ...]}} (used by multi-year runs)


@dataclass(frozen=True)
class Spec:
    kind: Kind
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    choices: tuple | None = None
    series: str = "rate"            # meaning of a SERIES value (see timeseries.SERIES_KINDS)
    required: bool = False
    optional: bool = False          # None allowed (feature disabled)
    connector: bool = False         # UI: show as data connector by default
    carrier_kind: str | None = None # UI/validation: required carrier kind of a BUS field
    label: str | None = None
    help: str = ""
    group: str = "General"


SPEC_KEY = "oems"


def param(kind: Kind, default=MISSING, *, default_factory=MISSING, kw_only=MISSING, **spec_kwargs):
    """Declare a dataclass field together with its :class:`Spec`."""
    if default is MISSING and default_factory is MISSING:
        spec_kwargs.setdefault("required", True)
    if default is None:
        spec_kwargs.setdefault("optional", True)
    if "choices" in spec_kwargs and spec_kwargs["choices"] is not None:
        spec_kwargs["choices"] = tuple(spec_kwargs["choices"])
    meta = {SPEC_KEY: Spec(kind=kind, **spec_kwargs)}
    kwargs = {"metadata": meta}
    if default is not MISSING:
        kwargs["default"] = default
    if default_factory is not MISSING:
        kwargs["default_factory"] = default_factory
    if kw_only is not MISSING:
        kwargs["kw_only"] = kw_only
    return dataclasses.field(**kwargs)


def specs(obj_or_cls) -> dict[str, Spec]:
    """All declared specs of a dataclass instance or class, in field order."""
    return {
        f.name: f.metadata[SPEC_KEY]
        for f in dataclasses.fields(obj_or_cls)
        if SPEC_KEY in f.metadata
    }


def spec_to_dict(name: str, spec: Spec, default) -> dict:
    """JSON-friendly description of one field (used by the UI catalog)."""
    d = {
        "name": name,
        "kind": spec.kind.value,
        "unit": spec.unit,
        "min": spec.min,
        "max": spec.max,
        "choices": list(spec.choices) if spec.choices else None,
        "series": spec.series,
        "required": spec.required,
        "optional": spec.optional,
        "connector": spec.connector,
        "carrier_kind": spec.carrier_kind,
        "label": spec.label or name,
        "help": spec.help,
        "group": spec.group,
    }
    if default is not MISSING and isinstance(default, (int, float, str, bool, type(None))):
        d["default"] = None if isinstance(default, float) and default == float("inf") else default
        d["unlimited"] = isinstance(default, float) and default == float("inf")     # empty field = no limit
    return d

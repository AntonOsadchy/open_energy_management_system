"""Component base class, parameter resolution and the build-time helpers.

A component is a node of the energy-system graph. It exposes *ports* (directed edges to buses),
each of which gets one explicit non-negative flow variable ``m.F[port_id, t]`` in MW created by
the model builder. A component's ``build`` method adds its own variables and constraints to a
Pyomo block and writes its equations directly on those flow variables.

To add a new asset type, subclass :class:`Component`, declare its parameters with
:func:`oems.schema.param` and implement ``ports`` and ``build`` (optionally ``port_bounds``,
``cost_terms``, ``states``, ``check``). Register it with :func:`register_component` so it shows
up in configs and in the UI palette.
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from dataclasses import KW_ONLY, dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

import numpy as np
import pyomo.environ as pyo

from ..costs import CostCategory, FlowCost
from ..exceptions import ValidationError
from ..ports import Port
from ..schema import Kind, Spec, param, specs
from ..timeseries import TimeGrid, as_param

if TYPE_CHECKING:
    from ..system import EnergySystem

REGISTRY: dict[str, type] = {}


def register_component(type_name: str | None = None, *, group: str = "Other", icon: str = "box",
                       label: str | None = None):
    """Class decorator that makes a component available to configs and the UI palette."""

    def deco(cls):
        name = type_name or cls.__name__
        cls.type_name = name
        cls.palette_group = group
        cls.icon = icon
        cls.label = label or name
        REGISTRY[name] = cls
        return cls

    return deco


def safe_name(name: str) -> str:
    return re.sub(r"\W", "_", name)


# --------------------------------------------------------------------------- resolution
class Resolvable:
    """Mixin: resolve declared fields against a grid into ``self.r`` (a namespace of arrays)."""

    r: SimpleNamespace

    def resolve(self, grid: TimeGrid, system: EnergySystem, report: list | None = None,
                owner: str | None = None) -> None:
        owner = owner or getattr(self, "name", type(self).__name__)
        r = SimpleNamespace()
        errors: list[str] = []
        for fname, sp in specs(self).items():
            try:
                setattr(r, fname, resolve_value(getattr(self, fname), sp, grid, system, report, owner, fname))
            except ValidationError as e:
                errors.extend(e.errors)
        if errors:
            raise ValidationError(errors)
        self.r = r


def _check_range(arr, sp: Spec, label: str):
    vals = np.atleast_1d(arr)
    finite = vals[np.isfinite(vals)]
    if sp.min is not None and np.any(vals < sp.min):
        raise ValidationError(f"{label}: must be >= {sp.min} (got min {np.min(vals):g})")
    if sp.max is not None and finite.size and np.any(finite > sp.max):
        raise ValidationError(f"{label}: must be <= {sp.max} (got max {np.max(finite):g})")


def resolve_value(val, sp: Spec, grid, system, report, owner, fname):
    label = f"{owner}.{fname}"
    if val is None:
        if sp.required and not sp.optional:
            raise ValidationError(f"{label}: is required")
        return None
    k = sp.kind
    if k == Kind.SCALAR:
        try:
            out = float(val)
        except (TypeError, ValueError) as err:
            raise ValidationError(f"{label}: expected a number, got {val!r}") from err
        _check_range(out, sp, label)
        return out
    if k == Kind.SERIES:
        arr = as_param(val, grid, kind=sp.series, name=fname, owner=owner, report=report)
        _check_range(arr, sp, label)
        return arr
    if k == Kind.BUS:
        bus = system.bus(val)
        if sp.carrier_kind and bus.carrier.kind != sp.carrier_kind:
            raise ValidationError(
                f"{label}: needs a bus of carrier kind {sp.carrier_kind!r}, "
                f"but {bus.name!r} is {bus.carrier.kind!r}")
        return bus
    if k == Kind.BUS_MAP:
        out = {}
        for key, coef in dict(val).items():
            bus = system.bus(key)
            if bus in out:
                raise ValidationError(f"{label}: bus {bus.name!r} listed twice")
            arr = as_param(coef, grid, kind="ratio", name=f"{fname}[{bus.name}]", owner=owner, report=report)
            if np.any(arr < 0):
                raise ValidationError(f"{label}[{bus.name}]: coefficients must be >= 0")
            out[bus] = arr
        return out
    if k == Kind.ENUM:
        if sp.choices and val not in sp.choices:
            raise ValidationError(f"{label}: must be one of {list(sp.choices)}, got {val!r}")
        return val
    if k == Kind.BOOL:
        return bool(val)
    if k == Kind.TARIFFS:
        out = []
        for i, tariff in enumerate(val):
            tariff.resolve(grid, system, report, owner=f"{owner}.tariffs[{i}]")
            out.append(tariff)
        return out
    return val  # STR, PERIOD, PORT_MAP: resolved by their users


# --------------------------------------------------------------------------- build helpers
class PortFlows:
    """View of the global flow variables for one component: ``F["charge"][t]``."""

    def __init__(self, m, comp_name: str):
        self._F = m.F
        self._c = comp_name

    def __getitem__(self, port: str):
        return _PortView(self._F, f"{self._c}.{port}")


class _PortView:
    __slots__ = ("_F", "_pid")

    def __init__(self, F, pid):
        self._F = F
        self._pid = pid

    def __getitem__(self, t):
        return self._F[self._pid, t]


class BuildContext:
    """Information shared with components while the model is built."""

    def __init__(self, m, grid: TimeGrid, tie_break: float):
        self.m = m
        self.grid = grid
        self.n = grid.n
        self.T = m.T
        self.dt = grid.dt
        self.w = grid.w
        self.wdt = grid.w * grid.dt
        self.tie_break = float(tie_break)

    def wsum(self, coef, view) -> object:
        """sum_t w_t * dt_t * coef_t * view[t] (skips zero coefficients)."""
        c = np.broadcast_to(np.asarray(coef, dtype=float), (self.n,)) * self.wdt
        nz = np.flatnonzero(c)
        if nz.size == 0:
            return 0.0
        return pyo.quicksum(float(c[t]) * view[int(t)] for t in nz)

    def prev(self, t: int):
        return t - 1 if t > 0 else None


def none_if_inf(x):
    return None if x is None or (isinstance(x, float) and math.isinf(x)) else x


# --------------------------------------------------------------------------- component
@dataclass(eq=False)
class Component(Resolvable, ABC):
    """Base class of all components.

    Generic options available on every component:

    * ``flow_costs``: ``{port: EUR/MWh | FlowCost(rate, category)}`` extra variable cost on a port
    * ``emission_factors``: ``{port: {"co2": t/MWh}}`` emissions caused by the flow on a port
    * ``bounds``: ``{port: (min MW, max MW)}`` extra bounds on a port's flow
    """

    name: str
    _: KW_ONLY
    flow_costs: dict = param(Kind.PORT_MAP, default_factory=dict, group="Costs",
                             help="Extra variable cost per port, EUR/MWh")
    emission_factors: dict = param(Kind.PORT_MAP, default_factory=dict, group="Emissions",
                                   help="Emission factors per port, {gas: t/MWh}")
    bounds: dict = param(Kind.PORT_MAP, default_factory=dict, group="Advanced",
                         help="Extra (min, max) MW bounds per port")

    type_name = "Component"
    palette_group = "Other"
    icon = "box"

    # --- structure -----------------------------------------------------------------
    @abstractmethod
    def ports(self) -> dict[str, Port]:
        """Ports of the component (requires ``resolve`` to have run)."""

    def port_bounds(self) -> dict[str, tuple]:
        """{port: (lb array | None, ub array | None)} applied directly as variable bounds."""
        return {}

    def check(self, grid: TimeGrid) -> list[str]:
        """Semantic checks after resolution. Raise ValidationError for errors, return warnings."""
        return []

    # --- model ---------------------------------------------------------------------
    @abstractmethod
    def build(self, blk: pyo.Block, F: PortFlows, ctx: BuildContext) -> None:
        """Add variables and constraints of this component to ``blk``."""

    def cost_terms(self, blk: pyo.Block, F: PortFlows, ctx: BuildContext) -> dict:
        """{CostCategory: linear expression in EUR}."""
        return {}

    def states(self, blk: pyo.Block) -> dict[str, pyo.Var]:
        """Time-indexed block variables reported as states."""
        return {}

    def default_emission_factors(self) -> dict[str, dict[str, np.ndarray]]:
        """Emission factors applied when the user gives none (grid connections use carrier defaults)."""
        return {}

    # --- generic options -------------------------------------------------------------
    def _check_ports_in(self, mapping: dict, what: str):
        names = set(self.ports())
        unknown = [p for p in mapping if p not in names]
        if unknown:
            raise ValidationError(f"{self.name}.{what}: unknown port(s) {unknown}; ports are {sorted(names)}")

    def resolved_flow_costs(self, grid, report) -> dict[str, tuple[np.ndarray, CostCategory]]:
        self._check_ports_in(self.flow_costs, "flow_costs")
        out = {}
        for port, val in self.flow_costs.items():
            fc = val if isinstance(val, FlowCost) else FlowCost(val)
            out[port] = (as_param(fc.rate, grid, kind="rate", name=f"flow_costs[{port}]", owner=self.name,
                                  report=report), fc.category)
        return out

    def resolved_emission_factors(self, grid, report) -> dict[str, dict[str, np.ndarray]]:
        self._check_ports_in(self.emission_factors, "emission_factors")
        out = {p: dict(g) for p, g in self.default_emission_factors().items()}
        for port, gases in self.emission_factors.items():
            out.setdefault(port, {})
            for gas, val in dict(gases).items():
                out[port][gas] = as_param(val, grid, kind="rate", name=f"emission_factors[{port}][{gas}]",
                                          owner=self.name, report=report)
        return out

    def resolved_bounds(self, grid, report) -> dict[str, tuple]:
        self._check_ports_in(self.bounds, "bounds")
        out = {}
        for port, (lo, hi) in self.bounds.items():
            lo_a = None if lo is None else as_param(lo, grid, kind="power", name=f"bounds[{port}].min",
                                                     owner=self.name, report=report)
            hi_a = None if hi is None else as_param(hi, grid, kind="power", name=f"bounds[{port}].max",
                                                     owner=self.name, report=report)
            out[port] = (lo_a, hi_a)
        return out

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"

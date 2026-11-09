"""The energy system: buses, components and policies, plus validate/build/solve."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import numpy as np

from .carrier import Bus, Carrier
from .components.base import Component
from .components.consumer import Demand
from .components.grid import GridConnection
from .components.storage import Storage
from .components.tariffs import EnergyCredit
from .emissions import EmissionPolicy
from .exceptions import InfeasibleModelError, OEMSError, SolverError, UnboundedModelError, ValidationError
from .model import build_model
from .ports import Direction
from .solver import SolveStatus, solve_model
from .timeseries import TimeGrid, as_param


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    alignment: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class EnergySystem:
    """Container of buses, components and emission policies.

    Components keep the raw inputs the user gave (scalars or Series); they are resolved against a
    :class:`TimeGrid` at build time, so the same system can be solved at different resolutions
    with ``solve(grid=...)``.
    """

    def __init__(self, grid: TimeGrid | None = None, name: str = "oems"):
        self.grid = grid
        self.name = name
        self.buses: dict[str, Bus] = {}
        self.components: dict[str, Component] = {}
        self.policies: dict[str, EmissionPolicy] = {}

    # ------------------------------------------------------------------ construction
    def add_bus(self, bus: str | Bus, carrier: Carrier | None = None, **kwargs) -> Bus:
        """Add a bus: ``add_bus("el", Carrier("electricity", kind="electricity"))`` or ``add_bus(Bus(...))``."""
        if not isinstance(bus, Bus):
            if carrier is None:
                raise ValidationError(f"bus {bus!r}: a carrier is required")
            bus = Bus(bus, carrier, **kwargs)
        self._check_name(bus.name)
        if bus.name in self.buses and self.buses[bus.name] is not bus:
            raise ValidationError(f"bus {bus.name!r} already exists")
        self.buses[bus.name] = bus
        return bus

    def add(self, *items):
        """Add components (or buses). Returns the item, or a list when several are given."""
        for item in items:
            if isinstance(item, Bus):
                self.add_bus(item)
                continue
            if not isinstance(item, Component):
                raise TypeError(f"cannot add {item!r}")
            self._check_name(item.name)
            if item.name in self.components:
                raise ValidationError(f"component {item.name!r} already exists")
            self.components[item.name] = item
        return items[0] if len(items) == 1 else list(items)

    def remove(self, name: str) -> None:
        self.components.pop(name, None) or self.buses.pop(name, None)

    @staticmethod
    def _check_name(name: str):
        if not name or "." in name:
            raise ValidationError(f"invalid name {name!r} (must be non-empty and contain no '.')")

    def bus(self, ref) -> Bus:
        """Resolve a bus reference (object or name). Unregistered Bus objects are added."""
        if isinstance(ref, Bus):
            known = self.buses.get(ref.name)
            if known is None:
                return self.add_bus(ref)
            if known is not ref:
                raise ValidationError(f"a different bus named {ref.name!r} is already registered")
            return ref
        if isinstance(ref, str) and ref in self.buses:
            return self.buses[ref]
        raise ValidationError(f"unknown bus {ref!r}")

    def set_emission_policy(self, gas: str = "co2", price=None, cap: float | None = None,
                            report_only: bool = False) -> EmissionPolicy:
        """Price (EUR/t, scalar or series) and/or cap (t) an emission type."""
        policy = EmissionPolicy(gas, price, cap, report_only)
        self.policies[gas] = policy
        return policy

    @property
    def carriers(self) -> dict[str, Carrier]:
        return {b.carrier.name: b.carrier for b in self.buses.values()}

    # ------------------------------------------------------------------ validation
    def validate(self, grid: TimeGrid | None = None, raise_on_error: bool = True) -> ValidationReport:
        """Resolve all inputs against ``grid`` and check the system. Raises ValidationError on errors."""
        grid = grid or self.grid
        rep = ValidationReport()
        if grid is None:
            rep.errors.append("no TimeGrid given (EnergySystem(grid=...) or solve(grid=...))")
            if raise_on_error:
                raise ValidationError(rep.errors)
            return rep
        resolved = SimpleNamespace(flow_costs={}, emission_factors={}, bounds={}, policy_prices={})
        ok = []
        for comp in self.components.values():
            try:
                comp.resolve(grid, self, rep.alignment)
                comp.ports()
                ok.append(comp)
            except ValidationError as e:
                rep.errors.extend(e.errors)
        for comp in ok:
            try:
                rep.warnings.extend(comp.check(grid))
                resolved.flow_costs[comp.name] = comp.resolved_flow_costs(grid, rep.alignment)
                resolved.emission_factors[comp.name] = comp.resolved_emission_factors(grid, rep.alignment)
                resolved.bounds[comp.name] = comp.resolved_bounds(grid, rep.alignment)
                comp.port_bounds()
            except ValidationError as e:
                rep.errors.extend(e.errors)
        for gas, pol in self.policies.items():
            if pol.price is not None:
                try:
                    resolved.policy_prices[gas] = as_param(pol.price, grid, kind="rate", name=f"policy[{gas}].price",
                                                           report=rep.alignment)
                except ValidationError as e:
                    rep.errors.extend(e.errors)
        if not rep.errors:
            self._check_topology(grid, rep)
            self._check_emissions(rep)
        if rep.errors and raise_on_error:
            raise ValidationError(rep.errors)
        self._resolved = resolved
        return rep

    def _check_topology(self, grid, rep: ValidationReport):
        if not self.components:
            rep.errors.append("the system has no components")
            return
        ports_on = {b: {Direction.IN: [], Direction.OUT: []} for b in self.buses}
        for comp in self.components.values():
            for pname, port in comp.ports().items():
                ports_on[port.bus.name][port.direction].append((comp, pname))
        for b, d in ports_on.items():
            bus = self.buses[b]
            if not d[Direction.IN] and not d[Direction.OUT]:
                rep.warnings.append(f"bus {b!r} is not connected to any component")
                continue
            if not bus.allow_slack and (not d[Direction.IN] or not d[Direction.OUT]):
                missing = "withdrawal (IN)" if not d[Direction.IN] else "supply (OUT)"
                rep.errors.append(f"bus {b!r} has no {missing} port; connect a component or set allow_slack=True")
                continue
            # fixed demand vs. supply capacity
            demand = np.zeros(grid.n)
            for comp, _ in d[Direction.IN]:
                if isinstance(comp, Demand):
                    demand += comp.r.profile
            cap = np.zeros(grid.n)
            for comp, pname in d[Direction.OUT]:
                hi = comp.port_bounds().get(pname, (None, None))[1]
                if hi is None:
                    cap = None
                    break
                cap = cap + hi
            # storage that can never sell: nothing else withdraws from the bus and grid exports earn nothing
            stores = [c for c, p in d[Direction.IN] if isinstance(c, Storage)]
            others = [c for c, p in d[Direction.IN] if not isinstance(c, (Storage, GridConnection))]
            grids = [c for c, p in d[Direction.IN] if isinstance(c, GridConnection)]
            if stores and not others and not any(
                    any(isinstance(t, EnergyCredit) for t in g.r.all_tariffs) for g in grids):
                rep.warnings.append(
                    f"bus {b!r}: {', '.join(c.name for c in stores)} has nothing to discharge into - no consumer on "
                    "the bus and grid exports earn nothing; wire an export_price (and allow export) or add a demand")
            if cap is not None and np.any(demand > cap + 1e-9) and not bus.allow_slack:
                k = int(np.sum(demand > cap + 1e-9))
                rep.warnings.append(f"bus {b!r}: fixed demand exceeds total supply capacity in {k} steps "
                                    "(unless storage or flexibility covers it, the model is infeasible)")

    def _check_emissions(self, rep: ValidationReport):
        for comp in self.components.values():
            if isinstance(comp, GridConnection):
                continue
            ports = comp.ports()
            for port, gases in comp.emission_factors.items():
                carrier = ports[port].bus.carrier
                both = set(gases) & set(carrier.emission_factor)
                if both:
                    rep.warnings.append(
                        f"{comp.name}.{port}: emission factor for {sorted(both)} is also set on carrier "
                        f"{carrier.name!r}; emissions may be counted twice (count at the system boundary)")

    # ------------------------------------------------------------------ build / solve
    def build(self, grid: TimeGrid | None = None, *, tie_break: float = 0.01, slack_all: bool = False):
        """Validate and build the Pyomo model. Returns ``(model, meta, validation_report)``."""
        grid = grid or self.grid
        rep = self.validate(grid)
        m, meta = build_model(self, grid, self._resolved, tie_break=tie_break, slack_all=slack_all)
        return m, meta, rep

    def solve(self, grid: TimeGrid | None = None, *, time_limit: float | None = None, threads: int | None = None,
              options: dict | None = None, tie_break: float = 0.01, diagnose: bool = True,
              slack_all: bool = False):
        """Build and solve the LP and return :class:`~oems.results.Results`.

        ``tie_break`` (EUR/MWh) is a tiny cost on storage throughput, converter activity and
        flexibility backlog that removes degenerate alternative optima; it is reported separately
        (category TIEBREAK) and excluded from ``Results.total_cost``.
        With ``diagnose`` an infeasible model is re-solved with slack on every bus to report where
        the energy balance fails.
        """
        from .results import Results

        grid = grid or self.grid
        m, meta, rep = self.build(grid, tie_break=tie_break, slack_all=slack_all)
        if grid.horizon is not None and "solver" not in (options or {}):
            # a lifetime horizon couples every step through the size variables: interior point (with crossover, so
            # duals stay exact) is several times faster than simplex there (15 hourly years: simplex ~40 min)
            options = {**(options or {}), "solver": "ipm"}
        info = solve_model(m, time_limit=time_limit, threads=threads, options=options)
        if info.status == SolveStatus.INFEASIBLE:
            report = None
            if diagnose and not slack_all:
                try:
                    diag = self.solve(grid, time_limit=time_limit, threads=threads, options=options,
                                      tie_break=0.0, diagnose=False, slack_all=True)
                    report = diag.slack
                except OEMSError:
                    report = None
            msg = "the model is infeasible"
            if report is not None and len(report):
                buses = ", ".join(sorted(report["bus"].unique()))
                msg += f"; the energy balance cannot be met on bus(es) {buses} (see .report)"
            raise InfeasibleModelError(msg, report)
        if info.status == SolveStatus.UNBOUNDED:
            raise UnboundedModelError(
                "the model is unbounded - typical causes: an export credit above the import cost without "
                "max_export, a negative marginal cost without a production limit, or a sink with negative cost "
                "and no p_max, or an optimised size without a maximum that pays off without limit")
        if not info.has_solution:
            raise SolverError(f"solver finished without a solution ({info.message})")
        return Results(self, m, meta, info, rep)

    def write_lp(self, path, grid: TimeGrid | None = None, tie_break: float = 0.01) -> None:
        """Write the LP file for inspection."""
        m, _meta, _rep = self.build(grid, tie_break=tie_break)
        m.write(str(path), io_options={"symbolic_solver_labels": True})

    def __repr__(self) -> str:
        return f"EnergySystem({len(self.buses)} buses, {len(self.components)} components)"

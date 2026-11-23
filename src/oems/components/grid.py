"""Grid connection: import/export of any carrier across the system boundary."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass

import numpy as np
import pyomo.environ as pyo

from ..costs import CostCategory
from ..exceptions import ValidationError
from ..ports import Direction, Port
from ..schema import Kind, param
from .base import Component, register_component
from .tariffs import MEASUREMENTS, EnergyCharge, EnergyCredit, PeakCharge, Tariff

#: The two directions of a connection: import (consumption) and export (production).
DIRECTIONS = ("import", "export")


@register_component(group="Grid", icon="grid", label="Grid connection")
@dataclass(eq=False)
class GridConnection(Component):
    """Import/export connection (electricity grid, gas supply, district heating).

    Ports: ``import`` (OUT, grid -> bus) and ``export`` (IN, bus -> grid).

    ``max_import`` and ``max_export`` are the connection's consumption and production capacity (MW, series allowed,
    e.g. a feed-in limit that changes over time); they default to no limit (inf); set ``max_export=0`` for an
    import-only connection.

    Costs come from ``tariffs`` plus the convenience arguments ``import_price``, ``import_fee``,
    ``export_price``, ``export_fee`` and ``peak_charge`` (all EUR/MWh or EUR/MW per period). Exports earn nothing
    unless an ``export_price`` (or another export credit) is given. ``import_capacity_fee`` and
    ``export_capacity_fee`` (EUR/MW/yr, category NETWORK) are fixed yearly fees on the consumption and production
    capacity (the highest ``max_import`` / ``max_export``, plus its expansion): pro rata for a horizon shorter than a
    year, every year (discounted and inflated) in a lifetime run. ``co2_price`` (EUR/t) prices the connection's CO2
    emissions in place of the system emission policy price.

    With ``optimise_size`` (lifetime runs, see ``oems.sizing``) each direction can be expanded on its own: an
    expansion ``x_import`` (MW) in [0, ``expansion_max_import``] raises ``max_import`` to ``max_import + x_import``
    in every step, CAPEX ``capex_import * x_import`` once and OPEX ``opex_import * x_import`` per year; the same for
    export. A max expansion of 0 keeps that direction as it is (its CAPEX / OPEX are then not needed). The reported
    sizes are the import and export capacity after the expansion (their minimum: the existing capacity).
    """

    bus: object = param(Kind.BUS)
    _: KW_ONLY
    tariffs: list = param(Kind.TARIFFS, default_factory=list, group="Tariffs")
    max_import: object = param(Kind.SERIES, float("inf"), unit="MW", series="power", min=0,
                               help="Consumption capacity of the connection (series allowed); with a variable size "
                                    "the existing one. Empty = no limit")
    max_export: object = param(Kind.SERIES, float("inf"), unit="MW", series="power", min=0,
                               help="Production capacity of the connection, e.g. a feed-in limit (series allowed; "
                                    "0 = import only); with a variable size the existing one. Empty = no limit")
    exclusive: bool = param(Kind.BOOL, False, help="Discourage simultaneous import and export (LP relaxation)")
    import_price: object = param(Kind.SERIES, None, unit="EUR/MWh", series="rate", connector=True, group="Tariffs")
    import_fee: object = param(Kind.SERIES, None, unit="EUR/MWh", series="rate", connector=True, group="Tariffs")
    export_price: object = param(Kind.SERIES, None, unit="EUR/MWh", series="rate", connector=True, group="Tariffs")
    export_fee: object = param(Kind.SERIES, None, unit="EUR/MWh", series="rate", connector=True, group="Tariffs")
    import_capacity_fee: float = param(Kind.SCALAR, None, unit="EUR/MW/yr", min=0, group="Tariffs",
                                       label="Import capacity fee",
                                       help="Fixed yearly grid fee per MW of consumption capacity (max_import, plus "
                                            "its expansion); pro rata for runs shorter than a year")
    export_capacity_fee: float = param(Kind.SCALAR, None, unit="EUR/MW/yr", min=0, group="Tariffs",
                                       label="Export capacity fee",
                                       help="Fixed yearly grid fee per MW of production capacity (max_export, plus "
                                            "its expansion); pro rata for runs shorter than a year")
    peak_charge: float = param(Kind.SCALAR, None, unit="EUR/MW/period", min=0, group="Tariffs")
    peak_period: str = param(Kind.PERIOD, "M", group="Tariffs")
    peak_measurement: str = param(Kind.ENUM, "step", choices=MEASUREMENTS, group="Tariffs")
    co2_price: object = param(Kind.SERIES, None, unit="EUR/t", series="rate", connector=True, group="Emissions",
                              help="CO2 price on this connection's emissions (needs a CO2 factor on the bus)")
    credit_export_emissions: bool = param(Kind.BOOL, False, group="Emissions",
                                          help="Credit avoided emissions on export with the import factor")
    optimise_size: bool = param(Kind.BOOL, False, group="Investment", label="Optimise expansion",
                                help="The lifetime optimisation may expand the import and export capacity (needs "
                                     "CAPEX and OPEX of each direction it may expand)")
    expansion_max_import: float = param(Kind.SCALAR, None, unit="MW", min=0, group="Investment",
                                        label="Max import expansion",
                                        help="Most consumption capacity that can be added to max_import. 0 = none, "
                                             "empty = no limit")
    capex_import: float = param(Kind.SCALAR, None, unit="EUR/MW", min=0, group="Investment", label="CAPEX import",
                                help="One-off cost per MW of consumption capacity added (e.g. connection fee and "
                                     "grid works)")
    opex_import: float = param(Kind.SCALAR, None, unit="EUR/MW/yr", min=0, group="Investment", label="OPEX import",
                               help="Yearly cost per MW of consumption capacity added")
    expansion_max_export: float = param(Kind.SCALAR, None, unit="MW", min=0, group="Investment",
                                        label="Max export expansion",
                                        help="Most production capacity that can be added to max_export. 0 = none, "
                                             "empty = no limit")
    capex_export: float = param(Kind.SCALAR, None, unit="EUR/MW", min=0, group="Investment", label="CAPEX export",
                                help="One-off cost per MW of production capacity added")
    opex_export: float = param(Kind.SCALAR, None, unit="EUR/MW/yr", min=0, group="Investment", label="OPEX export",
                               help="Yearly cost per MW of production capacity added")

    def resolve(self, grid, system, report=None, owner=None):
        super().resolve(grid, system, report, owner)
        sugar: list[Tariff] = []
        if self.import_price is not None:
            sugar.append(EnergyCharge(self.import_price, direction="import", category="ENERGY"))
        if self.import_fee is not None:
            sugar.append(EnergyCharge(self.import_fee, direction="import", category="NETWORK"))
        if self.export_price is not None:
            sugar.append(EnergyCredit(self.export_price, direction="export"))
        if self.export_fee is not None:
            sugar.append(EnergyCharge(self.export_fee, direction="export", category="NETWORK"))
        if self.peak_charge is not None:
            sugar.append(PeakCharge(self.peak_charge, direction="import", period=self.peak_period,
                                    measurement=self.peak_measurement))
        for i, t in enumerate(sugar):
            t.resolve(grid, system, report, owner=f"{self.name}.tariff_sugar[{i}]")
        self.r.all_tariffs = list(self.r.tariffs) + sugar

    def ports(self):
        return {"import": Port(self.r.bus, Direction.OUT), "export": Port(self.r.bus, Direction.IN)}

    def existing(self, direction: str) -> np.ndarray:
        """The existing capacity of one direction per step in MW: ``max_import`` or ``max_export`` (inf = none)."""
        return self.r.max_import if direction == "import" else self.r.max_export

    def expandable(self, direction: str) -> bool:
        """An optimised expansion may raise this direction (its max expansion is not 0)."""
        if not self.optimise_size:
            return False
        most = getattr(self.r, f"expansion_max_{direction}")
        return most is None or most > 0

    def limits(self) -> tuple[np.ndarray, np.ndarray]:
        """Import and export limit per step in MW (inf = none): ``max_import`` / ``max_export``; with an optimised
        expansion, the most it may reach."""
        out = []
        for d in DIRECTIONS:
            lim = self.existing(d)
            if self.expandable(d):
                most = getattr(self.r, f"expansion_max_{d}")
                lim = lim + (np.inf if most is None else most)
            out.append(lim)
        return out[0], out[1]

    def billed_capacity(self, direction: str) -> float:
        """The existing capacity of one direction (MW) its capacity fee is charged on: the highest ``max_import`` /
        ``max_export`` (inf = no limit given). An expansion is added to it."""
        return float(np.max(self.existing(direction)))

    def port_bounds(self):
        pi, pe = self.limits()
        return {"import": (None, pi), "export": (None, pe)}

    def default_emission_factors(self):
        out = {}
        for gas, factor in self.r.bus.carrier.emission_factor.items():
            arr = np.full(len(self.r.max_import), float(factor))
            out.setdefault("import", {})[gas] = arr
            if self.credit_export_emissions:
                out.setdefault("export", {})[gas] = -arr
        return out

    def emission_prices(self):
        return {} if self.co2_price is None else {"co2": self.r.co2_price}

    def resolved_emission_factors(self, grid, report):
        out = super().resolved_emission_factors(grid, report)
        if self.credit_export_emissions and "import" in out:
            for gas, arr in out["import"].items():
                out.setdefault("export", {}).setdefault(gas, -arr)
        return out

    def check(self, grid):
        warnings = []
        r = self.r
        for t in r.all_tariffs:
            warnings += [f"{self.name}: {w}" for w in t.check(grid)]
        if self.optimise_size:
            costs = {}
            for d in DIRECTIONS:
                if self.expandable(d):
                    costs |= {f"CAPEX {d}": getattr(r, f"capex_{d}"), f"OPEX {d}": getattr(r, f"opex_{d}")}
            self.check_sizing(grid, costs)
            for d in DIRECTIONS:
                if self.expandable(d) and not np.all(np.isfinite(self.existing(d))):
                    raise ValidationError(f"{self.name}: an expansion of {d} needs the existing max_{d} (0 for a new "
                                          f"connection), or a max {d} expansion of 0")
        for d in DIRECTIONS:
            if getattr(r, f"{d}_capacity_fee") and not np.isfinite(self.billed_capacity(d)):
                raise ValidationError(f"{self.name}: {d}_capacity_fee is charged per MW of {d} capacity: set "
                                      f"max_{d}")
        if self.co2_price is not None and not any("co2" in g for g in self.default_emission_factors().values()) \
                and not any("co2" in dict(g) for g in self.emission_factors.values()):
            warnings.append(f"{self.name}: co2_price is set but the connection has no CO2 emission factor "
                            "(set one on the bus), so it costs nothing")
        pi, pe = self.limits()
        if self.exclusive and (np.any(~np.isfinite(pi)) or np.any(~np.isfinite(pe))):
            if not self.optimise_size:
                raise ValidationError(f"{self.name}: exclusive=True requires finite max_import and max_export")
            warnings.append(f"{self.name}: exclusive=True has no effect where an expansion has no max")
        cost_imp = np.zeros(grid.n)    # EUR/MWh paid per MWh imported
        credit_exp = np.zeros(grid.n)  # EUR/MWh received per MWh exported
        for t in self.r.all_tariffs:
            if not isinstance(t, (EnergyCharge, EnergyCredit)):
                continue
            sign = 1.0 if isinstance(t, EnergyCharge) else -1.0
            if t.direction == "import":
                cost_imp += sign * t.r.rate
            else:
                credit_exp -= sign * t.r.rate
        risky = (credit_exp > cost_imp + 1e-9) & (pe > 0)
        unlimited = risky & ~np.isfinite(pi) & ~np.isfinite(pe)
        if np.any(unlimited):
            t = grid.index[int(np.argmax(unlimited))] if grid.is_datetime else int(np.argmax(unlimited))
            raise ValidationError(
                f"{self.name}: export credit exceeds import cost in {int(unlimited.sum())} steps (first at {t}) and "
                "import and export are unlimited, so buying and selling at once would earn without bound; "
                "set max_import / max_export (and a max expansion), or make export pay less than import costs")
        if np.any(risky) and not self.exclusive:
            warnings.append(f"{self.name}: export credit exceeds import cost in {int(risky.sum())} steps; "
                            "simultaneous import/export arbitrage is possible - consider exclusive=True")
        return warnings

    def build(self, blk, F, ctx):
        imp, exp = F["import"], F["export"]
        pi, pe = self.limits()
        for d, flow in (("import", imp), ("export", exp)):
            if not self.expandable(d):
                continue
            x = pyo.Var(bounds=(0, getattr(self.r, f"expansion_max_{d}")))     # MW added to this direction
            blk.add_component(f"x_{d}", x)
            lim = self.existing(d)
            blk.add_component(f"cap_{d}", pyo.Constraint(
                range(ctx.n), rule=lambda b, t, f=flow, x=x, lim=lim: f[t] <= float(lim[t]) + x))
        if self.exclusive:      # with an expansion: relative to the most each direction may reach
            idx = [t for t in range(ctx.n) if 0 < pi[t] < np.inf and 0 < pe[t] < np.inf]
            blk.exclusive = pyo.Constraint(
                idx, rule=lambda b, t: imp[t] / float(pi[t]) + exp[t] / float(pe[t]) <= 1)
        blk.tariff = pyo.Block(range(len(self.r.all_tariffs)))
        self._tariff_costs = []
        for i, tariff in enumerate(self.r.all_tariffs):
            flow = imp if tariff.direction == "import" else exp
            self._tariff_costs.append(tariff.build(blk.tariff[i], flow, ctx))

    def cost_terms(self, blk, F, ctx):
        r = self.r
        out: dict = {}
        for terms in self._tariff_costs:
            for cat, expr in terms.items():
                out[cat] = out.get(cat, 0) + expr
        self._tariff_costs = []     # handed over: kept here they would tie the solved model to the component
        if ctx.tie_break and np.any(r.max_export > 0) and any(isinstance(t, EnergyCredit) for t in r.all_tariffs):
            # paid exports: importing and exporting the same MW at one price is otherwise an equally cheap optimum
            tb = np.full(ctx.n, ctx.tie_break)
            out[CostCategory.TIEBREAK] = ctx.wsum(tb, F["import"]) + ctx.wsum(tb, F["export"])
        for d in DIRECTIONS:
            x = getattr(blk, f"x_{d}", None)
            if x is not None:       # on the capacity added only: the existing connection is paid for already
                out[CostCategory.INVESTMENT] = out.get(CostCategory.INVESTMENT, 0) + getattr(r, f"capex_{d}") * x
                out[CostCategory.FIXED_OM] = (out.get(CostCategory.FIXED_OM, 0)
                                              + getattr(r, f"opex_{d}") * ctx.horizon.annuity * x)
            fee = getattr(r, f"{d}_capacity_fee")
            if fee:                 # on the direction's whole capacity, its expansion included
                cap = self.billed_capacity(d) + (0.0 if x is None else x)
                out[CostCategory.NETWORK] = out.get(CostCategory.NETWORK, 0) + fee * ctx.years() * cap
        return out

    def solved_sizes(self, blk):
        """The import and export capacity after the expansion of each direction that may be expanded."""
        out = {}
        for d in DIRECTIONS:
            x = getattr(blk, f"x_{d}", None)
            if x is not None:
                added = max(0.0, float(pyo.value(x)))
                setattr(self.r, f"expansion_{d}", added)
                out[d] = (self.billed_capacity(d) + added, "MW")
        return out

    def size_limits(self):
        out = {}
        for d in DIRECTIONS:
            if self.expandable(d):
                most = getattr(self.r, f"expansion_max_{d}")
                base = self.billed_capacity(d)
                out[d] = (base, None if most is None else base + most)
        return out

    def periodic(self, blk) -> dict:
        """Billing-period variables (peaks) for reporting: {name: indexed Var}."""
        return {f"peak_{i}": blk.tariff[i].pk for i, t in enumerate(self.r.all_tariffs)
                if isinstance(t, PeakCharge)}

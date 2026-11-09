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


@register_component(group="Grid", icon="grid", label="Grid connection")
@dataclass(eq=False)
class GridConnection(Component):
    """Import/export connection (electricity grid, gas supply, district heating).

    Ports: ``import`` (OUT, grid -> bus) and ``export`` (IN, bus -> grid).

    Costs come from ``tariffs`` plus the convenience arguments ``import_price``, ``import_fee``,
    ``export_price``, ``export_fee`` and ``peak_charge`` (all EUR/MWh or EUR/MW per period).
    ``max_import`` and ``max_export`` default to no limit (inf); set ``max_export=0`` for an import-only
    connection. Exports earn nothing unless an ``export_price`` (or another export credit) is given.
    ``co2_price`` (EUR/t) prices the connection's CO2 emissions in place of the system emission policy price.
    """

    bus: object = param(Kind.BUS)
    _: KW_ONLY
    tariffs: list = param(Kind.TARIFFS, default_factory=list, group="Tariffs")
    max_import: object = param(Kind.SERIES, float("inf"), unit="MW", series="power", min=0)
    max_export: object = param(Kind.SERIES, float("inf"), unit="MW", series="power", min=0)
    exclusive: bool = param(Kind.BOOL, False, help="Discourage simultaneous import and export (LP relaxation)")
    import_price: object = param(Kind.SERIES, None, unit="EUR/MWh", series="rate", connector=True, group="Tariffs")
    import_fee: object = param(Kind.SERIES, None, unit="EUR/MWh", series="rate", connector=True, group="Tariffs")
    export_price: object = param(Kind.SERIES, None, unit="EUR/MWh", series="rate", connector=True, group="Tariffs")
    export_fee: object = param(Kind.SERIES, None, unit="EUR/MWh", series="rate", connector=True, group="Tariffs")
    peak_charge: float = param(Kind.SCALAR, None, unit="EUR/MW/period", min=0, group="Tariffs")
    peak_period: str = param(Kind.PERIOD, "M", group="Tariffs")
    peak_measurement: str = param(Kind.ENUM, "step", choices=MEASUREMENTS, group="Tariffs")
    co2_price: object = param(Kind.SERIES, None, unit="EUR/t", series="rate", connector=True, group="Emissions",
                              help="CO2 price on this connection's emissions (needs a CO2 factor on the bus)")
    credit_export_emissions: bool = param(Kind.BOOL, False, group="Emissions",
                                          help="Credit avoided emissions on export with the import factor")

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

    def port_bounds(self):
        return {"import": (None, self.r.max_import), "export": (None, self.r.max_export)}

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
        for t in self.r.all_tariffs:
            warnings += [f"{self.name}: {w}" for w in t.check(grid)]
        if self.co2_price is not None and not any("co2" in g for g in self.default_emission_factors().values()) \
                and not any("co2" in dict(g) for g in self.emission_factors.values()):
            warnings.append(f"{self.name}: co2_price is set but the connection has no CO2 emission factor "
                            "(set one on the bus), so it costs nothing")
        if self.exclusive and (np.any(~np.isfinite(self.r.max_import)) or np.any(~np.isfinite(self.r.max_export))):
            raise ValidationError(f"{self.name}: exclusive=True requires finite max_import and max_export")
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
        risky = (credit_exp > cost_imp + 1e-9) & (self.r.max_export > 0)
        unlimited = risky & ~np.isfinite(self.r.max_import) & ~np.isfinite(self.r.max_export)
        if np.any(unlimited):
            t = grid.index[int(np.argmax(unlimited))] if grid.is_datetime else int(np.argmax(unlimited))
            raise ValidationError(
                f"{self.name}: export credit exceeds import cost in {int(unlimited.sum())} steps (first at {t}) and "
                "import and export are unlimited, so buying and selling at once would earn without bound; "
                "set max_import / max_export or make export pay less than import costs")
        if np.any(risky) and not self.exclusive:
            warnings.append(f"{self.name}: export credit exceeds import cost in {int(risky.sum())} steps; "
                            "simultaneous import/export arbitrage is possible - consider exclusive=True")
        return warnings

    def build(self, blk, F, ctx):
        imp, exp = F["import"], F["export"]
        if self.exclusive:
            pi, pe = self.r.max_import, self.r.max_export
            idx = [t for t in range(ctx.n) if pi[t] > 0 and pe[t] > 0]
            blk.exclusive = pyo.Constraint(
                idx, rule=lambda b, t: imp[t] / float(pi[t]) + exp[t] / float(pe[t]) <= 1)
        blk.tariff = pyo.Block(range(len(self.r.all_tariffs)))
        self._tariff_costs = []
        for i, tariff in enumerate(self.r.all_tariffs):
            flow = imp if tariff.direction == "import" else exp
            self._tariff_costs.append(tariff.build(blk.tariff[i], flow, ctx))

    def cost_terms(self, blk, F, ctx):
        out: dict = {}
        for terms in self._tariff_costs:
            for cat, expr in terms.items():
                out[cat] = out.get(cat, 0) + expr
        self._tariff_costs = []     # handed over: kept here they would tie the solved model to the component
        if ctx.tie_break and np.any(self.r.max_export > 0) and any(isinstance(t, EnergyCredit)
                                                                    for t in self.r.all_tariffs):
            # paid exports: importing and exporting the same MW at one price is otherwise an equally cheap optimum
            tb = np.full(ctx.n, ctx.tie_break)
            out[CostCategory.TIEBREAK] = ctx.wsum(tb, F["import"]) + ctx.wsum(tb, F["export"])
        return out

    def periodic(self, blk) -> dict:
        """Billing-period variables (peaks) for reporting: {name: indexed Var}."""
        return {f"peak_{i}": blk.tariff[i].pk for i, t in enumerate(self.r.all_tariffs)
                if isinstance(t, PeakCharge)}

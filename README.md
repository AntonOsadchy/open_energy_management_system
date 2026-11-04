# Open EMS

Linear multi-carrier dispatch model (energy management system) for industrial-site electrification studies.
It minimises total system cost over a time horizon (perfect foresight) across grid import/export with tariffs,
behind-the-meter production, storage, converters (heat pumps, e-boilers, chillers, boilers, CHP) and flexible
heating/cooling demand. **No assets are built in:** a site is assembled from generic components, any number of
each, on any number of carriers (electricity, heat at several temperature levels, cold, gas, …).

Built on Pyomo + HiGHS. Runs at 15-min, 30-min or hourly resolution from the same system definition.

## Install

```bash
conda create -n oems python=3.12 -y && conda activate oems
pip install -e ".[dev,ui]"
python examples/make_data.py                         # synthetic example data
python examples/02_site_electrification.py --days 14 # baseline vs electrified site
oems-app                                             # drag-and-drop UI on http://127.0.0.1:8765
```

## Tutorial: a basic site in six steps

This builds a small site step by step: grid and demand, then PV, a battery, and finally a heat pump with a
hot-water tank. The full script is [examples/00_tutorial.py](examples/00_tutorial.py)
(`python examples/00_tutorial.py`). It needs no data files.

**1. Time grid and inputs.** Every run is defined on a `TimeGrid`. Inputs can be scalars (constant) or pandas
Series on that grid, or on another 15-min/hourly grid, which is aligned automatically.

```python
import numpy as np, pandas as pd
import oems
from oems import presets as P

grid = oems.TimeGrid(start="2025-06-02", periods=48, freq="1h", tz="Europe/Copenhagen")  # two days, hourly
hour = grid.index.hour
price = pd.Series(np.where((hour >= 7) & (hour < 21), 120.0, 60.0), index=grid.index)    # EUR/MWh
pv_cf = pd.Series(np.clip(np.sin((hour - 6) / 14 * np.pi), 0, None), index=grid.index)   # capacity factor 0..1
heat_load = pd.Series(np.where((hour >= 6) & (hour < 18), 0.8, 0.3), index=grid.index)    # MW
```

**2. A bus, a grid connection and a demand.** A bus is a node with an energy balance; components attach to it.
The system has nothing in it until you add it.

```python
es = oems.EnergySystem(grid)
el = es.add_bus("el", oems.Carrier("electricity", kind="electricity"))
es.add(
    oems.GridConnection("dso", el, import_price=price, import_fee=40, export_price=price,
                        max_import=2, max_export=1),          # MW; max_export defaults to 0 (no export)
    oems.Demand("base_el", el, profile=0.5),                  # 0.5 MW constant
)
res = es.solve()
print(res.total_cost)                                         # 3240 EUR
```

You can check this by hand: 14 day hours × 2 days × 0.5 MW × (120 + 40) + 10 night hours × 2 days × 0.5 MW ×
(60 + 40) = 2240 + 1000 = 3240 €.

**3. Add rooftop PV.** `pv` is a preset: a `Producer` with output `capacity × cf`, curtailable by default.
Surplus goes to export at the spot price (no fee).

```python
es.add(P.pv("pv", el, capacity=1.0, cf=pv_cf))               # total cost -> 645 EUR
```

**4. Add a battery.** The optimiser charges it from midday PV and discharges it in the evening, when the price is
highest.

```python
es.add(P.battery("bess", el, e_max=2, p_max=1, eta_rt=0.9))  # 2 MWh / 1 MW -> 516 EUR
```

**5. Add a second carrier: heat.** A heat bus with a heat pump (a `Converter` from `el` to `heat`), a hot-water
tank and a process-heat demand. The heat pump draws `heat / COP` from the electricity bus. The tank lets the
heat pump run when electricity is cheap.

```python
heat = es.add_bus("heat", oems.Carrier("heat", kind="heat"))
es.add(
    P.heat_pump("hp", el, heat, cop=3.0, p_th_max=1.5),      # cop may also be a Series
    P.hot_water_tank("tank", heat, e_max=4, p_max=1, loss_per_h=0.005),
    oems.Demand("process_heat", heat, profile=heat_load),
)
res = es.solve()                                              # 1481 EUR, now also supplying 26.4 MWh of heat
```

**6. Read the results.** Everything comes straight from the model's variables and duals:

```python
print(res.summary())                 # status, objective, cost by category
res.costs                            # EUR per component and category (ENERGY, NETWORK, REVENUE, ...)
res.flow_matrix("el")                # MWh on every connection to the el bus
res.flows["hp"]                      # MW per step on each heat-pump port
res.states                           # battery/tank energy content and PV curtailment
res.kpis()                           # import/export, self-consumption, cycles, full-load hours, ...
res.shadow_prices                    # marginal price per bus and step: el 100/120, heat about el/COP
res.to_csv("out/tutorial")           # all tables as CSV
```

**Next steps:** run the same system at 15-min resolution with `es.solve(grid=oems.TimeGrid(..., freq="15min"))`;
add a CO₂ price with `es.set_emission_policy("co2", price=85)` and emission factors on the grid import; add a
peak charge with `peak_charge=...` on the `GridConnection`; or make the heat demand shiftable with
`FlexibleDemand`. [examples/02_site_electrification.py](examples/02_site_electrification.py) uses all of these.

**The same site in the UI** (`oems-app`): drag in two **Bus** blocks and set their carriers to electricity and heat.
Then add **GridConnection**, **Demand**, and the presets **PV**, **Battery**, **Heat pump** and **Heat storage**, plus
a second **Demand**. Wire each component's energy connector to its bus (grid and battery have one **⇄** connector
for both directions). Type the constants into the properties panel, or upload a CSV, drag its columns from
**Inputs** onto the canvas and wire them to `import_price`, `cf` and `profile`. Set the start date, number of days and resolution in the top bar, then press **Run**.

## Concepts

| Concept | Meaning |
|---|---|
| `Carrier(name, kind)` | Energy type; `kind` ∈ electricity, heat, cold, fuel, other. Optional `emission_factor` applied to grid imports of that carrier. |
| `Bus(name, carrier)` | Node with its own energy balance. Several buses may share a carrier (e.g. `heat_90C`, `heat_45C`). |
| Component | Node connected to buses through **ports**. Each port is one explicit flow variable `m.F[port, t] ≥ 0` (MW). |
| `TimeGrid` | Regular 15min / 30min / 1h grid (tz-aware, DST-safe), optional cost weights. |

Components (`oems.<Name>`), all usable any number of times:

| Component | Ports | Purpose |
|---|---|---|
| `GridConnection` | `import` OUT, `export` IN | Electricity grid, gas supply, district heating; tariffs, peak charges, limits |
| `Producer` | `out` OUT | PV, wind, waste heat (profile or capacity × cf), curtailable |
| `Demand` | `in` IN | Fixed demand |
| `FlexibleDemand` | `in` IN | Shiftable demand (deferral/advance limits, rebound, window) |
| `Sink` | `in` IN | Heat rejection / dump with cost |
| `Storage` | `charge` IN, `discharge` OUT | Battery, hot/cold tank (efficiencies, self-discharge per hour) |
| `Converter` | `in_<bus>` IN, `out_<bus>` OUT | Any linear conversion; time-varying coefficients (COP) |
| `ThermalZone` | `heat` IN, `cold` IN | 1R1C thermal mass kept in a temperature band (cold store, hall) |

Tariffs attach to a `GridConnection`: `EnergyCharge`, `EnergyCredit`, `PeakCharge` (billing period, window, metering
interval), `CapacityLimit` (hard/soft), `FixedCharge`. Every component also accepts `flow_costs={port: €/MWh}`,
`emission_factors={port: {"co2": t/MWh}}` and `bounds={port: (min, max)}`.
`oems.presets` returns configured generic components: `battery`, `hot_water_tank`, `heat_pump`, `e_boiler`,
`chiller`, `gas_boiler`, `chp`, `link`, `pv`.

**Units:** MW, MWh, €, €/MWh; t CO₂, t/MWh, €/t; °C, MWh/K, K/MW. Rates that describe dynamics are per hour
(self-discharge, ramp), so switching resolution needs no parameter changes.

## Example

```python
import pandas as pd, oems
from oems import presets as P

grid = oems.TimeGrid(start="2025-01-01", periods=7 * 96, freq="15min", tz="Europe/Copenhagen")
es = oems.EnergySystem(grid)
el   = es.add_bus("el",   oems.Carrier("electricity", kind="electricity"))
heat = es.add_bus("heat", oems.Carrier("heat", kind="heat"))

es.add(
    oems.GridConnection("dso", el, import_price=da_price, import_fee=net_fee, export_price=da_price,
                        max_import=3, max_export=1, peak_charge=4500, peak_measurement="15min",
                        emission_factors={"import": {"co2": grid_co2}}),
    P.pv("pv", el, capacity=0.8, cf=pv_cf),
    P.battery("bess", el, e_max=2, p_max=1, eta_rt=0.9),
    P.heat_pump("hp", el, heat, cop=cop_series, p_th_max=1.5),
    P.hot_water_tank("tes", heat, e_max=10, p_max=3, loss_per_h=0.003),
    oems.FlexibleDemand("process_heat", heat, baseline=heat_load, p_max=3, window="D", max_deferred=2),
    oems.Demand("base_el", el, profile=el_load),
)
es.set_emission_policy("co2", price=85)                     # €/t (scalar or series), optional cap=...
res = es.solve()                                            # or es.solve(grid=other_resolution_grid)
print(res.summary()); res.to_csv("out/run1")
```

Inputs may be scalars, lists or pandas Series at **any** supported resolution: series are aligned to the model
grid by meaning (prices/powers held or averaged, temperatures interpolated, masks any-ed). Partial coverage or
time-zone mismatches raise instead of silently shifting data. `es.validate().alignment` lists every resample.

### Results (read directly from model variables and duals)

`res.flows` (every edge, MW) · `res.edges` (tidy long table) · `res.flow_matrix(bus)` · `res.states`
(SOC, backlog, temperature, curtailment) · `res.balance(bus)` · `res.costs` (component × category) ·
`res.shadow_prices` (marginal €/MWh per bus and step) · `res.emissions` / `res.emissions_t` ·
`res.emission_shadow_price` (with a cap) · `res.kpis()` · `res.resample("1h")` · `res.to_csv(folder)`.

### Config files and command line

```python
from oems.config import save_config, from_config
save_config(es, "my_site")            # my_site/system.yaml + inputs.csv
es2, opts = from_config("my_site/system.yaml")
```
```bash
python -m oems run my_site/system.yaml --freq 15min --out results/
```

## Drag-and-drop UI

`oems-app` (or `python -m oems app`) starts a local server and opens the editor. New to it? Press
**🎓 Tutorial** in the top bar: a guide card walks you through building the tutorial site on the canvas, ticks
off each step as you do it, and has a **Do it for me** button on every step.

* drag **Bus** blocks (central nodes, coloured by carrier kind), components and presets from the palette;
* upload CSVs — every column is listed as an input in the **Inputs** section of the palette (with a sparkline);
  drag (or click) an input to place it as its own block with one connector;
* wire component energy outputs → bus `supply`, bus `withdraw` → component energy inputs, and inputs → the
  grey parameter connectors (prices, profiles, COP, ambient temperature …). Invalid wires (wrong carrier kind,
  input → bus, double wires) are rejected. Grid connections and storages have a single **⇄** connector: one wire
  carries both directions, and after a run it shows both (e.g. `import 19.2 · export 3.7 MWh`);
* wires can be pulled from either end: from a bus or input to a block, or from a block's own connector (energy
  input, ⇄, grey parameter) to a bus or input. You can drop anywhere on the target block; the matching connector is
  chosen (bus supply/withdraw, the block's free energy connector, or its only free parameter);
* edit parameters, tariffs, flow costs and emission factors in the properties panel; set start, days,
  resolution, time zone and CO₂ price/cap in the top bar;
* **Validate** marks blocks with errors/warnings; **Run** solves in the background, then shows MWh on every wire,
  cost/FLH/cycle badges on blocks, cost breakdown, KPIs, bus balances with marginal prices and per-component plots;
  **Download CSV** gives all result tables; **Export config** gives a file for `python -m oems run`.

Projects and uploaded data are stored in `./oems_workspace` (`--workspace` to change).

## Mathematical formulation (summary)

Sets: steps `t` with length `Δt` [h] and weight `w`; buses `b`; ports `p` with flow `F[p,t] ≥ 0`.

* **Bus balance**: `Σ_{p∈OUT(b)} F[p,t] − Σ_{p∈IN(b)} F[p,t] + φ⁺ − φ⁻ = 0` (slack only with `allow_slack`);
  dual / (w Δt) = marginal price.
* **Grid**: `F_imp ≤ P_imp_max`, `F_exp ≤ P_exp_max`; optional `F_imp/P_imp + F_exp/P_exp ≤ 1`.
  Peak: `pk[k] ≥ mean_{t∈h} F[t]` for metering intervals `h` in billing period `k` (and window); cost `rate·pk[k]`.
* **Producer**: `F_out + κ = P_avail` (curtailable) or `F_out = P_avail`.
* **Storage**: `e[t] = (1−σ)^{Δt} e[t−1] + (η_ch F_ch − F_dis/η_dis) Δt`, `soc_min E ≤ e ≤ soc_max E`, end condition.
* **Converter**: `F_i = a_i/a_ref · F_ref`, `F_o = b_o/a_ref · F_ref`; capacity bounds on a chosen port; ramp limits.
* **FlexibleDemand**: `s⁺[t] − s⁻[t] = s⁺[t−1] − s⁻[t−1] + (D − F)Δt + ρ·d Δt`, `d ≥ D − F`, bounds on `s⁺, s⁻`,
  `s⁺ = s⁻ = 0` at the end of every window.
* **ThermalZone**: `θ[t] = θ[t−1] + Δt/C · (F_heat − F_cold + G − (θ[t−1] − θ_amb)/R)`, `θ_min ≤ θ ≤ θ_max`
  (soft with a comfort penalty).
* **Emissions**: `E[c,g] = Σ_t w Δt Σ_{p∈c} ε_p,g F[p,t]`; price adds `Σ w Δt π ε F`; cap `Σ E ≤ cap`.
* **Objective**: `min Σ_{c,k} C[c,k]` — cost per component and category (ENERGY, NETWORK, TAX, PEAK, REVENUE,
  OPERATION, DEGRADATION, FLEXIBILITY, EMISSIONS, FIXED, TIEBREAK, SLACK) as explicit accounting variables.

A tiny `tie_break` cost (default 0.01 €/MWh) on storage throughput, converter activity and flexibility backlog
removes degenerate alternative optima; it is reported as TIEBREAK and excluded from `res.total_cost`.

## Limitations

Pure LP: no on/off states, minimum up/down times or start costs (`p_min > 0` means must-run). The `exclusive`
options are relaxations — they reduce but do not forbid simultaneous import/export or charge/discharge.
FlexibleDemand limits shifted energy, not delay time. Storage is one node (no stratification). With standing
losses, 15-min and hourly results differ slightly (energy charged early in an hour decays within that hour at
15-min resolution). A peak metered per 15 min cannot be resolved on an hourly grid (a warning is given).

## Tests

```bash
pytest              # unit + validation (hand-calculated golden cases) + integration + UI (133 tests)
pytest -m slow      # full-year performance tests
python tests/make_catalog.py   # regenerate tests/TESTS.md - the catalogue of every test and what it checks
```
Browser tests need `pip install playwright && python -m playwright install chromium` (otherwise skipped).

Measured on a laptop (example site, 5 buses, 13–17 components, full year):

| Resolution | Steps | Build | Solve (HiGHS simplex) | Peak RAM |
|---|---|---|---|---|
| 1h | 8 760 | 2–4 s | 14–24 s | 1.0 GB |
| 15min | 35 040 | 9–18 s | 90–190 s | 1.5 GB |

## Adding a component

```python
from dataclasses import KW_ONLY, dataclass
import pyomo.environ as pyo
from oems.components.base import Component, register_component
from oems.ports import Port, Direction
from oems.schema import Kind, param

@register_component(group="Producer", icon="sun")
@dataclass(eq=False)
class MyDevice(Component):
    bus: object = param(Kind.BUS)
    _: KW_ONLY
    p_max: object = param(Kind.SERIES, unit="MW", series="power", min=0, connector=True)

    def ports(self):
        return {"out": Port(self.r.bus, Direction.OUT)}

    def port_bounds(self):
        return {"out": (None, self.r.p_max)}

    def build(self, blk, F, ctx):
        ...  # add blk variables/constraints written on F["out"][t]
```
The `param` specs drive validation, config files and the UI form; registering adds it to the UI palette.

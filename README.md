# Microgrid Sizing Tool

Linear multi-carrier dispatch model (energy management system) for industrial-site electrification studies.
It minimises total system cost over a time horizon (perfect foresight) across grid import/export with tariffs,
behind-the-meter production, storage, converters (heat pumps, e-boilers, chillers, boilers, CHP) and flexible
heating/cooling demand. **No assets are built in:** a site is assembled from generic components, any number of
each, on any number of carriers (electricity, heat at several temperature levels, cold, gas, …).

Built on Pyomo + HiGHS. Runs at 15-min, 30-min or hourly resolution from the same system definition.

## Install

```bash
conda create -n oems python=3.12 -y && conda activate oems
pip install -e ".[dev,ui,db]"                        # db: optional, for the BESS IQ database blocks
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
                        max_import=2, max_export=1),          # MW; both default to no limit
    oems.Demand("base_el", el, profile=0.5),                  # 0.5 MW constant
)
res = es.solve()
print(res.total_cost)                                         # 3240 EUR
```

You can check this by hand: 14 day hours × 2 days × 0.5 MW × (120 + 40) + 10 night hours × 2 days × 0.5 MW ×
(60 + 40) = 2240 + 1000 = 3240 €.

**3. Add rooftop PV.** `pv` is a preset: a `Producer` with output `capacity × cf`, curtailable by default (the **Allow curtailment**
tick box in the UI; unticked, all available output must be used or exported).
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
**Inputs** onto the canvas and wire them to `import_price`, `cf` and `profile`. Drag **⚙️ Run settings** (palette *Settings*) onto the canvas, set the start date, number of days and resolution in its properties, then press **▶ Run**.

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
python -m oems run my_site/system.yaml --years 15          # base period repeated 15 years, with degradation
python -m oems run my_site/sizing.yaml                      # lifetime: {mode: optimise, ...}: optimal PV / BESS sizes
```

## Drag-and-drop UI

`oems-app` (or `python -m oems app`) starts a local server and opens the editor. New to it? Press
**Tutorial** at the bottom of the left rail: a guide card walks you through building the tutorial site on the canvas, ticks
off each step as you do it, and has a **Do it for me** button on every step.

* drag **Bus** blocks (central nodes, coloured by carrier kind), components and presets from the palette;
* upload CSVs — every column is listed as an input in the **Inputs** section of the palette (with a sparkline);
  drag (or click) an input to place it as its own block with one connector;
* pull inputs straight from the **BESS IQ database**: drop a **🔐 DB connection** block (host, port, user,
  password, SSL), press **Connect** and choose the database from the dropdown; then drop **🗄 DB input** blocks and
  pick the series: day-ahead price, generation, load, weather or commodities by zone, a grid tariff by zone and
  connection type, or a **solar PV profile**: the modelled output of a `solar_pv_scenarios` system divided by its MWp,
  i.e. a 0…1 capacity factor to wire into a PV block's `cf` (palette shortcut **☀️ PV profile**). Optional transforms: K → °C, normalise to 0…1 (e.g. national solar → capacity factor), scale. The
  series is fetched for the project's start/days/resolution when you validate or run, averaged or held onto the model
  grid (the database switched day-ahead prices from hourly to 15-min in Oct 2025) and cached; a horizon beyond the
  data is an error, not silently filled. Connections are read-only. The password is kept per login in
  `oems_workspace/db.json` (readable only by you), never in the project file; the BESS IQ `.env` login is offered as
  the default;
* wire component energy ports (right side of a block) to a bus, and inputs → the grey parameter connectors (left
  side: prices, profiles, COP, ambient temperature …). Ports have no direction on the canvas and a bus is connectable all
  around its outline (each wire joins the bus on the side facing its block): whether a wire is an inflow or an outflow comes from the component port (a demand's `in` takes energy from
  the bus, a PV's `out` feeds it, a converter's `in1` / `out1` follow its definition). Invalid wires (wrong carrier
  kind, input → bus, double wires) are rejected. Grid connections and storages have a single **⇄** port: one wire
  carries both directions, and after a run it shows both (e.g. `import 19.2 · export 3.7 MWh`). Projects saved with
  the older bus `supply` / `withdraw` connectors are converted when opened;
* wires can be pulled from either end: from a block's energy port or an input to a bus or block, or from a bus port
  or grey parameter to a block. You can drop anywhere on the target block; the matching connector is chosen (the
  bus port, the block's first free energy port of a fitting carrier, or its only free parameter);
* **⚙️ Run settings** (palette *Settings*, not wired, one per project) holds every setting of a run; **▶ Run** only
  starts it (without the block, ▶ Run adds it and shows it instead). Its properties: the run mode (*Single period*,
  *Multi-year*, *Lifetime optimisation*), the **Base period** (a calendar year - start and days follow it; *Custom*
  keeps your own start / days), **Years**, resolution, **time zone**, a solver time limit, the compute (this computer
  or the cloud), the **CO₂ cap**, the discount rate and inflation (multi-year and lifetime runs) and the lifetime
  optimisation's targets. The block on the canvas sums up the run. They are saved with the project. The time zone is the model's
  local time: naive CSV timestamps are read in it, days and months (peak charges, cycle limits, windows) follow it
  and DST days have 23 / 25 hours; choosing a BESS IQ zone sets it to the zone's time zone (empty = naive time);
* every PV (Producer) and storage block has a **Size** toggle in its settings: **Fixed size** (its own capacity /
  energy and power fields) or **Variable size**, chosen by a lifetime optimisation within its min/max size (and an
  optional storage duration), with its **CAPEX and OPEX** (required). Variable-size blocks show 🔍, and their own
  size fields read *variable size*. The ⚙️ Run settings block then switches to *Lifetime optimisation* (Years = the
  lifetime), lists the variable-size blocks and shows the investment hurdles; ▶ Run does not start while a CAPEX or
  OPEX is missing. The CO₂ cap (t) is for every run; the CO₂ price is an input of the grid block. The results get a **Sizing** tab: NPV, IRR, payback
  and discounted payback, CAPEX, the optimal sizes, a cash-flow chart and the yearly cash-flow table (see *Lifetime
  capacity optimisation* below); the sized blocks get a 🔍 size badge;
* in *Multi-year* mode the base period is repeated **Years** times and
  batteries wired to a **📉 Degradation curve** (palette, BESS IQ database section: SoH curves by cycling rate from
  `bess_degradation_curves`) lose capacity year by year (see *Multi-year runs* below; optional discount rate for an
  NPV). The results get a **Year**
  selector that switches the Summary, the canvas badges (incl. *SoH %*) and 🔍 Inspect to that year; Inspect also
  downloads every year at once (*All years: CSV*), Excel has a Lifetime sheet and the zip a `lifetime.csv`;
* storage blocks have a **Cycling** section: cycle cost and an optional cycle limit (*Max cycles* per day / week /
  month / year, a cycle = (energy stored + energy removed) / 2 / capacity, i.e. after charge/discharge losses);
  results report the cycles counted the same way;
* edit parameters, tariffs, flow costs and emission factors in the properties panel; the CO₂ cap, period and time
  zone are in the ⚙️ Run settings block;
* **Run** marks blocks with errors (and stops), otherwise solves in the background, then shows MWh on every wire,
  cost/FLH/cycle badges on blocks, cost breakdown, KPIs, bus balances with marginal prices and per-component plots;
  the **Time series** tab lists every time step: the flow on every wire (MW), storage states, each bus's marginal
  price and the input series the optimiser used (after alignment, with their source);
  **🔍 Inspect** (results bar) opens a window to explore them: pick series (search, quick picks for flows /
  states / prices / inputs; the selected block's series are preselected), plot them with zoom and range slider (€ on
  a second axis), read them in the table below (click the chart to jump to that step) and download the selection or
  everything as **CSV or Excel** (Excel adds a Summary sheet with costs and KPIs);
* **⚠ Both ways at once**: when a grid connection imports and exports, or a storage charges and discharges, in the
  same step (possible because `exclusive` is a relaxation, e.g. to burn energy at negative prices), the run says so:
  a notice in the Summary tab and the report, a ⚠ badge on the block, the KPIs `simultaneous_steps` /
  `simultaneous_energy`, and a `check … at once [MW]` series (= min of the two flows, highlighted in the table) that
  Inspect lists first and preselects;
* **📄 Report** (in the Inspect window) downloads a self-contained HTML report - the system as drawn with the
  energy on every wire, key inputs (settings, blocks and parameters, input series), key outputs (profit, costs, KPIs,
  energy per wire) and charts (bus flows, prices, state of charge, cumulative profit); open it in a browser and print
  to PDF. Saved simulations give the same report;
  the Inspect window also has **⤓ All tables (zip)** with every result table; **Export config** (in the ⚙️ Run settings block) gives a file for `python -m oems run`;
* **⤓ Download CSV / Excel** (in the results bar) downloads the data of the selected tab, for the year on show:
  Summary (profit, costs, KPIs) or
  Sizing (the yearly cash flows; Excel adds the sizes and the investment KPIs);
* **One set of results per project / variant**: when a run finishes (logged in, project named) its results are
  saved as that project's results, replacing the ones of its previous run: the canvas and settings as they were run,
  the solved config, the summary, the plot series and the result CSVs. The Project tab shows them as **Results**
  under the project or variant (🗑 deletes them), and opening the project reopens them without solving again, also
  after a server restart. A multi-year run keeps every year (summaries and per-step tables), so its Year selector
  still works;

* **Save** (⌘S / Ctrl+S, the dot after the project name, or the save button in the Project tab) and **Saved cases** keep projects ("cases": canvas + settings, plus the results of its last run, reopened with the project) per user. Both ask you to log in (or create an
  account) first; **Saved cases** shows one list of all your cases (name, last saved, horizon, blocks), each followed by its
  results (results of projects that are not saved come last), to open or delete; renaming a project takes its
  results along. Other users' cases are never listed. Accounts are kept in `oems_workspace/users.json` (salted PBKDF2
  hashes, owner-readable only), each user's cases in `oems_workspace/users/<user>/projects/`; the first account
  created takes over projects saved before logins existed.

Projects, saved simulations (`simulations/<id>/`) and uploaded data are stored in `./oems_workspace`
(`--workspace` to change). Simulations go through a storage interface (`oems.ui.simulations.SimulationStore`);
the local folder store is the only backend for now.

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

## Multi-year runs and battery degradation

`oems.degradation.simulate_lifetime(cfg, years)` (UI: *Years* > 1; CLI: `--years`) repeats the base period for
`years` consecutive years. The simulation is **myopic, year by year**: each year is an ordinary solve, so the battery
is dispatched for maximum profit and cycles as much as that pays (no degradation cost in the objective, unless you set
`cycle_cost` / `max_cycles`). After each year:

1. its cycles are counted: (charged + discharged) · Δt / 2 / **nominal** e_max (the convention of the curves);
2. the state of health at the end of that year is read from the battery's **SoH curves** (one curve per cycling rate,
   SoH after year 1, 2, …): at age *a* with *N* cycles since commissioning the average rate is N / (days · a), and
   SoH is interpolated linearly between the two curves that bracket that rate. A single curve is followed by age
   alone. A rate outside the curves uses the nearest curve (with a note); a curve that ends
   earlier than the others is continued along its last-year slope;
3. the next year runs with `e_max = nominal · SoH` (power limits unchanged).

All curves end at the **end-of-life SoH** (0.645 for the BESS IQ `standard` profile). The year in which SoH first
falls below it is the battery's last; from the next year on it has **SoH 0** - no capacity and no power - while the
rest of the site keeps running for the full simulation length. A battery without curves keeps 100 % SoH (or its
**Fixed SoH**, see below), and years
with the same capacities as an earlier year (no degradation, or after retirement) reuse its solution, so they cost no
extra solve. Simplification: each year's SOC limits use the start-of-year capacity (slightly optimistic within a year).
With a discount rate *r*, year *k*'s profit is weighted by 1/(1+r)^k (NPV). A 1 h base year of a small site solves in
about 7 s, so 20 years take a few minutes.

**Fixed SoH** (storage field in the *Degradation* group, `soh: 0.8`): instead of wiring SoH curves, give one state of
health. The usable energy is then SoH × `e_max` in every run and every year - single period, multi-year and the
lifetime optimisation (where CAPEX and OPEX stay on the nominal size, so sizing accounts for the derating); power
is unchanged and SoC is shown against the usable energy. A storage takes either curves or a fixed SoH, not both.

## Lifetime capacity optimisation (PV / battery sizing)

`oems.sizing.optimise_lifetime(cfg)` (UI: ⚙️ Run settings block + *Lifetime optimisation*; CLI: a config with
`lifetime: {mode: optimise, years: N, discount_rate: r, inflation: i}`) chooses the sizes of components with `optimise_size` -
PV capacity `capacity` (MW, availability = size × `cf`), storage energy `E` (MWh) and power `P` (MW, for charge and
discharge; optionally `E = duration × P`) - within their min/max, over the **whole lifetime in one LP**:

* with `model: full`, the base period is repeated N times back to back into one chronological horizon (every input series tiled, so a
  battery's state of charge carries over from one year into the next); 15 hourly years are 131,400 steps;
* each step's costs are weighted by `year_scale / (1 + r)^y` (year y = 1..N, discounted at the end of the year as in
  multi-year runs; `year_scale` = 1 for a calendar-year base period, else 365 / days, so a 7-day base period stands
  for a year). Charges per billing period (peak, fixed) are discounted with the year they fall in;
* **inflation** (optional, `inflation: i`; UI: *Inflation %/yr* in the ⚙️ Run settings block, next to the discount
  rate): every operating cost of year y - energy prices, tariffs, OPEX - is the base period's × (1 + i)^(y-1) (year 1
  at base-period prices; CAPEX is paid up front and not inflated), so the step weight becomes
  `year_scale · (1 + i)^(y-1) / (1 + r)^y`. A uniform price level does not change the dispatch, only how much later
  savings are worth against the CAPEX: inflation makes an investment more attractive. The cash flows, NPV, IRR and
  payback are then **nominal**, so use a nominal discount rate. Multi-year runs (degradation) take it from the same
  block and scale each year's profit the same way;
* **CAPEX** (`capex` €/MW, or `capex_energy` €/MWh + `capex_power` €/MW) is paid once in year 0 (category INVESTMENT),
  **OPEX** (`opex` €/MW/yr or €/MWh/yr) every year (FIXED_OM, × the annuity factor Σ (1+i)^(y-1)/(1+r)^y);
* the objective - the present value of all costs - is minimal exactly where the **NPV of the investment** is maximal,
  because the site's cost without the investment is a constant.

To report the investment, a **reference case** is solved as well: the same horizon with every optimised size at its
minimum (min sizes are what exists already, 0 by default). Or a **baseline** system of its own: in the UI choose
**Baseline** in the ⚙️ Run settings block (*Lifetime optimisation*) - another saved variant of the project, e.g.
`steel_factory` for `steel_factory_bess_pv`; it is run with the same settings (period, years, discount rate,
inflation, CO₂ cap) and its own variable sizes at their minimum. In a config: `lifetime.reference` (a config whose
`time` and `lifetime` sections are replaced by the sized one's) and optionally `lifetime.reference_name`. Per year: savings = operating cost of the reference -
operating cost with the optimal sizes; net cash flow = savings - OPEX (year 0: - CAPEX). From these: **NPV**
(= reference cost - optimised cost, cross-checked), **IRR** (the rate at which the NPV is 0), **payback** and
**discounted payback** (when the cumulative cash flow turns positive, interpolated within the year). A size at its
maximum is flagged: a larger limit may pay off more. An optimised size without a maximum can make the model
unbounded if the asset pays off without limit (e.g. PV exporting at a profit).

**Investment hurdles** (optional; Run settings block *Investment hurdles*, config
`lifetime.hurdles: {min_irr: 0.12, max_payback: 7, payback: simple|discounted}`): the optimiser then picks the sizes
with the highest NPV *among those that meet the targets*, usually smaller ones (the first MWh earn the most). Both
are one linear constraint on the same yearly cash flows, so the model stays an LP:

* IRR ≥ h ⇔ the NPV at rate h is ≥ 0;
* payback ≤ P ⇔ the cumulative (discounted) net cash flow at P years is ≥ 0 (interpolated within the year).

The savings are measured against the reference case, so with hurdles the reference is solved **first** and the sized
model second (in the cloud: one machine after the other instead of two at once). Investing nothing always meets the
hurdles, so the run cannot become infeasible: when no size meets them, the result is "no investment". The Sizing tab
shows each target next to the achieved value, and *binding* when the target limited the sizes. The hurdles apply to
the whole investment (all optimised assets together), as the reported IRR and payback do.

Capacities are constant over the lifetime: no degradation and no augmentation in the lifetime optimisation.

**Model: one weighted year (default) or every year.** Without degradation every modelled year is the same year
(same inputs, same sizes), only discounted differently. So by default (`lifetime.model: weighted`, ⚙️ Run settings
*Model: one year, weighted*) the base period is modelled **once**: its costs weighted by `year_scale` × the annuity
factor, its emissions by the number of years. It gives the same sizes, NPV, cash flows, IRR, payback and hurdles as
stacking all years, N times smaller. On steel_factory_1, 20 hourly years: 21 s on a laptop instead of 175,200
steps and over an hour in the cloud, with sizes within 0.2 % and NPV within 0.25 %. The only simplification:
each year's storage level ends where it began (the storage's end condition) instead of carrying over. Choose
*every year in sequence* (`model: full`) for the full chronology (e.g. for degradation later); there a short base
period shares months across the stacked years.

## Cloud runs (Modal)

Big runs can be solved on a cloud machine while the app stays on your computer: in the ⚙️ Run settings block choose
**Compute: Cloud (Modal)** (single-period runs and lifetime optimisations; multi-year runs stay local). A machine
with 16 GB RAM and 2 cores (`oems.cloud.MEMORY_MB`, `CPUS`; the run message shows its peak) starts for the run, solves it with the same `oems` code and stops; billing is
per second. A lifetime optimisation sends its two models - the optimal sizes and the reference case without the
investment - to two machines at once, so it takes as long as the slower one (locally, and with investment hurdles,
they run one after the other). Only the run's config and the input series it uses go up (BESS IQ inputs are fetched locally first, so
database logins stay on your computer); projects, accounts and saved simulations stay local. The ⚙️ Run settings block estimates the
memory a run needs (about 30 kB per time step) and suggests the cloud when it is near this computer's RAM.

Set up once, in the app's Python environment:

```bash
pip install -e ".[cloud]"      # Modal
modal token new                # opens the browser to log in; stores ~/.modal.toml
```

The first cloud run builds the worker image (numpy, pandas, Pyomo and HiGHS pinned to your local versions, plus the
`oems` source), which takes a few minutes; later runs start in well under a minute. Code changes in `oems` are picked
up on the next run. From Python: `oems.cloud.solve_remote(cfg)`.

## Cross-check against BESS IQ

A full 2025 run of a stand-alone 1 MW / 2 MWh battery in DK_1 (round-trip efficiency 0.9, start at 50 % SOC, hourly
day-ahead prices, no tariffs, `max_cycles=365, cycle_period="Y"`) reproduces the BESS IQ simulation of the same case:
74,919 EUR vs 73,985 EUR profit (+1.3 %), the same energy charged (768.4 MWh), the same charge/discharge direction in
99.4 % of the hours (net power correlation 0.989). BESS IQ lets capacity fade within the year (2 -> 1.904 MWh); the
EMS keeps 2 MWh, which accounts for the small remaining difference. Without the cycle limit the EMS cycles about twice
a day (+13 % profit) - set `max_cycles` on the storage to compare like with like. That run predates counting
cycles on stored energy (BESS IQ counts terminal-side charged + discharged energy); with the current definition the
same `max_cycles` is slightly looser, so expect a marginally higher profit.

## Limitations

Pure LP: no on/off states, minimum up/down times or start costs (`p_min > 0` means must-run). The `exclusive`
options are relaxations — they reduce but do not forbid simultaneous import/export or charge/discharge (results flag
every step where it happens).
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

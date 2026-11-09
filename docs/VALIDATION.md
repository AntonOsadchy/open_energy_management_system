# Independent validation of Open EMS results

This guide is for someone, human or agent, who receives the results of one Open EMS run and has to check them
without trusting the model code. It explains what the model optimises, what each output file contains, and which
checks a correct result must pass.

## 1. The model in short

Open EMS is a **linear program (LP)** that dispatches an energy site at minimum total cost over a fixed horizon,
with **perfect foresight**: the whole horizon is solved at once, so the optimiser knows every future price.
The solver is HiGHS.

- **Time.** Steps t = 0…N−1 at 15 min or 1 h; `step_h` (Δt, hours) is given per step. Timestamps are
  timezone-aware and mark the *start* of each step. Daylight-saving days have 23 or 25 hours.
- **Buses** are nodes of a single carrier: electricity, heat, cold or gas. Every bus must balance in every step:
  `Σ flows into the bus = Σ flows out of the bus`.
- **Components** connect to buses through **ports**. Every port carries a non-negative flow in MW, the average
  power over the step. Energy in a step is `MW × step_h`.

| Component | Ports (direction relative to the bus) | Physics |
|---|---|---|
| GridConnection | `import` (into bus), `export` (out of bus) | `0 ≤ import ≤ max_import`, `0 ≤ export ≤ max_export`; both limits are unlimited by default |
| Producer (e.g. PV) | `out` | `out + curtailment = available profile` (when curtailable) |
| Demand | `in` | `in = profile` (fixed) |
| FlexibleDemand | `in` | Can shift energy within a window (deferred `s_plus`, advanced `s_minus`) |
| Storage | `charge` (out of bus), `discharge` (into bus) | `e[t] = (1−σ)^Δt·e[t−1] + (η_ch·charge − discharge/η_dis)·Δt`, with `e[−1] = soc_init·e_max` |
| Converter (heat pump, boiler…) | `in_<bus>`, `out_<bus>` | Fixed ratios to a reference port, e.g. heat = COP · electricity |
| ThermalZone | `heat`, `cold` | 1R1C temperature model `theta` |

**Storage details**
- **Defaults** (when absent from `config.json`): `eta_ch = eta_dis = 0.95`, `self_discharge = 0` (σ, per hour),
  `soc_min = 0`, `soc_max = 1`, `soc_init = 0.5`, `end = cyclic_geq`, `cycle_cost = 0`, no `max_cycles`.
- **End condition** (`end`):
  - `cyclic_geq` (default): `e[N−1] ≥ soc_init·e_max`.
  - `cyclic_eq`: the same with `=`.
  - `fixed`: `e[N−1] = soc_end·e_max`.
  - `free`: no end condition.
- **Cycle limit:** with `max_cycles` set, every calendar `cycle_period` (D/W/M/Y, local time) must satisfy
  `Σ (eta_ch·charge + discharge/eta_dis)·Δt / 2 ≤ max_cycles · e_max` (cycles counted on stored energy).
- **Cycle cost:** `cycle_cost` (€/MWh) is charged on discharged energy.

**Objective**

minimise `Σ costs` over components and categories. Every money amount is in EUR:

| Category | Source |
|---|---|
| ENERGY | `import_price × import energy` |
| NETWORK | `import_fee × import` + `export_fee × export` |
| TAX | tax tariffs, if configured |
| REVENUE | `−export_price × export energy` (negative cost) |
| PEAK | `peak_charge × max import` per billing period (and capacity overrun) |
| FIXED | fixed charge per billing period |
| OPERATION, DEGRADATION, FLEXIBILITY, EMISSIONS | marginal cost, `cycle_cost`, deviation cost and CO₂ price |
| TIEBREAK | 0.01 €/MWh on storage, converter and some grid flows. It only removes ties between equally good solutions and is **excluded** from `total_cost` |
| SLACK | penalty for an unbalanced bus. It is **excluded** from `total_cost` and **must be 0** in a valid run |

`total_cost` = the sum of all categories except TIEBREAK and SLACK. **System profit = −total_cost.**

**Known simplifications** (not errors)
- Linear only: there is no on/off state, minimum up-time or start cost.
- `exclusive` (no simultaneous charge and discharge, or import and export) is a relaxation, not a hard binary.
- Perfect foresight, so results are an upper bound on what a real controller would achieve.
- Storage capacity does not fade over time.

## 2. Output files

The output comes from **🔍 Inspect → ⤓ All tables (zip)**.

### `timeseries.csv` — one row per time step (the main file to check)
- `time`: start of the step, local time with UTC offset.
- `step_h`: Δt in hours.

The columns are grouped by prefix:

| Prefix | Example | Meaning |
|---|---|---|
| `flow` | `flow grid.import → el [MW]`, `flow el → bess.charge [MW]` | Port flow in MW. `A → B` gives the direction of energy |
| `state` | `state bess.e [MWh]`, `state bess.soc [0-1]` | Value at the **end** of the step. `soc = e / e_max` |
| `price` | `price el [EUR/MWh]` | **Marginal price of the bus**: the cost of supplying one more MWh to that bus in that step (LP dual) |
| `econ` | `econ revenue [EUR]`, `econ cost ENERGY [EUR]`, `econ profit [EUR]`, `econ profit cumulative [EUR]` | Money **per step**, read from the optimiser's own cost terms |
| `input` | `input grid.import_price [EUR/MWh] (DK_1 day-ahead)` | Time-varying inputs exactly as the optimiser used them, after alignment to the model grid. Scalar inputs are in `config.json` |

Other states:
- `curtailment` (MW, producer);
- `s_plus` / `s_minus` (MWh, flexible demand backlog);
- `theta` (°C, thermal zone).

### Other tables in the zip
| File | Content |
|---|---|
| `config.json` | The solved system: `time` (`start`, `freq`, `tz`, `periods`), `carriers`, `buses`, and `components` with every parameter that differs from its default (defaults are listed in §1 and in the app's properties panel). A series reference looks like `{"series": "<dataset>:<column>"}`; its values are in the matching `input` column (`datasets` points to local files that are not in the zip) |
| `flows.csv` | `timeseries` flows in long format: time, component, port, bus, carrier, direction (`out` = into the bus), MW, MWh |
| `states.csv` | time, component, state, value |
| `costs.csv` | component, category, EUR: horizon totals per component and category, straight from the optimiser |
| `periodic.csv` | Billing-period variables, e.g. the billed peak (MW) per month |
| `kpis.csv` | kpi, scope, value, unit (see below) |
| `shadow_prices.csv` | The `price` columns |
| `slack.csv` | Unbalanced energy per bus and time; must be empty |
| `metadata.json` | status, objective, total_cost, steps, resolution, start, end, warnings |
| `emissions.csv`, `emissions_t.csv` | Tonnes per component and gas, and per step |

### KPIs (`kpis.csv`)
| KPI | Definition |
|---|---|
| `import_energy`, `export_energy` | Σ flow·Δt on the grid ports (MWh) |
| `max_import` | Highest import flow (MW) |
| `max_billed_peak` | Highest value in `periodic.csv` (MW) |
| `production`, `curtailment` | Σ producer output and curtailment (MWh) |
| `discharged` | Σ discharge·Δt (MWh) |
| `equivalent_cycles` | discharged / e_max |
| `cycles_throughput` | Σ (eta_ch·charge + discharge/eta_dis)·Δt / 2 / e_max, counted on stored energy (after efficiency losses). `max_cycles` uses it too |
| `max_cycles_per_<day/week/month/year>` | Busiest calendar period by the same measure. Must be ≤ `max_cycles` when that is set |
| `soc_min`, `soc_mean`, `soc_max`, `soc_end` | Statistics of `state <storage>.soc`, in % |
| `full_load_hours` | Converter energy on its capacity port / p_max |
| `self_consumption`, `self_sufficiency` | Electricity: (production − export) / production, and the same / (that + import) |
| `avg_marginal_price` | Bus price weighted by withdrawals |
| `total_cost` | EUR, the same value as in `metadata.json` |

## 3. Checks a valid result must pass

Use these tolerances: 1e-6 relative for money and energy totals, and 1e-6 MW for flows. The solver works to
about 1e-7, so tiny negative values such as −1e-9 are noise.

**Feasibility**
1. `metadata.json` status is `optimal`; `slack.csv` is empty; no SLACK row in `costs.csv`.
2. Every flow ≥ 0. Every flow respects its bounds from `config.json` and the `input` columns: `max_import`,
   `max_export`, `p_ch_max`, `p_dis_max`, the converter `p_max`, and so on.
3. **Bus balance** in every step: for each bus, the flows with `… → bus` minus the flows with `bus → …` sum to 0.
4. **Fixed demand**: `flow bus → demand.in` equals the demand `input` profile.
5. **Producer**: output + curtailment = the available profile, or the profile × capacity for a PV profile
   normalised per MWp.

**Storage**
6. Recompute `e[t]` from the recursion in §1, using `soc_init`, `eta_ch`, `eta_dis`, `self_discharge` and
   `step_h`. It must equal `state <storage>.e`.
7. `soc_min ≤ soc ≤ soc_max` in every step, and the end condition holds at the last step.
8. If `max_cycles` is set, group (eta_ch·charge + discharge/eta_dis)·Δt/2/e_max by calendar period in **local time**. Each
   period must stay ≤ `max_cycles`.
9. Simultaneous charge and discharge (and simultaneous import and export) should be ~0. When the price is
   negative it can be rational, unless `exclusive` is set.

**Converters**
10. Port ratios hold in every step, e.g. `out_heat = COP(t) · in_el`.

**Economics**
11. Recompute money from flows and inputs. For each step: `econ cost ENERGY = import_price · import · Δt`;
    `econ revenue = export_price · export · Δt`; `econ cost NETWORK = fees · flow · Δt`;
    `econ cost DEGRADATION = cycle_cost · discharge · Δt`.
12. `econ profit = revenue − Σ econ cost …`, and `econ profit cumulative` is its running sum.
13. Σ `econ profit` over all steps − (PEAK + FIXED charges in `costs.csv`) = −`total_cost`. Peak and fixed
    charges are billed per period, so they are not in the step columns.
14. `costs.csv`, summed per category, equals the recomputed totals. `total_cost` excludes TIEBREAK, and
    `metadata.objective` ≈ total_cost + TIEBREAK.
15. **Peak:** for each billing period, `periodic.csv` peak = max import in that period (for the `step`
    measurement), and PEAK cost = Σ peak_charge × peak.

**Optimality (plausibility, not proof)**
16. On a bus fed by an unconstrained grid, `price` ≈ import_price + import_fee (± the 0.01 tie-break).
    When export is the marginal option, it is ≈ export_price − export_fee.
17. A battery should charge in low-price steps and discharge in high-price steps. Over a cycle, the discharge
    price should exceed the charge price / (η_ch·η_dis) plus cycle_cost. Steps where it does not are worth
    flagging.
18. A quick optimality check: re-solving with a constraint relaxed (e.g. more `e_max` or more cycles) must never
    give a higher cost.

**Units and time**
- MW, MWh, EUR, EUR/MWh; SOC is 0–1 in `timeseries` and % in `kpis`.
- Periods for days, months and cycles follow local time (`time.tz` in `config.json`).

## 4. Python starting point

```python
import json, zipfile, pandas as pd
z = zipfile.ZipFile("results.zip")
ts = pd.read_csv(z.open("timeseries.csv"), index_col="time")
cfg = json.load(z.open("config.json")); meta = json.load(z.open("metadata.json"))
costs = pd.read_csv(z.open("costs.csv"))
dt = ts["step_h"]
flow = ts.filter(like="flow ")
# bus balance per bus
for bus in [b["name"] for b in cfg["buses"]]:
    into = flow[[c for c in flow if c.split(" [")[0].endswith(f"→ {bus}")]].sum(axis=1)
    out = flow[[c for c in flow if c.startswith(f"flow {bus} →")]].sum(axis=1)
    print(bus, "max imbalance", (into - out).abs().max())
# profit identity
period = costs[costs.category.isin(["PEAK", "FIXED"])].EUR.sum()
print(ts["econ profit [EUR]"].sum() - period, -meta["total_cost"])
```

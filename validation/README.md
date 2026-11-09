# Independent validation of an Open EMS run: test_4_full_system

This folder lets someone, human or agent, check the results of one Open EMS run without trusting the model code.
It contains:

| File | Content |
|---|---|
| `README.md` | This file: the model, the run, the meaning of every output, and the checks to run |
| `test_4_full_system.xlsx` | The results and every input needed to validate them |
| `KPIs.xlsx` | Key run KPIs: a summary with formulas, the model's own KPI table, and the run status |

## 1. The run

| | |
|---|---|
| Horizon | 2025-01-01 to 2025-12-31 23:00, 8,760 steps of 1 h, time zone Europe/Copenhagen |
| System | One electricity bus `el` with four blocks, listed below |
| Inputs | DK_1 day-ahead price (import and export price), DK_1 n1_a_high consumption tariff (import fee), DK_1 n1_a_high feed-in tariff (export fee), and the DK1 PV capacity factor |

**Blocks on the `el` bus:**
- `grid`: import and export each ≤ 2 MW, `exclusive`.
- `demand`: flat 1 MW.
- `store`: 2 MWh, 1 MW charge and discharge.
- `prod`: PV profile.

**PV size:** the PV profile is the capacity factor with no capacity set, so the plant acts as about 1 MWp.

**Not in this run:** peak or fixed charges, emissions, and cycle limits.

## 2. The model in short

Open EMS is a **linear program (LP)** that dispatches an energy site at minimum total cost over a fixed horizon,
with **perfect foresight**: the whole horizon is solved at once, so the optimiser knows every future price.

- **Time.** `step_h` is Δt in hours. Timestamps mark the *start* of each step.
- **Buses** are nodes of a single carrier. Every bus must balance in every step:
  `Σ flows into the bus = Σ flows out of the bus`.
- **Blocks** connect to buses through **ports**. Each port has a fixed direction, either into the bus or out of it,
  and carries a non-negative flow in MW (the average power over the step). Energy in a step = `MW × step_h`.
  A block that can both deliver and take energy has two separate ports: the grid has `import` and `export`, and
  storage has `discharge` and `charge`. Net flow is the difference between the two, e.g. net import = `import − export`.

| Block | Ports | Constraints |
|---|---|---|
| GridConnection | `import` (into bus), `export` (out of bus) | `0 ≤ import ≤ max_import`, `0 ≤ export ≤ max_export` |
| Producer | `out` | `out + curtailment = profile` (curtailable) |
| Demand | `in` | `in = profile` (fixed) |
| Storage | `charge` (out of bus), `discharge` (into bus) | See the equations below |

**Storage equations**
- **Charge and discharge limits:** `0 ≤ charge ≤ p_ch_max`, `0 ≤ discharge ≤ p_dis_max`.
- **Energy balance:** `e[t] = (1−σ)^Δt·e[t−1] + (η_ch·charge − discharge/η_dis)·Δt`.
- **Start:** `e[−1] = soc_init·e_max`.
- **Bounds:** `soc_min·e_max ≤ e[t] ≤ soc_max·e_max`.
- **End condition** `cyclic_geq`: `e[last] ≥ soc_init·e_max`.

**`exclusive`**
- On the grid: `import/max_import + export/max_export ≤ 1`.
- On storage (off in this run): the same for charge and discharge.
- It is a linear relaxation, not an on/off binary.

**Objective:** minimise the sum of costs (EUR) over blocks and categories.

| Category | Formula (this run) |
|---|---|
| ENERGY | Σ import_price · import · Δt |
| NETWORK | Σ (import_fee · import + export_fee · export) · Δt |
| REVENUE | −Σ export_price · export · Δt (negative cost) |
| TIEBREAK | 0.01 EUR/MWh on grid import and export and on storage charge and discharge. It only breaks ties between equally good solutions and is **excluded** from total cost |

Other categories exist: PEAK, FIXED, TAX, OPERATION, DEGRADATION, FLEXIBILITY, EMISSIONS and SLACK. All are zero
here.

`total cost` = the sum of all categories except TIEBREAK. **System profit = −total cost.**

**Known simplifications (not errors)**
- Perfect foresight.
- No on/off states.
- No battery capacity fade.

## 3. `test_4_full_system.xlsx`

**Sheet `Parameters`:** every block parameter as solved, including defaults, with units. A time-varying parameter
names the `Time series` column that holds its values.

**Sheet `Time series`:** one row per hour.

| Column / prefix | Meaning |
|---|---|
| `time (Europe/Copenhagen, step start)`, `time UTC` | Start of the step: local time (no offset) and UTC |
| `step_h` | Δt in hours. 1 h every step; the DST days have 23 or 25 rows |
| `flow A → B [MW]` | Port flow; the arrow gives the direction of energy, e.g. `flow el → store.charge`, `flow grid.import → el` |
| `state store.e [MWh]`, `state store.soc [0-1]` | Stored energy and SOC at the **end** of the step (`soc = e / e_max`) |
| `state prod.curtailment [MW]` | PV available but not used |
| `price el [EUR/MWh]` | **Marginal price of the bus**: what one more MWh on the bus would cost in that step (LP dual) |
| `econ revenue`, `econ cost ENERGY`, `econ cost NETWORK [EUR]` | Money per step, read from the optimiser's own cost terms (revenue positive) |
| `econ profit [EUR]`, `econ profit cumulative [EUR]` | revenue − costs per step, and its running sum |
| `input … (source)` | The input series exactly as the optimiser used them, after alignment to the hourly grid |
| `ref cost without battery [EUR]` | **Not a model output.** A hand-computed benchmark: the cheapest cost of each hour with no battery |

**Sheet `Costs`:** the optimiser's totals per block and category (EUR).

## 4. `KPIs.xlsx`

**Sheet `Summary`:** the key run KPIs. The `how / note` column gives each formula, so every number can be
recomputed from the `Time series` sheet.

**Sheet `Model KPIs`:** the model's own KPI table.

| KPI | Definition |
|---|---|
| `import_energy`, `export_energy` | Σ flow · Δt on the grid ports (MWh) |
| `max_import` | Highest import flow (MW) |
| `discharged` | Σ discharge · Δt (MWh) |
| `equivalent_cycles` | discharged / e_max |
| `cycles_throughput` | Σ (eta_ch · charge + discharge / eta_dis) · Δt / 2 / e_max, i.e. counted on the energy actually stored in and removed from the battery, after efficiency losses |
| `max_cycles_per_day` | Busiest local calendar day by the same measure |
| `soc_min`, `soc_mean`, `soc_max`, `soc_end` | Statistics of the SOC, in % |
| `production`, `curtailment` | PV used and curtailed (MWh) |
| `self_consumption` | (PV − export) / PV |
| `self_sufficiency` | (PV − export) / ((PV − export) + import) |
| `delivered_energy` | Energy supplied to demand (MWh) |
| `avg_marginal_price` | Bus price weighted by withdrawals |

**Sheet `Run`:** status, objective, total cost, horizon, slack and warnings.

## 5. Checks a valid result must pass

Use these tolerances: 1e-6 relative for totals, and 1e-6 MW for flows. Values such as −1e-9 are solver noise.

**Feasibility**
1. Status is optimal and slack is none (`KPIs.xlsx` → Run).
2. Every flow ≥ 0, and the limits from `Parameters` hold: import ≤ 2, export ≤ 2, charge ≤ 1, discharge ≤ 1 MW.
3. **Bus balance** every hour. Inflows: `grid.import`, `store.discharge`, `prod.out`. Outflows: `grid.export`,
   `store.charge`, `demand.in`. The two must be equal.
4. `flow el → demand.in` = 1 MW every hour.
5. `flow prod.out → el` + `state prod.curtailment` = the PV `input` column.
6. Grid `exclusive`: `import/2 + export/2 ≤ 1`.

**Storage**
7. Recompute `e[t]` with the recursion in §2: e[−1] = 1 MWh, η 0.95/0.95, σ = 0. It must match `state store.e`.
8. 0 ≤ SOC ≤ 1 every hour, and the last-hour `e` ≥ 1 MWh.
9. Charging and discharging in the same hour is expected only when wasting energy pays off, i.e. when day-ahead
   price + import fee < 0.

**Economics**
10. Per step:
    - `econ cost ENERGY = import_price · import · Δt`;
    - `econ revenue = export_price · export · Δt`;
    - `econ cost NETWORK = (import_fee · import + export_fee · export) · Δt`.
11. `econ profit = revenue − Σ econ cost`. Its sum over the year = −total cost, because this run has no
    period charges.
12. The category sums of `Costs` equal the recomputed totals. Objective = total cost + TIEBREAK.

**Units and time:** MW, MWh, EUR, EUR/MWh. SOC is 0–1 in `Time series` and % in `Model KPIs`. Days are counted
in local time.

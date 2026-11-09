# Test 09: KPIs recomputed from the hourly results

| | |
|---|---|
| Run | `test_4_full_system` (2025-01-01 to 2025-12-31 23:00, 8,760 × 1 h, Europe/Copenhagen) |
| Date of check | 2026-11-05 |
| Data used | `test_4_full_system.xlsx` (`Parameters`, `Time series`) for every calculation. `KPIs.xlsx` (`Model KPIs`, `Summary`) only for the reported figures under test. |
| Result | **51 / 55 PASS. The 4 failures are one definition mismatch:** cycle throughput is computed differently from the README definition |
| Tolerance | 1e-6 relative |

## 1. Question

Does every KPI in `KPIs.xlsx` match the same figure recomputed from the hourly data?

## 2. Method

Each KPI is recomputed from `Time series` using the definitions in README §4 (`Model KPIs`) and the `how / note` column of `Summary`. The values compared are:
- all 18 `Model KPIs`;
- 37 numeric or text items in `Summary`.

Section headings and the run status are not KPIs and were skipped; the status is covered by README check 1.

## 3. Results

### 3.1 Model KPIs (18)

| KPI | Reported | Recomputed | Result |
|---|---:|---:|:---:|
| import_energy [MWh] | 6,777.615896 | 6,777.615896 | PASS |
| export_energy [MWh] | 35.707775 | 35.707775 | PASS |
| max_import [MW] | 2.000000 | 2.000000 | PASS |
| discharged [MWh] | 1,224.396633 | 1,224.396633 | PASS |
| equivalent_cycles | 612.198316 | 612.198316 | PASS |
| **cycles_throughput** | **645.267201** | **644.419280** | **FAIL (definition)** |
| **max_cycles_per_day** | **2.853750** | **2.850000** | **FAIL (definition)** |
| soc_min / mean / max / end [%] | 0 / 42.238448 / 100 / 50 | 0 / 42.238448 / 100 / 50 | PASS |
| production [MWh] | 2,150.367416 | 2,150.367416 | PASS |
| curtailment [MWh] | 59.681629 | 59.681629 | PASS |
| self_consumption | 0.983395 | 0.983395 | PASS |
| self_sufficiency | 0.237809 | 0.237809 | PASS |
| delivered_energy [MWh] | 8,760.000000 | 8,760.000000 | PASS |
| total_cost [EUR] | 598,823.125090 | 598,823.125090 | PASS |
| avg_marginal_price [EUR/MWh] | 88.288439 | 88.288439 | PASS |

All PASS items agree to within 2e-15 relative, which is floating-point rounding.

### 3.2 Summary (37)

| Group | Items | Result |
|---|---|:---:|
| Economics | Total cost, Energy, Network, Export revenue, Tie-break, Solver objective, Average cost of supplied demand (68.36 EUR/MWh), Reference cost without battery, Value of the battery (83,824.80 EUR) | 9 / 9 PASS |
| Energy balance | Demand, PV available / used / curtailed, Grid import / export, Battery charged / discharged / losses | 10 / 10 PASS |
| Grid | Max import, Max export, Hours importing and exporting at once, Avg DA price (time-weighted, on import, on export) | 6 / 6 PASS |
| Battery | Equivalent full cycles, Avg price and import fee when charging and when discharging, Hours charging / discharging / idle, Hours charging and discharging at once, SOC min / mean / max / end | 9 / 9 PASS |
| Battery, cycle throughput | **Cycles, throughput** (645.267201 vs 644.419280); **Max cycles in a day** (2.85375 vs 2.85000) | **0 / 2 (definition)** |
| Other | Hours with negative DA price, Self-consumption, Self-sufficiency | 3 / 3 PASS |

"Reference cost without battery" and "Value of the battery" match the summed `ref cost without battery` column. This test does not check how that column was calculated; that is a separate test.

### 3.3 The cycle-throughput mismatch

Two definitions of cycle throughput appear in the documentation:

| Source | Definition | Counts energy |
|---|---|---|
| README §4 (`cycles_throughput`) | Σ (η_ch · charge + discharge / η_dis) · Δt / 2 / e_max | inside the battery, after efficiency losses |
| `KPIs.xlsx` → `Summary` note | (charge + discharge) / 2 / e_max | at the bus, before losses |
| `Parameters` → `max_cycles` note | (charged + discharged) / 2 / e_max | at the bus, before losses |

| Definition | Cycles, year | Max in a day |
|---|---:|---:|
| After losses (README) | 644.419280 | 2.850000 |
| Before losses (Summary, Parameters) | **645.267201** | **2.853750** |
| Reported | 645.267201 | 2.853750 |

The reported values match the **before-losses** definition exactly. The values themselves are correct; the README definition is out of step with what the model reports. The gap is small (0.85 cycles a year, +0.13 %): charging losses (67.83 MWh) and discharging losses (64.44 MWh) nearly cancel.

## 4. Conclusion

**51 / 55 PASS.** Every energy, cost, price, SOC and hours KPI in `KPIs.xlsx` matches the hourly data to floating-point precision.

The 4 failures are one documentation inconsistency, reported in two sheets: `cycles_throughput` and `max_cycles_per_day` use the before-losses definition, (charge + discharge) / 2 / e_max, while README §4 describes the after-losses definition.

**Recommended fix:** choose one definition and use it in the README, the `Summary` note, the `Parameters` note and the code. This matters when a `max_cycles` limit is set: the definition decides how many cycles the battery is allowed. With the before-losses definition, the limit is about 0.13 % stricter here.

## 5. Reproduce

```bash
/usr/bin/python3 validation/reports/scripts/test_09_kpis.py validation
```

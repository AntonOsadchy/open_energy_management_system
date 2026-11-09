# Test 01: Total profit in KPIs vs total profit from hourly results

| | |
|---|---|
| Run | `test_4_full_system` (2025-01-01 to 2025-12-31 23:00, 8,760 × 1 h, Europe/Copenhagen) |
| Date of check | 2026-11-05 |
| Result | **PASS** |
| Tolerance | 1e-6 relative on totals (README §5) |
| Largest relative deviation found | 1.4e-15 (floating-point rounding) |

## 1. Question

Does the total profit reported in `KPIs.xlsx` equal the total profit you get by adding up the hourly results?

## 2. Data used

- **Hourly results (source of truth for this test):** `test_4_full_system.xlsx`, sheet `Time series`. Every computed number below comes from this sheet.
- **Reported figures (under test):** `KPIs.xlsx`. They are only read and compared, never used as an input.
  - `Summary`: Total cost, Energy, Network, Export revenue, Tie-break, Solver objective.
  - `Model KPIs`: `total_cost`.
  - `Run`: `total_cost`, `objective`.
- **Cross-reference:** `test_4_full_system.xlsx`, sheet `Costs`, which holds the optimiser's category totals.
- **Definitions:** README §2. Profit = −total cost. Total cost = ENERGY + NETWORK + REVENUE (revenue is a negative cost). TIEBREAK is excluded from total cost.
- Neither workbook contains formulas; every cell is a stored value. The KPIs are therefore numbers to check, not self-computing cells.

## 3. Method

The hourly profit is computed in two independent ways, then compared with every place where a total is reported.

**Route A: the model's own hourly columns**
- A1 = Σ `econ profit [EUR]`
- A2 = Σ (`econ revenue` − `econ cost ENERGY` − `econ cost NETWORK`)
- A3 = the last value of `econ profit cumulative [EUR]`

**Route B: recomputed from flows × input prices, without using any `econ` column**
- revenue_t = export_price_t · export_t · Δt
- energy_t = import_price_t · import_t · Δt
- network_t = (import_fee_t · import_t + export_fee_t · export_t) · Δt
- profit_t = revenue_t − energy_t − network_t
- B = Σ profit_t

**Internal consistency checks**
- Route A and route B are compared hour by hour.
- The cumulative profit column is checked against a running sum of the hourly profit column.

`step_h` is 1 h in all 8,760 rows.

## 4. Results

### 4.1 Total profit

| Source | Total profit [EUR] | Difference vs route B [EUR] |
|---|---:|---:|
| **B: recomputed from flows × prices** | **−598,823.12509035** | reference |
| A1: Σ `econ profit` | −598,823.12509035 | 0 |
| A2: Σ (revenue − costs) | −598,823.12509035 | 0 |
| A3: last `econ profit cumulative` | −598,823.12509035 | −2.8e-12 |
| KPIs `Summary`: −Total cost | −598,823.12509035 | −8.0e-10 |
| KPIs `Model KPIs`: −`total_cost` | −598,823.12509035 | −8.0e-10 |
| KPIs `Run`: −`total_cost` | −598,823.12509035 | −8.0e-10 |
| `Costs` sheet: −Σ (excluding TIEBREAK) | −598,823.12509035 | −8.0e-10 |

The largest relative difference is 8.0e-10 / 598,823 ≈ **1.4e-15**, about nine orders of magnitude inside the 1e-6 tolerance.

### 4.2 Components

| Component [EUR] | Route B (hourly) | KPIs `Summary` | `Costs` sheet | Max abs. difference |
|---|---:|---:|---:|---:|
| Energy (day-ahead on import) | 524,338.975968 | 524,338.975968 | 524,338.975968 | 4e-10 |
| Network (import + export fees) | 78,643.162074 | 78,643.162074 | 78,643.162074 | 3e-10 |
| Export revenue | 4,159.012952 | 4,159.012952 | 4,159.012952 (shown as −) | 0 |
| **Total cost** | **598,823.125090** | **598,823.125090** | **598,823.125090** | 8e-10 |

Total cost = 524,338.98 + 78,643.16 − 4,159.01 = 598,823.13 EUR. **Profit = −598,823.13 EUR.**

### 4.3 Objective and tie-break

The tie-break is recomputed from the hourly flows at 0.01 EUR/MWh.

| Item [EUR] | Recomputed | Reported |
|---|---:|---:|
| Tie-break, grid (import + export) | 68.133237 | 68.133237 (`Costs`) |
| Tie-break, storage (charge + discharge) | 25.810688 | 25.810688 (`Costs`) |
| Tie-break, total | 93.943925 | 93.943925 (`Summary`, `Costs`) |
| Total cost + tie-break | 598,917.069015 | 598,917.069015 (`Summary` and `Run` objective) |

The objective equals total cost plus tie-break, so excluding the tie-break from profit is applied consistently.

### 4.4 Hour-by-hour consistency

| Check | Max abs. difference |
|---|---:|
| `econ profit` vs route B, per hour | 1.1e-13 EUR |
| `econ profit` vs (`econ revenue` − `econ cost ENERGY` − `econ cost NETWORK`), per hour | 1.1e-13 EUR |
| `econ profit cumulative` vs running sum of `econ profit` | 5.8e-11 EUR |

All three are at machine precision. The total does not match by coincidence: every single hour agrees.

## 5. Conclusion

**PASS.** The total profit of **−598,823.13 EUR** (a net cost) is identical across:
- the hourly results, summed directly and recomputed from flows × prices;
- the three KPI sheets (`Summary`, `Model KPIs`, `Run`);
- the optimiser's `Costs` sheet.

Each component (energy, network, export revenue) also reconciles, the tie-break is correctly excluded, and the agreement holds hour by hour, not only in total.

## 6. Observations outside the scope of this test

- Profit is negative because the site is a net consumer: 1 MW of flat demand against 2 MWp of PV, which supplies 2,150 of the 8,760 MWh demand. "Profit" here is the negative of the electricity bill.
- *Correction (made during Test 02):* an earlier version of this note said that several numeric parameters appear as booleans in `Parameters` (`p_ch_max = True`, etc.). That was wrong. The cells hold real numbers (`p_ch_max = 1`, `soc_min = 0`). pandas converted them to booleans when reading the column, which mixes booleans and numbers. The workbook is correct, and later tests read `Parameters` with openpyxl.

## 7. Reproduce

```bash
/usr/bin/python3 validation/reports/scripts/test_01_total_profit.py validation
```

# Test 02: Demand is always satisfied

| | |
|---|---|
| Run | `test_4_full_system` (2025-01-01 to 2025-12-31 23:00, 8,760 × 1 h, Europe/Copenhagen) |
| Date of check | 2026-11-05 |
| Data used | `test_4_full_system.xlsx` only (sheets `Parameters`, `Time series`) |
| Result | **PASS** |
| Tolerance | 1e-6 MW on flows (README §5) |

## 1. Question

Is the site's demand fully met in every hour of the year?

"Met" needs more than the demand column showing the right number. The energy delivered to demand must come from real, feasible sources. This test therefore checks four things:

1. **Coverage:** every hour of the year is present, so no hour is silently missing.
2. **Delivery:** in every hour, the energy delivered to demand equals the demand profile.
3. **Supply:** the bus balances in every hour, so the delivered energy is actually supplied and nothing is made up by an unbalanced bus.
4. **Feasibility:** every supply source stays within its physical limits: grid import, battery discharge (backed by stored energy) and PV (no more than is available).

## 2. Data

- **Demand profile:** `Parameters` → `Demand demand / profile = 1` MW, stored as a numeric cell. It is a constant, so the workbook has no separate input column for demand.
- **Limits** (from `Parameters`):

  | Parameter | Value |
  |---|---|
  | `max_import` | 2 MW |
  | `p_dis_max` | 1 MW |
  | `e_max` | 2 MWh |
  | `soc_min` | 0 |

- **Hourly data** (from `Time series`): the six port flows, `state store.e`, `state prod.curtailment`, `input prod.profile`, `step_h`, and both time columns.
- **Slack:** no slack, unserved-energy or load-shedding column exists in the workbook. `Parameters` shows the bus `el` as a plain electricity bus.

## 3. Results

### 3.1 Coverage of the horizon

| Check | Result |
|---|---|
| Rows | 8,760 |
| UTC range | 2024-12-31 23:00 to 2025-12-31 22:00 (= 2025-01-01 00:00 to 2025-12-31 23:00 local) |
| UTC step | 1 h in all 8,759 gaps; no duplicates, no missing hours |
| `step_h` | 1 in every row |
| Local calendar days | 365 |
| Days not 24 h | 2025-03-30 (23 h) and 2025-10-26 (25 h), the DST changes, as expected |
| Empty demand cells | 0 |

### 3.2 Delivered demand = profile

| Check | Result |
|---|---|
| `flow el → demand.in`, min / max | 1 / 1 MW |
| Max abs. deviation from 1 MW | 0 (exact) |
| Hours below profile (beyond 1e-6) | **0** |
| Hours above profile | 0 |
| Energy delivered | 8,760 MWh (expected 8,760) |

### 3.3 Bus balance every hour

The test checks that **import + discharge + PV used = export + charge + demand** in every hour.

| Check | Result |
|---|---|
| Max abs. imbalance | 6.7e-16 MW (floating-point rounding) |
| Hours violating 1e-6 MW | **0** of 8,760 |

Annual check (MWh):

| Inflows | MWh | Outflows | MWh |
|---|---:|---|---:|
| Grid import | 6,777.616 | Demand | 8,760.000 |
| Battery discharge | 1,224.397 | Battery charge | 1,356.672 |
| PV used | 2,150.367 | Grid export | 35.708 |
| **Total in** | **10,152.380** | **Total out** | **10,152.380** |

### 3.4 Supply sources are feasible

| Source | Check | Result |
|---|---|---|
| All flows | ≥ 0 (beyond −1e-6) | 0 violations; minimum flow is 0.0 |
| Grid import | ≤ 2 MW | max 2.000; 0 violations |
| Battery discharge | ≤ 1 MW | max 1.000; 0 violations |
| Battery energy | `store.e` ≥ soc_min · e_max = 0 | min 0.000 MWh; 0 violations |
| PV used | ≤ available PV | 0 violations |
| PV | used + curtailed = available | max abs. difference 2.2e-16 MW |

This test only checks that stored energy never goes negative. Whether the SOC trajectory is consistent with the charge and discharge flows (the storage recursion) is a separate storage test.

### 3.5 How demand was met (for information)

- **Grid alone could always cover demand.** The 2 MW grid limit is above the 1 MW demand, so demand could never have been capacity-constrained in this setup.
- **1,346 h** (15 %) with zero grid import: demand was met entirely by PV and/or the battery.
- **664 h** with import at the 2 MW limit: the grid supplied demand and charged the battery at the same time.

## 4. Conclusion

**PASS.** Demand is satisfied in all 8,760 hours of 2025:
- **Delivery:** exactly 1 MW is delivered every hour, 8,760 MWh in total, with no gaps in the time axis.
- **Supply:** the bus balances in every hour to within 1e-15 MW.
- **Feasibility:** every supply source stays within its limits, so the delivered energy comes from real sources.

## 5. Caveats

- **Partly true by construction.** The model fixes `demand.in = profile` as a hard constraint (README §2), so 3.2 shows the constraint was respected rather than that the optimiser chose to serve demand. The checks that carry the weight are 3.3 and 3.4: an infeasible or slack-patched solution would fail those.
- **Constant demand.** Demand is a single number (1 MW), not a time series, so this test cannot catch a misaligned demand profile. That kind of error is only possible with a time-varying profile.

## 6. Reproduce

```bash
/usr/bin/python3 validation/reports/scripts/test_02_demand_satisfied.py validation
```

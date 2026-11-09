# Test 08: Bus marginal price `price el` is consistent

| | |
|---|---|
| Run | `test_4_full_system` (2025-01-01 to 2025-12-31 23:00, 8,760 × 1 h, Europe/Copenhagen) |
| Date of check | 2026-11-05 |
| Data used | `test_4_full_system.xlsx` only (sheets `Parameters`, `Time series`) |
| Result | **PASS** (9 / 9 conditions, 0 violations) |
| Tolerance | 1e-6 EUR/MWh, 1e-6 MW |

## 1. Question

`price el` is the model's marginal price of electricity on the bus: what one more MWh would cost in that hour. It is the LP dual of the bus balance. Tests 04 to 06 used it to explain the model's decisions. Is it consistent with what the grid, the PV and the battery are doing in each hour?

## 2. Expected values

In an optimal LP, the marginal price is set by whichever source or sink can still adjust in that hour. The tie-break of TB = 0.01 EUR/MWh on grid and battery flows (`Parameters` → `settings / tie_break`) is part of the marginal cost, so it appears in the expected values.

| Operating state | Expected `price el` |
|---|---|
| Import between 0 and 2 MW | = DA + import fee + TB (cost of one more MWh imported) |
| Export between 0 and 2 MW | = DA − export fee − TB (value of one more MWh exported) |
| Import at the 2 MW limit | ≥ DA + import fee + TB (the limit may add a premium) |
| No import and no export | between DA − export fee − TB and DA + import fee + TB |
| PV partly curtailed | = 0 (PV has zero marginal cost) |
| PV fully used | ≥ 0 |
| PV fully curtailed | ≤ 0 |
| Charging and discharging at once | < 0 (only worth doing when energy has negative value) |
| Battery charging or discharging between its limits, between two SOC bounds | The implied value of stored energy, (price + TB)/η_ch when charging or (price − TB)·η_dis when discharging, is the same in every such hour |

## 3. Results

| Condition | Hours | Violations | Detail |
|---|---:|---:|---|
| Import between 0 and 2 MW: = DA + fee + TB | 6,750 | **0** | max \|diff\| 5.7e-14 |
| Export between 0 and 2 MW: = DA − export fee − TB | 306 | **0** | max \|diff\| 1.1e-13 |
| Import at 2 MW limit: ≥ DA + fee + TB | 664 | **0** | Premium > 0 in 28 h, all with DA + fee < 0 (see 3.1) |
| No grid flow: within the two grid bounds | 1,040 | **0** | 327 h exactly at the import bound, 0 at the export bound |
| PV partly curtailed: = 0 | 31 | **0** | max \|price el\| 0 |
| PV fully used: ≥ 0 | 4,113 | **0** | |
| PV fully curtailed: ≤ 0 | 43 | **0** | |
| Charging and discharging at once: < 0 | 11 | **0** | −14.62 … −1.22 EUR/MWh |
| Battery between its limits: constant stored-energy value between SOC bounds | 314 pairs | **0** | max \|Δ\| 2.8e-14 EUR/MWh |

Every hour falls into exactly one of the four grid states: 6,750 + 664 + 306 + 1,040 = 8,760.

### 3.1 Premium at the 2 MW import limit

In 636 of the 664 hours at the limit, the price equals the import cost: the battery is charging at its 1 MW maximum at the same time, so neither limit sets a premium. In the other **28 hours** the price is above the import cost by up to 18.53 EUR/MWh. All 28 are hours when the site is **paid** to import (DA + fee between −27.45 and −1.30 EUR/MWh). The 2 MW limit stops the site taking more paid energy, so the bus values one extra MWh at less than the (negative) import price.

| Hour | DA + fee + TB | `price el` | Premium |
|---|---:|---:|---:|
| 2025-08-10 14:00 | −27.44 | −8.91 | 18.53 |
| 2025-04-06 15:00 | −19.40 | −4.26 | 15.14 |
| 2025-06-21 14:00 | −15.61 | −1.70 | 13.91 |
| 2025-06-21 13:00 | −15.36 | −1.70 | 13.66 |
| 2025-04-05 12:00 | −13.39 | −0.05 | 13.34 |

### 3.2 Price statistics

| Item | Value |
|---|---:|
| `price el` min / mean / max | −14.62 / 92.01 / 597.06 EUR/MWh |
| Hours with price < 0 | 35 |
| Hours with price = 0 | 61 |
| Average weighted by withdrawals (demand + charge + export) | 88.288439 EUR/MWh, equal to the `avg_marginal_price` KPI (Test 09) |

## 4. Conclusion

**PASS.** `price el` is exactly what an optimal LP should produce in every hour:
- **Grid:** it equals the import cost when the site imports, and the export value when it exports. It stays between the two when the grid is idle, and rises above the import cost only where the 2 MW limit blocks more paid import.
- **PV:** it is zero when PV is partly curtailed, non-negative when PV is fully used, and non-positive when PV is fully curtailed.
- **Battery:** the value of stored energy is constant over each battery cycle.

This confirms the price column, and supports the explanations in Tests 04, 05 and 06 that rely on it.

## 5. Notes

- **Tie-break included.** The 0.01 EUR/MWh tie-break is part of the marginal cost, so the bus price is 0.01 above DA + fee when importing and 0.01 below DA − fee when exporting. It is excluded from total cost (Test 01), but it does appear in the dual.
- **Necessary, not sufficient.** These conditions must hold at an optimum, but passing them does not prove optimality by itself. A full independent re-solve would.

## 6. Reproduce

```bash
/usr/bin/python3 validation/reports/scripts/test_08_bus_price.py validation
```

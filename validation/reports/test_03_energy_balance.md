# Test 03: Energy balance

| | |
|---|---|
| Run | `test_4_full_system` (2025-01-01 to 2025-12-31 23:00, 8,760 × 1 h, Europe/Copenhagen) |
| Date of check | 2026-11-05 |
| Data used | `test_4_full_system.xlsx` only (sheets `Parameters`, `Time series`) |
| Result | **PASS** |
| Tolerance | 1e-6 MWh per hour, 1e-6 relative on totals |

## 1. Question

Does everything imported plus produced equal consumption plus efficiency losses?

**Import + PV used = Demand + Export + Battery losses + Δ stored energy**

- **Δ stored energy:** the battery's end energy minus its start energy. It is zero over the year, because the battery ends where it started (1 MWh), but it matters hour by hour.
- **PV used:** produced PV after curtailment. Curtailed PV never reaches the bus, so it is not a loss of the system. It is shown separately.

## 2. Method

Battery losses are calculated **from the efficiencies**, not as the leftover of the balance. This means the balance is a real test and does not close by construction.

- Charging loss = (1 − η_ch) · charge
- Discharging loss = (1/η_dis − 1) · discharge
- η_ch = η_dis = 0.95 and self-discharge σ = 0 (from `Parameters`).

Δ stored energy per hour comes from the reported `state store.e`, with e[−1] = soc_init · e_max = 1 MWh.

The balance is checked:
- for the full year;
- in every one of the 8,760 hours.

## 3. Results

### 3.1 Annual balance (MWh)

| Supply | MWh | Use | MWh |
|---|---:|---|---:|
| Grid import | 6,777.616 | Demand | 8,760.000 |
| PV used | 2,150.367 | Grid export | 35.708 |
| | | Battery losses (from η) | 132.276 |
| | | Δ stored energy (1.000 → 1.000) | 0.000 |
| **Total** | **8,927.983** | **Total** | **8,927.983** |

**Residual: 9.7e-15 MWh.** That is floating-point rounding, about 1e-18 relative.

### 3.2 Battery losses

| Item | MWh |
|---|---:|
| Charged | 1,356.672 |
| Discharged | 1,224.397 |
| Loss while charging (5 % of charge) | 67.834 |
| Loss while discharging (1/0.95 − 1 of discharge) | 64.442 |
| **Total loss, from efficiencies** | **132.276** |
| Total loss, as charged − discharged − Δ stored | 132.276 (diff 3e-13) |
| Implied round-trip efficiency | 0.9025 (theory: 0.95 × 0.95 = 0.9025) |

The two ways of calculating the losses agree. The battery returns exactly 90.25 % of what it absorbs, as the parameters require.

### 3.3 PV

| Item | MWh |
|---|---:|
| PV available (input profile) | 2,210.049 |
| PV curtailed | 59.682 |
| PV used | 2,150.367 |
| Used + curtailed − available | −4.5e-13 |

### 3.4 Hourly balance

| Check | Result |
|---|---|
| Max \|supply − (use + losses + Δ stored)\| in any hour | 1.1e-15 MWh (2025-04-29 14:00) |
| Hours above 1e-6 MWh | **0** of 8,760 |
| Battery side: charge − discharge = losses + Δ stored, max \|diff\| | 1.1e-15 MWh |

## 4. Conclusion

**PASS.** In every hour and over the year, imported plus produced energy equals consumption (demand + export) plus battery efficiency losses plus the change in stored energy:
- **Annual totals:** 8,927.98 MWh = 8,927.98 MWh.
- **Battery losses:** 132.28 MWh, exactly what the 95 % / 95 % efficiencies imply (round-trip 90.25 %).
- **Unexplained energy:** none appears or disappears.

## 5. Notes

- **Curtailment:** 59.68 MWh of PV was not used. This is a decision not to produce, not a system loss, so it sits outside the balance (§3.3).
- **Hourly view:** within an hour, energy put into the battery that is still stored (Δ stored > 0) is not a loss. That is why the hourly check needs the Δ stored term.

## 6. Reproduce

```bash
/usr/bin/python3 validation/reports/scripts/test_03_energy_balance.py validation
```

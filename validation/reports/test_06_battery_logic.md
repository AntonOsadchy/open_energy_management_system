# Test 06: Battery charges when energy is cheap and discharges when it is expensive

| | |
|---|---|
| Run | `test_4_full_system` (2025-01-01 to 2025-12-31 23:00, 8,760 × 1 h, Europe/Copenhagen) |
| Date of check | 2026-11-05 |
| Data used | `test_4_full_system.xlsx` only (sheet `Time series`) |
| Result | **PASS** |
| Battery | 2 MWh, 1 MW, η_ch = η_dis = 0.95 (round-trip 90.25 %) |

## 1. Question

Does the battery follow a sensible buy-low, sell-high pattern?

## 2. Price measures

| Measure | Meaning |
|---|---|
| **DA + import fee** (main) | What one imported MWh costs the site |
| DA | Day-ahead price alone |
| Bus price `price el` | The model's marginal value of energy in that hour. It equals DA + fee when importing and DA − export fee when exporting, and between those it is set by the battery. |

A battery move is only worth making if the gain covers the round-trip losses: **discharge price × 0.9025 ≥ charge price.**

## 3. Results

### 3.1 Annual averages (energy-weighted)

| EUR/MWh | When charging | When discharging | All hours |
|---|---:|---:|---:|
| DA + import fee | **63.59** | **138.05** | 92.87 |
| DA | 52.11 | 125.79 | 81.10 |
| Bus price | 63.53 | 134.45 | 92.01 |

- Charging happens well below the year's average price, and discharging well above it.
- Break-even discharge price with losses: 63.53 / 0.9025 = 70.40 EUR/MWh. The actual average is **134.45**, about 1.9× the break-even.

| Operating hours | h |
|---|---:|
| Charging | 2,074 |
| Discharging | 1,444 |
| Both at once (paid-import hours, Test 04) | 11 |
| Idle | 5,253 |

### 3.2 Timing within each day

Each hour is ranked by DA + fee within its local day, from 0 = cheapest to 1 = most expensive.

| Check | Result |
|---|---:|
| Charged energy in the **cheapest half** of the day | **94.7 %** |
| Discharged energy in the **most expensive half** of the day | **96.9 %** |
| Average rank of charging | 0.21 |
| Average rank of discharging | 0.89 |
| Days with both charging and discharging | 363 |
| … where average discharge price > average charge price | **363 / 363** |
| … where the bus price spread covers the 9.75 % losses | **363 / 363** |

### 3.3 Hour-by-hour test: every energy move pays for its losses

These are the strict conditions a correctly working battery must meet in every hour:

| Condition | Violations |
|---|---:|
| Each charging hour: the **next** discharge hour's bus price × 0.9025 ≥ the charging bus price | **0** of 2,074 |
| Each discharging hour: its bus price × 0.9025 ≥ the bus price of the **previous** charging hour | **0** of 1,444 |

The battery never stores energy that it later sells at a loss after efficiency losses.

### 3.4 Apparent exceptions to "cheap/expensive" (all explained)

**Charging in the day's most expensive quarter: 9 h, 7.1 MWh.** All of these are late-evening hours (20:00–23:00). The energy is discharged soon after, at a higher price, often after midnight, which counts as the next calendar day:

| Charge hour | Bus price | Next discharge | Bus price |
|---|---:|---|---:|
| 2025-02-08 23:00 | 124.33 | 2025-02-09 00:00 | 145.70 |
| 2025-02-24 20:00 | 90.86 | 2025-02-24 22:00 | 123.65 |
| 2025-03-30 21:00 | 61.70 | 2025-03-31 07:00 | 157.20 |
| 2025-04-19 23:00 | 105.11 | 2025-04-20 05:00 | 119.34 |
| 2025-08-05 22:00 | 15.34 | 2025-08-05 23:00 | 22.45 |
| 2025-09-21 22:00 | 24.77 | 2025-09-21 23:00 | 30.75 |
| 2025-10-05 21:00–23:00 | 15.77–17.17 | 2025-10-06 00:00 | 31.55 |

**Discharging in the day's cheapest quarter: 19 h, 15.1 MWh.**
- **11 h** are the paid-import hours (DA + fee < 0). The battery charges and discharges at the same time to burn paid-for import through its losses (Test 04, README check 9).
- **7 h** discharge energy that was charged even more cheaply just before, often late the previous evening. For example, it charged at 21.80 on 2025-01-06 23:00 and discharged at 24.30 on 2025-01-07 00:00. The hour is cheap within its own day, but still dearer than the charging hour, after losses.
- **1 h** (2025-01-01 00:00) uses the 1 MWh the battery holds at the start of the run. The end-of-year condition only requires it to finish with ≥ 1 MWh.

### 3.5 Negative prices

| Situation | Hours | Battery behaviour |
|---|---:|---|
| DA < 0 | 441 | Charging in 313 h; discharging only (without charging) in 5 h |
| DA + import fee < 0 (paid to import) | 43 | Charging at the full 1 MW in 40 h |

### 3.6 Monthly averages (DA + import fee, EUR/MWh)

| Month | Charged MWh | Discharged MWh | Avg price charging | Avg price discharging |
|---|---:|---:|---:|---:|
| Jan | 117.3 | 106.8 | 82.41 | 148.84 |
| Feb | 114.1 | 103.0 | 102.84 | 164.34 |
| Mar | 118.6 | 107.0 | 62.52 | 144.68 |
| Apr | 119.2 | 107.5 | 51.15 | 135.18 |
| May | 109.9 | 99.2 | 42.07 | 129.27 |
| Jun | 116.9 | 105.5 | 37.18 | 119.49 |
| Jul | 114.2 | 103.0 | 65.11 | 134.66 |
| Aug | 112.3 | 101.3 | 50.79 | 131.51 |
| Sep | 125.2 | 113.0 | 47.77 | 142.95 |
| Oct | 128.5 | 116.0 | 66.44 | 133.47 |
| Nov | 96.5 | 87.1 | 87.97 | 147.38 |
| Dec | 84.1 | 74.9 | 74.32 | 121.21 |

The battery discharges at a higher price than it charges in every month.

### 3.7 Value of the battery

| Item | EUR |
|---|---:|
| Cost without battery (`ref cost without battery`, summed) | 682,647.92 |
| Cost with battery (Σ −`econ profit`) | 598,823.13 |
| **Battery saving** | **83,824.80** (≈ 68.46 EUR per MWh discharged) |

## 4. Conclusion

**PASS.** The battery charges when energy is cheap and discharges when it is expensive:
- **Annual prices:** on average it charges at 63.59 EUR/MWh and discharges at 138.05 EUR/MWh (DA + import fee).
- **Daily timing:** 95–97 % of its energy moves in the right half of the day.
- **Every day and every month:** discharge is at a higher price than charge.
- **Hour by hour:** no energy move fails to recover its 9.75 % losses.
- **Value:** the battery cuts the annual bill by **83,824.80 EUR**.

The few hours that look wrong on a simple daily ranking are late-evening charging for after-midnight or next-morning peaks, short cheap-to-slightly-dearer cycles, and the paid-import hours. All are economically correct.

## 5. Notes

- **Perfect foresight.** The battery's timing benefits from knowing all future prices.
- **Charging hours look costlier.** In 1,843 hours the cost with the battery is higher than without. These are the charging hours: buying energy that is worth more later.

## 6. Reproduce

```bash
/usr/bin/python3 validation/reports/scripts/test_06_battery_logic.py validation
```

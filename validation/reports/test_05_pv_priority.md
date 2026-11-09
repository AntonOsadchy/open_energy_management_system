# Test 05: PV serves demand first; the surplus fills the battery and is exported

| | |
|---|---|
| Run | `test_4_full_system` (2025-01-01 to 2025-12-31 23:00, 8,760 × 1 h, Europe/Copenhagen) |
| Date of check | 2026-11-05 |
| Data used | `test_4_full_system.xlsx` only (sheet `Time series`) |
| Result | **PASS, with explained exceptions** (all economically justified, none a model error) |
| Tolerance | 1e-6 MW |

## 1. Question

Is PV used to meet demand first, with only the remainder (the surplus) going to the battery and to export?

## 2. Method

All flows meet on one bus (`el`), so the workbook does not say which source fed which consumer. Each hour is therefore split according to the rule being tested:

- PV → demand = min(PV used, demand)
- PV surplus = PV used − PV → demand
- Demand left for grid and battery = demand − PV → demand

Four properties are then checked:

| | Property | Meaning |
|---|---|---|
| **A** | PV first | Demand is never served by the grid or the battery while usable PV is curtailed. |
| **B** | Surplus means demand is covered | In surplus hours, no import or discharge is needed for demand. |
| **C** | Export only from PV surplus | The battery does not export to the grid. |
| **D** | Surplus split | How the surplus is divided between battery and export, and whether export happens while the battery has room. |

Any exception is explained from the input prices and fees and the bus price `price el`.

## 3. Results

### 3.1 Overview

| Item | Value |
|---|---:|
| Hours with PV | 4,187 |
| Hours with PV < demand (1 MW) | 3,435 |
| Hours with PV ≥ demand | 752 |
| PV → demand | 2,018.46 MWh (max possible 2,060.89) |
| PV surplus | 131.91 MWh in 689 h |

### 3.2 A: PV serves demand first ✔

| Check | Result |
|---|---|
| Hours where available PV was held back from demand | 43 (42.44 MWh) |
| … with DA + import fee < 0 (site **paid** to import) | **43 / 43** |
| … with a positive import cost | **0** |

PV always serves demand first. The only exception is the 43 hours when the site is paid to import. There, replacing PV with grid import earns money (see Test 04). The 42.44 MWh gap between "PV → demand" and "max possible" is exactly these hours.

### 3.3 B: In surplus hours, demand is fully covered by PV ✔

| Check | Result |
|---|---|
| Surplus hours with battery discharge | **0** |
| Surplus hours with grid import | 252 |
| … where import ≤ battery charge (import only feeds the battery) | **252 / 252** |

In these 252 hours, the PV surplus is smaller than what the battery is charging. Grid import tops up the battery: in 139 hours it charges at the full 1 MW, and in 29 hours it charges up to full (2 MWh). No import or discharge is ever used for demand when PV covers it.

### 3.4 Where the surplus went ✔

| PV surplus 131.91 MWh | MWh | Share |
|---|---:|---:|
| → battery | 116.24 | 88 % |
| → export | 15.67 | 12 % |
| unaccounted | 0.00 | 0 % |

All surplus goes to the battery or to export, as the rule says. Curtailed PV is not part of the surplus; see Test 04.

### 3.5 Exceptions to a strict "PV first, then battery, then export" order

The optimiser has no fixed priority order. It chooses each flow by price. Three behaviours therefore differ from a strict rule. Each one is checked against the prices below.

**(1) Exporting surplus PV while the battery had room: 33 h, 4.36 MWh**

| Check | Result |
|---|---|
| Battery state in these hours | empty, or not charging |
| Export value (DA − export fee) | 2.3 … 78.2 EUR/MWh |
| Battery charged later in the same cycle (before its next discharge) at a **lower** bus price | **33 / 33** |

Example, 2025-04-10 11:00. The surplus earned 64.57 EUR/MWh by exporting. Later that day the battery was charged at no more than 12.87 EUR/MWh. Selling now and charging later was cheaper than storing now.

**(2) Battery exporting to the grid: 194 h, 20.04 MWh of the 35.71 MWh total export**

| Check | Result |
|---|---|
| Battery discharge in these hours | 0.9 … 1.0 MW |
| PV in these hours | ≤ 0.43 MW (no surplus) |
| Bus price | 23.8 … 474.4 EUR/MWh (mostly evening peaks) |

The battery discharges more than the demand that PV leaves uncovered, and sells the extra at high evening prices. This is battery arbitrage: export from the battery, not from PV. On a single bus it is equally valid to call this "PV exported while the battery serves demand". Either way, the export does not come from a PV surplus.

**(3) Battery also charged from the grid**

As shown in §3.3, the battery is also charged from grid import, not only from PV surplus. This includes the 664 h with import at the 2 MW limit (Test 02).

### 3.6 Export breakdown

| Total export 35.71 MWh | MWh |
|---|---:|
| From PV surplus | 15.67 |
| From battery discharge | 20.04 |

## 4. Conclusion

**PASS, with explained exceptions.**
- **PV first:** PV always serves demand first, except in the 43 hours when the site is paid to import instead.
- **Surplus:** all PV surplus goes to the battery (88 %) or to export (12 %), and demand never needs grid or battery energy while PV covers it.

The optimiser does **not** follow a strict rule-based "fill battery, then export" order. In every exception, the price data shows it chose the cheaper option:
- It exported surplus when the battery could be filled more cheaply later (33 / 33 confirmed).
- It sold battery energy to the grid at evening peak prices.
- It charged the battery from the grid when that paid off.

This is the expected behaviour of a cost-optimising dispatch, as opposed to a fixed self-consumption rule.

## 5. Notes

- **Split by convention.** The "PV first" split is an accounting choice on a single bus. Totals per source and per consumer are exact. Which MWh went where is not.
- **Perfect foresight.** Exception (1) relies on it: the model knows the cheaper later charging hours in advance. A real-time controller would act more like the strict rule.

## 6. Reproduce

```bash
/usr/bin/python3 validation/reports/scripts/test_05_pv_priority.py validation
```

# Test 04: PV is curtailed when electricity cost is negative

| | |
|---|---|
| Run | `test_4_full_system` (2025-01-01 to 2025-12-31 23:00, 8,760 × 1 h, Europe/Copenhagen) |
| Date of check | 2026-11-05 |
| Data used | `test_4_full_system.xlsx` only (sheet `Time series`) |
| Result | **PASS** |
| Tolerance | 1e-6 MW |

## 1. Question

Is PV curtailed when electricity has a negative cost, and only then?

## 2. What "negative cost" means for PV

PV has no marginal cost here (`marginal_cost = 0`, `curtailment_cost = 0`). An extra MWh of PV is worth whatever it displaces or earns:

| Where the extra PV MWh goes | Its value | Curtailing pays when |
|---|---|---|
| It replaces grid import | DA + import fee (import cost avoided) | **DA + import fee < 0**: the site is paid to import |
| It gets exported | DA − export fee (net export earnings) | **DA − export fee < 0**: exporting costs money |
| It goes into the battery | the value of that energy later | stored energy has no value later |

The model reports this marginal value directly as the bus price `price el`. An LP optimum must satisfy two rules:
- **PV curtailed** ⇒ `price el` ≤ 0
- **PV used** ⇒ `price el` ≥ 0

So partial curtailment requires `price el` = 0.

A negative **day-ahead price on its own is not the trigger.** The import fee (≈ 10.69 EUR/MWh) usually keeps the site's import cost positive even when the DA price is negative.

## 3. Results

### 3.1 Overview

| Item | Hours |
|---|---:|
| Hours with PV available | 4,187 |
| Hours with negative DA price (with PV) | 441 (415) |
| Hours with **DA + import fee < 0** (with PV) | 43 (43) |
| Hours with **DA − export fee < 0** (with PV) | 629 |
| **Hours with curtailment** | **74** (43 fully, 31 partially), 59.68 MWh in total |

### 3.2 Direction 1: whenever PV is curtailed, the cost is negative

| Check | Result |
|---|---|
| Curtailed hours with `price el` > 0 | **0** (range in curtailed hours: −14.62 … 0.00 EUR/MWh) |
| Curtailed hours where DA + import fee < 0 | 43 |
| Curtailed hours where DA + import fee ≥ 0 | 31 |
| … of which DA − export fee < 0 (exporting the surplus would cost money) | **31 / 31** |
| … of which unexplained | **0** |

The 31 hours with a positive import cost all look the same:
- PV exceeds the 1 MW demand.
- There is no import and no export.
- `price el` = 0 exactly.
- Exporting the surplus would lose money: DA − export fee is between −8.5 and −0.1 EUR/MWh.

Why the battery did not absorb the surplus:

| Battery situation | Hours | Why storing more PV had no value |
|---|---:|---|
| Already full (e = 2 MWh) | 11 | No room. |
| Not full, but fills from free PV later that day before its next discharge | 11 | Charging earlier has zero value: a tie, consistent with `price el` = 0. |
| Not full, and its next discharge is in an hour with DA + import fee < 0 | 9 | The room is kept for that later hour, when the site is **paid** to import and charge. These are the same hours as the simultaneous charge/discharge hours in README check 9. |

Five of the 31 hours have a positive DA price (0.01 … 1.66 EUR/MWh) that is still below the 1.755 EUR/MWh export fee. This is another reason a negative DA price is not the right test.

### 3.3 Direction 2: whenever the cost is negative, PV is curtailed

| Check | Result |
|---|---|
| Hours with DA + import fee < 0 and PV available | 43 |
| … PV fully curtailed | **43 / 43** |
| … PV used | 0.000 MWh |
| Grid import in these hours | 1.0 … 2.0 MW (the site imports as much as it can absorb) |
| Grid export in these hours | 0 |
| Hours with `price el` < 0 and PV not fully curtailed | **0** |
| Hours with PV used and `price el` < 0 | **0** |

When the site is paid to import, it replaces all PV with paid import. This is correct.

### 3.4 Negative DA price alone does not cause curtailment (as expected)

| Hours with PV, DA < 0 but DA + import fee ≥ 0 | 372 |
|---|---:|
| Curtailed | 26 (all export-side cases from §3.2) |
| Not curtailed | 346 |

In these hours, using PV still saves import that would cost DA + fee > 0, so curtailing would be wrong.

### 3.5 Monthly curtailment

| Month | Mar | Apr | May | Jun | Aug | Sep |
|---|---:|---:|---:|---:|---:|---:|
| Hours | 5 | 20 | 8 | 27 | 9 | 5 |
| MWh | 3.84 | 19.12 | 4.51 | 20.11 | 7.24 | 4.87 |

## 4. Conclusion

**PASS.** PV curtailment follows the economics exactly:
- **Paid to import:** in all 43 hours where the site is paid to import (DA + import fee < 0), PV is fully curtailed.
- **Unprofitable export:** the other 31 curtailed hours are all hours where surplus PV could only be exported at a loss (DA − export fee < 0) and the battery could not use it profitably.
- **No wrong curtailment:** PV is never curtailed when electricity has a positive value (`price el` > 0), and never used when it has a negative value.
- **DA alone is not the trigger:** a negative day-ahead price by itself does not cause curtailment, which is correct because of the import fee.

## 5. Notes

- **Partial curtailment as a tie.** In the 11 partial-curtailment hours where the battery fills later anyway, charging now or later is equally good. The 0.01 EUR/MWh tie-break settles it, and other equally good schedules exist.
- **Bus price is a model output.** `price el` comes from the LP dual. §3.2 and §3.3 therefore also give an independent explanation for each curtailed hour from the input prices and fees.

## 6. Reproduce

```bash
/usr/bin/python3 validation/reports/scripts/test_04_pv_curtailment.py validation
```

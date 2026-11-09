"""Test 05: PV serves demand first; the residual goes to the battery and export.
Data: test_4_full_system.xlsx only. Usage: python3 test_05_pv_priority.py <validation_dir>"""
import sys
import numpy as np
import pandas as pd

d = sys.argv[1]
TOL = 1e-6
ts = pd.read_excel(f"{d}/test_4_full_system.xlsx", sheet_name="Time series")
col = lambda p: [c for c in ts.columns if c.startswith(p)][0]
t = pd.to_datetime(ts[col("time (Europe")])
imp, exp_ = ts[col("flow grid.import")], ts[col("flow el → grid.export")]
ch, dis = ts[col("flow el → store.charge")], ts[col("flow store.discharge")]
pv, curt, av = ts[col("flow prod.out")], ts[col("state prod.curtailment")], ts[col("input prod.profile")]
dem, e = ts[col("flow el → demand.in")], ts[col("state store.e")]
da, fi, fe = ts[col("input grid.import_price")], ts[col("input grid.import_fee")], ts[col("input grid.export_fee")]
lam = ts[col("price el")]
imp_cost, exp_val = da + fi, da - fe
dt = 1.0
E_MAX, P_CH = 2.0, 1.0

# allocation "PV to demand first"
pv_to_dem = np.minimum(pv, dem)
surplus = pv - pv_to_dem                       # PV left after demand
rest_dem = dem - pv_to_dem                     # demand not met by PV
avail_to_dem = np.minimum(av, dem)             # PV that COULD have served demand
short = avail_to_dem - pv_to_dem               # PV withheld from demand
A = av > TOL
S = surplus > TOL
o = {}
o["hours_pv"] = int(A.sum())
o["hours_pv_lt_demand"] = int((A & (av < dem - TOL)).sum())
o["hours_pv_ge_demand"] = int((av >= dem - TOL).sum())

# A: PV first to demand
viol_A = A & (short > TOL)
o["A_hours_pv_withheld_from_demand"] = int(viol_A.sum())
o["A_withheld_MWh"] = float(short[viol_A].sum())
o["A_withheld_with_import_cost_negative"] = int((viol_A & (imp_cost < 0)).sum())
o["A_withheld_with_import_cost_ge0"] = int((viol_A & (imp_cost >= 0)).sum())
o["A_PV_to_demand_MWh"] = float(pv_to_dem.sum()); o["A_PV_to_demand_possible_MWh"] = float(avail_to_dem.sum())

# B: in surplus hours, demand needs no import / discharge
o["B_surplus_hours"] = int(S.sum()); o["B_surplus_MWh"] = float(surplus.sum())
o["B_surplus_hours_with_import"] = int((S & (imp > TOL)).sum())
o["B_surplus_hours_with_discharge"] = int((S & (dis > TOL)).sum())

# C: export only from PV surplus
exp_from_pv = np.minimum(exp_, surplus)
exp_other = exp_ - exp_from_pv
C = exp_other > TOL
o["C_export_hours"] = int((exp_ > TOL).sum()); o["C_export_MWh"] = float(exp_.sum())
o["C_export_not_from_pv_hours"] = int(C.sum()); o["C_export_not_from_pv_MWh"] = float(exp_other.sum())
o["C_export_while_discharging_hours"] = int(((exp_ > TOL) & (dis > TOL)).sum())

# D: split of surplus (net of any import/discharge in the hour; in surplus hours import=discharge=0 if B holds)
sur_to_ch = np.minimum(ch, surplus)
sur_to_exp = np.minimum(exp_, surplus - sur_to_ch)
sur_curt_none = surplus - sur_to_ch - sur_to_exp
o["D_surplus_to_battery_MWh"] = float(sur_to_ch.sum())
o["D_surplus_to_export_MWh"] = float(sur_to_exp.sum())
o["D_surplus_unallocated_MWh"] = float(sur_curt_none.sum())
X = S & (exp_ > TOL)
room = (E_MAX - e.shift(1).fillna(1.0)).clip(lower=0)   # room at start of hour
can_charge_more = (ch < P_CH - TOL) & (e < E_MAX - TOL)
o["D_export_hours"] = int(X.sum())
o["D_export_while_battery_full"] = int((X & (e >= E_MAX - TOL)).sum())
o["D_export_while_charging_at_max"] = int((X & (ch >= P_CH - TOL)).sum())
o["D_export_while_battery_could_take_more"] = int((X & can_charge_more).sum())
o["D_charge_from_surplus_hours"] = int((S & (ch > TOL)).sum())
# all-PV-surplus-to-battery hours vs export
o["D_bus_price_in_export_hours_min_max"] = (float(lam[X].min()), float(lam[X].max()))
o["D_exp_val_in_export_hours_min_max"] = (float(exp_val[X].min()), float(exp_val[X].max()))

for k, v in o.items():
    print(f"{k}\t{v!r}")
show = pd.DataFrame({"time": t, "pv_av": av, "pv": pv, "curt": curt, "imp": imp, "exp": exp_, "ch": ch, "dis": dis,
                     "e": e, "DA": da, "DA+fi": imp_cost, "DA-fe": exp_val, "price_el": lam}).round(4)
print("\nA: PV WITHHELD FROM DEMAND, IMPORT COST >= 0"); print(show[viol_A & (imp_cost >= 0)].to_string(index=False))
print("\nB: SURPLUS HOURS WITH IMPORT/DISCHARGE"); print(show[S & ((imp > TOL) | (dis > TOL))].to_string(index=False))
print("\nC: EXPORT NOT FROM PV"); print(show[C].to_string(index=False))
print("\nD: EXPORT WHILE BATTERY COULD TAKE MORE"); print(show[X & can_charge_more].to_string(index=False))

# B detail: in surplus hours with import, does import only feed the battery (import <= charge - surplus share)?
Bimp = S & (imp > TOL)
print("\nB_import_le_charge", int((Bimp & (imp <= ch + TOL)).sum()), "of", int(Bimp.sum()),
      "| charging at 1 MW in those hours:", int((Bimp & (ch >= P_CH - TOL)).sum()),
      "| hours where charge hits e_max:", int((Bimp & (ch < P_CH - TOL) & (e >= E_MAX - TOL)).sum()))
# C detail: battery-to-grid hours, prices
print("C_bus_price_min_max", float(lam[C].min()), float(lam[C].max()),
      "| discharge min/max", float(dis[C].min()), float(dis[C].max()),
      "| PV in those hours max", float(pv[C].max()))
# D detail: for export hours where battery had room, is the battery charged later (before its next discharge)
# at a bus price <= this hour's bus price?  -> storing now would have been the more expensive way to fill it.
Dset = X & can_charge_more
ok = 0; rows = []
for i in ts.index[Dset]:
    j = i + 1; later = []
    while j < len(ts) and dis[j] <= TOL:
        if ch[j] > TOL: later.append(lam[j])
        j += 1
    m = max(later) if later else np.nan
    good = bool(later) and m <= lam[i] + 1e-6
    ok += good
    rows.append((str(t[i]), round(lam[i], 2), round(m, 2) if later else None, str(t[j]) if j < len(ts) else "-", good))
for r in rows: print(r)
print("D_battery_filled_later_cheaper", ok, "of", int(Dset.sum()))

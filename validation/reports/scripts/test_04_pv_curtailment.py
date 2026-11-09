"""Test 04: PV is curtailed when electricity cost is negative (and only then).
Data: test_4_full_system.xlsx only. Usage: python3 test_04_pv_curtailment.py <validation_dir>"""
import sys
import pandas as pd

d = sys.argv[1]
TOL = 1e-6
ts = pd.read_excel(f"{d}/test_4_full_system.xlsx", sheet_name="Time series")
col = lambda p: [c for c in ts.columns if c.startswith(p)][0]
t = pd.to_datetime(ts[col("time (Europe")])
imp, exp_ = ts[col("flow grid.import")], ts[col("flow el → grid.export")]
ch, dis = ts[col("flow el → store.charge")], ts[col("flow store.discharge")]
pv, curt, av = ts[col("flow prod.out")], ts[col("state prod.curtailment")], ts[col("input prod.profile")]
e = ts[col("state store.e")]
da, fi, fe = ts[col("input grid.import_price")], ts[col("input grid.import_fee")], ts[col("input grid.export_fee")]
pexp = ts[col("input grid.export_price")]
lam = ts[col("price el")]

imp_cost = da + fi            # cost of 1 MWh imported
exp_val = pexp - fe           # net value of 1 MWh exported
C = curt > TOL                # curtailed hours
A = av > TOL                  # PV available
full = curt >= av - TOL       # fully curtailed
o = {}
o["hours_pv_available"] = int(A.sum())
o["hours_curtailed"] = int(C.sum()); o["hours_fully_curtailed"] = int((C & full).sum())
o["curtailed_MWh"] = float(curt.sum())
o["hours_DA_negative"] = int((da < 0).sum())
o["hours_DA_negative_and_pv"] = int(((da < 0) & A).sum())
o["hours_import_cost_negative"] = int((imp_cost < 0).sum())
o["hours_import_cost_negative_and_pv"] = int(((imp_cost < 0) & A).sum())
o["hours_export_value_negative_and_pv"] = int(((exp_val < 0) & A).sum())

# Direction 1: every curtailed hour has a negative marginal value of electricity
o["curt_with_bus_price_gt0"] = int((C & (lam > TOL)).sum())
o["bus_price_in_curtailed_hours_min_max"] = (float(lam[C].min()), float(lam[C].max()))
o["curt_with_import_cost_negative"] = int((C & (imp_cost < 0)).sum())
o["curt_with_import_cost_ge0"] = int((C & (imp_cost >= 0)).sum())
# those with import cost >= 0: explain by export value < 0 (surplus that would otherwise be exported)
other = C & (imp_cost >= 0)
o["curt_other_export_value_negative"] = int((other & (exp_val < 0)).sum())
o["curt_other_unexplained"] = int((other & (exp_val >= 0)).sum())
o["curt_other_importing"] = int((other & (imp > TOL)).sum())
o["curt_other_battery_full"] = int((other & (e >= 2 - TOL)).sum())
o["curt_other_charging_at_max"] = int((other & (ch >= 1 - TOL)).sum())
o["curt_with_DA_ge0"] = int((C & (da >= 0)).sum())

# Direction 2: when import cost < 0 and PV available, PV should be fully curtailed
N = (imp_cost < 0) & A
o["neg_cost_pv_hours"] = int(N.sum())
o["neg_cost_pv_fully_curtailed"] = int((N & full).sum())
o["neg_cost_pv_not_fully_curtailed"] = int((N & ~full).sum())
o["neg_cost_pv_used_MWh"] = float(pv[N].sum())
# when import cost < 0: is import maxed out given sinks? (import = demand + charge, no export)
o["neg_cost_import_min_max"] = (float(imp[N].min()), float(imp[N].max()))
o["neg_cost_export_max"] = float(exp_[N].max())
# Direction 2b: DA < 0 but import cost >= 0 -> PV NOT expected to be curtailed if site imports
M = (da < 0) & (imp_cost >= 0) & A
o["DAneg_costpos_pv_hours"] = int(M.sum())
o["DAneg_costpos_curtailed"] = int((M & C).sum())
# Complementarity: PV used > 0 -> bus price >= 0
o["pv_used_with_bus_price_lt0"] = int(((pv > TOL) & (lam < -TOL)).sum())
o["bus_price_lt0_and_pv_not_fully_curtailed"] = int(((lam < -TOL) & A & ~full).sum())

for k, v in o.items():
    print(f"{k}\t{v!r}")

# detail tables
show = pd.DataFrame({"time": t, "pv_avail": av, "pv_used": pv, "curt": curt, "import": imp, "export": exp_,
                     "charge": ch, "discharge": dis, "e": e, "DA": da, "DA+fee_imp": imp_cost,
                     "DA-fee_exp": exp_val, "price_el": lam})
print("\nCURTAILED, IMPORT COST >= 0"); print(show[other].round(4).to_string(index=False))
print("\nNEG IMPORT COST, PV NOT FULLY CURTAILED"); print(show[N & ~full].round(4).to_string(index=False))
print("\nMONTHLY CURTAILMENT"); print(show[C].groupby(show.time.dt.month).agg(hours=("curt", "size"), MWh=("curt", "sum")).round(3).to_string())

# For partial-curtailment hours with battery not full: does the battery reach e_max later the same day
# BEFORE its next discharge? If yes, charging earlier has zero value (bus price 0, a tie).
print("\nBATTERY-NOT-FULL CURTAILED HOURS: does battery fill later that day before discharging?")
day = t.dt.date
res = []
for i in ts.index[other & (e < 2 - TOL)]:
    j = i + 1; filled = False
    while j < len(ts) and day[j] == day[i]:
        if dis[j] > TOL: break
        if e[j] >= 2 - TOL: filled = True; break
        j += 1
    res.append((str(t[i]), round(float(e[i]), 4), filled, str(t[j]) if j < len(ts) else "-"))
for r in res: print(r)
print("filled_before_next_discharge", sum(r[2] for r in res), "of", len(res))

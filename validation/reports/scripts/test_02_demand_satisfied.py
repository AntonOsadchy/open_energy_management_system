"""Test 02: demand is always satisfied. Data: test_4_full_system.xlsx only.
Usage: python3 test_02_demand_satisfied.py <validation_dir>"""
import sys
import openpyxl
import pandas as pd

d = sys.argv[1]
f = f"{d}/test_4_full_system.xlsx"
TOL = 1e-6  # MW (README §5)

# Parameters read with openpyxl so numeric cells are not coerced to bool by pandas
P = {}
for r in openpyxl.load_workbook(f, read_only=True)["Parameters"].iter_rows(min_row=2, values_only=True):
    P[(r[0], r[1])] = r[2]
demand_mw = P[("Demand demand", "profile")]
max_imp = P[("GridConnection grid", "max_import")]
p_dis = P[("Storage store", "p_dis_max")]
e_max = P[("Storage store", "e_max")]
soc_min = P[("Storage store", "soc_min")]

ts = pd.read_excel(f, sheet_name="Time series")
col = lambda p: [c for c in ts.columns if c.startswith(p)][0]
dem = ts[col("flow el → demand.in")]
imp = ts[col("flow grid.import → el")]
exp_ = ts[col("flow el → grid.export")]
ch = ts[col("flow el → store.charge")]
dis = ts[col("flow store.discharge → el")]
pv = ts[col("flow prod.out → el")]
pv_av = ts[col("input prod.profile")]
curt = ts[col("state prod.curtailment")]
e = ts[col("state store.e")]
dt = ts[col("step_h")]
tl = pd.to_datetime(ts[col("time (Europe")])
tu = pd.to_datetime(ts[col("time UTC")])

o = {}
o["param_demand_profile_MW"] = demand_mw
o["param_types"] = {k: type(v).__name__ for k, v in P.items() if k[0] in ("Demand demand", "Storage store")}
o["slack_columns"] = [c for c in ts.columns if "slack" in c.lower() or "unserved" in c.lower() or "shed" in c.lower()]
o["bus_note"] = P.get(("bus", "el"))

# 1. coverage of the horizon
o["n_rows"] = len(ts)
o["utc_first"], o["utc_last"] = str(tu.iloc[0]), str(tu.iloc[-1])
gaps = tu.diff().dropna()
o["utc_step_counts"] = gaps.value_counts().to_dict()
o["utc_duplicates"] = int(tu.duplicated().sum())
o["step_h_unique"] = sorted(dt.unique().tolist())
loc_day = tl.dt.date.value_counts()
o["local_days"] = len(loc_day)
o["local_days_not_24h"] = {str(k): int(v) for k, v in loc_day[loc_day != 24].items()}
o["demand_NaN"] = int(dem.isna().sum())

# 2. delivered = profile every hour
dev = dem - demand_mw
o["demand_min"], o["demand_max"] = dem.min(), dem.max()
o["demand_max_abs_dev"] = dev.abs().max()
o["demand_hours_below_profile"] = int((dev < -TOL).sum())
o["demand_hours_above_profile"] = int((dev > TOL).sum())
o["demand_energy_MWh"] = (dem * dt).sum()
o["demand_energy_expected_MWh"] = (demand_mw * dt).sum()

# 3. bus balance: inflows = outflows
inflow = imp + dis + pv
outflow = exp_ + ch + dem
bal = inflow - outflow
o["balance_max_abs"] = bal.abs().max()
o["balance_hours_violated"] = int((bal.abs() > TOL).sum())
o["balance_worst_hour"] = str(tl.iloc[bal.abs().idxmax()])

# 4. supply sources physically valid
o["min_flow_any"] = min(imp.min(), exp_.min(), ch.min(), dis.min(), pv.min(), dem.min())
o["neg_flows_beyond_tol"] = int(((pd.concat([imp, exp_, ch, dis, pv, dem], axis=1)) < -TOL).sum().sum())
o["import_max"] = imp.max(); o["import_over_limit_h"] = int((imp > max_imp + TOL).sum())
o["discharge_max"] = dis.max(); o["discharge_over_limit_h"] = int((dis > p_dis + TOL).sum())
o["pv_used_over_available_h"] = int((pv > pv_av + TOL).sum())
o["pv_used+curt_vs_available_max_abs"] = (pv + curt - pv_av).abs().max()
o["e_min"] = e.min(); o["e_below_min_h"] = int((e < soc_min * e_max - TOL).sum())

# 5. how demand is met (energy attribution, net of the battery and export)
o["supply_import_MWh"] = (imp * dt).sum()
o["supply_discharge_MWh"] = (dis * dt).sum()
o["supply_pv_MWh"] = (pv * dt).sum()
o["sink_charge_MWh"] = (ch * dt).sum()
o["sink_export_MWh"] = (exp_ * dt).sum()
o["hours_import_at_limit"] = int((imp > max_imp - TOL).sum())
o["hours_no_import"] = int((imp < TOL).sum())
# hours where demand was met with no grid import at all (PV and/or battery only)
o["hours_demand_met_without_grid"] = int(((imp < TOL) & (dem >= demand_mw - TOL)).sum())
# minimum headroom: could the grid alone have covered demand in every hour?
o["grid_limit_ge_demand"] = max_imp >= demand_mw

for k, v in o.items():
    print(f"{k}\t{v!r}")
